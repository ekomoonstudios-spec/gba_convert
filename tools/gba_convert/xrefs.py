"""Build an xref index over the disassembly.

For every address loaded as a 32-bit immediate (literal pool), record
every code site that references it. This is the foundation for:

- Data-module shape classification (who writes this to palette RAM? VRAM?)
- Trampoline targeting (which lookup function owns this table?)
- Relocation (if we move this address, who needs patching?)

Luvdis emits immediate loads as two collaborating lines:

    ldr r1, _080025C4 @ =0x03005B00        <-- the load site
    ...
    _080025C4: .4byte 0x03005B00           <-- the literal pool entry

We capture both. The load-site comment is the primary signal (gives us
the referring function line); the literal pool is a secondary signal and
lets us catch any immediate the assembler can materialise.

Output: `output/xrefs.json`
    {
      "0x03005b00": [
        {"module_index": 1, "module_path": "mod_0001_08002570.s",
         "line": 24, "kind": "ldr_imm",
         "raw": "ldr r1, _080025C4 @ =0x03005B00"},
        ...
      ],
      ...
    }

Addresses are normalised to lowercase 8-hex-digit with `0x` prefix so
lookups are canonical.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import click


_LDR_IMM = re.compile(
    r"\bldr\s+r\w+\s*,\s*\S+\s*@\s*=0x([0-9a-fA-F]+)",
    re.IGNORECASE,
)
_LITERAL_POOL = re.compile(
    r"^\s*\S+:\s*\.4byte\s+0x([0-9a-fA-F]+)",
    re.IGNORECASE,
)


def _normalise(addr_hex: str) -> str:
    return f"0x{addr_hex.lower().zfill(8)}"


def _scan_module(module_path: Path) -> list[dict]:
    """Return (line_number, kind, raw, target_addr) tuples for one module."""
    hits: list[dict] = []
    for ln, line in enumerate(module_path.read_text().splitlines(), 1):
        m = _LDR_IMM.search(line)
        if m:
            hits.append({
                "line": ln,
                "kind": "ldr_imm",
                "raw": line.strip(),
                "target": _normalise(m.group(1)),
            })
            continue
        m = _LITERAL_POOL.match(line)
        if m:
            hits.append({
                "line": ln,
                "kind": "literal_pool",
                "raw": line.strip(),
                "target": _normalise(m.group(1)),
            })
    return hits


def build_xrefs(modules_dir: Path, modules_index: list[dict]) -> dict:
    """Scan every module; return {target_addr: [{module, line, kind, raw}, ...]}."""
    xrefs: dict[str, list[dict]] = {}
    for mod in modules_index:
        path = modules_dir / mod["path"]
        if not path.is_file():
            continue
        for hit in _scan_module(path):
            xrefs.setdefault(hit["target"], []).append({
                "module_index": mod["index"],
                "module_path": mod["path"],
                "line": hit["line"],
                "kind": hit["kind"],
                "raw": hit["raw"],
            })
    return xrefs


def rebuild(output_dir: Path) -> int:
    """Refresh output/xrefs.json. Returns the count of distinct targets."""
    modules_dir = output_dir / "modules"
    index_path = modules_dir / "_index.json"
    if not index_path.is_file():
        raise FileNotFoundError(
            f"{index_path} missing — run split_modules first."
        )
    modules_index = json.loads(index_path.read_text())
    xrefs = build_xrefs(modules_dir, modules_index)
    out = output_dir / "xrefs.json"
    out.write_text(json.dumps(xrefs, indent=2) + "\n")
    return len(xrefs)


HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "output"


@click.command()
@click.option("--output", type=click.Path(path_type=Path), default=DEFAULT_OUTPUT,
              show_default=True, help="Pipeline output directory.")
def main(output: Path) -> None:
    """Rebuild output/xrefs.json from the split modules."""
    n = rebuild(output)
    click.secho(f"  xrefs: {n} distinct target addresses → {output / 'xrefs.json'}",
                fg="green")


if __name__ == "__main__":
    main()
