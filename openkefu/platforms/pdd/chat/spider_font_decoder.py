from __future__ import annotations

import argparse
import base64
import json
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIRMED_SPIDER_MAPPING = {
    "콱": "你",
    "봤": "好",
}


@dataclass(frozen=True)
class FontMapReport:
    mapping: dict[str, str]
    codepoint_to_glyph: dict[int, int]
    glyph_names: list[str]
    font_names: dict[int, str]
    source: str

    @property
    def mapped_count(self) -> int:
        return len(self.mapping)

    @property
    def cmap_count(self) -> int:
        return len(self.codepoint_to_glyph)


class SpiderFontDecoder:
    def __init__(self, mapping: dict[str, str] | None = None):
        self.mapping = dict(CONFIRMED_SPIDER_MAPPING if mapping is None else mapping)

    @classmethod
    def from_font(
        cls,
        source: str | bytes | Path,
        *,
        seed_mapping: dict[str, str] | None = CONFIRMED_SPIDER_MAPPING,
    ) -> "SpiderFontDecoder":
        report = build_mapping_from_font(source, seed_mapping=seed_mapping)
        return cls(report.mapping)

    def decode_text(self, text: str) -> str:
        return "".join(self.mapping.get(char, char) for char in text)

    def decode_payload(self, payload: Any) -> Any:
        if isinstance(payload, str):
            return self.decode_text(payload)
        if isinstance(payload, list):
            return [self.decode_payload(item) for item in payload]
        if isinstance(payload, dict):
            return {key: self.decode_payload(value) for key, value in payload.items()}
        return payload


def build_mapping_from_font(
    source: str | bytes | Path,
    *,
    seed_mapping: dict[str, str] | None = CONFIRMED_SPIDER_MAPPING,
) -> FontMapReport:
    font_bytes, source_label = load_font_bytes(source)
    tables = _load_font_tables(font_bytes)
    glyph_names = _parse_post_names(tables.get("post", b""), _parse_num_glyphs(tables.get("maxp", b"")))
    codepoint_to_glyph = _parse_cmap(tables["cmap"])
    font_names = _parse_name_table(tables.get("name", b""))

    mapping: dict[str, str] = {}
    for codepoint, glyph_id in sorted(codepoint_to_glyph.items()):
        if not (0 <= glyph_id < len(glyph_names)):
            continue
        target = glyph_name_to_text(glyph_names[glyph_id])
        if target and target != chr(codepoint):
            mapping[chr(codepoint)] = target

    if seed_mapping:
        mapping.update(seed_mapping)

    return FontMapReport(
        mapping=mapping,
        codepoint_to_glyph=codepoint_to_glyph,
        glyph_names=glyph_names,
        font_names=font_names,
        source=source_label,
    )


def load_font_bytes(source: str | bytes | Path) -> tuple[bytes, str]:
    if isinstance(source, bytes):
        return source, "<bytes>"
    if isinstance(source, Path):
        return source.read_bytes(), str(source)

    text = source.strip()
    if text.startswith("data:font/"):
        _, payload = text.split(",", 1)
        return base64.b64decode(payload), "data-url"

    path = Path(text)
    if path.exists():
        return path.read_bytes(), str(path)

    return base64.b64decode(text), "base64"


def glyph_name_to_text(glyph_name: str) -> str:
    uni_match = re.fullmatch(r"uni([0-9A-Fa-f]{4})+", glyph_name)
    if uni_match:
        hex_part = glyph_name[3:]
        return "".join(chr(int(hex_part[index:index + 4], 16)) for index in range(0, len(hex_part), 4))

    u_match = re.fullmatch(r"u([0-9A-Fa-f]{4,6})", glyph_name)
    if u_match:
        return chr(int(u_match.group(1), 16))

    return ""


def decode_text(text: str, mapping: dict[str, str] | None = None) -> str:
    return SpiderFontDecoder(mapping or CONFIRMED_SPIDER_MAPPING).decode_text(text)


def _load_font_tables(font_bytes: bytes) -> dict[str, bytes]:
    if font_bytes[:4] == b"wOFF":
        return _load_woff_tables(font_bytes)
    return _load_sfnt_tables(font_bytes)


