r"""PreToolUse gate: Read, Grep and Glob never touch a path whose read would ask the operator for approval: a temp path
outside %TEMP%\claude, or the state share named by its UNC spelling.

Claude Code asks the operator before a file tool reads outside the working directories and the allow rules, and a
prompt freezes an unattended or busy session until the operator answers, which this workspace never accepts. The allow
rules in ~/.claude/settings.json cover every file under %TEMP%\claude (Read(~/AppData/Local/Temp/claude/**) and
Read(~/AppData/Local/Temp/*/claude/**)) and the share's drive spellings (Read(//z/**), Read(//d/crgolden-state/**)),
so every session may read every file there: its own, another session's, the gate records, the audits, the handoffs
and the Stop gate's own decisions, which is how a session finds a bug in a hook. What no rule covers is a blocklist:

  deny  Read of a file, or Grep or Glob of a path, under the temp folder but outside its claude folder (another
        program's files, the temp folder itself), in its long or realpath spelling.
  deny  Read, Grep or Glob of the state share through its UNC spelling, for every account; the reason names the
        spelling the allow rules match: the server's disk path on the server, the mapped drive elsewhere.

  deny  Read, Grep or Glob of a path in the Harness repository (harness_scope.py: its main checkout or a worktree of
        it, in any spelling, a junction into it included) for every session but the master, and an EnterWorktree
        that enters a Harness worktree by path or creates one from a working directory in Harness. Harness holds the
        hooks and gates that judge every session.

An 8.3 spelling is the short-path gate's (invoke_short_path_gate.py). Everything else defers to the normal permission
flow. Wiring: PRETOOLUSE-HOOK.md, "The read-scope gate". Any change must be verified with
Harness/Hooks/test_read_scope_gate.py.
"""

import json
import os
import sys
import tempfile

HOOKS = os.path.dirname(os.path.abspath(__file__))
if HOOKS not in sys.path:
    sys.path.insert(0, HOOKS)
import claude_temp  # noqa: E402
from claude_temp import long_path  # noqa: E402,F401
import harness_scope  # noqa: E402

DENY = "deny"
DEFER = "defer"

FILE_TOOLS = ("Read", "Grep", "Glob")
WORKTREE_TOOLS = ("EnterWorktree",)
CLAUDE_FOLDER = "claude"

REASON = (
    "Blocked: {path} is in the temp folder outside {root}, where no allow rule reaches, so reading it asks the "
    "operator for approval and freezes this session until they answer. Every script's and session's state is under "
    "{root}, and every file there is readable: read it there."
)
UNC_REASON = (
    "Blocked: {path} names the state share by its UNC spelling, which no allow rule matches, so reading it asks the "
    "operator for approval and freezes this session until they answer. Read the same file as {drive}."
)
FOREIGN_SHARE_REASON = (
    "Blocked: {path} names the state share by its UNC spelling, which no allow rule matches, and the share is not this "
    "account's: only the accounts its config lists sign in to it. This account's state is under {root}."
)


def spellings(path):
    """Every spelling of the path: as given, long and resolved."""
    full = os.path.abspath(path)
    return {os.path.normcase(form) for form in (full, long_path(full), os.path.realpath(full))}


def profile_temp():
    local = os.environ.get("LOCALAPPDATA", "")
    return os.path.join(local, "Temp") if local else ""


def temp_roots():
    """Every spelling of the temp folders: this process's (on a Remote Desktop server, the per-session Temp\\<n>) and
    the profile's own Temp above it, which prompts just the same."""
    roots = set()
    for candidate in (tempfile.gettempdir(), os.environ.get("TEMP", ""), os.environ.get("TMP", ""), profile_temp()):
        if candidate:
            roots |= spellings(candidate)
    return {root.rstrip("\\/") for root in roots if root}


def relative_parts(form, root):
    """The segments of a spelling below a root, or None when it is not under it; [] for the root itself."""
    if form == root:
        return []
    if form.startswith(root + os.sep):
        return form[len(root) + 1:].split(os.sep)
    return None


def under_temp(path, roots=None):
    """True for a path in the temp folder."""
    roots = temp_roots() if roots is None else roots
    return any(relative_parts(form, root) is not None for form in spellings(path) for root in roots)


