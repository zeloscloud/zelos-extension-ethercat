"""One EtherCAT segment with the agent as master: SOEM's simple_test (main + ecatcheck)
over pysoem, or the simulator for `demo`.

The cycle thread owns every state read and write; actions post state requests to it and
reach the mailbox under `_mbx_lock`. pysoem releases the GIL only around process data,
SDO and FoE; every other SOEM call holds it. `reconfig` is the long one (up to 6 s), so
the check loop tries it at most every RECONFIG_BACKOFF per slave; simple_test runs its
check on another thread and never stalls its cycle for it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import threading
import time
import warnings
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import zelos_sdk
from zelos_sdk import DataType

from zelos_extension_ethercat import pdo as pdo_mod
from zelos_extension_ethercat import protocol
from zelos_extension_ethercat.pdo import FmmuSpan, SiiImage, SlaveMap

logger = logging.getLogger(__name__)

DEMO_INTERFACE = "demo"

#: SOEM timeouts, us (ethercattype.h).
EC_TIMEOUTRET = 2000
EC_TIMEOUTSTATE = 2_000_000
EC_TIMEOUTMON = 500
#: simple_test: EC_TIMEOUTSTATE x 4 for SAFEOP, 200 x (cycle + 50 ms) for OP; polled here.
SAFEOP_TIMEOUT = 8.0
OP_TIMEOUT = 10.0
STATE_POLL = 0.01
#: Slave check cadence while the working counter is short (simple_test: 10 ms).
CHECK_INTERVAL = 0.1
#: Seconds between `reconfig` attempts on one slave: each pauses process data for the
#: whole segment while it holds the GIL, and every 100 ms made the pause continuous.
RECONFIG_BACKOFF = 10.0
#: Process data watchdog programmed into output slaves. SOEM and IgH write nothing, so
#: the ESC reset value (100 ms) applies there; 500 ms lengthens it to leave room for
#: Python pauses on small CPUs while still dropping outputs within half a second of the
#: master dying. The cycle treats as long a gap between complete exchanges as a drop-out.
OUTPUT_WATCHDOG_MS = 500.0
#: At most one "still recovering" summary per this many seconds; the per-state warnings
#: are once per slave per episode out of OP.
SUMMARY_INTERVAL = 60.0
#: GIL switch interval for the process: the default 5 ms lets another thread keep the
#: GIL that long after the cycle thread's sleep or frame wait returns.
SWITCH_INTERVAL = 0.001
#: The upward transitions a slave accepts, in order (ETG.1000.6 Table 9).
_LADDER = (protocol.PREOP, protocol.SAFEOP, protocol.OP)

MASTER_EVENT = "_master"
STATE_EVENT = "_state"

MASTER_FIELDS = [
    ("slaves", DataType.UInt16),
    ("al_state", DataType.UInt8),
    ("al_state_name", DataType.String),
    ("wkc", DataType.Int32),
    ("expected_wkc", DataType.Int32),
    ("wkc_ok", DataType.Boolean),
    ("wkc_errors", DataType.UInt64),
    ("lost_frames", DataType.UInt64),
    ("cycles", DataType.UInt64),
    ("cycle_time_us", DataType.Float64),
    ("jitter_us", DataType.Float64),
    ("dc_time_ns", DataType.Int64),
]
STATE_FIELDS = [
    ("al_state", DataType.UInt8),
    ("al_state_name", DataType.String),
    ("al_status_code", DataType.UInt16),
    ("al_status_text", DataType.String),
    ("lost", DataType.Boolean),
    ("outputs_cleared", DataType.Boolean),
    ("reconfigs", DataType.UInt32),
    ("error_acks", DataType.UInt32),
]


class SegmentError(Exception):
    """Bring-up or an action failed; one line for the user."""


def open_backend(interface: str) -> Any:
    """A pysoem Master on `interface`, or the simulator for `demo`."""
    if interface == DEMO_INTERFACE:
        from zelos_extension_ethercat.demo.simulator import SimMaster

        return SimMaster()
    import pysoem

    # Process data, SDO and FoE calls release the GIL: a long SDO on the actions thread
    # must not stall the cycle (the slaves' SM watchdog, 100 ms default, would trip).
    pysoem.settings.always_release_gil = True
    master = pysoem.Master()
    try:
        master.open(interface)
    except ConnectionError as e:
        raise SegmentError(
            f"cannot open {interface}: {e}. The interface must exist and the extension "
            "needs CAP_NET_RAW and CAP_NET_ADMIN (or root) to send raw EtherCAT frames"
        ) from e
    return master


@dataclass
class SlaveInfo:
    """One slave: identity, mapping, trace names, last state."""

    position: int
    name: str
    vendor_id: int
    product_code: int
    revision: int
    alias: int = 0
    trace_name: str = ""
    map: SlaveMap = field(default_factory=SlaveMap)
    input_span: FmmuSpan = FmmuSpan()
    output_span: FmmuSpan = FmmuSpan()
    input_problem: str = ""  # why inputs are not logged ("" = fine); `pdo.span_problem`
    output_problem: str = ""  # why writes are refused
    requested_state: int = protocol.OP  # below OP the check loop leaves the slave there
    al_state: int = 0
    al_status_code: int = 0
    lost: bool = False
    outputs_cleared: bool = False  # zeroed after a drop-out, nothing written since
    reconfigs: int = 0  # `reconfig` attempts since start
    error_acks: int = 0  # SAFEOP+ERROR acknowledgements since start
    reconfig_at: float = 0.0  # monotonic of the last attempt; 0 once OP or targeted
    stepping: bool = False  # below its held state and being stepped back up to it
    warned: set[int] = field(default_factory=set)  # states warned about since last in OP
    state_event: str = ""
    events: dict[str, pdo_mod.Pdo] = field(default_factory=dict)  # full event name -> PDO

    def describe(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "name": self.name,
            "trace_name": self.trace_name,
            "vendor_id": f"0x{self.vendor_id:08x}",
            "product_code": f"0x{self.product_code:08x}",
            "revision": f"0x{self.revision:08x}",
            "alias": self.alias,
            "al_state": protocol.al_state_name(self.al_state),
            "al_status_code": f"0x{self.al_status_code:04x}",
            "al_status_text": protocol.al_status_text(self.al_status_code),
            "lost": self.lost,
            "outputs_cleared": self.outputs_cleared,
            "reconfigs": self.reconfigs,
            "error_acks": self.error_acks,
            "mapping": self.map.source,
            "input_bits": self.map.input_bits,
            "output_bits": self.map.output_bits,
            "input_problem": self.input_problem,
            "output_problem": self.output_problem,
            "pdos": [p.event for p in self.map.pdos()],
        }


@dataclass
class StateRequest:
    """A `request_state`, served by the cycle thread one ladder rung at a time."""

    wanted: int
    info: SlaveInfo | None  # None: every slave
    steps: list[int] = field(default_factory=list)
    deadline: float = 0.0
    reached: int = protocol.NONE
    done: threading.Event = field(default_factory=threading.Event)


def _needs(state: int, step: int, wanted: int) -> bool:
    """Whether a slave at `state` is written `step`: below it on the way up; anywhere but
    at it on the last rung (a downward move is one write)."""
    current = state & 0x0F
    return current < step if step < wanted else current != step


class Segment:
    """One connection: a master on one interface, its slaves and their trace events."""

    def __init__(
        self,
        name: str,
        interface: str,
        cycle_time_ms: float,
        enable_outputs: bool,
        source: zelos_sdk.TraceSource,
        event_prefix: str | None,
        diagnostics_interval: float = 1.0,
        restore_outputs: bool = False,
    ) -> None:
        self.name = name
        self.interface = interface
        self.cycle_time_ms = cycle_time_ms
        self.enable_outputs = enable_outputs
        self.restore_outputs = restore_outputs
        self.source = source
        self.event_prefix = event_prefix
        self.diagnostics_interval = diagnostics_interval

        self.master: Any = None
        self.slaves: list[SlaveInfo] = []
        self.expected_wkc = 0
        self.state = "stopped"  # starting | running | failed | stopping | stopped
        self.error = ""
        self.cycles = 0
        self.wkc = 0
        self.wkc_errors = 0
        self.lost_frames = 0
        self.al_state = 0
        self.cycle_time_us = 0.0
        self.jitter_us = 0.0

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._mbx_lock = threading.Lock()
        self._out_lock = threading.Lock()
        self._staged: dict[int, bytearray] = {}  # position -> output image (pysoem view)
        self._dirty = False
        self._docheck = False
        self._requested_state = protocol.OP  # below OP the check loop stops pushing to OP
        self._requests: deque[StateRequest] = deque()
        self._request: StateRequest | None = None
        self._last_logged: dict[str, tuple] = {}
        self._summary_at = 0.0

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    async def run(self, stop: asyncio.Event) -> None:
        """Bring the segment up and exchange process data until `stop`.

        :raises SegmentError: bring-up failed (first contact is required)
        """
        self.state = "starting"
        # Daemon: the app's forced exit must not wait on a GIL-holding SOEM call.
        self._thread = threading.Thread(
            target=self._thread_main, name=f"ethercat-{self.name}", daemon=True
        )
        self._thread.start()
        while not self._ready.is_set() and self._thread.is_alive() and not stop.is_set():
            await asyncio.sleep(0.05)
        if not self._ready.is_set():
            self._stop.set()
            await asyncio.to_thread(self._thread.join, 5.0)
            if stop.is_set():
                return
            raise SegmentError(self.error or "stopped before the segment came up")
        stopping = asyncio.create_task(stop.wait())
        thread_done = asyncio.create_task(asyncio.to_thread(self._thread.join))
        await asyncio.wait([stopping, thread_done], return_when=asyncio.FIRST_COMPLETED)
        stopping.cancel()
        self._stop.set()
        await thread_done

    def _thread_main(self) -> None:
        sys.setswitchinterval(min(sys.getswitchinterval(), SWITCH_INTERVAL))
        try:
            self._bring_up()
        except Exception as e:
            if self._stop.is_set():
                logger.info("[%s] %s", self.name, e)
            else:
                self.state, self.error = "failed", str(e)
                logger.error("[%s] %s", self.name, e)
            self._close()
            return
        self.state = "running"
        self._ready.set()
        try:
            self._cycle_loop()
        except Exception as e:  # a bug or a vanished interface: the task ends, the app sees it
            self.state, self.error = "failed", str(e)
            logger.exception("[%s] cycle stopped: %s", self.name, e)
        else:
            self.state = "stopping"
        self._bring_down()

    def _close(self) -> None:
        with contextlib.suppress(Exception):
            if self.master is not None:
                self.master.close()
        if self.state != "failed":
            self.state = "stopped"

    # ─── Bring-up (simple_test main) ─────────────────────────────────────────

    def _bring_up(self) -> None:
        self.master = open_backend(self.interface)
        count = self.master.config_init()
        if count <= 0:
            raise SegmentError(f"no EtherCAT slaves answered on {self.interface}")
        logger.info("[%s] %d slaves found on %s", self.name, count, self.interface)
        self.slaves = [self._describe_slave(pos, s) for pos, s in enumerate(self.master.slaves, 1)]
        try:
            self.master.config_map()
        except Exception as e:  # pysoem ConfigMapError: the map is done, SOEM's CoE reads failed
            if type(e).__name__ != "ConfigMapError":
                raise
            logger.warning(
                "[%s] config_map: %s; mapping kept, FMMU spans checked per slave",
                self.name,
                "; ".join(str(x) for x in getattr(e, "error_list", [])) or e,
            )
        self.master.config_dc()
        for info, slave in zip(self.slaves, self.master.slaves, strict=True):
            self._check_spans(info, slave)
        self._register_events()
        if not self._wait_state(protocol.SAFEOP, SAFEOP_TIMEOUT):
            raise SegmentError(f"not every slave reached SAFEOP: {self._laggards(protocol.SAFEOP)}")
        # One zero exchange before OP, as simple_test does.
        self._stage_zero()
        self._apply_outputs()
        self.master.send_processdata()
        self.master.receive_processdata(EC_TIMEOUTRET)
        self.master.state = protocol.OP
        self.master.write_state()
        if not self._wait_state(protocol.OP, OP_TIMEOUT, exchange=True):
            raise SegmentError(f"not every slave reached OP: {self._laggards(protocol.OP)}")
        self._read_states()
        self._refresh_states()
        logger.info(
            "[%s] OP, expected working counter %d (pysoem %d), cycle %g ms, outputs %s",
            self.name,
            self.expected_wkc,
            self.master.expected_wkc,
            self.cycle_time_ms,
            "enabled" if self.enable_outputs else "disabled",
        )

    def _wait_state(self, wanted: int, timeout: float, exchange: bool = False) -> bool:
        """Poll `read_state` until every slave reports `wanted`, the deadline, or a stop;
        `exchange` keeps process data flowing (slaves leave SAFEOP only with valid outputs).

        :raises SegmentError: stopped while waiting
        """
        deadline = time.monotonic() + timeout
        period = self.cycle_time_ms / 1000 if exchange else STATE_POLL
        while True:
            if exchange:
                self.master.send_processdata()
                self.master.receive_processdata(EC_TIMEOUTRET)
            if self.master.read_state() == wanted:
                return True
            if self._stop.is_set():
                raise SegmentError("stopped during bring-up")
            if time.monotonic() >= deadline:
                return False
            time.sleep(period)

    def _check_spans(self, info: SlaveInfo, slave: Any) -> None:
        """FMMU spans, and whether pysoem's view reaches them; a problem is logged once."""
        info.input_span, info.output_span = pdo_mod.read_fmmus(slave._fprd)
        info.input_problem = pdo_mod.span_problem(
            info.input_span, info.map.input_bits, len(slave.input)
        )
        info.output_problem = pdo_mod.span_problem(
            info.output_span, info.map.output_bits, len(slave.output)
        )
        if info.input_problem:
            logger.warning(
                "[%s] %s: inputs not logged: %s", self.name, info.trace_name, info.input_problem
            )
        if not info.output_problem:
            info.output_problem = self._arm_watchdog(info, slave)
        if info.output_problem:
            logger.warning(
                "[%s] %s: outputs refused: %s", self.name, info.trace_name, info.output_problem
            )

    def _arm_watchdog(self, info: SlaveInfo, slave: Any) -> str:
        """OUTPUT_WATCHDOG_MS into an output slave (0x0420 resets on a power cycle, so again
        after every reconfig / recover); why it failed, or ""."""
        if not (self.enable_outputs and info.map.output_bits):
            return ""
        try:
            with warnings.catch_warnings():  # resolution notice from pysoem
                warnings.simplefilter("ignore")
                slave.set_watchdog("processdata", OUTPUT_WATCHDOG_MS)
        except Exception as e:  # noqa: BLE001
            return f"process data watchdog not set: {e}"
        return ""

    def _rearm_watchdog(self, info: SlaveInfo, slave: Any) -> None:
        if not info.output_problem and (problem := self._arm_watchdog(info, slave)):
            logger.warning(
                "[%s] %s: %s; the ESC default (100 ms) applies", self.name, info.trace_name, problem
            )

    def _laggards(self, wanted: int) -> str:
        self.master.read_state()
        self._refresh_states()
        return "; ".join(
            f"{s.trace_name} {protocol.al_state_name(s.al_state)} "
            f"(0x{s.al_status_code:04x} {protocol.al_status_text(s.al_status_code)})"
            for s in self.slaves
            if s.al_state != wanted
        )

    def _describe_slave(self, position: int, slave: Any) -> SlaveInfo:
        info = SlaveInfo(position, slave.name, slave.man, slave.id, slave.rev)
        info.trace_name = pdo_mod.slave_trace_name(position, slave.name, slave.id)
        with contextlib.suppress(Exception):
            info.alias = int.from_bytes(slave._fprd(protocol.REG_STATION_ALIAS, 2), "little")
        info.map = self._read_map(info, slave)
        pdo_mod.assign_names(info.map)
        with contextlib.suppress(Exception):  # delivered during this slave's mailbox calls
            slave.add_emergency_callback(
                lambda emcy, name=info.trace_name: logger.warning(
                    "[%s] %s EMCY 0x%04x, error register 0x%02x, data %02x %04x %04x",
                    self.name,
                    name,
                    emcy.error_code,
                    emcy.error_reg,
                    emcy.b1,
                    emcy.w1,
                    emcy.w2,
                )
            )
        logger.info(
            "[%s] %s: vendor 0x%08x product 0x%08x rev 0x%08x, %s mapping, %d in / %d out bits",
            self.name,
            info.trace_name,
            info.vendor_id,
            info.product_code,
            info.revision,
            info.map.source,
            info.map.input_bits,
            info.map.output_bits,
        )
        return info

    def _read_map(self, info: SlaveInfo, slave: Any) -> SlaveMap:
        """CoE where the SII says the slave has that mailbox, else the SII PDO categories."""
        sii = SiiImage(slave.eeprom_read)
        try:
            has_coe = bool(sii.mailbox_protocols() & pdo_mod.MBX_COE)
        except Exception as e:
            logger.warning(
                "[%s] %s: SII unreadable (%s); mapping by CoE", self.name, info.trace_name, e
            )
            has_coe = True
        if has_coe:
            discarded: list[Exception] = []
            try:
                lookup = self._od_lookup(info, slave, discarded)
                return pdo_mod.read_coe_map(slave.sdo_read, lookup)
            except Exception as e:  # any failure of the walk: the SII is next
                logger.info("[%s] %s: %s; mapping from SII", self.name, info.trace_name, e)
            finally:
                discarded += self._drain_errors()
                if discarded:
                    logger.info(
                        "[%s] %s: %d mailbox errors from the CoE walk discarded (first: %s)",
                        self.name,
                        info.trace_name,
                        len(discarded),
                        discarded[0],
                    )
        try:
            return pdo_mod.read_sii_map(sii)
        except Exception as e:
            logger.warning("[%s] %s: no PDO mapping (%s)", self.name, info.trace_name, e)
            return SlaveMap()

    def _od_lookup(
        self, info: SlaveInfo, slave: Any, discarded: list[Exception]
    ) -> pdo_mod.OdLookup | None:
        """Names and types from the OD when the slave serves SDO info. `slave.od` holds the
        GIL for the walk (EC_TIMEOUTRXM 700 ms per failed exchange); runs before the cycle.
        Its errors are drained into `discarded` after every read, before the next SDO."""
        try:
            objects = {obj.index: obj for obj in slave.od}
        except Exception as e:
            logger.info(
                "[%s] %s: no SDO info (%s); entries named by index", self.name, info.trace_name, e
            )
            return None

        def lookup(index: int, subindex: int) -> tuple[str, int] | None:
            obj = objects.get(index)
            if obj is None:
                return None
            try:
                with contextlib.suppress(Exception):
                    entries = obj.entries
                    if entries and subindex < len(entries):
                        return str(entries[subindex].name), int(entries[subindex].data_type)
                    if subindex == 0:
                        return str(obj.name), int(obj.data_type)
            finally:
                discarded.extend(self._drain_errors())
            return None

        discarded.extend(self._drain_errors())
        return lookup

    def _drain_errors(self) -> list[Exception]:
        """SOEM keeps one error ring per master, and the OD walk (SDO info) never pops
        it: pysoem's next SDO on any slave would raise those errors, or assert on another
        slave's (pysoem.pyx:907-913)."""
        collect = getattr(self.master, "_collect_mailbox_errors", None)  # pysoem 1.1.13
        if collect is None:
            return []
        try:
            return collect()
        except Exception as e:  # noqa: BLE001
            return [e]

    def _event(self, *parts: str) -> str:
        return "/".join(p for p in (self.event_prefix, *parts) if p)

    def _register_events(self) -> None:
        self.source.add_event(
            self._event(MASTER_EVENT),
            [zelos_sdk.TraceEventFieldMetadata(n, t) for n, t in MASTER_FIELDS],
        )
        for info in self.slaves:
            info.state_event = self._event(info.trace_name, STATE_EVENT)
            self.source.add_event(
                info.state_event, [zelos_sdk.TraceEventFieldMetadata(n, t) for n, t in STATE_FIELDS]
            )
            for pdo in info.map.pdos():
                fields = [
                    zelos_sdk.TraceEventFieldMetadata(e.field, e.kind)
                    for e in pdo.entries
                    if e.field
                ]
                if not fields:
                    continue
                event = self._event(info.trace_name, pdo.event)
                self.source.add_event(event, fields)
                info.events[event] = pdo

    # ─── Cycle (simple_test loop + ecatcheck) ────────────────────────────────

    def _cycle_loop(self) -> None:
        period_ns = int(self.cycle_time_ms * 1_000_000)
        timeout_us = max(100, min(EC_TIMEOUTRET, period_ns // 2000))
        next_ns = time.perf_counter_ns()
        last_check = last_diag = last_cycle = last_ok = next_ns
        stalled = False
        period_sum = period_count = jitter_max = 0
        self._summary_at = time.monotonic()
        while not self._stop.is_set():
            now = time.perf_counter_ns()
            if now < next_ns:
                time.sleep((next_ns - now) / 1e9)
                now = time.perf_counter_ns()
            jitter_max = max(jitter_max, abs(now - next_ns))
            next_ns += period_ns
            if now > next_ns:  # a whole period late: resync rather than burst
                next_ns = now + period_ns
            period_sum += now - last_cycle
            period_count += 1
            last_cycle = now

            # Before the next send: an ESC-only slave stays in OP through a watchdog trip
            # and the next write would drive the old outputs again.
            if not stalled and now - last_ok >= OUTPUT_WATCHDOG_MS * 1e6:
                stalled = True
                self._stall((now - last_ok) / 1e6)
            self._apply_outputs()
            self.master.send_processdata()
            self.wkc = self.master.receive_processdata(timeout_us)
            t_ns = time.time_ns()
            self.cycles += 1
            if self.wkc < 0:
                self.lost_frames += 1
            elif self.wkc < self.expected_wkc:
                self.wkc_errors += 1
            else:
                last_ok, stalled = now, False
                self._log_process_data(t_ns)

            if (
                self.wkc < self.expected_wkc or self._docheck
            ) and now - last_check >= CHECK_INTERVAL * 1e9:
                last_check = now
                self._check_slaves(t_ns)
            self._serve_request()
            if now - last_diag >= self.diagnostics_interval * 1e9:
                last_diag = now
                self.cycle_time_us = period_sum / period_count / 1000
                self.jitter_us = jitter_max / 1000
                period_sum = period_count = jitter_max = 0
                self._diagnostics_tick(t_ns)

    def _stall(self, gap_ms: float) -> None:
        """No complete exchange for OUTPUT_WATCHDOG_MS: every output slave's watchdog has
        tripped, so its outputs drop as for a drop-out (`_drop_outputs`)."""
        logger.warning(
            "[%s] no complete exchange for %.0f ms (output watchdog %g ms); treating as a "
            "drop-out of every output slave",
            self.name,
            gap_ms,
            OUTPUT_WATCHDOG_MS,
        )
        for info in self.slaves:
            if info.map.output_bits:
                self._drop_outputs(info)

    def _bring_down(self) -> None:
        """Outputs to zero for one cycle, every slave to INIT, interface closed."""
        with contextlib.suppress(Exception):
            self._stage_zero()
            self._apply_outputs()
            self.master.send_processdata()
            self.master.receive_processdata(EC_TIMEOUTRET)
            self.master.state = protocol.INIT
            self.master.write_state()
        self._close()
        logger.info("[%s] stopped", self.name)

    def _check_slaves(self, t_ns: int) -> None:
        """simple_test's ecatcheck. A held slave found below its held state is stepped
        back up to it, never above (`_step_held`); any slave while a state request is in
        flight is only watched for loss. `reconfig` blocks the cycle up to 6 s (SOEM
        steps INIT -> PREOP -> SAFEOP at EC_TIMEOUTSTATE each), so one slave gets it at
        most every RECONFIG_BACKOFF and is only observed in between; the rest is bounded
        by EC_TIMEOUTRET or EC_TIMEOUTMON per frame."""
        self._read_states()
        now = time.monotonic()
        docheck = False
        for info, slave in zip(self.slaves, self.master.slaves, strict=True):
            state = slave.state
            if state != protocol.OP:
                self._drop_outputs(info)
            held = self._request is not None or info.requested_state != protocol.OP
            if state != protocol.OP and not held:
                docheck = True
                if state == protocol.SAFEOP + protocol.ERROR_FLAG:
                    self._warn_once(info, slave, "acknowledging")
                    info.error_acks += 1
                    slave.state = protocol.SAFEOP + protocol.ERROR_FLAG  # ACK shares the bit
                    slave.write_state()
                elif state == protocol.SAFEOP:
                    self._warn_once(info, slave, "requesting OP")
                    slave.state = protocol.OP
                    slave.write_state()
                elif state > protocol.NONE and now - info.reconfig_at >= RECONFIG_BACKOFF:
                    self._reconfig(info, slave, now)
            elif (
                self._request is None
                and state > protocol.NONE
                and (state & 0x0F) < info.requested_state
            ):
                docheck = True
                self._step_held(info, slave, now)
            if state == protocol.NONE and not slave.is_lost:
                slave.state_check(protocol.OP, EC_TIMEOUTRET)
                if slave.state == protocol.NONE:
                    slave.is_lost = True
                    logger.error("[%s] %s lost", self.name, info.trace_name)
            if slave.is_lost:
                docheck = True
                if slave.state == protocol.NONE:
                    if slave.recover(EC_TIMEOUTMON):
                        slave.is_lost = False
                        self._rearm_watchdog(info, slave)
                        logger.info("[%s] %s recovered", self.name, info.trace_name)
                else:
                    slave.is_lost = False
                    logger.info("[%s] %s found", self.name, info.trace_name)
        if self._docheck and not docheck and self._requested_state == protocol.OP:
            logger.info("[%s] all slaves resumed OP", self.name)
        self._docheck = docheck
        self._refresh_states()
        self._log_states(t_ns, force=False)
        if docheck and now - self._summary_at >= SUMMARY_INTERVAL:
            self._summary_at = now
            out = [
                f"{i.trace_name} {protocol.al_state_name(i.al_state)} "
                f"({i.error_acks} acks, {i.reconfigs} reconfigs)"
                for i in self.slaves
                if i.al_state != protocol.OP
            ]
            if out:
                logger.warning("[%s] still out of OP: %s", self.name, "; ".join(out))

    def _warn_once(self, info: SlaveInfo, slave: Any, action: str) -> None:
        """Once per slave per state until it is next in OP; the summary repeats it."""
        if slave.state in info.warned:
            return
        info.warned.add(slave.state)
        logger.warning(
            "[%s] %s in %s 0x%04x %s; %s",
            self.name,
            info.trace_name,
            protocol.al_state_name(slave.state),
            slave.al_status,
            protocol.al_status_text(slave.al_status),
            action,
        )

    def _reconfig(self, info: SlaveInfo, slave: Any, now: float) -> int:
        """`ecx_reconfig_slave`: INIT, SMs, PREOP, SAFEOP, FMMUs (ethercatconfig.c:1612);
        SAFEOP at most, OP is the check loop's next pass. Pauses process data (GIL)."""
        state = slave.state
        info.reconfig_at = now
        info.reconfigs += 1
        reached = slave.reconfig(EC_TIMEOUTMON)
        if reached:
            slave.is_lost = False
            self._rearm_watchdog(info, slave)
        logger.warning(
            "[%s] %s in %s: reconfig paused process data for %.0f ms, reached %s"
            " (attempt %d, next in %.0f s)",
            self.name,
            info.trace_name,
            protocol.al_state_name(state),
            (time.monotonic() - now) * 1000,
            protocol.al_state_name(reached),
            info.reconfigs,
            RECONFIG_BACKOFF,
        )
        return reached

    def _step_held(self, info: SlaveInfo, slave: Any, now: float) -> None:
        """A held slave below its held state (TwinCAT's Auto Restore States, never above
        it): `reconfig` first, since after a power cycle `recover` restores only the
        station address and reconfig the SM and FMMU registers; it stops at SAFEOP, so a
        slave held lower is then written down. Once per RECONFIG_BACKOFF; an ERROR flag is
        acknowledged first."""
        state = slave.state
        if state & protocol.ERROR_FLAG:
            self._warn_once(info, slave, "acknowledging")
            info.error_acks += 1
            slave.write_state()  # ACK shares the ERROR bit
            return
        if now - info.reconfig_at < RECONFIG_BACKOFF:
            return
        if not info.stepping:
            info.stepping = True
            logger.warning(
                "[%s] %s in %s below its held %s; reconfiguring",
                self.name,
                info.trace_name,
                protocol.al_state_name(state),
                protocol.al_state_name(info.requested_state),
            )
        reached = self._reconfig(info, slave, now)
        if (reached & 0x0F) > info.requested_state:
            slave.state = info.requested_state
            slave.write_state()

    def _targets(self, req: StateRequest) -> list[tuple[SlaveInfo, Any]]:
        if req.info is None:
            return list(zip(self.slaves, self.master.slaves, strict=True))
        return [(req.info, self.master.slaves[req.info.position - 1])]

    def _serve_request(self) -> None:
        """One step of the pending state request: the hold first, then each rung of the
        ladder written and polled, EC_TIMEOUTSTATE per rung."""
        req = self._request
        if req is None:
            if not self._requests:
                return
            req = self._request = self._requests.popleft()
            if req.info is None:
                self._requested_state = req.wanted
            for info, _ in self._targets(req):
                info.requested_state = req.wanted
                info.reconfig_at = 0.0  # a request is a fresh start for the backoff
                info.stepping = False
            if req.wanted != protocol.OP:
                self._stage_zero(None if req.info is None else {req.info.position})
            self._read_states()
            lowest = min(s.state & 0x0F for _, s in self._targets(req))
            req.steps = [s for s in _LADDER if lowest < s < req.wanted] + [req.wanted]
            self._write_step(req)
            return
        self._read_states()
        self._refresh_states()
        step = req.steps[0]
        if any(_needs(s.state, step, req.wanted) for _, s in self._targets(req)):
            if time.monotonic() < req.deadline:
                return
        elif len(req.steps) > 1:
            req.steps.pop(0)
            self._write_step(req)
            return
        req.reached = self.master.state if req.info is None else self._targets(req)[0][1].state
        self._request = None
        if req.wanted == protocol.OP:
            self._docheck = True  # the check loop re-requests OP for every slave
        req.done.set()

    def _write_step(self, req: StateRequest) -> None:
        step = req.steps[0]
        for _, slave in self._targets(req):
            if _needs(slave.state, step, req.wanted):
                slave.state = step
                slave.write_state()
        req.deadline = time.monotonic() + EC_TIMEOUTSTATE / 1e6

    def _read_states(self) -> None:
        """`read_state`, and the expected WKC for the states it read (before any write
        changes pysoem's cached `slave.state`)."""
        self.master.read_state()
        self.expected_wkc = sum(
            _wkc_share(info, slave.state)
            for info, slave in zip(self.slaves, self.master.slaves, strict=True)
        )

    def _refresh_states(self) -> None:
        for info, slave in zip(self.slaves, self.master.slaves, strict=True):
            info.al_state, info.al_status_code, info.lost = (
                slave.state,
                slave.al_status,
                slave.is_lost,
            )
            if slave.state == protocol.OP:
                info.warned.clear()
            if info.reconfig_at and slave.state == protocol.OP:
                info.reconfig_at = 0.0
                logger.info("[%s] %s back in OP after reconfig", self.name, info.trace_name)
            if info.stepping and (slave.state & 0x0F) >= info.requested_state:
                info.stepping = False
                logger.info(
                    "[%s] %s back in its held %s",
                    self.name,
                    info.trace_name,
                    protocol.al_state_name(slave.state),
                )
        self.al_state = self.master.state

    def _counters(self) -> dict[str, Any]:
        return {
            "slaves": len(self.slaves),
            "wkc": self.wkc,
            "expected_wkc": self.expected_wkc,
            "wkc_errors": self.wkc_errors,
            "lost_frames": self.lost_frames,
            "cycles": self.cycles,
            "cycle_time_us": self.cycle_time_us,
            "jitter_us": self.jitter_us,
        }

    def _diagnostics_tick(self, t_ns: int) -> None:
        """`_master`, every `_state`, and every PDO once; `read_state` holds the GIL for
        at most (1 + slaves) frames at EC_TIMEOUTRET. Any slave out of OP (its held state)
        sets the check: with the expected WKC following the states, a short WKC need not."""
        self._read_states()
        if self._request is None and any(
            slave.state != info.requested_state
            for info, slave in zip(self.slaves, self.master.slaves, strict=True)
        ):
            self._docheck = True
        self._refresh_states()
        self._log_states(t_ns, force=True)
        self.source.log_at(
            t_ns,
            self._event(MASTER_EVENT),
            {
                **self._counters(),
                "al_state": self.al_state,
                "al_state_name": protocol.al_state_name(self.al_state),
                "wkc_ok": self.wkc >= self.expected_wkc,
                "dc_time_ns": int(self.master.dc_time),
            },
        )
        # Not while the working counter is short: the image holds stale inputs.
        if self.wkc >= self.expected_wkc:
            self._log_process_data(t_ns, force=True)

    def _log_states(self, t_ns: int, force: bool) -> None:
        for info in self.slaves:
            values = (
                info.al_state,
                info.al_status_code,
                info.lost,
                info.outputs_cleared,
                info.reconfigs,
                info.error_acks,
            )
            if force or self._last_logged.get(info.state_event) != values:
                self._last_logged[info.state_event] = values
                self.source.log_at(
                    t_ns,
                    info.state_event,
                    {
                        "al_state": info.al_state,
                        "al_state_name": protocol.al_state_name(info.al_state),
                        "al_status_code": info.al_status_code,
                        "al_status_text": protocol.al_status_text(info.al_status_code),
                        "lost": info.lost,
                        "outputs_cleared": info.outputs_cleared,
                        "reconfigs": info.reconfigs,
                        "error_acks": info.error_acks,
                    },
                )

    def _log_process_data(self, t_ns: int, force: bool = False) -> None:
        """Each PDO event on change (or `force`), decoded from pysoem's per-slave views; not
        for a slave below SAFEOP or lost (its image is stale while the WKC excludes it)."""
        for info, slave in zip(self.slaves, self.master.slaves, strict=True):
            if not info.events or slave.is_lost or (slave.state & 0x0F) < protocol.SAFEOP:
                continue
            inputs = slave.input if info.map.inputs and not info.input_problem else None
            outputs = slave.output if info.map.outputs and not info.output_problem else None
            for event, pdo in info.events.items():
                image, start = (
                    (outputs, info.output_span.start_bit)
                    if pdo.is_output
                    else (inputs, info.input_span.start_bit)
                )
                if image is None:
                    continue
                values = {
                    e.field: protocol.decode(image, start + e.bit_offset, e.bit_length, e.kind)
                    for e in pdo.entries
                    if e.field
                }
                key = tuple(values.values())
                if force or self._last_logged.get(event) != key:
                    self._last_logged[event] = key
                    self.source.log_at(t_ns, event, values)

    # ─── Outputs ─────────────────────────────────────────────────────────────

    def _stage_zero(self, positions: Iterable[int] | None = None) -> int:
        """Zero the staged outputs of every writable slave (or `positions`) for the next
        exchange; returns the number of slaves staged in all."""
        with self._out_lock:
            for info, slave in zip(self.slaves, self.master.slaves, strict=True):
                if (
                    info.map.outputs
                    and not info.output_problem
                    and (positions is None or info.position in positions)
                ):
                    self._staged[info.position] = bytearray(len(slave.output))
            self._dirty = True
        return len(self._staged)

    def _drop_outputs(self, info: SlaveInfo) -> None:
        """A slave out of OP loses its staged outputs unless `restore_outputs`: they stay
        zero after recovery until written again (no automatic restart)."""
        if self.restore_outputs or not self.enable_outputs:
            return
        with self._out_lock:
            staged = self._staged.get(info.position)
            if staged is None or not any(staged):
                return
            staged[:] = bytes(len(staged))
            self._dirty = True
        info.outputs_cleared = True
        logger.info(
            "[%s] %s: outputs cleared after drop-out; write again to re-apply",
            self.name,
            info.trace_name,
        )

    def _apply_outputs(self) -> None:
        """Staged images into pysoem's views, only this slave's bit span: bit-sized
        neighbours share the byte."""
        if not self._dirty:
            return
        with self._out_lock:
            self._dirty = False
            for info, slave in zip(self.slaves, self.master.slaves, strict=True):
                staged = self._staged.get(info.position)
                if staged is None:
                    continue
                current = bytearray(slave.output)
                bits, start = info.map.output_bits, info.output_span.start_bit
                raw = protocol.extract_bits(staged, start, bits)
                protocol.insert_bits(current, start, bits, raw)
                slave.output = bytes(current)

    # ─── Action surface (called from the actions thread) ─────────────────────

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "interface": self.interface,
            "state": self.state,
            "error": self.error,
            "al_state": protocol.al_state_name(self.al_state),
            **self._counters(),
            "cycle_time_ms": self.cycle_time_ms,
            "cycle_time_us": round(self.cycle_time_us, 1),
            "jitter_us": round(self.jitter_us, 1),
            "outputs_enabled": self.enable_outputs,
        }

    def _require_running(self) -> None:
        if self.state != "running":
            raise RuntimeError(
                f"connection '{self.name}' is {self.state}"
                + (f": {self.error}" if self.error else "")
            )

    def find_slave(self, selector: str) -> tuple[SlaveInfo, Any]:
        """A slave by position (`2`), trace name (`2_EL1004`) or unique name (`EL1004`)."""
        self._require_running()
        text = selector.strip()
        if not text:
            raise ValueError("slave is required: a position or a name from list_slaves")
        by_position = {str(s.position): s for s in self.slaves}
        by_trace = {s.trace_name: s for s in self.slaves}
        found = by_position.get(text) or by_trace.get(text) or by_trace.get(protocol.sanitize(text))
        if found is None:
            named = [s for s in self.slaves if s.name == text]
            if len(named) > 1:
                raise ValueError(f"{len(named)} slaves are named {text!r}; pick one by position")
            found = named[0] if named else None
        if found is None:
            raise ValueError(
                f"unknown slave {selector!r}; slaves: "
                + ", ".join(s.trace_name for s in self.slaves)
            )
        return found, self.master.slaves[found.position - 1]

    def list_pdos(self, info: SlaveInfo) -> list[dict[str, Any]]:
        return [
            {
                "event": pdo.event,
                "pdo_index": f"0x{pdo.index:04x}",
                "direction": "output" if pdo.is_output else "input",
                "sync_manager": pdo.sync_manager,
                "entries": [
                    {
                        "field": e.field,
                        "name": e.name,
                        "index": f"0x{e.index:04x}",
                        "subindex": e.subindex,
                        "bit_offset": e.bit_offset,
                        "bit_length": e.bit_length,
                        "data_type": str(e.kind),
                    }
                    for e in pdo.entries
                    if e.field
                ],
            }
            for pdo in info.map.pdos()
        ]

    def sdo_read(self, slave: Any, index: int, subindex: int, size: int = 0) -> bytes:
        with self._mbx_lock:
            return bytes(slave.sdo_read(index, subindex, size))

    def sdo_write(self, slave: Any, index: int, subindex: int, data: bytes) -> None:
        """Gated with the outputs: an SDO can command a device (CiA 402 0x6040)."""
        if not self.enable_outputs:
            raise ValueError(
                f"SDO writes are disabled: enable_outputs is off for connection '{self.name}'; "
                "enable 'Enable Outputs' on the connection and restart to write SDOs"
            )
        with self._mbx_lock:
            slave.sdo_write(index, subindex, data)

    def od_browse(self, slave: Any, index: int | None) -> list[dict[str, Any]]:
        """The object dictionary (SDO info): every object, or one with its entries. The
        walk holds the GIL for every exchange, so only while held below OP.

        :raises RuntimeError: the segment is in OP
        """
        if self._requested_state == protocol.OP:
            raise RuntimeError(
                "browse_od pauses process data for the whole walk; request_state SAFEOP "
                "(or PREOP) on the connection first, then OP again afterwards"
            )
        with self._mbx_lock:
            try:
                return self._od_rows(slave, index)
            finally:
                self._drain_errors()

    def _od_rows(self, slave: Any, index: int | None) -> list[dict[str, Any]]:
        objects = list(slave.od)
        if index is not None:
            objects = [o for o in objects if o.index == index]
            if not objects:
                raise ValueError(f"object 0x{index:04x} is not in the dictionary")
        out = []
        for obj in objects:
            row: dict[str, Any] = {
                "index": f"0x{obj.index:04x}",
                "name": str(obj.name),
                "object_code": int(obj.object_code),
                "data_type": int(obj.data_type),
            }
            if index is not None:
                row["entries"] = [_od_entry(n, e) for n, e in enumerate(obj.entries)] or [
                    _od_entry(0, obj)
                ]
            out.append(row)
        return out

    def find_entry(self, info: SlaveInfo, target: str) -> tuple[pdo_mod.Pdo, pdo_mod.PdoEntry]:
        """An output entry by `<pdo event>/<field>` or `0x7000:01`."""
        text = target.strip()
        for pdo in info.map.outputs:
            for entry in pdo.entries:
                if not entry.field:
                    continue
                if (
                    text == f"{pdo.event}/{entry.field}"
                    or text.lower() == f"0x{entry.index:04x}:{entry.subindex:02x}"
                ):
                    return pdo, entry
        names = ", ".join(
            f"{p.event}/{e.field}" for p in info.map.outputs for e in p.entries if e.field
        )
        raise ValueError(
            f"unknown output {target!r} on {info.trace_name}; outputs: {names or 'none'}"
        )

    def write_output(self, info: SlaveInfo, entry: pdo_mod.PdoEntry, text: str) -> object:
        """Stage one output value; the next cycle carries it. Refused unless enabled, while
        the segment or the slave is held below OP, and for an unreachable output span."""
        if not self.enable_outputs:
            raise ValueError(
                f"outputs are disabled on connection '{self.name}'; enable 'Enable Outputs' "
                "on the connection and restart to drive outputs"
            )
        if self._requested_state != protocol.OP or info.requested_state != protocol.OP:
            raise ValueError(
                f"{info.trace_name} is held below OP by request_state; request OP first"
            )
        if info.output_problem:
            raise ValueError(
                f"outputs of {info.trace_name} cannot be written: {info.output_problem}"
            )
        value = protocol.coerce(text, entry.kind)
        raw = protocol.encode(value, entry.bit_length, entry.kind)
        with self._out_lock:
            protocol.insert_bits(
                self._staged[info.position],
                info.output_span.start_bit + entry.bit_offset,
                entry.bit_length,
                raw,
            )
            self._dirty = True
        info.outputs_cleared = False
        logger.info("[%s] %s output %s = %r", self.name, info.trace_name, entry.field, value)
        return value

    def clear_outputs(self) -> int:
        """Every output back to zero on the next cycle; the number of slaves with outputs."""
        self._require_running()
        count = self._stage_zero()
        logger.info("[%s] outputs cleared", self.name)
        return count

    def request_state(self, state_name: str, info: SlaveInfo | None = None) -> str:
        """Hold every slave, or one, at an AL state; the cycle thread serves the request.
        Returns the state reached."""
        self._require_running()
        req = StateRequest(protocol.REQUESTABLE_STATES[state_name], info)
        self._requests.append(req)
        deadline = time.monotonic() + len(_LADDER) * EC_TIMEOUTSTATE / 1e6 + 1.0
        while not req.done.wait(STATE_POLL):
            if self.state != "running" or time.monotonic() >= deadline:
                raise RuntimeError(f"connection '{self.name}' is {self.state}; request not served")
        logger.warning(
            "[%s] %s requested for %s; reached %s",
            self.name,
            state_name,
            "all slaves" if info is None else info.trace_name,
            protocol.al_state_name(req.reached),
        )
        return protocol.al_state_name(req.reached)


def _wkc_share(info: SlaveInfo, state: int) -> int:
    """One slave's share of the process data LRW at `state`: +1 with a read FMMU, +2 with
    a write FMMU. The ESC counts an LRW read +1, write +2 per slave; SOEM's overlap map
    counts per slave the same way (ethercatconfig.c:1471-1481) into expectedWKC =
    outputsWKC * 2 + inputsWKC (pysoem.pyx:602; LWR x2, ethercatmain.c:1969); IgH
    datagram_pair.c:97-100. Its plain map counts a slave only when it advances the
    logical address (ethercatconfig.c:1222-1246, 1292-1333), so bit slaves go uncounted.
    FMMUs from the readback, else the mapping. SAFEOP: inputs only (a uC slave keeps its
    output SM off until OP); below SAFEOP nothing. An image split over frames (above
    EC_MAXLRWDATA) is not modelled."""
    current = state & 0x0F
    if current < protocol.SAFEOP:
        return 0
    share = 1 if info.input_span.bits or info.map.input_bits else 0
    if current == protocol.OP and (info.output_span.bits or info.map.output_bits):
        share += 2
    return share


def _od_entry(subindex: int, entry: Any) -> dict[str, Any]:
    return {
        "subindex": subindex,
        "name": str(entry.name),
        "data_type": int(entry.data_type),
        "bit_length": int(entry.bit_length),
        "access": int(entry.obj_access),
    }
