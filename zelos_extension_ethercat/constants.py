"""Shared constants: trace naming and which names are usable."""

from collections.abc import Collection

import zelos_sdk

#: Default leading trace-source name (`advanced.prefix`).
DEFAULT_PREFIX = "EtherCAT"

#: Source the extension's logs take when the prefix is cleared; with a prefix
#: they land in it as `<prefix>/log`. Both names are reserved as connection names.
LOG_SOURCE_NAME = "ethercat_log"
RESERVED_CONNECTION_NAMES = ("log", LOG_SOURCE_NAME)


def trace_layout(prefix: str, connection: str) -> tuple[str, str | None]:
    """The one trace-naming rule: (source name, event prefix) for a connection.

    With a prefix, one shared source carries every connection and a connection's
    events read `<connection>/<event>`. Cleared, each connection owns its source
    and the events are unprefixed.
    """
    if prefix:
        return prefix, connection
    return connection, None


def name_error(value: str, label: str, reserved: Collection[str] = ()) -> str | None:
    """Why user-typed `value` is not a legal trace name, or None if it is.

    Names become trace path segments, so a separator (`/ . @ :`) would silently
    re-nest the tree. Anything the SDK sanitizer rewrites is rejected, not renamed.
    """
    if not value:
        return None
    if value in reserved:
        return f"Invalid {label} {value!r}: reserved for the extension's own logs."
    clean = zelos_sdk.sanitize_name(value, kind="source")
    if clean == value:
        return None
    offender = next((c for c, ok in zip(value, clean, strict=False) if c != ok), value[-1])
    return (
        f"Invalid {label} {value!r}: {offender!r} is not allowed. "
        "Use letters, digits, space, '_' or '-'."
    )
