"""EtherCAT actions, free functions registered under `<ACTION_PREFIX>/<name>`.

Free functions, not connection methods: one surface whether or not a connection
runs. Failures raise: the protocol reads its verdict from an exception, so
{"success": False} would report a successful run.
"""

from __future__ import annotations

import inspect
import logging
import sys
from typing import TYPE_CHECKING, Any

import zelos_sdk

from zelos_extension_ethercat import protocol

if TYPE_CHECKING:
    from zelos_sdk.actions import ActionsRegistry

    from zelos_extension_ethercat.master import Segment

logger = logging.getLogger(__name__)

#: Running connections by name.
_connections: dict[str, Segment] = {}


def set_connections(connections: dict[str, Any]) -> None:
    """Bind the connections every action in this module operates on."""
    _connections.clear()
    _connections.update(connections)


def _get_connection(connection: str = "") -> Segment:
    """The named connection; the only one when `connection` is empty."""
    names = ", ".join(_connections)
    if not _connections:
        raise RuntimeError("extension is not running")
    if not connection:
        if len(_connections) == 1:
            return next(iter(_connections.values()))
        raise ValueError(f"Several connections are configured; set connection to one of: {names}")
    found = _connections.get(zelos_sdk.sanitize_name(connection.strip(), kind="source"))
    if found is None:
        raise ValueError(f"Unknown connection '{connection}'. Connections: {names}")
    return found


def _connection_field(func: Any) -> Any:
    return zelos_sdk.action.text(
        "connection",
        title="Connection",
        required=False,
        default="",
        description="Connection name; may be omitted when only one is configured",
    )(func)


def _slave_field(func: Any) -> Any:
    return zelos_sdk.action.text(
        "slave",
        title="Slave",
        description="Position (2), trace name (2_EL1004) or unique name (EL1004); see list_slaves",
    )(func)


def _index(text: str, label: str, limit: int) -> int:
    try:
        value = int(str(text).strip(), 0)
    except ValueError:
        raise ValueError(f"{label} must be a number such as 0x1018 or 4120, not {text!r}") from None
    if not 0 <= value <= limit:
        raise ValueError(f"{label} {value} is outside 0..0x{limit:x}")
    return value


def _run(what: str, call: Any) -> Any:
    """Dispatch a mailbox call; anything not self-describing becomes a RuntimeError."""
    try:
        return call()
    except (ValueError, RuntimeError, TimeoutError):
        raise
    except Exception as e:
        logger.exception("%s failed", what)
        raise RuntimeError(f"{what} failed: {type(e).__name__}: {e}") from e


# ─── Status and discovery ───────────────────────────────────────────────────


@zelos_sdk.action(
    "Get Status",
    "Master state, working counter and cycle statistics; every connection when omitted",
)
@_connection_field
def get_status(connection: str = "") -> dict[str, Any]:
    if connection:
        return _get_connection(connection).status()
    statuses = [c.status() for c in _connections.values()]
    return {"connections": statuses, "count": len(statuses)}


@zelos_sdk.action(
    "List Slaves", "The slaves found on the segment: identity, AL state, mapping and trace names"
)
@_connection_field
def list_slaves(connection: str = "") -> dict[str, Any]:
    segment = _get_connection(connection)
    rows = [s.describe() for s in segment.slaves]
    return {"connection": segment.name, "slaves": rows, "count": len(rows)}


@zelos_sdk.action(
    "List PDOs", "A slave's process data mapping: each PDO event, its entries, offsets and types"
)
@_connection_field
@_slave_field
def list_pdos(slave: str, connection: str = "") -> dict[str, Any]:
    segment = _get_connection(connection)
    info, _ = segment.find_slave(slave)
    pdos = segment.list_pdos(info)
    return {"slave": info.trace_name, "mapping": info.map.source, "pdos": pdos, "count": len(pdos)}


# ─── CoE ────────────────────────────────────────────────────────────────────


