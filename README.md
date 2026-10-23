# Zelos EtherCAT

A Zelos extension that makes the agent the EtherCAT master of a segment on a dedicated network interface: scan the slaves, bring them to OP, exchange process data every cycle, trace every PDO by name, watch the working counter and AL states, and reach CoE objects.

## Features

- 🔎 **Bus scan**: every slave with vendor, product, revision, alias and position
- 🔁 **State machine**: INIT, PREOP, SAFEOP, OP with AL status codes decoded to text
- 📡 **Process data**: cyclic exchange on its own thread; every PDO entry traced by the name the device gives it (CoE object dictionary or SII), typed from the mapping
- 📈 **Diagnostics**: working counter expected vs actual, lost frames, cycle time and jitter, per-slave AL state and status code
- 🧰 **CoE**: SDO read and write, object dictionary browsing where the slave supports SDO info
- 🛡️ **Outputs off by default**: never written at start, zero on stop, driven only through `write_output` on a connection with Enable Outputs, which also gates `write_sdo`
- ♻️ **Recovery**: a slave that falls out of OP or drops off the bus is brought back, as SOEM's reference master does; its outputs stay zero until written again unless Restore Outputs After Recovery is on; a slave that will not progress is reconfigured at most every 10 s, since that pauses process data for up to 6 s and can trip the other slaves' output watchdogs; a slave held below OP comes back to its held state, never above it
- 🏭 **Multiple segments**: one connection per interface
- 🧪 **Demo mode**: a simulated EK1100 + EL1004 + 2x EL2004 + EL3004 segment, no hardware

## Quick Start

```bash
zelos extensions install zeloscloud/zelos-extension-ethercat
```

In the app:

1. **Install** the extension from the Zelos App
2. **Configure** a connection: pick the interface the segment hangs off from the Interface dropdown, then press Auto-configure to list the slaves on it (the scan brings them to INIT, so a segment another master runs there drops out of OP)
3. **Start** the extension
4. **View** data in your Zelos App: `EtherCAT/<connection>/<position>_<slave>/<pdo>`

### Privileges on Linux

Sending raw EtherCAT frames needs `CAP_NET_RAW` and `CAP_NET_ADMIN`. The agent runs the extension, so give the agent's service those capabilities and its children inherit them:

```ini
# sudo systemctl edit zelos-agent
[Service]
AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN
```

then `sudo systemctl daemon-reload && sudo systemctl restart zelos-agent`. An agent running as root needs nothing. The alternative, `sudo setcap cap_net_raw,cap_net_admin+ep "$(readlink -f .venv/bin/python)"` on the extension's interpreter, grants the capabilities to every extension sharing that uv-managed Python and needs uv 0.7.9 or newer (older interpreters load `libpython` through an `$ORIGIN` rpath, which the loader refuses for a capability-bearing binary).

The interface must be dedicated to EtherCAT: no IP address, no other traffic. Windows needs Npcap in WinPcap-compatible mode, as any EtherCAT master there.

## Configuration

### Connections

| Setting | Default | Description |
|---------|---------|-------------|
| Name | interface | Trace name; unique |
| Interface | | Network interface of the segment (`eth1`); `demo` runs the simulator |
| Cycle Time (ms) | 10 | Process data period, 0.1 to 100 |
| Enable Outputs | off | Allow `write_output` and `write_sdo`; off keeps every output at zero and refuses both |
| Restore Outputs After Recovery | off | Off: a slave that drops out of OP has its outputs zeroed and they stay zero after recovery until written again (no automatic restart). On: the last written outputs are driven again once it is back in OP, as PLC masters do |

### Advanced

Applies to every connection.

| Setting | Default | Description |
|---------|---------|-------------|
| `prefix` | `EtherCAT` | Trace source: `EtherCAT/<connection>/...`; cleared = one source per connection |
| `diagnostics_interval` | 1.0 s | Period of the `_master` and `_state` events, and of repeating an unchanged PDO |
| `log_level` | `INFO` | Logging verbosity |

## Trace layout

