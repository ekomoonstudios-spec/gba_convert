# Module → C view (polish pass)

You are producing the readable C view for one GBA module. **Ghidra's
decompiler has already done the hard lifting** — function boundaries,
control flow, locals, types. Your job is to polish Ghidra's output into
idiomatic GBA C using our conventions, and fall back to the annotated
assembly only where Ghidra was wrong or missing.

The resulting `.c` is compiled by `arm-none-eabi-gcc` and is the edit
surface for ROM modifications. Follow the rules below *in addition to*
the system prompt (`CLAUDE.md`).

## Module metadata

- **Source file:** `{module_path}`
- **Address range:** `{addr_start}` – `{addr_end}`
- **Kind:** `{kind}`   (code | data | mixed)
- **Lines in annotated asm:** `{line_count}`

## Glossary — canonical names (use these verbatim)

```markdown
{glossary}
```

## Module dossier (this module's functions, globals, I/O writes, constants, category)

This is the analyst's notes for THIS module only — richer per-module
context than the glossary. Cross-check Ghidra against this: if the
dossier names a function or global, rename Ghidra's `FUN_080012c4` /
`DAT_03001234` to match.

```markdown
{dossier_md}
```

## Ghidra decompiler output (primary input)

```c
{ghidra_c}
```

If this reads "(no ghidra functions in this module's address range)"
or "(no ghidra_c/ directory — …)", Ghidra isn't available for this
run — fall back to translating the annotated assembly directly.

## Shared `gba.h` (already exists; `#include` it)

Provides:

- Fixed-width typedefs: `u8`, `u16`, `u32`, `s8`, `s16`, `s32`.
- `REG_*` macros for I/O registers (e.g. `REG_DISPCNT`, `REG_KEYINPUT`).
- BIOS SWI wrappers (e.g. `bios_vblank_wait()`, `bios_div(num, denom)`).
- Memory base constants: `EWRAM_BASE`, `IWRAM_BASE`, `VRAM_BASE`,
  `OAM_BASE`, `PALETTE_BASE`, `ROM_BASE`, `SRAM_BASE`.

Do not redeclare any of these. Just `#include "gba.h"`.

## Annotated assembly (fallback / cross-check)

```arm
{module_source}
```

---

## Polish rules

1. **Keep Ghidra's control flow.** If Ghidra lifted a loop into a
   `while` or a switch, keep that shape. Don't rewrite structure
   unless it's demonstrably wrong.
2. **Rename per the dossier + glossary.** Replace every
   `FUN_080012c4`, `DAT_03001234`, `iVar1`, `local_8` with the
   canonical name if one exists. Unknown-but-reasonable names stay as
   `sub_080012c4` / `local_var`.
3. **Swap raw addresses for `REG_*` macros.** `*(short *)0x04000130`
   → `REG_KEYINPUT`. Use the macros in `gba.h`.
4. **SWI calls use wrappers.** Ghidra often emits `swi(6)` or
   `__svc(6)` — turn those into `bios_div(...)`, `bios_vblank_wait()`,
   etc. Match the SWI number to the table in `CLAUDE.md`.
5. **Types carry widths.** Ghidra usually gets this right (`short` vs
   `int` vs `byte`). Normalize to `u8` / `u16` / `u32` / `s8` / `s16`
   / `s32` from `gba.h`. Don't use bare `int`.
6. **Backrefs.** Every translated function body gets
   `// asm: mod_XXXX.s lines Y–Z` at the top. This is how a reviewer
   cross-checks against the original.
7. **Hardware-specific semantics → inline asm.** If Ghidra emitted a
   pattern that depends on specific CPSR flags, LDM/STM ordering for
   timing, or behaviour C can't express, use a GCC extended-asm block
   instead of fabricating C:

   ```c
   __asm__ volatile (
       "..."
       : "=r"(out) : "r"(in) : "cc", "r0"
   );
   ```
8. **Function signatures match the ABI.** AAPCS: args in `r0`–`r3`,
   return in `r0`. Ghidra usually infers this correctly; verify
   against the dossier's `args:` / `returns:` entries.
9. **Data modules.** If `kind` is `data` and Ghidra has nothing
   useful, emit typed globals where you can infer shape
   (`const u16 palette_start[256]`) or `extern const u8 raw[N];` for
   opaque blobs. Don't turn 1500 `.byte` lines into C initializers.
10. **Don't fabricate.** If the annotated asm has `@ purpose unclear`
    and Ghidra's output is also ambiguous, keep the region as a
    `// TODO:` with inline asm — don't invent plausible-looking C.
11. **Compilability.** The C must build with:

    ```sh
    arm-none-eabi-gcc -mthumb -mcpu=arm7tdmi -Os \
        -nostdlib -ffreestanding -Wall -c mod_XXXX.c
    ```

    No libc. No heap. No floats unless the original used soft-float
    helpers (then keep `extern` declarations).
12. **No `main()`.** This is linked into a full ROM.

## Output format

Respond with a **single JSON object**, no prose, no markdown fences:

```json
{{
  "c_source": "string — the ENTIRE .c file contents, starting with a /* @source: mod_XXXX.s */ header and `#include \"gba.h\"`",
  "gba_h_additions": [
    {{
      "name": "REG_SOMETHING",
      "kind": "macro | typedef | extern | enum",
      "definition": "full C declaration or #define line",
      "reason": "why it's needed — short"
    }}
  ],
  "notes": "one short paragraph: what Ghidra got right/wrong, renames applied, ambiguity remaining. Empty string if nothing."
}}
```

`c_source` is written verbatim to `output/c_view/<same-basename>.c`.
`gba_h_additions` entries are appended to `output/c_view/gba.h` only
if that `name` isn't already present. Use sparingly.
