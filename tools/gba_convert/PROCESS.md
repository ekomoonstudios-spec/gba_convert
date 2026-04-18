# GBA ROM → Annotated Disassembly + C View Pipeline

End-to-end process for taking a raw GBA ROM, disassembling it with Luvdis,
splitting the result into LLM-sized chunks, driving an agentic analysis
pass that produces human-readable annotations + a memory/variable map,
and translating each annotated module into **C pseudocode** that is both
easier for an LLM to read and — crucially — editable + re-compilable via
`arm-none-eabi-gcc` for surgical ROM edits.

Two round-trippable loops:

- **ROM ↔ ASM** — Luvdis output reassembles to an identical ROM.
- **C → ASM → ROM** — only for _edited_ functions; the rest of the ROM
  stays verbatim from Luvdis. See §11.

We chose C over Python because:

1. C's semantics (fixed-width ints, raw pointers, no GC) match ARMv4
   almost 1:1. Python doesn't.
2. `arm-none-eabi-gcc` actually exists and compiles C to ARMv4-THUMB.
   No equivalent Python-to-ARMv4 compiler exists.
3. The original GBA games were (mostly) compiled _from_ C. Going back
   to C is reversing the original compilation direction, not inventing
   a new one.
4. LLM-driven edits in C are cheaper (fewer tokens per logical operation
   than ASM) and more reliable (LLMs reason about C far better than
   about register-level ARM).

---

## 1. Goals

Given a `*.gba` ROM, produce:

1. **`rom.s`** — raw Luvdis disassembly (full ROM).
2. **`modules/mod_<ADDR>.s`** — the same disassembly split into smaller logical
   files, each small enough to fit in a single LLM prompt (~1–2k lines).
3. **`annotated/mod_<ADDR>.s`** — per-module copy with inline comments:
   - BIOS/SWI call identification (`swi 0x6` → `Div`, etc.)
   - Likely function purpose ("reads joypad", "copies tiles to VRAM")
   - Register usage notes on entry/exit
4. **`variables.md`** — discovered globals, RAM layout, I/O-register usage,
   constants, and any function-name guesses promoted to canonical names.
5. **`functions.cfg`** — Luvdis config file of discovered/named functions,
   usable for re-disassembling with better labels.
6. **`c_view/mod_<ADDR>.c`** + **`c_view/gba.h`** — C translation of
   each annotated module. Function/variable names come from
   `variables.md`, so the C reads like ordinary code rather than
   register soup. This is also the **edit surface** for LLM-driven ROM
   modifications (§11).
7. **`rebuilt.gba`** (future) — a round-tripped ROM built from the
   original disassembly with any edited C functions surgically spliced
   in, verified by hash. _Implemented in `rebuild.py` + `recompile.py`;
   see §11._

Items 4, 5, and 6 are the comprehension deliverables. Item 7 closes the
ROM-editing loop — edits land in C, get compiled back to ASM, and are
spliced into the full disassembly for reassembly.

---

## 2. Prerequisites

- Python 3.8+ (Luvdis requires 3.6+; we use modern typing).
- Local Luvdis checkout at `../../luvdis/` (already present in this repo).
  We invoke it via `python -m luvdis` with `PYTHONPATH` pointed at that dir —
  no `pip install` required.
- A GBA ROM file (user-provided; not committed to the repo).
- LLM access — **open decision**, see §7.

Dependencies for the orchestrator itself are pinned in
`tools/gba_convert/requirements.txt` (click, tqdm inherited from Luvdis;
`anthropic` only if we go the API route).

---

## 3. Directory Layout

