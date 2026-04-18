"""Step 2: split a Luvdis .s file into LLM-sized module chunks.

Cuts on function directives (`thumb_func`, `arm_func`,
`non_word_aligned_thumb_func`) and respects a max-lines ceiling. Each
output module gets a header comment with its address range, source
range, and inferred kind.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

FUNC_DIRECTIVE = re.compile(
    r"^\s*(?:thumb_func_start|arm_func_start|non_word_aligned_thumb_func_start)\s+"
)
LABEL_ADDR = re.compile(r"^_?(sub|func|[A-Za-z_])\w*?_?([0-9A-Fa-f]{7,8}):")
HEX_IN_COMMENT = re.compile(r"0[xX]([0-9A-Fa-f]{7,8})")
DATA_DIRECTIVE = re.compile(r"^\s*\.(byte|hword|word|ascii|asciz|incbin|fill|space)\b")
SECTION_DIRECTIVE = re.compile(r"^\s*\.section\b")


@dataclass
class Module:
    index: int
    addr_start: str
    addr_end: str
    src_line_start: int
    src_line_end: int
    kind: str  # "code" | "data" | "mixed"
    path: str  # relative to modules dir


def _extract_addr(line: str) -> str | None:
    m = LABEL_ADDR.match(line.lstrip())
    if m:
        return f"0x{int(m.group(2), 16):08X}"
    m = HEX_IN_COMMENT.search(line)
    if m:
        return f"0x{int(m.group(1), 16):08X}"
    return None


def _classify(lines: Iterable[str]) -> str:
    code_hits = 0
    data_hits = 0
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("@"):
            continue
        if DATA_DIRECTIVE.match(ln):
            data_hits += 1
        elif FUNC_DIRECTIVE.match(ln) or s.endswith(":") or re.match(r"\s*[a-z]", ln):
            code_hits += 1
    if code_hits == 0 and data_hits > 0:
        return "data"
    if data_hits == 0 and code_hits > 0:
        return "code"
    return "mixed"


def split_asm(
    asm_path: Path,
    modules_dir: Path,
    *,
    max_lines: int = 1500,
) -> list[Module]:
    modules_dir.mkdir(parents=True, exist_ok=True)
    for old in modules_dir.glob("mod_*.s"):
        old.unlink()

    text = asm_path.read_text(errors="replace").splitlines(keepends=False)
    boundaries: list[int] = []  # line indices where a chunk may begin
    last = 0

    for i, line in enumerate(text):
        is_func = bool(FUNC_DIRECTIVE.match(line))
        # Hard size cut: if we've gone max_lines without any boundary, cut
        # at the next sensible spot (blank line or data-directive block).
        if is_func or (i - last >= max_lines and (not line.strip() or DATA_DIRECTIVE.match(line))):
            boundaries.append(i)
            last = i

    if not boundaries:
        boundaries = [0]
    if boundaries[0] != 0:
        boundaries.insert(0, 0)
    boundaries.append(len(text))  # sentinel

    modules: list[Module] = []
    idx = 0
    cursor = 0  # current boundary index
    while cursor < len(boundaries) - 1:
        chunk_start_line = boundaries[cursor]
        chunk_end_line = boundaries[cursor + 1]

        # Expand until either size ceiling or no more boundaries.
        next_cursor = cursor + 1
        while (
            next_cursor < len(boundaries) - 1
            and (boundaries[next_cursor + 1] - chunk_start_line) <= max_lines
        ):
            next_cursor += 1
            chunk_end_line = boundaries[next_cursor]

        chunk_lines = text[chunk_start_line:chunk_end_line]
        addr_start = _first_addr(chunk_lines) or "0x08000000"
        addr_end = _last_addr(chunk_lines) or addr_start
        kind = _classify(chunk_lines)

        rel_name = f"mod_{idx:04d}_{addr_start[2:]}.s"
        out_path = modules_dir / rel_name
        header = (
            f"@ Module: {rel_name}\n"
            f"@ Range:  {addr_start} – {addr_end}\n"
            f"@ Source: {asm_path.name} lines {chunk_start_line + 1}–{chunk_end_line}\n"
            f"@ Kind:   {kind}\n"
            f"@ Index:  {idx}\n"
            f"@ ----------------------------------------------------------\n\n"
        )
        out_path.write_text(header + "\n".join(chunk_lines) + "\n")

        modules.append(
            Module(
                index=idx,
                addr_start=addr_start,
                addr_end=addr_end,
                src_line_start=chunk_start_line + 1,
                src_line_end=chunk_end_line,
                kind=kind,
                path=rel_name,
            )
        )
        idx += 1
        cursor = next_cursor

    index_path = modules_dir / "_index.json"
    index_path.write_text(
        json.dumps([asdict(m) for m in modules], indent=2) + "\n"
    )
    return modules


def _first_addr(lines: list[str]) -> str | None:
    for ln in lines:
        a = _extract_addr(ln)
        if a:
            return a
    return None


def _last_addr(lines: list[str]) -> str | None:
    for ln in reversed(lines):
        a = _extract_addr(ln)
        if a:
            return a
    return None