| Event | Fields |
|-------|--------|
| `<connection>/_master` | `slaves`, `al_state`, `al_state_name`, `wkc`, `expected_wkc`, `wkc_ok`, `wkc_errors`, `lost_frames`, `cycles`, `cycle_time_us`, `jitter_us`, `dc_time_ns` |
| `<connection>/<slave>/_state` | `al_state`, `al_state_name`, `al_status_code`, `al_status_text`, `lost`, `outputs_cleared`, `reconfigs`, `error_acks` |
| `<connection>/<slave>/<pdo>` | One field per mapped entry, e.g. `2_EL1004/Channel 1` with `Input`, `5_EL3004/AI Standard Channel 1` with `Underrange` ... `Value` |

Slaves are `<position>_<name>` (`2_EL1004`), so two identical terminals stay apart. A PDO is logged when a value changes and once per diagnostics interval, only on cycles whose working counter is complete for the states the slaves are in (a slave in SAFEOP counts its inputs only; one below SAFEOP or lost counts nothing and logs nothing).

Terminals with fewer than 8 bits (EL1002, EL2004, ...) share bytes; one whose bits straddle a byte boundary (the third of EL2002 + EL2004 + EL2004) is reported by `list_slaves` with `input_problem` / `output_problem`: its inputs are not logged and its outputs refused, the neighbours are unaffected.

## Actions

| Action | Description |
|--------|-------------|
| `get_status` | Master state, working counter and cycle statistics |
| `list_slaves` | Identity, AL state, mapping source and trace names of every slave |
| `list_pdos` | A slave's PDO entries with offsets and types |
| `read_sdo` | CoE SDO upload, decoded by size unless a data type is given |
| `write_sdo` | CoE SDO download with an explicit data type; needs Enable Outputs |
| `browse_od` | The object dictionary, or one object with its entries; pauses process data, so only while the connection is held below OP (`request_state` SAFEOP first) |
| `write_output` | Set one output (`Channel 1/Output` or `0x7000:01`); needs Enable Outputs and the slave in OP |
| `clear_outputs` | Every output back to zero |
| `request_state` | Request INIT, PREOP, SAFEOP or OP for every slave or one; below OP outputs are zeroed and the master holds there, with `write_output` refused, until OP is requested again |

With several connections, pass `connection` to pick one. Slaves are addressed by position, trace name or unique device name.

```bash
zelos actions execute EtherCAT/list_slaves
zelos actions execute EtherCAT/read_sdo '{"slave": "5", "index": "0x1018", "subindex": 1}'
zelos actions execute EtherCAT/write_output '{"slave": "3", "output": "Channel 1/Output", "value": "true"}'
```

## Development

```bash
just install      # deps + pre-commit hooks
just format       # ruff format + fix
just check        # ruff lint + format check
just test         # pytest
just dev          # run app mode locally
just demo         # simulated segment
just scan [IFACE] # interfaces, or the slaves on one
```

## Links

- [Zelos Documentation](https://docs.zeloscloud.io)
- [SDK Guide](https://docs.zeloscloud.io/sdk)
- [GitHub Issues](https://github.com/zeloscloud/zelos-extension-ethercat/issues)

## CLI Usage

No installation required, just use `uv run`:

```bash
uv run main.py                       # app mode (Zelos App config)
uv run main.py demo                  # simulated segment, outputs enabled
uv run main.py scan                  # network interfaces
uv run main.py scan eth1             # slaves on eth1
uv run main.py trace eth1 --cycle-time 5 --enable-outputs
```

## Support

For help and support:
- 📖 [Zelos Documentation](https://docs.zeloscloud.io)
- 🐛 [GitHub Issues](https://github.com/zeloscloud/zelos-extension-ethercat/issues)
- 📧 help@zeloscloud.io

## License

MIT License - see [LICENSE](LICENSE) for details.

EtherCAT® is registered trademark and patented technology, licensed by Beckhoff Automation GmbH & Co. KG, Germany.

---

**Built with [Zelos](https://zeloscloud.io)**
