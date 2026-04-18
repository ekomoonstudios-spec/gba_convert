#!/usr/bin/env bash
# Usage: scripts/edit_by_category.sh <category> "<instruction>"
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$HERE/edit.py" --category "$1" "$2"
python3 "$HERE/recompile.py"
python3 "$HERE/rebuild.py"
