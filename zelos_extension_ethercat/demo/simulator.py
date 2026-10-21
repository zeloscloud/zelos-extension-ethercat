"""A simulated EtherCAT segment behind pysoem's `Master` / slave surface, so the demo
and the tests run the real bring-up, mapping, cycle and recovery code with no NIC.

The segment is the bench the extension is meant for: an EK1100 coupler, an EL1004
(4 DI, SII mapped, wired back from the first EL2004's first two channels), two EL2004
(4 DO each, SII mapped, packed into one byte as SOEM does) and an EL3004 (4 AI, CoE
mapped with an object dictionary). A test drops a slave by clearing its `present`.

Process data lives in one `io_map` laid out as SOEM's `ecx_config_map_group` does it,
so a slave's `input` / `output` view is the same byte window pysoem hands out.
"""

from __future__ import annotations

import math
import random
import struct
import time
from dataclasses import dataclass, field
from typing import Any

from zelos_extension_ethercat import protocol
from zelos_extension_ethercat.pdo import (
    FMMU_REGISTER,
    FMMU_SIZE,
    FMMU_STRUCT,
    FMMU_TYPE_READ,
    FMMU_TYPE_WRITE,
    MBX_COE,
    SII_CAT_END,
    SII_CAT_RXPDO,
    SII_CAT_STRINGS,
    SII_CAT_TXPDO,
    SII_CATEGORY_START_WORD,
    SII_MBX_PROTO_WORD,
    SM_COMM_TYPE,
    SM_PDO_ASSIGN,
)

BECKHOFF = 0x00000002
#: CoE type indices used below.
BOOL, SINT, INT, UDINT, VISIBLE_STRING, BIT1, BIT2 = 0x01, 0x02, 0x03, 0x07, 0x09, 0x30, 0x31


# ─── SII image builder ──────────────────────────────────────────────────────