@zelos_sdk.action("Read SDO", "Read a CoE object (SDO upload)")
@_connection_field
@_slave_field
@zelos_sdk.action.text("index", title="Index", description="e.g. 0x1018")
@zelos_sdk.action.integer(
    "subindex", title="Subindex", required=False, default=0, minimum=0, maximum=255
)
@zelos_sdk.action.select(
    "data_type",
    title="Data Type",
    choices=["auto", *protocol.SDO_TYPES],
    required=False,
    default="auto",
    description="How to decode the bytes; auto picks an unsigned integer by length",
)
def read_sdo(
    slave: str, index: str, subindex: int = 0, data_type: str = "auto", connection: str = ""
) -> dict[str, Any]:
    segment = _get_connection(connection)
    info, device = segment.find_slave(slave)
    idx = _index(index, "index", 0xFFFF)
    data = _run(
        f"Read of 0x{idx:04x}:{subindex:02x}", lambda: segment.sdo_read(device, idx, int(subindex))
    )
    if data_type == "auto":
        # By size only: a dictionary lookup would walk the OD holding the GIL.
        data_type = {1: "u8", 2: "u16", 4: "u32", 8: "u64"}.get(len(data), "hex")
    return {
        "slave": info.trace_name,
        "index": f"0x{idx:04x}",
        "subindex": int(subindex),
        "data_type": data_type,
        "bytes": data.hex(),
        "value": protocol.decode_sdo(data, data_type),
    }


@zelos_sdk.action(
    "Write SDO",
    "Write a CoE object (SDO download). Changes device parameters and can command drives: "
    "check the manual first. Needs Enable Outputs on the connection",
)
@_connection_field
@_slave_field
@zelos_sdk.action.text("index", title="Index", description="e.g. 0x8000")
@zelos_sdk.action.integer(
    "subindex", title="Subindex", required=False, default=0, minimum=0, maximum=255
)
@zelos_sdk.action.text(
    "value", title="Value", description="Number (0x.. allowed), true/false, text, or hex bytes"
)
@zelos_sdk.action.select(
    "data_type", title="Data Type", choices=list(protocol.SDO_TYPES), default="u8"
)
def write_sdo(
    slave: str,
    index: str,
    value: str,
    subindex: int = 0,
    data_type: str = "u8",
    connection: str = "",
) -> dict[str, Any]:
    segment = _get_connection(connection)
    info, device = segment.find_slave(slave)
    idx = _index(index, "index", 0xFFFF)
    data = protocol.encode_sdo(str(value), data_type)
    _run(
        f"Write of 0x{idx:04x}:{subindex:02x}",
        lambda: segment.sdo_write(device, idx, int(subindex), data),
    )
    return {
        "slave": info.trace_name,
        "index": f"0x{idx:04x}",
        "subindex": int(subindex),
        "bytes": data.hex(),
    }


@zelos_sdk.action(
    "Browse Object Dictionary",
    "List a slave's CoE objects (SDO info), or one object with its entries. The walk "
    "pauses process data, so only while the connection is held below OP: request_state "
    "SAFEOP first, OP again afterwards",
)
@_connection_field
@_slave_field
@zelos_sdk.action.text(
    "index",
    title="Index",
    required=False,
    default="",
    description="One object, e.g. 0x6000; empty lists every object",
)
def browse_od(slave: str, index: str = "", connection: str = "") -> dict[str, Any]:
    segment = _get_connection(connection)
    info, device = segment.find_slave(slave)
    idx = _index(index, "index", 0xFFFF) if index.strip() else None
    objects = _run("Object dictionary read", lambda: segment.od_browse(device, idx))
    return {"slave": info.trace_name, "objects": objects, "count": len(objects)}


# ─── Outputs and state ──────────────────────────────────────────────────────


