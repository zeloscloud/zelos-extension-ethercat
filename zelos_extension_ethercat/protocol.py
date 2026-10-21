"""EtherCAT protocol constants and value codecs: AL states, CoE data types, bit-packed
process data. Pure Python, no bus; the seam the unit tests cover.

References: ETG.1000.6 (AL state machine, data type index), SOEM ethercattype.h.
"""

from __future__ import annotations

import struct

import zelos_sdk
from zelos_sdk import DataType

# AL control / status register values (ETG.1000.6 Table 9; SOEM ec_state).
NONE, INIT, PREOP, BOOT, SAFEOP, OP = 0x00, 0x01, 0x02, 0x03, 0x04, 0x08
#: Bit 4 of the AL status is the error indication; written back it acknowledges it.
ERROR_FLAG = 0x10

#: As the TwinCAT online tab spells them.
AL_STATE_NAMES = {
    NONE: "NONE",
    INIT: "INIT",
    PREOP: "PREOP",
    BOOT: "BOOT",
    SAFEOP: "SAFEOP",
    OP: "OP",
}
#: Which states an action may request; BOOT needs a firmware flow this extension lacks.
REQUESTABLE_STATES = {"INIT": INIT, "PREOP": PREOP, "SAFEOP": SAFEOP, "OP": OP}


def al_state_name(state: int) -> str:
    """`OP`, `SAFEOP+ERROR`, ... for an AL status value."""
    base = AL_STATE_NAMES.get(state & 0x0F, f"0x{state & 0x0F:02X}")
    return f"{base}+ERROR" if state & ERROR_FLAG else base


def al_status_text(code: int) -> str:
    """The ETG.1000.6 Table 15 text of an AL status code (SOEM's table via pysoem)."""
    if code == 0:
        return "No error"
    import pysoem  # deferred: the demo and the tests never open a NIC

    return pysoem.al_status_code_to_string(code)


# CoE data type index (ETG.1000.6 Table 63; CiA 301 7.4.7) -> trace data type.
_COE_TYPES: dict[int, DataType] = {
    0x0001: DataType.Boolean,  # BOOL
    0x0002: DataType.Int8,  # SINT
    0x0003: DataType.Int16,  # INT
    0x0004: DataType.Int32,  # DINT
    0x0005: DataType.UInt8,  # USINT
    0x0006: DataType.UInt16,  # UINT
    0x0007: DataType.UInt32,  # UDINT
    0x0008: DataType.Float32,  # REAL
    0x0009: DataType.String,  # VISIBLE_STRING
    0x000A: DataType.Binary,  # OCTET_STRING
    0x000B: DataType.String,  # UNICODE_STRING
    0x0010: DataType.Int32,  # INT24
    0x0011: DataType.Float64,  # LREAL
    0x0012: DataType.Int64,  # INT40
    0x0013: DataType.Int64,  # INT48
    0x0014: DataType.Int64,  # INT56
    0x0015: DataType.Int64,  # LINT
    0x0016: DataType.UInt32,  # UINT24
    0x0017: DataType.UInt64,  # UINT40
    0x0018: DataType.UInt64,  # UINT48
    0x0019: DataType.UInt64,  # UINT56
    0x001B: DataType.UInt64,  # ULINT
    0x001E: DataType.UInt8,  # BYTE
    0x001F: DataType.UInt16,  # WORD
    0x0020: DataType.UInt32,  # DWORD
    0x002D: DataType.UInt8,  # BITARR8
    0x002E: DataType.UInt16,  # BITARR16
    0x002F: DataType.UInt32,  # BITARR32
    0x0030: DataType.Boolean,  # BIT1
}
_COE_TYPES.update(dict.fromkeys(range(49, 56), DataType.UInt8))  # BIT2..BIT8

_UNSIGNED = (DataType.UInt8, DataType.UInt16, DataType.UInt32, DataType.UInt64)
_SIGNED = (DataType.Int8, DataType.Int16, DataType.Int32, DataType.Int64)
_FLOATS = {DataType.Float32: "<f", DataType.Float64: "<d"}


def trace_type(coe_type: int, bit_length: int) -> DataType:
    """The trace type of a PDO entry: its CoE type, else by width (an unknown or unlisted
    type still traces as the integer of its width)."""
    found = _COE_TYPES.get(coe_type)
    if found is not None:
        return found
    if bit_length == 1:
        return DataType.Boolean
    for width, kind in ((8, DataType.UInt8), (16, DataType.UInt16), (32, DataType.UInt32)):
        if bit_length <= width:
            return kind
    return DataType.UInt64 if bit_length <= 64 else DataType.Binary


def extract_bits(image: bytes, bit_offset: int, bit_length: int) -> int:
    """The unsigned value of `bit_length` bits at `bit_offset` of a little-endian image."""
    start, end = bit_offset // 8, (bit_offset + bit_length + 7) // 8
    raw = int.from_bytes(image[start:end], "little") >> (bit_offset % 8)
    return raw & ((1 << bit_length) - 1)


