#!/usr/bin/env bash
# Import CSV users through the running File Browser API. Requires Python 3.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/filebrowser_import_users.py" "$@"
