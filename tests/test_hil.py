"""Against a real segment; skipped unless ZELOS_ETHERCAT_IFACE is set. Needs root.

    sudo ZELOS_ETHERCAT_IFACE=<nic> .venv/bin/python -m pytest tests/test_hil.py

DRIVES OUTPUTS: the first output of the first digital output terminal (ESC PDI
Digital I/O, e.g. an EL2xxx) is switched on and off; nothing else is written.
Disconnect anything that must not switch. The segment is taken through SAFEOP,
INIT and a killed master. Every claim is checked against the ESC itself over raw
APRD datagrams (AL status, watchdog, output register), never against the
extension's own view.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import struct
import subprocess
import sys
import time

import pytest
import zelos_sdk
from conftest import wait_until

from zelos_extension_ethercat import actions, protocol
from zelos_extension_ethercat.cli import app
from zelos_extension_ethercat.master import OUTPUT_WATCHDOG_MS, Segment

IFACE = os.environ.get("ZELOS_ETHERCAT_IFACE", "")
pytestmark = pytest.mark.skipif(not IFACE, reason="ZELOS_ETHERCAT_IFACE not set")

ETH_P_ECAT = 0x88A4
APRD, LRW = 0x01, 0x0C
INDEX = 0xF0  # above SOEM's 16 frame indexes: its receive drops our replies
AL_STATUS, PDI_CONTROL, WD_DIVIDER, WD_TIME, WD_STATUS = 0x0130, 0x0140, 0x0400, 0x0420, 0x0440
DIGITAL_OUT, FMMU = 0x0F00, 0x0600
FMMU_WRITE = 2
PDI_DIGITAL_IO = 0x04
#: The ESC trips at OUTPUT_WATCHDOG_MS after the last process data write.
TRIPPED = OUTPUT_WATCHDOG_MS / 1000 + 0.2


def esc_read(position: int, register: int, length: int) -> bytes:
    """One APRD of a slave's ESC register beside the running master."""
    datagram = struct.pack("<BBhHHH", APRD, INDEX, 1 - position, register, length, 0)
    payload = datagram + bytes(length + 2)
    frame = b"\xff" * 6 + b"\x02" * 6 + struct.pack(">H", ETH_P_ECAT)
    frame += struct.pack("<H", len(payload) | 0x1000) + payload
    with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ECAT)) as s:
        s.bind((IFACE, ETH_P_ECAT))
        s.settimeout(0.2)
        s.send(frame.ljust(60, b"\0"))
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            data, address = s.recvfrom(1600)
            if address[2] != socket.PACKET_OUTGOING and data[16:18] == bytes((APRD, INDEX)):
                assert struct.unpack_from("<H", data, 26 + length)[0] == 1, "no slave answered"
                return data[26 : 26 + length]
    raise AssertionError(f"no reply to APRD {position}:0x{register:04x}")


def al_state(position: int) -> int:
    return esc_read(position, AL_STATUS, 2)[0] & 0x1F


def watchdog_ok(position: int) -> bool:
    return bool(esc_read(position, WD_STATUS, 2)[0] & 1)


def watchdog_ms(position: int) -> float:
    divider, time_ = struct.unpack(
        "<HH", esc_read(position, WD_DIVIDER, 2) + esc_read(position, WD_TIME, 2)
    )
    return time_ * (divider + 2) * 40e-6


def pins(position: int) -> int:
    """The output pins driven: 0xF00 keeps the last write, but the ESC drives its
    Digital I/O outputs only while the SM watchdog runs (ET1200/ET1100 datasheet,
    output driver: high impedance once it expires)."""
    return esc_read(position, DIGITAL_OUT, 1)[0] if watchdog_ok(position) else 0


def output_bit(position: int) -> int:
    """The logical image bit of a slave's first output, from its write FMMU."""
    for n in range(4):
        reg = esc_read(position, FMMU + 16 * n, 16)
        logical, _, start_bit, _, _, _, kind, active = struct.unpack("<IHBBHBBB3x", reg)
        if active and kind == FMMU_WRITE:
            return logical * 8 + start_bit
    raise AssertionError(f"slave {position} has no write FMMU")


def sniffer() -> socket.socket:
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ECAT))
    s.bind((IFACE, ETH_P_ECAT))
    s.settimeout(1.0)
    return s


def first_sent(s: socket.socket, bit: int) -> int:
    """The bit in the next process data frame (LRW) back from the ring: as the slaves got it."""
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        data, address = s.recvfrom(1600)
        if address[2] != socket.PACKET_OUTGOING and data[16] == LRW:
            offset = bit // 8 - struct.unpack_from("<I", data, 18)[0]
            return data[26 + offset] >> (bit % 8) & 1
    raise AssertionError("no process data frame after the stall")


def bench_segment(name: str = "bench") -> Segment:
    advanced = app.resolve_advanced({})
    connection = {
        **app.CONNECTION_DEFAULTS,
        "name": name,
        "interface": IFACE,
        "enable_outputs": True,
    }
    shared = zelos_sdk.TraceSource(f"{advanced['prefix']}_{name}")
    return app.build_segment(connection, advanced["prefix"], shared, advanced)


def digital_output(segment: Segment) -> tuple[int, str]:
    """The first Digital I/O PDI slave with outputs, and its first output (pin bit 0)."""
    for info in segment.slaves:
        if info.map.outputs and esc_read(info.position, PDI_CONTROL, 1)[0] == PDI_DIGITAL_IO:
            pdo = info.map.outputs[0]
            return info.position, f"{pdo.event}/{pdo.entries[0].field}"
    pytest.skip("no digital output terminal (ESC PDI Digital I/O) on the segment")


