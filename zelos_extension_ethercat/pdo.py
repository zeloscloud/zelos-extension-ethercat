"""Process data mapping: which object sits where in a slave's input and output image,
and what it is called.

Two sources, as SOEM's `config_map` decides it (ethercatconfig.c): a slave with a
CoE mailbox is mapped from its SM PDO assignment objects (0x1C1x -> 0x16xx/0x1Axx
mapping entries, ETG.1000.6 5.6.7.4), any other from the SII PDO categories of its
EEPROM (ETG.1000.6 5.4; ETG.2010). Both walks are pure functions over reader
callables, so the tests run them on synthetic data.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass, field

from zelos_sdk import DataType

from zelos_extension_ethercat import protocol

#: SM assignment and type objects (ETG.1000.6 Table 67).
SM_COMM_TYPE = 0x1C00
SM_PDO_ASSIGN = 0x1C10
SM_TYPE_OUTPUTS, SM_TYPE_INPUTS = 3, 4
SM_MAX = 8  # EC_MAXSM
#: SII word addresses (SOEM ethercattype.h ECT_SII_*).
SII_MBX_PROTO_WORD = 0x001C
SII_CATEGORY_START_WORD = 0x0040
SII_CAT_STRINGS, SII_CAT_TXPDO, SII_CAT_RXPDO = 10, 50, 51
SII_CAT_END = 0xFFFF
#: Mailbox protocol bits of SII word 0x1C (ETG.1000.6 Table 20).
MBX_COE = 0x0004
#: ESC FMMU registers: 16 bytes each from 0x0600 (ETG.1000.4 Table 40), laid out as
#: SOEM's `ec_fmmut`: LogStart u32, LogLength u16, LogStartbit, LogEndbit, PhysStart u16,
#: PhysStartBit, FMMUtype, FMMUactive.
FMMU_REGISTER, FMMU_SIZE, FMMU_COUNT = 0x0600, 16, 4
FMMU_STRUCT = struct.Struct("<IHBBHBBB")
FMMU_TYPE_READ, FMMU_TYPE_WRITE = 1, 2

SdoRead = Callable[[int, int], bytes]
#: (index, subindex) -> (name, CoE data type) from the object dictionary, or None.
OdLookup = Callable[[int, int], tuple[str, int] | None]


class MapError(Exception):
    """The mapping of one slave could not be read; the message says why."""


@dataclass
class PdoEntry:
    """One mapped object: `bit_offset` is within the slave's image of its direction."""

    index: int
    subindex: int
    bit_length: int
    name: str = ""
    coe_type: int = 0
    bit_offset: int = 0
    field: str = ""

    @property
    def is_filler(self) -> bool:
        """Padding: index 0, which SOEM's slaveinfo also skips."""
        return self.index == 0

    @property
    def kind(self) -> DataType:
        return protocol.trace_type(self.coe_type, self.bit_length)


@dataclass
class Pdo:
    """`is_output` comes from the SM type (CoE 0x1C00) or the SII category, as SOEM maps
    it; the PDO index range (0x16xx / 0x1Axx) is only a convention."""

    index: int
    sync_manager: int
    name: str = ""
    entries: list[PdoEntry] = field(default_factory=list)
    event: str = ""
    is_output: bool = False

    @property
    def bit_length(self) -> int:
        return sum(e.bit_length for e in self.entries)


@dataclass
class SlaveMap:
    """A slave's PDOs in image order, per direction, with offsets assigned."""

    outputs: list[Pdo] = field(default_factory=list)
    inputs: list[Pdo] = field(default_factory=list)
    source: str = "none"  # coe | sii | none

    @property
    def output_bits(self) -> int:
        return sum(p.bit_length for p in self.outputs)

    @property
    def input_bits(self) -> int:
        return sum(p.bit_length for p in self.inputs)

    def pdos(self) -> list[Pdo]:
        return [*self.outputs, *self.inputs]


def _u16(data: bytes) -> int:
    return int.from_bytes(data[:2], "little")


