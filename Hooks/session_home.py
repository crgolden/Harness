r"""Where a session keeps its own state: its parking lot (parking-lot/<session>) and its work-product declaration
(work-products/<session>.json), under %TEMP%\claude (claude_temp.claude_root(): CLAUDE_CODE_TMPDIR when set), and
nowhere else. .claude is a protected directory whose writes no allow rule pre-approves, so no state goes there.

Disk Cleanup deletes a temp file idle for seven days by its last-access time, which the server does not update on a
read, so every read sets the access time of a state file not accessed for a day (keep_fresh).

The signatures keep the workspace and transcript arguments every caller passes, so a caller never has to know where
the state lives.
"""

import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402

CHECKOUT_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PARKING_LOT_NAME = "parking-lot"
WORK_PRODUCTS_NAME = "work-products"
FRESH_SECONDS = 24 * 60 * 60


def state_root():
    """%TEMP%\\claude, where every session keeps its state."""
    return claude_temp.claude_root()


def home(workspace=None, transcript_path=None, session_id=None):
    return state_root()


def roots(workspace=None, transcript_path=None, session_id=None):
    return [state_root()]


def keep_fresh(path, now=None):
    """Set the access time of a state file, or of every file in a state folder, last accessed more than a day ago,
    so Disk Cleanup never sees it idle. The write time is kept. Any failure leaves the file as it was."""
    now = time.time() if now is None else now
    files = [path] if os.path.isfile(path) else glob.glob(os.path.join(path, "**", "*"), recursive=True)
    for name in files:
        try:
            stat = os.stat(name)
            if os.path.isfile(name) and now - stat.st_atime > FRESH_SECONDS:
                os.utime(name, ns=(int(now * 1e9), stat.st_mtime_ns))
        except OSError:
            continue


def parking_root(root=None):
    """The folder holding every session's parking lot."""
    return os.path.join(root or state_root(), PARKING_LOT_NAME)


def parking_dir(root, session_id):
    return os.path.join(parking_root(root), str(session_id))


def parking_dirs(workspace, session_id, transcript_path=None):
    path = parking_dir(state_root(), session_id)
    keep_fresh(path)
    return [path]


def all_parking_dirs(workspace=None):
    """Every session's parking-lot folder."""
    return [path for path in glob.glob(os.path.join(parking_root(), "*")) if os.path.isdir(path)]


def work_products_dir(root=None):
    return os.path.join(root or state_root(), WORK_PRODUCTS_NAME)


def declaration_path(workspace, session_id, transcript_path=None):
    path = os.path.join(work_products_dir(), str(session_id) + ".json")
    keep_fresh(path)
    return path


def main_checkout(checkout=CHECKOUT_ROOT):
    """The main checkout a git worktree belongs to (its .git file names <main>\\.git\\worktrees\\<name>), else the
    checkout itself."""
    try:
        with open(os.path.join(checkout, ".git"), encoding="utf-8") as handle:
            line = handle.readline().strip()
    except OSError:
        return checkout
    if not line.startswith("gitdir:"):
        return checkout
    gitdir = os.path.normpath(os.path.join(checkout, line[len("gitdir:"):].strip()))
    if os.path.basename(os.path.dirname(gitdir)).lower() != "worktrees":
        return checkout
    return os.path.dirname(os.path.dirname(os.path.dirname(gitdir)))


def workspace_root(checkout=CHECKOUT_ROOT, environ=None):
    """The folder of repos this Tools checkout serves: CRGOLDEN_WORKSPACE when set, else the folder the main Tools
    checkout sits in. Tools requires neither: a clone anywhere else resolves to its own parent, which holds no
    consumer repos, and every reader treats a repo it does not find there as absent."""
    environ = os.environ if environ is None else environ
    return environ.get("CRGOLDEN_WORKSPACE") or os.path.dirname(main_checkout(checkout))


def tools_checkout(checkout=CHECKOUT_ROOT, environ=None):
    """The workspace's Tools checkout, which holds the skills, the master session and the coordination scripts the
    hooks reach: every hook and harness takes a skill's path from here, never from beside its own folder."""
    return os.path.join(workspace_root(checkout, environ), "Tools")


def workspace_doc(relative, workspace=None, environ=None, must_exist=True):
    """The path of one fleet doc the workspace hands Tools (its AGENTS folder, a file in it, AGENTS.md or CLAUDE.md),
    else None when the workspace holds no such file or folder and `must_exist` (a writer creating the doc passes
    False). This is the one place Tools names the workspace's doc layout: every reader takes its path from here,
    and test_main_checkout_gate.py fails a path to those docs built from a workspace anywhere else."""
    parts = (relative or "").replace("\\", "/").strip("/").split("/")
    if not parts or parts[0] not in WORKSPACE_DOCS or (parts[0] != "AGENTS" and len(parts) > 1) \
            or any(part in ("", ".", "..") for part in parts):
        raise ValueError("not a workspace doc: {0!r}".format(relative))
    path = os.path.join(workspace or workspace_root(environ=environ), *parts)
    return path if not must_exist or os.path.exists(path) else None


WORKSPACE_DOCS = ("AGENTS", "AGENTS.md", "CLAUDE.md")


if __name__ == "__main__":
    if sys.argv[1:] == ["workspace"]:
        sys.stdout.write(workspace_root())
    elif len(sys.argv) == 3 and sys.argv[1] == "doc":
        found = workspace_doc(sys.argv[2])
        if found:
            sys.stdout.write(found)
        sys.exit(0 if found else 1)
    else:
        sys.stderr.write("usage: session_home.py workspace | doc <AGENTS/...|AGENTS.md|CLAUDE.md>\n")
        sys.exit(2)
