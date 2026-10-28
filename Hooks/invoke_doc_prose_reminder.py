"""PostToolUse reminder: an Edit or Write that adds prose to a permanent markdown doc puts the no-history rule
in front of the agent.

Context only, never a block, by operator decision. No detector separates an anecdote from a durable finding
(invoke_doc_state_gate.py cut three candidates on that evidence), so every added prose line in scope gets the
reminder, and the definitive prose_lint.py tells among those lines are quoted when present. PostToolUse, on
the same wiring as invoke_comment_reminder.py, whose added-line reader it reuses.

The rule is the docs-audit skill's criterion 3 and its pre-write test; the reminder names that file rather
than restating it. The status-bearing docs are exempt by the doc-state gate's own list, read by import. Working
docs are exempt by location: the temp tree (the session scratchpad sits inside it), ~/.claude/plans, the
memory directory, and the workspace session-state roots the doc-state gate names (SESSION_STATE_ROOTS: audit
runs, gate records, handoffs, work-product declarations). Banking progress there is the intended place for it.

FAIL-OPEN: anything unreadable emits nothing.
"""

import importlib.util
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from invoke_comment_reminder import added_lines  # noqa: E402
import claude_temp  # noqa: E402
import session_home  # noqa: E402
from invoke_doc_state_gate import EXEMPT_BASENAMES, EXEMPT_PREFIXES, FENCE, SESSION_STATE_ROOTS, under_root  # noqa: E402

PROSE_LINT = os.path.join(session_home.workspace_root(), "Tools", "Claude", "skills", "docs-audit", "scripts",
                          "prose_lint.py")
RULE_FILE = "Tools/Claude/skills/docs-audit/SKILL.md"
TRANSIENT_ROOTS = (tempfile.gettempdir(), claude_temp.root(),
                   os.path.join(os.path.expanduser("~"), ".claude", "plans")) + SESSION_STATE_ROOTS
TRANSIENT_DIR_PARTS = {"memory", "scratchpad"}
TELL_LIMIT = 5
LINE_LIMIT = 120

REMINDER = (
    "You just added {count} line(s) to {file}, a permanent doc. Permanent docs carry no anecdote, history or "
    "narrative: no dates or commit SHAs, no \"used to\", \"was measured\", \"previously\", no story of what a "
    "run, session or commit did, no status. Test each added line: strip every date, every \"earlier\" and "
    "every sentence about what happened; if the rule still reads, keep only that. Otherwise it is a violation "
    "of our standards (criterion 3 in {rule}): rewrite it as the rule or the trap, or delete it.\n"
    "Progress, findings and readings belong in your turn report or a transient doc (the scratchpad, a plan), "
    "never here."
)
TELLS_HEADER = "\nprose_lint.py tells in the added lines:"


def is_transient(file_path):
    if under_root(file_path, TRANSIENT_ROOTS):
        return True
    lowered = os.path.normcase(os.path.abspath(file_path))
    base = os.path.basename(lowered)
    if base in EXEMPT_BASENAMES or base.startswith(EXEMPT_PREFIXES):
        return True
    return bool(set(lowered.split(os.sep)) & TRANSIENT_DIR_PARTS)


def prose_lines(lines):
    in_fence = False
    for line in lines:
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and line.strip():
            yield line


def load_lint():
    try:
        spec = importlib.util.spec_from_file_location("prose_lint", PROSE_LINT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.scan_line
    except Exception:
        return None


def clip(text):
    return text if len(text) <= LINE_LIMIT else text[:LINE_LIMIT - 3] + "..."


def in_scope(payload):
    if payload.get("tool_name") not in ("Edit", "Write"):
        return False
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    file_path = tool_input.get("file_path")
    return isinstance(file_path, str) and file_path.lower().endswith(".md") and not is_transient(file_path)


def reminder(payload, scan_line=None):
    """The reminder text for this edit, or None."""
    if not in_scope(payload):
        return None
    file_path = payload["tool_input"]["file_path"]
    added = list(prose_lines(added_lines(payload)))
    if not added:
        return None
    message = REMINDER.format(count=len(added), file=file_path, rule=RULE_FILE)
    tells = []
    if scan_line is not None:
        tells = ["  [{0}] {1}  <- {2}".format(label, found, clip(line.strip()))
                 for line in added for label, found in scan_line(line)]
    if tells:
        message += TELLS_HEADER + "\n" + "\n".join(tells[:TELL_LIMIT])
    return message


def main():
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        message = reminder(payload, load_lint()) if in_scope(payload) else None
    except Exception:
        return 0
    if message:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}
        }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
