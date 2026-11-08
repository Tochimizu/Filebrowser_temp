# File Browser project management

Use **only `run_script.sh`** for account operations. It works from any working
directory, prints every available option, and reads credentials automatically.
With no flags it shows help and performs no action.

```bash
bash ./run_script.sh

# Explain the complete workflow directly in the terminal
bash ./run_script.sh --details
```

Run under WSL/Linux with Bash, Python 3 and Docker Compose. The implementation
and legacy helpers are in `internal/`; they are not public entry points.

## Credentials

Create `credentials/admin.csv` with one or more administrators:

```csv
username,password
account_manager,ReplaceWithUniqueManagerPassword123!
second_admin,ReplaceWithUniqueSecondAdminPassword123!
```

Create `credentials/user.csv` with default users:

```csv
username,password
alice,ReplaceWithUniqueAlicePassword123!
bob,"ReplaceWithUniqueBobPassword,456!"
```

Use the exact `username,password` headers. UTF-8 BOM, CRLF and quoted commas,
quotes and spaces are supported; password whitespace is preserved. Usernames
start with a letter/digit and use only letters, digits, `_`, `-`, `.`, `@`
(1–64 characters; no `..`). Case-insensitive duplicates and usernames appearing
in both roles are rejected. Passwords need at least the server minimum
(default: 12 characters) and at most 72 UTF-8 bytes. Administrator passwords
cannot contain newlines or NUL; no passwords may contain NUL.

The CSV files are excluded from Git. Setup/import sets their file mode to 600.
They must stay outside `admin/data`. Passwords are not displayed or placed in
Docker arguments/environment. Login uses a matching administrator from the CSV,
preferring `account_manager` when present; other administrator usernames work too.

## Options

| Flag | Behavior |
| --- | --- |
| `--setup` | Read `credentials/admin.csv`. Initialize a fresh service if needed and create/verify every listed administrator with scope `/`. Existing accounts are preserved. |
| `--dry-run` | Preview the selected operation without writes. Used alone, validate both CSVs and check proposed admin/default-user creation against live server state. |
| `--create-users` | Read `credentials/user.csv` and create ordinary accounts scoped to `/users/<username>`. |
| `--delete-account` | List accounts, ask for an exact username, and ask whether to remove that account's entire scoped folder branch. Require two typed confirmations. |
| `--clear-users-and-data` | Delete every live non-admin account and its scoped folders/files, including accounts absent from the CSV. Keep all administrators. Require two typed confirmations. |
| `--hard-clear` | Delete every account, including all administrators, the active database and every file under `admin/data`. Leave the service stopped until `--setup`. Require two typed confirmations. |
| `--details` | Print this complete guide as plain terminal text, including CSV rules, scopes, deletion warnings and recovery. Reads no credentials and performs no Docker/account operations. Run separately from action flags. |
| `--help` | Show all options and examples. |

Setup and import can be combined. Destructive actions must run separately from
setup/import and from each other. There is no confirmation-bypass flag.

```bash
# Validate everything, without creating accounts
bash ./run_script.sh --dry-run

# Initialize administrators and then create default users
bash ./run_script.sh --setup --create-users

# Preview either action
bash ./run_script.sh --setup --dry-run
bash ./run_script.sh --create-users --dry-run

# Add administrators from the CSV later
bash ./run_script.sh --setup

# Add default users from the CSV later
bash ./run_script.sh --create-users

# Interactive cleanup
bash ./run_script.sh --delete-account
bash ./run_script.sh --clear-users-and-data
bash ./run_script.sh --hard-clear

# Start again after hard clear
bash ./run_script.sh --setup --create-users
```

On a fresh project, a dry run checks CSVs, password lengths, Docker availability,
deployment configuration and local folder conflicts; it reports that live API
checks require setup. Against an existing running service it also checks
administrator credentials/roles, server password policy, existing accounts,
folder conflicts and overlapping scopes. Dry runs do not start containers,
create directories, change permissions, or generate Python cache files.

All requested CSVs and account conflicts are checked before account creation.
Existing safely configured users are skipped without resetting passwords.
Existing administrator passwords are verified during setup. Conflicting roles,
scopes or permissions cause an error; existing users are not silently promoted.
Editing a CSV alone does not rotate an existing account's password.
Account creation is not transactional if the server fails partway through.

## Folder isolation and shared administration

All CSV administrators get File Browser scope `/`: the served `/srv` directory,
mapped to this project's `admin/data`. They can access all default-user folders.
Linux root/sudo is not required. File Browser administrator permissions are global.

Each default user gets scope `/users/<username>`, mapped to
`admin/data/users/<username>`, and sees that folder as `/`. It can create,
edit, rename, delete and download its own files. Administrator, command execution
and public sharing permissions are disabled.

Self-registration and external symlink following are disabled. Blank scopes for
new users get individual homes under `/users`. The Docker image is pinned to
`filebrowser/filebrowser:v2.63.23`, which includes
[the upstream symlink isolation fixes](https://github.com/filebrowser/filebrowser/security/advisories/GHSA-239w-m3h6-ch8v).
Upstream has [archived the project](https://github.com/filebrowser/filebrowser/releases/tag/v2.63.23)
and does not plan further fixes.

A folder without a matching safe account is not automatically assigned to a new
account. Inspect or remove old data intentionally before reusing its username.

## Deletion confirmations and recovery

Destructive commands require an interactive terminal. The script shows the
accounts and absolute folder paths affected, and asks for two exact phrases.
A missing or incorrect answer cancels before account/data changes.

For individual deletion, keeping files is the default. If data removal is chosen,
the entire scope and every descendant file are selected, including nested users'
folders. Deleting a root-scoped administrator such as `account_manager` can erase
the **WHOLE BRANCH: all of `admin/data`**, not just that administrator's personal
folder. Other account records are retained, but their files in the deleted branch
will be lost. Both confirmations explicitly warn about this.

File Browser protects its last administrator. Add another administrator using
`--setup` before deleting the original, or use `--hard-clear` to remove the whole
system. Bulk default-user cleanup refuses scopes that would erase an
administrator's scoped branch.

The service is stopped for deletion. A private database snapshot goes under
`.backups/<timestamp>/`, outside the served data. Folder branches temporarily move
to private quarantine so a failed activation can restore them and the database;
after successful activation the quarantined data is erased. Symbolic links are
not followed out of a deletion branch. All local management operations share a
lock; avoid concurrent external administrator/filesystem changes.

**Database snapshots do not back up uploaded files.** Successful data deletion is
irreversible without a separate backup. If final erasure fails, the command names
the remaining private quarantine folder for inspection. Files and accounts must
be inspected after interrupted/failed operations.

`--clear-users-and-data` leaves administrators, orphaned folders without an account,
and files outside the removed users' scopes. `--hard-clear` also removes orphaned
served data and resets sessions, shares and database settings. Credentials,
Compose/configuration, source code and existing private database backups remain.
It does not automatically recreate an administrator; run `--setup` afterward.

## Development checks

```bash
python3 -m unittest discover -s tests -v
python3 tests/docker_smoke_test.py
python3 tests/management_smoke_test.py
```

Docker smoke tests create isolated temporary databases/projects with fake
credentials. The management smoke test invokes the public shell entry point and
exercises both confirmations, cancellation, individual/bulk/root-scope deletion,
hard clear, fresh setup, multiple administrators and student folder isolation.
They do not change the live project's accounts or uploaded files.
