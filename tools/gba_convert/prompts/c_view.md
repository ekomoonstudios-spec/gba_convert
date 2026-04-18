# Module → C view request

You are translating one ANNOTATED GBA assembly module into C. The C
will be compiled by `arm-none-eabi-gcc` and is both a comprehension
aid AND the edit surface for ROM modifications. Follow the rules
below _in addition to_ the system prompt (`CLAUDE.md`).

## Module metadata

- **Source file:** `{module_path}`
- **Address range:** `{addr_start}` – `{addr_end}`
- **Kind:** `{kind}`   (code | data | mixed)
- **Lines in this module:** `{line_count}`

## Current `variables.md`

Use these names. Don't invent new ones for functions or globals that
are already listed here.

```markdown
{variables_md}
```

## Shared `gba.h` (already exists; `#include` it)

Provides:

- Fixed-width typedefs: `u8`, `u16`, `u32`, `s8`, `s16`, `s32`.
- `REG_*` macros for I/O registers (e.g. `REG_DISPCNT`, `REG_KEYINPUT`).
- BIOS SWI wrappers (e.g. `bios_vblank_wait()`, `bios_div(num, denom)`).
- Memory base constants: `EWRAM_BASE`, `IWRAM_BASE`, `VRAM_BASE`,
  `OAM_BASE`, `PALETTE_BASE`, `ROM_BASE`, `SRAM_BASE`.

Do not redeclare any of these. Just `#include "gba.h"`.

## Annotated assembly

```arm
{module_source}
```

---

## Translation rules

1. **Compilability.** The C must build with:

   ```sh
   arm-none-eabi-gcc -mthumb -mcpu=arm7tdmi -Os \
       -nostdlib -ffreestanding -Wall -c mod_XXXX.c
   ```

   No libc calls. No heap. No floating point unless the annotated ASM
   was using soft-float helpers (then keep calling them as `extern`).

2. **Types carry LDR/STR widths.** `ldrb` → `uint8_t`, `ldrh` →
   `uint16_t`, `ldr` → `uint32_t`. Don't use `int` for everything;
   the width is semantically important.

3. **Use names from `variables.md`.** Function and global names must
   match exactly. If `variables.md` calls something
   `update_player_input`, use that.

4. **Named I/O registers.** `*(volatile u16*)0x04000130` →
   `REG_KEYINPUT`. Use the `REG_*` macros from `gba.h`.

5. **BIOS calls use the wrapper.** `swi 0x06` → `bios_div(...)`.
   Don't emit inline `__asm__("swi 0x06")` unless the calling
   convention differs from the wrapper.

6. **Backrefs on every non-trivial block.** Every translated function
   body gets `// asm: mod_XXXX.s lines Y–Z` at the top, and any
   non-obvious local block gets its own `// asm:` comment. This is
   how a reviewer cross-checks the translation.

7. **Hardware-specific semantics → inline asm.** If the original
   depends on specific CPSR flag state, LDM/STM ordering for timing,
   or other behaviour C can't express, emit a GCC extended-asm block
   rather than fabricate wrong C:

   ```c
   __asm__ volatile (
       "mov r0, %0\n\t"
       "..."
       : "=r"(result)
       : "r"(input)
       : "r0", "cc"
   );
   ```

8. **Function signatures match the ABI.** AAPCS: args in `r0`–`r3`,
   return in `r0`. If a function uses `r4`–`r11` they're local
   variables (callee-saved). Don't over-declare arguments.

9. **Data modules.** If `kind` is `data`, emit a C file that declares
   the data as typed globals where you can infer the shape (e.g.
   `const u16 palette_start[256]`). If the shape is opaque, emit
   `extern const u8 mod_XXXX_raw[N];` and `#include` the raw bytes
   verbatim via `.incbin` equivalents — don't try to turn 1500 lines
   of `.byte` into C initialisers.

10. **Don't fabricate.** If a region is `@ purpose unclear`, keep it
    as a `// TODO:` comment with an inline asm block, don't invent a
    plausible-looking C function that "does something like this."

11. **No `main()`.** This module is linked into a full ROM; there's
    no process entry point beyond the real entry point the analysis
    step already identified.

## Output format

Respond with a **single JSON object**, no prose, no markdown fences:

```json
{{
  "c_source": "string — the ENTIRE .c file contents, starting with the /* @source: ... */ header block and `#include \"gba.h\"`",
  "gba_h_additions": [
    {{
      "name": "REG_SOMETHING",
      "kind": "macro | typedef | extern | enum",
      "definition": "full C declaration or #define line",
      "reason": "why it's needed — short"
    }}
  ],
  "notes": "one short paragraph of anything noteworthy: ambiguity in the ASM, assumptions made, data shapes guessed at. Empty string if nothing."
}}
```

`c_source` is written verbatim to `output/c_view/<same-basename>.c`.
`gba_h_additions` entries are appended to `output/c_view/gba.h` only
if that exact `name` isn't already present. Use this field _sparingly_
— if a REG is already in `gba.h`, don't re-add it.