def insert_bits(image: bytearray, bit_offset: int, bit_length: int, raw: int) -> None:
    """Overwrite `bit_length` bits at `bit_offset` of `image` with `raw`, in place."""
    start, end = bit_offset // 8, (bit_offset + bit_length + 7) // 8
    mask = ((1 << bit_length) - 1) << (bit_offset % 8)
    current = int.from_bytes(image[start:end], "little")
    merged = (current & ~mask) | ((raw << (bit_offset % 8)) & mask)
    image[start:end] = merged.to_bytes(end - start, "little")


def decode(image: bytes, bit_offset: int, bit_length: int, kind: DataType) -> object:
    """A PDO entry's value from the process image."""
    raw = extract_bits(image, bit_offset, bit_length)
    if kind == DataType.Boolean:
        return bool(raw)
    if kind in _SIGNED:
        return raw - (1 << bit_length) if raw & (1 << (bit_length - 1)) else raw
    if kind in _FLOATS:
        return struct.unpack(_FLOATS[kind], raw.to_bytes(bit_length // 8, "little"))[0]
    if kind == DataType.String:
        return raw.to_bytes(bit_length // 8, "little").decode("ascii", "replace").rstrip("\0")
    if kind == DataType.Binary:
        return raw.to_bytes((bit_length + 7) // 8, "little")
    return raw


def encode(value: object, bit_length: int, kind: DataType) -> int:
    """A value as the raw bits of a PDO entry.

    :raises ValueError: the value does not fit the entry
    """
    limit = 1 << bit_length
    if kind == DataType.Boolean:
        return 1 if value else 0
    if kind in _FLOATS:
        return int.from_bytes(struct.pack(_FLOATS[kind], float(value)), "little")  # type: ignore[arg-type]
    if kind in (DataType.String, DataType.Binary):
        data = value.encode("ascii") if isinstance(value, str) else bytes(value)  # type: ignore[arg-type]
        if len(data) * 8 > bit_length:
            raise ValueError(f"{len(data)} bytes do not fit {bit_length} bits")
        return int.from_bytes(data.ljust((bit_length + 7) // 8, b"\0"), "little")
    number = int(value)  # type: ignore[call-overload]
    low, high = (-(limit // 2), limit // 2 - 1) if kind in _SIGNED else (0, limit - 1)
    if not low <= number <= high:
        raise ValueError(f"{number} is outside {low}..{high} for a {bit_length}-bit {kind}")
    return number & (limit - 1)


def coerce(text: str, kind: DataType) -> object:
    """A typed value from action text: `true`/`false`/`1`/`0` for bools, `0x..` allowed
    for integers, hex digits for binary.

    :raises ValueError: the text is not a value of `kind`
    """
    text = text.strip()
    if kind == DataType.Boolean:
        lowered = text.lower()
        if lowered in ("true", "1", "on", "yes"):
            return True
        if lowered in ("false", "0", "off", "no"):
            return False
        raise ValueError(f"{text!r} is not a boolean (true/false)")
    if kind in _FLOATS:
        return float(text)
    if kind == DataType.String:
        return text
    if kind == DataType.Binary:
        return bytes.fromhex(text.replace(" ", ""))
    return int(text, 0)


#: What the SDO actions accept as `data_type`, and how many bytes each is on the wire
#: (0 = as long as the text).
SDO_TYPES: dict[str, tuple[DataType, int]] = {
    "u8": (DataType.UInt8, 1),
    "u16": (DataType.UInt16, 2),
    "u32": (DataType.UInt32, 4),
    "u64": (DataType.UInt64, 8),
    "i8": (DataType.Int8, 1),
    "i16": (DataType.Int16, 2),
    "i32": (DataType.Int32, 4),
    "i64": (DataType.Int64, 8),
    "f32": (DataType.Float32, 4),
    "f64": (DataType.Float64, 8),
    "string": (DataType.String, 0),
    "hex": (DataType.Binary, 0),
}

#: `data_type` names by CoE type index, for the `auto` choice of the SDO actions.
SDO_TYPE_OF_COE: dict[int, str] = {
    coe: name
    for name, (kind, _) in reversed(list(SDO_TYPES.items()))
    for coe, found in _COE_TYPES.items()
    if found == kind and coe < 0x0030
}


def encode_sdo(text: str, data_type: str) -> bytes:
    """SDO payload bytes from action text and a `SDO_TYPES` name."""
    kind, size = SDO_TYPES[data_type]
    value = coerce(text, kind)
    if size == 0:
        return value.encode("ascii") if isinstance(value, str) else bytes(value)  # type: ignore[arg-type]
    return encode(value, size * 8, kind).to_bytes(size, "little")


def decode_sdo(data: bytes, data_type: str) -> object:
    """An SDO payload as a value; a size mismatch falls back to hex."""
    kind, size = SDO_TYPES[data_type]
    if size and len(data) != size:
        return data.hex()
    if size == 0:
        return data.hex() if kind == DataType.Binary else decode(data, 0, len(data) * 8, kind)
    return decode(data, 0, size * 8, kind)


def sanitize(value: str, kind: str = "source") -> str:
    """A device-provided name as the trace catalog takes it; `kind` as `sanitize_name`."""
    return zelos_sdk.sanitize_name(value.strip(), kind=kind).strip() if value.strip() else ""
