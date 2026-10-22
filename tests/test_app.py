"""The demo segment end to end: bring-up on the simulator, process data in the trace,
the action surface, output arming, and recovery from a dropped slave."""

import asyncio
import time

import pytest
import zelos_sdk
from conftest import wait_until

from zelos_extension_ethercat import actions, protocol
from zelos_extension_ethercat import master as master_mod
from zelos_extension_ethercat.cli import app
from zelos_extension_ethercat.demo.simulator import SimMaster
from zelos_extension_ethercat.master import Segment


def _segment(name="demo", enable_outputs=True, cycle_ms=2.0):
    """A demo-interface segment on the app's resolution, with a fast diagnostics tick."""
    advanced = app.resolve_advanced({"advanced": {"diagnostics_interval": 0.1}})
    shared = zelos_sdk.TraceSource(f"{advanced['prefix']}_{name}")
    connection = {
        **app.CONNECTION_DEFAULTS,
        "name": name,
        "interface": "demo",
        "cycle_time": cycle_ms,
    }
    connection["enable_outputs"] = enable_outputs
    return app.build_segment(connection, advanced["prefix"], shared, advanced), shared


async def _running(segment: Segment, body):
    stop = asyncio.Event()
    task = asyncio.create_task(segment.run(stop))
    try:
        await wait_until(lambda: segment.state == "running", 5.0)
        await body(stop)
    finally:
        stop.set()
        await asyncio.wait_for(task, 5.0)


def test_demo_segment_traces_slaves_pdos_and_diagnostics():
    segment, shared = _segment()
    actions.set_connections({segment.name: segment})

    async def body(stop):
        await wait_until(lambda: segment.cycles >= 50, 5.0)
        logged = segment._last_logged
        # One event per PDO, named from the SII / the OD, under <connection>/<slave>/<pdo>.
        assert shared.get_event("demo/2_EL1004/Channel 1") is not None
        assert shared.get_event("demo/5_EL3004/AI Standard Channel 4") is not None
        assert shared.get_event("demo/_master") is not None
        await wait_until(lambda: "demo/5_EL3004/AI Standard Channel 4" in logged, 2.0)
        status = actions.get_status()["connections"][0]
        assert status["al_state"] == "OP" and status["expected_wkc"] == 6 and status["wkc"] == 6
        assert status["wkc_errors"] == 0 and status["lost_frames"] == 0
        slaves = actions.list_slaves()["slaves"]
        assert [s["trace_name"] for s in slaves] == [
            "1_EK1100",
            "2_EL1004",
            "3_EL2004",
            "4_EL2004",
            "5_EL3004",
        ]
        assert slaves[0]["alias"] == 1001 and slaves[4]["mapping"] == "coe"
        assert not any(s["input_problem"] or s["output_problem"] for s in slaves)
        pdos = actions.list_pdos(slave="EL3004")["pdos"]
        assert pdos[0]["entries"][-1] == {
            "field": "Value",
            "name": "Value",
            "index": "0x6000",
            "subindex": 17,
            "bit_offset": 16,
            "bit_length": 16,
            "data_type": "int16",
        }

    asyncio.run(_running(segment, body))
    assert segment.state == "stopped"