def in_claude_folder(path):
    """Whether a spelling of the path lies where the allow rules reach: <temp>\\claude under any temp folder, or
    <profile Temp>\\<one folder>\\claude, the Remote Desktop server's per-session spelling."""
    profile = {root.rstrip("\\/") for root in spellings(profile_temp())} if profile_temp() else set()
    for form in spellings(path):
        for root in temp_roots():
            parts = relative_parts(form, root)
            if parts and parts[0] == CLAUDE_FOLDER:
                return True
            if parts and root in profile and len(parts) > 1 and parts[1] == CLAUDE_FOLDER:
                return True
    return False


def unc_share_drive(path):
    """The spelling an allow rule matches for a path named through the state share's UNC spelling, or None for any
    other path: the server's own disk path on the server, the mapped drive elsewhere. No allow rule matches the UNC
    spelling for any account, enabled or not."""
    config = claude_temp.share_config()
    server, share = config.get("server"), config.get("share")
    if not (server and share):
        return None
    unc = os.path.normcase("\\\\" + server + "\\" + share)
    full = os.path.abspath(path)
    parts = relative_parts(os.path.normcase(full), unc)
    if parts is None:
        return None
    on_server = os.environ.get("COMPUTERNAME", "").upper() == str(server).upper()
    drive = config.get("drive")
    base = config.get("serverPath") if on_server or not drive else drive.rstrip(":") + ":\\"
    if not base:
        return None
    rest = full[len(unc) + 1:] if parts else ""
    return os.path.join(base, rest) if rest else base


def share_user():
    """Whether this account is one the share's config lists, the only accounts that sign in to it."""
    users = claude_temp.share_config().get("users", [])
    return os.environ.get("USERNAME", "").lower() in {str(name).lower() for name in users}


def glob_base(pattern, folder):
    """The folder a Glob pattern reaches before its first wildcard, resolved against the folder it searches: an
    absolute pattern, or one climbing out with .., names its own root whatever path says."""
    wild = min([pattern.find(mark) for mark in "*?[{" if mark in pattern] or [len(pattern)])
    prefix = os.path.dirname(pattern[:wild].rstrip("/\\") + os.sep) if wild < len(pattern) else pattern
    return os.path.normpath(os.path.join(folder, prefix)) if prefix else folder


def evaluate(tool_name, tool_input, cwd=None, session_id=None, environ=None, harness=None):
    """(decision, reason). Split out so the harness and the dispatcher call it in-process; `harness` names a fixture
    Harness main checkout in place of <workspace>\\Harness."""
    if not isinstance(tool_input, dict):
        return DEFER, ""
    if tool_name in WORKTREE_TOOLS:
        if harness_scope.is_master(environ):
            return DEFER, ""
        path = tool_input.get("path")
        reached = None
        if isinstance(path, str) and path.strip():
            reached = path if harness_scope.in_harness(path, harness) else None
        elif isinstance(cwd, str) and cwd.strip() and harness_scope.in_harness(cwd, harness):
            reached = cwd
        return (DENY, harness_scope.WORKTREE_REASON.format(path=reached)) if reached else (DEFER, "")
    if tool_name not in FILE_TOOLS:
        return DEFER, ""
    target = tool_input.get("file_path") if tool_name == "Read" else tool_input.get("path")
    targets = [target] if isinstance(target, str) and target.strip() else []
    pattern = tool_input.get("pattern")
    if tool_name == "Glob" and isinstance(pattern, str) and pattern.strip():
        folder = targets[0] if targets else (cwd or os.getcwd())
        targets.append(glob_base(pattern, folder))
    try:
        for path in targets:
            if harness_scope.in_harness(path, harness) and not harness_scope.is_master(environ):
                return DENY, harness_scope.READ_REASON.format(path=path)
            drive = unc_share_drive(path)
            if drive and not share_user():
                return DENY, FOREIGN_SHARE_REASON.format(path=path, root=claude_temp.raw_claude_root())
            if drive:
                return DENY, UNC_REASON.format(path=path, drive=drive)
            if under_temp(path) and not in_claude_folder(path):
                return DENY, REASON.format(path=path, root=claude_temp.raw_claude_root())
    except (TypeError, ValueError):
        return DEFER, ""
    return DEFER, ""


def main():
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    decision, reason = evaluate(payload.get("tool_name"), payload.get("tool_input"), payload.get("cwd"),
                                payload.get("session_id"))
    if decision == DENY:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": DENY,
                                                            "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
