#!/usr/bin/env bash
# Bootstrap or verify account_manager using credentials/admin.csv.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/filebrowser_setup.py" "$@"
