"""Step 4: translate annotated ASM modules into C via Claude.

Input:  output/annotated/*.s  +  output/variables.md
Output: output/c_view/*.c     +  output/c_view/gba.h (accumulated)

Read the PROCESS.md §4 and §10 before making changes. The resulting C
must compile with `arm-none-eabi-gcc -mthumb -Os -nostdlib
-ffreestanding`, because it's the edit surface for the surgical
recompile path in §11b.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from anthropic import Anthropic

HERE = Path(__file__).resolve().parent
SYSTEM_PROMPT_PATH = HERE / "CLAUDE.md"
PROMPT_TEMPLATE_PATH = HERE / "prompts" / "c_view.md"

MODEL = "claude-opus-4-7"
MAX_TOKENS = 16_000


@dataclass
class CViewResult:
    module_index: int
    c_path: Path
    notes: str
    header_additions: int


class CTranslator:
    def __init__(self, output_dir: Path, *, model: str = MODEL) -> None:
        self.output_dir = output_dir
        self.annotated_dir = output_dir / "annotated"
        self.per_module_dir = output_dir / "per_module"
        self.ghidra_c_dir = output_dir / "ghidra_c"
        self.c_dir = output_dir / "c_view"
        self.c_dir.mkdir(parents=True, exist_ok=True)
        self.gba_h_path = self.c_dir / "gba.h"
        self.variables_md_path = output_dir / "variables.md"
        self.progress_path = output_dir / ".progress_c.json"

        self.client = Anthropic()
        self.model = model
        self.system_prompt = SYSTEM_PROMPT_PATH.read_text()
        self.template = PROMPT_TEMPLATE_PATH.read_text()

        if not self.gba_h_path.exists():
            self.gba_h_path.write_text(_GBA_H_SEED)

    def translate_all(
        self,
        modules: list[dict],
        *,
        force: bool = False,
        skip_data: bool = True,
    ) -> list[CViewResult]:
        progress = self._load_progress()
        results: list[CViewResult] = []

        for mod in modules:
            idx = mod["index"]
            if skip_data and mod.get("kind") == "data":
                progress.setdefault("skipped_data", []).append(idx)
                continue
            if not force and idx in progress["completed"]:
                continue
            annotated = self.annotated_dir / mod["path"]
            if not annotated.is_file():
                progress["skipped"][str(idx)] = f"no annotated file at {annotated}"
                self._save_progress(progress)
                continue
            try:
                result = self.translate_one(mod, annotated)
            except Exception as exc:
                progress["errors"][str(idx)] = f"{type(exc).__name__}: {exc}"
                self._save_progress(progress)
                raise
            results.append(result)
            progress["completed"].append(idx)
            progress["errors"].pop(str(idx), None)
            self._save_progress(progress)

        return results

    def translate_one(self, mod: dict, annotated_path: Path) -> CViewResult:
        source = annotated_path.read_text()
        glossary = self.variables_md_path.read_text() if self.variables_md_path.is_file() else ""

        stem = Path(mod["path"]).stem
        dossier_path = self.per_module_dir / f"{stem}.md"
        dossier_md = dossier_path.read_text() if dossier_path.is_file() else "(no analysis dossier found)"

        ghidra_c = _load_ghidra_c(self.ghidra_c_dir, mod["addr_start"], mod["addr_end"])

        user_prompt = self.template.format(
            module_path=mod["path"],
            addr_start=mod["addr_start"],
            addr_end=mod["addr_end"],
            kind=mod["kind"],
            line_count=len(source.splitlines()),
            glossary=glossary,
            dossier_md=dossier_md,
            ghidra_c=ghidra_c,
            module_source=source,
        )

        message = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": self.system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw = "".join(
            block.text for block in message.content if block.type == "text"
        )
        parsed = _extract_json(raw)

        c_source = parsed.get("c_source", "")
        if not c_source.strip():
            raise RuntimeError(f"module {mod['index']}: empty c_source from model")

        c_path = self.c_dir / mod["path"].replace(".s", ".c")
        c_path.write_text(c_source)

        additions = parsed.get("gba_h_additions", []) or []
        added = self._merge_gba_h(additions)

        return CViewResult(
            module_index=mod["index"],
            c_path=c_path,
            notes=parsed.get("notes", "") or "",
            header_additions=added,
        )

    def _merge_gba_h(self, additions: list[dict]) -> int:
        if not additions:
            return 0
        existing = self.gba_h_path.read_text()
        appended: list[str] = []
        for add in additions:
            name = (add.get("name") or "").strip()
            definition = (add.get("definition") or "").strip()
            if not name or not definition:
                continue
            if re.search(rf"\b{re.escape(name)}\b", existing):
                continue
            block = [
                f"/* {add.get('reason', '').strip() or 'added by translate_to_c'} */",
                definition,
                "",
            ]
            appended.append("\n".join(block))
            existing += "\n" + block[0] + "\n" + block[1] + "\n"
        if appended:
            with self.gba_h_path.open("a") as fh:
                fh.write("\n/* --- appended by translate_to_c --- */\n")
                fh.write("\n".join(appended))
        return len(appended)

    def _load_progress(self) -> dict:
        if self.progress_path.exists():
            return json.loads(self.progress_path.read_text())
        return {"completed": [], "errors": {}, "skipped": {}}

    def _save_progress(self, progress: dict) -> None:
        self.progress_path.write_text(json.dumps(progress, indent=2) + "\n")


_GHIDRA_ADDR_RE = re.compile(r"^([0-9a-fA-F]{8})\.c$")


def _load_ghidra_c(ghidra_c_dir: Path, addr_start: str, addr_end: str) -> str:
    """Concatenate every ghidra_c/<addr>.c whose address is in this module.

    Files are named by the function's entry address in hex, e.g.
    `080012c4.c`. Returns a single string with a header per function,
    or a short placeholder if Ghidra output is missing.
    """
    if not ghidra_c_dir.is_dir():
        return "(no ghidra_c/ directory — run ghidra.py first, or skip if decompiler isn't installed)"
    try:
        lo = int(addr_start, 16)
        hi = int(addr_end, 16)
    except ValueError:
        return "(could not parse module address range)"

    chunks: list[str] = []
    for p in sorted(ghidra_c_dir.glob("*.c")):
        m = _GHIDRA_ADDR_RE.match(p.name)
        if not m:
            continue
        addr = int(m.group(1), 16)
        if lo <= addr <= hi:
            chunks.append(f"/* ---- 0x{addr:08X}  ({p.name}) ---- */\n{p.read_text()}")
    if not chunks:
        return "(no ghidra functions in this module's address range)"
    return "\n\n".join(chunks)


_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = _JSON_OBJ.search(raw)
        if not m:
            raise
        return json.loads(m.group(0))


_GBA_H_SEED = """/* gba.h — shared definitions for the C view.
 *
 * Written once by translate_to_c.py. Additions are appended by the
 * translator when it encounters a register or wrapper not listed here.
 * Hand-edit freely; the translator only _adds_, never rewrites.
 */
