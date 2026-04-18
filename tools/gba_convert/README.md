# gba_convert

An end-to-end pipeline that takes a GBA ROM, produces a readable C view
of every function, lets you edit the C (in code or via natural language),
and splices the recompiled bytes back into a working `.gba`.

The output is a **controlled-delta ROM**: unchanged regions stay
byte-identical to the original; only the functions you touched change.

See [PROCESS.md](PROCESS.md) for the full design doc, including why
C (not Python) for the intermediate representation.

---

## Quick start

```sh
cd tools/gba_convert

# 1. Python deps (Luvdis + Anthropic SDK)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ../../luvdis   # the bundled Luvdis checkout

# 2. Analysis only (Stage A) — no ARM toolchain needed
export ANTHROPIC_API_KEY=sk-...
python pipeline.py path/to/your_rom.gba

# 3. (Optional) ARM toolchain, required only for edits + rebuild
brew install --cask gcc-arm-embedded     # macOS
brew install gbafix                       # optional, for header checksum

# 4. Edit + rebuild
python edit.py "bump max HP to 999"       # NL → edited/mod_*.c
python recompile.py                        # splice compiled bytes
python rebuild.py                          # → output/rebuilt.gba
```

See [Output artifacts](#output-artifacts) for the full `output/` tree.

---

## Pipeline overview

```mermaid
flowchart TD
    ROM["rom.gba"]:::io --> D[disassemble.py<br/><i>Luvdis</i>]
    D -->|rom.s| S[split_modules.py]
    S -->|modules/mod_*.s| A[analyze.py<br/><i>Claude</i>]
    A -->|annotated/mod_*.s<br/>variables.md<br/>functions.cfg| T[translate_to_c.py<br/><i>Claude</i>]
    T -->|c_view/mod_*.c<br/>c_view/gba.h| ED[edit.py<br/><i>Claude — NL</i>]
    T -.->|hand-edit| MAN["edited/mod_*.c<br/>(direct code edit)"]:::io
    ED -->|edited/mod_*.c| RC[recompile.py<br/><i>gcc + splice</i>]
    MAN --> RC
    RC -->|recompiled/mod_*.s| RB[rebuild.py<br/><i>as + ld + objcopy + gbafix</i>]
    RB --> OUT["rebuilt.gba"]:::io

    classDef io fill:#eef,stroke:#339,stroke-width:2px;
```

The arrows show data flow. Each box is a script; its label's italics
note the external tool or model it drives. Everything left of the
`c_view` boundary is **read-only analysis**; everything right of it is
**surgical modification**.

---

## Components

### Stage A — read-only analysis

| Script | Input | Output | Role |
|---|---|---|---|
| [disassemble.py](disassemble.py) | `rom.gba` | `output/rom.s`, `rom.hash.txt`, `rom.info.txt` | Shells out to [Luvdis](../../luvdis) to produce a single ARMv4 / THUMB disassembly. Records the SHA-1 so later steps can verify round-trip. |
| [split_modules.py](split_modules.py) | `output/rom.s` | `output/modules/mod_NNNN_ADDR.s` + `_index.json` | Cuts `rom.s` at function boundaries (`thumb_func_start`, `arm_func_start`) and at size-based fallback points. Each module is independently analyzable and labels its address range + `kind` (`code` / `data` / `mixed`). |
| [analyze.py](analyze.py) | `modules/*.s` + `CLAUDE.md` + `prompts/module_analysis.md` | `annotated/*.s`, accumulating `variables.md` + `functions.cfg`, `.progress.json` | One Claude call per module. Adds `@` comments to the assembly, promotes confidently-named functions/globals/registers into `variables.md`, and writes high-confidence function addresses into `functions.cfg` (a Luvdis seed file). `kind == "data"` modules are skipped by default. |
| [translate_to_c.py](translate_to_c.py) | `annotated/*.s` + `variables.md` + `prompts/c_view.md` | `c_view/*.c`, accumulating `c_view/gba.h`, `.progress_c.json` | One Claude call per annotated module. Produces C that compiles with `arm-none-eabi-gcc -mthumb -Os -nostdlib -ffreestanding`. The C uses types that preserve LDR/STR widths, names from `variables.md`, `REG_*` macros from `gba.h`, and `bios_*` SWI wrappers. Inline `__asm__` is used wherever C can't express ARM semantics. |

### Stage B — editing

| Script | Input | Output | Role |
|---|---|---|---|
| [edit.py](edit.py) | `"bump max HP to 999"` + `c_view/*.c` + `variables.md` | `edited/mod_*.c` | Two-stage Claude call. Stage 1 picks which module the instruction targets (from `variables.md` + the module list). Stage 2 rewrites that module under hard constraints (signatures immutable, `#include "gba.h"` only, must fit in original byte span). If `arm-none-eabi-gcc` rejects the output, stderr + the failed source are fed back and Claude retries up to 3 times. |
| *(alternative)* direct edit | `cp c_view/X.c edited/X.c` + your editor | `edited/mod_*.c` | Skip `edit.py` entirely and write the C yourself. Anything in `edited/` that differs from its `c_view/` twin is picked up by `recompile.py`. |

### Stage C — round trip

| Script | Input | Output | Role |
|---|---|---|---|
| [recompile.py](recompile.py) | `edited/*.c` (diffed against `c_view/*.c`), `annotated/*.s` | `recompiled/mod_*.s`, `build_c/*.o`, `build_c/*.bin` | For each edited module: `gcc -c` → `objcopy --only-section=.text` → raw bytes. Locates the edited function's byte span in the annotated `.s`, checks the compiled length against the original (**fails if over-size** — the edit has to fit). Pads with THUMB nops (`0xC046`) if smaller. Splices those bytes back in, leaving the rest of the module byte-identical. |
| [rebuild.py](rebuild.py) | `annotated/*.s` (or `recompiled/*.s` when present), [linker.ld](linker.ld) | `build/*.o`, `build/rebuilt.elf`, `output/rebuilt.gba` | `arm-none-eabi-as` each `.s` → `.o`, `ld -T linker.ld` → `elf`, `objcopy -O binary` → raw ROM, `gbafix` → correct header checksum, then SHA-1 verify against `rom.hash.txt`. An unspliced rebuild should match the original hash exactly — any drift is a Luvdis or toolchain bug. A spliced rebuild differs at exactly the edited byte spans. |

### Support files

| File | Role |
|---|---|
| [CLAUDE.md](CLAUDE.md) | Shared system prompt for all Claude calls. Contains the GBA memory map, I/O register table, SWI table, calling convention, and style rules. Cached via prompt caching. |
| [prompts/module_analysis.md](prompts/module_analysis.md) | Per-module user prompt for `analyze.py`. |
| [prompts/c_view.md](prompts/c_view.md) | Per-module user prompt for `translate_to_c.py`. |
| [prompts/edit_target.md](prompts/edit_target.md) | Stage-1 user prompt for `edit.py` (target selection). |
| [prompts/edit_apply.md](prompts/edit_apply.md) | Stage-2 user prompt for `edit.py` (apply + retry). |
| [linker.ld](linker.ld) | Minimal GBA linker script. Places all module `.text` at `0x08000000` in the declaration order Luvdis emitted — which matches the original ROM layout. |
| [PROCESS.md](PROCESS.md) | Full design doc (why C, surgical-splice model, §11a/§11b invariants). |

### Output artifacts

```text
output/
├── rom.s                   ← raw Luvdis disassembly
├── rom.info.txt            ← detected ROM title
├── rom.hash.txt            ← SHA-1 of the original ROM
├── modules/
│   ├── _index.json         ← manifest: path, addr_start, addr_end, kind
│   └── mod_NNNN_ADDR.s
├── annotated/              ← step 3: ASM + @ comments
│   └── mod_NNNN_ADDR.s
├── c_view/                 ← step 4: C edit surface
│   ├── gba.h               ← shared defs (REG_*, u8/u16/u32, SWI wrappers)
│   └── mod_NNNN_ADDR.c
├── edited/                 ← YOUR edits (from edit.py or hand)
│   └── mod_NNNN_ADDR.c
├── recompiled/             ← recompile.py output (splice target)
│   └── mod_NNNN_ADDR.s
├── build_c/                ← gcc intermediates (per-module .o / .bin)
├── build/                  ← as/ld intermediates (rebuilt.elf)
├── rebuilt.gba             ← final rebuild
├── variables.md            ← accumulating memory / function map
├── functions.cfg           ← Luvdis config (high-confidence functions)
├── .progress.json          ← resumable analyze state
└── .progress_c.json        ← resumable C-view state
```

---

## Running individual steps

```sh
# --- Stage A: analysis ---
python pipeline.py rom.gba --only disasm           # just re-run Luvdis
python pipeline.py rom.gba --only split            # just re-chunk rom.s
python pipeline.py rom.gba --only analyze          # step 3 only (ASM annotate)
python pipeline.py rom.gba --only cview            # step 4 only (→ C)
python pipeline.py rom.gba --skip-analyze          # disasm + split, no LLM
python pipeline.py rom.gba --skip-cview            # disasm + split + analyze
python pipeline.py rom.gba --only cview --force    # redo completed modules
python pipeline.py rom.gba --only analyze --limit 3  # smoke-test on 3 mods

# --- Stage B: edits ---
python edit.py "bump max HP to 999"                # NL → Claude picks target
python edit.py --module mod_0017_080A1B30 "bump HP"  # NL on a known module

# --- Stage C: round trip ---
python recompile.py                                # compile edits + splice
python rebuild.py                                  # → rebuilt.gba
python rebuild.py --no-splice                      # rebuild from annotated/ only
```

Each LLM step is idempotent — `.progress.json` tracks step 3, and
`.progress_c.json` tracks step 4. Delete the relevant file (or pass
`--force`) to redo that pass.

---

## Tuning

- `--default-mode BYTE|THUMB|WORD` — Luvdis's fallback for unknown
  addresses. `BYTE` is safe; `THUMB` gives cleaner output on code-heavy
  ROMs but can mis-disassemble data.
- `--max-lines 1500` — max lines per module chunk. Smaller = more LLM
  calls but better focus; larger = cheaper but risks hitting context
  limits on complex modules.
- `--model claude-opus-4-7` — any Anthropic model ID. Haiku is fine for
  a cheap triage pass on `analyze`; switch to Opus for `cview` and edits.
- `--include-data` — by default, `kind == "data"` modules are skipped in
  `analyze` and `cview` (no point sending `.byte` dumps to an LLM). Pass
  this to process them anyway.
- `--limit N` — process at most N modules in each LLM step. Useful for
  smoke-testing the pipeline cheaply before a full run.

---

## Changing a ROM

If you run `pipeline.py` against a different ROM than last time, the
existing `output/` is auto-archived to `output.<shortsha>/` so previous
runs aren't clobbered.

---

## What's not included

- Graphics / tileset / text extraction — separate tools.
- Multi-function-per-module edits — `recompile.py` assumes one edited
  function per module. Editing two in the same module works only if
  their combined size still fits the original span.
- Relocation — if your edit doesn't fit in the original byte span,
  `recompile.py` fails. There is no out-of-line trampoline path yet.
- `.rodata` splicing — only `.text` is currently swapped back in. If an
  edit introduces new read-only data, it'll be dropped at the
  `objcopy --only-section=.text` step.

---

## Files

| File                         | Purpose                                                |
|------------------------------|--------------------------------------------------------|
| `pipeline.py`                | CLI orchestrator (Stage A)                             |
| `disassemble.py`             | Step 1 — wraps local Luvdis                            |
| `split_modules.py`           | Step 2 — chunks rom.s                                  |
| `analyze.py`                 | Step 3 — ASM annotation via Claude                     |
| `translate_to_c.py`          | Step 4 — ASM → C via Claude                            |
| `edit.py`                    | Stage B — natural-language edits via Claude + retry    |
| `recompile.py`               | Stage C — compile edited C + splice bytes              |
| `rebuild.py`                 | Stage C — `as` + `ld` + `objcopy` + `gbafix` → `.gba`  |
| `linker.ld`                  | GBA linker script for `rebuild.py`                     |
| `CLAUDE.md`                  | System prompt (shared across all Claude calls)         |
| `prompts/module_analysis.md` | Per-module prompt for step 3                           |
| `prompts/c_view.md`          | Per-module prompt for step 4                           |
| `prompts/edit_target.md`     | Stage-1 prompt for `edit.py` (target selection)        |
| `prompts/edit_apply.md`      | Stage-2 prompt for `edit.py` (apply + retry)           |
| `PROCESS.md`                 | Full design doc                                        |