```text
tools/gba_convert/
├── PROCESS.md              ← this file
├── CLAUDE.md               ← system prompt for analysis + C-view agents
├── README.md               ← quick-start
├── requirements.txt
├── pipeline.py             ← top-level orchestrator (CLI entrypoint)
├── disassemble.py          ← step 1: run Luvdis
├── split_modules.py        ← step 2: chunk rom.s into modules/
├── analyze.py              ← step 3: annotate ASM, build variables.md
├── translate_to_c.py       ← step 4: annotated ASM → C
├── recompile.py            ← (future) edited C → fresh ASM → splice
├── rebuild.py              ← (future) rebuild .gba from spliced ASM
├── prompts/
│   ├── module_analysis.md  ← prompt for step 3
│   └── c_view.md           ← prompt for step 4
└── output/
    ├── rom.s
    ├── rom.info.txt            ← `luvdis info` output (hash, detected title)
    ├── modules/
    │   └── mod_08000000.s
    ├── annotated/
    │   └── mod_08000000.s      ← ASM with @-comments (step 3 output)
    ├── c_view/
    │   ├── gba.h               ← shared defs (REG_*, u8/u16/u32, SWI wrappers)
    │   └── mod_08000000.c      ← C translation (step 4 output)
    ├── variables.md            ← accumulated across runs
    ├── functions.cfg           ← accumulated across runs
    ├── edited/
    │   └── mod_08000000.c      ← (future) user-edited copies
    ├── recompiled/
    │   └── mod_08000000.s      ← (future) gcc output, spliced into annotated/
    └── rebuilt.gba             ← (future) output of rebuild.py
```

`output/` is gitignored. The ROM itself must **never** be committed.

---

## 4. Pipeline Steps

### Step 1 — Disassemble (`disassemble.py`)

**Input:** `<rom>.gba`
**Output:** `output/rom.s`, `output/rom.info.txt`, `output/functions.cfg` (seed)

Actions:
1. Run `luvdis info <rom>` → capture ROM hash + detected title.
2. Run `luvdis disasm <rom> -o output/rom.s -co output/functions.cfg`
   with `--default-mode BYTE` (safe default; can be overridden per-ROM).
3. If a previous `output/functions.cfg` exists, pass it back in via `-c`
   so that human-named functions survive re-runs.

We shell out to Luvdis via `subprocess` rather than importing it, so that
the Luvdis codebase stays untouched and can be swapped for a pip install
later without code changes.

### Step 2 — Split into Modules (`split_modules.py`)

**Input:** `output/rom.s`
**Output:** `output/modules/mod_<HEX_ADDR>.s`

Luvdis emits `.s` with interleaved code + data. The splitter walks the file
and cuts on these boundaries:

- Every `thumb_func`/`arm_func`/`non_word_aligned_thumb_func` directive.
- Large `.byte`/`.word` data blocks (≥ N bytes) — these become their own
  "data-only" modules tagged `mod_<ADDR>.data.s`.
- Size ceiling: if an accumulating chunk exceeds `--max-lines` (default
  1500), emit it at the next function boundary.

Each output file carries a header comment:

```
@ Module: mod_080002A0.s
@ Range:  0x080002A0 – 0x08000C14
@ Source: output/rom.s lines 1204–2739
@ Kind:   code | data | mixed
```

This header is what the analysis step keys on to know the memory range it's
looking at.

### Step 3 — Agentic Analysis (`analyze.py`)

**Input:** `output/modules/*.s` + `output/variables.md` (accumulating)
**Output:** `output/annotated/*.s`, updated `output/variables.md`,
            updated `output/functions.cfg`

For each module (in address order):

1. Load the module file + `CLAUDE.md` system prompt + current
   `variables.md` (as context — keeps previously-discovered names
   consistent).
2. Send to the LLM with the module-analysis prompt template.
3. Expect a structured response containing:
   - **annotated assembly** (original lines, with `@` comments added) —
     written to `output/annotated/mod_<ADDR>.s`.
   - **new variable / register / memory entries** — appended to
     `variables.md` under the appropriate section.
   - **function-name guesses** — merged into `functions.cfg` so that
     the next re-disassembly picks them up.
4. Rate-limit / checkpoint between modules so a failed run resumes
   from the last completed module (via a `.progress.json` sidecar).

Module ordering matters: we go low → high address so that early passes
(entry point, BIOS init, main loop) populate `variables.md` before
later game-logic modules consume it.

### Step 4 — C View (`translate_to_c.py`)

**Input:** `output/annotated/*.s` + `output/variables.md`
**Output:** `output/c_view/*.c`, `output/c_view/gba.h`