#ifndef GBA_CONVERT_GBA_H
#define GBA_CONVERT_GBA_H

#include <stdint.h>

typedef uint8_t  u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef int8_t   s8;
typedef int16_t  s16;
typedef int32_t  s32;

/* Memory region bases. */
#define BIOS_BASE     0x00000000u
#define EWRAM_BASE    0x02000000u
#define IWRAM_BASE    0x03000000u
#define IO_BASE       0x04000000u
#define PALETTE_BASE  0x05000000u
#define VRAM_BASE     0x06000000u
#define OAM_BASE      0x07000000u
#define ROM_BASE      0x08000000u
#define SRAM_BASE     0x0E000000u

/* LCD / display. */
#define REG_DISPCNT   (*(volatile u16*)(IO_BASE + 0x000))
#define REG_DISPSTAT  (*(volatile u16*)(IO_BASE + 0x004))
#define REG_VCOUNT    (*(volatile u16*)(IO_BASE + 0x006))
#define REG_BG0CNT    (*(volatile u16*)(IO_BASE + 0x008))
#define REG_BG1CNT    (*(volatile u16*)(IO_BASE + 0x00A))
#define REG_BG2CNT    (*(volatile u16*)(IO_BASE + 0x00C))
#define REG_BG3CNT    (*(volatile u16*)(IO_BASE + 0x00E))

/* Input. */
#define REG_KEYINPUT  (*(volatile u16*)(IO_BASE + 0x130))
#define REG_KEYCNT    (*(volatile u16*)(IO_BASE + 0x132))

/* Interrupts. */
#define REG_IE        (*(volatile u16*)(IO_BASE + 0x200))
#define REG_IF        (*(volatile u16*)(IO_BASE + 0x202))
#define REG_IME       (*(volatile u16*)(IO_BASE + 0x208))

/* DMA source pointers (high halves of the 32-bit src/dst regs). */
#define REG_DMA0SAD   (*(volatile u32*)(IO_BASE + 0x0B0))
#define REG_DMA1SAD   (*(volatile u32*)(IO_BASE + 0x0BC))
#define REG_DMA2SAD   (*(volatile u32*)(IO_BASE + 0x0C8))
#define REG_DMA3SAD   (*(volatile u32*)(IO_BASE + 0x0D4))

/* Timer 0 (others follow at +4/+8/+C). */
#define REG_TM0CNT_L  (*(volatile u16*)(IO_BASE + 0x100))

/* BIOS SWI wrappers. The actual SWI lives in ROM BIOS; the linker
 * resolves these via the usual `ldr r*, =<addr>` pattern plus the
 * GCC attribute below. */
#define SWI_ATTR __attribute__((long_call, noinline))

SWI_ATTR void bios_soft_reset(void);
SWI_ATTR void bios_halt(void);
SWI_ATTR void bios_vblank_wait(void);         /* swi 0x05 */
SWI_ATTR s32  bios_div(s32 num, s32 denom);   /* swi 0x06, returns quotient */
SWI_ATTR s32  bios_sqrt(u32 x);               /* swi 0x08 */
SWI_ATTR void bios_cpu_set(const void *src, void *dst, u32 mode); /* 0x0B */
SWI_ATTR void bios_cpu_fast_set(const void *src, void *dst, u32 mode); /* 0x0C */
SWI_ATTR void bios_lz77_uncomp_wram(const void *src, void *dst);  /* 0x11 */
SWI_ATTR void bios_lz77_uncomp_vram(const void *src, void *dst);  /* 0x12 */

#endif /* GBA_CONVERT_GBA_H */
"""
