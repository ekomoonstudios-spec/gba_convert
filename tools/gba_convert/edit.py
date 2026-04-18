"""Step 4.5: apply a natural-language edit to a c_view module.

Workflow:
    python edit.py "bump max HP from 100 to 999"

  Stage 1: Claude reads variables.md + the list of c_view modules and
           picks the most likely target file.
  Stage 2: Claude rewrites that module to carry out the instruction.
  Stage 3: We try compiling the result with arm-none-eabi-gcc. On
           failure, the stderr is fed back to Claude and Stage 2 repeats
           (up to MAX_RETRIES times).
  Stage 4: On success, the new source lands in output/edited/<name>.c.

After this, run:
    python recompile.py   # splice compiled bytes into recompiled/*.s
    python rebuild.py     # assemble + link + objcopy → rebuilt.gba

See PROCESS.md §11b for the surgical-splice model.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import click
from anthropic import Anthropic

from recompile import check_toolchain, compile_module

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "output"
SYSTEM_PROMPT_PATH = HERE / "CLAUDE.md"
TARGET_PROMPT_PATH = HERE / "prompts" / "edit_target.md"
APPLY_PROMPT_PATH = HERE / "prompts" / "edit_apply.md"

MODEL = "claude-opus-4-7"
MAX_TOKENS = 16_000
MAX_RETRIES = 3


@dataclass
class EditResult:
    module_name: str
    c_path: Path
    notes: str
    attempts: int


class Editor:
    def __init__(self, output_dir: Path, *, model: str = MODEL) -> None:
        self.output_dir = output_dir
        self.c_view_dir = output_dir / "c_view"
        self.edited_dir = output_dir / "edited"
        vars_path = output_dir / "variables.md"
        self.variables_md = vars_path.read_text() if vars_path.is_file() else "(variables.md not found)"

        self.client = Anthropic()
        self.model = model
        self.system_prompt = SYSTEM_PROMPT_PATH.read_text()
        self.target_template = TARGET_PROMPT_PATH.read_text()
        self.apply_template = APPLY_PROMPT_PATH.read_text()

    def find_target(self, instruction: str) -> str:
        modules = sorted(p.name for p in self.c_view_dir.glob("mod_*.c"))
        if not modules:
            raise click.ClickException(f"no c_view modules found in {self.c_view_dir}")
        prompt = self.target_template.format(
            instruction=instruction,
            variables_md=self.variables_md,
            module_list="\n".join(f"- {m}" for m in modules),
        )
        raw = self._call(prompt)
        parsed = _extract_json(raw)
        candidates = parsed.get("candidates") or []
        reasoning = parsed.get("reasoning", "(no reasoning)")
        if not candidates:
            raise click.ClickException(
                f"Claude found no candidate module. Reasoning: {reasoning}"
            )
        top = candidates[0]
        if top not in modules:
            raise click.ClickException(
                f"Claude picked unknown module {top!r}. Reasoning: {reasoning}"
            )
        click.echo(f"  reasoning: {reasoning}")
        if len(candidates) > 1:
            click.echo(f"  also considered: {', '.join(candidates[1:])}")
        return top

    def apply_edit(self, module_name: str, instruction: str) -> EditResult:
        baseline = (self.c_view_dir / module_name).read_text()
        self.edited_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.edited_dir / module_name
        obj_path = out_path.with_suffix(".o.test")

        retry_context = ""
        last_stderr = ""
        last_source = ""

        for attempt in range(1, MAX_RETRIES + 1):
            click.echo(f"  attempt {attempt}/{MAX_RETRIES}...")
            prompt = self.apply_template.format(
                instruction=instruction,
                module_name=module_name,
                source=baseline,
                variables_md=self.variables_md,
                retry_context=retry_context,
            )
            raw = self._call(prompt)
            parsed = _extract_json(raw)
            new_source = parsed.get("c_source", "") or ""
            notes = parsed.get("notes", "") or ""

            if not new_source.strip():
                raise click.ClickException(
                    f"Claude returned empty c_source on attempt {attempt}"
                )

            out_path.write_text(new_source)
            last_source = new_source

            try:
                compile_module(out_path, obj_path, self.c_view_dir)
                obj_path.unlink(missing_ok=True)
                click.secho(f"  ✓ compiled cleanly on attempt {attempt}", fg="green")
                return EditResult(
                    module_name=module_name,
                    c_path=out_path,
                    notes=notes,
                    attempts=attempt,
                )
            except subprocess.CalledProcessError as exc:
                last_stderr = (exc.stderr or "").strip() or "(no stderr)"
                click.secho(f"  ✗ gcc rejected attempt {attempt}", fg="yellow")
                for line in last_stderr.splitlines()[:8]:
                    click.echo(f"    {line}")
                if attempt < MAX_RETRIES:
                    retry_context = _retry_block(last_source, last_stderr)

        out_path.write_text(last_source)
        raise click.ClickException(
            f"Edit failed after {MAX_RETRIES} attempts. "
            f"Last attempt saved at {out_path}. Last stderr:\n{last_stderr}"
        )

    def _call(self, prompt: str) -> str:
        message = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=[{
                "type": "text",
                "text": self.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in message.content if b.type == "text")


def _retry_block(prev_source: str, stderr: str) -> str:
    return (
        "\n\n---\n\n"
        "## Your previous attempt failed to compile\n\n"
        "### Your previous output\n\n"
        "```c\n"
        f"{prev_source}\n"
        "```\n\n"
        "### gcc stderr\n\n"
        "```\n"
        f"{stderr}\n"
        "```\n\n"
        "Fix the error(s) and return corrected source in the same JSON "
        "format. Do NOT relax the hard constraints — signatures stay, "
        "`#include \"gba.h\"` stays, no libc."
    )


_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_OBJ.search(raw)
        if not m:
            raise
        return json.loads(m.group(0))


@click.command()
@click.argument("instruction")
@click.option("--output", type=click.Path(path_type=Path), default=DEFAULT_OUTPUT,
              show_default=True,
              help="Output directory created by pipeline.py.")
@click.option("--module", "module_override", default=None,
              help="Skip Stage 1. Edit this module directly "
                   "(e.g. mod_0017_080A1B30.c or just mod_0017_080A1B30).")
@click.option("--model", default=MODEL, show_default=True,
              help="Anthropic model ID.")
def main(
    instruction: str,
    output: Path,
    module_override: str | None,
    model: str,
) -> None:
    """Apply a natural-language edit to one c_view module."""
    output = output.resolve()
    c_view = output / "c_view"

    if not c_view.is_dir():
        click.secho(
            f"  {c_view} missing — run `python pipeline.py ROM` first "
            f"(through step 4).",
            fg="red",
        )
        sys.exit(2)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        click.secho("  ANTHROPIC_API_KEY not set. Export it and re-run.", fg="red")
        sys.exit(2)

    click.secho("== toolchain check ==", fg="cyan", bold=True)
    tc = check_toolchain()
    for tool, path in tc.found.items():
        click.echo(f"  ✓ {tool}: {path}")
    for tool in tc.missing:
        click.secho(f"  ✗ {tool}: MISSING", fg="red")
    if not tc.ok:
        click.secho(
            "  Install the ARM toolchain "
            "(macOS: `brew install --cask gcc-arm-embedded`).",
            fg="red",
        )
        sys.exit(2)

    editor = Editor(output, model=model)

    if module_override:
        target = module_override if module_override.endswith(".c") else f"{module_override}.c"
        if not (c_view / target).is_file():
            click.secho(f"  {c_view / target} not found.", fg="red")
            sys.exit(2)
        click.echo(f"  using explicit target: {target}")
    else:
        click.secho("== stage 1: pick target module ==", fg="cyan", bold=True)
        target = editor.find_target(instruction)
        click.echo(f"  target: {target}")

    click.secho("== stage 2: apply edit + compile loop ==", fg="cyan", bold=True)
    result = editor.apply_edit(target, instruction)

    click.secho(
        f"\n  ✓ wrote {result.c_path} (attempts: {result.attempts})",
        fg="green",
    )
    if result.notes:
        click.echo(f"  notes: {result.notes}")
    click.echo(
        "\n  Next:\n"
        "    python recompile.py   # splice compiled bytes into recompiled/*.s\n"
        "    python rebuild.py     # assemble → rebuilt.gba"
    )


if __name__ == "__main__":
    main()
