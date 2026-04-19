"""Step 5: rebuild a .gba from annotated (possibly spliced) assembly.

Approach: concatenate all module `.s` files (minus the 7-line per-module
headers inserted by split_modules.py) into one composite source, then run
it through the standard as/ld/objcopy chain. This matches the original
Luvdis output semantically (macros + label scoping are defined once at
the top of module 0) and — for an unspliced build — produces a
byte-identical .gba to the original ROM.

Pipeline:
    1. build composite.s from annotated/mod_*.s (falling back to
       recompiled/mod_*.s if --splice is set and that module was edited)
    2. arm-none-eabi-as  -mcpu=arm7tdmi -mthumb-interwork composite.s -o composite.o
    3. arm-none-eabi-ld  -T linker.ld composite.o -o rebuilt.elf
    4. arm-none-eabi-objcopy -O binary rebuilt.elf rebuilt.gba
    5. SHA-1 compare against output/rom.hash.txt (the invariant-1 check).

A matching hash after a clean (non-spliced) build confirms the whole
disassemble-split-annotate chain is byte-exact and that any later
mismatch was introduced by an actual edit, not by a tool bug.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import click

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "output"
DEFAULT_LINKER = HERE / "linker.ld"

REQUIRED_TOOLS = ("arm-none-eabi-as", "arm-none-eabi-ld", "arm-none-eabi-objcopy")
OPTIONAL_TOOLS = ("gbafix",)

MODULE_HEADER_LINES = 7  # matches split_modules.py


@dataclass
class ToolchainCheck:
    found: dict[str, str]
    missing_required: list[str]
    missing_optional: list[str]

    @property
    def ok(self) -> bool:
        return not self.missing_required


def check_toolchain() -> ToolchainCheck:
    found: dict[str, str] = {}
    missing_req: list[str] = []
    missing_opt: list[str] = []
    for tool in REQUIRED_TOOLS:
        path = shutil.which(tool)
        if path:
            found[tool] = path
        else:
            missing_req.append(tool)
    for tool in OPTIONAL_TOOLS:
        path = shutil.which(tool)
        if path:
            found[tool] = path
        else:
            missing_opt.append(tool)
    return ToolchainCheck(found, missing_req, missing_opt)


@dataclass
class SourceBreakdown:
    sources: list[Path]
    n_recompiled: int
    n_annotated: int
    n_raw: int


def collect_source_files(
    modules_dir: Path,
    annotated_dir: Path,
    recompiled_dir: Path | None,
) -> SourceBreakdown:
    """Return module .s files in _index.json order.

    Priority per module: recompiled/ > annotated/ > modules/. The
    `modules/` dir is the authoritative enumeration — every module must
    exist there — and the other two directories layer over it.
    """
    index_path = modules_dir / "_index.json"
    if not index_path.is_file():
        raise FileNotFoundError(f"{index_path} missing — run split first.")
    index = json.loads(index_path.read_text())

    recompiled: dict[str, Path] = {}
    if recompiled_dir and recompiled_dir.is_dir():
        recompiled = {p.name: p for p in recompiled_dir.glob("mod_*.s")}
    annotated: dict[str, Path] = {}
    if annotated_dir.is_dir():
        annotated = {p.name: p for p in annotated_dir.glob("mod_*.s")}

    sources: list[Path] = []
    n_r = n_a = n_m = 0
    for entry in index:
        name = entry["path"]
        raw_path = modules_dir / name
        if not raw_path.is_file():
            raise FileNotFoundError(f"{raw_path} missing.")
        if name in recompiled:
            sources.append(recompiled[name]); n_r += 1
        elif name in annotated:
            sources.append(annotated[name]); n_a += 1
        else:
            sources.append(raw_path); n_m += 1
    return SourceBreakdown(sources, n_r, n_a, n_m)


def build_composite(sources: list[Path], out_path: Path) -> int:
    """Concatenate module bodies (minus 7-line headers) into one .s.

    Returns the total line count written. Matches rom.s byte-for-byte
    when inputs are the unmodified modules — this is the property
    checks.py's `concat` invariant already verifies.
    """
    total_lines = 0
    with out_path.open("w") as fh:
        for s in sources:
            body = s.read_text(errors="replace").splitlines()
            if len(body) < MODULE_HEADER_LINES:
                raise RuntimeError(
                    f"{s.name}: only {len(body)} lines (expected >= {MODULE_HEADER_LINES} header lines)"
                )
            for ln in body[MODULE_HEADER_LINES:]:
                fh.write(ln)
                fh.write("\n")
                total_lines += 1
    return total_lines


def assemble(src: Path, obj: Path) -> None:
    subprocess.run(
        ["arm-none-eabi-as", "-mcpu=arm7tdmi", "-mthumb-interwork",
         "-o", str(obj), str(src)],
        check=True, capture_output=True, text=True,
    )


def link(obj: Path, elf_out: Path, linker_script: Path) -> None:
    subprocess.run(
        ["arm-none-eabi-ld", "-T", str(linker_script),
         "-o", str(elf_out), str(obj)],
        check=True, capture_output=True, text=True,
    )


def objcopy_to_bin(elf: Path, bin_out: Path) -> None:
    subprocess.run(
        ["arm-none-eabi-objcopy", "-O", "binary", str(elf), str(bin_out)],
        check=True, capture_output=True, text=True,
    )


def fix_header(rom: Path) -> None:
    if not shutil.which("gbafix"):
        click.secho(
            "  gbafix not on PATH; skipping header fix "
            "(emulators tolerate bad header checksums).",
            fg="yellow",
        )
        return
    subprocess.run(["gbafix", str(rom)], check=True)


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@click.command()
@click.option("--output", type=click.Path(path_type=Path), default=DEFAULT_OUTPUT,
              show_default=True, help="Output directory from pipeline.py.")
@click.option("--linker", type=click.Path(path_type=Path), default=DEFAULT_LINKER,
              show_default=True, help="Linker script for arm-none-eabi-ld.")
@click.option("--splice/--no-splice", default=True, show_default=True,
              help="If output/recompiled/ exists, prefer those over annotated/.")
@click.option("--skip-gbafix", is_flag=True,
              help="Don't re-checksum the ROM header after objcopy.")
@click.option("--verify/--no-verify", default=True, show_default=True,
              help="Compare SHA-1 of rebuilt.gba against the original ROM hash.")
def main(
    output: Path,
    linker: Path,
    splice: bool,
    skip_gbafix: bool,
    verify: bool,
) -> None:
    """Rebuild rebuilt.gba from the (possibly spliced) disassembly."""
    output = output.resolve()
    modules = output / "modules"
    annotated = output / "annotated"
    recompiled = output / "recompiled"
    build_dir = output / "build"
    composite = build_dir / "composite.s"
    obj = build_dir / "composite.o"
    elf = build_dir / "rebuilt.elf"
    rebuilt = output / "rebuilt.gba"

    if not modules.is_dir():
        click.secho(f"  {modules} missing — run pipeline step 2 first.", fg="red")
        sys.exit(2)

    click.secho("== toolchain check ==", fg="cyan", bold=True)
    tc = check_toolchain()
    for tool, path in tc.found.items():
        click.echo(f"  ✓ {tool}: {path}")
    for tool in tc.missing_required:
        click.secho(f"  ✗ {tool}: MISSING (required)", fg="red")
    for tool in tc.missing_optional:
        click.secho(f"  ⚠ {tool}: missing (optional)", fg="yellow")
    if not tc.ok:
        click.secho(
            "  Install the ARM toolchain (macOS: "
            "`brew install arm-none-eabi-gcc`; "
            "Linux: `apt install gcc-arm-none-eabi`).",
            fg="red",
        )
        sys.exit(2)

    if not linker.is_file():
        click.secho(f"  linker script {linker} missing.", fg="red")
        sys.exit(2)

    click.secho("== collect sources ==", fg="cyan", bold=True)
    breakdown = collect_source_files(
        modules, annotated, recompiled if splice else None,
    )
    sources = breakdown.sources
    click.echo(
        f"  {len(sources)} module(s): "
        f"{breakdown.n_recompiled} recompiled, "
        f"{breakdown.n_annotated} annotated, "
        f"{breakdown.n_raw} raw"
    )
    if not sources:
        click.secho("  no sources found.", fg="red")
        sys.exit(2)

    build_dir.mkdir(parents=True, exist_ok=True)

    click.secho("== build composite.s ==", fg="cyan", bold=True)
    n_lines = build_composite(sources, composite)
    click.echo(f"  wrote {composite} ({n_lines} lines)")

    click.secho("== assemble ==", fg="cyan", bold=True)
    assemble(composite, obj)
    click.echo(f"  wrote {obj}")

    click.secho("== link ==", fg="cyan", bold=True)
    link(obj, elf, linker)
    click.echo(f"  wrote {elf}")

    click.secho("== objcopy ==", fg="cyan", bold=True)
    objcopy_to_bin(elf, rebuilt)
    click.echo(f"  wrote {rebuilt} ({rebuilt.stat().st_size} bytes)")

    if not skip_gbafix:
        click.secho("== gbafix ==", fg="cyan", bold=True)
        fix_header(rebuilt)

    if verify:
        click.secho("== verify ==", fg="cyan", bold=True)
        hash_file = output / "rom.hash.txt"
        if not hash_file.is_file():
            click.secho("  no rom.hash.txt; skipping verify.", fg="yellow")
        else:
            expected = hash_file.read_text().strip()
            actual = sha1(rebuilt)
            if expected == actual:
                click.secho(f"  ✓ SHA-1 matches original: {actual}", fg="green")
            else:
                click.secho("  ✗ SHA-1 MISMATCH", fg="red")
                click.echo(f"    expected: {expected}")
                click.echo(f"    actual:   {actual}")
                if breakdown.n_recompiled:
                    click.secho(
                        f"    ({breakdown.n_recompiled} module(s) were spliced — a mismatch is expected.)",
                        fg="yellow",
                    )
                else:
                    click.secho(
                        "    No splices — this means a Luvdis / toolchain / splitter bug. "
                        "Run `python checks.py` first to narrow it down.",
                        fg="yellow",
                    )
                    sys.exit(1)


if __name__ == "__main__":
    main()