def test_coe_actions_and_output_arming():
    segment, _ = _segment()
    actions.set_connections({segment.name: segment})

    async def body(stop):
        read = actions.read_sdo(slave="5", index="0x1008")
        assert (read["data_type"], read["value"]) == ("hex", "454c33303034")
        assert actions.read_sdo(slave="5", index="0x1008", data_type="string")["value"] == "EL3004"
        assert actions.read_sdo(slave="5_EL3004", index="0x1018", subindex=2)["value"] == 0x0BBC3052
        with pytest.raises(RuntimeError, match="read-only"):
            actions.write_sdo(slave="5", index="0x1018", subindex=1, value="1", data_type="u32")
        actions.write_sdo(slave="5", index="0x8000", subindex=6, value="0", data_type="u8")
        assert actions.read_sdo(slave="5", index="0x8000", subindex=6)["value"] == 0
        # The OD walk pauses process data: refused in OP, served while held below it.
        with pytest.raises(RuntimeError, match="request_state SAFEOP"):
            actions.browse_od(slave="5", index="0x6000")
        assert actions.request_state(state="SAFEOP")["reached"] == "SAFEOP"
        od = actions.browse_od(slave="5", index="0x6000")["objects"][0]
        assert od["name"] == "AI Inputs Ch.1" and od["entries"][17]["name"] == "Value"
        assert actions.request_state(state="OP")["reached"] == "OP"
        with pytest.raises(ValueError, match="unknown output"):
            actions.write_output(slave="3", output="Channel 9/Output", value="true")
        # The EL1004's first channels are wired back from the first EL2004.
        actions.write_output(slave="3", output="Channel 2/Output", value="true")
        await wait_until(
            lambda: segment._last_logged.get("demo/2_EL1004/Channel 2") == (True,), 2.0
        )
        assert segment._last_logged["demo/3_EL2004/Channel 2"] == (True,)
        # The two EL2004 share one output byte: the second lands at bit 4, bit 1 stays.
        actions.write_output(slave="4", output="Channel 1/Output", value="true")
        await wait_until(
            lambda: segment._last_logged.get("demo/4_EL2004/Channel 1") == (True,), 2.0
        )
        sim: SimMaster = segment.master
        assert sim.io_map[0] == 0b0001_0010
        # Output slaves get the process data watchdog; input-only slaves are left alone.
        assert [getattr(sl, "watchdog_ms", None) for sl in sim.slaves[1:4]] == [None, 500.0, 500.0]

        async def drop_and_recover(slave):
            slave.present, slave._current = False, protocol.NONE
            await wait_until(lambda: segment.slaves[2].lost, 3.0)
            slave.present = True
            await wait_until(
                lambda: segment.slaves[2].al_state == protocol.OP and segment.wkc == 6, 5.0
            )

        # Default: the dropped EL2004's outputs are cleared, the neighbour's bit 4 kept;
        # the power cycle reset its watchdog, so it is programmed again.
        await drop_and_recover(sim.slaves[2])
        await wait_until(lambda: sim.io_map[0] == 0b0001_0000, 2.0)
        assert sim.slaves[2].watchdog_ms == 500.0
        assert actions.list_slaves()["slaves"][2]["outputs_cleared"] is True
        actions.write_output(slave="3", output="Channel 2/Output", value="true")
        await wait_until(lambda: sim.io_map[0] == 0b0001_0010, 2.0)
        assert actions.list_slaves()["slaves"][2]["outputs_cleared"] is False
        # restore_outputs_after_recovery: the last written outputs are driven again.
        segment.restore_outputs = True
        await drop_and_recover(sim.slaves[2])
        await asyncio.sleep(0.1)
        assert sim.io_map[0] == 0b0001_0010
        assert actions.list_slaves()["slaves"][2]["outputs_cleared"] is False
        assert actions.clear_outputs()["slaves"] == 2
        await wait_until(
            lambda: segment._last_logged.get("demo/2_EL1004/Channel 2") == (False,), 2.0
        )
        assert sim.io_map[0] == 0

    asyncio.run(_running(segment, body))


class _Clock:
    """`time` for master.py with a monotonic that a test can advance."""

    offset = 0.0

    def __getattr__(self, name):
        return getattr(time, name)

    def monotonic(self):
        return time.monotonic() + self.offset


