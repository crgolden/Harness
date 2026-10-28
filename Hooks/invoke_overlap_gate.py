r"""PreToolUse gate: no session changes a Tools file another session is changing without telling it first.

Each session makes every Tools change in its own worktree, %USERPROFILE%\source\worktrees\<session-id>\Tools, so
two sessions can each hold uncommitted edits to the same file and only find out when one rebases onto the other.
This gate makes the overlap visible before the edit:

  deny  an Edit, Write or NotebookEdit of a file in a Tools worktree while another worktree there holds an
        uncommitted change to the same path, until this session has sent a SendMessage naming that worktree's
        session id (its first eight characters are enough);
  deny  a "git worktree add" while any other worktree there holds an uncommitted change, on the same condition.

A session names a peer by its session id because the id never changes, while a session's display name does. Any
change must be verified with Harness/Hooks/test_overlap_gate.py.
"""

import json
import os
import re
import subprocess

DENY = "deny"
DEFER = "defer"

WORKTREES = os.path.join(os.environ.get("USERPROFILE", ""), "source", "worktrees")
CHECKOUT_NAME = "Tools"
ID_PREFIX_LENGTH = 8

WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")
SHELL_TOOLS = ("PowerShell",)
RE_WORKTREE_ADD = re.compile(r"\bgit(?:\.exe)?\b[^\n]*\bworktree\s+add\b", re.I)

OVERLAP_REASON = (
    "Blocked: {action}, but another session is changing the same Tools files in its own worktree, uncommitted:"
    "\n{overlaps}\nTell each owner before you change them: SendMessage the session whose id starts with the id shown, "
    "naming its id and the files, and agree who changes what. Then retry."
)


def worktrees(root=WORKTREES):
    """(session id, checkout path) for every Tools worktree under root."""
    found = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return found
    for name in names:
        checkout = os.path.join(root, name, CHECKOUT_NAME)
        if os.path.exists(os.path.join(checkout, ".git")):
            found.append((name, checkout))
    return found


def dirty_paths(checkout):
    """The repo-relative paths, with forward slashes, that git reports changed or untracked in checkout."""
    result = subprocess.run(["git", "-C", checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return set()
    entries = result.stdout.split("\0")
    paths = set()
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        paths.add(entry[3:].replace("\\", "/"))
        if entry[0] in "RC":
            index += 1
    return paths


def under(path, root):
    try:
        normalized = os.path.normcase(os.path.abspath(path))
        base = os.path.normcase(os.path.abspath(root))
    except (TypeError, ValueError):
        return False
    return normalized.startswith(base.rstrip(os.sep) + os.sep)


def locate(file_path, root=WORKTREES):
    """(session id, repo-relative path) of a file inside a Tools worktree under root, else None."""
    for owner, checkout in worktrees(root):
        if under(file_path, checkout):
            return owner, os.path.relpath(os.path.abspath(file_path), checkout).replace("\\", "/")
    return None


def announced(transcript_path, owner):
    """True when this session's transcript holds a SendMessage whose message names owner's session id."""
    prefix = owner[:ID_PREFIX_LENGTH]
    if not transcript_path or not prefix:
        return False
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if prefix not in line or '"SendMessage"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                content = (record.get("message") or {}).get("content") if isinstance(record, dict) else None
                for block in content if isinstance(content, list) else []:
                    if (isinstance(block, dict) and block.get("type") == "tool_use"
                            and block.get("name") == "SendMessage"
                            and prefix in str((block.get("input") or {}).get("message", ""))):
                        return True
    except OSError:
        return False
    return False


def overlaps(session_id, wanted, transcript_path, root=WORKTREES, status=dirty_paths):
    """[(owner, checkout, paths)] for every other worktree holding uncommitted paths this session wants and has not
    announced to that worktree's owner. `wanted(path)` decides which of a worktree's paths matter."""
    found = []
    for owner, checkout in worktrees(root):
        if session_id and owner == session_id:
            continue
        paths = sorted(path for path in status(checkout) if wanted(path))
        if paths and not announced(transcript_path, owner):
            found.append((owner, checkout, paths))
    return found


def describe(found):
    return "\n".join("  {0} ({1}): {2}".format(owner[:ID_PREFIX_LENGTH], checkout, ", ".join(paths))
                     for owner, checkout, paths in found)


def evaluate(tool_name, tool_input, transcript_path, session_id, root=WORKTREES, status=dirty_paths):
    """(decision, reason). Split out so the harness and the dispatcher call it in-process."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool_name in WRITE_TOOLS:
        target = tool_input.get("file_path") or tool_input.get("notebook_path")
        located = locate(target, root) if isinstance(target, str) else None
        if not located:
            return DEFER, ""
        found = overlaps(session_id, lambda path: path == located[1], transcript_path, root, status)
        action = "this edit changes {0}".format(located[1])
    elif tool_name in SHELL_TOOLS:
        command = tool_input.get("command")
        if not isinstance(command, str) or not RE_WORKTREE_ADD.search(command):
            return DEFER, ""
        found = overlaps(session_id, lambda path: True, transcript_path, root, status)
        action = "this command creates a worktree"
    else:
        return DEFER, ""
    if not found:
        return DEFER, ""
    return DENY, OVERLAP_REASON.format(action=action, overlaps=describe(found))