For each annotated module, produce a C file that reads the same
behaviour as the assembly but in a form that's cheap for an LLM to
reason about and — critically — compilable back to ARMv4-THUMB via
`arm-none-eabi-gcc`. The C view is the preferred edit surface: edits
made here can round-trip to a rebuilt ROM via §11.

Per module:

1. Load the annotated `.s` + `variables.md` + the C-view prompt.
2. Ask the LLM to translate using names from `variables.md`, so that
   `sub_08001234` becomes `update_player_input` (etc.).
3. Expect a C file that:
   - Starts with a header block tying it back to the source:
     `/* @source: annotated/mod_0003_0800E1FC.s  0x0800E1FC–... */`.
   - Uses `<stdint.h>` types (`uint8_t`, `int16_t`, `uint32_t`, …)
     so LDR/STR widths are explicit.
   - Uses the `REG_*` macros from `gba.h` (not raw addresses).
   - Uses the SWI wrappers (`bios_div`, `bios_vblank_wait`, …) from
     `gba.h` instead of inline `swi` asm.
   - Carries `// asm:` backrefs on each non-trivial block so the
     ASM↔C mapping is auditable.
4. Write to `output/c_view/<same-basename>.c`.
5. Checkpoint via `.progress_c.json` (separate from step 3 progress).

`gba.h` is written once on the first run (idempotent). It contains:

- Fixed-width typedefs (`u8`, `u16`, `u32`, `s8`, `s16`, `s32`).
- `REG_*` macros for the I/O registers enumerated in `CLAUDE.md`.
- BIOS SWI wrappers declared `extern` (the _definitions_ live in
  the original ROM's BIOS-call stubs, which we don't touch).
- Memory-region base address constants (`EWRAM_BASE`, `VRAM_BASE`, …).

Step 4 depends on step 3 output — if annotations are thin the C is
thin too. Run step 3 first, inspect `variables.md`, then translate.

**Constraint discipline.** The C must compile with
`arm-none-eabi-gcc -mthumb -Os -nostdlib -ffreestanding`. No libc, no
heap allocation, no floating point unless the annotated ASM used the
soft-float helpers. If an instruction has no clean C equivalent
(e.g. SMLAL with specific flag behaviour the surrounding code
depends on), emit a GCC inline-asm block rather than fabricate
wrong C.

---

## 5. `CLAUDE.md` (analysis agent instructions)

A separate file; not yet written. It will instruct the analysis agent to:

- Identify **GBA BIOS calls** (`swi` immediates 0x00–0x2A) by their known
  names and document the calling convention used.
- Recognise **memory-mapped I/O** accesses to `0x04000000–0x040003FE`
  (REG_DISPCNT, REG_KEYINPUT, DMA regs, timers, sound) and annotate.
- Recognise **memory regions** by address prefix: ROM (`0x08…`),
  EWRAM (`0x02…`), IWRAM (`0x03…`), VRAM (`0x06…`), OAM (`0x07…`),
  palette RAM (`0x05…`), save (`0x0E…`).
- Produce **stable function names** — same behaviour across modules must
  get the same name. Use `variables.md` as the source of truth.
- Prefer **short `@` comments** over verbose prose; assembly stays readable.
- Never invent addresses or symbol names that aren't derivable from the
  code shown.

---

## 6. Re-run Semantics

The pipeline is idempotent w.r.t. a given ROM hash:

- `output/rom.info.txt` is checked on every run. If the hash changes,
  `output/` is archived to `output.<old-hash>/` before proceeding.
- Step 2 is fully deterministic — same `rom.s` + same `--max-lines`
  produces the same modules.
- Step 3 is resumable via `.progress.json`; completed modules are
  skipped unless `--force` is passed.

This matters because step 3 is the expensive one (LLM calls), and
iterating on the prompt should not require re-running steps 1–2.

---

## 7. Open Decisions

**Blocker for implementation:**

