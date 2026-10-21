"""Value codecs: the pure seam under process data and the SDO actions."""

import pytest
from zelos_sdk import DataType

from zelos_extension_ethercat import protocol


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


def test_coerce_and_sdo_codec_edges():
    """What the app path does not exercise: hex integers, hex bytes, signed SDO payloads,
    a size mismatch, and a non-boolean."""
    assert protocol.coerce("0x10", DataType.UInt16) == 16
    assert protocol.coerce("de ad", DataType.Binary) == b"\xde\xad"
    with pytest.raises(ValueError):
        protocol.coerce("maybe", DataType.Boolean)
    assert protocol.encode_sdo("-2", "i8") == b"\xfe"
    assert protocol.decode_sdo(b"\x01\x02\x03", "u16") == "010203"  # size mismatch: hex
