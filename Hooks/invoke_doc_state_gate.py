"""PreToolUse gate: refuse to write operational state into a permanent markdown doc.

WHAT THIS ACTUALLY ENFORCES, AND WHAT IT CANNOT. It detects a mechanical TELL -- a shouted status
token beside a date, a heading asserting a present condition, a pasted HTTP header, a phrase naming
git or deploy state. It does NOT detect the three motives that produce those tells, and a green run
is not evidence the motives are handled. Read the motives in the operator-facing note below and in
the `docs-policy` memory; treat this gate as a floor, exactly like the reading-coverage gate next to
it, which raises the floor without being a comprehension test.

WHY IT EXISTS. `Tools/AGENTS/CODE-STYLE.md` and the `docs-policy` memory both already ban operational
state in durable docs, in full, with the operator's own escalation quoted ("why are you polluting
our permanent documentation with temporary infra state? you've done this multiple times now"). On
2026-09-10 an agent that had that memory loaded wrote "FIXED and live", "STILL OPEN - re-verified",
a `Last-Modified` header value and per-page counts into `Tools/SearchConsole/AGENTS.md` anyway. The
operator's diagnosis is the one that matters: "what you're doing doesn't even make sense and it's
more effort for you to continually disobey than to comply". Writing the reading costs MORE
keystrokes than omitting it, so laziness does not explain it, and a stronger prohibition would not
have helped -- the prohibition was already there and already specific. Three motives were named:

  1. BANKING PROOF-OF-WORK. Having just verified something, the agent pastes the reading into a
     durable doc so the effort is visible and permanent. It serves the writer, not the document,
     which is why it costs extra effort.
  2. CONFLATING THE TURN REPORT WITH THE REFERENCE DOC. Readings belong in what the agent tells the
     operator: ephemeral, actionable, discardable. Fear of losing them at compaction gets them
     "preserved" in the nearest .md. Measurements are MEANT to be re-derived; that is the entire
     reason the method is what gets written down.
  3. MISTAKING STATUS-TRACKING FOR HELPFULNESS. "DONE, live" / "STILL OPEN" feels like closing the
     loop. It converts a reference doc into an unaudited backlog that rots.

PRECISION WAS MEASURED BEFORE THIS SHIPPED, NOT AFTER. Six probe rounds over the real corpus (every
non-exempt .md under AGENTS/ and Tools/) cut three candidate checks that could not survive contact:
a thousand-separator scan fired 200 times on legitimate MC/DC truth-table rows and vendor quotas; a
"N rows/clicks/impressions" scan fired 81 times on the same tables; a bare verification-stamp scan
fired 46 times, overwhelmingly on the legitimate genre "Measured <date>: that theory is wrong",
where the date is provenance for a durable finding rather than a status snapshot. What survives
fires twice across 61 files, and both are genuine. The shipped shape is the INTERSECTION -- a stamp
only counts beside a status assertion -- and the announcement form: a SHOUTED or bolded token, not
lowercase narrative prose, because "that shipped on <date>" is a different defect (history) and
"(CLOSED <date> - reason)" is a legitimate design-log convention in the hook docs.

DENY, NOT ASK. The remedy is a judgement call ("rewrite the sentence"), which normally argues for
`ask`. It denies anyway because the measured false-positive rate is near zero, and because `ask`
would spend an operator prompt on every hit -- the exact approval spam the shell gate exists to
avoid. A check that cannot be made precise enough to deny is a check that is not ready; three were
cut on that rule rather than shipped as prompts.

SCOPE. Only .md files, and only the NEW text (`content` for Write, `new_string` for Edit) -- an
agent is not blocked for prose it is not touching. Docs whose CONTENT is status are exempt by name:
a backlog, an audit ledger, a review round, a decommission inventory, a coverage baseline. The
memory directory is point-in-time by definition, and the scratchpad is temporary. The session-state
folders under %TEMP%\\claude (`audits`, `crgolden-gates`, `handoffs`, `parking-lot`, `work-products`) hold run
ledgers, gate logs, handoffs and session state whose whole content is dated state, so they are exempt by root. The
workspace-scoped
skills beside them under `crgolden/.claude/skills` are permanent and stay in scope. `test_doc_state_gate.py`
asserts the exempt fixtures still exist, so retiring one surfaces as a failure rather than a silent
exemption gap.

FENCE STATE IS RECONSTRUCTED FOR AN EDIT. Every pattern here would fire inside a command example or
a JSON sample. Stripping fences within the new text is not enough, because an Edit sees a fragment
whose enclosing fence may have been opened outside it. The gate locates `old_string` in the target
file and counts the fences before it; landing inside a fence defers the whole check. When the file
cannot be read it falls back to treating the fragment as balanced, which is the fail-open direction.

FAIL-OPEN, DELIBERATELY, like the gate beside it. Anything unparseable defers.
"""

