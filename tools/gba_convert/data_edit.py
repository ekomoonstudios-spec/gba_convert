"""Typed views + round-trip encoders for data regions in the ROM.

v1 scope: **palette slices only**. Tileset / tilemap / sprite views come
next.

Source of truth
---------------

Slices are read straight from the **ROM binary** (file offset =
`addr - 0x08000000`). Luvdis' `rom.s` strips labels from large data
runs, so address-to-line lookup is unreliable for palette blobs. The
binary has no such gaps.

Output
------

    output/data_view/
        pal_NNNN_ADDR.png      ← swatch PNG
        pal_NNNN_ADDR.meta.json ← slice metadata (addr, rom_offset, bytes)

For palette editing the user:
    1. opens the PNG,
    2. recolors cells (each cell = one BGR555 color),
    3. saves,
    4. `recompile.py` re-encodes the PNG to bytes and splices them back
       into the ROM at the same offset.

Size constraint: the encoded bytes must match the slice length exactly.

Classifier (v1 — line-proximity + alignment)
--------------------------------------------

A ROM address is a palette candidate when, in some referring code
module:

  1. It's loaded as an `ldr =0x08xxxxxx` immediate, AND
  2. a `ldr =0x05000xxx` (palette RAM) immediate appears within ±WINDOW
     lines of it, AND
  3. its low bit is 0 (THUMB function pointers `addr | 1` are excluded).

Condition 3 matters: ROMs commonly stash THUMB function-pointer tables
near palette-RAM setup code (struct-driven palette managers), and
without it we mis-classify callback tables as palettes.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import click
from PIL import Image


PALETTE_RAM_START = 0x05000000
PALETTE_RAM_END = 0x050003FF
ROM_START = 0x08000000
ROM_END = 0x09FFFFFF
PROXIMITY_WINDOW = 50        # lines between paired xrefs in the same code module
DEFAULT_PALETTE_BYTES = 32   # one 16-color sub-palette (16 × u16)

# Swatch grid layout.
SWATCH_CELL_PX = 16       # each color cell is 16×16 px
SWATCH_COLS = 16          # GBA palettes are organised as 16-color sub-palettes


@dataclass
class PaletteSlice:
    """One palette region in the ROM — (addr, rom_offset, length)."""
    addr: int                # absolute ROM address of the slice start
    rom_offset: int          # file offset = addr - ROM_START
    length: int              # byte count
    png_path: Path
    meta_path: Path


def rom_slice(rom_bytes: bytes, addr: int, length: int) -> bytes | None:
    """Return `length` bytes starting at ROM address `addr`, or None if
    the slice would run past the end of the ROM.
    """
    offset = addr - ROM_START
    if offset < 0 or offset + length > len(rom_bytes):
        return None
    return rom_bytes[offset:offset + length]


def is_bgr555_stream(data: bytes) -> bool:
    """True if every u16 in `data` has bit 15 clear. BGR555 uses 15 bits;
    the top bit is always 0 on real palette data. Blobs of function
    pointers or packed struct data will typically set this bit — useful
    as a final palette-vs-garbage filter.
    """
    if len(data) < 2 or len(data) % 2:
        return False
    return all(data[i] & 0x80 == 0 for i in range(1, len(data), 2))


def collect_palette_candidates(xrefs: dict) -> set[int]:
    """Scan xrefs for halfword-aligned ROM addresses that are line-proximate
    to palette-RAM addresses in the same referring code module.

    Excludes THUMB function pointers (low bit = 1) — struct-driven palette
    managers commonly stash callback tables near palette-RAM setup code
    and we must not mistake them for palettes.
    """
    by_module: dict[str, list[tuple[int, int]]] = {}
    for target, refs in xrefs.items():
        t = int(target, 16)
        for r in refs:
            by_module.setdefault(r["module_path"], []).append(
                (int(r["line"]), t)
            )

    candidates: set[int] = set()
    for entries in by_module.values():
        pal_lines = [(ln, t) for ln, t in entries
                     if PALETTE_RAM_START <= t <= PALETTE_RAM_END]
        rom_lines = [(ln, t) for ln, t in entries
                     if ROM_START <= t <= ROM_END and (t & 1) == 0]
        if not pal_lines or not rom_lines:
            continue
        for pl, _ in pal_lines:
            for rl, rt in rom_lines:
                if abs(pl - rl) <= PROXIMITY_WINDOW:
                    candidates.add(rt)
    return candidates


# ---- Palette render / encode ---------------------------------------


def _bgr555_to_rgb(value: int) -> tuple[int, int, int]:
    """GBA stores color as little-endian u16: 0bbbbbgggggrrrrr (LSB → R)."""
    r = value & 0x1F
    g = (value >> 5) & 0x1F
    b = (value >> 10) & 0x1F
    return (r * 255 // 31, g * 255 // 31, b * 255 // 31)


def _rgb_to_bgr555(rgb: tuple[int, int, int]) -> int:
    r = round(rgb[0] * 31 / 255)
    g = round(rgb[1] * 31 / 255)
    b = round(rgb[2] * 31 / 255)
    return (b << 10) | (g << 5) | r


def render_palette(data: bytes, out_png: Path) -> tuple[int, int]:
    """Decode `data` as BGR555 u16 stream and render a swatch PNG.

    Returns (cols, rows) of the grid. Tail bytes (odd count) are ignored.
    """
    colors = [
        (data[i] | (data[i + 1] << 8))
        for i in range(0, len(data) - 1, 2)
    ]
    if not colors:
        raise ValueError("no bytes to render as palette")

    rows = (len(colors) + SWATCH_COLS - 1) // SWATCH_COLS
    img = Image.new("RGB", (SWATCH_COLS * SWATCH_CELL_PX,
                            rows * SWATCH_CELL_PX), (0, 0, 0))
    px = img.load()
    for i, c in enumerate(colors):
        rgb = _bgr555_to_rgb(c)
        col = i % SWATCH_COLS
        row = i // SWATCH_COLS
        x0 = col * SWATCH_CELL_PX
        y0 = row * SWATCH_CELL_PX
        for dy in range(SWATCH_CELL_PX):
            for dx in range(SWATCH_CELL_PX):
                px[x0 + dx, y0 + dy] = rgb
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)
    return SWATCH_COLS, rows


def encode_palette(png_path: Path, expected_bytes: int | None = None) -> bytes:
    """Sample each swatch cell centre → BGR555 → bytes.

    If `expected_bytes` is given, fails when the encoded stream doesn't
    match — protects the splice contract (must fit original span).
    """
    img = Image.open(png_path).convert("RGB")
    w, h = img.size
    if w % SWATCH_CELL_PX or h % SWATCH_CELL_PX:
        raise ValueError(
            f"{png_path.name} is {w}×{h}; expected dimensions divisible "
            f"by {SWATCH_CELL_PX}"
        )
    cols = w // SWATCH_CELL_PX
    rows = h // SWATCH_CELL_PX
    if cols != SWATCH_COLS:
        raise ValueError(
            f"{png_path.name} has {cols} columns; expected {SWATCH_COLS}"
        )

    out = bytearray()
    half = SWATCH_CELL_PX // 2
    px = img.load()
    for r in range(rows):
        for c in range(cols):
            x = c * SWATCH_CELL_PX + half
            y = r * SWATCH_CELL_PX + half
            v = _rgb_to_bgr555(px[x, y])
            out.append(v & 0xFF)
            out.append((v >> 8) & 0xFF)

    if expected_bytes is not None and len(out) != expected_bytes:
        raise ValueError(
            f"encoded {len(out)} bytes, expected {expected_bytes} "
            f"(palette size changed — splice would not fit)"
        )
    return bytes(out)


# ---- Pipeline step --------------------------------------------------


def process_rom(
    output_dir: Path,
    rom_path: Path,
    *,
    force: bool = False,
) -> list[PaletteSlice]:
    """Find palette candidates via xrefs, slice bytes from the ROM binary,
    and render a swatch PNG per slice.
    """
    xrefs_path = output_dir / "xrefs.json"
    if not xrefs_path.is_file():
        raise FileNotFoundError(
            f"{xrefs_path} missing — run `python xrefs.py` or analyze.py."
        )
    xrefs = json.loads(xrefs_path.read_text())
    palette_candidates = collect_palette_candidates(xrefs)

    rom_bytes = rom_path.read_bytes()
    data_view_dir = output_dir / "data_view"
    data_view_dir.mkdir(parents=True, exist_ok=True)

    slices: list[PaletteSlice] = []
    for addr in sorted(palette_candidates):
        data = rom_slice(rom_bytes, addr, DEFAULT_PALETTE_BYTES)
        if data is None or not is_bgr555_stream(data):
            continue

        name = f"pal_{len(slices):04d}_{addr:08X}"
        png = data_view_dir / f"{name}.png"
        meta = data_view_dir / f"{name}.meta.json"

        if force or not png.is_file() or not meta.is_file():
            render_palette(data, png)
            meta.write_text(json.dumps({
                "addr": f"0x{addr:08X}",
                "rom_offset": addr - ROM_START,
                "length": len(data),
            }, indent=2) + "\n")

        slices.append(PaletteSlice(
            addr=addr,
            rom_offset=addr - ROM_START,
            length=len(data),
            png_path=png,
            meta_path=meta,
        ))
    return slices


def summarise(slices: Iterable[PaletteSlice]) -> dict[str, int]:
    return {"palette_slices": len(list(slices))}


# ---- CLI ------------------------------------------------------------


HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "output"


def _default_rom(output_dir: Path) -> Path | None:
    """Read rom.meta.json (written by disassemble.py) for the ROM path."""
    meta = output_dir / "rom.meta.json"
    if not meta.is_file():
        return None
    return Path(json.loads(meta.read_text())["rom_path"])


@click.group()
def cli() -> None:
    """Typed views + round-trip encoders for palette data in the ROM."""


@cli.command("build")
@click.option("--output", type=click.Path(path_type=Path), default=DEFAULT_OUTPUT,
              show_default=True, help="Pipeline output directory.")
@click.option("--rom", "rom_path", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None,
              help="Path to the .gba ROM binary. Defaults to rom.meta.json's rom_path.")
@click.option("--force", is_flag=True, help="Re-render even if PNG exists.")
def build_cmd(output: Path, rom_path: Path | None, force: bool) -> None:
    """Locate palette slices in the ROM and render swatch PNGs."""
    if rom_path is None:
        rom_path = _default_rom(output)
        if rom_path is None:
            raise click.UsageError(
                f"--rom not given and {output / 'rom.meta.json'} missing; "
                "run disassemble.py or pass --rom explicitly."
            )
    slices = process_rom(output, rom_path, force=force)
    counts = summarise(slices)
    click.secho(f"  {counts}", fg="green")
    for s in slices:
        click.echo(
            f"  0x{s.addr:08X}  rom+{s.rom_offset:#x}  "
            f"({s.length} bytes) → {s.png_path.name}"
        )


@cli.command("encode")
@click.argument("png", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", type=click.Path(path_type=Path), default=DEFAULT_OUTPUT,
              show_default=True, help="Pipeline output directory.")
def encode_cmd(png: Path, output: Path) -> None:
    """Re-encode an edited palette PNG to raw bytes (stdout as hex)."""
    meta_path = output / "data_view" / (png.stem + ".meta.json")
    expected = None
    if meta_path.is_file():
        expected = json.loads(meta_path.read_text()).get("length")
    data = encode_palette(png, expected_bytes=expected)
    click.echo(data.hex())


if __name__ == "__main__":
    cli()