def _load_woff_tables(font_bytes: bytes) -> dict[str, bytes]:
    if len(font_bytes) < 44:
        raise ValueError("WOFF data is incomplete")
    _, _, _, num_tables = struct.unpack_from(">4sIIIH", font_bytes, 0)
    offset = 44
    tables: dict[str, bytes] = {}
    for _ in range(num_tables):
        if offset + 20 > len(font_bytes):
            raise ValueError("WOFF table directory is incomplete")
        tag_bytes, table_offset, comp_len, orig_len, _ = struct.unpack_from(">4sIIII", font_bytes, offset)
        offset += 20
        chunk = font_bytes[table_offset:table_offset + comp_len]
        if len(chunk) != comp_len:
            raise ValueError(f"WOFF table {tag_bytes!r} is incomplete")
        if comp_len != orig_len:
            chunk = zlib.decompress(chunk)
        if len(chunk) != orig_len:
            raise ValueError(f"WOFF table {tag_bytes!r} has wrong decompressed length")
        tables[tag_bytes.decode("ascii")] = chunk
    return tables


def _load_sfnt_tables(font_bytes: bytes) -> dict[str, bytes]:
    if len(font_bytes) < 12:
        raise ValueError("sfnt font data is incomplete")
    num_tables = struct.unpack_from(">H", font_bytes, 4)[0]
    offset = 12
    tables: dict[str, bytes] = {}
    for _ in range(num_tables):
        if offset + 16 > len(font_bytes):
            raise ValueError("sfnt table directory is incomplete")
        tag_bytes, _, table_offset, length = struct.unpack_from(">4sIII", font_bytes, offset)
        offset += 16
        tables[tag_bytes.decode("ascii")] = font_bytes[table_offset:table_offset + length]
    return tables


def _parse_num_glyphs(maxp: bytes) -> int:
    if len(maxp) < 6:
        return 0
    return struct.unpack_from(">H", maxp, 4)[0]


def _parse_cmap(cmap: bytes) -> dict[int, int]:
    if len(cmap) < 4:
        raise ValueError("cmap table is incomplete")
    num_tables = struct.unpack_from(">H", cmap, 2)[0]
    records = []
    for index in range(num_tables):
        record_offset = 4 + index * 8
        platform_id, encoding_id, subtable_offset = struct.unpack_from(">HHI", cmap, record_offset)
        if subtable_offset + 2 <= len(cmap):
            fmt = struct.unpack_from(">H", cmap, subtable_offset)[0]
            records.append((platform_id, encoding_id, fmt, subtable_offset))

    priority = {
        (3, 10, 12): 0,
        (0, 4, 12): 1,
        (0, 3, 12): 2,
        (3, 1, 4): 3,
        (3, 0, 4): 4,
        (0, 3, 4): 5,
        (0, 1, 4): 6,
    }
    records.sort(key=lambda item: priority.get(item[:3], 100))
    for _, _, fmt, subtable_offset in records:
        if fmt == 12:
            return _parse_cmap_format_12(cmap[subtable_offset:])
        if fmt == 4:
            return _parse_cmap_format_4(cmap[subtable_offset:])
    raise ValueError("no supported cmap subtable found")


def _parse_cmap_format_12(subtable: bytes) -> dict[int, int]:
    if len(subtable) < 16:
        raise ValueError("cmap format 12 subtable is incomplete")
    n_groups = struct.unpack_from(">I", subtable, 12)[0]
    mapping: dict[int, int] = {}
    offset = 16
    for _ in range(n_groups):
        start, end, start_glyph = struct.unpack_from(">III", subtable, offset)
        offset += 12
        for codepoint in range(start, end + 1):
            mapping[codepoint] = start_glyph + codepoint - start
    return mapping