1. **LLM invocation.** Two options:
   - **Anthropic API** (`anthropic` Python SDK) — needs
     `ANTHROPIC_API_KEY`, gives us structured streaming, caching, usage
     metrics. Better for batch pipelines.
   - **Claude Code CLI** — shell out to `claude -p "<prompt>"`. No key
     needed, but harder to parse structured output and to parallelise.

   _Recommendation:_ API. Prompt caching over `CLAUDE.md` +
   `variables.md` is a big win when processing hundreds of modules.

**Not blockers, but worth deciding:**

2. **Default-mode for Luvdis** (`BYTE` vs `THUMB`). `BYTE` is safer but
   produces huge data blocks; `THUMB` gets cleaner output on code-heavy
   ROMs. Start with `BYTE`, expose a CLI flag.
3. **Max lines per module.** Default 1500; tune after first real run.
4. **Parallelism.** Step 3 is embarrassingly parallel per-module, but
   `variables.md` is shared mutable state. Either serialize, or do two
   passes (pass 1: independent annotation; pass 2: reconciliation).

---

## 8. Out of Scope (for v1)

- Graphics/tileset extraction (separate tool).
- Script/text extraction (needs charmap knowledge per-game).
- Web UI / visualisation of the memory map.
- **Whole-ROM rebuild from C.** We only recompile _edited_ functions;
  the rest of the ROM comes from Luvdis's matching disassembly. See §10
  for why, and §11 for how the surgical splice works.

---

## 9. Success Criteria

**Milestone 1 — analysis works end-to-end.** Run on the provided ROM
and produce a `variables.md` that identifies:

- The entry point and `AgbMain`.
- The main game loop.
- Every BIOS SWI actually called.
- The I/O register writes that configure the display mode.

**Milestone 2 — C view works end-to-end.** For each annotated module,
produce a `c_view/*.c` that:

- Compiles with `arm-none-eabi-gcc -mthumb -Os -nostdlib -ffreestanding
  -c mod_XXXX.c`. (The result isn't expected to be byte-identical to
  the original; just "it compiles and produces code of a sane size".)
- References names from `variables.md` consistently.
- Carries `// asm:` backref comments on every non-trivial block.
- Compiles against the shared `c_view/gba.h`.

**Milestone 3 — unmodified ROM round-trip via ASM.** `python rebuild.py`
produces a `rebuilt.gba` whose SHA-1 matches `rom.hash.txt` exactly,
built from `annotated/*.s` alone (no C recompile). This proves the
assembly toolchain is wired correctly.

**Milestone 4 — surgical C edit round-trip.** Edit one function in
`edited/mod_XXXX.c`, run `recompile.py` to splice in the new bytes,
then `rebuild.py`. Result is a new ROM that:

- Differs from the original by _exactly_ the edited function's byte
  range (diffing `rebuilt.gba` against the original shows bytes changed
  only in that region).
- Boots on `mGBA` (or similar) and exhibits the edited behaviour.

Anything beyond these is iteration on the prompts.

---

## 10. Why the C view is editable, and what that means

C is an impedance match for ARMv4-THUMB in a way Python (or any
garbage-collected language) isn't:

| Concern            | ARMv4 / GBA                           | C                              | Python                |
|--------------------|---------------------------------------|--------------------------------|-----------------------|
| Integers           | Fixed 8/16/32-bit, wrap on overflow   | Same (`uint8_t` etc.)          | Arbitrary precision   |
| Memory model       | Raw pointers, MMIO                    | Same                           | GC'd objects          |
| Calling convention | AAPCS                                 | GCC emits AAPCS by default     | Name-based            |
| Control flow       | Cond flags, jumps, LDM/STM            | `if`/`for`, compiler-generated | `if`/`for`/exceptions |
| Compiler to ARMv4  | (n/a — is the target)                 | `arm-none-eabi-gcc`            | does not exist        |

Because `arm-none-eabi-gcc` compiles C to ARMv4-THUMB, **edited C can
round-trip to the ROM** — but with two important caveats:

1. **Not byte-identical.** GCC will compile a given C function to ASM
   that _does the same thing_ but differs in register allocation,
   instruction ordering, and sometimes peephole choices. So the bytes
   of a recompiled function won't match the original unless the
   original was compiled from the exact same C with the exact same
   toolchain version (rarely true for retail ROMs).