import json
import os
import re
import sys

DENY = "deny"
DEFER = "defer"

FENCE = re.compile(r"^\s*(?:```|~~~)")
INLINE_CODE = re.compile(r"`[^`]*`")
QUOTED = re.compile(r"\"[^\"]*\"|'[^']*'|“[^”]*”")

DATE = re.compile(
    r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})")

# Case-sensitive on purpose: an announcement shouts. Lowercase "that shipped on <date>" is
# narrative history, a different defect, and firing on it swamped the signal in probe round 4.
SHOUTED_STATUS = re.compile(r"\b(?:DONE|SHIPPED|NOT SHIPPED|STILL OPEN|HAS OPENED|NOT YET SHIPPED)\b")

# Phrases specific enough to fire without a date. Each names the writer's own git or deploy state,
# which the docs-policy memory lists as a banned shape in its own right.
STATE_PHRASE = re.compile(
    r"\b(?:ha(?:s|ve)\s+not\s+shipped|not\s+yet\s+(?:shipped|landed)"
    r"|is\s+now\s+(?:deployed|live)|live\s+in\s+production"
    r"|uncommitted\s+working\s+tree|pending\s+commit|already\s+(?:shipped|landed)"
    r"|is\s+currently\s+(?:failing|broken|down))\b", re.I)

# A heading is the worst place for a present condition, because a heading is what a reader greps.
HEADING_STATUS = re.compile(
    r"^\s{0,3}#{1,6}\s.*\b(?:DONE|SHIPPED|STILL OPEN|NOT SHIPPED|HAS OPENED|COMPLETE"
    r"|FIXED and live|is currently \w+|are still on)\b")

PASTED_HEADER = re.compile(r"\b(?:Last-Modified|ETag)\s*:\s*\S", re.I)

FINDINGS = {
    "dated-status": (
        "a status word beside a date",
        "State what is true regardless of when it was read, or delete the line. If the reader needs "
        "the current state, give them the command that re-derives it."),
    "git-deploy-state": (
        "your own git or deploy state",
        "This is false the moment someone commits or deploys. Describe the behaviour, and point at "
        "the live source or the command that shows the state."),
    "heading-status": (
        "a heading asserting a present condition",
        "A heading cannot survive its own fix, and it is what a future reader greps. Name the "
        "subject, not its status."),
    "pasted-header": (
        "a pasted HTTP response header",
        "That is a reading from one request. Keep the method that produced it, never the value."),
}

# Docs whose content IS status. Grounded in the real listing, not invented; the harness asserts they
# still exist so retiring one fails a test instead of silently widening the gate.
EXEMPT_BASENAMES = {
    "config_audit.md", "lifecycle_audit.md",
    "recovery-notes.md", "claude-recovery.md",
}
EXEMPT_PREFIXES = ("changelog", "example-")
EXEMPT_DIR_PARTS = {"retired", "coverage", "audit-catalogs", "memory", "scratchpad"}
if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_home  # noqa: E402

WORKSPACE = session_home.workspace_root()
TOOLS_CHECKOUT = session_home.tools_checkout()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402

SESSION_STATE_ROOTS = tuple(os.path.join(claude_temp.claude_root(), name)
                            for name in ("audits", "crgolden-gates", "handoffs", "parking-lot", "work-products"))

REASON_TEMPLATE = (
    "Blocked: this edit to {path} writes {what} into a permanent doc.\n\n"
    "  {line}\n\n"
    "{remedy}\n\n"
    "Operational state (a reading, a date, a ship/deploy state) goes stale without anything "
    "detecting it, and reads as fact to the next agent. Readings belong in what you tell the "
    "operator this turn, not in a file. Write the METHOD and the TRAP, never the reading.\n\n"
    "If this doc's CONTENT is genuinely status - a backlog, an audit ledger, a review round - it "
    "belongs on the exempt list in Harness/Hooks/invoke_doc_state_gate.py, which is a deliberate "
    "decision to make, not a workaround to reach for. Any change to this hook must be verified with "
    "Harness/Hooks/test_doc_state_gate.py."
)


