# Default-user CSV import

Use the public entry point:

```bash
bash ./run_script.sh --create-users --dry-run
bash ./run_script.sh --create-users
```

The script reads `credentials/user.csv` and authenticates automatically using
`credentials/admin.csv`. Initialize administrator accounts first with:

```bash
bash ./run_script.sh --setup
```

Both CSVs use `username,password` headers. Default users get private scopes at
`/users/<username>`; all administrators have shared scope `/`.

Safe existing users are skipped and their passwords retained. Conflicting
accounts, overlapping non-admin scopes, unsafe symbolic links and unassigned
existing folders are rejected before import. Administrator, command execution
and public sharing permissions are disabled for default users.

Use `--clear-users-and-data` for testing cleanup, or `--delete-account` to choose
one account and decide whether to keep its files. Both require two confirmations.
See [README.md](README.md) for complete options, CSV format and deletion warnings.