def test_outputs_disabled_refuses_writes_and_a_lost_slave_recovers(monkeypatch):
    """Outputs off: writes refused and the output image all zero through a drop-out of
    the first EL2004 and its recovery; a segment-wide hold below OP is kept; a slave
    that will not leave PREOP gets one reconfig per RECONFIG_BACKOFF."""
    clock = _Clock()
    monkeypatch.setattr(master_mod, "time", clock)
    segment, _ = _segment(name="bench", enable_outputs=False)
    actions.set_connections({segment.name: segment})

    async def body(stop):
        with pytest.raises(ValueError, match="outputs are disabled"):
            actions.write_output(slave="3", output="Channel 1/Output", value="true")
        with pytest.raises(ValueError, match="SDO writes are disabled"):
            actions.write_sdo(slave="5", index="0x8000", subindex=6, value="0", data_type="u8")
        # The first EL2004 drops off the bus, then comes back.
        sim: SimMaster = segment.master
        zero = bytes(sim.output_bytes)
        el2004 = sim.slaves[2]
        el2004.present, el2004._current = False, protocol.NONE
        await wait_until(lambda: segment.wkc_errors > 0, 3.0)
        await wait_until(lambda: segment.slaves[2].lost, 3.0)
        assert bytes(sim.io_map[: sim.output_bytes]) == zero
        el2004.present = True
        await wait_until(
            lambda: segment.slaves[2].al_state == protocol.OP and segment.wkc == 6, 5.0
        )
        assert actions.list_slaves()["slaves"][2]["lost"] is False
        assert bytes(sim.io_map[: sim.output_bytes]) == zero
        reached = actions.request_state(state="SAFEOP")["reached"]
        assert reached == "SAFEOP"
        await asyncio.sleep(0.3)  # recovery must not push it back to OP while held
        assert segment.al_state == protocol.SAFEOP
        # A held slave that vanishes is flagged lost, recovered and stepped back to its
        # held state, never above it.
        el2004.present, el2004._current = False, protocol.NONE
        await wait_until(lambda: segment.slaves[2].lost, 3.0)
        el2004.present = True
        await wait_until(lambda: not segment.slaves[2].lost, 3.0)
        await wait_until(lambda: segment.slaves[2].al_state == protocol.SAFEOP, 3.0)
        await asyncio.sleep(0.3)
        assert segment.slaves[2].al_state == protocol.SAFEOP
        assert segment.al_state == protocol.SAFEOP
        assert actions.request_state(state="OP")["reached"] == "OP"
        assert bytes(sim.io_map[: sim.output_bytes]) == zero
        # Stuck in PREOP: reconfig once, then observed only until the backoff passes.
        calls = []
        before = segment.slaves[2].reconfigs
        real_reconfig = el2004.reconfig

        def reconfig(timeout=500):
            calls.append(1)
            return real_reconfig(timeout) if len(calls) > 2 else 0

        el2004.reconfig = reconfig
        el2004._current = protocol.PREOP
        await wait_until(lambda: len(calls) == 1, 3.0)
        await asyncio.sleep(0.3)
        assert len(calls) == 1 and segment.slaves[2].al_state == protocol.PREOP
        clock.offset += master_mod.RECONFIG_BACKOFF
        await wait_until(lambda: len(calls) == 2, 3.0)
        clock.offset += master_mod.RECONFIG_BACKOFF
        await wait_until(lambda: segment.slaves[2].al_state == protocol.OP, 3.0)
        assert len(calls) == 3
        assert actions.list_slaves()["slaves"][2]["reconfigs"] == before + 3

    asyncio.run(_running(segment, body))


def test_expected_wkc_counts_bit_slaves_and_follows_states(monkeypatch):
    """EL1004 + EL2004: each counts (1 + 2) though SOEM counts neither (pysoem's 0); a
    slave left in SAFEOP with a full WKC is still brought back to OP; a SAFEOP hold
    expects inputs only and keeps logging."""
    layout = ("EK1100", "EL1004", "EL2004")
    monkeypatch.setattr(master_mod, "open_backend", lambda interface: SimMaster(layout=layout))
    segment, _ = _segment(name="wkc")
    actions.set_connections({segment.name: segment})

    async def body(stop):
        sim: SimMaster = segment.master
        assert (segment.expected_wkc, sim.expected_wkc, segment.wkc) == (3, 0, 3)
        sim.slaves[2]._current = protocol.SAFEOP  # ESC-only: still counts its outputs
        await wait_until(lambda: sim.slaves[2]._current == protocol.OP, 2.0)
        assert actions.request_state(state="SAFEOP")["reached"] == "SAFEOP"
        await wait_until(lambda: segment.expected_wkc == 1, 1.0)
        errors, logged = segment.wkc_errors, segment._last_logged.get("wkc/2_EL1004/Channel 3")
        await wait_until(lambda: segment._last_logged.get("wkc/2_EL1004/Channel 3") != logged, 2.0)
        assert segment.wkc_errors == errors

    asyncio.run(_running(segment, body))


