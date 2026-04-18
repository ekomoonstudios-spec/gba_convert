"""Step 5: rebuild a .gba from annotated (possibly spliced) assembly.

See PROCESS.md §11a for the full design.

Pipeline:
    1. `arm-none-eabi-as -mcpu=arm7tdmi -mthumb-interwork -o <o> <s>`
       for every `.s` in `annotated/` (or `recompiled/` when splicing).
    2. `arm-none-eabi-ld -T linker.ld -o rebuilt.elf *.o`
    3. `arm-none-eabi-objcopy -O binary rebuilt.elf rebuilt.gba`
    4. `gbafix rebuilt.gba` — recompute header checksum.
    5. Compare SHA-1 of `rebuilt.gba` against `output/rom.hash.txt`.
       Matching hash = Luvdis disassembly round-trips cleanly.

STATUS: STUB. Toolchain detection + hash comparison + CLI are wired;
the actual as/ld/objcopy calls raise NotImplementedError with the
exact commands to fill in. When you're ready to implement, each
`NotImplementedError` block is a drop-in target.
"""
from __future__ import annotations

import hashlib
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


def collect_source_files(
    annotated_dir: Path,
    recompiled_dir: Path | None,
) -> list[Path]:
    """Return the list of .s files to assemble, preferring recompiled/."""
    annotated = sorted(annotated_dir.glob("mod_*.s"))
    if not recompiled_dir or not recompiled_dir.is_dir():
        return annotated

    recompiled = {p.name: p for p in recompiled_dir.glob("mod_*.s")}
    merged: list[Path] = []
    for p in annotated:
        merged.append(recompiled.get(p.name, p))
    return merged


def assemble(src: Path, obj: Path) -> None:
    """
    TODO implement:

        arm-none-eabi-as -mcpu=arm7tdmi -mthumb-interwork \
            -o <obj> <src>

    Returns nothing on success, raises CalledProcessError on failure.
    """
    raise NotImplementedError(
        "assemble() — fill in with arm-none-eabi-as invocation. "
        "See PROCESS.md §11a step 1."
    )


def link(objects: list[Path], elf_out: Path, linker_script: Path) -> None:
    """
    TODO implement:

        arm-none-eabi-ld -T <linker_script> -o <elf_out> <objects...>
    """
    raise NotImplementedError(
        "link() — fill in with arm-none-eabi-ld invocation. "
        "See PROCESS.md §11a step 2."
    )


def objcopy_to_bin(elf: Path, bin_out: Path) -> None:
    """
    TODO implement:

        arm-none-eabi-objcopy -O binary <elf> <bin_out>
    """
    raise NotImplementedError(
        "objcopy_to_bin() — fill in with arm-none-eabi-objcopy. "
        "See PROCESS.md §11a step 3."
    )


def fix_header(rom: Path) -> None:
    """Recompute the GBA header checksum. `gbafix` is optional — if it's
    not on PATH, we skip silently but warn the user."""
    if not shutil.which("gbafix"):
        click.secho(
            "  gbafix not on PATH; skipping header fix. "
            "Install via `brew install gbafix` or `pip install gbafix`.",
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
              show_default=True,
              help="Output directory created by pipeline.py.")
@click.option("--linker", type=click.Path(path_type=Path), default=DEFAULT_LINKER,
              show_default=True,
              help="Linker script for arm-none-eabi-ld.")
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
    """Rebuild rebuilt.gba from the (possibly spliced) disassembly.

    STUB: toolchain detection + file collection + verify work today.
    Assembly/link/objcopy raise NotImplementedError — fill them in to
    complete Milestone 3 (see PROCESS.md §9).
    """
    output = output.resolve()
    annotated = output / "annotated"
    recompiled = output / "recompiled"
    build_dir = output / "build"
    rebuilt = output / "rebuilt.gba"
    elf = build_dir / "rebuilt.elf"

    if not annotated.is_dir():
        click.secho(f"  {annotated} missing — run pipeline step 3 first.", fg="red")
        sys.exit(2)

    click.secho("== toolchain check ==", fg="cyan", bold=True)
    tc = check_toolchain()
    for tool, path in tc.found.items():
        click.echo(f"  ✓ {tool}: {path}")
    for tool in tc.missing_required:
        click.secho(f"  ✗ {tool}: MISSING (required)", fg="red")
    for tool in tc.missing_optional:
        click.secho(f"  ⚠ {tool}: missing (optional; checksum fix skipped)", fg="yellow")
    if not tc.ok:
        click.secho(
            "  Install the ARM toolchain (macOS: "
            "`brew install --cask gcc-arm-embedded`; "
            "Linux: `apt install gcc-arm-none-eabi`).",
            fg="red",
        )
        sys.exit(2)

    if not linker.is_file():
        click.secho(
            f"  linker script {linker} missing — see linker.ld.example "
            f"or PROCESS.md §11a.",
            fg="red",
        )
        sys.exit(2)

    click.secho("== collect sources ==", fg="cyan", bold=True)
    sources = collect_source_files(annotated, recompiled if splice else None)
    n_spliced = sum(1 for p in sources if recompiled.is_dir() and p.is_relative_to(recompiled))
    click.echo(f"  {len(sources)} .s files "
               f"({n_spliced} from recompiled/, {len(sources) - n_spliced} from annotated/)")
    if not sources:
        click.secho("  no sources found.", fg="red")
        sys.exit(2)

    build_dir.mkdir(parents=True, exist_ok=True)

    # -------- assembly / link / objcopy --------
    click.secho("== assemble ==", fg="cyan", bold=True)
    objs: list[Path] = []
    for s in sources:
        o = build_dir / (s.stem + ".o")
        assemble(s, o)   # TODO: implement
        objs.append(o)

    click.secho("== link ==", fg="cyan", bold=True)
    link(objs, elf, linker)   # TODO: implement

    click.secho("== objcopy ==", fg="cyan", bold=True)
    objcopy_to_bin(elf, rebuilt)   # TODO: implement

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
                click.secho(f"  ✗ SHA-1 MISMATCH", fg="red")
                click.echo(f"    expected: {expected}")
                click.echo(f"    actual:   {actual}")
                click.secho(
                    "  If you spliced in recompiled C, expect a mismatch "
                    "(the edit is the point). If you built from annotated/ "
                    "alone, this is a Luvdis / assembly-toolchain bug.",
                    fg="yellow",
                )


if __name__ == "__main__":
    main()
