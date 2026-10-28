"""What a work-product item names: the repo files its edit changes (Harness/AGENTS/GATES.md, "Declaring the work product":
an item is an edit, never an outcome, and names the file or files it changes).

Shared by invoke_gate_cadence_gate.py, which refuses a declaration whose item names no file when it is written and
again when a gate launches, and by parking_lot_commits.py, which owes a commit message only for a checkout holding
files the session's items name. One reader, so the write, the launch and the parking lot cannot disagree."""

import re

RE_TOKEN = re.compile(r"[^\s`'\"(),;:<>\[\]{}|*]+")
RE_FILE_NAME = re.compile(
    r"^(?:[\w-]+(?:\.[\w-]+)*\.(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{1,10}|\.[A-Za-z][\w.-]*)$")


def item_paths(item):
    """Every file the item names, forward slashes: a token whose last segment is a file name with an extension
    holding a letter (App.tsx, Login.cshtml, gate.ps1) or a dotfile (.gitignore). A version (10.0.13) is no file."""
    paths = []
    for token in RE_TOKEN.findall(item or ""):
        token = token.replace("\\", "/").rstrip(".")
        if token.startswith("./"):
            token = token[2:]
        if RE_FILE_NAME.match(token.rsplit("/", 1)[-1]):
            paths.append(token)
    return paths


def names_path(item, paths):
    """True when the item names, by full path or by file name, one of the repo-relative `paths`."""
    for token in item_paths(item):
        if any(path == token or path.endswith("/" + token) for path in paths):
            return True
    return False
