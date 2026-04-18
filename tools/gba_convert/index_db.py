"""SQLite + FTS5 search index over analyze.py's output.

Purpose
-------

`edit.py` needs to route a natural-language instruction to the right
module. Once the ROM has 100+ modules, shipping them all to Claude for
Stage 1 routing is wasteful — most are obviously irrelevant to any one
instruction. This index pre-filters to ~20 candidates via keyword search,
then Claude makes the final semantic pick.

Ingested from
-------------

- `output/.module_summaries.json`  — one row per module (category, summary)
- `output/per_module/<stem>.json`  — structured per-module facts
  (functions, globals, io_writes, constants, notes)
- `output/per_module/<stem>.md`    — full dossier markdown for FTS body
- `output/.character_mentions.json` — character ↔ module map

Index lives at `output/index.sqlite`. Rebuild is destructive; drop + recreate.

Usage
-----

    # Called automatically at the end of analyze.py.
    python index_db.py rebuild
    python index_db.py search "increase max HP"
    python index_db.py search "joypad" --category input --limit 5
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import click


DEFAULT_OUTPUT = Path(__file__).resolve().parent / "output"


@dataclass
class SearchHit:
    module_id: int
    path: str
    category: str
    summary: str
    score: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS modules (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    addr_start TEXT NOT NULL,
    addr_end TEXT NOT NULL,
    kind TEXT NOT NULL,
    category TEXT NOT NULL,
    category_reason TEXT,
    summary TEXT
);

CREATE TABLE IF NOT EXISTS functions (
    module_id INTEGER NOT NULL REFERENCES modules(id),
    name TEXT,
    address TEXT,
    mode TEXT,
    summary TEXT,
    args TEXT,
    returns TEXT,
    confidence TEXT
);
CREATE INDEX IF NOT EXISTS idx_functions_name ON functions(name);
CREATE INDEX IF NOT EXISTS idx_functions_address ON functions(address);

CREATE TABLE IF NOT EXISTS io_writes (
    module_id INTEGER NOT NULL REFERENCES modules(id),
    register TEXT,
    value_or_source TEXT,
    purpose TEXT
);
CREATE INDEX IF NOT EXISTS idx_io_writes_register ON io_writes(register);

CREATE TABLE IF NOT EXISTS characters (
    name TEXT NOT NULL,
    module_id INTEGER NOT NULL REFERENCES modules(id),
    role TEXT,
    evidence TEXT,
    confidence TEXT,
    PRIMARY KEY (name, module_id)
);
CREATE INDEX IF NOT EXISTS idx_characters_name ON characters(name);

CREATE VIRTUAL TABLE IF NOT EXISTS modules_fts USING fts5(
    path, category, summary, dossier_body,
    content='', tokenize='porter unicode61'
);
"""


