"""One EtherCAT segment with the agent as its master: bring-up, cyclic process data,
diagnostics and recovery, modelled on SOEM's simple_test (OpenEtherCATsociety/SOEM
test/linux/simple_test). pysoem drives the NIC; `interface` `demo` runs the simulator.

The cycle runs on its own thread, never on the event loop; actions reach the
mailbox under `_mbx_lock` while the cycle keeps going.

GIL: pysoem releases it only around process data, SDO and FoE calls. Every other
SOEM call here (`read_state`, `write_state`, per-slave `state_check`, `reconfig`,
`recover`, `_fprd`, `eeprom_read`, `od`) holds it, so each is either kept off the
running cycle, bounded by a short SOEM timeout, or documented where it is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import zelos_sdk
from zelos_sdk import DataType

from zelos_extension_ethercat import pdo as pdo_mod
from zelos_extension_ethercat import protocol
from zelos_extension_ethercat.pdo import FmmuSpan, MapError, SiiImage, SlaveMap

logger = logging.getLogger(__name__)

DEMO_INTERFACE = "demo"

#: SOEM timeouts, in us (ethercattype.h).
EC_TIMEOUTRET = 2000
EC_TIMEOUTSTATE = 2_000_000
EC_TIMEOUTMON = 500
#: simple_test waits EC_TIMEOUTSTATE * 4 for SAFEOP and 200 x (one cycle + 50 ms) for OP.
#: Here both are Python polling loops (one `read_state` per poll, a 2 ms frame timeout),
#: not SOEM's blocking `state_check`, so the GIL and the stop event stay reachable.
SAFEOP_TIMEOUT = 8.0
OP_TIMEOUT = 10.0
STATE_POLL = 0.01
#: While the working counter is short, slaves are checked this often (simple_test: 10 ms;
#: each check is a broadcast state read plus any recovery, so it stays off the cycle).
CHECK_INTERVAL = 0.1
#: Configured station alias register (ETG.1000.4 Table 31).
REG_STATION_ALIAS = 0x0012

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
]


class SegmentError(Exception):
    """The segment cannot be brought up or an action cannot run; one line for the user."""


def open_backend(interface: str) -> Any:
    """A pysoem Master on `interface`, or the simulator for `demo`.

    :raises SegmentError: the interface cannot be opened (missing, or no CAP_NET_RAW)
    """
    if interface == DEMO_INTERFACE:
        from zelos_extension_ethercat.demo.simulator import SimMaster

        return SimMaster()
    import pysoem

    # Every SOEM call releases the GIL: a 700 ms SDO read on the actions thread must
    # not stall the cycle, or the slaves' SM watchdog (100 ms default) trips.
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
    """What the extension knows of one slave: identity, mapping, trace names, last state."""

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
    #: Why a direction is unusable through pysoem's view (inputs not logged, writes
    #: refused), or "" when it is fine; see `pdo.span_problem`.
    input_problem: str = ""
    output_problem: str = ""
    #: The state this slave alone is held at by `request_state`; below OP the check
    #: loop leaves it there.
    requested_state: int = protocol.OP
    al_state: int = 0
    al_status_code: int = 0
    lost: bool = False
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
            "mapping": self.map.source,
            "input_bits": self.map.input_bits,
            "output_bits": self.map.output_bits,
            "input_problem": self.input_problem,
            "output_problem": self.output_problem,
            "pdos": [p.event for p in self.map.pdos()],
        }


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
    ) -> None:
        self.name = name
        self.interface = interface
        self.cycle_time_ms = cycle_time_ms
        self.enable_outputs = enable_outputs
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
        #: The state the master holds the segment at; below OP recovery stops pushing to OP.
        self._requested_state = protocol.OP
        self._last_logged: dict[str, tuple] = {}
        self._last_logged_at: dict[str, int] = {}

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    async def run(self, stop: asyncio.Event) -> None:
        """Bring the segment up and exchange process data until `stop`.

        :raises SegmentError: bring-up failed (first contact is required)
        """
        self.state = "starting"
        # Daemon: the app's forced exit past its shutdown bound must not wait on a
        # SOEM call that holds the GIL (a reconfig, an OD walk).
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
        self.master.config_map()
        self.master.config_dc()
        for info, slave in zip(self.slaves, self.master.slaves, strict=True):
            self._check_spans(info, slave)
        self._register_events()
        if not self._wait_state(protocol.SAFEOP, SAFEOP_TIMEOUT):
            raise SegmentError(f"not every slave reached SAFEOP: {self._laggards(protocol.SAFEOP)}")
        self.expected_wkc = self.master.expected_wkc
        # Outputs start at zero: one valid exchange before OP, as simple_test does.
        self._zero_outputs()
        self.master.send_processdata()
        self.master.receive_processdata(EC_TIMEOUTRET)
        self.master.state = protocol.OP
        self.master.write_state()
        if not self._wait_state(protocol.OP, OP_TIMEOUT, exchange=True):
            raise SegmentError(f"not every slave reached OP: {self._laggards(protocol.OP)}")
        self._refresh_states()
        logger.info(
            "[%s] OP, expected working counter %d, cycle %g ms, outputs %s",
            self.name,
            self.expected_wkc,
            self.cycle_time_ms,
            "enabled" if self.enable_outputs else "disabled",
        )

    def _wait_state(self, wanted: int, timeout: float, exchange: bool = False) -> bool:
        """Poll until every slave reports `wanted`, the deadline passes, or a stop.

        `exchange`: keep process data flowing meanwhile (slaves leave SAFEOP only with
        valid outputs). Each poll is one `read_state` (a broadcast, then one read per
        slave only when they disagree; each frame bounded by EC_TIMEOUTRET).

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
        """Where the slave's bits sit, from its FMMUs, and whether pysoem's view reaches
        them; a direction that does not fit is logged once here and then left alone."""
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
        if info.output_problem:
            logger.warning(
                "[%s] %s: outputs refused: %s", self.name, info.trace_name, info.output_problem
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
            info.alias = int.from_bytes(slave._fprd(REG_STATION_ALIAS, 2), "little")
        info.map = self._read_map(info, slave)
        pdo_mod.assign_names(info.map)
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
            try:
                return pdo_mod.read_coe_map(slave.sdo_read, self._od_lookup(info, slave))
            except MapError as e:
                logger.info("[%s] %s: %s; mapping from SII", self.name, info.trace_name, e)
        try:
            return pdo_mod.read_sii_map(sii)
        except Exception as e:
            logger.warning("[%s] %s: no PDO mapping (%s)", self.name, info.trace_name, e)
            return SlaveMap()

    def _od_lookup(self, info: SlaveInfo, slave: Any) -> pdo_mod.OdLookup | None:
        """Names and types from the object dictionary, when the slave serves SDO info.

        `slave.od` walks the dictionary over the mailbox holding the GIL; it runs here
        before the cycle exists, bounded by EC_TIMEOUTRXM (700 ms) per failed exchange.
        """
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
            with contextlib.suppress(Exception):
                entries = obj.entries
                if entries and subindex < len(entries):
                    return str(entries[subindex].name), int(entries[subindex].data_type)
                if subindex == 0:
                    return str(obj.name), int(obj.data_type)
            return None

        return lookup

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
        last_check = last_diag = last_cycle = next_ns
        periods: list[int] = []
        jitter_max = 0
        while not self._stop.is_set():
            now = time.perf_counter_ns()
            if now < next_ns:
                time.sleep((next_ns - now) / 1e9)
                now = time.perf_counter_ns()
            jitter_max = max(jitter_max, abs(now - next_ns))
            next_ns += period_ns
            if now > next_ns:  # fell behind by a whole period: resync rather than burst
                next_ns = now + period_ns
            periods.append(now - last_cycle)
            last_cycle = now

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
                self._log_process_data(t_ns)

            if (
                self.wkc < self.expected_wkc or self._docheck
            ) and now - last_check >= CHECK_INTERVAL * 1e9:
                last_check = now
                self._check_slaves(t_ns)
            if now - last_diag >= self.diagnostics_interval * 1e9:
                last_diag = now
                if periods:
                    self.cycle_time_us = sum(periods) / len(periods) / 1000
                self.jitter_us = jitter_max / 1000
                periods.clear()
                jitter_max = 0
                self._diagnostics_tick(t_ns)

    def _bring_down(self) -> None:
        """Outputs to zero for one cycle, every slave to INIT, interface closed."""
        with contextlib.suppress(Exception):
            self._zero_outputs()
            self.master.send_processdata()
            self.master.receive_processdata(EC_TIMEOUTRET)
            self.master.state = protocol.INIT
            self.master.write_state()
        self._close()
        logger.info("[%s] stopped", self.name)

    def _check_slaves(self, t_ns: int) -> None:
        """simple_test's ecatcheck: acknowledge errors, re-request OP, reconfigure a slave
        that fell back, recover one that vanished.

        Runs on the cycle thread at most every CHECK_INTERVAL while the WKC is short,
        and every call here holds the GIL: `read_state` and `state_check` are bounded
        by EC_TIMEOUTRET per frame (2 ms), `recover` by a few frames at EC_TIMEOUTMON
        (500 us). `reconfig` is the exception: SOEM walks INIT -> PREOP -> SAFEOP -> OP
        with EC_TIMEOUTSTATE (2 s) per step, so a slave that answers but will not
        progress stalls the cycle up to 8 s; a healthy one takes tens of ms. pysoem has
        no non-blocking form of it, and the stall is the one simple_test accepts.
        """
        self.master.read_state()
        docheck = False
        if self._requested_state != protocol.OP:  # held below OP on request: observe only
            self._refresh_states()
            self._log_states(t_ns, force=False)
            return
        for info, slave in zip(self.slaves, self.master.slaves, strict=True):
            state = slave.state
            if info.requested_state != protocol.OP:  # this slave alone is held: observe only
                continue
            if state != protocol.OP:
                docheck = True
                if state == protocol.SAFEOP + protocol.ERROR_FLAG:
                    logger.warning(
                        "[%s] %s SAFEOP+ERROR 0x%04x %s; acknowledging",
                        self.name,
                        info.trace_name,
                        slave.al_status,
                        protocol.al_status_text(slave.al_status),
                    )
                    slave.state = protocol.SAFEOP + protocol.ERROR_FLAG  # ACK shares the bit
                    slave.write_state()
                elif state == protocol.SAFEOP:
                    logger.warning("[%s] %s in SAFEOP; requesting OP", self.name, info.trace_name)
                    slave.state = protocol.OP
                    slave.write_state()
                elif state > protocol.NONE:
                    if slave.reconfig(EC_TIMEOUTMON):
                        slave.is_lost = False
                        logger.info("[%s] %s reconfigured", self.name, info.trace_name)
                elif not slave.is_lost:
                    slave.state_check(protocol.OP, EC_TIMEOUTRET)
                    if slave.state == protocol.NONE:
                        slave.is_lost = True
                        logger.error("[%s] %s lost", self.name, info.trace_name)
            if slave.is_lost:
                if slave.state == protocol.NONE:
                    if slave.recover(EC_TIMEOUTMON):
                        slave.is_lost = False
                        logger.info("[%s] %s recovered", self.name, info.trace_name)
                else:
                    slave.is_lost = False
                    logger.info("[%s] %s found", self.name, info.trace_name)
        if self._docheck and not docheck:
            logger.info("[%s] all slaves resumed OP", self.name)
        self._docheck = docheck
        self._refresh_states()
        self._log_states(t_ns, force=False)

    def _refresh_states(self) -> None:
        for info, slave in zip(self.slaves, self.master.slaves, strict=True):
            info.al_state, info.al_status_code, info.lost = (
                slave.state,
                slave.al_status,
                slave.is_lost,
            )
        self.al_state = self.master.state

    def _diagnostics_tick(self, t_ns: int) -> None:
        """`_master`, every `_state`, and every PDO once; `read_state` holds the GIL for
        at most (1 + slaves) frames at EC_TIMEOUTRET."""
        self.master.read_state()
        self._refresh_states()
        self._log_states(t_ns, force=True)
        self.source.log_at(
            t_ns,
            self._event(MASTER_EVENT),
            {
                "slaves": len(self.slaves),
                "al_state": self.al_state,
                "al_state_name": protocol.al_state_name(self.al_state),
                "wkc": self.wkc,
                "expected_wkc": self.expected_wkc,
                "wkc_ok": self.wkc >= self.expected_wkc,
                "wkc_errors": self.wkc_errors,
                "lost_frames": self.lost_frames,
                "cycles": self.cycles,
                "cycle_time_us": self.cycle_time_us,
                "jitter_us": self.jitter_us,
                "dc_time_ns": int(self.master.dc_time),
            },
        )
        # Every PDO at least once per interval, so a static input still plots; not while
        # the working counter is short, when the image holds stale inputs.
        if self.wkc >= self.expected_wkc:
            self._log_process_data(t_ns, force=True)

    def _log_states(self, t_ns: int, force: bool) -> None:
        for info in self.slaves:
            values = (info.al_state, info.al_status_code, info.lost)
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
                    },
                )

    def _log_process_data(self, t_ns: int, force: bool = False) -> None:
        """Each PDO event on change (or `force`), decoded from pysoem's per-slave views."""
        for info, slave in zip(self.slaves, self.master.slaves, strict=True):
            if not info.events:
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

    def _zero_outputs(self) -> None:
        with self._out_lock:
            self._staged = {
                info.position: bytearray(len(slave.output))
                for info, slave in zip(self.slaves, self.master.slaves, strict=True)
                if info.map.outputs and not info.output_problem
            }
            self._dirty = True
        self._apply_outputs()

    def _apply_outputs(self) -> None:
        """Staged output images into pysoem's views: only this slave's bit span, since
        bit-sized neighbours share the byte."""
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
            "slaves": len(self.slaves),
            "expected_wkc": self.expected_wkc,
            "wkc": self.wkc,
            "wkc_errors": self.wkc_errors,
            "lost_frames": self.lost_frames,
            "cycles": self.cycles,
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
        with self._mbx_lock:
            slave.sdo_write(index, subindex, data)

    def od_browse(self, slave: Any, index: int | None) -> list[dict[str, Any]]:
        """The object dictionary (SDO info): every object, or one with its entries.

        pysoem's `od` walk holds the GIL for every mailbox exchange, which pauses the
        cycle for the whole walk; in OP that trips every slave's SM watchdog. So only
        while the segment is held below OP, where no process data is due.

        :raises RuntimeError: the segment is in OP
        """
        if self._requested_state == protocol.OP:
            raise RuntimeError(
                "browse_od pauses process data for the whole walk; request_state SAFEOP "
                "(or PREOP) on the connection first, then OP again afterwards"
            )
        with self._mbx_lock:
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
                    row["entries"] = [
                        {
                            "subindex": n,
                            "name": str(e.name),
                            "data_type": int(e.data_type),
                            "bit_length": int(e.bit_length),
                            "access": int(e.obj_access),
                        }
                        for n, e in enumerate(obj.entries)
                    ] or [
                        {
                            "subindex": 0,
                            "name": str(obj.name),
                            "data_type": int(obj.data_type),
                            "bit_length": int(obj.bit_length),
                            "access": int(obj.obj_access),
                        }
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
        """Stage one output value; the next cycle carries it. Refused unless enabled, and
        for a slave whose outputs pysoem's view cannot reach whole."""
        if not self.enable_outputs:
            raise ValueError(
                f"outputs are disabled on connection '{self.name}'; enable 'Enable Outputs' "
                "on the connection and restart to drive outputs"
            )
        if info.output_problem:
            raise ValueError(
                f"outputs of {info.trace_name} cannot be written: {info.output_problem}"
            )
        value = protocol.coerce(text, entry.kind)
        raw = protocol.encode(value, entry.bit_length, entry.kind)
        with self._out_lock:
            staged = self._staged.setdefault(
                info.position, bytearray(len(self.master.slaves[info.position - 1].output))
            )
            protocol.insert_bits(
                staged, info.output_span.start_bit + entry.bit_offset, entry.bit_length, raw
            )
            self._dirty = True
        logger.info("[%s] %s output %s = %r", self.name, info.trace_name, entry.field, value)
        return value

    def clear_outputs(self) -> int:
        """Every output back to zero on the next cycle; the number of slaves with outputs."""
        self._require_running()
        with self._out_lock:
            for staged in self._staged.values():
                staged[:] = bytes(len(staged))
            self._dirty = True
        logger.info("[%s] outputs cleared", self.name)
        return len(self._staged)

    def request_state(self, state_name: str, info: SlaveInfo | None = None) -> str:
        """Request an AL state for every slave or one; returns the state reached.

        The wait is a polling loop (per-slave `state_check` or `read_state`, each
        bounded by EC_TIMEOUTRET per frame) rather than SOEM's `state_check` at
        EC_TIMEOUTSTATE, which would hold the GIL, and so the cycle, for up to 2 s.
        """
        self._require_running()
        wanted = protocol.REQUESTABLE_STATES[state_name]
        slave = self.master.slaves[info.position - 1] if info is not None else None
        target = slave if slave is not None else self.master
        with self._mbx_lock:
            if wanted != protocol.OP:
                self.clear_outputs()
            target.state = wanted
            target.write_state()
            deadline = time.monotonic() + EC_TIMEOUTSTATE / 1e6
            while True:
                reached = (
                    self.master.read_state()
                    if slave is None
                    else slave.state_check(wanted, EC_TIMEOUTRET)
                )
                if reached == wanted or time.monotonic() >= deadline:
                    break
                time.sleep(STATE_POLL)
        if info is None:
            self._requested_state = wanted
            for each in self.slaves:
                each.requested_state = wanted
        else:
            info.requested_state = wanted
        self._docheck = True  # back to OP: the check loop re-requests it for every slave
        logger.warning(
            "[%s] %s requested for %s; reached %s",
            self.name,
            state_name,
            "all slaves" if slave is None else "one slave",
            protocol.al_state_name(reached),
        )
        return protocol.al_state_name(reached)
