"""PDO mapping from the SII and from CoE, FMMU spans, and trace naming."""

from zelos_sdk import DataType

from zelos_extension_ethercat import pdo
from zelos_extension_ethercat.demo import simulator


def test_sii_map_names_and_offsets():
    """A two-SM, two-PDO TxPDO category plus an inactive PDO (SyncM 0xFF) and a padding entry."""
    strings = ["Status", "Value", "Channel 1", "Channel 2", "Unused"]
    txpdos = [
        (
            0x1A00,
            3,
            "Channel 1",
            [(0x6000, 1, "Status", 0x05, 8), (0x6000, 0x11, "Value", 0x03, 16)],
        ),
        (0x1A10, 0xFF, "Unused", [(0x6010, 1, "Status", 0x05, 8)]),
        (0x1A01, 3, "Channel 2", [(0, 0, "", 0, 4), (0x6001, 0x11, "Value", 0x03, 16)]),
    ]
    image = simulator.build_sii(2, 0x1234, 1, 0, strings, txpdos, [])
    found = pdo.read_sii_map(pdo.SiiImage.from_bytes(image))
    pdo.assign_names(found)

    assert found.source == "sii" and not found.outputs
    assert [p.index for p in found.inputs] == [0x1A00, 0x1A01]
    assert not any(p.is_output for p in found.inputs)
    assert [p.event for p in found.inputs] == ["Channel 1", "Channel 2"]
    ch1, ch2 = found.inputs
    assert [(e.field, e.bit_offset, e.kind) for e in ch1.entries] == [
        ("Status", 0, DataType.UInt8),
        ("Value", 8, DataType.Int16),
    ]
    assert [(e.field, e.bit_offset) for e in ch2.entries] == [("", 24), ("Value", 28)]
    assert found.input_bits == 44


def test_coe_map_walks_sm_assignment_with_od_names():
    """The simulated EL3004: 0x1C00 types, 0x1C13 assignment, 0x1A0x entries, OD lookups."""
    slave = simulator._el3004()
    objects = {o.index: o for o in slave.od}

    def lookup(index, sub):
        obj = objects.get(index)
        if obj is None:
            return None
        if obj.entries and sub < len(obj.entries):
            return obj.entries[sub].name, obj.entries[sub].data_type
        return obj.name, obj.data_type

    found = pdo.read_coe_map(slave.sdo_read, lookup)
    pdo.assign_names(found)
    assert found.source == "coe" and not found.outputs and len(found.inputs) == 4
    first = found.inputs[0]
    assert first.event == "AI Standard Channel 1"
    assert [e.field for e in first.entries if e.field] == [
        "Underrange",
        "Overrange",
        "Limit 1",
        "Limit 2",
        "Error",
        "TxPDO State",
        "TxPDO Toggle",
        "Value",
    ]
    value = first.entries[-1]
    assert (value.bit_offset, value.bit_length, value.kind) == (16, 16, DataType.Int16)
    assert found.inputs[3].entries[-1].bit_offset == 3 * 32 + 16


def test_fmmu_spans_read_the_programmed_fmmus():
    """Registers packed as SOEM's `ec_fmmut`: an input FMMU of 4 bits from bit 4 of one
    byte, an output FMMU of 2 bytes, and an inactive one that is ignored."""

    def fmmu(length, start_bit, end_bit, fmmu_type, active):
        return pdo.FMMU_STRUCT.pack(0, length, start_bit, end_bit, 0x1000, 0, fmmu_type, active)

    registers = [
        fmmu(1, 4, 7, pdo.FMMU_TYPE_READ, 1),
        fmmu(2, 0, 7, pdo.FMMU_TYPE_WRITE, 1),
        fmmu(1, 2, 7, pdo.FMMU_TYPE_WRITE, 0),
    ]

    def fprd(address, size):
        n = (address - pdo.FMMU_REGISTER) // pdo.FMMU_SIZE
        return registers[n].ljust(size, b"\0") if n < len(registers) else bytes(size)

    inputs, outputs = pdo.read_fmmus(fprd)
    assert (inputs.start_bit, inputs.bits) == (4, 4)
    assert (outputs.start_bit, outputs.bits) == (0, 16)
    assert pdo.span_problem(inputs, 4, 1) == ""
    assert "straddle" in pdo.span_problem(pdo.FmmuSpan(6, 4), 4, 1)
    assert "FMMU covers" in pdo.span_problem(outputs, 20, 3)


def test_naming_is_position_first_and_collision_free():
    assert pdo.slave_trace_name(2, "EL1004", 0x03EC3052) == "2_EL1004"
    assert pdo.slave_trace_name(5, "", 0x03EC3052) == "5_0x03EC3052"
    found = pdo.SlaveMap(
        outputs=[
            pdo.Pdo(
                0x1600,
                2,
                "Outputs",
                [pdo.PdoEntry(0x7000, 1, 16), pdo.PdoEntry(0x7000, 2, 16)],
                is_output=True,
            )
        ],
        inputs=[
            pdo.Pdo(0x1A00, 3, "Outputs", [pdo.PdoEntry(0x6000, 1, 16, "Value")]),
            pdo.Pdo(0x1A01, 3),
        ],
    )
    pdo.assign_names(found)
    assert [p.event for p in found.pdos()] == ["Outputs", "Outputs_1a00", "txpdo_1a01"]
    assert [e.field for e in found.outputs[0].entries] == ["0x7000_01", "0x7000_02"]
    assert pdo.expected_wkc(outputs_wkc=1, inputs_wkc=2) == 4
