"""Value codecs and AL state decoding: the pure seam under process data and the SDO actions."""

import pytest
from zelos_sdk import DataType

from zelos_extension_ethercat import protocol


def test_al_state_names_and_status_text():
    assert protocol.al_state_name(0x08) == "OP"
    assert protocol.al_state_name(0x14) == "SAFEOP+ERROR"
    assert protocol.al_state_name(0x00) == "NONE"
    assert protocol.al_status_text(0) == "No error"
    # ETG.1000.6 Table 15, via SOEM's table.
    assert protocol.al_status_text(0x001B) == "Sync manager watchdog"
    assert protocol.al_status_text(0x0011) == "Invalid requested state change"


def test_trace_type_from_coe_type_then_width():
    assert protocol.trace_type(0x0003, 16) == DataType.Int16
    assert protocol.trace_type(0x0031, 2) == DataType.UInt8  # BIT2
    assert protocol.trace_type(0, 1) == DataType.Boolean
    assert protocol.trace_type(0, 12) == DataType.UInt16
    assert protocol.trace_type(0, 100) == DataType.Binary


def test_decode_and_encode_bit_packed_round_trip():
    # An EL3004 channel: status bits then an INT16 value, at bit offset 4 of a shared byte.
    image = bytearray(6)
    protocol.insert_bits(image, 4 + 15, 1, 1)  # TxPDO Toggle
    protocol.insert_bits(image, 4 + 16, 16, protocol.encode(-1234, 16, DataType.Int16))
    assert protocol.decode(bytes(image), 4 + 15, 1, DataType.Boolean) is True
    assert protocol.decode(bytes(image), 4 + 16, 16, DataType.Int16) == -1234
    assert protocol.decode(bytes(image), 4, 1, DataType.Boolean) is False
    raw = protocol.encode(1.5, 32, DataType.Float32)
    assert protocol.decode(raw.to_bytes(4, "little"), 0, 32, DataType.Float32) == 1.5
    with pytest.raises(ValueError):
        protocol.encode(300, 8, DataType.UInt8)
    with pytest.raises(ValueError):
        protocol.encode(-129, 8, DataType.Int8)


def test_coerce_action_text():
    assert protocol.coerce("true", DataType.Boolean) is True
    assert protocol.coerce("0", DataType.Boolean) is False
    assert protocol.coerce("0x10", DataType.UInt16) == 16
    assert protocol.coerce("de ad", DataType.Binary) == b"\xde\xad"
    with pytest.raises(ValueError):
        protocol.coerce("maybe", DataType.Boolean)


def test_sdo_codec():
    assert protocol.encode_sdo("0x1234", "u16") == b"\x34\x12"
    assert protocol.encode_sdo("-2", "i8") == b"\xfe"
    assert protocol.encode_sdo("EL3004", "string") == b"EL3004"
    assert protocol.decode_sdo(b"\x34\x12", "u16") == 0x1234
    assert protocol.decode_sdo(b"EL3004", "string") == "EL3004"
    assert protocol.decode_sdo(b"\x01\x02\x03", "u16") == "010203"  # size mismatch: hex
    assert protocol.SDO_TYPE_OF_COE[0x0007] == "u32"
    assert protocol.SDO_TYPE_OF_COE[0x0009] == "string"