def _assign_offsets(pdos: list[Pdo]) -> list[Pdo]:
    """Entries packed back to back in PDO order, as SOEM lays an SM out."""
    offset = 0
    for pdo in pdos:
        for entry in pdo.entries:
            entry.bit_offset = offset
            offset += entry.bit_length
    return pdos


# ─── CoE: SM PDO assignment ─────────────────────────────────────────────────


def read_coe_map(sdo_read: SdoRead, od_lookup: OdLookup | None = None) -> SlaveMap:
    """The mapping a CoE slave reports, SM by SM as SOEM's `ecx_readPDOmap` walks it
    (ethercatcoe.c): SM2 onwards, with its slave-bug corrections (an SM2 typed 2 shifts
    every type up by one; an SM2 or SM3 typed 0 is taken as outputs or inputs).

    `sdo_read(index, subindex)` returns the object's bytes. Names and types come from
    `od_lookup` where the slave supports SDO info; otherwise entries are named by
    index:subindex and typed by width.

    :raises MapError: 0x1C00 cannot be read, or it maps no process data (SOEM then
        maps from the SII, and so does the caller)
    """
    try:
        sm_count = sdo_read(SM_COMM_TYPE, 0)[0]
    except Exception as e:
        raise MapError(f"no SM communication type object (0x1C00): {e}") from e
    found = SlaveMap(source="coe")
    bug_add = 0
    for sm in range(2, min(sm_count, SM_MAX)):
        try:
            sm_type = sdo_read(SM_COMM_TYPE, sm + 1)[0]
        except Exception:
            continue
        if sm == 2 and sm_type == 2:
            bug_add = 1
        if sm_type:
            sm_type += bug_add
        if sm_type == 0 and sm in (2, 3):
            sm_type = SM_TYPE_OUTPUTS if sm == 2 else SM_TYPE_INPUTS
        if sm_type not in (SM_TYPE_OUTPUTS, SM_TYPE_INPUTS):
            continue
        is_output = sm_type == SM_TYPE_OUTPUTS
        pdos = _read_assignment(sdo_read, SM_PDO_ASSIGN + sm, sm, is_output, od_lookup)
        (found.outputs if is_output else found.inputs).extend(pdos)
    if not found.input_bits and not found.output_bits:
        raise MapError("the CoE PDO assignment maps no process data")
    _assign_offsets(found.outputs)
    _assign_offsets(found.inputs)
    return found


def _read_assignment(
    sdo_read: SdoRead, assign: int, sm: int, is_output: bool, od_lookup: OdLookup | None
) -> list[Pdo]:
    """The PDOs of one SM: 0x1C1x lists the PDO indices, each PDO lists u32 entries
    `index << 16 | subindex << 8 | bit_length` (ETG.1000.6 Table 70)."""
    try:
        count = sdo_read(assign, 0)[0]
    except Exception:
        return []  # SOEM: an SM without an assign object maps nothing
    pdos = []
    for n in range(1, count + 1):
        pdo_index = _u16(sdo_read(assign, n))
        if pdo_index == 0:
            continue
        pdo = Pdo(pdo_index, sm, is_output=is_output)
        if od_lookup is not None and (named := od_lookup(pdo_index, 0)):
            pdo.name = named[0]
        for sub in range(1, sdo_read(pdo_index, 0)[0] + 1):
            packed = int.from_bytes(sdo_read(pdo_index, sub)[:4], "little")
            entry = PdoEntry(packed >> 16, (packed >> 8) & 0xFF, packed & 0xFF)
            if not entry.is_filler and od_lookup is not None:
                named = od_lookup(entry.index, entry.subindex)
                if named:
                    entry.name, entry.coe_type = named
            pdo.entries.append(entry)
        pdos.append(pdo)
    return pdos


# ─── SII: EEPROM PDO categories ─────────────────────────────────────────────


