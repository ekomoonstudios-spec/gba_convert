"""Top-level orchestrator: disasm → split → analyze.

Usage:
    python pipeline.py ROM.gba
    python pipeline.py ROM.gba --skip-analyze
    python pipeline.py ROM.gba --only analyze --force

Env:
    ANTHROPIC_API_KEY must be set for the analyze step.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import click

from disassemble import disassemble
from split_modules import split_asm
from analyze import Analyzer, MODEL
from translate_to_c import CTranslator

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "output"


@click.command()
@click.argument("rom", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", type=click.Path(path_type=Path), default=DEFAULT_OUTPUT,
              help="Output directory (default: tools/gba_convert/output).")
@click.option("--default-mode", type=click.Choice(["BYTE", "THUMB", "WORD"]),
              default="BYTE", show_default=True,
              help="Luvdis default mode for unknown addresses.")
@click.option("--max-lines", type=int, default=1500, show_default=True,
              help="Max lines per module chunk.")
@click.option("--model", default=MODEL, show_default=True,
              help="Anthropic model ID for the analyze step.")
@click.option("--only", type=click.Choice(["disasm", "split", "analyze", "cview"]),
              default=None, help="Run only one phase.")
@click.option("--skip-analyze", is_flag=True,
              help="Run disasm + split, skip all LLM steps.")
@click.option("--skip-cview", is_flag=True,
              help="Run disasm + split + analyze, skip the C-view step.")
@click.option("--force", is_flag=True,
              help="Re-run completed modules in LLM steps.")
@click.option("--include-data", is_flag=True,
              help="Run LLM steps on pure-data modules too. "
                   "By default kind=data modules are skipped — they're "
                   "just `.byte` dumps and waste calls.")
def main(
    rom: Path,
    output: Path,
    default_mode: str,
    max_lines: int,
    model: str,
    only: str | None,
    skip_analyze: bool,
    skip_cview: bool,
    force: bool,
    include_data: bool,
) -> None:
    """Disassemble ROM, split, annotate with Claude, translate to C."""
    output.mkdir(parents=True, exist_ok=True)
    _archive_if_new_rom(rom, output)

    modules_path = output / "modules" / "_index.json"

    if only in (None, "disasm"):
        click.secho("== step 1: disassemble ==", fg="cyan", bold=True)
        result = disassemble(
            rom,
            output,
            default_mode=default_mode,
            seed_config=output / "functions.cfg",
        )
        click.echo(f"  asm:   {result.asm_path}")
        click.echo(f"  hash:  {result.rom_hash}")
        click.echo(f"  info:  {result.rom_info.splitlines()[0] if result.rom_info else '(none)'}")
        (output / "rom.hash.txt").write_text(result.rom_hash + "\n")
        if only == "disasm":
            return

    if only in (None, "split"):
        click.secho("== step 2: split into modules ==", fg="cyan", bold=True)
        mods = split_asm(
            asm_path=output / "rom.s",
            modules_dir=output / "modules",
            max_lines=max_lines,
        )
        click.echo(f"  wrote {len(mods)} modules to {output / 'modules'}")
        if only == "split":
            return

    if skip_analyze or only == "split":
        return

    if only in (None, "analyze"):
        click.secho("== step 3: analyze ==", fg="cyan", bold=True)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            click.secho(
                "  ANTHROPIC_API_KEY is not set. Export it and re-run with --only analyze.",
                fg="red",
            )
            sys.exit(2)
        if not modules_path.is_file():
            click.secho(
                f"  {modules_path} missing — run step 2 first.",
                fg="red",
            )
            sys.exit(2)
        modules = json.loads(modules_path.read_text())
        n_data = sum(1 for m in modules if m.get("kind") == "data")
        if not include_data and n_data:
            click.echo(f"  skipping {n_data} data-only modules "
                       f"(pass --include-data to override)")
        analyzer = Analyzer(output, model=model)
        results = analyzer.analyze_all(modules, force=force,
                                       skip_data=not include_data)
        click.echo(f"  analysed {len(results)} new modules")
        click.echo(f"  variables: {analyzer.variables_md_path}")
        click.echo(f"  functions: {analyzer.functions_cfg_path}")

    if skip_cview or only == "analyze":
        return

    if only in (None, "cview"):
        click.secho("== step 4: translate to C ==", fg="cyan", bold=True)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            click.secho(
                "  ANTHROPIC_API_KEY is not set. Export it and re-run with --only cview.",
                fg="red",
            )
            sys.exit(2)
        if not modules_path.is_file():
            click.secho(
                f"  {modules_path} missing — run step 2 first.",
                fg="red",
            )
            sys.exit(2)
        if not (output / "annotated").is_dir():
            click.secho(
                "  output/annotated/ missing — run step 3 first.",
                fg="red",
            )
            sys.exit(2)
        modules = json.loads(modules_path.read_text())
        n_data = sum(1 for m in modules if m.get("kind") == "data")
        if not include_data and n_data:
            click.echo(f"  skipping {n_data} data-only modules "
                       f"(pass --include-data to override)")
        translator = CTranslator(output, model=model)
        results = translator.translate_all(modules, force=force,
                                           skip_data=not include_data)
        click.echo(f"  translated {len(results)} new modules")
        click.echo(f"  c_view:    {translator.c_dir}")
        click.echo(f"  gba.h:     {translator.gba_h_path}")


def _archive_if_new_rom(rom: Path, output: Path) -> None:
    """If output/ was built from a different ROM, move it aside."""
    import hashlib

    hash_file = output / "rom.hash.txt"
    if not hash_file.is_file():
        return
    old_hash = hash_file.read_text().strip()

    h = hashlib.sha1()
    with rom.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    new_hash = h.hexdigest()

    if new_hash == old_hash:
        return
    archive = output.with_name(f"{output.name}.{old_hash[:8]}")
    click.secho(
        f"  ROM hash changed ({old_hash[:8]} → {new_hash[:8]}); "
        f"archiving previous output to {archive.name}",
        fg="yellow",
    )
    if archive.exists():
        shutil.rmtree(archive)
    output.rename(archive)
    output.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    main()
