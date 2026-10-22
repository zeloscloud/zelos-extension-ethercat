# CLAUDE.md

## Commands

```bash
just install      # deps + pre-commit hooks
just format       # ruff format + ruff check --fix
just check        # ruff check + ruff format --check
just test         # pytest (the demo segment runs in-process)
just dev          # app mode, reads the Zelos App config
just demo         # simulated EK1100 + EL1004 + 2x EL2004 + EL3004
just scan [IFACE] # interfaces, or the slaves on one (CAP_NET_RAW on Linux)
just package      # zelos extensions package .
```

## Key Files

| Path | Role |
|------|------|
| `main.py` | Click entry point (`demo`, `scan`, `trace`); re-exports `ACTION_PREFIX` |
| `zelos_extension_ethercat/__init__.py` | `ACTION_PREFIX = "EtherCAT"` - must match `name` in `extension.toml` |
| `zelos_extension_ethercat/actions.py` | Free-function action surface + `register_actions()` |
| `zelos_extension_ethercat/master.py` | `Segment`: one connection; bring-up, cycle thread, diagnostics, recovery, action methods |
| `zelos_extension_ethercat/pdo.py` | PDO mapping from CoE (`read_coe_map`) and SII (`SiiImage`, `read_sii_map`), FMMU start bits, naming |
| `zelos_extension_ethercat/protocol.py` | AL states and status text, CoE type -> trace type, bit-packed decode/encode, action text coercion, SDO codec |
| `zelos_extension_ethercat/autoconfig.py` | NIC list for `list_interfaces` / `auto_config`; child-process slave scan for `scan_interface` / `main.py scan` |
| `zelos_extension_ethercat/constants.py` | `trace_layout` (the one trace-naming rule), `name_error`, reserved names |
| `zelos_extension_ethercat/cli/app.py` | Config load, `connections[]` / `advanced` resolution, `init_sdk`, `run_until_stopped` |
| `zelos_extension_ethercat/demo/simulator.py` | `SimMaster` / `SimSlave`: pysoem's surface over a simulated segment; `build_sii` |
| `tests/test_protocol.py`, `tests/test_pdo.py` | The pure seams |
| `tests/test_app.py` | The demo segment end to end: trace, actions, output arming, recovery |

## Architecture

### Startup

- `cli/app.run_config` is the only place that starts anything: config -> `register_actions` -> `init_sdk(prefix)` -> `build_segment` per connection -> `run_until_stopped`.
- Registration must precede `init()` (later actions may never be advertised); `init_global_source(prefix)` precedes `init()` so it is reused, not duplicated.
- Logs: `TraceLoggingHandler` on the prefix source (`<prefix>/log`); cleared prefix, own `ethercat_log` source. `log` and `ethercat_log` are reserved connection names.
- Startup problems are one `logger.error` + `sys.exit(1)`; tracebacks are for bugs. First contact is required: a segment that does not reach OP ends the run with exit 1 (`SegmentError`).

### Segment (`master.py`)

