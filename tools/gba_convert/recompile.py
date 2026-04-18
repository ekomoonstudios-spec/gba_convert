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

import re
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


_DIRECTIVE_WIDTH = {
    ".byte": 1, ".db": 1,
    ".2byte": 2, ".hword": 2, ".short": 2, ".half": 2,
    ".4byte": 4, ".word": 4, ".long": 4, ".int": 4,
}
_FUNC_START_MACROS = (
    "thumb_func_start",
    "arm_func_start",
    "non_word_aligned_thumb_func_start",
)
_FUNC_END_MACROS = ("thumb_func_end", "arm_func_end")
_SKIP_DIRECTIVES = {
    ".pool", ".text", ".syntax", ".type", ".size", ".global", ".globl",
    ".thumb", ".arm", ".thumb_func", ".end", ".equ", ".set", ".if",
    ".endif", ".else", ".ltorg", ".extern", ".section", ".data",
    ".macro", ".endm", ".include", ".purgem",
}
_ALIGN_DIRECTIVES = {".align", ".balign", ".p2align"}
_ZERO_DIRECTIVES = {".space", ".skip", ".zero"}
_STR_DIRECTIVES = {".ascii": False, ".asciz": True, ".string": True}


def original_byte_span(annotated: Path, func_name: str) -> tuple[int, int, int]:
    """Locate `func_name`'s byte span in the annotated .s.

    Returns (start_line, end_line, total_bytes) where:
      - start_line is the zero-based index of the `func_name:` label line.
      - end_line is the first line AFTER the span (exclusive).
      - total_bytes is how many bytes the body between them assembles to.

    The span ends at the next `*_func_start` macro, a plausible next
    function label (`sub_XXX:`, `_0xADDR:`), or EOF.

    Only the *count* is authoritative here; the real bytes come from
    the original ROM and don't need to be recovered — all callers use
    the count for budget enforcement.
    """
    lines = annotated.read_text().splitlines()
    label_pat = re.compile(rf"^\s*{re.escape(func_name)}\s*:\s*(@.*)?$")

    start = None
    mode = "thumb"
    for i, line in enumerate(lines):
        if label_pat.match(line):
            start = i
            for j in range(max(0, i - 4), i):
                prev = lines[j].strip()
                if prev.startswith("arm_func_start"):
                    mode = "arm"
                elif prev.startswith(_FUNC_START_MACROS):
                    mode = "thumb"
            break
    if start is None:
        raise ValueError(
            f"label {func_name!r} not found in {annotated.name}"
        )

    next_func = re.compile(r"^\s*(?:" + "|".join(_FUNC_START_MACROS) + r")\b")
    next_label = re.compile(r"^\s*(?:_0?[xX]?[0-9A-Fa-f]+|sub_[0-9A-Fa-f]+)\s*:")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if next_func.match(lines[i]) or next_label.match(lines[i]):
            end = i
            break

    total = _count_bytes(lines[start + 1 : end], mode)
    return start, end, total


def _count_bytes(body: list[str], mode: str) -> int:
    width_inst = 4 if mode == "arm" else 2
    total = 0
    for raw in body:
        line = raw.split("@", 1)[0].strip()
        if not line or line.endswith(":"):
            continue
        first = line.split()[0]
        if first in _FUNC_START_MACROS or first in _FUNC_END_MACROS:
            continue
        if first.startswith("."):
            directive = first
            operand = line[len(first):].strip()
            w = _DIRECTIVE_WIDTH.get(directive)
            if w is not None:
                ops = [x for x in operand.split(",") if x.strip()]
                total += len(ops) * w
                continue
            if directive in _STR_DIRECTIVES:
                total += _count_string_bytes(operand, _STR_DIRECTIVES[directive])
                continue
            if directive in _ZERO_DIRECTIVES:
                n = operand.split(",")[0].strip()
                try:
                    total += int(n, 0)
                except ValueError:
                    pass
                continue
            if directive in _ALIGN_DIRECTIVES or directive in _SKIP_DIRECTIVES:
                continue
            continue
        total += width_inst
    return total


def _count_string_bytes(operand: str, null_terminate: bool) -> int:
    total = 0
    for m in re.finditer(r'"((?:[^"\\]|\\.)*)"', operand):
        s = re.sub(r"\\.", "X", m.group(1))
        total += len(s)
        if null_terminate:
            total += 1
    return total


_BYTES_PER_LINE = 16


def splice(
    annotated_src: Path,
    spliced_dst: Path,
    start_line: int,
    end_line: int,
    new_bytes: bytes,
) -> None:
    """Rewrite `annotated_src` into `spliced_dst` with lines
    (start_line, end_line) replaced by the bytes of `new_bytes`
    emitted as `.byte` directives. The label line at `start_line`
    is kept verbatim; everything at `end_line` onward is kept verbatim.

    Caller must have ensured len(new_bytes) == original span bytes
    (padded with THUMB nops if needed).
    """
    lines = annotated_src.read_text().splitlines(keepends=True)
    prefix = "".join(lines[: start_line + 1])
    suffix = "".join(lines[end_line:])

    body_parts = ["\t@ --- recompiled by recompile.py ---\n"]
    for i in range(0, len(new_bytes), _BYTES_PER_LINE):
        chunk = new_bytes[i : i + _BYTES_PER_LINE]
        body_parts.append(
            "\t.byte " + ", ".join(f"0x{b:02X}" for b in chunk) + "\n"
        )
    body_parts.append("\t@ --- end recompiled ---\n")

    spliced_dst.parent.mkdir(parents=True, exist_ok=True)
    spliced_dst.write_text(prefix + "".join(body_parts) + suffix)


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

    start, end, n_orig = original_byte_span(mod.annotated_path, func_name)
    n_new = len(new_bytes)

    if n_new > n_orig:
        click.secho(
            f"  ✗ {mod.name}: compiled size {n_new} > original {n_orig}. "
            f"Edit must fit in the original span, or the function must "
            f"be relocated (out of scope).",
            fg="red",
        )
        raise click.ClickException(f"{mod.name}: over-size")

    if n_new < n_orig:
        pad = n_orig - n_new
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
    splice(mod.annotated_path, dst, start, end, new_bytes)


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
