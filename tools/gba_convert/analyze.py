"""Step 3: drive Claude over each module to produce annotated .s,
per-module markdown dossiers, and high-confidence entries in
functions.cfg.

Context layout (important — read before editing):

- `variables.md` is a **glossary-only** file. Hand-editable. Small.
  The analyzer does NOT append per-module sections here — it reads it
  as stable cacheable context.
- `output/per_module/<module>.md` is written fresh per module each run.
  That's where functions, globals, io_writes, constants, category, and
  notes for that module live. Downstream tools read these *selectively*.
- `modules.md` is the short index table (id, category, summary).
- `categories.json` groups ids by category for `edit.py --category`.

Prompt caching works because `variables.md` is stable across the loop;
the per-module file changes per call but isn't in the cache prefix.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from anthropic import Anthropic

import index_db

HERE = Path(__file__).resolve().parent
SYSTEM_PROMPT_PATH = HERE / "CLAUDE.md"
PROMPT_TEMPLATE_PATH = HERE / "prompts" / "module_analysis.md"

MODEL = "claude-opus-4-7"
MAX_TOKENS = 16_000


CATEGORIES = {
    "audio", "video", "input", "gameplay", "ui",
    "system", "bios_wrapper", "data", "unknown",
}


@dataclass
class AnalysisResult:
    module_index: int
    annotated_path: Path
    functions_added: list[dict]
    globals_added: list[dict]
    io_writes: list[dict]
    constants: list[dict]
    category: str
    category_reason: str
    notes: str


class Analyzer:
    def __init__(self, output_dir: Path, *, model: str = MODEL) -> None:
        self.output_dir = output_dir
        self.modules_dir = output_dir / "modules"
        self.annotated_dir = output_dir / "annotated"
        self.annotated_dir.mkdir(parents=True, exist_ok=True)
        self.per_module_dir = output_dir / "per_module"
        self.per_module_dir.mkdir(parents=True, exist_ok=True)
        self.variables_md_path = output_dir / "variables.md"
        self.functions_cfg_path = output_dir / "functions.cfg"
        self.modules_md_path = output_dir / "modules.md"
        self.categories_json_path = output_dir / "categories.json"
        self.characters_md_path = output_dir / "characters.md"
        self.characters_sidecar_path = output_dir / ".character_mentions.json"
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
        skip_data: bool = True,
    ) -> list[AnalysisResult]:
        progress = self._load_progress()
        results: list[AnalysisResult] = []

        for mod in modules:
            idx = mod["index"]
            if skip_data and mod.get("kind") == "data":
                progress.setdefault("skipped_data", []).append(idx)
                self._record_module_summary(mod, category="data",
                                            category_reason="kind=data (skipped by analyzer)",
                                            summary="Pure-data module (not LLM-analysed).")
                continue
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

        self._rewrite_modules_md(modules)
        self._rewrite_categories_json(modules)
        self._rewrite_characters_md()
        try:
            n = index_db.rebuild(self.output_dir)
            print(f"  index_db: {n} modules → {self.output_dir / 'index.sqlite'}")
        except Exception as exc:  # noqa: BLE001 — index is optional
            print(f"  index_db: skipped ({type(exc).__name__}: {exc})")
        return results

    def analyze_one(self, mod: dict) -> AnalysisResult:
        module_path = self.modules_dir / mod["path"]
        source = module_path.read_text()
        glossary = self.variables_md_path.read_text()

        user_prompt = self.template.format(
            module_path=mod["path"],
            addr_start=mod["addr_start"],
            addr_end=mod["addr_end"],
            kind=mod["kind"],
            line_count=len(source.splitlines()),
            glossary=glossary,
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
        category = (parsed.get("category") or "unknown").strip().lower()
        if category not in CATEGORIES:
            category = "unknown"
        category_reason = (parsed.get("category_reason") or "").strip()
        characters = parsed.get("characters", []) or []
        notes = parsed.get("notes", "") or ""

        if characters:
            self._record_character_mentions(mod, characters)

        self._write_per_module_md(mod, functions, globals_, io_writes,
                                  constants, category, category_reason, notes)
        self._write_per_module_json(mod, functions, globals_, io_writes,
                                    constants, category, category_reason, notes)
        self._append_functions_cfg(functions)
        self._record_module_summary(
            mod,
            category=category,
            category_reason=category_reason,
            summary=_short_summary(functions, notes),
        )

        return AnalysisResult(
            module_index=mod["index"],
            annotated_path=annotated_path,
            functions_added=functions,
            globals_added=globals_,
            io_writes=io_writes,
            constants=constants,
            category=category,
            category_reason=category_reason,
            notes=notes,
        )

    def _write_per_module_md(
        self,
        mod: dict,
        functions: list[dict],
        globals_: list[dict],
        io_writes: list[dict],
        constants: list[dict],
        category: str,
        category_reason: str,
        notes: str,
    ) -> None:
        """Write a fresh markdown dossier for THIS module only.

        File: output/per_module/<same-stem>.md. Overwrites any previous
        version so re-runs stay idempotent. `translate_to_c.py`,
        `edit.py`, and future tooling read this file selectively so no
        downstream call ever pulls the full accumulated analysis.
        """
        block: list[str] = [
            f"# Module `{mod['path']}`",
            "",
            f"- **id:** {mod['index']}",
            f"- **range:** `{mod['addr_start']}` – `{mod['addr_end']}`",
            f"- **kind:** {mod.get('kind', '?')}",
            f"- **category:** `{category}`"
            + (f" — {category_reason}" if category_reason else ""),
            "",
        ]
        if functions:
            block.append("## Functions")
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
            block.append("## Globals")
            for g in globals_:
                name = g.get("name") or "_"
                block.append(
                    f"- `{g.get('address','?')}` **{name}** "
                    f"({g.get('type','?')}, {g.get('access','?')}) — {g.get('purpose','')}"
                )
            block.append("")
        if io_writes:
            block.append("## I/O writes")
            for w in io_writes:
                block.append(
                    f"- **{w.get('register','?')}** ← {w.get('value_or_source','?')}"
                    f" — {w.get('purpose','')}"
                )
            block.append("")
        if constants:
            block.append("## Constants")
            for c in constants:
                block.append(
                    f"- `{c.get('value','?')}` — {c.get('meaning','')}"
                    f" _(ctx: {c.get('context','')})_"
                )
            block.append("")
        if notes.strip():
            block.append("## Notes")
            block.append(notes.strip())
            block.append("")

        stem = Path(mod["path"]).stem
        out_path = self.per_module_dir / f"{stem}.md"
        out_path.write_text("\n".join(block) + "\n")

    def _write_per_module_json(
        self,
        mod: dict,
        functions: list[dict],
        globals_: list[dict],
        io_writes: list[dict],
        constants: list[dict],
        category: str,
        category_reason: str,
        notes: str,
    ) -> None:
        """Structured sidecar next to the .md, for index_db ingestion."""
        payload = {
            "index": mod["index"],
            "path": mod["path"],
            "addr_start": mod["addr_start"],
            "addr_end": mod["addr_end"],
            "kind": mod.get("kind", "unknown"),
            "category": category,
            "category_reason": category_reason,
            "functions": functions,
            "globals": globals_,
            "io_writes": io_writes,
            "constants": constants,
            "notes": notes,
        }
        stem = Path(mod["path"]).stem
        (self.per_module_dir / f"{stem}.json").write_text(
            json.dumps(payload, indent=2) + "\n"
        )

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

    def _record_module_summary(
        self, mod: dict, *, category: str, category_reason: str, summary: str,
    ) -> None:
        """Write one module's category row into a sidecar JSON keyed by index.

        Accumulates across runs; _rewrite_modules_md renders it into modules.md.
        """
        sidecar = self.output_dir / ".module_summaries.json"
        data: dict = {}
        if sidecar.exists():
            try:
                data = json.loads(sidecar.read_text())
            except json.JSONDecodeError:
                data = {}
        data[str(mod["index"])] = {
            "index": mod["index"],
            "path": mod["path"],
            "addr_start": mod["addr_start"],
            "addr_end": mod["addr_end"],
            "kind": mod.get("kind", "unknown"),
            "category": category,
            "category_reason": category_reason,
            "summary": summary,
        }
        sidecar.write_text(json.dumps(data, indent=2) + "\n")

    def _rewrite_modules_md(self, modules: list[dict]) -> None:
        """Render modules.md — one row per module, indexed by id.

        Pulls category/summary from the sidecar populated during analysis;
        modules that weren't analysed fall back to a placeholder row so the
        file is always a complete index the user can target edits from.
        """
        sidecar = self.output_dir / ".module_summaries.json"
        summaries: dict = {}
        if sidecar.exists():
            try:
                summaries = json.loads(sidecar.read_text())
            except json.JSONDecodeError:
                summaries = {}

        lines = [
            "# Module index",
            "",
            "Generated by `analyze.py`. Each row is one module; use the `id`",
            "column to target it from `edit.py` (e.g. `--module 42`).",
            "",
            "| id | path | range | kind | category | summary |",
            "|---:|------|-------|------|----------|---------|",
        ]
        for mod in modules:
            info = summaries.get(str(mod["index"]))
            if info is None:
                info = {
                    "category": "data" if mod.get("kind") == "data" else "(not analysed)",
                    "summary": "",
                }
            safe_summary = (info.get("summary", "") or "").replace("|", r"\|")
            lines.append(
                f"| {mod['index']} "
                f"| `{mod['path']}` "
                f"| `{mod['addr_start']}`–`{mod['addr_end']}` "
                f"| {mod.get('kind', '?')} "
                f"| `{info.get('category', 'unknown')}` "
                f"| {safe_summary} |"
            )
        self.modules_md_path.write_text("\n".join(lines) + "\n")

    def _record_character_mentions(self, mod: dict, characters: list[dict]) -> None:
        """Accumulate character mentions into a sidecar JSON.

        Schema on disk:
            {
              "Mario": [{"module_index": 42, "module_path": "mod_...",
                         "role": "player", "evidence": "...",
                         "confidence": "high"}, ...],
              ...
            }

        Rolled up into `characters.md` at the end of `analyze_all`.
        """
        data: dict = {}
        if self.characters_sidecar_path.exists():
            try:
                data = json.loads(self.characters_sidecar_path.read_text())
            except json.JSONDecodeError:
                data = {}
        for c in characters:
            name = (c.get("name") or "").strip()
            if not name:
                continue
            entry = {
                "module_index": mod["index"],
                "module_path": mod["path"],
                "role": (c.get("role") or "unknown").strip().lower(),
                "evidence": (c.get("evidence") or "").strip(),
                "confidence": (c.get("confidence") or "medium").strip().lower(),
            }
            bucket = data.setdefault(name, [])
            # Dedup by module index so re-runs don't pile duplicates.
            bucket[:] = [e for e in bucket if e["module_index"] != mod["index"]]
            bucket.append(entry)
        self.characters_sidecar_path.write_text(json.dumps(data, indent=2) + "\n")

    def _rewrite_characters_md(self) -> None:
        """Render characters.md from the mention sidecar.

        Preserves a user-editable section at the top of the file (anything
        above the `<!-- AUTO-GENERATED BELOW -->` marker) so hand-added
        characters survive re-runs.
        """
        sidecar = self.characters_sidecar_path
        mentions: dict = {}
        if sidecar.exists():
            try:
                mentions = json.loads(sidecar.read_text())
            except json.JSONDecodeError:
                mentions = {}

        marker = "<!-- AUTO-GENERATED BELOW — hand edits above this line survive re-runs -->"
        user_section = _CHARACTERS_USER_HEADER
        if self.characters_md_path.exists():
            prev = self.characters_md_path.read_text()
            if marker in prev:
                user_section = prev.split(marker, 1)[0].rstrip() + "\n"

        auto_lines: list[str] = [marker, "",
                                 "# Auto-detected character mentions", ""]
        if not mentions:
            auto_lines.append("_No character mentions detected yet. "
                              "They tend to appear in dialogue / UI / sprite modules._")
        else:
            for name in sorted(mentions.keys(), key=str.lower):
                entries = mentions[name]
                roles = sorted({e["role"] for e in entries if e.get("role")})
                auto_lines.append(f"## {name}")
                if roles:
                    auto_lines.append(f"- **role(s):** {', '.join(roles)}")
                auto_lines.append(f"- **appears in {len(entries)} module(s):**")
                for e in sorted(entries, key=lambda x: x["module_index"]):
                    auto_lines.append(
                        f"  - id `{e['module_index']}` "
                        f"[`{e['module_path']}`](per_module/{Path(e['module_path']).stem}.md) "
                        f"_(conf: {e.get('confidence', '?')})_ — {e.get('evidence', '')}"
                    )
                auto_lines.append("")

        self.characters_md_path.write_text(user_section + "\n" + "\n".join(auto_lines) + "\n")

    def _rewrite_categories_json(self, modules: list[dict]) -> None:
        """Group module ids by category for quick lookup from edit.py."""
        sidecar = self.output_dir / ".module_summaries.json"
        summaries: dict = {}
        if sidecar.exists():
            try:
                summaries = json.loads(sidecar.read_text())
            except json.JSONDecodeError:
                summaries = {}

        grouped: dict[str, list[int]] = {c: [] for c in CATEGORIES}
        for mod in modules:
            info = summaries.get(str(mod["index"]))
            if info is None:
                cat = "data" if mod.get("kind") == "data" else "unknown"
            else:
                cat = info.get("category", "unknown")
            grouped.setdefault(cat, []).append(mod["index"])

        self.categories_json_path.write_text(json.dumps(grouped, indent=2) + "\n")

    def _load_progress(self) -> dict:
        if self.progress_path.exists():
            return json.loads(self.progress_path.read_text())
        return {"completed": [], "errors": {}}

    def _save_progress(self, progress: dict) -> None:
        self.progress_path.write_text(json.dumps(progress, indent=2) + "\n")


def _short_summary(functions: list[dict], notes: str) -> str:
    """One-line summary for modules.md — prefers the first named function."""
    for f in functions:
        name = f.get("name") or ""
        desc = (f.get("summary") or "").strip()
        if name and desc:
            return f"{name} — {desc}"
        if name:
            return name
    first_line = (notes or "").strip().splitlines()[:1]
    return first_line[0] if first_line else ""


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


_CHARACTERS_USER_HEADER = """# Characters