def build_sii(
    vendor: int,
    product: int,
    revision: int,
    mailbox_protocols: int,
    strings: list[str],
    txpdos: list[tuple[int, int, str, list[tuple[int, int, str, int, int]]]],
    rxpdos: list[tuple[int, int, str, list[tuple[int, int, str, int, int]]]],
) -> bytes:
    """An SII EEPROM image (ETG.2010): identity words, then the string, TxPDO and RxPDO
    categories. PDOs are (index, sync manager, name, entries); entries
    (index, subindex, name, data type, bit length). Names index `strings` (1-based).
    """
    header = bytearray(SII_CATEGORY_START_WORD * 2)
    struct.pack_into("<III", header, 0x0008 * 2, vendor, product, revision)
    struct.pack_into("<H", header, SII_MBX_PROTO_WORD * 2, mailbox_protocols)

    def name_idx(name: str) -> int:
        return strings.index(name) + 1 if name else 0

    def pdo_category(pdos: list[Any]) -> bytes:
        out = bytearray()
        for index, sm, name, entries in pdos:
            out += struct.pack("<HBBBBH", index, len(entries), sm, 0, name_idx(name), 0)
            for e_index, sub, e_name, dtype, bits in entries:
                out += struct.pack("<HBBBBH", e_index, sub, name_idx(e_name), dtype, bits, 0)
        return bytes(out)

    string_cat = bytes([len(strings)]) + b"".join(
        bytes([len(s)]) + s.encode("latin-1") for s in strings
    )
    body = bytearray()
    for cat, data in (
        (SII_CAT_STRINGS, string_cat),
        (SII_CAT_TXPDO, pdo_category(txpdos)),
        (SII_CAT_RXPDO, pdo_category(rxpdos)),
    ):
        if not data:
            continue
        data = data + (b"\0" if len(data) % 2 else b"")
        body += struct.pack("<HH", cat, len(data) // 2) + data
    body += struct.pack("<H", SII_CAT_END)
    return bytes(header) + bytes(body)


# ─── Object dictionary (SDO info) ───────────────────────────────────────────


@dataclass
class OdEntry:
    name: str
    data_type: int
    bit_length: int
    obj_access: int = 0x0007  # readable in every state


@dataclass
class OdObject:
    index: int
    name: str
    data_type: int
    object_code: int  # 7 VAR, 8 ARRAY, 9 RECORD (CiA 301)
    entries: list[OdEntry] = field(default_factory=list)

    @property
    def bit_length(self) -> int:
        return sum(e.bit_length for e in self.entries)

    @property
    def obj_access(self) -> int:
        return self.entries[0].obj_access if len(self.entries) == 1 else 0


# ─── Slaves ─────────────────────────────────────────────────────────────────


@dataclass
class _Fmmu:
    """One programmed FMMU, as SOEM fills `ec_fmmut`."""

    log_start: int
    log_length: int
    start_bit: int
    end_bit: int
    fmmu_type: int

    def pack(self) -> bytes:
        return FMMU_STRUCT.pack(
            self.log_start, self.log_length, self.start_bit, self.end_bit, 0, 0, self.fmmu_type, 1
        ).ljust(FMMU_SIZE, b"\0")


class SimSlave:
    """The pysoem `CdefSlave` surface the master code uses."""

    def __init__(self, name: str, product: int, revision: int, sii: bytes, alias: int = 0) -> None:
        self.name, self.man, self.id, self.rev = name, BECKHOFF, product, revision
        self._sii = sii
        self._alias = alias
        self.state = protocol.INIT  # pysoem: the cached state, and the request after assignment
        self._current = protocol.INIT
        self.al_status = 0
        self.is_lost = False
        self.input_bits = 0
        self.output_bits = 0
        self.objects: dict[int, OdObject] = {}
        self.values: dict[tuple[int, int], bytes] = {}
        self.present = True
        self._io_map = bytearray()
        self._fmmus: list[_Fmmu] = []
        self._in_off = self._out_off = 0

    # identity and mapping sources
    def eeprom_read(self, word: int, timeout: int = 20000) -> bytes:
        return self._sii[2 * word : 2 * word + 4].ljust(4, b"\xff")

    def _fprd(self, address: int, size: int, timeout_us: int = 2000) -> bytes:
        if not self.present:
            raise RuntimeError("no answer")
        if address == protocol.REG_STATION_ALIAS:
            return self._alias.to_bytes(2, "little")[:size]
        if FMMU_REGISTER <= address < FMMU_REGISTER + 4 * FMMU_SIZE:
            n = (address - FMMU_REGISTER) // FMMU_SIZE
            return self._fmmus[n].pack() if n < len(self._fmmus) else bytes(FMMU_SIZE)
        raise RuntimeError(f"register 0x{address:04x} not simulated")

    # CoE
    @property
    def od(self) -> list[OdObject]:
        if not self.objects:
            raise RuntimeError("no SDO info")
        return list(self.objects.values())

    def sdo_read(self, index: int, subindex: int, size: int = 0) -> bytes:
        if not self.objects:
            raise RuntimeError("no mailbox")
        try:
            return self.values[(index, subindex)]
        except KeyError:
            raise RuntimeError(f"SDO abort: 0x{index:04x}:{subindex:02x} does not exist") from None

    def sdo_write(self, index: int, subindex: int, data: bytes) -> None:
        if (index, subindex) not in self.values:
            raise RuntimeError(f"SDO abort: 0x{index:04x}:{subindex:02x} does not exist")
        if index < 0x2000:
            raise RuntimeError(f"SDO abort: 0x{index:04x}:{subindex:02x} is read-only")
        self.values[(index, subindex)] = data

    # state machine
    def write_state(self) -> int:
        if self.present:
            self._current = self.state & 0x0F
            self.al_status = 0
        return 1

    def state_check(self, expected: int, timeout: int = 2000) -> int:
        self.state = self._current if self.present else protocol.NONE
        return self.state

    def reconfig(self, timeout: int = 500) -> int:
        if not self.present:
            return 0
        self._current = self.state = protocol.OP
        return self.state

    def recover(self, timeout: int = 500) -> int:
        if not self.present:
            return 0
        self._current = self.state = protocol.INIT
        return 1

    # process data: pysoem's views are Ibytes/Obytes wide, one byte for a bit slave
    def _view(self, offset: int, bits: int) -> bytes:
        return bytes(self._io_map[offset : offset + max(1, bits // 8)]) if bits else b""

    @property
    def input(self) -> bytes:
        return self._view(self._in_off, self.input_bits)

    @property
    def output(self) -> bytes:
        return self._view(self._out_off, self.output_bits)

    @output.setter
    def output(self, value: bytes) -> None:
        self._io_map[self._out_off : self._out_off + len(value)] = value  # memcpy, as pysoem

    def _set_input(self, raw: int) -> None:
        """Simulation only: the slave's own input bits into the shared image."""
        protocol.insert_bits(self._io_map, self._in_off * 8 + self._in_start, self.input_bits, raw)

    _in_start = 0


def _ek1100() -> SimSlave:
    sii = build_sii(BECKHOFF, 0x044C2C52, 0x00120000, 0, ["EK1100"], [], [])
    return SimSlave("EK1100", 0x044C2C52, 0x00120000, sii, alias=1001)


#: Beckhoff digital terminals: product code and channel count.
DIGITAL = {
    "EL1002": (0x03EA3052, 2, False),
    "EL1004": (0x03EC3052, 4, False),
    "EL1008": (0x03F03052, 8, False),
    "EL2002": (0x07D23052, 2, True),
    "EL2004": (0x07D43052, 4, True),
    "EL2008": (0x07D83052, 8, True),
}


def _digital(model: str) -> SimSlave:
    """An ELx00y digital terminal, SII mapped: one PDO per channel, one BOOL entry each."""
    product, channels, output = DIGITAL[model]
    kind = "Output" if output else "Input"
    names = [model, *(f"Channel {n + 1}" for n in range(channels)), kind]
    base = 0x1600 if output else 0x1A00
    obj = 0x7000 if output else 0x6000
    pdos = [
        (base + n, 0, f"Channel {n + 1}", [(obj + 0x10 * n, 1, kind, BOOL, 1)])
        for n in range(channels)
    ]
    sii = build_sii(
        BECKHOFF, product, 0x00100000, 0, names, [] if output else pdos, pdos if output else []
    )
    slave = SimSlave(model, product, 0x00100000, sii)
    if output:
        slave.output_bits = channels
    else:
        slave.input_bits = channels
    return slave


def _el3004() -> SimSlave:
    """CoE mapped: 0x1C13 assigns 0x1A00..0x1A03, each the EL30xx standard channel."""
    sii = build_sii(BECKHOFF, 0x0BBC3052, 0x00140000, MBX_COE, ["EL3004"], [], [])
    slave = SimSlave("EL3004", 0x0BBC3052, 0x00140000, sii)
    slave.input_bits = 4 * 32
    objects = slave.objects
    values = slave.values
    objects[0x1008] = OdObject(
        0x1008, "Device name", VISIBLE_STRING, 7, [OdEntry("Device name", VISIBLE_STRING, 48)]
    )
    values[(0x1008, 0)] = b"EL3004"
    objects[0x1018] = OdObject(
        0x1018,
        "Identity",
        0,
        9,
        [OdEntry("SubIndex 000", 0x05, 8)]
        + [
            OdEntry(n, UDINT, 32)
            for n in ("Vendor ID", "Product code", "Revision", "Serial number")
        ],
    )
    for sub, value in enumerate((4, BECKHOFF, 0x0BBC3052, 0x00140000, 12345678)):
        values[(0x1018, sub)] = value.to_bytes(1 if sub == 0 else 4, "little")
    objects[SM_COMM_TYPE] = OdObject(SM_COMM_TYPE, "Sync manager type", 0, 8)
    for sub, value in enumerate((4, 1, 2, 3, 4)):
        values[(SM_COMM_TYPE, sub)] = bytes([value])
    values[(SM_PDO_ASSIGN + 2, 0)] = b"\0"  # no RxPDOs
    values[(SM_PDO_ASSIGN + 3, 0)] = b"\x04"
    standard = [
        (1, "Underrange", BIT1, 1),
        (2, "Overrange", BIT1, 1),
        (3, "Limit 1", BIT2, 2),
        (5, "Limit 2", BIT2, 2),
        (7, "Error", BIT1, 1),
        (0, "", 0, 7),  # filler
        (14, "TxPDO State", BIT1, 1),
        (15, "TxPDO Toggle", BIT1, 1),
        (17, "Value", INT, 16),
    ]
    for ch in range(4):
        pdo_index, obj_index = 0x1A00 + ch, 0x6000 + 0x10 * ch
        values[(SM_PDO_ASSIGN + 3, ch + 1)] = pdo_index.to_bytes(2, "little")
        objects[pdo_index] = OdObject(pdo_index, f"AI Standard Channel {ch + 1}", 0, 9)
        values[(pdo_index, 0)] = bytes([len(standard)])
        entries = [OdEntry("SubIndex 000", 0x05, 8)] + [OdEntry("", 0, 0)] * 17
        for n, (sub, name, dtype, bits) in enumerate(standard, 1):
            index = obj_index if sub else 0
            values[(pdo_index, n)] = ((index << 16) | (sub << 8) | bits).to_bytes(4, "little")
            if sub:
                entries[sub] = OdEntry(name, dtype, bits)
        objects[obj_index] = OdObject(obj_index, f"AI Inputs Ch.{ch + 1}", 0, 9, entries)
        settings = 0x8000 + 0x10 * ch
        objects[settings] = OdObject(
            settings,
            f"AI Settings Ch.{ch + 1}",
            0,
            9,
            [OdEntry("SubIndex 000", 0x05, 8)]
            + [OdEntry("", 0, 0)] * 5
            + [OdEntry("Enable filter", BOOL, 1, 0x003F)],
        )
        values[(settings, 0)] = b"\x06"
        values[(settings, 6)] = b"\x01"
    return slave


# ─── Master ─────────────────────────────────────────────────────────────────


#: The demo segment, in bus order.
DEMO_LAYOUT = ("EK1100", "EL1004", "EL2004", "EL2004", "EL3004")
_FACTORIES = {"EK1100": _ek1100, "EL3004": _el3004}


def make_slave(model: str) -> SimSlave:
    return _FACTORIES[model]() if model in _FACTORIES else _digital(model)


class SimMaster:
    """The pysoem `Master` surface over the simulated segment; `layout`: models in bus order."""

    def __init__(self, layout: tuple[str, ...] = DEMO_LAYOUT) -> None:
        self.slaves: list[SimSlave] = []
        self.state = protocol.INIT
        self.expected_wkc = 0
        self.dc_time = 0
        self.io_map = bytearray()
        self.output_bytes = 0
        self._layout = layout
        self._t0 = time.monotonic()
        self._rng = random.Random(1)
        self._open = False

    def open(self, interface: str) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def config_init(self, usetable: bool = False) -> int:
        self.slaves = [make_slave(model) for model in self._layout]
        for s in self.slaves:
            s.state = s._current = protocol.PREOP
        self.state = protocol.PREOP
        return len(self.slaves)

    def config_map(self) -> int:
        """SOEM's layout: every slave's outputs, byte-aligned, then every slave's inputs;
        bit-sized slaves (fewer than 8 bits) packed back to back at the bit level."""
        log_addr = self._pack(0, FMMU_TYPE_WRITE)
        self.output_bytes = log_addr
        size = self._pack(log_addr, FMMU_TYPE_READ)
        self.io_map = bytearray(size)
        for s in self.slaves:
            s._io_map = self.io_map
            s.state = s._current = protocol.SAFEOP
        self.state = protocol.SAFEOP
        self.expected_wkc = sum(
            (2 if s.output_bits else 0) + (1 if s.input_bits else 0) for s in self.slaves
        )
        return size

    def _pack(self, log_addr: int, fmmu_type: int) -> int:
        """`ecx_config_create_*_mappings` for one direction; returns the next byte address."""
        bit_pos = 0
        for s in self.slaves:
            bits = s.output_bits if fmmu_type == FMMU_TYPE_WRITE else s.input_bits
            if not bits:
                continue
            if bits > 7:  # byte oriented
                if bit_pos:
                    log_addr, bit_pos = log_addr + 1, 0
                length = (bits + 7) // 8
                fmmu = _Fmmu(log_addr, length, 0, 7, fmmu_type)
                log_addr += length
            else:  # bit oriented
                start = log_addr
                end_bit = bit_pos + bits - 1
                fmmu = _Fmmu(start, end_bit // 8 + 1, bit_pos, end_bit % 8, fmmu_type)
                log_addr, bit_pos = start + end_bit // 8, end_bit % 8 + 1
                if bit_pos > 7:
                    log_addr, bit_pos = log_addr + 1, 0
            s._fmmus.append(fmmu)
            if fmmu_type == FMMU_TYPE_WRITE:
                s._out_off = fmmu.log_start
            else:
                s._in_off, s._in_start = fmmu.log_start, fmmu.start_bit
        return log_addr + (1 if bit_pos else 0)

    def config_dc(self) -> bool:
        return False

    def read_state(self) -> int:
        for s in self.slaves:
            s.state_check(0)
        self.state = min(s.state for s in self.slaves)
        return self.state

    def write_state(self) -> int:
        for s in self.slaves:
            s.state = self.state
            s.write_state()
        return len(self.slaves)

    def send_processdata(self) -> int:
        self._simulate()
        return 1

    def receive_processdata(self, timeout: int = 2000) -> int:
        self.dc_time = time.monotonic_ns()
        # Below SAFEOP the process data SMs are off, so the slave does not count.
        present = [s for s in self.slaves if s.present and s._current >= protocol.SAFEOP]
        return sum((2 if s.output_bits else 0) + (1 if s.input_bits else 0) for s in present)

    # simulation
    def _elapsed(self) -> float:
        return time.monotonic() - self._t0

    def _first(self, model: str) -> SimSlave | None:
        return next((s for s in self.slaves if s.name == model), None)

    def _simulate(self) -> None:
        """Every digital input terminal: channels 1-2 follow the first EL2004's channels
        1-2, the rest free-run; the EL3004 sine, triangle, noise, constant."""
        t = self._elapsed()
        if not self.io_map:
            return
        el2004 = self._first("EL2004")
        outputs = (
            protocol.extract_bits(self.io_map, el2004._out_off * 8 + el2004._fmmus[0].start_bit, 2)
            if el2004
            else 0
        )
        bits = outputs | (int(t % 1.0 < 0.5) << 2) | (int(t % 5.0 < 2.5) << 3)
        for s in self.slaves:
            if s.input_bits and s.input_bits <= 8 and s._fmmus:
                s._set_input(bits & ((1 << s.input_bits) - 1))
        el3004 = self._first("EL3004")
        if el3004 is None:
            return
        toggle = int(t * 100) & 1
        volts = (
            10.0 * math.sin(2 * math.pi * 0.2 * t),
            10.0 * (2 * abs((t / 4.0) % 1.0 - 0.5) - 0.5),
            0.5 + self._rng.gauss(0, 0.01),
            2.5,
        )
        image = bytearray()
        for v in volts:
            value = max(-32768, min(32767, round(v / 10.0 * 32767)))
            image += struct.pack("<HH", toggle << 15, value & 0xFFFF)
        self.io_map[el3004._in_off : el3004._in_off + len(image)] = image