2. **We therefore only recompile _edited_ functions.** Unchanged
   regions of the ROM are taken verbatim from the Luvdis disassembly,
   which _is_ byte-identical. The rebuild process (§11) splices the
   recompiled edited-function bytes into the original byte stream,
   leaving everything else untouched.

This gives us a practical edit loop:

- Read a function in C (cheaper tokens, easier for LLMs to modify).
- Edit the C.
- `recompile.py` compiles only that function and splices the result
  back into `annotated/*.s`.
- `rebuild.py` produces a new `.gba`.

The untouched 99% of the ROM comes from the original disassembly, so
the only bytes that change are inside the edited function's range. If
the edited function's new compiled size _exceeds_ the original slot,
`recompile.py` fails loudly rather than silently overflowing — the
user has to either shrink the edit or provide a trampoline (§11).

The C view is allowed to _lose information_ that isn't load-bearing:
LDM ordering, register numbers, literal-pool layout. If the ASM has
semantics that C can't express cleanly (rare — specific CPSR flag
behaviour, interleaved loads for pipeline timing), the translator
drops to GCC inline `__asm__` rather than fabricating wrong C.

---

## 11. ROM round-trip — ASM alone + surgical C splice

Two future scripts close the loop: `rebuild.py` for the pure ASM round
trip, and `recompile.py` for the C-edit splice that sits in front of it.

### 11a. Pure ASM round trip (`rebuild.py`)

Luvdis's output is deliberately designed to reassemble to an identical
ROM. Pipeline:

1. `arm-none-eabi-as -mcpu=arm7tdmi -mthumb-interwork` — assemble each
   `.s` in `annotated/` → `.o`.
2. `arm-none-eabi-ld -T linker.ld` — link with a script anchored at
   `0x08000000`.
3. `arm-none-eabi-objcopy -O binary` — strip ELF → `rebuilt.gba`.
4. `gbafix rebuilt.gba` — recompute the ROM header checksum.
5. Compare SHA-1 of `rebuilt.gba` against `rom.hash.txt`. Identical
   hash = disassembly is matching.

Requires the ARM bare-metal toolchain:

- macOS: `brew install --cask gcc-arm-embedded` + `brew install gbafix`
- Linux: `apt install gcc-arm-none-eabi` (+ `gbafix` from pip/source).

This step is independent of step 4 — annotations are `@` comments and
don't affect the assembled bytes.

### 11b. Surgical C edit splice (`recompile.py`)

Takes edited C in `output/edited/mod_XXXX.c` and reflows it back into
`annotated/` without disturbing surrounding bytes:

1. Identify which functions in the C file differ from `c_view/` (the
   unedited translation). Only those are recompiled.
2. For each edited function `foo` at original address `0x080XXXXX`
   with original byte length `N`:
   1. Compile the one function:
      `arm-none-eabi-gcc -mthumb -Os -nostdlib -ffreestanding -c
      -o foo.o edited/mod_XXXX.c`.
   2. Extract `foo`'s section from the `.o` via `objdump` + `objcopy`.
   3. Compare the new byte length `N'` against the original `N`.
      - If `N' <= N`: pad the tail with NOPs to preserve the address
        map. Safe splice.
      - If `N' > N`: fail loudly. The user must either shrink the edit,
        bump to a larger slot manually, or introduce a trampoline
        (jump to a fresh region of ROM). v1 doesn't auto-trampoline —
        that's a whole bin-packing problem.
3. Write the patched ASM to `output/recompiled/mod_XXXX.s`. This is
   then picked up by `rebuild.py` instead of the original `annotated/`
   version for those modules.
4. Emit a diff report listing every changed function, its address
   range, and its new vs. old size.

The property we preserve: **bytes outside edited functions stay
byte-identical to the original ROM**. That's why the new ROM is a
controlled delta, not a recompile of the whole game.

Status of both scripts: not yet implemented. Stubs will be placed so
the directory layout is ready.