This file maps characters (player, NPCs, enemies, bosses) to the modules
that hold their data or behaviour. Two sections:

1. **Hand-curated** (below, before the auto-generated marker) —
   canonical character list. Add characters you've discovered or want
   to add *even if the analyzer didn't find them*. Anything here
   survives `analyze.py` re-runs.
2. **Auto-detected mentions** (below the marker) — rebuilt every run
   from `.character_mentions.json`, which the analyzer fills when it
   sees direct evidence (embedded strings, sprite tables, etc.).

## Hand-curated characters

_Add entries like:_

```
- **Mario** (player) — sprite in `mod_0048_0810AC00.s`, stats in `mod_0051_080F9A20.s`
- **Bowser** (boss) — AI state machine in `mod_0112_0821B040.s`
```

## Adding a new character (not yet in the ROM)

If you want to *introduce* a new character, list the modules you plan
to change (usually: sprite table, stats block, a new state-machine
function). `edit.py --character <name>` will route edits to those
modules in order.

"""


_VARIABLES_TEMPLATE = """# Glossary (hand-edited; survives re-runs)

This is the **canonical name registry** for the ROM. It is small,
stable, and passed into every LLM call as cacheable context. Per-module
analysis data lives in `per_module/<module>.md`, not here.

Add entries like:

    - `sub_080024C0` → **AgbMain** — main game loop entry
    - `0x03001000` → **g_player_state** (struct, 0x40 bytes)

The analyzer reads this file as authoritative — if an entry here names
`sub_080024C0` as `AgbMain`, every module must use that name.

## Functions

(none yet — fill in as you discover canonical names)

## Globals / RAM

(none yet)

## Conventions

- Function names: `PascalCase` if they feel "engine-ish" (AgbMain,
  UpdateSprite), `snake_case` if they feel "game-logicy".
- Global names: `snake_case` with a `g_` prefix for globals that live
  beyond one frame, bare `snake_case` for short-lived scratch.
"""