def _connect(output_dir: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(output_dir / "index.sqlite")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _check_fts5(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
        conn.execute("DROP TABLE _fts5_probe")
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            "This SQLite build lacks FTS5. Install a Python with FTS5 "
            "support (python.org and Homebrew builds have it)."
        ) from exc


def rebuild(output_dir: Path) -> int:
    """Drop and rebuild the search index. Returns module count indexed."""
    summaries_path = output_dir / ".module_summaries.json"
    per_module_dir = output_dir / "per_module"
    characters_path = output_dir / ".character_mentions.json"

    summaries: dict = {}
    if summaries_path.is_file():
        summaries = json.loads(summaries_path.read_text())
    mentions: dict = {}
    if characters_path.is_file():
        mentions = json.loads(characters_path.read_text())

    db_path = output_dir / "index.sqlite"
    if db_path.exists():
        db_path.unlink()

    conn = _connect(output_dir)
    _check_fts5(conn)
    conn.executescript(_SCHEMA)

    for info in summaries.values():
        mod_id = int(info["index"])
        conn.execute(
            "INSERT INTO modules "
            "(id, path, addr_start, addr_end, kind, category, category_reason, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mod_id,
                info.get("path", ""),
                info.get("addr_start", ""),
                info.get("addr_end", ""),
                info.get("kind", ""),
                info.get("category", "unknown"),
                info.get("category_reason", "") or "",
                info.get("summary", "") or "",
            ),
        )

        stem = Path(info.get("path", "")).stem
        facts_path = per_module_dir / f"{stem}.json"
        dossier_md_path = per_module_dir / f"{stem}.md"

        functions: list[dict] = []
        io_writes: list[dict] = []
        if facts_path.is_file():
            try:
                facts = json.loads(facts_path.read_text())
            except json.JSONDecodeError:
                facts = {}
            functions = facts.get("functions", []) or []
            io_writes = facts.get("io_writes", []) or []

        for f in functions:
            conn.execute(
                "INSERT INTO functions "
                "(module_id, name, address, mode, summary, args, returns, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mod_id,
                    f.get("name"),
                    f.get("address"),
                    f.get("mode"),
                    f.get("summary"),
                    json.dumps(f.get("args")) if f.get("args") else None,
                    f.get("returns"),
                    f.get("confidence"),
                ),
            )
        for w in io_writes:
            conn.execute(
                "INSERT INTO io_writes "
                "(module_id, register, value_or_source, purpose) "
                "VALUES (?, ?, ?, ?)",
                (
                    mod_id,
                    w.get("register"),
                    w.get("value_or_source"),
                    w.get("purpose"),
                ),
            )

        dossier_body = dossier_md_path.read_text() if dossier_md_path.is_file() else ""
        conn.execute(
            "INSERT INTO modules_fts "
            "(rowid, path, category, summary, dossier_body) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                mod_id,
                info.get("path", ""),
                info.get("category", "unknown"),
                info.get("summary", "") or "",
                dossier_body,
            ),
        )

    for name, entries in mentions.items():
        for e in entries:
            conn.execute(
                "INSERT OR REPLACE INTO characters "
                "(name, module_id, role, evidence, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    name,
                    int(e["module_index"]),
                    e.get("role"),
                    e.get("evidence"),
                    e.get("confidence"),
                ),
            )

    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM modules").fetchone()[0]
    conn.close()
    return count


_FTS_STRIP = re.compile(r"[^\w\s]+")


def _sanitize_fts_query(q: str) -> str:
    """Strip FTS5 operator chars; prefix-match each token."""
    cleaned = _FTS_STRIP.sub(" ", q).strip()
    tokens = [t for t in cleaned.split() if len(t) >= 2]
    return " ".join(f"{t}*" for t in tokens)


def search(
    output_dir: Path,
    query: str,
    *,
    limit: int = 20,
    category: str | None = None,
) -> list[SearchHit]:
    db_path = output_dir / "index.sqlite"
    if not db_path.is_file():
        raise FileNotFoundError(
            f"{db_path} missing — run `python index_db.py rebuild` "
            f"or re-run analyze.py."
        )

    fts_q = _sanitize_fts_query(query)
    if not fts_q:
        return []

    conn = _connect(output_dir)
    conn.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT m.id, m.path, m.category, m.summary, "
            "bm25(modules_fts) AS score "
            "FROM modules_fts "
            "JOIN modules m ON m.id = modules_fts.rowid "
            "WHERE modules_fts MATCH ?"
        )
        params: list = [fts_q]
        if category:
            sql += " AND m.category = ?"
            params.append(category)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    return [
        SearchHit(
            module_id=r["id"],
            path=r["path"],
            category=r["category"],
            summary=r["summary"] or "",
            score=r["score"],
        )
        for r in rows
    ]


@click.group()
def cli() -> None:
    """Search index over analyze.py's output."""


@cli.command("rebuild")
@click.option("--output", type=click.Path(path_type=Path), default=DEFAULT_OUTPUT,
              show_default=True, help="Pipeline output directory.")
def rebuild_cmd(output: Path) -> None:
    """Drop and rebuild output/index.sqlite."""
    n = rebuild(output)
    click.secho(f"  indexed {n} modules into {output / 'index.sqlite'}", fg="green")


@cli.command("search")
@click.argument("query")
@click.option("--output", type=click.Path(path_type=Path), default=DEFAULT_OUTPUT,
              show_default=True, help="Pipeline output directory.")
@click.option("--category", default=None, help="Restrict to one category.")
@click.option("--limit", type=int, default=20, show_default=True)
def search_cmd(query: str, output: Path, category: str | None, limit: int) -> None:
    """Search for modules matching QUERY."""
    hits = search(output, query, limit=limit, category=category)
    if not hits:
        click.echo("  (no hits)")
        return
    for h in hits:
        summary = (h.summary or "")[:80]
        click.echo(f"  [{h.module_id:4d}] {h.category:<12} {h.path}  {summary}")


if __name__ == "__main__":
    cli()