def under_root(file_path, roots):
    normalized = os.path.normcase(os.path.abspath(file_path))
    return any(normalized.startswith(os.path.normcase(os.path.abspath(root)).rstrip(os.sep) + os.sep)
               for root in roots)


def is_exempt(file_path):
    if under_root(file_path, SESSION_STATE_ROOTS):
        return True
    lowered = file_path.lower()
    base = os.path.basename(lowered)
    if base in EXEMPT_BASENAMES or base.startswith(EXEMPT_PREFIXES):
        return True
    parts = set(lowered.replace("/", os.sep).split(os.sep))
    return bool(parts & EXEMPT_DIR_PARTS)


def opens_unclosed_fence(text):
    """True when `text` leaves a markdown code fence open."""
    return sum(1 for line in text.splitlines() if FENCE.match(line)) % 2 == 1


def starts_inside_fence(file_path, old_string):
    """True when the region an Edit replaces sits inside a fenced block opened earlier in the file.

    Falls back to False (check the text) when the file or the anchor cannot be read, which is the
    fail-open direction: a missed violation beats blocking a legitimate edit to a code sample.
    """
    if not old_string or not file_path or not os.path.isfile(file_path):
        return False
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as handle:
            body = handle.read()
    except Exception:
        return False
    index = body.find(old_string)
    if index == -1:
        return False
    return opens_unclosed_fence(body[:index])


def scannable_lines(text):
    """Yield prose lines with fenced blocks, inline code and quoted spans removed."""
    in_fence = False
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        yield QUOTED.sub("", INLINE_CODE.sub("", line)), line


def find_violation(text):
    """The first (finding, offending source line) in `text`, or (None, None)."""
    for stripped, original in scannable_lines(text):
        if not stripped.strip():
            continue
        if HEADING_STATUS.search(stripped):
            return "heading-status", original
        if PASTED_HEADER.search(stripped):
            return "pasted-header", original
        if SHOUTED_STATUS.search(stripped) and DATE.search(stripped):
            return "dated-status", original
        if STATE_PHRASE.search(stripped):
            return "git-deploy-state", original
    return None, None


def evaluate(file_path, new_text, old_string=None):
    """The whole decision as data: (decision, reason). Split out so the harness can call it."""
    if not file_path or not isinstance(new_text, str) or not new_text.strip():
        return DEFER, ""
    if not file_path.lower().endswith(".md"):
        return DEFER, ""
    if is_exempt(file_path):
        return DEFER, ""
    if starts_inside_fence(file_path, old_string):
        return DEFER, ""

    finding, line = find_violation(new_text)
    if finding is None:
        return DEFER, ""

    what, remedy = FINDINGS[finding]
    return DENY, REASON_TEMPLATE.format(
        path=file_path, what=what, line=line.strip()[:200], remedy=remedy)


def work_product_widening(file_path, tool_input, transcript_path):
    """invoke_gate_cadence_gate.py owns what the work product is; a widened declaration is denied here
    because this hook already sees every Edit and Write. A declaration the module cannot judge is denied too,
    so a broken import never waves a widening through."""
    if "work-products" not in str(file_path).replace("\\", "/").lower():
        return None
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import invoke_gate_cadence_gate
        return invoke_gate_cadence_gate.work_product_edit_problem(file_path, tool_input, transcript_path)
    except Exception as error:
        return ("Blocked: the work-product check could not run ({0}: {1}), so this change to a declaration is "
                "unjudged. Do not retry it through another tool; tell the operator that "
                "invoke_gate_cadence_gate.work_product_edit_problem raised this error, and go on with work that "
                "does not edit the declaration.".format(type(error).__name__, str(error)[:200]))


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

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str):
        return 0

    widened = work_product_widening(file_path, tool_input, payload.get("transcript_path"))
    if widened:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": DENY, "permissionDecisionReason": widened}},
            separators=(",", ":")))
        return 1

    # Write carries the whole file; Edit carries only the replacement.
    new_text = tool_input.get("content")
    old_string = None
    if not isinstance(new_text, str):
        new_text = tool_input.get("new_string")
        old_string = tool_input.get("old_string")
    if not isinstance(new_text, str):
        return 0

    decision, reason = evaluate(file_path, new_text, old_string)
    if decision != DEFER:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }, separators=(",", ":")))
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