- Backend is duck-typed on pysoem's `Master`/`CdefSlave` surface; `interface == "demo"` opens `SimMaster`. `pysoem.settings.always_release_gil = True` so a long SDO on the actions thread never stalls the cycle (the slaves' SM watchdog would trip). The cycle thread lowers `sys.setswitchinterval` to 1 ms (GIL handoff latency).
- **The GIL is released only around process data, SDO and FoE** (pysoem.pyx 1.1.13). `read_state`, `write_state`, per-slave `state_check`, `reconfig`, `recover`, `_fprd`, `eeprom_read` and `od` hold it. So: no `state_check(EC_TIMEOUTSTATE)` anywhere; the SAFEOP/OP waits poll `read_state` with `time.sleep` and a deadline, and check the stop; `read_sdo` `auto` types by size; `browse_od` is refused in OP. Accepted stalls, on the cycle thread while the WKC is short: `reconfig` (`ecx_reconfig_slave` steps INIT -> PREOP -> SAFEOP at EC_TIMEOUTSTATE each, so up to 6 s for a slave that answers but will not progress; tens of ms healthy), `recover` (a few frames at 500 us), `read_state` ((1 + slaves) x 2 ms).
- **Every state read and write runs on the cycle thread.** `request_state` posts a `StateRequest` that the cycle thread serves between exchanges (hold first, outputs zeroed, then each rung of PREOP -> SAFEOP -> OP written and polled, EC_TIMEOUTSTATE per rung, only to the slaves below it); the action waits on its event. Nothing on the actions thread calls `read_state` / `write_state` / `state_check`, so the check loop's `slave.state = x; write_state()` cannot be clobbered.
- Bring-up is SOEM's simple_test: `config_init` -> per-slave mapping -> `config_map` (a `ConfigMapError` is logged and the run goes on; the span checks guard the mapping) -> `config_dc` -> FMMU spans -> SAFEOP wait (8 s) -> zero outputs + one exchange -> request OP, exchanging every cycle while polling (10 s). EMCY messages are logged per slave (`add_emergency_callback`; pysoem delivers them during that slave's mailbox calls).
- Mapping: SII word 0x1C says whether the slave has a CoE mailbox; then `read_coe_map` (0x1C00 SM types from SM2 with SOEM's slave-bug corrections, 0x1C1x assignments, 0x16xx/0x1Axx entries, names and types from `slave.od` when SDO info exists; any failure of the walk -> SII; SOEM's error ring is drained after every OD read, since the walk never pops it and pysoem's next SDO on any slave would raise or assert on it), else `read_sii_map` (TxPDO 50 / RxPDO 51 categories, names from the string category). A PDO's direction is the SM type / category, not its index. Offsets are the running bit count per direction in SM then PDO order, each further SM starting on a byte, as SOEM lays the image out.
- **Bit-sized slaves share bytes.** A slave with fewer than 8 bits is bit-oriented: SOEM packs it at the bit level after its predecessor and pysoem's `slave.input`/`output` is a 1-byte view that ignores `Istartbit`. `read_fmmus` reads the programmed FMMUs (0x0600, `ec_fmmut`: LogStartbit at byte 6, LogLength at 4, type at 11, active at 12) into `FmmuSpan(start_bit, bits)`; `span_problem` refuses a direction whose span straddles past the view (the third of EL2002 + EL2004 + EL2004) or whose mapped bits disagree with the FMMU length: inputs not logged, writes refused, one warning at bring-up, `input_problem`/`output_problem` in `list_slaves`.
- Cycle thread (daemon): monotonic schedule, resync when a whole period late; `send_processdata`/`receive_processdata(timeout = min(2 ms, period/2))`; `wkc < 0` counts `lost_frames`, `wkc < expected` counts `wkc_errors`. **The expected WKC is ours, not pysoem's** (`_wkc_share`, recomputed at every `_read_states`): per slave +1 with a read FMMU, +2 with a write FMMU, inputs only in SAFEOP, nothing below SAFEOP or lost; SOEM's plain map counts a slave only when it advances the logical address, so bit slaves go uncounted (pysoem's value is logged at bring-up). A slave below SAFEOP or lost logs no PDOs. **Stall guard**: `OUTPUT_WATCHDOG_MS` without a complete exchange drops every output slave's outputs through `_drop_outputs` before the next send (an ESC-only terminal stays in OP through a watchdog trip, so the next write would drive the old outputs again); PDOs decode from pysoem's per-slave views and log on change, all once per `diagnostics_interval` with `_master` and `_state`. **No PDO is logged while the WKC is short** (the image holds stale inputs), the forced per-interval log included.
- Recovery is simple_test's ecatcheck, run from the cycle thread at most every 100 ms while `wkc < expected` or a check is pending (the diagnostics tick sets it for any slave out of its requested state, since the expected WKC follows the states): ack SAFEOP+ERROR, re-request OP from SAFEOP, `reconfig` a slave in another state, mark NONE as lost, `recover` a lost one. `reconfig` pauses process data for the whole segment (GIL, up to 6 s), so one slave gets it at most every `RECONFIG_BACKOFF` (10 s) and is only observed in between; the backoff clears when the slave reaches OP or a `request_state` targets it. One WARNING per attempt with the measured pause, one INFO when it is back in OP, `reconfigs` on its `_state`. The SAFEOP+ERROR and "requesting OP" warnings are once per slave per state until it is next in OP, plus a "still out of OP" summary at most every 60 s. Every reconfig / recover re-arms the output watchdog (0x0420 resets on a power cycle). simple_test runs ecatcheck on its own thread; a GIL-releasing `reconfig` plus a check thread is the follow-up. A slave seen out of OP has its staged outputs zeroed (`_drop_outputs`: once, logged, `outputs_cleared` on its `_state` until the next `write_output`), so they stay zero after recovery; with `restore_outputs` the image is untouched and the last written outputs are driven again. A held slave (`SlaveInfo.requested_state` below OP; a segment-wide request sets every slave's plus `_requested_state`), is never pushed toward OP but is still flagged lost and recovered, then brought back to its held state by `_step_held`: `reconfig` first, under the same backoff (after a power cycle `recover` restores only the station address, `reconfig` the SM and FMMU registers; it stops at SAFEOP), then the held state written down to that slave if lower; an ERROR flag is acknowledged first; one INFO once there, `stepping` meanwhile. Any slave while a request is in flight is only watched for loss.
- Outputs: a staged image per slave (pysoem view coordinates); `_apply_outputs` merges only the slave's own bit span into `slave.output`, since bit-sized neighbours share the byte. `_stage_zero` zeroes every writable slave (before OP, in `_bring_down`, `clear_outputs`, a segment-wide hold) or one (a per-slave hold). `write_output` refuses unless `enable_outputs`, while the segment or that slave is held below OP, and for a slave with an `output_problem` (an FMMU that was not read back counts: writes would land on a neighbour's bits).
- Mailbox calls from the actions thread (SDO, OD walk) take `_mbx_lock`; the cycle never does (SOEM serialises datagrams at the port).
- Shutdown: `run()` races the thread against the stop event; the bring-up waits raise on stop; `_bring_down` then close; `SHUTDOWN_TIMEOUT` 3 s in the app, then a 0.5 s trace flush and `os._exit(1)` (a daemon cycle thread stuck in a GIL-holding call cannot hold the exit).

