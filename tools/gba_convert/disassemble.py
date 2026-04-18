"""Step 1: disassemble a GBA ROM with Luvdis.

Shells out to `python -m luvdis` using the local Luvdis checkout at
`../../luvdis/`. No pip install needed.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
LUVDIS_DIR = REPO_ROOT / "luvdis"


@dataclass
class DisasmResult:
    rom_path: Path
    rom_hash: str
    rom_info: str
    asm_path: Path
    config_out_path: Path


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _luvdis_env() -> dict:
    """Ensure local Luvdis is importable even without `pip install -e`."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{LUVDIS_DIR}{os.pathsep}{existing}" if existing else str(LUVDIS_DIR)
    )
    return env


def _run_luvdis(args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "luvdis", *args]
    proc = subprocess.run(
        cmd,
        env=_luvdis_env(),
        check=False,
        capture_output=capture,
        text=True,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-10:]
        raise RuntimeError(
            f"luvdis {args[0]} failed (exit {proc.returncode}):\n  "
            + "\n  ".join(tail)
        )
    return proc


def disassemble(
    rom_path: Path,
    output_dir: Path,
    *,
    default_mode: str = "BYTE",
    start: int | None = None,
    stop: int | None = None,
    seed_config: Path | None = None,
) -> DisasmResult:
    rom_path = Path(rom_path).resolve()
    if not rom_path.is_file():
        raise FileNotFoundError(f"ROM not found: {rom_path}")
    if not LUVDIS_DIR.is_dir():
        raise FileNotFoundError(
            f"Luvdis checkout missing at {LUVDIS_DIR}; clone it there first."
        )

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    asm_path = output_dir / "rom.s"
    config_out = output_dir / "functions.cfg"
    info_path = output_dir / "rom.info.txt"

    rom_hash = _sha1(rom_path)

    info = _run_luvdis(["info", str(rom_path)], capture=True)
    info_text = (info.stdout or "") + (info.stderr or "")
    info_path.write_text(f"sha1: {rom_hash}\n\n{info_text}")

    args = [
        "disasm",
        str(rom_path),
        "-o",
        str(asm_path),
        "-co",
        str(config_out),
        "--default-mode",
        default_mode,
    ]
    if start is not None:
        args += ["--start", hex(start)]
    if stop is not None:
        args += ["--stop", hex(stop)]
    if seed_config is not None and seed_config.is_file():
        args += ["-c", str(seed_config)]

    _run_luvdis(args)

    return DisasmResult(
        rom_path=rom_path,
        rom_hash=rom_hash,
        rom_info=info_text.strip(),
        asm_path=asm_path,
        config_out_path=config_out,
    )