def _parse_cmap_format_4(subtable: bytes) -> dict[int, int]:
    if len(subtable) < 16:
        raise ValueError("cmap format 4 subtable is incomplete")
    length, seg_count_x2 = struct.unpack_from(">HH", subtable, 2)
    subtable = subtable[:length]
    seg_count = seg_count_x2 // 2

    end_codes_offset = 14
    start_codes_offset = end_codes_offset + 2 * seg_count + 2
    deltas_offset = start_codes_offset + 2 * seg_count
    range_offsets_offset = deltas_offset + 2 * seg_count

    end_codes = struct.unpack_from(f">{seg_count}H", subtable, end_codes_offset)
    start_codes = struct.unpack_from(f">{seg_count}H", subtable, start_codes_offset)
    deltas = struct.unpack_from(f">{seg_count}h", subtable, deltas_offset)
    range_offsets = struct.unpack_from(f">{seg_count}H", subtable, range_offsets_offset)

    mapping: dict[int, int] = {}
    for index, (start, end, delta, range_offset) in enumerate(zip(start_codes, end_codes, deltas, range_offsets)):
        if start == 0xFFFF and end == 0xFFFF:
            continue
        for codepoint in range(start, end + 1):
            if range_offset == 0:
                glyph_id = (codepoint + delta) & 0xFFFF
            else:
                range_word = range_offsets_offset + 2 * index
                glyph_offset = range_word + range_offset + 2 * (codepoint - start)
                if glyph_offset + 2 > len(subtable):
                    continue
                glyph_id = struct.unpack_from(">H", subtable, glyph_offset)[0]
                if glyph_id:
                    glyph_id = (glyph_id + delta) & 0xFFFF
            if glyph_id:
                mapping[codepoint] = glyph_id
    return mapping


def _parse_post_names(post: bytes, num_glyphs: int) -> list[str]:
    if num_glyphs <= 0:
        return []
    if len(post) < 34:
        return [f"glyph{index}" for index in range(num_glyphs)]

    version = struct.unpack_from(">I", post, 0)[0]
    if version != 0x00020000:
        return [f"glyph{index}" for index in range(num_glyphs)]

    count = struct.unpack_from(">H", post, 32)[0]
    count = min(count, num_glyphs)
    offset = 34
    indices = list(struct.unpack_from(f">{count}H", post, offset))
    offset += count * 2

    custom_count = max([index for index in indices if index >= 258], default=257) - 257
    custom_names: list[str] = []
    for _ in range(custom_count):
        if offset >= len(post):
            break
        length = post[offset]
        offset += 1
        name = post[offset:offset + length].decode("ascii", errors="replace")
        offset += length
        custom_names.append(name)

    names = [f"glyph{index}" for index in range(num_glyphs)]
    for glyph_id, name_index in enumerate(indices):
        if name_index >= 258:
            custom_index = name_index - 258
            if custom_index < len(custom_names):
                names[glyph_id] = custom_names[custom_index]
        else:
            names[glyph_id] = _standard_post_name(name_index)
    return names


def _standard_post_name(index: int) -> str:
    if index == 0:
        return ".notdef"
    return f"mac{index}"


def _parse_name_table(name_table: bytes) -> dict[int, str]:
    if len(name_table) < 6:
        return {}
    _, count, string_offset = struct.unpack_from(">HHH", name_table, 0)
    result: dict[int, str] = {}
    for index in range(count):
        record_offset = 6 + index * 12
        if record_offset + 12 > len(name_table):
            break
        platform_id, _, _, name_id, length, offset = struct.unpack_from(">HHHHHH", name_table, record_offset)
        start = string_offset + offset
        raw = name_table[start:start + length]
        if not raw:
            continue
        if platform_id in {0, 3}:
            value = raw.decode("utf-16-be", errors="replace")
        else:
            value = raw.decode("latin-1", errors="replace")
        result.setdefault(name_id, value)
    return result


def _main() -> int:
    parser = argparse.ArgumentParser(description="Decode PDD spider-font text with a generated glyph mapping.")
    parser.add_argument("--font", help="Font file path, base64, or data:font/... URL.")
    parser.add_argument("--text", default="", help="Text to decode, for example: 콱봤")
    parser.add_argument("--dump-map", action="store_true", help="Print the generated mapping as JSON.")
    parser.add_argument("--no-seed", action="store_true", help="Do not include confirmed sample mappings.")
    args = parser.parse_args()

    seed = None if args.no_seed else CONFIRMED_SPIDER_MAPPING
    if args.font:
        report = build_mapping_from_font(args.font, seed_mapping=seed)
        decoder = SpiderFontDecoder(report.mapping)
        print(
            json.dumps(
                {
                    "source": report.source,
                    "font_names": report.font_names,
                    "cmap_count": report.cmap_count,
                    "mapped_count": report.mapped_count,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if args.dump_map:
            print(json.dumps(report.mapping, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        decoder = SpiderFontDecoder(seed)

    if args.text:
        print(decoder.decode_text(args.text))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
