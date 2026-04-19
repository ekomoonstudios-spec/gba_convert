#!/usr/bin/env python3
"""Iterative decompile/compile/compare loop for a single module.

Usage examples:
  # dry-run (no LLM, no toolchain)
  python iterative_loop.py output/modules/mod_0000_08000000.s --mode dryrun --iterations 2

  # full (requires ANTHROPIC_API_KEY and arm-none-eabi toolchain)
  python iterative_loop.py output/modules/mod_0000_08000000.s --mode llm --compile

The script attempts to use the repository's `translate_to_c.py` and
`recompile.py` when available. When dependencies are missing it falls
back to a lightweight pseudo-decompile and prints diagnostics.
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path
from typing import Tuple, Optional
import subprocess
import shutil
import struct

import click


def parse_module_bytes(s_path: Path) -> bytes:
    """Extract raw bytes from `.byte`, `.hword`/`.2byte`, and `.word` directives.

    Falls back to scanning for explicit 0x.. tokens if directives are sparse.
    """
    data = bytearray()
    text = s_path.read_text()
    lines = text.splitlines()
    for raw in lines:
        line = raw.split("@", 1)[0].strip()
        if not line:
            continue
        # .byte / .2byte / .4byte / .hword / .word / .ascii / .asciz / .space
        m = re.match(r"^\s*\.(byte|db)\s+(.*)$", line, flags=re.IGNORECASE)
        if m:
            ops = m.group(2)
            for token in re.split(r",\s*", ops):
                if not token:
                    continue
                token = token.strip()
                if token.startswith('"') or token.startswith("'"):
                    try:
                        s = ast.literal_eval(token)
                        data += s.encode('latin1') if isinstance(s, str) else s
                    except Exception:
                        continue
                else:
                    try:
                        v = int(token, 0)
                    except Exception:
                        continue
                    data.append(v & 0xFF)
            continue

        m2 = re.match(r"^\s*\.(2byte|hword|short|half)\s+(.*)$", line, flags=re.IGNORECASE)
        if m2:
            ops = m2.group(2)
            for token in re.split(r",\s*", ops):
                token = token.strip()
                if not token:
                    continue
                try:
                    v = int(token, 0)
                except Exception:
                    continue
                data += int(v).to_bytes(2, "little", signed=False)
            continue

        m4 = re.match(r"^\s*\.(4byte|word|long|int)\s+(.*)$", line, flags=re.IGNORECASE)
        if m4:
            ops = m4.group(2)
            for token in re.split(r",\s*", ops):
                token = token.strip()
                if not token:
                    continue
                try:
                    v = int(token, 0)
                except Exception:
                    continue
                data += int(v).to_bytes(4, "little", signed=False)
            continue

        mspace = re.match(r"^\s*\.(space|skip|zero)\s+(\S+)", line, flags=re.IGNORECASE)
        if mspace:
            try:
                n = int(mspace.group(2), 0)
            except Exception:
                n = 0
            data += b"\x00" * n
            continue

    # fallback: scan for standalone 0xHH tokens if we collected nothing
    if not data:
        for m in re.finditer(r"0x([0-9A-Fa-f]{2})", text):
            data.append(int(m.group(1), 16))

    return bytes(data)


def pseudo_decompile(module_path: Path, out_c: Path) -> None:
    """Write a minimal, readable C skeleton that documents the bytes.

    This is a fallback for environments without an LLM. The produced C
    is not a faithful decompile but gives a human-editable starting
    point for iterative refinement.
    """
    addr_match = re.search(r"mod_\d+_([0-9A-Fa-f]+)\.s$", module_path.name)
    addr = addr_match.group(1).lower() if addr_match else "unknown"
    func_name = f"sub_{addr}"

    orig = parse_module_bytes(module_path)
    # show up to first 256 bytes as comment
    preview = " ".join(f"{b:02X}" for b in orig[:256])

    header = """
#include <stdint.h>
#include <stddef.h>
#include "gba.h"