@zelos_sdk.action(
    "Write Output",
    "Set one process data output; it drives the terminal from the next cycle until changed, "
    "cleared, held below OP or the extension stops. Needs Enable Outputs on the connection "
    "and the slave in OP (not held by request_state)",
)
@_connection_field
@_slave_field
@zelos_sdk.action.text(
    "output",
    title="Output",
    description="`<pdo>/<field>` as traced (Channel 1/Output) or 0x7000:01",
)
@zelos_sdk.action.text(
    "value", title="Value", description="true/false for bits, a number otherwise"
)
def write_output(slave: str, output: str, value: str, connection: str = "") -> dict[str, Any]:
    segment = _get_connection(connection)
    info, _ = segment.find_slave(slave)
    pdo, entry = segment.find_entry(info, output)
    written = segment.write_output(info, entry, str(value))
    return {"slave": info.trace_name, "output": f"{pdo.event}/{entry.field}", "value": written}


@zelos_sdk.action("Clear Outputs", "Every output of the connection back to zero")
@_connection_field
def clear_outputs(connection: str = "") -> dict[str, Any]:
    segment = _get_connection(connection)
    return {"connection": segment.name, "slaves": segment.clear_outputs()}


@zelos_sdk.action(
    "Request State",
    "Request an AL state (INIT, PREOP, SAFEOP, OP) for every slave or one. Below OP the "
    "outputs are zeroed, writes refused, and the state held until OP is requested again",
)
@_connection_field
@zelos_sdk.action.select(
    "state", title="State", choices=list(protocol.REQUESTABLE_STATES), default="OP"
)
@zelos_sdk.action.text(
    "slave", title="Slave", required=False, default="", description="One slave; empty for all"
)
def request_state(state: str, slave: str = "", connection: str = "") -> dict[str, Any]:
    segment = _get_connection(connection)
    info = segment.find_slave(slave)[0] if slave.strip() else None
    reached = _run(f"State change to {state}", lambda: segment.request_state(state, info))
    return {"connection": segment.name, "requested": state, "reached": reached}


# ─── Config-form hooks (standalone: run with the extension stopped or running) ──


@zelos_sdk.action(
    "List Interfaces",
    "Network interfaces on the machine running the agent, as choices for a connection's "
    "Interface field, which also accepts a name typed by hand",
    standalone=True,
    read_only=True,
    hidden=True,
)
def list_interfaces() -> dict[str, Any]:
    from zelos_extension_ethercat.autoconfig import interfaces

    return {
        "status": "success",
        "choices": [{"value": i["name"], "detail": i["detail"]} for i in interfaces()],
    }


@zelos_sdk.action(
    "Auto-configure",
    "Scan each interface the form's connections name (unsaved edits included) and report "
    "the slaves on it; the connections are kept as entered, a demo connection added when "
    "there is none. A scan brings every slave on the interface to INIT: a segment another "
    "master runs there drops out of OP. An interface a running connection owns is not "
    "scanned. Review, then save and start.",
    standalone=True,
    # Not read_only: config_init broadcasts INIT to whatever hangs off a scanned NIC.
    hidden=True,
)
@zelos_sdk.action.object(
    "config",
    properties={},
    title="Config",
    description="The config form's current (possibly unsaved) data. Empty: the saved config",
    required=False,
)
def auto_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    from zelos_extension_ethercat.autoconfig import propose

    if config is None:
        config = _saved_config()
    # Standalone actions also run in the live process: never scan under a running master.
    owners = {
        c.interface: c.name for c in _connections.values() if c.state in ("starting", "running")
    }
    return propose([c for c in config.get("connections") or [] if isinstance(c, dict)], owners)


def _saved_config() -> dict[str, Any]:
    try:
        from zelos_sdk.extensions import load_config

        return load_config() or {}
    except Exception:  # no config yet, or it does not validate
        return {}


def register_actions(registry: ActionsRegistry) -> list[str]:
    """Register every @action function here by bare name; `init(name=ACTION_PREFIX)`
    adds the `EtherCAT/` prefix. Returns the registered names."""
    module = sys.modules[__name__]
    registered: list[str] = []
    for name, obj in inspect.getmembers(module):
        if name.startswith("_"):
            continue
        if inspect.isfunction(obj) and hasattr(obj, "_action"):
            registry.register(obj, name=name)
            registered.append(name)
    logger.info("Registered %d EtherCAT actions", len(registered))
    return registered
