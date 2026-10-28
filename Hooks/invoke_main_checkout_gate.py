r"""PreToolUse gate: no session edits the Tools checkout on main, which every session runs and shares.

The live gates, hooks, skills and scripts run from the Tools checkout on main. Every change to it is made on a branch
in the session's own worktree and reaches main only when the operator merges it (Tools/AGENTS.md, "Every Tools change
is made in a worktree"), so two sessions never edit, gate and message one shared tree at once. This gate keeps the
running copy out of a session's reach:

  deny  an Edit, Write or NotebookEdit of any path in the checkout, a skill reached through a skills junction
        included, since the path is resolved;
  deny  an Edit or Write after which settings.json's hooks block differs, or settings.json no longer parses,
        since which hooks run is the operator's to change;
  deny  a file-tool write to a quarantine record, so a quarantined session cannot free itself;
  deny  a file-tool write under the Stop gate's state directory (claude_temp.stop_gate_state_root()), so no
        session forges its block count, its delivered asks or the decision log judged from;
  deny  a file-tool write under the gate records (claude_temp.gate_records_root()), so no session forges the
        PASSED record the cadence and Stop gates read; only GateCommon.ps1 writes one;
  deny  a file-tool write under the hooks' transcript index (claude_temp.transcript_index_root()), so no session
        hides a transcript line (an operator message, a refused write, a /goal) from the readers that use it;
  deny  an Edit, Write or NotebookEdit of any path in the Harness main checkout (harness_scope.in_main_checkout), for
        every session, the master included: the master changes Harness in its own worktree (Harness/AGENTS.md).

A settings.json edit that changes the hooks block, and a write to a quarantine record, the Stop gate's state, a gate
record or the transcript index, are also a gate change
(gate_change()): the dispatcher quarantines the session that tried it (session_quarantine.py) and refuses its every
call from then on. A write in the checkout is denied but never ejects, since editing the wrong tree is a mistake, not an attempt on the
gate. Shell commands are not judged: running a gate's tests from the checkout is ordinary work, and text cannot tell
a read from a write. Any change must be verified with Harness/Hooks/test_main_checkout_gate.py.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session_quarantine import QUARANTINE_ROOT  # noqa: E402
import claude_temp  # noqa: E402
import harness_scope  # noqa: E402
import session_home  # noqa: E402

STOP_GATE_STATE_ROOT = claude_temp.stop_gate_state_root()
GATE_RECORDS_ROOT = claude_temp.gate_records_root()
TRANSCRIPT_INDEX_ROOT = claude_temp.transcript_index_root()

DENY = "deny"
DEFER = "defer"
UNPARSABLE = object()

SETTINGS = os.path.join(os.environ.get("USERPROFILE", ""), ".claude", "settings.json")
TOOLS_CHECKOUT = session_home.main_checkout()

WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")

CHECKOUT_REASON = (
    "Blocked: {target} is in the Tools checkout on main, which every session runs and shares, so every Tools change "
    "is made in a worktree. Make the change in this session's one worktree, on the branch named by its session id: git -C \"{checkout}\" worktree "
    "add -b <session-id> \"{worktrees}\\<session-id>\\Tools\" origin/main, with <session-id> written out literally in "
    "both places (it is $env:CLAUDE_CODE_SESSION_ID, the name of this session's transcript file). Gate it there, and "
    "give the operator the branch to commit, push and merge as a pull request (Tools/AGENTS.md)."
)
WORKTREES = os.path.join(os.environ.get("USERPROFILE", ""), "source", "worktrees")

SETTINGS_REASON = (
    "Blocked: this edit {why} ({settings}). Which hooks judge a session is the operator's to change, never a "
    "session's; give the operator the change instead."
)


def resolved_spellings(path):
    """The path's every spelling on the state share (claude_temp.state_spellings), each resolved and long: a mapped
    drive resolves to its UNC form, so a fence written against D:\\crgolden-state holds for a write through Z: too."""
    forms = set()
    for spelling in claude_temp.state_spellings(claude_temp.long_path(os.path.abspath(path))):
        for form in (spelling, os.path.realpath(spelling)):
            forms.add(os.path.normcase(claude_temp.long_path(form)).rstrip(os.sep))
    return forms


def under(path, root):
    try:
        targets, bases = resolved_spellings(path), resolved_spellings(root)
    except (TypeError, ValueError, OSError):
        return False
    return any(target == base or target.startswith(base + os.sep) for target in targets for base in bases)


def guarded_target(target, checkout=TOOLS_CHECKOUT):
    return under(target, checkout)


def same_file(path, other):
    try:
        return os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(other))
    except (TypeError, ValueError, OSError):
        return False


def hooks_block(text):
    """The parsed hooks object of a settings.json text, or UNPARSABLE."""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return UNPARSABLE
    return parsed.get("hooks") if isinstance(parsed, dict) else UNPARSABLE


def settings_after(tool_name, tool_input, settings):
    """The text settings.json would hold after this Edit or Write, or None when it cannot be computed."""
    if tool_name == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None
    try:
        with open(settings, "r", encoding="utf-8") as handle:
            current = handle.read()
    except OSError:
        return None
    old, new = tool_input.get("old_string"), tool_input.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str) or old not in current:
        return None
    return current.replace(old, new) if tool_input.get("replace_all") else current.replace(old, new, 1)


def gate_change(tool_name, tool_input, settings=SETTINGS):
    """Why this call changes which hooks judge sessions, or None: an Edit or Write after which settings.json's hooks
    block differs, or after which settings.json no longer parses, which would switch every hook off."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool_name not in WRITE_TOOLS:
        return None
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(target, str):
        return None
    if under(target, QUARANTINE_ROOT):
        return "wrote to a quarantine record: {0}".format(target)
    if under(target, STOP_GATE_STATE_ROOT):
        return "wrote to the Stop gate's own state: {0}".format(target)
    if under(target, GATE_RECORDS_ROOT):
        return "wrote to a gate record, which only a gate writes: {0}".format(target)
    if under(target, TRANSCRIPT_INDEX_ROOT):
        return "wrote to the hooks' transcript index: {0}".format(target)
    if not same_file(target, settings):
        return None
    after = settings_after(tool_name, tool_input, settings)
    if after is None:
        return None
    try:
        with open(settings, "r", encoding="utf-8") as handle:
            before = hooks_block(handle.read())
    except OSError:
        before = UNPARSABLE
    changed = hooks_block(after)
    if changed is UNPARSABLE:
        return "made settings.json unparsable, which switches every hook off"
    if changed != before:
        return "changed the hooks block of settings.json"
    return None


def evaluate(tool_name, tool_input, checkout=TOOLS_CHECKOUT, settings=SETTINGS, harness=None):
    """(decision, reason). Split out so the harness and the dispatcher call it in-process; `harness` names a fixture
    Harness main checkout in place of <workspace>\\Harness."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool_name not in WRITE_TOOLS:
        return DEFER, ""
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if isinstance(target, str) and guarded_target(target, checkout):
        return DENY, CHECKOUT_REASON.format(target=target, checkout=checkout, worktrees=WORKTREES)
    if isinstance(target, str) and harness_scope.in_main_checkout(target, harness):
        return DENY, harness_scope.MAIN_CHECKOUT_REASON.format(target=target, checkout=harness or harness_scope.main_checkout(),
                                                              worktrees=WORKTREES)
    why = gate_change(tool_name, tool_input, settings)
    if why:
        return DENY, SETTINGS_REASON.format(why=why, settings=settings)
    return DEFER, ""