async def all_op(segment: Segment, timeout: float = 15.0) -> None:
    """Every slave in OP on the wire, and the full working counter."""
    await wait_until(
        lambda: (
            segment.wkc == segment.expected_wkc
            and all(al_state(s.position) == protocol.OP for s in segment.slaves)
        ),
        timeout,
    )


def run(body) -> Segment:
    """`body(segment, position, output)` on a running bench segment; stopped after."""
    segment = bench_segment()
    actions.set_connections({segment.name: segment})

    async def main():
        stop = asyncio.Event()
        task = asyncio.create_task(segment.run(stop))
        try:
            await wait_until(lambda: segment.state == "running", 30.0)
            await all_op(segment)
            await body(segment, *digital_output(segment))
        finally:
            stop.set()
            await asyncio.wait_for(task, 5.0)

    asyncio.run(main())
    return segment


def write(position: int, output: str, on: bool) -> None:
    actions.write_output(slave=str(position), output=output, value=str(on).lower())


def test_bench_outputs_reach_the_pins():
    async def body(segment, position, output):
        assert pins(position) == 0, "outputs must start off"
        assert watchdog_ms(position) == pytest.approx(OUTPUT_WATCHDOG_MS)
        with pytest.raises(RuntimeError, match="no CoE mailbox"):  # a digital terminal has none
            actions.read_sdo(slave=str(position), index="0x1018", subindex=1)
        write(position, output, True)
        await wait_until(lambda: pins(position) & 1, 1.0)
        write(position, output, False)
        await wait_until(lambda: not pins(position) & 1, 1.0)
        write(position, output, True)
        await wait_until(lambda: pins(position) & 1, 1.0)
        actions.clear_outputs()
        await wait_until(lambda: pins(position) == 0, 1.0)
        status = segment.status()
        assert status["lost_frames"] == 0 and status["wkc_errors"] == 0

    run(body)


def test_bench_stall_trips_the_watchdog_and_outputs_stay_off():
    async def body(segment, position, output):
        write(position, output, True)
        await wait_until(lambda: pins(position) & 1, 1.0)
        apply = segment._apply_outputs
        bit = output_bit(position)
        stalled = []

        def stall_once():  # the cycle thread hangs before one send
            if not stalled:
                stalled.append((time.monotonic(), sniffer()))
                time.sleep(TRIPPED + 0.3)
            apply()

        segment._apply_outputs = stall_once
        await wait_until(lambda: stalled, 1.0)
        started, sniff = stalled[0]
        await asyncio.sleep(TRIPPED - (time.monotonic() - started))
        assert not watchdog_ok(position), "ESC watchdog did not trip"
        with sniff:
            # The first frame after the stall must not re-drive what the watchdog dropped.
            assert await asyncio.to_thread(first_sent, sniff, bit) == 0
        await all_op(segment)
        # Not restored: off after recovery until written again.
        assert pins(position) == 0 and watchdog_ok(position)
        assert actions.list_slaves()["slaves"][position - 1]["outputs_cleared"] is True
        write(position, output, True)
        await wait_until(lambda: pins(position) & 1, 1.0)

    run(body)


def test_bench_safeop_refuses_writes_and_holds_outputs_off():
    async def body(segment, position, output):
        write(position, output, True)
        await wait_until(lambda: pins(position) & 1, 1.0)
        assert actions.request_state(state="SAFEOP")["reached"] == "SAFEOP"
        assert {al_state(s.position) for s in segment.slaves} == {protocol.SAFEOP}
        assert pins(position) == 0
        with pytest.raises(ValueError, match="held below OP"):
            write(position, output, True)
        assert actions.request_state(state="OP")["reached"] == "OP"
        await all_op(segment)
        await asyncio.sleep(0.2)
        assert pins(position) == 0, "outputs came back on without a write"
        write(position, output, True)
        await wait_until(lambda: pins(position) & 1, 1.0)

    run(body)


def test_bench_stop_turns_outputs_off_and_slaves_to_init():
    seen = {}

    async def body(segment, position, output):
        write(position, output, True)
        await wait_until(lambda: pins(position) & 1, 1.0)
        seen["position"], seen["slaves"] = position, [s.position for s in segment.slaves]

    segment = run(body)
    assert segment.state == "stopped"
    assert pins(seen["position"]) == 0
    assert [al_state(p) for p in seen["slaves"]] == [protocol.INIT] * len(seen["slaves"])


KILLED_MASTER = """
import asyncio, sys
sys.path.insert(0, "tests")
from test_hil import actions, bench_segment, digital_output, write
async def main():
    segment = bench_segment("killed")
    actions.set_connections({segment.name: segment})
    task = asyncio.create_task(segment.run(asyncio.Event()))
    while segment.state != "running":
        await asyncio.sleep(0.05)
    while segment.wkc != segment.expected_wkc:
        await asyncio.sleep(0.05)
    position, output = digital_output(segment)
    write(position, output, True)
    print(position, flush=True)
    await task
asyncio.run(main())
"""


def test_bench_killed_master_fails_safe_and_the_next_one_comes_up():
    child = subprocess.Popen(
        [sys.executable, "-c", KILLED_MASTER], stdout=subprocess.PIPE, text=True
    )
    try:
        position = int(child.stdout.readline())
        deadline = time.monotonic() + 1.0
        while not pins(position) & 1:
            assert time.monotonic() < deadline, "output never reached the pin"
        child.send_signal(signal.SIGKILL)
        child.wait(5)
        time.sleep(TRIPPED)
        assert not watchdog_ok(position), "ESC watchdog did not trip"
    finally:
        child.kill()

    async def body(segment, position, output):
        assert pins(position) == 0
        write(position, output, True)
        await wait_until(lambda: pins(position) & 1, 1.0)
        actions.clear_outputs()
        await wait_until(lambda: pins(position) == 0, 1.0)

    run(body)
