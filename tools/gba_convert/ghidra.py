"""Step 3.5: run Ghidra's decompiler over the ROM, dumping per-function C.

Output: one file per function at `output/ghidra_c/<addr>.c` where
`<addr>` is the lowercase 8-hex-digit entry address (e.g. `080012c4.c`).

`translate_to_c.py` picks these up per-module by address range.

How this works
--------------

We shell out to Ghidra's `analyzeHeadless` CLI with a post-script
(`ghidra_postscript.py`) that:

1. Imports the GBA ROM (raw binary, ARM Cortex-M / v4T, THUMB).
2. Runs auto-analysis.
3. Walks every recognised function, invokes the Decompiler API, writes
   the C result to `<output>/ghidra_c/<addr>.c`.

This step is **optional**. If Ghidra isn't installed, the pipeline
still works — `translate_to_c.py` just goes straight from annotated
assembly (no decompiler skeleton).

Requirements
------------

- Ghidra 11.x+ (uses its Python 3 support via PyGhidra). Older Ghidra
  with Jython also works; the post-script is written to be compatible
  with both.
- `GHIDRA_INSTALL_DIR` env var, OR `analyzeHeadless` on PATH.

Usage
-----

    export GHIDRA_INSTALL_DIR=/path/to/ghidra_11.x
    python ghidra.py ROM.gba --output output
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import click

HERE = Path(__file__).resolve().parent
POSTSCRIPT_PATH = HERE / "ghidra_postscript.py"


@dataclass
class GhidraResult:
    ok: bool
    n_files: int
    out_dir: Path
    reason: str = ""


def find_headless() -> Path | None:
    """Locate `analyzeHeadless` via env or PATH. None if missing."""
    env_dir = os.environ.get("GHIDRA_INSTALL_DIR")
    if env_dir:
        candidate = Path(env_dir) / "support" / "analyzeHeadless"
        if candidate.is_file():
            return candidate
    which = shutil.which("analyzeHeadless")
    return Path(which) if which else None


def decompile(
    rom: Path,
    output_dir: Path,
    *,
    headless: Path | None = None,
    project_name: str = "gba_convert",
) -> GhidraResult:
    """Run Ghidra headless over `rom`, dump per-function C to output_dir/ghidra_c/."""
    ghidra_c = output_dir / "ghidra_c"
    ghidra_c.mkdir(parents=True, exist_ok=True)

    headless = headless or find_headless()
    if headless is None:
        return GhidraResult(
            ok=False,
            n_files=0,
            out_dir=ghidra_c,
            reason=(
                "analyzeHeadless not found. Set GHIDRA_INSTALL_DIR or add "
                "<ghidra>/support to PATH. Skipping Ghidra pass — "
                "translate_to_c.py will fall back to annotated asm."
            ),
        )

    with tempfile.TemporaryDirectory(prefix="ghidra_proj_") as proj_dir:
        cmd = [
            str(headless),
            proj_dir,
            project_name,
            "-import", str(rom),
            "-processor", "ARM:LE:32:v4t",
            "-loader", "BinaryLoader",
            "-loader-baseAddr", "0x08000000",
            "-scriptPath", str(HERE),
            "-postScript", POSTSCRIPT_PATH.name, str(ghidra_c),
            "-deleteProject",
            "-analysisTimeoutPerFile", "1800",
        ]
        click.echo(f"  running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").splitlines()[-20:]
            return GhidraResult(
                ok=False,
                n_files=0,
                out_dir=ghidra_c,
                reason=(
                    f"analyzeHeadless exited {result.returncode}.\n"
                    + "\n".join(tail)
                ),
            )

    n = len(list(ghidra_c.glob("*.c")))
    return GhidraResult(ok=True, n_files=n, out_dir=ghidra_c)


@click.command()
@click.argument("rom", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", type=click.Path(path_type=Path),
              default=HERE / "output", show_default=True,
              help="Pipeline output directory.")
@click.option("--ghidra-install-dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=None,
              help="Override GHIDRA_INSTALL_DIR (location containing support/analyzeHeadless).")
def main(rom: Path, output: Path, ghidra_install_dir: Path | None) -> None:
    """Run Ghidra's headless decompiler over ROM."""
    headless = None
    if ghidra_install_dir is not None:
        headless = ghidra_install_dir / "support" / "analyzeHeadless"
        if not headless.is_file():
            click.secho(f"  no analyzeHeadless at {headless}", fg="red")
            sys.exit(2)

    click.secho("== ghidra: decompile ROM ==", fg="cyan", bold=True)
    out = decompile(rom, output, headless=headless)
    if not out.ok:
        click.secho(f"  SKIPPED: {out.reason}", fg="yellow")
        sys.exit(0)
    click.echo(f"  wrote {out.n_files} function(s) to {out.out_dir}")


if __name__ == "__main__":
    main()
