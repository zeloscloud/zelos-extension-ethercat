"""Zelos EtherCAT Extension - EtherCAT support for Zelos.

The agent is the EtherCAT master on a dedicated NIC (pysoem / SOEM): bus scan,
AL state machine, cyclic process data with CoE/SII-named PDOs, diagnostics,
CoE SDO access and guarded output writes. `demo` runs a simulated segment.
"""

#: Action namespace, live and in the packaged inventory; equals `name` in `extension.toml`.
ACTION_PREFIX = "EtherCAT"

__all__ = ["ACTION_PREFIX"]
