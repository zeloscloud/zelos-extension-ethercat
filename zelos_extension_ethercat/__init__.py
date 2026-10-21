"""Zelos EtherCAT Extension - EtherCAT support for Zelos.

The agent is the EtherCAT master on a dedicated NIC (pysoem / SOEM): bus scan,
AL state machine, cyclic process data with CoE/SII-named PDOs, diagnostics,
CoE SDO access and guarded output writes. `demo` runs a simulated segment.
"""

#: Action namespace for this extension. Single source for both surfaces - the
#: live registration (`zelos_sdk.init(name=ACTION_PREFIX)`) and the at-rest
#: inventory the packaging step dumps from `main.py`, which re-exports this.
#: A mismatch would silently produce two unrelated action trees.
#:
#: Matches `name` in `extension.toml`, which is what a user sees in the
#: extension list, so the address they read there is the address they type.
ACTION_PREFIX = "EtherCAT"

__all__ = ["ACTION_PREFIX"]
