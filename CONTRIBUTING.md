# Contributing

## Prerequisites

- [Zelos](https://zeloscloud.io) 26.0.10 or later
- [Zelos CLI](https://docs.zeloscloud.io/reference/cli/) 0.1.11 or later
- [just](https://github.com/casey/just)
- [uv](https://docs.astral.sh/uv/)

## Layout

`extension.toml` describes everything the extension contributes:

| Part | Files | Manifest |
| ---- | ----- | -------- |
| Agent runtime | `main.py`, the Python package, `pyproject.toml`, `config.schema.json` | `[agent]`, `[config]` |

The agent runtime and its tests:

| Path                                         | Purpose                                                                    |
| -------------------------------------------- | -------------------------------------------------------------------------- |
| `main.py`                                    | Entry point; the app, `demo`, `scan` and `trace` commands                  |
| `zelos_extension_ethercat/protocol.py`       | AL states, CoE data types and bit-packed process data codecs; no bus       |
| `zelos_extension_ethercat/pdo.py`            | Process data mapping from CoE or SII: where each object sits and its name  |
| `zelos_extension_ethercat/master.py`         | One segment with the agent as master: bring-up, cycle thread and recovery  |
| `zelos_extension_ethercat/actions.py`        | The EtherCAT actions                                                       |
| `zelos_extension_ethercat/autoconfig.py`     | List the NICs, and scan the ones the form names                            |
| `zelos_extension_ethercat/constants.py`      | Trace naming and which names are usable                                    |
| `zelos_extension_ethercat/cli/app.py`        | Config load, `connections[]` and `advanced` resolution, startup validation |
| `zelos_extension_ethercat/demo/simulator.py` | The simulated segment behind pysoem's master and slave surface             |
| `tests/test_app.py`                          | The demo segment end to end: bring-up, trace, actions and recovery         |
| `tests/test_pdo.py`                          | PDO mapping from the SII and from CoE, FMMU spans and naming               |
| `tests/test_protocol.py`                     | Value codecs                                                               |
| `tests/conftest.py`                          | Shared test helpers                                                        |

## Commands

| Command                | Description                                                                 |
| ---------------------- | --------------------------------------------------------------------------- |
| `just install`         | Install dependencies and the pre-commit hooks                               |
| `just format`          | Format code with ruff                                                       |
| `just check`           | Check formatting and lint (ruff)                                            |
| `just test`            | Run tests (pytest)                                                          |
| `just package`         | Package for the Zelos marketplace                                           |
| `just release VERSION` | Bump version, check, test, commit, tag                                      |
| `just clean`           | Remove build output, caches and packages                                    |
| `just run`             | Run the extension against `./config.json`                                   |
| `just demo`            | Run a simulated segment with outputs enabled, no hardware                   |
| `just scan [IFACE]`    | List network interfaces, or the slaves on one; needs `CAP_NET_RAW` on Linux |

`just --list` also shows the recipes each part adds. `just install` installs the
pre-commit hooks in `.pre-commit-config.yaml`.

The tests run on the simulator. They need no network interface and no privileges.

## Run it in Zelos

```bash
zelos extensions install-local .
zelos extensions start local.ethercat --config '{"connections": [{"interface": "demo"}]}'
zelos extensions logs local.ethercat
```

A local install runs from your project directory, so you do not install again after a code
change:

- Agent runtime: stop and start the extension. After a dependency change, stop it and run
  `zelos extensions reinstall <id>`.
- `extension.toml`: run `zelos extensions install-local .` again.

The `demo` interface runs the simulated segment, so no hardware is involved.

To run it as a local process instead, put the config in `./config.json`. `just run` connects
to the agent at `ZELOS_AGENT_URL`, else `http://localhost:2300`:

```bash
echo '{"connections": [{"interface": "demo"}]}' > config.json
just run
```

This config leaves outputs disabled. `just demo` runs the same segment with outputs enabled.

A real segment needs a dedicated NIC, and `CAP_NET_RAW` and `CAP_NET_ADMIN` on Linux. The
README covers both.

## Packaging

```bash
just package
```

The archive lands next to `extension.toml` as `<name>-<version>.tar.gz`, where `<name>` is the
manifest name in lowercase with every other run of characters turned into one hyphen. It holds
`extension.toml`, the README, the icon, the config schema and the paths listed in
`[package] paths`. With an agent runtime it also holds `pyproject.toml`, `uv.lock` and
`actions.json`, the list of actions that run while the extension is stopped.

## Release

Add the release to `CHANGELOG.md` and commit it, then:

```bash
just release 1.0.0
git push --follow-tags
```

The release workflow checks, tests and packages the extension, then attaches the archive to a
GitHub release.
