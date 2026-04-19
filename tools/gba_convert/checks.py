"""Inter-stage invariants for the gba_convert pipeline.

Two checks, both pure-Python and byte-level. They are cheap and should
be runnable after any change to the splitter or analyzer.

Invariants:

- **concat**: concatenating the raw bodies of all `modules/mod_*.s`
  (minus the 7-line module header inserted by `split_modules.py`) must
  reproduce `rom.s` byte-for-byte. Catches bugs in the splitter.

- **comments**: for every `annotated/mod_*.s`, stripping all `@`
  comments (both Luvdis's original ones and the ones we spliced in)
  must yield the same bytes as stripping `@` comments from the
  corresponding `modules/mod_*.s`. Catches any accidental mutation of
  instruction lines by the analyzer/splicer.

Usage:

    python checks.py                 # run all checks against output/
    python checks.py --check concat
    python checks.py --check comments
    python checks.py --output PATH
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

import click

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "output"

MODULE_HEADER_LINES = 7


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _strip_comment(line: str) -> str:
    """Strip a trailing `@ ...` comment, respecting `"..."` strings."""
    in_str = False
    escape = False
    for i, ch in enumerate(line):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if ch == "@" and not in_str:
            return line[:i].rstrip()
    return line.rstrip()


def _strip_comments_normalize(text: str) -> list[str]:
    """Strip `@` comments, drop resulting blank lines, return line list."""
    out = []
    for ln in text.splitlines():
        stripped = _strip_comment(ln)
        if stripped.strip():
            out.append(stripped)
    return out


def check_concat(output: Path) -> CheckResult:
    rom_s = output / "rom.s"
    index_path = output / "modules" / "_index.json"
    if not rom_s.is_file():
        return CheckResult("concat", False, f"missing {rom_s}")
    if not index_path.is_file():
        return CheckResult("concat", False, f"missing {index_path}")

    modules = json.loads(index_path.read_text())
    rom_lines = rom_s.read_text(errors="replace").splitlines()

    reconstructed: list[str] = []
    for mod in modules:
        mod_path = output / "modules" / mod["path"]
        if not mod_path.is_file():
            return CheckResult("concat", False, f"missing {mod_path}")
        body = mod_path.read_text(errors="replace").splitlines()
        if len(body) < MODULE_HEADER_LINES:
            return CheckResult(
                "concat", False,
                f"{mod['path']}: only {len(body)} lines, expected ≥{MODULE_HEADER_LINES} header lines",
            )
        reconstructed.extend(body[MODULE_HEADER_LINES:])

    if reconstructed == rom_lines:
        return CheckResult(
            "concat", True,
            f"{len(modules)} modules concatenate to {len(rom_lines)} lines — identical to rom.s",
        )

    # Find first divergence for a useful error.
    n = min(len(reconstructed), len(rom_lines))
    first_diff = next((i for i in range(n) if reconstructed[i] != rom_lines[i]), n)
    detail_lines = [
        f"rom.s has {len(rom_lines)} lines, concat has {len(reconstructed)} lines",
        f"first divergence at line {first_diff + 1}:",
    ]
    if first_diff < len(rom_lines):
        detail_lines.append(f"  rom.s:   {rom_lines[first_diff]!r}")
    if first_diff < len(reconstructed):
        detail_lines.append(f"  concat:  {reconstructed[first_diff]!r}")
    return CheckResult("concat", False, "\n".join(detail_lines))


def check_comments(output: Path) -> CheckResult:
    annotated_dir = output / "annotated"
    modules_dir = output / "modules"
    if not annotated_dir.is_dir():
        return CheckResult("comments", False, f"missing {annotated_dir}")
    if not modules_dir.is_dir():
        return CheckResult("comments", False, f"missing {modules_dir}")

    annotated = sorted(annotated_dir.glob("mod_*.s"))
    if not annotated:
        return CheckResult(
            "comments", True,
            "no annotated modules yet — nothing to check (run analyze first)",
        )

    mismatches: list[str] = []
    for ann_path in annotated:
        orig_path = modules_dir / ann_path.name
        if not orig_path.is_file():
            mismatches.append(f"{ann_path.name}: no matching {orig_path}")
            continue
        orig_code = _strip_comments_normalize(orig_path.read_text(errors="replace"))
        ann_code = _strip_comments_normalize(ann_path.read_text(errors="replace"))
        if orig_code == ann_code:
            continue
        n = min(len(orig_code), len(ann_code))
        first_diff = next((i for i in range(n) if orig_code[i] != ann_code[i]), n)
        msg = [
            f"{ann_path.name}: orig has {len(orig_code)} code lines, annotated has {len(ann_code)}",
            f"  first divergence at stripped line {first_diff + 1}:",
        ]
        if first_diff < len(orig_code):
            msg.append(f"    orig:     {orig_code[first_diff]!r}")
        if first_diff < len(ann_code):
            msg.append(f"    annotated:{ann_code[first_diff]!r}")
        mismatches.append("\n".join(msg))

    if not mismatches:
        return CheckResult(
            "comments", True,
            f"{len(annotated)} annotated module(s) match their originals after stripping `@` comments",
        )
    return CheckResult("comments", False, "\n".join(mismatches))


CHECKS = {
    "concat": check_concat,
    "comments": check_comments,
}


@click.command()
@click.option("--output", type=click.Path(path_type=Path), default=DEFAULT_OUTPUT,
              help="Output directory to check.")
@click.option("--check", "which", type=click.Choice(["all", *CHECKS]), default="all",
              help="Which check to run (default: all).")
def main(output: Path, which: str) -> None:
    """Run inter-stage invariants against an output/ directory."""
    names = list(CHECKS) if which == "all" else [which]
    results = [CHECKS[name](output) for name in names]

    for r in results:
        tag = click.style("PASS", fg="green") if r.ok else click.style("FAIL", fg="red")
        click.echo(f"[{tag}] {r.name}: {r.detail}")

    sys.exit(0 if all(r.ok for r in results) else 1)


if __name__ == "__main__":
    main()
