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

import os

# Prefer a Gemini adapter when the GEMINI_API_KEY is present so users
# can supply Gemini credentials instead of Anthropic. Fall back to the
# installed Anthropic SDK if the adapter or key are not present.
if os.environ.get("GEMINI_API_KEY"):
    try:
        from gemini_adapter import GeminiClient as Anthropic
    except Exception:
        from anthropic import Anthropic
else:
    from anthropic import Anthropic

HERE = Path(__file__).resolve().parent
SYSTEM_PROMPT_PATH = HERE / "CLAUDE.md"
PROMPT_TEMPLATE_PATH = HERE / "prompts" / "c_view.md"

MODEL = "claude-opus-4-7"
MAX_TOKENS = 65_536


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

    def translate_one(self, mod: dict, annotated_path: Path, ghidra_hint: str = "") -> CViewResult:
        source = annotated_path.read_text()
        variables_md = self.variables_md_path.read_text()

        user_prompt = self.template.format(
            module_path=mod["path"],
            addr_start=mod["addr_start"],
            addr_end=mod["addr_end"],
            kind=mod["kind"],
            line_count=len(source.splitlines()),
            variables_md=variables_md,
            module_source=source,
        )

        if ghidra_hint and ghidra_hint.strip():
            user_prompt += (
                "\n\n## Ghidra decompiler output (use as a starting point — correct it where needed)\n\n"
                "```c\n"
                + ghidra_hint.strip()
                + "\n```\n"
                "\nUse the Ghidra output above as structural guidance. "
                "Fix types, variable names, register macros, and BIOS calls "
                "as per the rules above. Do not copy Ghidra variable names "
                "verbatim if they conflict with variables.md.\n"
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


_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)


def _fix_json_strings(raw: str) -> str:
    """Attempt to repair unescaped control chars inside JSON string values.

    LLMs often return JSON where the string values contain literal
    newlines, tabs, or unescaped backslashes.  This helper walks the
    raw text and escapes them so ``json.loads`` can succeed.
    """
    out: list[str] = []
    in_string = False
    i = 0
    while i < len(raw):
        ch = raw[i]
        if not in_string:
            if ch == '"':
                in_string = True
            out.append(ch)
            i += 1
        else:
            if ch == '\\' and i + 1 < len(raw):
                # already-escaped sequence — keep both chars
                out.append(ch)
                out.append(raw[i + 1])
                i += 2
            elif ch == '"':
                in_string = False
                out.append(ch)
                i += 1
            elif ch == '\n':
                out.append('\\n')
                i += 1
            elif ch == '\r':
                out.append('\\r')
                i += 1
            elif ch == '\t':
                out.append('\\t')
                i += 1
            else:
                out.append(ch)
                i += 1
    return "".join(out)


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    # strip markdown code fences
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)

    # 1) Try direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2) Extract outermost { … } and try
    m = _JSON_OBJ.search(raw)
    if m:
        fragment = m.group(0)
        try:
            return json.loads(fragment)
        except json.JSONDecodeError:
            pass
        # 3) Repair unescaped control characters and retry
        try:
            return json.loads(_fix_json_strings(fragment))
        except json.JSONDecodeError:
            pass

    # 4) Try to salvage a truncated JSON response (max_tokens hit).
    #    Look for "c_source": "..." and extract what we can.
    salvage = re.search(r'"c_source"\s*:\s*"', raw)
    if salvage:
        start = salvage.end()
        # walk the string collecting chars, handling escapes
        chars: list[str] = []
        i = start
        while i < len(raw):
            ch = raw[i]
            if ch == '\\' and i + 1 < len(raw):
                esc = raw[i + 1]
                if esc == 'n': chars.append('\n')
                elif esc == 't': chars.append('\t')
                elif esc == 'r': chars.append('\r')
                elif esc == '"': chars.append('"')
                elif esc == '\\': chars.append('\\')
                else: chars.append(ch + esc)
                i += 2
            elif ch == '"':
                break  # proper end of string
            else:
                chars.append(ch)
                i += 1
        c_source = ''.join(chars)
        if c_source.strip():
            return {
                "c_source": c_source,
                "gba_h_additions": [],
                "notes": "(salvaged from truncated JSON)",
            }

    # 5) Last resort: treat the entire response as raw C source.
    return {
        "c_source": raw,
        "gba_h_additions": [],
        "notes": "(raw LLM output — JSON extraction failed; treated as plain C)",
    }


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
