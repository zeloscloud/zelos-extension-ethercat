# Changelog

All notable changes to Zelos EtherCAT Extension will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- The agent as EtherCAT master on a dedicated NIC (pysoem 1.1.13 / SOEM, prebuilt wheels): bus scan, AL state machine with status codes decoded, cyclic process data on a dedicated thread, working counter and cycle statistics. The expected working counter is computed per slave from its programmed FMMUs and its AL state (SOEM's own count misses bit-sized terminals); pysoem's value is logged at bring-up for comparison.
- Per-slave PDO events named from the CoE PDO mapping (0x1C1x, 0x16xx/0x1Axx, object dictionary) or the SII PDO categories for slaves without a mailbox; datatypes from the mapping entries.
- Trace layout `EtherCAT/<connection>/<position>_<slave>/<pdo>`, with `_master` and per-slave `_state` diagnostics events.
- Actions: `get_status`, `list_slaves`, `list_pdos`, `read_sdo`, `write_sdo`, `browse_od`, `write_output`, `clear_outputs`, `request_state`; config-form hooks `list_interfaces`, `auto_config`. Auto-configure scans only the interfaces the form's connections name and reports their slaves: a scan brings every slave on the NIC to INIT, so a segment another master runs there would drop out of OP. An interface a running connection owns is not scanned.
- Outputs disabled by default: never written at start, zero on stop; `write_output` needs Enable Outputs on the connection and refuses a slave held below OP; `write_sdo` needs Enable Outputs too (an SDO can command a drive); reads stay ungated.
- Recovery of slaves that fall out of OP or drop off the bus, after SOEM's simple_test. A recovered slave's outputs stay zero until written again (`outputs_cleared` on its `_state` event); `restore_outputs_after_recovery` drives the last written outputs again instead, as PLC masters do. A slave that answers but will not progress is reconfigured at most every 10 s, since the reconfig pauses process data for the segment; `reconfigs` counts the attempts on its `_state` event. SAFEOP+ERROR is acknowledged on every check pass, as SOEM and TwinCAT (Auto Restore States) do; `error_acks` counts them. A slave held below OP by `request_state` that drops out and is recovered is stepped back to its held state, never above it. With outputs enabled, each output slave gets a 500 ms process data watchdog at bring-up and again after every reconfig or recover, so its outputs drop if the master dies; a slave that rejects it has its outputs refused. 500 ms without a complete exchange (a stalled cycle) counts as a drop-out of every output slave, so outputs do not come back by themselves. Recovery warnings are once per slave per state, with a summary at most every 60 s. CoE emergency (EMCY) messages are logged per slave.
- `demo`: a simulated EK1100 + EL1004 + 2x EL2004 + EL3004 segment; `scan` and `trace` CLI commands.
- Bit-sized terminals sharing a byte are decoded and written at their FMMU start bit; one straddling a byte is reported (`input_problem` / `output_problem`), its inputs not logged and its outputs refused.
- No GIL-holding SOEM waits on the cycle path: bring-up and `request_state` poll; `browse_od` only while held below OP; the forced exit past the 3 s shutdown bound.

### Changed
- Needs Zelos 26.0.10 or later.
- Zelos AI runs Get Status, List Slaves, List PDOs and Read SDO without asking first: they only read.

---

*For maintainers: When releasing, move items from [Unreleased] to a new version section:*

```markdown
## [0.1.0] - 2026-01-01

### Added
- (move items from Unreleased here)
```
