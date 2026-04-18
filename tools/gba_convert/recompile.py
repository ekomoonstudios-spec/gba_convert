"""Step 4b: compile edited C modules and splice the fresh bytes back into
the Luvdis disassembly.

See PROCESS.md §11b for the full design.

Pipeline (per edited module):
    1. Detect edits:  diff `output/edited/mod_XXXX.c` against
       `output/c_view/mod_XXXX.c`. No edits → nothing to do.
    2. `arm-none-eabi-gcc -mthumb -mcpu=arm7tdmi -Os -nostdlib
        -ffreestanding -c -o <obj> <edited.c>`
    3. `arm-none-eabi-objcopy -O binary --only-section=.text <obj> <bin>`
    4. Size check vs the original byte span from the annotated .s:
         - N' == N : splice verbatim.
         - N' <  N : splice + pad tail with `nop` (0x46C0) to N.
         - N' >  N : FAIL LOUDLY. Caller must either shrink the edit or
                     relocate the function (out of scope for the stub).
    5. Rewrite the corresponding `annotated/mod_XXXX.s` into
       `recompiled/mod_XXXX.s` with the edited function's `.byte` span
       replaced by the freshly compiled bytes. Everything outside the
       edited function stays byte-identical.

STATUS: STUB. Toolchain detection + edit detection + CLI are wired;
gcc invocation, section extraction, and splice logic raise
NotImplementedError with the exact commands/steps to fill in.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import click

HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "output"

REQUIRED_TOOLS = (
    "arm-none-eabi-gcc",
    "arm-none-eabi-objcopy",
    "arm-none-eabi-objdump",
)

GCC_FLAGS = (
    "-mthumb",
    "-mcpu=arm7tdmi",
    "-Os",
    "-nostdlib",
    "-ffreestanding",
    "-Wall",
    "-fno-builtin",
    "-c",
)

THUMB_NOP = b"\xc0\x46"   # `mov r8, r8` — canonical THUMB nop (0x46C0 LE)


@dataclass
class ToolchainCheck:
    found: dict[str, str]
    missing: list[str]

    @property
    def ok(self) -> bool:
        return not self.missing


@dataclass
class EditedModule:
    """One C module that has a matching edit in output/edited/."""
    name: str                 # e.g. "mod_0017_080A1B30.c"
    edited_path: Path         # output/edited/<name>
    baseline_path: Path       # output/c_view/<name>
    annotated_path: Path      # output/annotated/<name>.replace('.c', '.s')


def check_toolchain() -> ToolchainCheck:
    found: dict[str, str] = {}
    missing: list[str] = []
    for tool in REQUIRED_TOOLS:
        path = shutil.which(tool)
        if path:
            found[tool] = path
        else:
            missing.append(tool)
    return ToolchainCheck(found, missing)


def detect_edited_modules(
    edited_dir: Path,
    c_view_dir: Path,
    annotated_dir: Path,
) -> list[EditedModule]:
    """Find every `edited/*.c` whose bytes differ from its `c_view/` twin."""
    if not edited_dir.is_dir():
        return []
    out: list[EditedModule] = []
    for edited in sorted(edited_dir.glob("mod_*.c")):
        baseline = c_view_dir / edited.name
        annotated = annotated_dir / (edited.stem + ".s")
        if not baseline.is_file():
            click.secho(
                f"  ⚠ {edited.name}: no c_view baseline; skipping.",
                fg="yellow",
            )
            continue
        if not annotated.is_file():
            click.secho(
                f"  ⚠ {edited.name}: no annotated/{annotated.name}; skipping.",
                fg="yellow",
            )
            continue
        if edited.read_bytes() == baseline.read_bytes():
            continue
        out.append(
            EditedModule(
                name=edited.name,
                edited_path=edited,
                baseline_path=baseline,
                annotated_path=annotated,
            )
        )
    return out


def compile_module(src: Path, obj: Path, gba_h_dir: Path) -> None:
    """arm-none-eabi-gcc <GCC_FLAGS> -I<gba_h_dir> -o <obj> <src>.

    Raises subprocess.CalledProcessError on failure; .stderr carries
    the gcc diagnostic verbatim so callers (e.g. edit.py) can feed it
    back to the LLM.
    """
    cmd = [
        "arm-none-eabi-gcc",
        *GCC_FLAGS,
        f"-I{gba_h_dir}",
        "-o", str(obj),
        str(src),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def extract_text_bin(obj: Path, bin_out: Path) -> None:
    """arm-none-eabi-objcopy -O binary --only-section=.text <obj> <bin_out>.

    Note: if an edit introduces `.rodata` we currently drop it. The
    surgical splice model is text-only; a rodata-bearing edit needs
    out-of-line relocation (PROCESS.md §11b future work).
    """
    cmd = [
        "arm-none-eabi-objcopy",
        "-O", "binary",
        "--only-section=.text",
        str(obj),
        str(bin_out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def original_byte_span(annotated: Path, func_name: str) -> tuple[int, int, bytes]:
    """
    TODO implement:

    Locate the `.byte` / `.hword` / `.word` span in the annotated .s
    that corresponds to `func_name` (the label) and return:
        (start_line, end_line, original_bytes)

    Strategy:
        - Find the label `func_name:` (or `thumb_func_start func_name`).
        - Walk forward until the next function label or .pool/.align
          boundary.
        - Assemble the literal bytes that appear on those lines.

    See PROCESS.md §11b step 4 for the exact stopping rules.
    """
    raise NotImplementedError(
        "original_byte_span() — locate the func's byte span in the "
        "annotated .s. See PROCESS.md §11b step 4."
    )


def splice(
    annotated_src: Path,
    spliced_dst: Path,
    func_name: str,
    new_bytes: bytes,
) -> None:
    """
    TODO implement:

    Replace the bytes belonging to `func_name` in `annotated_src` with
    `new_bytes`, writing the result to `spliced_dst`. Everything
    outside the function's span is copied verbatim.

    Length contract (enforced by caller, re-check here defensively):
        len(new_bytes) <= len(original_bytes)
    Pad the tail with THUMB_NOP (0xC046) to hit the original length
    exactly — the surrounding code assumes fixed offsets.

    See PROCESS.md §11b steps 4–5.
    """
    raise NotImplementedError(
        "splice() — rewrite the annotated .s with new_bytes in place "
        "of func_name's original byte span. See PROCESS.md §11b step 5."
    )


def recompile_one(
    mod: EditedModule,
    build_dir: Path,
    recompiled_dir: Path,
    gba_h_dir: Path,
) -> None:
    """Compile mod.edited_path, size-check, splice into recompiled_dir."""
    obj = build_dir / (mod.edited_path.stem + ".o")
    bin_ = build_dir / (mod.edited_path.stem + ".bin")
    compile_module(mod.edited_path, obj, gba_h_dir)      # TODO
    extract_text_bin(obj, bin_)                           # TODO

    new_bytes = bin_.read_bytes()

    # The edited function's name is the module's label — for v1 we assume
    # one function per module (the common case for surgical edits). If a
    # module bundles several, we'll need the LLM to mark which one changed.
    func_name = _guess_func_name(mod.edited_path)

    start, end, original = original_byte_span(mod.annotated_path, func_name)
    n_new, n_orig = len(new_bytes), len(original)

    if n_new > n_orig:
        click.secho(
            f"  ✗ {mod.name}: compiled size {n_new} > original {n_orig}. "
            f"Edit must fit in the original span, or the function must "
            f"be relocated (out of scope).",
            fg="red",
        )
        raise click.ClickException(f"{mod.name}: over-size")

    if n_new < n_orig:
        pad = (n_orig - n_new)
        if pad % 2 != 0:
            raise click.ClickException(
                f"{mod.name}: odd-byte padding ({pad}); THUMB is 2-byte"
            )
        new_bytes = new_bytes + THUMB_NOP * (pad // 2)
        click.secho(
            f"  ⚠ {mod.name}: compiled {n_new}B, padded with "
            f"{pad}B of THUMB nops to match original {n_orig}B.",
            fg="yellow",
        )
    else:
        click.echo(f"  ✓ {mod.name}: compiled {n_new}B (exact fit).")

    dst = recompiled_dir / mod.annotated_path.name
    splice(mod.annotated_path, dst, func_name, new_bytes)   # TODO


def _guess_func_name(edited_c: Path) -> str:
    """Placeholder: strip `mod_NNNN_ADDR.c` → `sub_ADDR`.

    The real implementation will parse the `/* @source: ... */` header
    the translator writes and/or the label on the original annotated .s.
    """
    stem = edited_c.stem                     # mod_0017_080A1B30
    parts = stem.split("_")
    if len(parts) >= 3 and all(c in "0123456789abcdefABCDEF" for c in parts[-1]):
        return f"sub_{parts[-1].lower()}"
    return stem


@click.command()
@click.option("--output", type=click.Path(path_type=Path), default=DEFAULT_OUTPUT,
              show_default=True,
              help="Output directory created by pipeline.py.")
@click.option("--only", "only_name", type=str, default=None,
              help="Recompile just one module (e.g. mod_0017_080A1B30.c).")
def main(output: Path, only_name: str | None) -> None:
    """Compile edited C modules and splice the bytes into recompiled/.

    STUB: toolchain + edit detection work today; gcc/objcopy/splice
    calls raise NotImplementedError — fill them in to complete
    Milestone 4 (see PROCESS.md §9).
    """
    output = output.resolve()
    c_view = output / "c_view"
    edited = output / "edited"
    annotated = output / "annotated"
    recompiled = output / "recompiled"
    build = output / "build_c"

    for required in (c_view, annotated):
        if not required.is_dir():
            click.secho(
                f"  {required} missing — run pipeline steps 3 and 4 first.",
                fg="red",
            )
            sys.exit(2)

    click.secho("== toolchain check ==", fg="cyan", bold=True)
    tc = check_toolchain()
    for tool, path in tc.found.items():
        click.echo(f"  ✓ {tool}: {path}")
    for tool in tc.missing:
        click.secho(f"  ✗ {tool}: MISSING", fg="red")
    if not tc.ok:
        click.secho(
            "  Install the ARM toolchain (macOS: "
            "`brew install --cask gcc-arm-embedded`; "
            "Linux: `apt install gcc-arm-none-eabi`).",
            fg="red",
        )
        sys.exit(2)

    click.secho("== detect edits ==", fg="cyan", bold=True)
    mods = detect_edited_modules(edited, c_view, annotated)
    if only_name:
        mods = [m for m in mods if m.name == only_name]
    if not mods:
        if not edited.is_dir():
            click.secho(
                f"  {edited} doesn't exist yet. Copy a file from "
                f"c_view/ into edited/ and modify it to trigger a "
                f"recompile.",
                fg="yellow",
            )
        else:
            click.secho("  no edits detected (edited/ matches c_view/).",
                        fg="yellow")
        return
    click.echo(f"  {len(mods)} edited module(s) to recompile:")
    for m in mods:
        click.echo(f"    - {m.name}")

    recompiled.mkdir(parents=True, exist_ok=True)
    build.mkdir(parents=True, exist_ok=True)

    click.secho("== compile + splice ==", fg="cyan", bold=True)
    for m in mods:
        recompile_one(m, build_dir=build, recompiled_dir=recompiled,
                      gba_h_dir=c_view)

    click.secho(
        f"\n  Done. Spliced modules in {recompiled}. "
        f"Run `python rebuild.py` to turn them into a .gba.",
        fg="green",
    )


if __name__ == "__main__":
    main()
