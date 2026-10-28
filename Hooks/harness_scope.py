r"""Where the Harness repository is, and who may reach it: the master session alone (Harness/AGENTS.md, "Operating rules").

Harness holds the hooks and the gate library that judge every session, so a session that can read it holds a map of
its own exits. Three gates share this module and deny every session but the master:

  the read-scope gate       a Read, Grep or Glob of a path in Harness, and an EnterWorktree that creates or enters a
                            worktree of it;
  the PreToolUse shell gate a PowerShell command naming a path in Harness, a git worktree add of it included;
  the main-checkout gate    a file-tool write on the Harness main checkout, which every session's hooks run from, for
                            every session, the master included: the master changes Harness in its own worktree.

What stays open to every session: the two scripts every session runs by name (Gates\gate_status.py, which the
gate-status skill runs, and Hooks\session_home.py, which a repo's gate.ps1 runs); GateCommon.ps1, which a repo's
gate.ps1 loads through GATE_LIBRARY without naming it; and the rules card, which the SessionStart hook puts in context.
The skills stay in the Tools repository and are read there. A junction anywhere that resolves into Harness is a route
into it and is denied, since every spelling of a path is judged.

The master is told apart by master_session.runs_as: MASTER_SESSION=1 in the hook's environment on the master host, which
no tool call can set. Verified by the harnesses of the three gates (test_read_scope_gate.py, test_pretooluse_gate.py,
test_main_checkout_gate.py), each with must-block and control rows against a fixture Harness.
"""

import os
import re
import sys

HOOKS = os.path.dirname(os.path.abspath(__file__))
if HOOKS not in sys.path:
    sys.path.insert(0, HOOKS)
import claude_temp  # noqa: E402
import master_session  # noqa: E402
import session_home  # noqa: E402

HARNESS_NAME = "Harness"
ENTRY_SCRIPTS = frozenset({
    ("gates", "gate_status.py"),
    ("hooks", "session_home.py"),
})
RE_WORKTREE_OF_HARNESS = re.compile(r"[\\/]source[\\/]worktrees[\\/][^\\/]+[\\/]harness(?=[\\/]|$)", re.IGNORECASE)
RE_PATH_TOKEN = re.compile(
    r"\"[A-Za-z]:[^\"]+\"|'[A-Za-z]:[^']+'|[A-Za-z]:[\\/][^\s\"']+"
    r"|\$env:\w+[\\/][^\s\"']*|(?<![\w.-])~(?:[\\/][^\s\"']*)?"
    r"|(?<![\w.:-])Harness(?=[\s\"']|$)|(?<![\w\"'=:])[^\s\"'=|;&<>]*[\\/][^\s\"']*")
RE_ENV_PREFIX = re.compile(r"^\$env:(\w+)", re.IGNORECASE)

READ_REASON = (
    "Blocked: {path} is in the Harness repository, which holds the hooks and gates that judge every session, and only "
    "the master session reads it (Harness/AGENTS.md, Operating rules). The skills stay in Tools, the rules reach every "
    "session through the SessionStart context, and a hook defect is reported on the coordination board for the master "
    "to take."
)
WORKTREE_REASON = (
    "Blocked: this creates or enters a worktree of the Harness repository ({path}), which only the master session does, "
    "in its own worktree at %USERPROFILE%\\source\\worktrees\\<session-id>\\Harness (Harness/AGENTS.md, Operating rules). "
    "Every other session reports a hook defect on the coordination board for the master to take."
)
COMMAND_REASON = (
    "Blocked: this command names {path} in the Harness repository, which only the master session reaches "
    "(Harness/AGENTS.md, Operating rules). A session runs the named entry scripts (Gates\\gate_status.py, "
    "Hooks\\session_home.py) by name, and a repo's gate.ps1 loads GateCommon.ps1 through GATE_LIBRARY; everything else "
    "in Harness is the master's."
)
MAIN_CHECKOUT_REASON = (
    "Blocked: {target} is in the Harness main checkout, which every session's hooks run from, so nothing is edited there, "
    "the master included. The master makes the change in its own worktree: git -C \"{checkout}\" worktree add -b "
    "<session-id> \"{worktrees}\\<session-id>\\Harness\" origin/main, gates it there, and the operator merges it as a "
    "pull request; any other session reports the defect on the coordination board (Harness/AGENTS.md, Operating rules)."
)


def is_master(environ=None):
    return master_session.runs_as(master_session.MASTER_SESSION_VARIABLE, environ)


def main_checkout(workspace=None):
    """The Harness main checkout: <workspace>\\Harness."""
    return os.path.join(workspace or session_home.workspace_root(), HARNESS_NAME)


def normal(path):
    return os.path.normcase(claude_temp.long_path(os.path.abspath(path))).rstrip("\\/")


def spellings(path):
    """Every spelling of a path: as given, long, and resolved through any junction."""
    forms = {normal(path)}
    try:
        forms.add(os.path.normcase(os.path.realpath(claude_temp.long_path(os.path.abspath(path)))).rstrip("\\/"))
    except (OSError, ValueError):
        pass
    return forms


def under(form, root):
    root = normal(root)
    return form == root or form.startswith(root + os.sep)


def in_main_checkout(path, harness=None):
    """True when a spelling of the path lies under the Harness main checkout."""
    try:
        return any(under(form, harness or main_checkout()) for form in spellings(path))
    except (TypeError, ValueError, OSError):
        return False


def in_harness(path, harness=None):
    """True when a spelling of the path lies under the Harness main checkout or under a Harness worktree."""
    try:
        forms = spellings(path)
    except (TypeError, ValueError, OSError):
        return False
    return any(under(form, harness or main_checkout()) or RE_WORKTREE_OF_HARNESS.search(form) for form in forms)


def entry_script(path):
    """True when the path names one of the scripts every session runs by name, wherever its checkout is."""
    try:
        full = os.path.normcase(os.path.abspath(path))
    except (TypeError, ValueError):
        return False
    folder, name = os.path.basename(os.path.dirname(full)), os.path.basename(full)
    return (folder, name) in ENTRY_SCRIPTS


def command_paths(command, cwd=None, environ=None):
    """Every path-shaped token of a command, absolute: a drive path, quoted or not, a $env:NAME path, a ~ path, a bare
    Harness token, or any token holding a path separator, resolved against cwd (the session's working directory) when
    relative. With no cwd a relative token names nothing judgeable and is left out, never resolved against the hook's
    own process directory. A URL resolves to a path under cwd that names nothing."""
    environ = os.environ if environ is None else environ
    found = []
    for token in RE_PATH_TOKEN.findall(command or ""):
        token = token.strip("\"'")
        if not token:
            continue
        prefixed = RE_ENV_PREFIX.match(token)
        if prefixed:
            token = environ.get(prefixed.group(1), "") + token[prefixed.end():]
        elif token.startswith("~"):
            token = os.path.join(environ.get("USERPROFILE", ""), token[2:] if len(token) > 1 else "")
        elif not os.path.isabs(token) and not re.match(r"^[A-Za-z]:", token):
            if not cwd:
                continue
            token = os.path.join(cwd, token)
        found.append(token)
    return found


def denied_command_path(command, cwd=None, harness=None, environ=None):
    """The path that makes a command the master's alone, else None: the working directory itself when it lies in
    Harness (a relative command then runs there), or the first path the command names in Harness that is not a named
    entry script."""
    if isinstance(cwd, str) and cwd.strip() and in_harness(cwd, harness):
        return cwd
    for path in command_paths(command, cwd, environ):
        if in_harness(path, harness) and not entry_script(path):
            return path
    return None
