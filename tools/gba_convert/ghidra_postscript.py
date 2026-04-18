"""Ghidra post-script — runs INSIDE Ghidra's JVM, not on the host.

Invoked by `ghidra.py` via `analyzeHeadless ... -postScript
ghidra_postscript.py <out_dir>`. Must be compatible with both Ghidra's
Jython (old) and PyGhidra (Python 3, 11.x+) — so we stick to the
lowest-common-denominator API and avoid f-strings / type hints.

For each function in the imported program, decompile it and write
`<out_dir>/<entry_addr_hex_lower>.c`.

Ghidra injects a set of globals into the script's namespace:
    currentProgram, monitor, getScriptArgs(), askString(...), etc.

References:
    ghidra.app.decompiler.DecompInterface
    ghidra.util.task.ConsoleTaskMonitor
"""
# pylint: disable=undefined-variable,import-error
from __future__ import print_function

import os

from ghidra.app.decompiler import DecompInterface
from ghidra.util.task import ConsoleTaskMonitor


def _out_dir():
    args = getScriptArgs()  # noqa: F821 — injected by Ghidra
    if not args:
        raise RuntimeError("ghidra_postscript.py expects one argument: the output dir")
    d = args[0]
    if not os.path.isdir(d):
        os.makedirs(d)
    return d


def _addr_key(func):
    entry = func.getEntryPoint()
    # Drop Ghidra's address-space prefix (e.g. "ram:") and lowercase.
    raw = str(entry).split(":")[-1].lower()
    # Zero-pad to 8 hex digits so files sort naturally.
    return raw.zfill(8)


def _header_comment(func, addr_hex):
    sig = func.getSignature().getPrototypeString()
    return (
        "/* Decompiled by Ghidra.\n"
        " * Function: " + func.getName() + "\n"
        " * Entry:    0x" + addr_hex + "\n"
        " * Signature: " + sig + "\n"
        " */\n"
    )


def _decompile_one(ifc, func, timeout_seconds=60):
    monitor = ConsoleTaskMonitor()
    result = ifc.decompileFunction(func, timeout_seconds, monitor)
    if result is None or not result.decompileCompleted():
        return None
    dfunc = result.getDecompiledFunction()
    if dfunc is None:
        return None
    return dfunc.getC()


def run():
    out_dir = _out_dir()
    program = currentProgram  # noqa: F821
    fn_mgr = program.getFunctionManager()
    total = fn_mgr.getFunctionCount()
    print("ghidra_postscript: decompiling " + str(total) + " functions → " + out_dir)

    ifc = DecompInterface()
    ifc.openProgram(program)

    written = 0
    for func in fn_mgr.getFunctions(True):  # True = forward iteration
        if monitor.isCancelled():  # noqa: F821 — Ghidra global
            break
        if func.isThunk() or func.isExternal():
            continue
        addr_hex = _addr_key(func)
        out_path = os.path.join(out_dir, addr_hex + ".c")
        c_src = _decompile_one(ifc, func)
        if c_src is None:
            print("  skip " + addr_hex + " (decompile failed)")
            continue
        with open(out_path, "w") as fh:
            fh.write(_header_comment(func, addr_hex))
            fh.write(c_src)
        written += 1

    ifc.dispose()
    print("ghidra_postscript: wrote " + str(written) + " files")


run()