/* Pseudo-decompiled from %s */
/* original bytes (first 256): %s */
""" % (module_path.name, preview)

    body = (
        "\nvoid " + func_name + "(void) {\n"
        "    // TODO: replace this skeleton with real C\n"
        "    // original bytes length: " + str(len(orig)) + "\n"
        "    return;\n"
        "}\n"
    )

    out_c.parent.mkdir(parents=True, exist_ok=True)
    out_c.write_text(header + body)


def compare_bytes(orig: bytes, new: bytes, max_lines: int = 16) -> Tuple[int, str]:
    """Return (diff_count, human_readable_sample)."""
    n = min(len(orig), len(new))
    diffs = []
    diff_count = 0
    for i in range(n):
        if orig[i] != new[i]:
            diff_count += 1
            if len(diffs) < max_lines:
                diffs.append(f"@{i:06X}: orig={orig[i]:02X} new={new[i]:02X}")
    # account for length differences
    if len(orig) != len(new):
        diff_count += abs(len(orig) - len(new))
    sample = "\n".join(diffs)
    return diff_count, sample


def _find_ghidra_analyze_headless() -> Optional[Path]:
    """Locate an analyzeHeadless executable from GHIDRA_HOME, workspace install, or PATH."""
    gh_home = os.environ.get("GHIDRA_HOME")
    if gh_home:
        p = Path(gh_home) / "support" / "analyzeHeadless"
        if p.exists():
            return p
    # try workspace ghidra_install
    inst_root = Path.cwd().parent.parent / "ghidra_install"
    if not inst_root.exists():
        inst_root = Path("/workspaces/codespaces-blank/ghidra_install")
    if inst_root.exists():
        for d in inst_root.iterdir():
            if d.is_dir() and d.name.lower().startswith("ghidra"):
                p = d / "support" / "analyzeHeadless"
                if p.exists():
                    return p
    p = shutil.which("analyzeHeadless")
    if p:
        return Path(p)
    return None


def ghidra_decompile(mod_entry: dict, out_c_path: Path, rom_path: Optional[Path], project_root: Path) -> bool:
    """Run Ghidra headless to decompile functions in the module address range.

    Returns True if `out_c_path` was produced.
    """
    analyze = _find_ghidra_analyze_headless()
    if analyze is None:
        click.secho("Ghidra analyzeHeadless not found; set GHIDRA_HOME or install under ghidra_install", fg="red")
        return False

    start = mod_entry.get("addr_start")
    end = mod_entry.get("addr_end")
    if not start or not end:
        click.secho("Module entry missing addr_start/addr_end", fg="red")
        return False

    # determine ROM to import (may be unused if we instead build an ELF from the module)
    rom_file = None
    if rom_path:
        rom_file = Path(rom_path)
        if not rom_file.exists():
            click.secho(f"Provided ROM not found: {rom_file}", fg="yellow")
            rom_file = None
    if rom_file is None:
        candidates = list(Path.cwd().glob("**/*.gba"))
        if candidates:
            rom_file = candidates[0]

    project_root = project_root.resolve()
    proj_dir = project_root / "ghidra_project"
    if proj_dir.exists():
        shutil.rmtree(proj_dir)
    proj_dir.mkdir(parents=True, exist_ok=True)
    project_name = "iter_ghidra"

    # ensure script paths are absolute
    script_path = project_root / "ghidra_decompile.py"
    script_contents = r"""# ghidra_decompile.py
from ghidra.app.decompiler import DecompInterface
from ghidra.util.task import ConsoleTaskMonitor
import sys

def to_addr_obj(a):
    try:
        return toAddr(a)
    except:
        return toAddr(int(a, 16))

if len(sys.argv) < 4:
    print('Usage: ghidra_decompile.py start_addr end_addr out_path')
else:
    start = sys.argv[1]
    end = sys.argv[2]
    out_path = sys.argv[3]
    di = DecompInterface()
    di.openProgram(currentProgram)
    fm = currentProgram.getFunctionManager()
    out_lines = []
    s_addr = to_addr_obj(start)
    e_addr = to_addr_obj(end)
    for func in fm.getFunctions(True):
        entry = func.getEntryPoint()
        if entry.compareTo(s_addr) >= 0 and entry.compareTo(e_addr) <= 0:
            res = di.decompileFunction(func, 60, ConsoleTaskMonitor())
            if res.decompileCompleted():
                cfunc = res.getDecompiledFunction()
                if cfunc:
                    out_lines.append('// Function: %s at %s\n' % (func.getName(), entry))
                    out_lines.append(str(cfunc.getC()))
                    out_lines.append('\n\n')
    f = open(out_path, 'w')
    f.write('\n'.join(out_lines))
    f.close()
"""
    script_path.write_text(script_contents)

    # Also write a Java GhidraScript fallback (does not require PyGhidra)
    script_java_path = project_root / "ghidra_decompile.java"
    script_java = r"""import java.io.*;
import ghidra.app.decompiler.*;
import ghidra.util.task.ConsoleTaskMonitor;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;
import ghidra.program.model.address.Address;

