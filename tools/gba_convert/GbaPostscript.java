// Ghidra post-script — runs INSIDE Ghidra's JVM, not on the host.
//
// Invoked by `ghidra.py` via `analyzeHeadless ... -postScript
// GbaPostscript.java <out_dir>`. Written in Java because Ghidra 12
// removed Jython; the alternative (PyGhidra) needs an extra pip package
// and a different launcher, and there's no reason to bring that in.
//
// For each non-thunk, non-external function in the imported program,
// decompile it and write `<out_dir>/<entry_addr_hex_lower>.c`.
//
// @category GBA
//@keybinding
//@menupath
//@toolbar
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.decompiler.DecompiledFunction;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionManager;

import java.io.File;
import java.io.FileWriter;

public class GbaPostscript extends GhidraScript {

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length == 0) {
            throw new RuntimeException("GbaPostscript expects one argument: the output dir");
        }
        File outDir = new File(args[0]);
        if (!outDir.isDirectory() && !outDir.mkdirs()) {
            throw new RuntimeException("could not create output dir: " + outDir);
        }

        FunctionManager fm = currentProgram.getFunctionManager();
        int total = fm.getFunctionCount();
        println("GbaPostscript: decompiling " + total + " functions -> " + outDir);

        DecompInterface ifc = new DecompInterface();
        ifc.openProgram(currentProgram);

        int written = 0;
        int skipped = 0;
        for (Function f : fm.getFunctions(true)) {
            if (monitor.isCancelled()) break;
            if (f.isThunk() || f.isExternal()) continue;

            String addr = f.getEntryPoint().toString();
            int colon = addr.lastIndexOf(':');
            if (colon >= 0) addr = addr.substring(colon + 1);
            addr = addr.toLowerCase();
            while (addr.length() < 8) addr = "0" + addr;

            DecompileResults res = ifc.decompileFunction(f, 60, monitor);
            if (res == null || !res.decompileCompleted()) {
                skipped++;
                continue;
            }
            DecompiledFunction dfunc = res.getDecompiledFunction();
            if (dfunc == null) {
                skipped++;
                continue;
            }

            File out = new File(outDir, addr + ".c");
            try (FileWriter fw = new FileWriter(out)) {
                fw.write("/* Decompiled by Ghidra.\n");
                fw.write(" * Function: " + f.getName() + "\n");
                fw.write(" * Entry:    0x" + addr + "\n");
                fw.write(" * Signature: " + f.getSignature().getPrototypeString() + "\n");
                fw.write(" */\n");
                fw.write(dfunc.getC());
            }
            written++;
        }

        ifc.dispose();
        println("GbaPostscript: wrote " + written + " files (skipped " + skipped + ")");
    }
}
