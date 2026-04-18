"""Step 3: drive Claude over each module to produce annotated .s,
variables.md, and high-confidence entries in functions.cfg.

Uses the Anthropic API with prompt caching on the system prompt
(`CLAUDE.md`) + the accumulating `variables.md`, so per-module cost
stays low even across hundreds of modules.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from anthropic import Anthropic

HERE = Path(__file__).resolve().parent
SYSTEM_PROMPT_PATH = HERE / "CLAUDE.md"
PROMPT_TEMPLATE_PATH = HERE / "prompts" / "module_analysis.md"

MODEL = "claude-opus-4-7"
MAX_TOKENS = 16_000


@dataclass
class AnalysisResult:
    module_index: int
    annotated_path: Path
    functions_added: list[dict]
    globals_added: list[dict]
    io_writes: list[dict]
    constants: list[dict]
    notes: str


class Analyzer:
    def __init__(self, output_dir: Path, *, model: str = MODEL) -> None:
        self.output_dir = output_dir
        self.modules_dir = output_dir / "modules"
        self.annotated_dir = output_dir / "annotated"
        self.annotated_dir.mkdir(parents=True, exist_ok=True)
        self.variables_md_path = output_dir / "variables.md"
        self.functions_cfg_path = output_dir / "functions.cfg"
        self.progress_path = output_dir / ".progress.json"

        self.client = Anthropic()
        self.model = model
        self.system_prompt = SYSTEM_PROMPT_PATH.read_text()
        self.template = PROMPT_TEMPLATE_PATH.read_text()

        if not self.variables_md_path.exists():
            self.variables_md_path.write_text(_VARIABLES_TEMPLATE)

    def analyze_all(
        self,
        modules: list[dict],
        *,
        force: bool = False,
    ) -> list[AnalysisResult]:
        progress = self._load_progress()
        results: list[AnalysisResult] = []

        for mod in modules:
            idx = mod["index"]
            if not force and idx in progress["completed"]:
                continue
            try:
                result = self.analyze_one(mod)
            except Exception as exc:  # noqa: BLE001 — surface + continue
                progress["errors"][str(idx)] = f"{type(exc).__name__}: {exc}"
                self._save_progress(progress)
                raise
            results.append(result)
            progress["completed"].append(idx)
            progress["errors"].pop(str(idx), None)
            self._save_progress(progress)

        return results

    def analyze_one(self, mod: dict) -> AnalysisResult:
        module_path = self.modules_dir / mod["path"]
        source = module_path.read_text()
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

        annotated_path = self.annotated_dir / mod["path"]
        annotated_path.write_text(parsed.get("annotated_source", source))

        functions = parsed.get("functions", []) or []
        globals_ = parsed.get("globals", []) or []
        io_writes = parsed.get("io_writes", []) or []
        constants = parsed.get("constants", []) or []
        notes = parsed.get("notes", "") or ""

        self._append_variables(mod, functions, globals_, io_writes, constants, notes)
        self._append_functions_cfg(functions)

        return AnalysisResult(
            module_index=mod["index"],
            annotated_path=annotated_path,
            functions_added=functions,
            globals_added=globals_,
            io_writes=io_writes,
            constants=constants,
            notes=notes,
        )

    def _append_variables(
        self,
        mod: dict,
        functions: list[dict],
        globals_: list[dict],
        io_writes: list[dict],
        constants: list[dict],
        notes: str,
    ) -> None:
        if not any((functions, globals_, io_writes, constants, notes.strip())):
            return
        block = [f"\n## Module `{mod['path']}`  ({mod['addr_start']}–{mod['addr_end']})\n"]
        if functions:
            block.append("### Functions")
            for f in functions:
                block.append(
                    f"- **{f.get('name','?')}** @ `{f.get('address','?')}` "
                    f"({f.get('mode','?')}) — {f.get('summary','')}"
                )
                if f.get("args"):
                    block.append(f"  - args: {f['args']}")
                if f.get("returns"):
                    block.append(f"  - returns: {f['returns']}")
            block.append("")
        if globals_:
            block.append("### Globals")
            for g in globals_:
                name = g.get("name") or "_"
                block.append(
                    f"- `{g.get('address','?')}` **{name}** "
                    f"({g.get('type','?')}, {g.get('access','?')}) — {g.get('purpose','')}"
                )
            block.append("")
        if io_writes:
            block.append("### I/O writes")
            for w in io_writes:
                block.append(
                    f"- **{w.get('register','?')}** ← {w.get('value_or_source','?')}"
                    f" — {w.get('purpose','')}"
                )
            block.append("")
        if constants:
            block.append("### Constants")
            for c in constants:
                block.append(
                    f"- `{c.get('value','?')}` — {c.get('meaning','')}"
                    f" _(ctx: {c.get('context','')})_"
                )
            block.append("")
        if notes.strip():
            block.append("### Notes")
            block.append(notes.strip())
            block.append("")

        with self.variables_md_path.open("a") as fh:
            fh.write("\n".join(block) + "\n")

    def _append_functions_cfg(self, functions: list[dict]) -> None:
        highs = [f for f in functions if f.get("confidence") == "high" and f.get("name")]
        if not highs:
            return
        existing = set()
        if self.functions_cfg_path.exists():
            for line in self.functions_cfg_path.read_text().splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = s.split()
                if len(parts) >= 2:
                    existing.add(parts[1])
        new_lines = []
        for f in highs:
            addr = str(f.get("address", "")).lower()
            if not addr or addr in existing:
                continue
            mode = "thumb_func" if (f.get("mode", "thumb").lower() == "thumb") else "arm_func"
            new_lines.append(f"{mode} {addr} {f['name']}")
            existing.add(addr)
        if new_lines:
            with self.functions_cfg_path.open("a") as fh:
                fh.write("\n".join(new_lines) + "\n")

    def _load_progress(self) -> dict:
        if self.progress_path.exists():
            return json.loads(self.progress_path.read_text())
        return {"completed": [], "errors": {}}

    def _save_progress(self, progress: dict) -> None:
        self.progress_path.write_text(json.dumps(progress, indent=2) + "\n")


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


_VARIABLES_TEMPLATE = """# Variables & Memory Map

Automatically accumulated by the gba_convert analysis pipeline.
Each module appends a section below. Do not hand-edit the per-module
sections — if you rename something, update this file's top-level
glossary and let the re-run overwrite module-level entries.

## Glossary (hand-edited; survives re-runs)

_Put canonical names here. The analyzer reads this file as context for
every module call, so entries written here become the source of truth._

"""