def test_a_stalled_cycle_drops_outputs_before_the_next_send():
    """A cycle stall past the output watchdog: the ESC-only EL2004 trips but stays in OP,
    so the next send would drive its old outputs again; they go out as zero instead."""
    segment, _ = _segment(name="stall")
    actions.set_connections({segment.name: segment})

    async def body(stop):
        sim: SimMaster = segment.master
        actions.write_output(slave="3", output="Channel 1/Output", value="true")
        await wait_until(lambda: sim.io_map[0] & 1, 2.0)
        sent, receive, send = [], sim.receive_processdata, sim.send_processdata

        def stalled(timeout=2000):
            sim.receive_processdata = receive
            time.sleep(master_mod.OUTPUT_WATCHDOG_MS / 1000 + 0.1)
            return receive(timeout)

        def recorded():
            sent.append(sim.io_map[0])
            return send()

        sim.receive_processdata, sim.send_processdata = stalled, recorded
        await wait_until(lambda: len(sent) > 5, 3.0)
        assert not any(b & 1 for b in sent[1:])
        assert sim.slaves[2].wd_trips == 1 and sim.slaves[2]._current == protocol.OP
        assert actions.list_slaves()["slaves"][2]["outputs_cleared"] is True

    asyncio.run(_running(segment, body))


def test_bit_slaves_sharing_bytes_merge_and_a_straddler_is_refused(monkeypatch, caplog):
    """Inputs EL1002 + EL1004 + EL1004 (bits 0-1, 2-5, 6-9) and outputs EL2004 + EL2004 +
    EL2002 + EL2004 + EL2004 (0-3, 4-7, 8-9, 10-13, 14-17): the writes of each terminal
    land in its own bits, and the ones straddling a byte are refused / not logged."""
    layout = (
        "EK1100",
        "EL1002",
        "EL1004",
        "EL1004",
        "EL2004",
        "EL2004",
        "EL2002",
        "EL2004",
        "EL2004",
    )
    monkeypatch.setattr(master_mod, "open_backend", lambda interface: SimMaster(layout=layout))
    segment, _ = _segment(name="straddle")
    actions.set_connections({segment.name: segment})

    async def body(stop):
        slaves = actions.list_slaves()["slaves"]
        assert [s["trace_name"] for s in slaves if s["input_problem"]] == ["4_EL1004"]
        assert [s["trace_name"] for s in slaves if s["output_problem"]] == ["9_EL2004"]
        assert "4_EL1004: inputs not logged" in caplog.text
        with pytest.raises(ValueError, match="cannot be written"):
            actions.write_output(slave="9", output="Channel 1/Output", value="true")
        actions.write_output(slave="6", output="Channel 1/Output", value="true")  # bit 4
        actions.write_output(slave="8", output="Channel 4/Output", value="true")  # bit 13
        actions.write_output(slave="5", output="Channel 1/Output", value="true")  # bit 0
        sim: SimMaster = segment.master
        await wait_until(lambda: sim.io_map[:2] == bytes([0b0001_0001, 0b0010_0000]), 2.0)
        # The first EL1004 (bits 2-5) follows the first EL2004's channel 1; the straddler
        # logs nothing.
        await wait_until(
            lambda: segment._last_logged.get("straddle/3_EL1004/Channel 1") == (True,), 2.0
        )
        assert not any(e.startswith("straddle/4_EL1004/") for e in segment._last_logged)

    asyncio.run(_running(segment, body))


def test_resolve_connections_names_and_rejects_duplicates(caplog):
    resolved = app.resolve_connections({"connections": [{"interface": "eth1"}]})
    assert resolved[0]["name"] == "eth1" and resolved[0]["enable_outputs"] is False
    with pytest.raises(SystemExit):
        app.resolve_connections(
            {"connections": [{"interface": "eth1"}, {"name": "x", "interface": "eth1"}]}
        )
    assert "one master per interface" in caplog.text
    with pytest.raises(SystemExit):
        app.resolve_connections({"connections": [{"interface": "eth1", "cycle_time": 150}]})
    assert "outside 0.1..100" in caplog.text


def test_auto_config_lists_interfaces_and_never_scans(monkeypatch):
    from zelos_extension_ethercat import autoconfig

    def no_scan(*args, **kwargs):
        raise AssertionError("auto_config must not scan")

    monkeypatch.setattr(autoconfig, "interfaces", lambda: [{"name": "eth1", "detail": "up, igb"}])
    monkeypatch.setattr(autoconfig, "scan", no_scan)
    monkeypatch.setattr(autoconfig.subprocess, "Popen", no_scan)
    empty = actions.auto_config({})
    assert empty["config"]["connections"] == [{"name": "demo", "interface": "demo"}]
    assert "eth1 (up, igb)" in empty["message"]
    kept = [{"name": "line", "interface": "eth2"}]
    assert actions.auto_config({"connections": kept})["config"]["connections"] == kept