### Config

- `connections[]`: `name` (defaults to the interface), `interface`, `cycle_time` ms, `enable_outputs`, `restore_outputs_after_recovery`; one master per interface. `advanced`: `prefix`, `diagnostics_interval`, `log_level`. `resolve_advanced` merges over `ADVANCED_DEFAULTS`; an absent `prefix` is the default, an empty one clears it.
- Prefix and connection names go through `name_error`; duplicate names or interfaces are hard errors.
- `demo` replaces the connections with one `demo` connection on the simulator, outputs enabled; `trace` builds a one-entry config through the same path.

### Trace layout

- `trace_layout(prefix, connection)`: with a prefix, one source `<prefix>` and events `<connection>/<event>`; cleared, one source per connection with unprefixed events.
- Below the connection: `_master`, `<position>_<slave>/_state`, `<position>_<slave>/<pdo event>` (`assign_names`: sanitized PDO name or `rxpdo_1600`/`txpdo_1a00`, index suffix on collision; fields sanitized entry names or `0x6000_01`; fillers skipped).

### Actions

- Free functions in `actions.py`, bound via `set_connections`. Optional `connection`: `ValueError` listing names when omitted with several; `get_status` covers all. Slaves by position, trace name or unique device name (`Segment.find_slave`).
- Failure convention: **raise**. Input errors `ValueError`; not running `RuntimeError`; unknown mailbox errors become `RuntimeError` after `logger.exception` (`_run`).
- Standalone + hidden + `read_only`: `list_interfaces` (the schema's `action-choices` for Interface) and `auto_config` (the schema's `ui:options.autoconfig`; interface list in the message, form connections kept, demo when empty). `auto_config` never scans: `config_init` broadcasts INIT and drops a segment another master runs out of OP, so only a named interface is scanned (TwinCAT, SOEM slaveinfo, IgH convention). Keep the schema in step.
- `scan_interface` (standalone, not `read_only`): child-process scan of one named NIC, 5 s deadline; refuses an interface a running connection owns (standalone actions also run in the live process).
- `browse_od` raises in OP (the OD walk pauses process data): `request_state SAFEOP` first.

### Simulator

- Mirrors the planned bench: EK1100 (alias 1001), EL1004 and two EL2004 via SII (`build_sii` writes ETG.2010 categories; `_digital` builds any ELx00y from `DIGITAL`), EL3004 via CoE with an object dictionary (0x1008, 0x1018, 0x1C00, 0x1C13, 0x1A0x, 0x60x0, 0x80x0). One `io_map` laid out as SOEM's `ecx_config_map_group` (`_pack`: outputs then inputs, bit slaves packed at the bit level, byte slaves aligned), `_fprd(0x0600)` answers correctly packed `ec_fmmut` registers, views are pysoem's widths (1 byte for a bit slave, `(bits + 7) // 8` otherwise), `output` setter is a memcpy as pysoem's. Real-segment fidelity: `expected_wkc` is SOEM's undercount; the WKC counts +1/+2 per slave, nothing below SAFEOP, no outputs in SAFEOP for a uC (mailbox) slave; `reconfig` stops at SAFEOP; clearing `present` is a power cycle (SM/FMMU and watchdog registers lost until `reconfig`, SAFEOP refused 0x0017 meanwhile); an ERROR needs an ack; a request skipping a rung is refused 0x0011; the SM watchdog trips (`wd_trips`): ESC-only stays in OP, a uC slave goes SAFEOP+ERROR 0x001B. Digital inputs: channels 1-2 follow the first EL2004's outputs, 3-4 free-run; EL3004 sine, triangle, noise, constant. A test drops a slave by clearing `present` (and `_current`) and puts it back. `SimMaster(layout=(...))` builds other chains (the straddle test).

## Code Style

- ruff, line length 100, google docstrings
- `contextlib.suppress(Exception)` over bare `try/except: pass`
- comments state the constraint, not the code