class SiiImage:
    """Byte access to a slave's SII EEPROM through a `fetch(word) -> 4 bytes` reader
    (pysoem's `eeprom_read`), cached per 2-word read."""

    def __init__(self, fetch: Callable[[int], bytes], max_words: int = 0x2000) -> None:
        self._fetch = fetch
        self._cache: dict[int, bytes] = {}
        self.max_words = max_words

    @classmethod
    def from_bytes(cls, image: bytes) -> SiiImage:
        return cls(lambda word: image[2 * word : 2 * word + 4].ljust(4, b"\xff"), len(image) // 2)

    def u8(self, byte_addr: int) -> int:
        word = (byte_addr // 4) * 2
        if word >= self.max_words:
            raise MapError(f"SII read past the EEPROM at word 0x{word:04X}")
        if word not in self._cache:
            self._cache[word] = bytes(self._fetch(word))
        return self._cache[word][byte_addr % 4]

    def u16(self, byte_addr: int) -> int:
        return self.u8(byte_addr) | (self.u8(byte_addr + 1) << 8)

    def mailbox_protocols(self) -> int:
        return self.u16(SII_MBX_PROTO_WORD * 2)

    def categories(self) -> dict[int, int]:
        """Category type -> byte address of its data, walked like SOEM's `ecx_siifind`."""
        found: dict[int, int] = {}
        addr = SII_CATEGORY_START_WORD * 2
        while True:
            cat = self.u16(addr)
            if cat == SII_CAT_END:
                return found
            size_words = self.u16(addr + 2)
            found.setdefault(cat, addr + 4)
            addr += 4 + 2 * size_words

    def strings(self) -> list[str]:
        """The string category: a count byte, then length-prefixed strings; 1-based
        indices elsewhere in the SII, so [0] is the empty name."""
        start = self.categories().get(SII_CAT_STRINGS)
        if start is None:
            return [""]
        out, addr = [""], start + 1
        for _ in range(self.u8(start)):
            length = self.u8(addr)
            out.append(bytes(self.u8(addr + 1 + i) for i in range(length)).decode("latin-1"))
            addr += 1 + length
        return out


def read_sii_map(sii: SiiImage) -> SlaveMap:
    """The mapping of a slave without CoE, from its SII TxPDO (inputs) and RxPDO (outputs)
    categories: 8-byte PDO headers each followed by 8-byte entries (ETG.2010 Table 19/20).
    A PDO whose SyncM is 0xFF is not active and does not map."""
    found = SlaveMap(source="sii")
    strings = sii.strings()
    categories = sii.categories()
    for cat, target in ((SII_CAT_RXPDO, found.outputs), (SII_CAT_TXPDO, found.inputs)):
        start = categories.get(cat)
        if start is None:
            continue
        is_output = cat == SII_CAT_RXPDO
        # The category size is the word before its data.
        end = start + 2 * sii.u16(start - 2)
        addr = start
        while addr + 8 <= end:
            index, n_entries, sync_manager = sii.u16(addr), sii.u8(addr + 2), sii.u8(addr + 3)
            pdo = Pdo(index, sync_manager, _string(strings, sii.u8(addr + 5)), is_output=is_output)
            addr += 8
            for _ in range(n_entries):
                pdo.entries.append(
                    PdoEntry(
                        index=sii.u16(addr),
                        subindex=sii.u8(addr + 2),
                        name=_string(strings, sii.u8(addr + 3)),
                        coe_type=sii.u8(addr + 4),
                        bit_length=sii.u8(addr + 5),
                    )
                )
                addr += 8
            if sync_manager < 0xFF:
                target.append(pdo)
        # SOEM lays an image out SM by SM, PDOs in category order within one.
        target.sort(key=lambda p: p.sync_manager)
    _assign_offsets(found.outputs)
    _assign_offsets(found.inputs)
    return found


def _string(strings: list[str], idx: int) -> str:
    return strings[idx] if 0 < idx < len(strings) else ""


# ─── FMMU spans ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FmmuSpan:
    """Where a slave's data of one direction sits in the logical image, from the FMMUs
    the master programmed: `start_bit` within the first byte (SOEM's Istartbit /
    Ostartbit) and `bits` covered in all. `bits == 0`: no FMMU of that type."""

    start_bit: int = 0
    bits: int = 0

    def fits(self, view_bytes: int) -> bool:
        """Whether the whole span lies inside a view of `view_bytes` from the first byte."""
        return self.start_bit + self.bits <= view_bytes * 8


def read_fmmus(fprd: Callable[[int, int], bytes]) -> tuple[FmmuSpan, FmmuSpan]:
    """(inputs, outputs) spans of a slave, read back from its FMMU registers.

    SOEM packs bit-sized slaves (an EL1004's 4 inputs) back to back at the bit level,
    so the second of two in a row starts at bit 4 of a byte it shares with the first,
    and a third can straddle into the next byte; pysoem's `slave.input` view is one
    byte and ignores both. The FMMU says where the bits really are: type 1 reads
    inputs, 2 writes outputs; LogLength bytes from LogStartbit to LogEndbit.
    """
    inputs = outputs = FmmuSpan()
    for n in range(FMMU_COUNT):
        try:
            reg = fprd(FMMU_REGISTER + n * FMMU_SIZE, FMMU_SIZE)
        except Exception:
            break
        _, length, start_bit, end_bit, _, _, fmmu_type, active = FMMU_STRUCT.unpack_from(reg)
        if not active or not length:
            continue
        bits = length * 8 - start_bit - (7 - end_bit)
        if fmmu_type == FMMU_TYPE_READ:
            inputs = FmmuSpan(inputs.start_bit if inputs.bits else start_bit, inputs.bits + bits)
        elif fmmu_type == FMMU_TYPE_WRITE:
            outputs = FmmuSpan(
                outputs.start_bit if outputs.bits else start_bit, outputs.bits + bits
            )
    return inputs, outputs


def span_problem(span: FmmuSpan, mapped_bits: int, view_bytes: int) -> str:
    """Why a direction of a slave cannot be used through pysoem's view, or "".

    The FMMU length is SOEM's own bit count for the slave (Ibits / Obits, rounded up
    to bytes for byte-oriented slaves): a mapping that disagrees would decode or write
    the wrong bits. A span past the view (a bit slave straddling a byte) is unreachable.
    """
    if not mapped_bits or not span.bits:
        return ""
    if not mapped_bits <= span.bits <= (mapped_bits + 7) // 8 * 8:
        return f"mapping says {mapped_bits} bits but the FMMU covers {span.bits}"
    if not span.fits(view_bytes):
        return (
            f"{span.bits} bits from bit {span.start_bit} straddle past the {view_bytes}-byte "
            "view pysoem gives this slave"
        )
    return ""


# ─── Naming ─────────────────────────────────────────────────────────────────


def slave_trace_name(position: int, name: str, product: int) -> str:
    """`<position>_<name>`: bus order first, as TwinCAT's `Term n (EL1004)` numbers it,
    which keeps two identical terminals apart without renaming either."""
    clean = protocol.sanitize(name) or f"0x{product:08X}"
    return f"{position}_{clean}"


def assign_names(slave_map: SlaveMap) -> None:
    """Event names per PDO and field names per entry, unique within their scope.

    A PDO without a name is `rxpdo_1600` / `txpdo_1a00`; a second PDO of a name takes
    its index as suffix. Entries without a name read `0x6000_01`; fillers get none.
    """
    events: set[str] = set()
    for pdo in slave_map.pdos():
        base = protocol.sanitize(pdo.name) or (
            f"{'rxpdo' if pdo.is_output else 'txpdo'}_{pdo.index:04x}"
        )
        pdo.event = base if base not in events else f"{base}_{pdo.index:04x}"
        events.add(pdo.event)
        fields: set[str] = set()
        for entry in pdo.entries:
            if entry.is_filler or entry.bit_length == 0:
                entry.field = ""
                continue
            name = protocol.sanitize(entry.name, "field") or (
                f"0x{entry.index:04x}_{entry.subindex:02x}"
            )
            entry.field = name if name not in fields else f"{name}_{entry.subindex:02x}"
            fields.add(entry.field)


def expected_wkc(outputs_wkc: int, inputs_wkc: int) -> int:
    """SOEM simple_test: an LRW counts 2 per output FMMU and 1 per input FMMU."""
    return outputs_wkc * 2 + inputs_wkc
