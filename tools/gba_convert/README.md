# gba_convert

A four-step pipeline that takes a GBA ROM and produces:

1. An annotated, re-assemblable disassembly.
2. A memory / variable map (`variables.md`).
3. A C translation of each module (editable, compilable via
   `arm-none-eabi-gcc` — the edit surface for surgical ROM mods).

Future scripts (`recompile.py`, `rebuild.py`) close the loop:
edit the C, splice the recompiled function back into the disassembly,
and produce a new `.gba` that differs from the original by exactly
the bytes you changed.

See [PROCESS.md](PROCESS.md) for the full design doc, including why
C (not Python) for the intermediate representation.

---

## Quick start

```sh
cd tools/gba_convert

# 1. Set up a venv (Luvdis + Anthropic SDK)
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ../../luvdis   # the bundled Luvdis checkout

# 2. Point at your ROM
export ANTHROPIC_API_KEY=sk-...
python pipeline.py path/to/your_rom.gba
```

Output lands in `output/`:

```text
output/
├── rom.s                ← raw Luvdis disassembly
├── rom.info.txt         ← ROM hash + detected title
├── rom.hash.txt
├── modules/
│   ├── _index.json
│   └── mod_0000_08000000.s ...
├── annotated/
│   └── mod_0000_08000000.s ...     ← ASM + @ comments (step 3)
├── c_view/
│   ├── gba.h                        ← shared C defs (REG_*, u8/u16/u32, SWI wrappers)
│   └── mod_0000_08000000.c ...     ← C translation (step 4)
├── variables.md         ← growing memory / function map
├── functions.cfg        ← Luvdis config of named functions
├── .progress.json       ← resumable analyze state
└── .progress_c.json     ← resumable C-view state
```

---

## Running individual steps

```sh
python pipeline.py rom.gba --only disasm           # just re-run Luvdis
python pipeline.py rom.gba --only split            # just re-chunk rom.s
python pipeline.py rom.gba --only analyze          # step 3 only (ASM annotate)
python pipeline.py rom.gba --only cview            # step 4 only (→ C)
python pipeline.py rom.gba --skip-analyze          # disasm + split, no LLM
python pipeline.py rom.gba --skip-cview            # disasm + split + analyze
python pipeline.py rom.gba --only cview --force    # redo completed modules
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
  a first cheap pass; switch to Opus for real analysis.

---

## Changing a ROM

If you run `pipeline.py` against a different ROM than last time, the
existing `output/` is auto-archived to `output.<shortsha>/` so previous
runs aren't clobbered.

---

## What's not included (yet)

- **`recompile.py`** — compile an edited C module via
  `arm-none-eabi-gcc` and splice the fresh bytes into the disassembly.
  See [PROCESS.md §11b](PROCESS.md).
- **`rebuild.py`** — `arm-none-eabi-as` / `ld` / `objcopy` / `gbafix`
  to produce a `.gba` from the (possibly spliced) disassembly. See
  [PROCESS.md §11a](PROCESS.md).
- Graphics / tileset / text extraction — separate tools.

---

## Files

| File                         | Purpose                                    |
|------------------------------|--------------------------------------------|
| `pipeline.py`                | CLI orchestrator                           |
| `disassemble.py`             | Step 1 — wraps local Luvdis                |
| `split_modules.py`           | Step 2 — chunks rom.s                      |
| `analyze.py`                 | Step 3 — ASM annotation via Claude         |
| `translate_to_c.py`          | Step 4 — ASM → C via Claude                |
| `CLAUDE.md`                  | System prompt for analysis + C-view agents |
| `prompts/module_analysis.md` | Per-module prompt for step 3               |
| `prompts/c_view.md`          | Per-module prompt for step 4               |
| `PROCESS.md`                 | Full design doc                            |
