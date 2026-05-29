# Photon Implementation Errors

## `python` Command Missing During Syntax Check

### What Was The Error
Running `python -m py_compile plugins/platforms/photon/cli.py` failed because this shell has no `python` executable on `PATH`.

### How Did You Fix It
Use the repo virtualenv Python or `python3` for verification commands.

### What Did Not Work
Calling bare `python` did not work in this environment.

### What Did You Learn
This checkout follows the project guidance to prefer `.venv`; command examples should not assume `python` exists as an alias.

### What Will You Try Next Time
Probe `.venv/bin/python`, then `venv/bin/python`, then `python3` before running Python verification commands.

## Backtick Search Pattern Broke Shell Quoting

### What Was The Error
An `rg` command used a double-quoted search pattern containing Markdown backticks, and Bash treated the backtick as command substitution, causing an unexpected EOF syntax error.

### How Did You Fix It
Rerun the search with a safer pattern that does not include raw backticks inside shell quotes.

### What Did Not Work
Putting Markdown snippets with backticks directly inside a double-quoted shell argument was fragile.

### What Did You Learn
Documentation text often includes shell-special characters; searches for it should use single quotes or simpler substrings.

### What Will You Try Next Time
Search for plain words first, or wrap the pattern in single quotes when it contains Markdown backticks.
