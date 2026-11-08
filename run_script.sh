
#!/usr/bin/env bash
# The single public interface for this File Browser project.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$SCRIPT_DIR"

# Display available options
show_help() {
    cat <<'EOF'
Available options:
    - ./run_script.sh --setup                 : Initialize the service and create/verify administrators from credentials/admin.csv
    - ./run_script.sh --dry-run               : Preview operations without changes and validate both CSV files
    - ./run_script.sh --create-users          : Create users from credentials/user.csv
    - ./run_script.sh --delete-account        : Interactively delete a selected account and optionally its data
    - ./run_script.sh --clear-users-and-data  : Delete all non-admin accounts and their data
    - ./run_script.sh --hard-clear            : Delete all accounts, database, and stored data
    - ./run_script.sh --details               : Display the complete management guide
    - ./run_script.sh --help                  : Display available options

Options can be combined when supported (e.g. --setup --create-users).

For more information, see README.md.
EOF
}

# Display help when no arguments are provided
if [[ $# -eq 0 ]]; then
    show_help
    exit 0
fi

# Handle help flags
for arg in "$@"; do
    case "$arg" in
        --help|-h)
            show_help
            exit 0
            ;;
    esac
done

# Forward management operations to Python
exec python3 -B "$SCRIPT_DIR/internal/filebrowser_manage.py" "$@"
