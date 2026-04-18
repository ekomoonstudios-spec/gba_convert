#!/usr/bin/env bash
# Usage: scripts/edit_by_module.sh <module_id> "<instruction>"
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$HERE/edit.py" --module "$1" "$2"
python3 "$HERE/recompile.py"
python3 "$HERE/rebuild.py"