public class ghidra_decompile extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args == null || args.length < 3) {
            println("Usage: ghidra_decompile start_addr end_addr out_path");
            return;
        }
        String start = args[0];
        String end = args[1];
        String outPath = args[2];
        Address s = toAddr(start);
        Address e = toAddr(end);
        DecompInterface di = new DecompInterface();
        di.openProgram(currentProgram);
        FunctionManager fm = currentProgram.getFunctionManager();
        PrintWriter out = new PrintWriter(new FileWriter(outPath));
        try {
            for (Function func : fm.getFunctions(true)) {
                Address entry = func.getEntryPoint();
                if (entry.compareTo(s) >= 0 && entry.compareTo(e) <= 0) {
                    DecompileResults res = di.decompileFunction(func, 60, new ConsoleTaskMonitor());
                    if (res.decompileCompleted()) {
                        out.println("// Function: " + func.getName() + " at " + entry);
                        if (res.getDecompiledFunction() != null) {
                            out.println(res.getDecompiledFunction().getC());
                        }
                        out.println();
                    }
                }
            }
        } finally {
            out.close();
        }
    }
}
"""
    script_java_path.write_text(script_java)

    # Prefer importing a small ELF built from the module bytes when available
    elf_candidate = None
    try:
        # try to locate the module file under output/modules
        mod_path = project_root / "modules" / mod_entry.get("path", "")
        if mod_path.exists():
            # create an ELF file from the module bytes
            elf_candidate = project_root / (mod_path.stem + ".elf")
            payload = parse_module_bytes(mod_path)
            if payload:
                # build a minimal ELF32 (ARM) with one PT_LOAD at p_vaddr = addr_start
                load_addr = int(start, 0)
                e_ident = bytearray(16)
                e_ident[0:4] = b"\x7fELF"
                e_ident[4] = 1  # ELFCLASS32
                e_ident[5] = 1  # ELFDATA2LSB
                e_ident[6] = 1  # EV_CURRENT
                # rest zero
                e_type = 2
                e_machine = 0x28  # EM_ARM
                e_version = 1
                e_entry = load_addr
                e_phoff = 52
                e_shoff = 0
                e_flags = 0
                e_ehsize = 52
                e_phentsize = 32
                e_phnum = 1
                e_shentsize = 0
                e_shnum = 0
                e_shstrndx = 0
                header = bytes(e_ident) + struct.pack('<HHIIIIIHHHHHH', e_type, e_machine, e_version, e_entry, e_phoff, e_shoff, e_flags, e_ehsize, e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx)
                p_offset = e_phoff + e_phentsize * e_phnum
                p_vaddr = load_addr
                p_paddr = p_vaddr
                p_filesz = len(payload)
                p_memsz = p_filesz
                p_flags = 5  # PF_R | PF_X
                p_align = 0x1000
                phdr = struct.pack('<IIIIIIII', 1, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, p_flags, p_align)
                with open(elf_candidate, 'wb') as f:
                    f.write(header)
                    f.write(phdr)
                    # ensure we are at p_offset
                    cur = f.tell()
                    if cur < p_offset:
                        f.write(b'\x00' * (p_offset - cur))
                    f.write(payload)
    except Exception:
        elf_candidate = None

    if elf_candidate and elf_candidate.exists():
        input_file = elf_candidate.resolve()
    elif rom_file is not None:
        input_file = Path(rom_file).resolve()
    else:
        click.secho("No suitable input file found for Ghidra import", fg="red")
        return False

    # prefer Java script (works headless without PyGhidra); pass absolute script path
    script_to_use = script_java_path.resolve()
    cmd = [
        str(analyze),
        str(proj_dir),
        project_name,
        "-import",
        str(input_file),
        "-scriptPath",
        str(project_root.resolve()),
        "-postScript",
        script_to_use.name,
        start,
        end,
        str(out_c_path),
        "-overwrite",
    ]
    click.echo("Running Ghidra headless: " + " ".join(cmd[:4]) + " ...")
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as exc:
        click.secho(f"Ghidra analyzeHeadless failed: {exc}", fg="red")
        return False
    if out_c_path.exists():
        click.echo(f"Ghidra produced: {out_c_path}")
        return True
    click.secho("Ghidra finished but output file not produced", fg="yellow")
    return False


@click.command()
@click.argument("module", type=click.Path(exists=True, path_type=Path))
@click.option("--iterations", type=int, default=3, show_default=True,
              help="Maximum iterations")
@click.option("--mode", type=click.Choice(["dryrun", "llm", "ghidra", "ghidra_llm"]), default="dryrun",
              help="Decompile mode; `llm`/`ghidra_llm` require GEMINI_API_KEY or ANTHROPIC_API_KEY")
@click.option("--rom", type=click.Path(exists=False, path_type=Path), default=None,
              help="Optional path to the original ROM to analyze with Ghidra")
@click.option("--compile/--no-compile", default=False,
              help="Attempt to compile generated C (requires arm toolchain)")
def main(module: Path, iterations: int, mode: str, rom: Optional[Path], compile: bool) -> None:
    output_root = module.parent.parent  # e.g. .../output
    c_view_dir = output_root / "c_view"
    run_dir = output_root / "iterative_runs"
    run_dir.mkdir(parents=True, exist_ok=True)

    click.echo(f"module: {module}")
    orig_bytes = parse_module_bytes(module)
    click.echo(f"original bytes extracted: {len(orig_bytes)} B")

    # optional imports that require toolchain/LLM
    recompile_mod = None
    translator_mod = None
    if compile:
        try:
            import recompile as recompile_mod
        except Exception as exc:
            click.secho("Toolchain helper import failed: recompile.py not usable", fg="yellow")
            recompile_mod = None

    if mode in ("llm", "ghidra_llm"):
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GEMINI_API_KEY")):
            click.secho("No LLM API key found — set ANTHROPIC_API_KEY or GEMINI_API_KEY", fg="red")
            return
        try:
            from translate_to_c import CTranslator
            translator_mod = CTranslator(output_root)
        except Exception as exc:
            click.secho(f"Failed to construct CTranslator: {exc}", fg="red")
            translator_mod = None

    last_c = None
    for it in range(1, iterations + 1):
        click.echo(f"\n--- iteration {it}/{iterations} ---")

        # 1) Decompile
        if mode == "llm" and translator_mod is not None:
            click.echo("Decompiling with LLM (translate_to_c)")
            # find module metadata in _index.json
            idx_path = output_root / "modules" / "_index.json"
            if not idx_path.exists():
                click.secho("modules/_index.json missing — run pipeline split first", fg="red")
                return
            mods = json.loads(idx_path.read_text())
            mod_entry = next((m for m in mods if m.get("path") == module.name), None)
            if not mod_entry:
                click.secho("module not found in _index.json", fg="red")
                return
            try:
                res = translator_mod.translate_one(mod_entry, module)
                c_path = res.c_path
                click.echo(f"LLM produced: {c_path}")
            except Exception as exc:
                click.secho(f"LLM translation failed: {exc}", fg="red")
                return
        elif mode == "ghidra_llm" and translator_mod is not None:
            # --- Ghidra first, then LLM refinement ---
            idx_path = output_root / "modules" / "_index.json"
            if not idx_path.exists():
                click.secho("modules/_index.json missing — run pipeline split first", fg="red")
                return
            mods = json.loads(idx_path.read_text())
            mod_entry = next((m for m in mods if m.get("path") == module.name), None)
            if not mod_entry:
                click.secho("module not found in _index.json", fg="red")
                return
            ghidra_tmp = run_dir / (module.stem + "_ghidra_raw.c")
            click.echo("Running Ghidra headless decompile...")
            ghidra_ok = ghidra_decompile(mod_entry, ghidra_tmp, rom, output_root)
            ghidra_hint = ""
            if ghidra_ok and ghidra_tmp.exists():
                ghidra_hint = ghidra_tmp.read_text()
                click.echo(f"Ghidra raw output: {len(ghidra_hint)} chars")
            else:
                click.secho("Ghidra produced no output; LLM will decompile from ASM alone", fg="yellow")
            click.echo("Refining with LLM (translate_to_c + Ghidra hint)...")
            try:
                res = translator_mod.translate_one(mod_entry, module, ghidra_hint=ghidra_hint)
                c_path = res.c_path
                click.echo(f"LLM produced: {c_path}")
            except Exception as exc:
                click.secho(f"LLM translation failed: {exc}", fg="red")
                return
        elif mode == "ghidra":
            # attempt headless Ghidra decompile for the module
            idx_path = output_root / "modules" / "_index.json"
            if not idx_path.exists():
                click.secho("modules/_index.json missing — run pipeline split first", fg="red")
                return
            mods = json.loads(idx_path.read_text())
            mod_entry = next((m for m in mods if m.get("path") == module.name), None)
            if not mod_entry:
                click.secho("module not found in _index.json", fg="red")
                return
            c_path = run_dir / (module.stem + ".c")
            click.echo("Attempting Ghidra headless decompile (this may take a while)")
            ok = ghidra_decompile(mod_entry, c_path, rom, output_root)
            if not ok:
                click.secho("Ghidra decompile failed or produced nothing; falling back to pseudo-decompile", fg="yellow")
                if not c_path.exists():
                    pseudo_decompile(module, c_path)
            else:
                click.echo(f"Using Ghidra output: {c_path}")
        else:
            # dryrun or fallback
            c_path = run_dir / (module.stem + ".c")
            if not c_path.exists():
                click.echo("Writing pseudo-C fallback")
                pseudo_decompile(module, c_path)
            else:
                click.echo(f"Using existing C: {c_path}")

        last_c = c_path

        # 2) Compile (optional)
        compiled_bytes = b""
        if compile:
            if recompile_mod is None:
                click.secho("Compile requested but recompile helper unavailable", fg="yellow")
            else:
                tc = recompile_mod.check_toolchain()
                if not tc.ok:
                    click.secho(f"Missing toolchain: {tc.missing}", fg="red")
                else:
                    click.echo("Compiling C to binary (.text / .rodata)")
                    build_dir = run_dir / "build"
                    build_dir.mkdir(parents=True, exist_ok=True)
                    obj = build_dir / (c_path.stem + ".o")
                    bin_out = build_dir / (c_path.stem + ".bin")
                    # also ensure raw .bin source files exist as actual binary
                    # (LLM may emit .incbin references)
                    raw_bin_src = c_view_dir / (c_path.stem + ".bin")
                    if not raw_bin_src.exists():
                        raw_bytes_src = parse_module_bytes(module)
                        if raw_bytes_src:
                            raw_bin_src.write_bytes(raw_bytes_src)
                            click.echo(f"  wrote raw binary include: {raw_bin_src}")
                    try:
                        recompile_mod.compile_module(c_path, obj, c_view_dir)
                        recompile_mod.extract_text_bin(obj, bin_out)
                        compiled_bytes = bin_out.read_bytes()
                        if not compiled_bytes:
                            # fall back to .rodata (data-only modules emit here)
                            rodata_out = build_dir / (c_path.stem + "_rodata.bin")
                            subprocess.run([
                                "arm-none-eabi-objcopy", "-O", "binary",
                                "--only-section=.rodata",
                                str(obj), str(rodata_out),
                            ], check=True, capture_output=True)
                            if rodata_out.exists() and rodata_out.stat().st_size > 0:
                                compiled_bytes = rodata_out.read_bytes()
                                click.echo(f"compiled .rodata (data module): {len(compiled_bytes)} B")
                            else:
                                click.secho("compiled .text and .rodata are both 0 bytes — no code or data", fg="yellow")
                        else:
                            click.echo(f"compiled .text: {len(compiled_bytes)} B")
                    except subprocess.CalledProcessError as exc:
                        click.secho(f"Compilation failed (exit {exc.returncode}):", fg="red")
                        if exc.stderr:
                            click.echo(exc.stderr[:2000])
                        if exc.stdout:
                            click.echo(exc.stdout[:500])
                    except Exception as exc:
                        click.secho(f"Compilation failed: {exc}", fg="red")

        # 3) Compare
        if compiled_bytes:
            diff_count, sample = compare_bytes(orig_bytes, compiled_bytes)
            if diff_count == 0:
                click.secho("Compiled bytes match original — converged.", fg="green")
                return
            else:
                click.secho(f"Differences: {diff_count} byte(s)", fg="yellow")
                if sample:
                    click.echo("Sample diffs:\n" + sample)
        else:
            click.secho("No compiled bytes to compare (compile skipped or failed)", fg="yellow")

        # 4) If LLM mode, provide the diff to the next decompile pass.
        if mode in ("llm", "ghidra_llm") and translator_mod is not None and compiled_bytes:
            click.echo("Feeding diff back to LLM for next pass (not implemented: pass diff via annotated file)")
            # Implementation note: to give feedback to the translator we would
            # modify the annotated .s to include the diff or extend the prompt.
            # This repository's `translate_to_c` reads the annotated file, so
            # an approach is to insert a comment with the sample diffs before
            # calling `translate_one` again. For safety we do not mutate files
            # automatically in this script unless explicitly requested.

    click.secho("Iterations complete (no convergence).", fg="yellow")


if __name__ == "__main__":
    main()
