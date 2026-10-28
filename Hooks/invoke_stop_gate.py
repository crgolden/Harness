"""Stop-gate hook: the Stop hook registered in ~/.claude/settings.json.

The logic is unchanged and deliberately so; this is a HOST change, not a rule change. The gate's cost
was never its analysis -- measured on this machine, a full run on a two-line transcript took 3701ms of
which roughly 1900ms was pwsh startup and 1800ms was parsing the 35KB script. The analysis itself is
milliseconds. Python starts in ~174ms, so the same work lands at roughly a fifteenth of the cost, on a
hook that fires on every single turn.

A persistent server was considered and rejected: Claude Code spawns a command and reads stdout, so a
daemon still needs a client, and any pwsh client pays the same ~1652ms floor before it can open a
socket. Once the client is fast enough to be worth writing it can simply run the gate, and the daemon
only adds stale-process, crash-recovery and concurrency failure modes to a component whose whole job is
to be dependable.

CASE SENSITIVITY IS LOAD-BEARING: the stop tokens (NEEDED:, REFUSING:, DONE:, VERIFIED:, QUEUED:,
WAITING:) match only in capitals, so the operator can find them in long prose and a sentence opening
"Done:" is never read as a claim. Every other pattern compiles with re.I, because the PowerShell gate
this file was ported from matched case-insensitively; dropping it there would silently narrow a detector.

THERE IS NO SECOND IMPLEMENTATION. The PowerShell gate and the differential harness that kept the two in
step were deleted once it was clear only this file is ever executed: settings.json has always pointed
the Stop hook here, so the twin's sole reader was the harness comparing it against this file, and the
only drift either could catch was drift that had no consequence. Every case the deleted harnesses alone
covered was ported into test_stop_gate.py and proved green BEFORE they were removed.
"""

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from goal_pin import (COMPACT_SUMMARY_FLAG, FLAGS, RE_PEER_MESSAGE, RE_STOP_GATE, RE_SYSTEM_REMINDER,  # noqa: E402
                      active_goal_command, block_text, command_args, goal_in_force, normalised_words,
                      operator_text, pin_reader_digest)
from stop_tokens import token_line  # noqa: E402
import coordination  # noqa: E402
import invoke_prompt_gate  # noqa: E402
import session_home  # noqa: E402

ALLOW_STOP = 0
QUARANTINE_LOAD_FAILED = (
    "STOP GATE: the quarantine module could not load ({error}), so this stop cannot be checked against an eject.\n"
    "Every stop is blocked until the operator repairs the Tools checkout's Hooks/; tell them in plain prose."
)


def quarantine_halt(session_id, quarantine_root=None):
    """(signal, reason) for a session ejected for trying to change the gate that judges it, else None. Only the
    write-protection gate ejects; the Stop hook halts what it ejected. session_quarantine loads only here, and
    when it cannot load the stop is blocked with that failure named, never allowed."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import session_quarantine
    except Exception as error:
        return "block:quarantine-unloadable:1", QUARANTINE_LOAD_FAILED.format(
            error=type(error).__name__ + ": " + str(error)[:200])
    halt = session_quarantine.halt_envelope(session_id, quarantine_root)
    return ("eject:quarantined", halt["stopReason"]) if halt else None

# The trailer veto is switched off as of 2026-09-08 at the operator's request ("too aggressive"); the
# commit-block veto was switched off the same day and re-enabled on 2026-09-09 narrowed to turns that
# claim DONE:. The goal veto has no flag and stays live. test_stop_gate.py reads these same flags to
# decide whether to run a veto's block cases or its pass-through control.
TRAILER_VETO_ENABLED = False
COMMIT_BLOCK_VETO_ENABLED = True

WORKSPACE_ROOT = session_home.workspace_root()
STOP_LAUNCHER = r"""import os,runpy,sys
p=os.path.join(os.environ.get('USERPROFILE',''),os.environ.get('CLAUDE_TOOLS_ROOT') or '?','Hooks','launch_stop.py')
try:runpy.run_path(p,run_name='__main__')
except OSError:sys.stdout.write('{"decision":"block","reason":"STOP GATE: the Tools stop hook launcher is missing under CLAUDE_TOOLS_ROOT, so every stop is blocked. Tell the operator in plain prose to set it or pull the Tools checkout on main."}')
"""

WORKSPACE_MISSING_REASON = ("STOP GATE: the stop hook cannot find the workspace at {path}, so it cannot judge any stop,"
                            " and a NEEDED: line or question is refused while it is missing.\n"
                            "Only the operator can restore it, or point CRGOLDEN_WORKSPACE at the folder of repos this"
                            " Tools checkout serves."
                            " End the turn with one line opening REFUSING: that quotes at least four words of the"
                            " operator's message, for example:\nREFUSING: to continue \"<their words>\" until the"
                            " workspace exists at {path}.")

RE_FENCE = re.compile(r"^\s*```.*?^\s*```", re.MULTILINE | re.DOTALL | FLAGS)

# The token means "there is a thing only the operator can supply". Captured with its payload so a
# line declaring the OPPOSITE can be told apart from one naming a real blocker.
RE_NEEDED_LINE = token_line("NEEDED")
RE_NEEDED = RE_NEEDED_LINE

# Anchored at the payload's start and kept deliberately short. Every one of the 59 genuine NEEDED:
# lines measured across a week opens with the thing itself -- "your decision on", "a decision on",
# "the long-running task itself" -- so none of them are at risk from this.
RE_NEEDED_NEGATED = re.compile(
    r"^[\s*_`\-]*(nothing|none|no\b|nil|n/?a|zero|nada|not\s+a\s+thing)", FLAGS)
RE_REFUSING_LINE = token_line("REFUSING")
RE_REFUSING = RE_REFUSING_LINE
RE_DONE_LINE = token_line("DONE")
RE_DONE = RE_DONE_LINE
RE_VERIFIED = token_line("VERIFIED")

# Waiting on something another session started (its gate, the gate slot it holds, its work), once this
# session's work product is finished. No negation pattern, unlike NEEDED:, because a pinned goal restated on
# the line may well open with "nothing".
RE_QUEUED_LINE = token_line("QUEUED")

# Waiting on something this session started that is still running: its own gate, a background task whose
# completion notice has not arrived, or a permission matrix it requested whose run is still open.
RE_WAITING_LINE = token_line("WAITING")

# The master session's idle stop: nothing is left of its runbook cycle and the hourly wake task will start the next.
# Only the master may use it, told apart by MASTER_SESSION, which Start-MasterSession.ps1 sets in the claude process
# it launches and no tool call can set in its parent, on the one machine the master runs on.
RE_IDLE_LINE = token_line("IDLE")
from master_session import MASTER_HOST, MASTER_SESSION_VARIABLE, runs_as  # noqa: E402,F401
MASTER_WAKE_VARIABLE = "MASTER_WAKE"
MASTER_WAKE_TASK = "Claude Code - Master Wake"
MASTER_IDLE_SIGNAL = "allow:master-idle"
MASTER_WAKE_SIGNAL = "allow:master-wake-sent"

# A poke process: the headless claude Coordination/Send-SessionPoke.ps1 starts, on any machine, to deliver one
# read-the-board message to a session there. Its launcher sets the variable in the claude process's environment.
POKE_VARIABLE = "CRGOLDEN_SESSION_POKE"
POKE_SIGNAL = "allow:poke-sent"
# The stops that have no operator, goal, page or board row of their own, so the vetoes built on those skip them.
MESSENGER_SIGNALS = (MASTER_WAKE_SIGNAL, POKE_SIGNAL)
AUDITED_SIGNALS = ("allow:blocker-token", "allow:question-mark", "allow:work-complete", "allow:queued-for-gate",
                   "allow:waiting-on-own-gate", MASTER_IDLE_SIGNAL)
EJECT_STRIKE_LIMIT = 2
EJECT_STRIKE_SUFFIX = ".ejects.json"

# The line Invoke-TriagePermissionMatrix.ps1 prints for a request; the run it starts is open while the request
# file waits or while state\matrix-run.json beside it records a start at or after the request.
RE_MATRIX_REQUEST = re.compile(r"requested the permission matrix in (?P<path>[^;\r\n]*?matrix-(?P<stamp>\d{14})\.json)")
MATRIX_RUN_FILE = "matrix-run.json"

MACHINE_WAKE_ORIGINS = ("peer", "task-notification")
RE_TASK_NOTIFICATION = re.compile(r"^\s*<task-notification>", FLAGS)
RE_IDLE_NOTICE = re.compile(r"^\s*\[Cross-session idle notice\]", FLAGS)

RE_GRANT = re.compile(
    r"(stopping (is|was) earned|"
    r"you (can|may) (now )?stop\b|"
    r"ok(ay)? to stop\b|"
    r"permission to stop\b|"
    r"supersedes the stop gate|"
    r"no need to (continue|keep going|keep working)|"
    r"stand down\b|"
    r"don'?t (continue|keep going)\b)",
    FLAGS,
)

# THE PROSE EXITS ARE GONE. Until 2026-09-09 three more patterns earned a stop here: a blocker phrase
# anywhere in the message ("blocked on you", "your call", "needs your decision", "which do you want"), a
# refusal phrase in the opening lines ("I won't", "I'm not going to") and a decision request in the tail
# ("tell me which", "do you want me to", "please confirm"). They predate the NEEDED: and REFUSING: tokens
# and were kept as a net for phrasings agents use unprompted. The operator retired them because they
# were doing the opposite: a message that quoted one of the phrases, discussed the gate, mentioned an
# aside as "your call", or promised "I'll report back" beside a genuine blocker slipped through on a
# string it never meant as a stop. Replaying every recorded prose-path allow whose transcript survived
# (38 of 54) through the gate without them: 12 still exited through a question mark, a completion claim
# or REFUSING:, 6 were already blocked by vetoes added since, 8 were genuine asks that now cost one
# NEEDED: line or a question mark, and 12 were the accidental exits the retirement is for. What earns a
# stop now is a token on its own line, a question mark, or the operator's own grant -- nothing an agent
# writes by accident. STOP-HOOK.md carries the replay.

RE_ANNOUNCE = re.compile(
    r"\b(proceed(ing)? now|continu(e|ing) now|next I'?ll|I'?ll now|working on it|"
    r"standing by|report back|I'?ll (report|update|check|start|begin|run|continue|keep)|"
    r"kicking off|in the meantime|while (that|we) (runs?|wait))\b",
    FLAGS,
)

# No re.MULTILINE: this must anchor at the end of the string it is handed, not at every line end.
RE_TRAILING_QUESTION = re.compile(r"\?[\"')\]]*\s*$")

# A closing question this short is a tag ("Sound good?", "Shall I?"), not a decision, so an
# announcement anywhere in the message still disqualifies it.
TAG_QUESTION_MAX_WORDS = 3

# LINE-ANCHORED ON PURPOSE, and fences are deliberately NOT stripped first.
#
# A commit-message trailer occupies a whole line, and the place it does damage is inside the fenced
# block the operator copies -- which is the exact text every other detector here discards. So this one
# reads the raw message, and precision comes from the anchor instead: only leading whitespace may
# precede the token. Discussing the rule in prose writes it inline and usually backticked
# (`Co-Authored-By: Claude ...`), or behind a list marker, and neither starts a line.
RE_ATTRIBUTION_TRAILER = re.compile(
    r"^[ \t]*(?:Co-Authored-By:[ \t]*Claude|Claude-Session:[ \t]*https?://)",
    re.MULTILINE | FLAGS,
)

# Delivering a commit message by REFERENCE rather than by value. The operator reads a terminal and
# cannot open a scratchpad file or scroll back reliably, so a pointer is not a delivery.
RE_COMMIT_POINTER = re.compile(
    r"commit message[s]?\b[^.\n]{0,80}?\b("
    r"in (my|the) (previous|last|earlier) (message|reply|turn)|"
    r"(given|drafted|provided|listed|posted) (in|above|earlier|previously)|"
    r"in the scratchpad|see COMMIT-MESSAGES|COMMIT-MESSAGES\.md|from my previous)",
    FLAGS,
)

# Any fenced block at all. Deliberately loose: the veto only asks whether a message block was
# DELIVERED, and guessing at a commit message's internal shape would reject legitimate ones.
RE_ANY_FENCE = re.compile(r"^\s*```", re.MULTILINE)

# A double-quoted or backticked span, removed before the pointer search so that NAMING a phrase is not
# taken for USING it. Non-greedy and single-line, so an unterminated quote cannot swallow the message.
RE_QUOTED_SPAN = re.compile(r"[\"“][^\"”\n]{0,200}[\"”]|`[^`\n]{0,200}`")

# The tools whose file_path means the file was WRITTEN. Read carries one too, and until 2026-09-09 it
# counted: a file merely opened in a tree the operator had dirtied made the agent owe a block for the
# operator's own work, which is one of the two ways a stale block gets re-posted.
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# The body of each fenced block, for the commit-state check, which reads what a block NAMES.
RE_FENCE_BODY = re.compile(r"^\s*```[^\n]*\n(.*?)^\s*```", re.MULTILINE | re.DOTALL)

# One path-like token: a slash, a short extension, or an extensionless dotfile, and no spaces. A line
# made only of these (after an optional bullet or a "Files:" label) is a file-list line; a path inside a
# sentence is not, because a commit body that says "Claude/x.py carries the replay" is not listing the
# file it covers.
#
# The dotfile alternative is load-bearing rather than cosmetic: block_file_lines requires EVERY token on
# a line to be path-shaped and drops the whole line otherwise, so one unmatched name silently discards
# the real paths beside it. Without it a list line reading `.gitattributes, e2e/layout.ts` contributed
# nothing and the gate answered "no block names them" about a file the block did name.
RE_PATH_TOKEN = re.compile(r"^`?(?:[\w.@~+-]+[\\/])*[\w.@~+-]+`?,?$")
RE_PATH_SHAPED = re.compile(r"[\\/]|\.\w{1,8}`?,?$|^`?\.\w+`?,?$")
RE_FILES_LABEL = re.compile(r"^\s*(?:[-*]\s*)?(?:files?|touches|covers)\s*:\s*", FLAGS)

RE_QUESTION_WORD = re.compile(r"[a-z0-9']+")
REPEAT_QUESTION_SIMILARITY = 0.8
RE_GOAL_WORD = re.compile(r"\bgoal\b", re.IGNORECASE)
PARKED_COLLECTION = "questions"
OPERATOR_ASKS_COLLECTION = "asks"
STOP_FEEDBACK_PREFIX = "Stop hook feedback:"
STOP_GATE_REASON_PREFIX = "STOP GATE:"
GOAL_FEEDBACK_OPENER = "["
RE_PARKED_SHORT_ID = re.compile(r"^q\d+$")
RE_SHORT_ID_MENTION = re.compile(r"\b(q\d+)\b")
PARKED_ID_SIMILARITY = 0.5
PARKED_NEEDED_MIN_WORDS = 4
TOLD_TO_END_ON_NEEDED = "end the turn with a needed:"
RE_PENDING_ID = re.compile(r"^q\d+-[a-z0-9]+(?:-[a-z0-9]+)*$", re.IGNORECASE)
RE_WORK_PRODUCT_PATH = re.compile(r"[\\/]" + re.escape(session_home.WORK_PRODUCTS_NAME) + r"[\\/][^\\/]+\.json$",
                                  re.IGNORECASE)
OPERATOR_SCAN_CHUNK_BYTES = 1 << 20
PENDING_QUESTION_SUFFIX = ".question.json"
PENDING_ASKS_KEPT = 20

REPEATED_QUESTION_REASON = """STOP GATE: you already put this to the operator, and they have not written since:
  "{question}"
Asking it again, as a question or a NEEDED: line, buys a stop by repetition, and a status without an ask earns no
stop either. Do the work that does not depend on the answer, now, with a tool call. With none left, one wait stands:
mark every open item "pending": "<qN-slug>" in your declaration, read the page, then a NEEDED: naming that qN alone."""

REPEATED_ON_WAKE_REASON = """STOP GATE: a peer or a task started this turn, and the operator has not answered:
  "{question}"
Do not ask it again. Reply to the peer if it asked you something, then end the turn with a short line that asks
nothing (at most 280 characters, naming no question id): with your ask pending and no work of your own open, that
stop is earned."""

UNREAD_ON_WAKE_REASON = """STOP GATE: a peer or a task started this turn, and an ask you put to the operator names a parked
question that no read of your page this turn covers:
  "{question}"
The operator answers on the page, which the transcript never shows, so whether they answered is unknown. Read the
page now (ArtifactData query on questions where status == answered) and act on any answer before you end the turn."""

QUIET_WAKE_MAX_CHARS = 280
LOUD_WAKE_REASON = """STOP GATE: a peer or a task started this turn while an ask to the operator is open and no work of your
own is, so the turn ends with one short line that asks nothing. Your last message {problem}. Restating an open ask in
prose is asking it again; answer the peer if it is owed a reply, then end with a short line."""

STOP_RULES_POINTER = "Rules: Harness/Hooks/stop-gate-rules.txt (in your context since session start)."

TOKEN_MENU = ("Earned only by: a closing question the user must answer, or a line opening with NEEDED:, REFUSING:,"
              " QUEUED: or WAITING:, or DONE: with VERIFIED:.")

GATE_REASON = """STOP GATE: nothing in your last message earns a stop. Keep working now, with a tool call.
""" + TOKEN_MENU + "\n" + STOP_RULES_POINTER
GATE_REASON_HEAD = "nothing in your last message earns a stop."

STOP_TOKENS = ("NEEDED", "REFUSING", "QUEUED", "WAITING", "DONE", "VERIFIED")
TOKEN_ALTERNATION = "(" + "|".join(STOP_TOKENS) + ")"
RE_TOKEN_AT_START = re.compile(r"^[ \t]*(?:\*\*)?" + TOKEN_ALTERNATION + r":(?:\*\*)?[ \t]*\S", re.MULTILINE)
RE_TOKEN_BLANK = re.compile(r"^[ \t]*(?:\*\*)?" + TOKEN_ALTERNATION + r":(?:\*\*)?[ \t]*\r?$", re.MULTILINE)
RE_TOKEN_MARKED = re.compile(r"^[ \t]*(?:[-*+>]|\d+[.)])[ \t]+(?:\*\*)?" + TOKEN_ALTERNATION + ":", re.MULTILINE)
RE_TOKEN_MID_LINE = re.compile(r"^.*?\S.*?\b" + TOKEN_ALTERNATION + ":", re.MULTILINE)
RE_TOKEN_WRONG_CASE = re.compile(r"^[ \t]*(needed|refusing|queued|waiting|done|verified):", re.MULTILINE | re.IGNORECASE)
QUESTION_WINDOW_LINES = 6


def near_miss_note(text):
    """The one near-miss a refused message most plausibly meant as its stop, phrased as what to change, or None.
    Read only on the generic block, so it decides no verdict: it says which of the common misses this was."""
    stripped = RE_FENCE.sub("", text or "")
    at_start = set(RE_TOKEN_AT_START.findall(stripped))
    if "DONE" in at_start and "VERIFIED" not in at_start:
        return "your DONE: line has no VERIFIED: line naming the command you ran and what it printed"
    if "VERIFIED" in at_start and "DONE" not in at_start:
        return "your VERIFIED: line has no DONE: line restating the ask"
    if {"DONE", "VERIFIED"} <= at_start:
        return ("a DONE: needs a tool call in this turn: run the command your VERIFIED: line names, then restate"
                " DONE: and VERIFIED: quoting what it printed")
    for payload in RE_NEEDED.findall(stripped):
        if RE_NEEDED_NEGATED.match(payload):
            return "your NEEDED: line says nothing is needed; delete it and keep working, or name what is needed"
    marked = RE_TOKEN_MARKED.search(stripped)
    if marked and not at_start:
        return ("your {0}: follows a list marker or quote; a token counts only as the first thing on its line"
                .format(marked.group(1)))
    blank = RE_TOKEN_BLANK.search(stripped)
    if blank and not at_start:
        return "your {0}: line has nothing after the token; put its payload on the same line".format(blank.group(1))
    lines = [line for line in stripped.splitlines() if line.strip()]
    asked = [line for line in lines[-QUESTION_WINDOW_LINES:] if line.rstrip().endswith("?")]
    if not asked and any(line.rstrip().endswith("?") for line in lines[:-QUESTION_WINDOW_LINES]):
        return "your question is not among the last {0} lines".format(QUESTION_WINDOW_LINES)
    question = closing_question(stripped)
    if question and RE_ANNOUNCE.search(question):
        return ("your closing question also promises a next step ({0}), which cancels it; remove that phrase"
                .format(RE_ANNOUNCE.search(question).group(0)))
    if question and len(question.split()) <= TAG_QUESTION_MAX_WORDS and RE_ANNOUNCE.search(stripped):
        return ("your closing question is a bare tag under a promised next step ({0}); ask the real choice in"
                " full, or remove the promise".format(RE_ANNOUNCE.search(stripped).group(0)))
    if not at_start:
        for line in lines:
            mid = RE_TOKEN_MID_LINE.match(line)
            if mid:
                return "your {0}: sits mid-line; a token counts only opening its own line".format(mid.group(1))
            wrong = RE_TOKEN_WRONG_CASE.match(line)
            if wrong and wrong.group(1) not in STOP_TOKENS:
                return "your {0}: is not in capitals; a token counts only as {1}:".format(wrong.group(1),
                                                                                        wrong.group(1).upper())
    return None

RE_SESSION_NAME = re.compile(r"(?<![\w.-])crgolden-[0-9a-f]{2}(?!\w)", re.IGNORECASE)
RE_DOUBLE_QUOTED = re.compile(r"[\"“][^\"”\n]{0,400}[\"”]")
SESSION_NAME_REASON = ("STOP GATE: your message names a session by its name ({names}). Refer to every session, your own "
                       "included, by its session id: a session name is not unique (two live sessions can carry the "
                       "same one) and a restart or reset changes it, so the reader cannot tell which session you mean."
                       " The id is in the peer's message, its worktree folder or its board row, and "
                       "~/.claude/sessions/*.json maps a name to the ids carrying it now. Only a double-quoted quote "
                       "of someone's words may keep a name.\n")


def named_sessions(text):
    """The session names a message uses outside double-quoted quotes, lowercased and sorted."""
    unquoted = RE_DOUBLE_QUOTED.sub("", text or "")
    return sorted({match.group(0).lower() for match in RE_SESSION_NAME.finditer(unquoted)})


TRAILER_REASON = ("STOP GATE: a line of your message is a Claude attribution trailer (Co-Authored-By: Claude or"
                  " Claude-Session:).\nRe-post that commit message block in full without those lines; this workspace"
                  " never uses them.\n" + STOP_RULES_POINTER)

COMMIT_MISSING_HEAD = "STOP GATE: you claim DONE: while files you edited are still dirty, and no commit message block names them."

COMMIT_STALE_HEAD = "STOP GATE: your commit message block names files that are no longer dirty, so it is stale."

COMMIT_STATE_BODY = ("Found by git status --porcelain just now: {state}\n"
                     "Run git status --porcelain this turn and write one fresh block per repo still holding your work,"
                     " ending with a line per file; nothing for a clean repo. A sentence pointing at a message"
                     " elsewhere (\"given in my previous message\", \"drafted in the scratchpad\") delivers none:"
                     " delete it and paste the block.\n" + STOP_RULES_POINTER)

COMMIT_STATE_SHOWN = 3


def commit_state_reason(owed, stale, missing, ambiguous=None):
    """The block reason for the commit-state veto, carrying the porcelain result so the agent does not
    have to guess what the gate saw -- the same ordering fix the other reasons made: put the fact the
    agent needs in context at the moment of writing."""
    lines = []
    for path, repos in sorted((ambiguous or {}).items()):
        lines.append("ambiguous: {0} is in {1}, so list it as {2}".format(
            path, ", ".join(repos), " or ".join("{0}/{1}".format(repo, path) for repo in repos)))
    for repo in sorted(stale):
        lines.append("{0} clean now, do NOT list {1}".format(repo, ", ".join(stale[repo])))
    for repo in sorted(owed):
        tag = "owed now, no block names them" if repo in missing else "owed now"
        lines.append("{0} {1} {2}".format(repo, tag, ", ".join(owed[repo])))
    shown = "; ".join(lines[:COMMIT_STATE_SHOWN]) or "nothing"
    if len(lines) > COMMIT_STATE_SHOWN:
        shown += "; and {0} more".format(len(lines) - COMMIT_STATE_SHOWN)
    head = COMMIT_STALE_HEAD if stale else COMMIT_MISSING_HEAD
    return head + "\n" + COMMIT_STATE_BODY.format(state=shown)

GOAL_REASON_TEMPLATE = ("STOP GATE: your stop does not restate the operator's pinned goal: \"{goal}\"\n"
                        "Every earned stop carries its words verbatim. A DONE:, QUEUED: or WAITING: line carries them"
                        " in that line itself; a GOAL: line beside it does not count. A NEEDED: line or a closing"
                        " question carries them in that line, in the question, or on a line opening GOAL:. DONE: only"
                        " when that WHOLE goal is finished.\n" + STOP_RULES_POINTER)


def goal_reason(goal):
    """The goal veto's block reason, carrying the operator's goal so it is in context when the agent
    rewrites the claim -- the same ordering fix the trailer veto made: a rule loses to whatever is in
    context at the moment of writing, so put the goal there."""
    return GOAL_REASON_TEMPLATE.format(goal=re.sub(r"\s+", " ", goal).strip())

def is_machine_wake(record, text):
    """True when a user record was written by a peer session, a background task's completion or the harness's
    idle notice for a session this one subscribed to (a meta record with no origin), not the operator."""
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    if origin.get("kind"):
        return origin["kind"] in MACHINE_WAKE_ORIGINS
    if record.get("isMeta") is True and RE_IDLE_NOTICE.match(text):
        return True
    return bool(RE_PEER_MESSAGE.search(text) or RE_TASK_NOTIFICATION.match(text))


def update_fact(facts, line):
    """Fold one transcript line into facts. True means the scan has what it needs and should stop."""
    if not line.strip():
        return False
    try:
        record = json.loads(line)
    except Exception:
        # A record still being flushed lands here and is skipped in silence, which makes the gate
        # judge the PREVIOUS message with no trace that it did. Counted so the log can show it.
        facts["badjson"] = facts.get("badjson", 0) + 1
        return False
    if not isinstance(record, dict):
        return False

    if record.get("type") == "attachment":
        return False

    if "type" not in record or "message" not in record:
        return False

    # Diagnostic only, and deliberately recorded BEFORE any detector runs: this is the newest
    # message-bearing record the scan met, which is what a verdict cannot be reconstructed without.
    # Replaying blocks at their exact byte offsets showed 45 of 99 replaying as ALLOW, so the gate's
    # own view of the transcript is the missing evidence rather than any detector's wording.
    if facts.get("newest") is None:
        content_preview = record.get("message")
        blocks = content_preview.get("content") if isinstance(content_preview, dict) else None
        facts["newest"] = {
            "type": record.get("type"),
            "ts": record.get("timestamp"),
            "blocks": ",".join(
                b.get("type", "?") for b in blocks if isinstance(b, dict)
            ) if isinstance(blocks, list) else ("<str>" if isinstance(blocks, str) else "?"),
        }

    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None

    if record.get("type") == "user":
        is_prose = isinstance(content, str)
        carries_tool_result = False
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or "type" not in b:
                    continue
                if b["type"] == "text":
                    is_prose = True
                elif b["type"] == "tool_result":
                    carries_tool_result = True
        if not is_prose:
            return False

        text = block_text(content)

        # Scan past the gate's own feedback to reach what the OPERATOR actually said.
        if RE_STOP_GATE.search(text):
            if facts["tool_uses_since_block"] is None:
                facts["tool_uses_since_block"] = facts["tool_uses"]
            return False

        machine_wake = is_machine_wake(record, text)

        # A harness record that wakes no one (another Stop hook's feedback, such as /goal's; a skill body; a command
        # caveat) is nobody's message, so it neither speaks for the operator nor ends their turn.
        if record.get("isMeta") is True and not machine_wake:
            return False

        # Newest prose wins, so this latches rather than overwrites: the record supplying the text is
        # no longer always the record that ends the scan.
        if facts["last_user_text"] is None:
            facts["last_user_text"] = "" if machine_wake else text

        # Prose does NOT automatically start an operator turn. Only shapes that are true by
        # construction are excused -- a tool_result cannot appear in a prompt the operator just typed,
        # and a system-reminder is written by the harness rather than sent by anyone. See STOP-HOOK.md.
        if RE_SYSTEM_REMINDER.search(text):
            return False

        if carries_tool_result:
            return False
        return True

    if record.get("type") != "assistant":
        return False

    # The work-complete floor, counted here because this scan already spans exactly the right window.
    # Substring over the RAW line, matching the compact serialization Claude Code writes.
    if '"type":"tool_use"' in line:
        if facts["tool_calls"] < 2:
            facts["tool_calls"] += 1
        if isinstance(content, list):
            facts["tool_uses"] += sum(1 for block in content
                                      if isinstance(block, dict) and block.get("type") == "tool_use")
        # Which files this turn WROTE, for the commit-state veto. The record is already parsed above;
        # only write tools count, so a Read of the operator's dirty file owes nothing.
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                # A message to a peer in THIS turn is what makes a QUEUED: stop wake up again.
                if block.get("name") == "SendMessage":
                    arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                    recipient = arguments.get("to")
                    if isinstance(recipient, str) and recipient.strip():
                        facts["messaged"] = True
                        facts["messaged_to"].add(recipient.strip())
                    continue
                if block.get("name") in ("PowerShell", "Bash"):
                    arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                    command = str(arguments.get("command") or "")
                    if command:
                        facts["commands"].append(command)
                    continue
                if block.get("name") not in WRITE_TOOLS:
                    continue
                arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                target = arguments.get("file_path") or arguments.get("notebook_path")
                if isinstance(target, str) and target:
                    facts["edited"].add(target)

    if facts["assistant_text"]:
        return False
    if not isinstance(content, list):
        return False
    joined = block_text(content).strip()
    if joined:
        facts["assistant_text"] = joined
    return False


def read_facts(path, max_scan_bytes=8388608):
    """Read the transcript BACKWARDS in chunks, stopping at the first prose user record."""
    facts = {"assistant_text": None, "last_user_text": None, "tool_calls": 0, "newest": None,
             "size": None, "badjson": 0, "endsnl": None, "edited": set(), "commands": [], "messaged": False,
             "messaged_to": set(),
             "tool_uses": 0, "tool_uses_since_block": None}
    if not path or not os.path.exists(path):
        return facts

    try:
        facts["size"] = os.path.getsize(path)
        # A complete record ends in a newline. Its absence means the writer is mid-record, which is
        # the one condition under which the newest message is unreadable rather than merely absent.
        if facts["size"]:
            with open(path, "rb") as probe:
                probe.seek(facts["size"] - 1)
                facts["endsnl"] = probe.read(1) == b"\n"
    except Exception:
        pass

    chunk_size = 65536
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        position = fh.tell()
        scanned = 0
        pending = b""

        while position > 0 and scanned < max_scan_bytes:
            take = min(chunk_size, position)
            position -= take
            scanned += take
            fh.seek(position)
            combined = fh.read(take) + pending

            # Walk the chunk's newlines backwards, which yields lines newest-first so the early exit
            # stops the read rather than merely skipping work.
            end = len(combined)
            found_any = False
            while end > 0:
                newline = combined.rfind(b"\n", 0, end)
                if newline < 0:
                    break
                found_any = True
                start = newline + 1
                if end > start:
                    if update_fact(facts, combined[start:end].decode("utf-8", "replace")):
                        return facts
                end = newline

            if not found_any:
                pending = combined
                continue
            pending = combined[:end]

        # The file's first line has no preceding newline, so it is reachable only through pending.
        if position == 0 and pending:
            update_fact(facts, pending.decode("utf-8", "replace"))

    return facts


def operator_granted_stop(text):
    """The only un-forgeable signal: read from a record the agent does not write."""
    if not text:
        return False
    return bool(RE_GRANT.search(text))


def declares_a_blocker(text):
    """True when a NEEDED: line names something, rather than declaring nothing is needed.

    `NEEDED: nothing from you` read as a blocker and earned a stop, which inverts the token: it is
    the one signal that asserts the operator is required, and it was being used to assert they are
    not. The message it appeared in also announced its own next steps -- normally disqualifying --
    but the announce check sits after this one and was never reached.

    A message may carry several NEEDED: lines; one real blocker among them is still a blocker.
    """
    for payload in RE_NEEDED_LINE.findall(text):
        if not RE_NEEDED_NEGATED.match(payload):
            return True
    return False


def continuing_is_impossible(text):
    """Returns the NAME of the signal that earned the stop, or None.

    Three exits: a NEEDED: line naming a thing, a REFUSING: line, or a question mark ending one of the
    closing lines, where that line does not announce the agent's next step and is not a bare tag under an
    announcement elsewhere. Each is something an agent
    writes on purpose. The prose phrasings that used to sit beside them were retired on 2026-09-09 --
    see the note above RE_ANNOUNCE -- because a phrase can be quoted, discussed or used as an aside,
    and every one of those bought a stop the message had not asked for.
    """
    if not text:
        return None

    # Quoted output must not declare a stop on the agent's behalf.
    unquoted = RE_FENCE.sub("", text)

    if declares_a_blocker(unquoted):
        return "blocker-token"

    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        return None

    if RE_REFUSING.search(unquoted):
        return "refusal-token"

    # A question does not stop being a question because the agent put context underneath it, so the
    # closing question is the last of the final six lines that ends in "?". Whole LINES only, so a
    # rhetorical mid-sentence "why?" still earns nothing.
    question = next((line for line in reversed(lines[-6:]) if is_question_line(line)), None)
    if question is None:
        return None

    # Announce-then-stop disqualifies, judged on the QUESTION LINE: "Proceeding now. Sound good?" is a
    # promise wearing a question mark. Until 2026-09-14 the veto searched the whole message, so an aside
    # anywhere above a genuine question -- "I'll start Functions once the peer reports" -- or an announce
    # phrase merely quoted in an analysis cancelled the question. STOP-HOOK.md carries the replay.
    if RE_ANNOUNCE.search(question):
        return None

    # The evasion that scoping opens: the promise on its own line and a bare tag beneath it. A tag
    # carries no decision, so a short question under an announcement anywhere still earns nothing.
    if len(question.split()) <= TAG_QUESTION_MAX_WORDS and RE_ANNOUNCE.search(text):
        return None

    return "question-mark"


def is_question_line(line):
    """A line ending in "?" that is not a restated GOAL: line, whose own last character may be a "?" the operator
    wrote: restating the goal asks nothing."""
    return bool(RE_TRAILING_QUESTION.search(line)) and not RE_GOAL_LINE.match(line)


def closing_question(text):
    """The question line a stop would be earned by: the last of the final six lines ending in "?"."""
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    return next((line for line in reversed(lines[-6:]) if is_question_line(line)), None)


def same_question(first, second, similarity=REPEAT_QUESTION_SIMILARITY):
    """True when two question lines share at least `similarity` of their words both ways (almost all by default),
    so one is the other asked again."""
    first_words = set(RE_QUESTION_WORD.findall((first or "").lower()))
    second_words = set(RE_QUESTION_WORD.findall((second or "").lower()))
    if not first_words or not second_words:
        return False
    return len(first_words & second_words) / len(first_words | second_words) >= similarity


def same_parked_ask(first, second, parked_ids):
    """True when two asks both name the short id (q3) of a question this session parked: rewording a parked
    question does not make it a new ask."""
    named = set(RE_SHORT_ID_MENTION.findall((first or "").lower())) & set(
        RE_SHORT_ID_MENTION.findall((second or "").lower()))
    return bool(named & parked_ids)


def is_human_record(record):
    """True for a record the operator typed: a turn, or a mid-turn message carried as an attachment."""
    attachment = record.get("attachment") if isinstance(record.get("attachment"), dict) else {}
    origin = record.get("origin") or attachment.get("origin")
    return isinstance(origin, dict) and origin.get("kind") == "human"


def operator_records_at_most(transcript_path, limit):
    """True when the operator has typed no more than `limit` records in the transcript; an unreadable one is
    False, so a broken read never widens which question goes unparked."""
    count = 0
    try:
        with open(transcript_path, "rb") as handle:
            for raw in handle:
                if b'"human"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(record, dict) and is_human_record(record):
                    count += 1
                    if count > limit:
                        return False
    except (OSError, TypeError):
        return False
    return True


def asks_for_the_goal(text, signal, goal, transcript_path):
    """A closing question naming GOAL, when no goal is pinned or the operator has written only once. Such a question
    is what the goal gate tells a session without one to ask, and no work tool can run before it is answered."""
    if signal != "allow:question-mark":
        return False
    question = closing_question(text)
    if not question or not RE_GOAL_WORD.search(question):
        return False
    return goal is None or operator_records_at_most(transcript_path, 1)


def asks_for_a_goal(text, signal, goal):
    """True when no goal is pinned and every ask the stop makes, a NEEDED: line or the closing question, asks for
    one. The goal gate runs no tool without a goal, so such an ask is the session's only move."""
    if goal is not None or signal not in ("allow:blocker-token", "allow:question-mark"):
        return False
    asks = earning_asks(text, signal)
    return bool(asks) and all(RE_GOAL_WORD.search(ask) for ask in asks)


def own_work_left(transcript_path, workspace):
    """True when this session's declaration holds an open item waiting on no parked question: work of its own that a
    stop would walk away from. No readable declaration declares none."""
    state = pending_items(transcript_path, workspace)
    return bool(state and state[0])


QUIET_WAKE_SIGNAL = "allow:woken-with-ask-pending"


def loud_wake_problem(text, pending):
    """What keeps a woken turn's last message from being the short line that asks nothing, else "": past
    QUIET_WAKE_MAX_CHARS, or naming a parked question id (q12) an ask already put to the operator names."""
    body = (text or "").strip()
    if len(body) > QUIET_WAKE_MAX_CHARS:
        return "runs {0} characters, past the {1} a quiet ending allows".format(len(body), QUIET_WAKE_MAX_CHARS)
    asked_ids = {found for ask in pending for found in RE_SHORT_ID_MENTION.findall((ask or "").lower())}
    named = sorted(found for found in RE_SHORT_ID_MENTION.findall(body.lower()) if found in asked_ids)
    if named:
        return "names {0}, which you already asked the operator".format(", ".join(named))
    return ""


def recorded_parking_url(workspace, session_id, transcript_path=None):
    """The session's parking-lot page URL, as parking_lot.py record-url wrote it under the session's home or the
    workspace (session_home.parking_dirs), or None before it is published."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import session_home
    for directory in session_home.parking_dirs(workspace, session_id, transcript_path):
        try:
            with open(os.path.join(directory, "state.json"), "r", encoding="utf-8") as handle:
                url = json.load(handle).get("url")
        except (OSError, ValueError, AttributeError, TypeError):
            continue
        if isinstance(url, str) and url:
            return url
    return None


def parked_questions(transcript_path, page_url=None):
    """(question, doc_id) for every question this session wrote to its parking-lot page's questions store, read
    from its own ArtifactData calls, since the store itself is out of the hook's reach. A write counts only once
    its tool_result came back without an error, and, once the session has recorded its page, only when the call
    targets that page: a denied call, or one to another artifact's store, parked nothing."""
    return [(data["question"], doc_id) for data, doc_id in parked_writes(transcript_path, page_url)]


def parked_writes(transcript_path, page_url=None):
    """(data, doc_id) for every question write parked_questions() counts, with the whole document written."""
    pending = {}
    landed = set()
    try:
        with open(transcript_path, "rb") as handle:
            for raw in handle:
                if b'"ArtifactData"' not in raw and b'"tool_result"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                content = (record.get("message") or {}).get("content") if isinstance(record, dict) else None
                for part in content if isinstance(content, list) else []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool_result" and not part.get("is_error"):
                        landed.add(part.get("tool_use_id"))
                    if not (part.get("type") == "tool_use" and part.get("name") == "ArtifactData"
                            and isinstance(part.get("input"), dict)):
                        continue
                    call = part["input"]
                    if page_url and call.get("url") != page_url:
                        continue
                    writes = call.get("writes") if call.get("action") == "batch" else [call]
                    for write in writes if isinstance(writes, list) else []:
                        data = write.get("data") if isinstance(write, dict) else None
                        if (write.get("collection") == PARKED_COLLECTION and isinstance(data, dict)
                                and isinstance(data.get("question"), str)):
                            pending.setdefault(part.get("id"), []).append((data, str(write.get("doc_id") or "")))
    except (OSError, TypeError):
        return []
    return [entry for use_id, entries in pending.items() if use_id in landed for entry in entries]


NO_PAGE_STOP_REASON = ("STOP GATE: this session has no parking-lot page, and no stop is earned before it has one: the"
                       " operator answers the session's questions and audits its work there. Set it up with the"
                       " parking-lot skill (parking_lot.py render, the Artifact publish, parking_lot.py record-url and"
                       " the session/goal write), then end the turn.\n" + STOP_RULES_POINTER)
UNPARKED_START_DECISION_REASON = ("STOP GATE: your DONE: walks past a decision the SessionStart hook handed the operator,"
                                  " and no question on your parking-lot page quotes it:\n  \"{line}\"\nPark it with"
                                  " the parking-lot skill, quoting the whole line in its context, and ask it; withdraw"
                                  " it with the operator's words if they answer in chat.\n" + STOP_RULES_POINTER)
RE_START_DECISION = re.compile(r"^\s*OPERATOR ACTION:")


def session_start_context(transcript_path):
    """(start, text): the newest SessionStart context the session-start rules hold it to (invoke_goal_gate's
    session_start, for a record no older than that rule) and its whole text, read from the saved file when Claude
    Code saved it to one. (None, None) when there is none or it cannot be read."""
    try:
        import invoke_goal_gate
        start = invoke_goal_gate.session_start(transcript_path)
        if start is None or not invoke_goal_gate.started_under_the_rule(start):
            return None, None
        if not start["saved"]:
            return start, start["text"]
        with open(start["saved"], "r", encoding="utf-8", errors="replace") as handle:
            return start, handle.read()
    except Exception:
        return None, None


def unparked_start_decision(start_text, transcript_path, page_url=None):
    """The first line of the SessionStart context that hands the operator a decision or an action, which
    Invoke-SessionStart.ps1 opens with "OPERATOR ACTION:", and that no question parked on the session's page quotes in
    full, in its question or its context, else None. The marker, never the wording, picks the line: the rules card
    the same context carries describes such lines without being one."""
    lines = [line.strip() for line in (start_text or "").splitlines() if RE_START_DECISION.search(line)]
    if not lines:
        return None
    parked = [normalised_words("{0} {1}".format(data.get("question") or "", data.get("context") or ""))
              for data, _ in parked_writes(transcript_path, page_url)]
    for line in lines:
        wanted = normalised_words(line).strip()
        if not any(wanted in text for text in parked):
            return line
    return None


def is_parked(question, transcript_path, page_url=None):
    """True when the closing question names a parked question's short id (q6) and shares most of that row's words
    (same_question at PARKED_ID_SIMILARITY), or is that parked question asked again (same_question). Both are
    measured both ways, so neither a placeholder row nor a parked fragment of a few words covers a question. No other
    id counts: the session picks the id, so any word or slug of one would park anything."""
    lowered = (question or "").lower()
    for text, doc_id in parked_questions(transcript_path, page_url):
        lead = doc_id.split("-", 1)[0].lower()
        if (RE_PARKED_SHORT_ID.match(lead) and lead in RE_SHORT_ID_MENTION.findall(lowered)
                and same_question(question, text, PARKED_ID_SIMILARITY)):
            return True
        if same_question(question, text):
            return True
    return False


def unparked_needed(text, signal, transcript_path, page_url=None):
    """The first NEEDED: payload that names no question parked on the session's page, else None. A NEEDED: is that
    question however it is reworded, so naming a parked row's short id (q6) parks it, provided the row holds a
    question of PARKED_NEEDED_MIN_WORDS words or more rather than a placeholder; so does asking the parked question
    again. An ask to commit or push is exempt, since a parked question never asks that (artifact-usage)."""
    if signal != "allow:blocker-token":
        return None
    parked = [(question, doc_id.split("-", 1)[0].lower()) for question, doc_id in
              parked_questions(transcript_path, page_url)]
    for ask in earning_asks(text, signal):
        if asks_to_land(ask):
            continue
        mentions = set(RE_SHORT_ID_MENTION.findall(ask.lower()))
        if any((RE_PARKED_SHORT_ID.match(lead) and lead in mentions
                and len(set(RE_QUESTION_WORD.findall(question.lower()))) >= PARKED_NEEDED_MIN_WORDS)
               or same_question(ask, question) for question, lead in parked):
            continue
        return ask
    return None


def told_to_end_on_needed(transcript_path):
    """True when the newest tool result is a hook's denial telling the session to end the turn with a NEEDED: line
    (the dispatcher or one of its gates could not run): every tool is refused, the page write included, so that
    NEEDED: cannot be parked first."""
    last = None
    try:
        with open(transcript_path, "rb") as handle:
            for raw in handle:
                if b'"tool_result"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                content = (record.get("message") or {}).get("content") if isinstance(record, dict) else None
                for part in content if isinstance(content, list) else []:
                    if isinstance(part, dict) and part.get("type") == "tool_result":
                        last = part
    except (OSError, TypeError):
        return False
    return bool(last and last.get("is_error") and TOLD_TO_END_ON_NEEDED in block_text(last.get("content")).lower())


def page_documents(content):
    """{doc id: status} for every question document an ArtifactData read printed, one JSON object per document."""
    if isinstance(content, list):
        content = "\n".join(part.get("text") or "" for part in content if isinstance(part, dict))
    found = {}
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{"id":', content if isinstance(content, str) else ""):
        try:
            document, _ = decoder.raw_decode(content, match.start())
        except ValueError:
            continue
        data = document.get("data") if isinstance(document, dict) else None
        if isinstance(data, dict) and isinstance(document.get("id"), str):
            found[document["id"].lower()] = data.get("status") if isinstance(data.get("status"), str) else ""
    return found


def is_stop_feedback(record):
    """True for the record that hands a refused stop's reason back to the session: each one starts a new stop
    attempt. The /goal hook refuses the same attempt in a record of its own, its reason opening with the bracketed
    condition, and Claude Code may write that record before this gate runs; counting it as a new attempt would
    discard every read made for the stop being judged. So a feedback record is a boundary unless it is /goal's
    alone: a body opening with '[' and carrying no line of this gate's own reason."""
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content")
    if record.get("type") != "user" or not isinstance(content, str) or not content.startswith(STOP_FEEDBACK_PREFIX):
        return False
    body = content[len(STOP_FEEDBACK_PREFIX):].strip()
    goal_only = body.startswith(GOAL_FEEDBACK_OPENER) and not any(
        line.strip().startswith(STOP_GATE_REASON_PREFIX) for line in body.splitlines())
    return not goal_only


def starts_a_woken_turn(record):
    """True for a user record whose whole content is a peer's message, a task's completion or an idle notice: a turn
    the operator did not start."""
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content")
    return record.get("type") == "user" and isinstance(content, str) and is_machine_wake(record, content)


FULL_PAGE_LIMIT = 1000


def reads_every_question(action, query):
    """Whether a read of the questions store returned every question it answers for: a list, or a query for status ==
    answered, so a question it did not return is still open. A query ordered and cut to a few rows (order_by, or a
    limit below FULL_PAGE_LIMIT) returns only those, so its silence on a question says nothing."""
    if action == "list":
        return True
    if action != "query" or query.get("where") != [["status", "==", "answered"]] or query.get("order_by"):
        return False
    limit = query.get("limit")
    return limit is None or (isinstance(limit, int) and limit >= FULL_PAGE_LIMIT)


def page_reads_this_turn(transcript_path, page_url=None):
    """(covers_all, statuses) from the reads of the page's questions store made in this stop attempt, that is,
    since the operator's newest message and since the newest refused stop: whether one read answered for every
    question at once (a list, or a query for status == answered, whose silence on a question says it is still
    open), and the status of each document a read returned. The operator answers on the page at any moment, so a
    read from an earlier attempt is stale, and the session's own park write proves nothing about what they did
    after it. A turn a peer, a task or an idle notice starts is a new attempt too: the operator may have answered
    while the session was stopped."""
    calls, covers_all, statuses = {}, False, {}
    try:
        with open(transcript_path, "rb") as handle:
            for raw in handle:
                if (b'"human"' not in raw and b'"ArtifactData"' not in raw and b'"tool_result"' not in raw
                        and STOP_FEEDBACK_PREFIX.encode() not in raw and b'"user"' not in raw):
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if is_human_record(record) or is_stop_feedback(record) or starts_a_woken_turn(record):
                    calls, covers_all, statuses = {}, False, {}
                    continue
                content = (record.get("message") or {}).get("content")
                for part in content if isinstance(content, list) else []:
                    if not isinstance(part, dict):
                        continue
                    call = part.get("input") if isinstance(part.get("input"), dict) else {}
                    if (part.get("type") == "tool_use" and part.get("name") == "ArtifactData"
                            and (not page_url or call.get("url") == page_url)):
                        calls[part.get("id")] = call
                    elif part.get("type") == "tool_result" and not part.get("is_error") \
                            and part.get("tool_use_id") in calls:
                        call = calls[part["tool_use_id"]]
                        if call.get("action") in ("get", "query", "list"):
                            if call.get("collection") != PARKED_COLLECTION:
                                continue
                            query = call.get("query") if isinstance(call.get("query"), dict) else {}
                            if reads_every_question(call.get("action"), query) and \
                                    "next_cursor" not in json.dumps(part.get("content")):
                                covers_all = True
                            statuses.update(page_documents(part.get("content")))
    except (OSError, TypeError):
        return False, {}
    return covers_all, statuses


def parked_ids_named(ask, parked):
    """The doc ids of the parked questions an ask names, by short id (q6) or by asking the same question."""
    mentions = set(RE_SHORT_ID_MENTION.findall(ask.lower()))
    named = set()
    for question, doc_id in parked:
        lead = doc_id.split("-", 1)[0].lower()
        if (RE_PARKED_SHORT_ID.match(lead) and lead in mentions) or same_question(ask, question):
            named.add(doc_id.lower())
    return named


def parked_ask_states(asks, transcript_path, page_url=None):
    """(answered, unread) among asks already put to the operator: those naming a parked question a read of the page
    this turn shows answered, and those naming one no read this turn covers. The operator answers on the page, which
    the transcript never shows, so neither kind is known to be unanswered."""
    if not asks:
        return [], []
    parked = parked_questions(transcript_path, page_url)
    covers_all, statuses = page_reads_this_turn(transcript_path, page_url)
    answered, unread = [], []
    for ask in asks:
        named = parked_ids_named(ask, parked)
        if any(statuses.get(doc_id) == "answered" for doc_id in named):
            answered.append(ask)
        elif not covers_all and any(doc_id not in statuses for doc_id in named):
            unread.append(ask)
    return answered, unread


def without_goal_words(line, goal):
    """The line with the pinned goal's words taken out wherever it restates them, quoted or bare: the goal veto has a
    stop line carry the goal, and the goal is the operator's words, never an ask, so a qN inside it names nothing."""
    wanted = normalised_words(goal or "").strip()
    words = normalised_words(line or "")
    if not wanted or " " + wanted + " " not in words:
        return line
    return words.replace(" " + wanted + " ", " ")


def unread_parked_asks(text, signal, transcript_path, page_url=None, goal=None):
    """What is wrong with an ask that names a parked question, else "": the operator answers on the page too, so an
    ask naming one stands only after a read of the page this turn that shows it still open. The pinned goal restated
    on the line is no ask (without_goal_words)."""
    parked = parked_questions(transcript_path, page_url)
    named = set()
    for ask in earning_asks(text, signal) + wait_lines(text, signal):
        named |= parked_ids_named(without_goal_words(ask, goal), parked)
    if not named:
        return ""
    covers_all, statuses = page_reads_this_turn(transcript_path, page_url)
    answered = sorted(doc_id for doc_id in named if statuses.get(doc_id) in ("answered", "withdrawn"))
    if answered:
        return ("names {0}, which your page shows answered or withdrawn: act on that answer instead of naming it as "
                "still waiting").format(", ".join(answered))
    unread = sorted(doc_id for doc_id in named if not covers_all and doc_id not in statuses)
    if unread:
        return "names {0}, and no read of your page since the operator's newest message shows it still open".format(
            ", ".join(unread))
    return ""


RE_PROSE_ASK_CUE = re.compile(
    r"\?\s*$|\byour (?:answer|decision|call)\b|\bneeds? (?:you|your)\b|\bwait(?:s|ing)? (?:on|for) you\b"
    r"|\banswer q\d+\b|\bplease (?:answer|decide|approve)\b|\bunanswered\b|\bstill open\b", re.IGNORECASE)
RE_PROSE_REPORT = re.compile(
    r"\b(?:was|is) (?:answered|approved)\b|\byou (?:answered|approved)\b|\bapproved it\b|\bwithdrawn\b"
    r"|\byour answer was\b|\bq\d+ asked\b", re.IGNORECASE)


def prose_reasks(text, transcript_path, page_url=None):
    """What is wrong with a line of the message, other than its NEEDED:, QUEUED: and WAITING: lines and closing question
    (unread_parked_asks owns those), that asks the operator about a parked question again ("q121 still needs your answer"), else "": a
    re-ask in prose reaches the operator as surely as a NEEDED: line does. A line asks when it names a parked question
    by its short id and carries an asking cue (RE_PROSE_ASK_CUE, a question mark only at the line's end); a line that
    reports the answer in the past tense (RE_PROSE_REPORT: "q121 was answered: keep the delete", "q6 asked whether to
    keep it; your answer was yes") asks nothing, cue words or not."""
    parked = parked_questions(transcript_path, page_url)
    if not parked:
        return ""
    named = set()
    closing = (closing_question(text) or "").strip()
    for line in RE_FENCE.sub("", text or "").splitlines():
        if (closing and line.strip() == closing) or RE_NEEDED_LINE.search(line) or \
                line.lstrip().startswith(("QUEUED:", "WAITING:")):
            continue
        if not RE_PROSE_ASK_CUE.search(line) or RE_PROSE_REPORT.search(line):
            continue
        mentions = set(RE_SHORT_ID_MENTION.findall(line.lower()))
        for _, doc_id in parked:
            lead = doc_id.split("-", 1)[0].lower()
            if RE_PARKED_SHORT_ID.match(lead) and lead in mentions:
                named.add(doc_id.lower())
    if not named:
        return ""
    covers_all, statuses = page_reads_this_turn(transcript_path, page_url)
    answered = sorted(doc_id for doc_id in named if statuses.get(doc_id) in ("answered", "withdrawn"))
    if answered:
        return "asks the operator again about {0}, which your page shows answered or withdrawn".format(
            ", ".join(answered))
    unread = sorted(doc_id for doc_id in named if not covers_all and doc_id not in statuses)
    if unread:
        return "asks the operator about {0}, and no read of your page since their newest message shows it still open".format(
            ", ".join(unread))
    return ""


def unanswered_operator_asks(transcript_path, page_url):
    """What is owed on the page's asks collection, the operator's own questions for the session, else "". In this stop
    attempt (since the operator's newest message and the newest refused stop) a read must cover every open ask (a list,
    or a query for status == open), and each open ask it returned must get a reply: an update setting status replied.
    The operator writes an ask on the page at any moment, so only a read made now shows what waits."""
    calls, covered, open_ids, replied = {}, False, set(), set()
    try:
        with open(transcript_path, "rb") as handle:
            for raw in handle:
                if (b'"human"' not in raw and b'"ArtifactData"' not in raw and b'"tool_result"' not in raw
                        and STOP_FEEDBACK_PREFIX.encode() not in raw and b'"user"' not in raw):
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if is_human_record(record) or is_stop_feedback(record) or starts_a_woken_turn(record):
                    calls, covered, open_ids, replied = {}, False, set(), set()
                    continue
                content = (record.get("message") or {}).get("content")
                for part in content if isinstance(content, list) else []:
                    if not isinstance(part, dict):
                        continue
                    call = part.get("input") if isinstance(part.get("input"), dict) else {}
                    if (part.get("type") == "tool_use" and part.get("name") == "ArtifactData"
                            and call.get("url") == page_url):
                        calls[part.get("id")] = call
                    elif part.get("type") == "tool_result" and not part.get("is_error") \
                            and part.get("tool_use_id") in calls:
                        call = calls[part["tool_use_id"]]
                        writes = call.get("writes") if call.get("action") == "batch" else [call]
                        if call.get("action") in ("query", "list") and call.get("collection") == OPERATOR_ASKS_COLLECTION:
                            query = call.get("query") if isinstance(call.get("query"), dict) else {}
                            if call.get("action") == "list" or query.get("where") == [["status", "==", "open"]]:
                                covered = True
                            open_ids |= {doc_id for doc_id, status in page_documents(part.get("content")).items()
                                         if status == "open"}
                        for write in writes if isinstance(writes, list) else []:
                            data = write.get("data") if isinstance(write, dict) else None
                            if (isinstance(data, dict) and write.get("collection") == OPERATOR_ASKS_COLLECTION
                                    and (write.get("op") or write.get("action")) in ("set", "update")
                                    and data.get("status") == "replied" and str(data.get("reply") or "").strip()):
                                replied.add(str(write.get("doc_id") or "").lower())
    except (OSError, TypeError):
        return ""
    if not covered:
        return ("no read of your page's {0} since the operator's newest message: run ArtifactData query on {0} where "
                "status == open, at {1}").format(OPERATOR_ASKS_COLLECTION, page_url)
    owed = sorted(open_ids - replied)
    if owed:
        return ("the operator asked {0} on your page and it has no reply yet: answer each with an ArtifactData update "
                "of its reply, repliedAt (measured) and status replied").format(", ".join(owed))
    return ""


def pending_items(transcript_path, workspace):
    """(unpended, pending) from this session's work-product declaration: how many open items name no parked
    question, and the parked question id every item carrying one names, done or not. A done item still waits when
    its approval does. None when there is no readable declaration."""
    cadence = cadence_module()
    if cadence is None or not transcript_path:
        return None
    try:
        with open(cadence.work_product_path(transcript_path, cadence.WORKSPACE if workspace is None else workspace),
                  "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        return None
    entries = [entry for items in repos.values() if isinstance(items, list) for entry in items if isinstance(entry, dict)]
    pending = [parked_id(entry) for entry in entries if parked_id(entry)]
    unpended = [entry for entry in entries if entry.get("done") is not True and not parked_id(entry)]
    return len(unpended), pending


def parked_id(entry):
    """The parked question id a declared item waits on, or None when it names none in the qN-slug shape."""
    pending = entry.get("pending")
    return pending if isinstance(pending, str) and RE_PENDING_ID.match(pending) else None


def parked_before_marked(transcript_path, pending_ids, page_url=None):
    """True when every pending id names a question this session parked (a landed write to its page) BEFORE the
    first Edit or Write of its work-product declaration that names the id. The session writes both, so the order
    is the proof the question existed when the item was marked as waiting on it; no such edit fails closed."""
    wanted = {pending.lower() for pending in pending_ids}
    parked_at, landed, marked_at = {}, set(), {}
    try:
        with open(transcript_path, "rb") as handle:
            for position, raw in enumerate(handle):
                if b'"tool_use"' not in raw and b'"tool_result"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                content = (record.get("message") or {}).get("content") if isinstance(record, dict) else None
                for part in content if isinstance(content, list) else []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool_result" and not part.get("is_error"):
                        landed.add(part.get("tool_use_id"))
                    if part.get("type") != "tool_use" or not isinstance(part.get("input"), dict):
                        continue
                    call = part["input"]
                    if part.get("name") == "ArtifactData" and (not page_url or call.get("url") == page_url):
                        writes = call.get("writes") if call.get("action") == "batch" else [call]
                        for write in writes if isinstance(writes, list) else []:
                            doc_id = str(write.get("doc_id") or "").lower() if isinstance(write, dict) else ""
                            if write.get("collection") == PARKED_COLLECTION and doc_id in wanted:
                                parked_at.setdefault(doc_id, []).append((position, part.get("id")))
                    elif part.get("name") in ("Edit", "Write") and RE_WORK_PRODUCT_PATH.search(
                            str(call.get("file_path") or "")):
                        written = (str(call.get("new_string") or "") + str(call.get("content") or "")).lower()
                        for pending in wanted:
                            if pending in written:
                                marked_at.setdefault(pending, position)
    except (OSError, TypeError):
        return False
    for pending in wanted:
        if pending not in marked_at:
            return False
        if not any(at < marked_at[pending] and use_id in landed for at, use_id in parked_at.get(pending, [])):
            return False
    return True


def waits_on_parked_answers(text, signal, transcript_path, workspace, page_url, goal=None):
    """True when a NEEDED: stop is the honest wait the declaration records: every open item is pending on a
    question parked on this session's page, and every NEEDED: line names one of those questions by its short id
    and asks nothing new. Such a line may stand again until the operator answers; it is not a repeat. The pinned
    goal quoted on the line, which the goal veto asks for, is the operator's words and never a new question, even
    when it ends in a question mark."""
    if signal != "allow:blocker-token":
        return False
    state = pending_items(transcript_path, workspace)
    if not state or state[0] or not state[1]:
        return False
    if not parked_before_marked(transcript_path, state[1], page_url):
        return False
    shorts = {pending.split("-", 1)[0].lower() for pending in state[1]}
    lines = [payload for payload in RE_NEEDED_LINE.findall(RE_FENCE.sub("", text or ""))
             if not RE_NEEDED_NEGATED.match(payload)]
    return bool(lines) and all("?" not in without_quoted_goal(line, goal)
                               and shorts & set(RE_SHORT_ID_MENTION.findall(line.lower())) for line in lines)


def without_quoted_goal(line, goal):
    """The line with every quoted span holding exactly the goal's words removed."""
    wanted = normalised_words(goal or "")
    if not wanted:
        return line
    return RE_QUOTED_SPAN.sub(lambda span: "" if normalised_words(span.group(0)) == wanted else span.group(0), line)


def last_operator_marker(transcript_path):
    """An identity for the newest record the operator typed, a turn or a mid-turn message, else None: its uuid,
    else its promptId, else its byte offset in the transcript, so two distinct operator records never share one."""
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as handle:
            end, carry = size, b""
            while end > 0:
                start = max(0, end - OPERATOR_SCAN_CHUNK_BYTES)
                handle.seek(start)
                lines = (handle.read(end - start) + carry).split(b"\n")
                offsets, position = [], start
                for raw in lines:
                    offsets.append(position)
                    position += len(raw) + 1
                if start > 0:
                    carry = lines.pop(0)
                    offsets.pop(0)
                else:
                    carry = b""
                for raw, offset in zip(reversed(lines), reversed(offsets)):
                    if b'"human"' not in raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        continue
                    if is_human_record(record):
                        if record.get("uuid"):
                            return record.get("uuid")
                        if record.get("promptId"):
                            return "prompt:" + str(record.get("promptId"))
                        return "offset:" + str(offset)
                end = start
    except (OSError, TypeError):
        return None
    return None


WAIT_SIGNALS = ("allow:queued-for-gate", "allow:waiting-on-own-gate")


def earning_asks(text, signal):
    """The asks that earned a stop, each on its own: every NEEDED: payload naming a blocker, or the closing question."""
    if signal == "allow:blocker-token":
        return [p.strip() for p in RE_NEEDED_LINE.findall(RE_FENCE.sub("", text or ""))
                if not RE_NEEDED_NEGATED.match(p)]
    if signal == "allow:question-mark":
        question = closing_question(text)
        return [question] if question else []
    return []


def wait_lines(text, signal):
    """The QUEUED: and WAITING: lines of a wait stop, judged by the page read alone: they are no ask, so they never
    enter the pending asks a repeat check matches against."""
    if signal not in WAIT_SIGNALS:
        return []
    return [line.strip() for line in RE_FENCE.sub("", text or "").splitlines()
            if line.lstrip().startswith(("QUEUED:", "WAITING:"))]


def load_pending_asks(state_root, safe_id, marker):
    """Every ask that earned a stop since the operator's newest message, oldest first."""
    try:
        with open(os.path.join(state_root, safe_id + PENDING_QUESTION_SUFFIX), "r", encoding="utf-8") as handle:
            pending = json.load(handle)
    except (OSError, ValueError):
        return []
    if not isinstance(pending, dict) or "marker" not in pending or pending.get("marker") != marker:
        return []
    asks = pending.get("asks")
    if isinstance(asks, list):
        return [ask for ask in asks if isinstance(ask, str) and ask.strip()]
    question = pending.get("question")
    return [question] if isinstance(question, str) and question.strip() else []


def save_pending_asks(state_root, safe_id, marker, asks, refused=False):
    """None once the asks are saved, else the path that could not be written: an ask the gate forgets is one it
    lets be asked again, so the caller refuses the stop rather than lose it unseen. refused marks asks a stop the gate
    refused put to the operator: its text reached them all the same."""
    path = os.path.join(state_root, safe_id + PENDING_QUESTION_SUFFIX)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"marker": marker, "asks": asks[-PENDING_ASKS_KEPT:], "refused": bool(refused)}, handle)
    except OSError:
        return path
    return None


def pending_asks_from_refused_stop(state_root, safe_id, marker):
    """True when the asks pending since the operator's newest message were recorded by a stop the gate refused: the
    operator read that stop's text, so a later stop in the same turn owes no second ask."""
    try:
        with open(os.path.join(state_root, safe_id + PENDING_QUESTION_SUFFIX), "r", encoding="utf-8") as handle:
            pending = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(pending, dict) and pending.get("marker") == marker and pending.get("refused") is True


WAKE_OPENING = "a peer or a task started this turn"
REFUSED_OPENING = "an earlier stop of yours this turn was refused after its ask reached the operator"


def turn_reason(reason, refused_turn):
    """A wake reason, opened for a turn that a refused stop's delivered ask continues rather than a peer or a task."""
    return reason.replace(WAKE_OPENING, REFUSED_OPENING, 1) if refused_turn else reason


def names_unsaved_path(text, path):
    """True when the stop's own text names the path the gate could not write: the operator then sees why the ask
    went unrecorded, and the session needs no state file to end its turn, so a persistent failure never traps it."""
    return os.path.normcase(path) in os.path.normcase(text or "")


PENDING_UNSAVED_REASON = ("STOP GATE: your stop puts an ask to the operator, but the gate could not record it at {path},"
                          " so it could not tell a repeat of it from a new ask. Nothing you wrote is wrong; end the turn"
                          " again with the same ask, naming this path exactly and why it could not be written (its"
                          " length, a missing folder, access): a stop that names it is let through.\n" + STOP_RULES_POINTER)


def carries_attribution_trailer(text):
    """True when a line of the message is a Claude attribution trailer.

    Unlike every other detector here this reads the RAW text, fences included, because the fenced
    commit-message block is precisely where the trailer does its damage: the operator copies that block
    into `git commit`, so a trailer inside it reaches history.
    """
    if not text:
        return False
    return bool(RE_ATTRIBUTION_TRAILER.search(text))


def repo_relative(path, workspace):
    """(repo, repo-relative path with forward slashes) for a file inside a workspace repo, else None."""
    try:
        full = os.path.abspath(path)
        base = os.path.abspath(workspace)
        prefix = base + os.sep
        if not full.lower().startswith(prefix.lower()):
            return None
        parts = full[len(prefix):].split(os.sep)
        if len(parts) < 2 or not parts[0]:
            return None
        if not os.path.isdir(os.path.join(base, parts[0], ".git")):
            return None
        return parts[0], "/".join(parts[1:])
    except Exception:
        return None


def porcelain_paths(output):
    """The paths in `git status --porcelain` output: two status columns, a space, then the path, with a
    rename written as `old -> new` (the new name is the one that is dirty) and odd names quoted."""
    paths = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        if len(entry) >= 2 and entry[0] == '"' and entry[-1] == '"':
            entry = entry[1:-1]
        entry = entry.replace("\\", "/").rstrip("/")
        if entry:
            paths.append(entry)
    return paths


def dirty_paths(names, workspace, timeout=None):
    """{repo: [dirty paths]} for every repo in `names` whose git call SUCCEEDED, an empty list meaning
    clean. A repo git could not read is left out entirely, never reported as clean: this dict feeds the
    staleness check, and "clean because git failed" would convict a fresh block.

    A git that is missing or fails yields no verdict. A slow git is waited for: a time limit here would let a
    loaded machine skip the commit-message checks.

    Parallel because it is not free. Measured on this machine, 16 repos serially cost ~2.7s and in
    parallel ~1.7s -- which is exactly why the caller passes only the repos the SESSION edited, usually
    one or two, rather than every repo in the workspace.
    """
    names = sorted(set(n for n in names if n))
    if not names:
        return {}

    def check(name):
        try:
            out = subprocess.run(
                ["git", "-C", os.path.join(workspace, name), "status", "--porcelain", "--untracked-files=all"],
                capture_output=True, text=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if out.returncode != 0:
                return name, None
            return name, porcelain_paths(out.stdout)
        except Exception:
            return name, None

    try:
        with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
            return {name: paths for name, paths in pool.map(check, names) if paths is not None}
    except Exception:
        return {}


def remember_edited_paths(state_root, safe_id, touched):
    """Union this turn's written paths, per repo, into the session's record and return the whole thing
    as {repo: set(paths) or None}. None marks a repo remembered by an older gate that stored names only;
    for it the whole tree is owed, as before, and nothing can be called stale.

    Kept in its own file rather than the block-counter state, because that one is written only on a
    block and read only mid-turn. The rule this serves is about work left dirty ACROSS turns -- three
    lockfiles once sat uncommitted from 9/1 to 9/3 -- so the set has to survive an allow.
    """
    path = os.path.join(state_root, safe_id + ".repos.json")
    repos = set()
    paths = {}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            repos = set(stored.get("repos", []))
            paths = {r: set(v) for r, v in (stored.get("paths") or {}).items() if isinstance(v, list)}
    except Exception:
        repos, paths = set(), {}
    before = (set(repos), {r: set(v) for r, v in paths.items()})
    for repo, files in touched.items():
        repos.add(repo)
        paths.setdefault(repo, set()).update(files)
    if (repos, paths) != before:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"repos": sorted(repos), "paths": {r: sorted(v) for r, v in paths.items()}},
                          fh, separators=(",", ":"))
        except Exception:
            pass
    return {repo: paths.get(repo) for repo in repos}


def content_digest(data):
    """sha256 of file bytes with CRLF folded to LF, so a working copy and its committed blob compare equal
    whichever line endings the checkout wrote."""
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def remember_written_digests(state_root, safe_id, digests):
    """Record what each file held when this session last wrote it, and return {repo: {path: digest}}.

    `digests` is this turn's writes as {repo: {path: digest or None}}; None (the file is gone) forgets the
    path. Taken at the stop that ends the writing turn, so a later turn can ask whether HEAD now holds
    exactly that content -- the session's edit landed -- when the file is dirty again with someone else's.
    """
    path = os.path.join(state_root, safe_id + ".written.json")
    written = {}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                written = {r: dict(v) for r, v in json.load(fh).items() if isinstance(v, dict)}
    except Exception:
        written = {}
    if digests:
        for repo, files in digests.items():
            entry = written.setdefault(repo, {})
            for rel, digest in files.items():
                if digest:
                    entry[rel] = digest
                else:
                    entry.pop(rel, None)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(written, fh, separators=(",", ":"))
        except Exception:
            pass
    return written


LANDED_HISTORY_DEPTH = 50


def superseded_after_landing(repo_path, digests, timeout=None):
    """The paths in {path: digest} whose working copy no longer holds what this session last wrote, while that
    content landed in one of the path's last commits: the session's edit was committed and a later writer
    changed the file since, so the dirt on it now is theirs. Any failure supersedes nothing."""
    superseded = set()
    for rel in sorted(digests):
        try:
            with open(os.path.join(repo_path, *rel.split("/")), "rb") as fh:
                if content_digest(fh.read()) == digests[rel]:
                    continue
            log = subprocess.run(
                ["git", "-C", repo_path, "log", "-n", str(LANDED_HISTORY_DEPTH), "--format=", "--raw", "--no-abbrev",
                 "--", rel],
                capture_output=True, text=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            blobs = [line.split()[3] for line in log.stdout.splitlines()
                     if line.startswith(":") and len(line.split()) > 4]
            if log.returncode != 0 or not blobs:
                continue
            out = subprocess.run(
                ["git", "-C", repo_path, "cat-file", "--batch"],
                input="".join(blob + "\n" for blob in blobs).encode("utf-8"),
                capture_output=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            data, position = out.stdout, 0
            for _ in blobs:
                end = data.index(b"\n", position)
                header = data[position:end].split()
                position = end + 1
                if len(header) != 3 or header[1] != b"blob":
                    continue
                size = int(header[2])
                if content_digest(data[position:position + size]) == digests[rel]:
                    superseded.add(rel)
                    break
                position += size + 1
        except Exception:
            continue
    return superseded


def landed_in_head(repo_path, digests, timeout=None):
    """The paths in {path: digest} whose HEAD blob holds exactly that content, read in one
    `git cat-file --batch`. Any failure lands nothing, which leaves the file owed as it was before."""
    rels = sorted(digests)
    if not rels:
        return set()
    try:
        out = subprocess.run(
            ["git", "-C", repo_path, "cat-file", "--batch"],
            input="".join("HEAD:{0}\n".format(r) for r in rels).encode("utf-8"),
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode != 0:
            return set()
        data, position, landed = out.stdout, 0, set()
        for rel in rels:
            end = data.index(b"\n", position)
            header = data[position:end].split()
            position = end + 1
            if len(header) != 3 or header[1] != b"blob":
                continue
            size = int(header[2])
            if content_digest(data[position:position + size]) == digests[rel]:
                landed.add(rel)
            position += size + 1
        return landed
    except Exception:
        return set()


def same_file(a, b):
    """Two path spellings name one file when the components of one end the other: a block may write the
    path repo-relative, workspace-relative or as a bare name, and porcelain writes it repo-relative."""
    pa = [p for p in a.replace("\\", "/").lower().strip("./").split("/") if p]
    pb = [p for p in b.replace("\\", "/").lower().strip("./").split("/") if p]
    if not pa or not pb:
        return False
    short, long_ = (pa, pb) if len(pa) <= len(pb) else (pb, pa)
    return long_[-len(short):] == short


def block_file_lines(text):
    """The path tokens on file-list lines of every fenced block in the message."""
    tokens = []
    for body in RE_FENCE_BODY.findall(text):
        for line in body.split("\n"):
            candidate = RE_FILES_LABEL.sub("", line.strip())
            candidate = re.sub(r"^[-*]\s+", "", candidate)
            if not candidate:
                continue
            parts = [p for p in re.split(r"[\s,]+", candidate) if p]
            if all(RE_PATH_TOKEN.match(p) and RE_PATH_SHAPED.search(p) for p in parts):
                tokens.extend(p.strip("`,") for p in parts)
    return tokens


def resolve_named(named, known):
    """Assign each path a block lists to the ONE session repo it can mean, or report it ambiguous.

    same_file compares path endings only, so a bare `TESTING.md` listed for the dirty repo used to match
    the same name in every other repo the session had edited: a correct block was convicted as stale for a
    repo already committed, and a block for one repo covered another. A path written with a session repo's
    name first belongs to that repo alone; an unqualified one belongs to the single session repo holding a
    file of that name, and to none when several do.

    `known` is {repo: paths the session is known to have in it}. Returns ({repo: [tokens]}, {token: [repos]}).
    """
    by_lower = {repo.lower(): repo for repo in known}
    claimed = {repo: [] for repo in known}
    ambiguous = {}
    for token in named:
        parts = [p for p in token.replace("\\", "/").split("/") if p]
        if len(parts) > 1 and parts[0].lower() in by_lower:
            claimed[by_lower[parts[0].lower()]].append(token)
            continue
        owners = sorted(repo for repo, paths in known.items() if any(same_file(p, token) for p in paths))
        if len(owners) == 1:
            claimed[owners[0]].append(token)
        elif owners:
            ambiguous[token] = owners
    return claimed, ambiguous


def judge_commit_blocks(text, session_paths, dirty, landed=None):
    """Compare what the message's blocks NAME with what git says is dirty NOW.

    Returns (owed, stale, missing, ambiguous). `owed` is {repo: [paths]} -- the session's own written files
    that are still dirty, or the whole tree for a repo remembered without paths. A file in `landed`
    ({repo: set(paths)}) is not owed: HEAD holds what the session last wrote to it, so the dirt on it now is
    another writer's. `stale` is {repo: [paths]}
    -- files this session wrote that a block still lists although they are clean now. `missing` is the
    owed repos no block covers. `ambiguous` is {path: [repos]} -- a bare listed path that names a file in
    several session repos and so counts for none. Only repos git answered for (keys of `dirty`) are judged.
    """
    owed = {}
    for repo, present in dirty.items():
        remembered = session_paths.get(repo)
        if remembered is None:
            files = sorted(present)
        else:
            done = (landed or {}).get(repo, ())
            files = sorted(p for p in present if any(same_file(p, s) for s in remembered)
                           and not any(same_file(p, d) for d in done))
        if files:
            owed[repo] = files

    known = {repo: set(remembered) if remembered is not None else set(dirty.get(repo, ()))
             for repo, remembered in session_paths.items()}
    claimed, ambiguous = resolve_named(block_file_lines(text), known)
    stale = {}
    for repo, present in dirty.items():
        remembered = session_paths.get(repo)
        if not remembered:
            continue
        gone = [s for s in sorted(remembered) if not any(same_file(s, p) for p in present)]
        hits = [s for s in gone if any(same_file(s, n) for n in claimed.get(repo, ()))]
        if hits:
            stale[repo] = hits

    missing = [repo for repo, files in owed.items()
               if not any(same_file(f, n) for f in files for n in claimed.get(repo, ()))]
    return owed, stale, missing, ambiguous


def misstates_the_goal(text, goal):
    """True when a message carries a DONE: line and none of them restate the operator's pinned goal.

    The unit mismatch this closes: DONE: is defined against the original ask, the gate's window is one
    turn, and nothing ever compared the two. An audited session wrote 15 completion claims of which 10
    restated a sub-task, an audit, or a fix for the agent's own earlier error while the operator's
    goal stood unfinished -- and every one cleared the floor, because each was a true statement about
    something. The echo is what the drift protocol already relies on: a restatement of the assignment
    is the one thing an agent working on something narrower cannot produce without the mismatch
    showing.

    Verbatim after normalisation, deliberately. A word-overlap score would let "audited what is still
    outstanding" pass for "nothing outstanding", which is the exact substitution being caught. The
    operator controls the goal's length, so a short pin costs a short echo.

    A message with no DONE: line is not judged: the veto is about a claim, not about the absence of
    one. Fences are stripped first, as for every completion detector, so quoted output cannot trip
    it -- the lesson the second veto had to relearn.
    """
    if not text or not goal:
        return False
    claims = completion_claims(text) + queued_claims(text) + waiting_claims(text)
    if not claims:
        return False
    wanted = normalised_words(goal)
    return not any(wanted in normalised_words(claim) for claim in claims)


RE_GOAL_LINE = token_line("GOAL")


def ask_misstates_the_goal(text, goal):
    """True when a NEEDED: or closing-question stop carries none of the operator's pinned goal's words: not in a
    NEEDED: line, not in the closing question, and not on a line of its own opening GOAL:. An ask is a stop like
    any other, so it is held to the goal it pauses, which keeps a session from asking its way off the assignment.
    No pinned goal judges nothing."""
    if not text or not goal:
        return False
    stripped = RE_FENCE.sub("", text)
    lines = RE_NEEDED_LINE.findall(stripped) + RE_GOAL_LINE.findall(stripped) + [closing_question(stripped) or ""]
    wanted = normalised_words(goal)
    return not any(wanted in normalised_words(line) for line in lines)


def completion_claims(text):
    """The payloads of every line-anchored DONE: in the prose, fences stripped.

    Shared by the two vetoes that judge a claim rather than a turn: the goal veto asks what the claim
    says, the commit-block veto asks only whether one was made. Line-anchored on purpose -- "all done
    here" in a sentence is not a claim, and a DONE: quoted inside a fence is someone else's.
    """
    if not text:
        return []
    return RE_DONE_LINE.findall(RE_FENCE.sub("", text))


def queued_claims(text):
    """The payloads of every line-anchored QUEUED:, fences stripped.

    A queue says the work product is finished and only another session's work is left, so the goal veto
    reads these (and waiting_claims) beside completion_claims and a pinned goal must be echoed. The commit-block veto does not:
    the operator narrowed it to DONE: alone on 2026-09-20, because a queued session re-posting its blocks
    on every wait turn was the re-paste the content check exists to stop.
    """
    if not text:
        return []
    return RE_QUEUED_LINE.findall(RE_FENCE.sub("", text))


def queued_for_a_gate(text, messaged, transcript_path=None, temp_root=None, workspace=None, messaged_to=()):
    """A QUEUED: line while this session's declared work product is closed and it waits on something another
    session started: a peer's gate actually live on this machine in a turn that messaged a peer who can wake
    the session, a process holding this account's gate slot (Harness/Gates/GateSlot.ps1), or a peer the line
    names that this turn messaged and whose session is running on this machine.

    A message alone proves nothing: a session could queue behind work that was only announced, or send a
    message for the sake of this check, so a named peer must also be running here. The gate slot needs no
    message, because its holder may be no session at all (the alert-triage runner's gate), so there is no one
    to ask; it is read from the slot's file lock, never from the record beside it. The live gate, the slot and
    the closed declaration are read from the cadence hook, which owns all three. Any failure earns nothing, as
    on every allow path."""
    if not queued_claims(text) or not transcript_path:
        return False
    try:
        cadence = cadence_module()
        if cadence is None:
            return False
        root = cadence.TEMP_CLAUDE_ROOT if temp_root is None else temp_root
        closed = cadence.work_product_problem(
            transcript_path, cadence.WORKSPACE if workspace is None else workspace, root) is None
        if not closed:
            return False
        workspace = cadence.WORKSPACE if workspace is None else workspace
        if (messaged and cadence.live_peer_gate(transcript_path, temp_root=root) is not None) \
                or cadence.gate_slot_holder() is not None:
            return closure_problem(cadence, transcript_path, workspace, text, waits_on_gate=True) is None
        peer = queued_on_a_messaged_peer(text, messaged_to, transcript_path)
        if peer is None:
            return False
        return closure_problem(cadence, transcript_path, workspace, text, waits_on_gate=False,
                               blocked=peer_change_blocks(peer, transcript_path)) is None
    except Exception:
        return False


def closure_problem(cadence, transcript_path, workspace, text, waits_on_gate, blocked=None):
    """work_product_closure's reason this session's work product is not closed in fact (an edit no item names, a done
    item's repo ungated, an open standing item), else None. A done flag alone is the session's own claim."""
    import parking_lot_commits
    import work_product_closure
    said = []

    def approved(words):
        if not said:
            said.append(cadence.operator_texts(transcript_path))
        return cadence.operator_approved(words, said[0])

    session_id = os.path.splitext(os.path.basename(transcript_path))[0]
    settled = settled_lookup(cadence, transcript_path)
    return work_product_closure.closure_problem(
        cadence, transcript_path, workspace, session_id, text, waits_on_gate, parking_lot_commits.landing_state,
        parking_lot_commits.gate_status_module().EXCLUDED, approved=approved, failed_here=failed_on_this_tree,
        blocked=blocked, waits_on_open=lambda entry: waits_on_open_question(entry, settled))


def handoff_problem(text, transcript_path, workspace=None, settled=None):
    """For a NEEDED:, closing question or quiet wake: the closure reason the work product is not closed in fact, with
    no gate wait to excuse an ungated repo and only a measured block (gate_blocked) excusing one, else None. Fails
    open like every veto."""
    if not transcript_path:
        return None
    try:
        cadence = cadence_module()
        if cadence is None:
            return None
        workspace = cadence.WORKSPACE if workspace is None else workspace
        return closure_problem(cadence, transcript_path, workspace, text, False,
                               blocked=gate_blocked(cadence, transcript_path, workspace, text, settled))
    except Exception:
        return None


def gate_blocked(cadence, transcript_path, workspace, text, settled=None):
    """blocked(repo, checkout): True while no gate may run in the checkout yet, measured, never claimed. The cadence
    hook refuses a repo's gate while one of its declared items is open, so a repo with an item waiting on a parked
    question is blocked on that answer; and it refuses a Tools gate while another checkout's PASSED change is not on
    origin (peer_green_unlanded), which blocks only a stop asking to land that checkout by its path."""
    held = repos_waiting_on_answers(transcript_path, workspace, settled)
    landing = landing_asked(text)
    session_id = os.path.splitext(os.path.basename(transcript_path))[0]

    def blocked(repo, checkout):
        if repo in held:
            return True
        if not landing:
            return False
        if names_path(text, checkout) and merge_pending(checkout):
            return True
        if os.path.normcase(checkout) == os.path.normcase(os.path.join(workspace, repo)) \
                and held_by_a_peer_green(cadence, repo, workspace, session_id, text):
            return True
        try:
            found = cadence.peer_green_unlanded('pwsh -File "{0}"'.format(os.path.join(checkout, "gate.ps1")),
                                                workspace)
        except Exception:
            return False
        return bool(found) and names_path(text, str(found[0]))

    return blocked


def merge_pending(checkout):
    """True while the checkout holds an unconcluded merge, whose commit is the operator's: a landing ask naming that
    checkout waits on exactly that commit (work_product_closure.merge_in_progress)."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import work_product_closure
        return work_product_closure.merge_in_progress(checkout)
    except Exception:
        return False


def names_path(text, path):
    """True when the text names the path whole, never as the start of a longer name or of a path beneath it."""
    def spelled(value):
        return os.path.normcase(value).replace("/", "\\")
    return re.search(r"(?<![\w-]){0}(?![\w\\-])".format(re.escape(spelled(path))), spelled(text or "")) is not None


def held_by_a_peer_green(cadence, repo, workspace, session_id, text):
    """True while the repo's main checkout holds another session's PASSED gate record, completed after origin's newest
    commit, whose change is not on origin yet, and the stop names that checkout: no item can be worked there without
    staling that record, so the item waits on the landing the stop asks for. Measured, never claimed."""
    checkout = os.path.join(workspace, repo)
    if not names_path(text, checkout):
        return False
    try:
        import parking_lot_commits
        statuses = parking_lot_commits.gate_status_module()
        record = statuses.read_record(repo, statuses.checkout_state_dir(repo, checkout))
    except Exception:
        return False
    if not isinstance(record, dict) or record.get("verdict") != "PASSED" or record.get("session") == session_id:
        return False
    head = cadence.origin_head(checkout)
    completed = cadence.parse_timestamp(record.get("completedAt"))
    if head is None or head[1] is None or completed is None or completed <= head[1]:
        return False
    return not cadence.landed(checkout, head[0])


def unblocked_open_items(transcript_path, workspace, text, settled=None):
    """How many open declared items wait on no parked question and are not held by a peer's unlanded green change in
    their repo's main checkout (held_by_a_peer_green): the work a landing ask would walk away from."""
    try:
        cadence = cadence_module()
        workspace = cadence.WORKSPACE if workspace is None else workspace
        with open(cadence.work_product_path(transcript_path, workspace), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, AttributeError):
        return 0
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        return 0
    session_id = os.path.splitext(os.path.basename(transcript_path))[0]
    settled = settled_lookup(cadence, transcript_path, settled)
    count = 0
    for repo, entries in repos.items():
        unpended = [entry for entry in entries if isinstance(entry, dict) and entry.get("done") is not True
                    and not waits_on_open_question(entry, settled)] if isinstance(entries, list) else []
        if unpended and not held_by_a_peer_green(cadence, repo, workspace, session_id, text):
            count += len(unpended)
    return count


def repos_waiting_on_answers(transcript_path, workspace, settled=None):
    """The declared repos with an open item that names, in "pending", a parked question no read shows settled."""
    try:
        cadence = cadence_module()
        with open(cadence.work_product_path(transcript_path, workspace), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, AttributeError):
        return set()
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        return set()
    settled = settled_lookup(cadence, transcript_path, settled)
    return {repo for repo, entries in repos.items() if isinstance(entries, list)
            and any(isinstance(entry, dict) and entry.get("done") is not True
                    and waits_on_open_question(entry, settled) for entry in entries)}


def settled_question_ids(cadence, transcript_path):
    """The parked question ids, lowercased, that a read of this session's own page has ever shown answered, plus
    those this stop attempt's reads show answered or withdrawn, plus those this session withdrew with a write that
    landed. An item pending on one of them waits on nothing: the answer, or the withdrawal, is work."""
    settled = set()
    try:
        artifact_gate = cadence.artifact_gate_module()
    except Exception:
        artifact_gate = None
    if artifact_gate is not None:
        try:
            settled.update(str(doc_id).lower() for doc_id, _ in artifact_gate.answered_rows(transcript_path))
        except Exception:
            pass
        try:
            settled.update(own_withdrawn_ids(artifact_gate, transcript_path))
        except Exception:
            pass
    _, statuses = page_reads_this_turn(transcript_path)
    settled.update(str(doc_id).lower() for doc_id, status in statuses.items() if status in ("answered", "withdrawn"))
    return settled


def own_withdrawn_ids(artifact_gate, transcript_path):
    """The question ids, lowercased, this session withdrew: an ArtifactData write to questions setting status
    withdrawn whose result is no error. A withdrawn question is never reopened, so no later read is needed. Only a
    line naming "withdrawn" beside ArtifactData, or the result of such a call by its id, is parsed: every other line
    is passed over on a byte test."""
    calls, withdrawn = {}, set()
    with open(transcript_path, "rb") as handle:
        for raw in handle:
            if not (b'"withdrawn"' in raw and b'"ArtifactData"' in raw) and not (
                    calls and b'"tool_use_id"' in raw
                    and any(str(call_id).encode() in raw for call_id in calls)):
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            content = (record.get("message") or {}).get("content") if isinstance(record, dict) else None
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                call = part.get("input") if isinstance(part.get("input"), dict) else {}
                if part.get("type") == "tool_use" and part.get("name") == "ArtifactData" \
                        and call.get("action") in artifact_gate.WRITE_ACTIONS:
                    ids = [str(entry.get("doc_id")).lower() for entry in artifact_gate.write_entries(call)
                           if entry.get("collection") == PARKED_COLLECTION and entry.get("op") in ("set", "update")
                           and isinstance(entry.get("data"), dict) and entry["data"].get("status") == "withdrawn"]
                    if ids and part.get("id"):
                        calls[part.get("id")] = ids
                elif part.get("type") == "tool_result" and not part.get("is_error") \
                        and part.get("tool_use_id") in calls:
                    withdrawn.update(calls.pop(part["tool_use_id"]))
    return withdrawn


def settled_lookup(cadence, transcript_path, settled=None):
    """settled as a zero-argument callable returning the settled ids: the caller's own (decide() shares one per stop),
    else one that reads them at most once. Nothing is read until an item actually carries "pending"."""
    if callable(settled):
        return settled
    if settled is not None:
        return lambda: settled
    found = []

    def lookup():
        if not found:
            found.append(settled_question_ids(cadence, transcript_path))
        return found[0]
    return lookup


def waits_on_open_question(entry, settled):
    """True when a declared item names, in "pending", a parked question no read shows answered or withdrawn. settled
    is a zero-argument callable (settled_lookup), called only for an item that carries "pending"."""
    pending = parked_id(entry)
    return bool(pending) and pending.lower() not in settled()


def failed_on_this_tree(repo, workspace, session_id, checkout):
    """True when the gate record a message for this checkout would carry failed on exactly the tree as it is now, so
    the repo's next step is a fix rather than a gate. Never while the checkout holds an unconcluded merge: the gate
    fails there at its origin check until the merge is committed, and that commit is the operator's, not a fix."""
    import parking_lot_commits
    import work_product_closure
    if work_product_closure.merge_in_progress(checkout):
        return False
    state, record = parking_lot_commits.gate_state(repo, workspace, session_id, checkout)
    if state != "RED" or not isinstance(record, dict):
        return False
    gate_status = parking_lot_commits.gate_status_module()
    recorded = record.get(gate_status.FIELD_MANIFEST)
    return isinstance(recorded, dict) and recorded == gate_status.manifest(
        gate_status.recorded_path(record, "repoPath", checkout))


def names_session_id(text, session_id):
    """True when the text names the session by its full id or its first eight characters, never by its name, which
    is neither unique nor stable across a restart."""
    session_id = str(session_id or "").lower()
    return bool(session_id) and any(re.search(r"(?<![\w-])" + re.escape(form) + r"(?![\w])", text.lower())
                                    for form in (session_id, session_id[:8]))


def queued_on_a_messaged_peer(text, messaged_to, transcript_path):
    """The session id of a peer that a QUEUED: line names by id, that this turn's SendMessage addressed by its
    socket, and whose session the registry shows running on this machine, else None."""
    recipients = {recipient.lower() for recipient in messaged_to}
    if not recipients:
        return None
    payloads = " ".join(queued_claims(text))
    for peer in reachable_peers(transcript_path):
        session_id = str(peer.get("sessionId") or "")
        socket = str(peer.get("messagingSocketPath") or "").lower()
        addressed = bool(socket) and any(socket in r for r in recipients)
        if session_id and addressed and names_session_id(payloads, session_id):
            return session_id
    return None


SESSION_WORKTREES = os.path.join(os.environ.get("USERPROFILE", ""), "source", "worktrees")


def peer_change_blocks(peer, transcript_path, root=None):
    """blocked(repo, checkout) for a QUEUED: line on a messaged peer: True while that peer's own worktree of the repo,
    <root>\\<its session id>\\<repo>, holds a change origin lacks, so this session's gate would judge a tree the
    peer's landing is about to change. Measured from the peer's checkout, never claimed."""
    wanted = str(peer or "").lower()
    session_ids = [str(entry.get("sessionId") or "") for entry in reachable_peers(transcript_path)
                   if wanted and str(entry.get("sessionId") or "").lower() == wanted]
    base = SESSION_WORKTREES if root is None else root

    def blocked(repo, checkout):
        for session_id in session_ids:
            peer_checkout = os.path.join(base, session_id, repo)
            if (session_id and os.path.normcase(peer_checkout) != os.path.normcase(checkout)
                    and os.path.exists(os.path.join(peer_checkout, ".git")) and checkout_unlanded(peer_checkout)):
                return True
        return False

    return blocked


def checkout_unlanded(checkout):
    import work_product_closure
    return work_product_closure.checkout_unlanded(checkout)


def waiting_claims(text):
    """The payloads of every line-anchored WAITING:, fences stripped. Judged by the goal veto and not the
    commit-block veto, for the reasons queued_claims gives."""
    if not text:
        return []
    return RE_WAITING_LINE.findall(RE_FENCE.sub("", text))


def waiting_on_own_gate(text, transcript_path, temp_root=None, workspace=None, slot_directory=None):
    """A WAITING: line while something this session started is still running: a background task whose
    completion notice has not arrived (a gate or any other command), its own gate holding the gate slot, or a
    permission matrix it requested whose run is still open.

    The cadence hook owns when a background task is still running, so this borrows its collect() and is_live()
    rather than reimplementing them, and shares its scan cache so the walk resumes from the last byte either
    hook read. Only reached when a WAITING: line is present, so every other stop pays nothing. Any failure
    earns nothing: unlike a veto, an allow path that errs must err towards the block.
    """
    if not waiting_claims(text) or not transcript_path:
        return False
    try:
        cadence = cadence_module()
        if cadence is None:
            return False
        root = cadence.TEMP_CLAUDE_ROOT if temp_root is None else temp_root
        workspace = cadence.WORKSPACE if workspace is None else workspace
        if cadence.work_product_problem(transcript_path, workspace, root) is not None:
            return False
        if not own_work_running(cadence, transcript_path, root, slot_directory):
            return False
        waits_on_gate = own_gate_holds_slot(cadence, transcript_path, slot_directory) or own_gate_running(
            cadence, transcript_path, root)
        return closure_problem(cadence, transcript_path, workspace, text, waits_on_gate) is None
    except Exception:
        return False


def own_gate_running(cadence, transcript_path, root):
    """True while a gate this session launched in the background is still live: the one wait that excuses a done item
    whose repo is not yet gated."""
    gates, _ = cadence.collect(cadence.session_files(transcript_path), cadence.classify, unfinished_only=True,
                               cache_root=os.path.join(root, cadence.SCAN_CACHE_DIRECTORY))
    now = time.time()
    return any(gate["state"] == cadence.BACKGROUND and cadence.is_live(gate, now) for gate in gates)


def own_work_running(cadence, transcript_path, root, slot_directory=None):
    """True while a background task this session launched is still live, its own gate holds the gate slot, or
    a permission matrix it requested is still open."""
    tasks, _ = cadence.collect(cadence.session_files(transcript_path), lambda command: True, unfinished_only=True,
                               cache_root=os.path.join(root, cadence.SCAN_CACHE_DIRECTORY))
    now = time.time()
    if any(task["state"] == cadence.BACKGROUND and cadence.is_live(task, now) for task in tasks):
        return True
    if own_gate_holds_slot(cadence, transcript_path, slot_directory):
        return True
    return own_matrix_open(cadence.session_files(transcript_path))


def own_matrix_open(paths):
    """True while the newest permission matrix this session's transcripts show it requesting is still open: its
    request file still waits for the poll, or the run file beside it records a start at or after the request.
    The poll removes the run file when it scores the run, so a finished matrix holds nothing open."""
    newest = None
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if "requested the permission matrix in" not in line:
                        continue
                    for match in RE_MATRIX_REQUEST.finditer(line.replace("\\\\", "\\")):
                        if newest is None or match.group("stamp") > newest[1]:
                            newest = (match.group("path").strip(), match.group("stamp"))
        except OSError:
            continue
    if newest is None:
        return False
    request, stamp = newest
    if os.path.isfile(request):
        return True
    run_file = os.path.join(os.path.dirname(os.path.dirname(request)), MATRIX_RUN_FILE)
    try:
        with open(run_file, "r", encoding="utf-8-sig") as handle:
            started = str(json.load(handle).get("StartedAt") or "")
    except (OSError, ValueError, AttributeError):
        return False
    if not started.endswith(("+00:00", "Z")):
        return False
    requested = datetime.strptime(stamp, "%Y%m%d%H%M%S").strftime("%Y-%m-%dT%H:%M:%S")
    return started[:19] >= requested


def own_gate_holds_slot(cadence, transcript_path, slot_directory=None):
    """True while this account's gate slot is held by a gate this session started. The slot's file lock is held only
    while the gate's process lives, and GateSlot.ps1 names the session in the holder's owner, so this sees a gate
    whose launching tool call came back rejected after the process had already started, which the transcript does
    not show as a launch."""
    holder = cadence.gate_slot_holder(slot_directory)
    session = os.path.splitext(os.path.basename(transcript_path or ""))[0]
    return bool(holder and session and "session " + session in str(holder.get("owner", "")))


def wake_task_scheduled(runner=subprocess.run):
    """True while the master's wake task is registered, not disabled, and has a next run time, so an idle master
    is certain to be started again."""
    try:
        done = runner(["schtasks", "/Query", "/TN", MASTER_WAKE_TASK, "/FO", "CSV", "/NH", "/V"],
                      capture_output=True, text=True)
    except Exception:
        return False
    if done.returncode != 0:
        return False
    import csv
    rows = [row for row in csv.reader(done.stdout.splitlines()) if len(row) > 3]
    if not rows:
        return False
    next_run, status = rows[0][2].strip(), rows[0][3].strip()
    return status.lower() != "disabled" and next_run not in ("", "N/A")


WAKE_TASK_CHECK = wake_task_scheduled


def master_idle(text, transcript_path, temp_root=None, workspace=None, environ=None):
    """An IDLE: line from the master session, with its declared work product closed and its wake task scheduled.
    Only reached when an IDLE: line is present, so every other stop pays nothing. Any failure earns nothing."""
    if not text or not RE_IDLE_LINE.search(RE_FENCE.sub("", text)) or not transcript_path:
        return False
    if not runs_as(MASTER_SESSION_VARIABLE, environ):
        return False
    try:
        cadence = cadence_module()
        if cadence is None:
            return False
        root = cadence.TEMP_CLAUDE_ROOT if temp_root is None else temp_root
        if cadence.work_product_problem(
                transcript_path, cadence.WORKSPACE if workspace is None else workspace, root) is not None:
            return False
        return bool(WAKE_TASK_CHECK())
    except Exception:
        return False


def master_wake_sent(messaged, environ=None):
    """The wake process's stop: it ran from the wake task (MASTER_WAKE on the master's machine) and this turn
    called SendMessage. It has no operator to quote, no goal and no page, so nothing else could earn it."""
    return bool(messaged) and runs_as(MASTER_WAKE_VARIABLE, environ)


def poke_sent(messaged, environ=None):
    """A poke process's stop: Send-SessionPoke.ps1 started it (POKE_VARIABLE in its own environment, on whichever
    machine the target session runs) and this turn called SendMessage. Like the wake process, it has no operator to
    quote, no goal, no page and no board row, so nothing else could earn it."""
    environ = os.environ if environ is None else environ
    return bool(messaged) and environ.get(POKE_VARIABLE) == "1"


MASTER_PUSH_REASON = ("STOP GATE: you are the master, and nobody watches this conversation, so the operator learns of"
                      " a question only from a push: {ids} is open on your page and no PushNotification has named it."
                      " Send one PushNotification (status proactive), under 200 characters, naming {ids} and your"
                      " parking-lot page link, then end the turn. Each question needs naming once.\n"
                      + STOP_RULES_POINTER)
UNREGISTERED_REASON = ("STOP GATE: {problem} The operator's /goal is held back until it lands. Then ask them for the"
                       " goal, or carry on with it.\n" + STOP_RULES_POINTER)
UNCONSENTED_REASON = ("STOP GATE: {problem} Then ask the operator for the goal, or carry on with it.\n"
                      + STOP_RULES_POINTER)
UNAUDITED_REASON = ("STOP GATE: a stop that claims to be honest is audited first, and {problem}\n" + STOP_RULES_POINTER)
FILLER_STRIKE_WHY = ("ended two turns in a row on a run of refused stops with no tool call between them and no operator "
                     "message between the two (eject:filler-streak twice in the Stop gate's decisions.log)")


def audit_required(transcript_path, session_id, workspace, state_root, temp_root=None):
    """The reason a stop that claims an honest stop earns nothing yet (declared_items.audit_required_problem), else
    None: the session's work-product declaration and its session-audit cache, read from the files, never from the
    session's narration. The decisions log beside this gate's state supplies the ejects the audit must catalog."""
    import declared_items
    cadence = cadence_module()
    path = cadence.work_product_path(transcript_path, workspace)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = None
    data = data if isinstance(data, dict) else None
    audit_root = os.path.join(temp_root, "audits", "session-audit") if temp_root else None
    return declared_items.audit_required_problem(session_id, data, path, root=audit_root,
                                                 decisions_log=os.path.join(state_root, "decisions.log"))


def eject_strikes(state_root, safe_id, marker):
    """The number of filler-streak ejects this one makes since the operator's newest message, this one counted:
    <session>.ejects.json keeps the marker of that message and the count, and a new marker starts the count over.
    A file that cannot be written counts this eject as the first, so the gate never ends a session over lost state."""
    path = os.path.join(state_root, safe_id + EJECT_STRIKE_SUFFIX)
    count = 1
    try:
        with open(path, "r", encoding="utf-8") as handle:
            kept = json.load(handle)
        if isinstance(kept, dict) and kept.get("marker") == marker:
            count = int(kept.get("count", 0)) + 1
    except (OSError, ValueError, TypeError):
        count = 1
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"marker": marker, "count": count}, handle)
    except OSError:
        pass
    return count


def quarantine_for_filler(session_id, quarantine_root, evidence):
    """Quarantines a session for its second filler-streak eject with no operator message between, through
    session_quarantine, and returns the stop reason. A quarantine module that cannot load still ends the turn with
    the eject, since the record is the only thing lost."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import session_quarantine
    except Exception:
        return None
    return session_quarantine.quarantine(session_id, FILLER_STRIKE_WHY, evidence, quarantine_root,
                                         template=session_quarantine.FILLER_STOP_REASON)


def prompt_rule_applies(transcript_path, effective_at=None):
    """True when the session started no earlier than the prompt gate's rule took effect in this checkout, the same
    cutoff the prompt gate itself applies, so a session already running is never asked to register."""
    effective_at = effective_at if effective_at is not None else invoke_prompt_gate.rule_effective_at()
    return invoke_prompt_gate.started_since(transcript_path, effective_at)


MASTER_PUSH_READ_REASON = ("STOP GATE: you are the master, and a stop that leaves a parked question open owes the"
                           " operator a push naming it, but no read of your page this turn shows which are open."
                           " Read it (ArtifactData query on questions where status == answered), push any open"
                           " question no PushNotification has named, then end the turn.\n" + STOP_RULES_POINTER)


def session_withdrawn_ids(transcript_path, page_url=None):
    """The doc ids of the questions this session withdrew on its page, from its own ArtifactData writes that came
    back without an error."""
    withdrawn = {}
    landed = set()
    try:
        with open(transcript_path, "rb") as handle:
            for raw in handle:
                if b'"ArtifactData"' not in raw and b'"tool_result"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                content = (record.get("message") or {}).get("content") if isinstance(record, dict) else None
                for part in content if isinstance(content, list) else []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool_result" and not part.get("is_error"):
                        landed.add(part.get("tool_use_id"))
                    if not (part.get("type") == "tool_use" and part.get("name") == "ArtifactData"
                            and isinstance(part.get("input"), dict)):
                        continue
                    call = part["input"]
                    if page_url and call.get("url") != page_url:
                        continue
                    writes = call.get("writes") if call.get("action") == "batch" else [dict(call, op=call.get("action"))]
                    for write in writes if isinstance(writes, list) else []:
                        data = write.get("data") if isinstance(write, dict) else None
                        if (write.get("collection") == PARKED_COLLECTION and isinstance(data, dict)
                                and data.get("status") == "withdrawn"):
                            withdrawn.setdefault(part.get("id"), set()).add(str(write.get("doc_id") or ""))
    except (OSError, TypeError):
        return set()
    return {doc_id for use_id, ids in withdrawn.items() if use_id in landed for doc_id in ids}


def pushed_text(transcript_path):
    """Every PushNotification message this session sent, lower-cased and joined, whatever its result: a "not sent"
    result means only that the operator was already looking."""
    messages = []
    try:
        with open(transcript_path, "rb") as handle:
            for raw in handle:
                if b'"PushNotification"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except ValueError:
                    continue
                content = (record.get("message") or {}).get("content") if isinstance(record, dict) else None
                for part in content if isinstance(content, list) else []:
                    if (isinstance(part, dict) and part.get("type") == "tool_use"
                            and part.get("name") == "PushNotification" and isinstance(part.get("input"), dict)):
                        messages.append(str(part["input"].get("message") or ""))
    except (OSError, TypeError):
        return ""
    return "\n".join(messages).lower()


def unpushed_parked_ids(transcript_path, page_url=None):
    """The short ids (q7) of this session's parked questions still open that no PushNotification has named, or None
    while no read of the page this turn covers every question: the operator answers there, so without that read an
    old answered question would be pushed again. Open is neither shown answered or withdrawn by that read nor
    withdrawn by the session itself."""
    parked = parked_questions(transcript_path, page_url)
    if not parked:
        return []
    covers_all, statuses = page_reads_this_turn(transcript_path, page_url)
    if not covers_all:
        return None
    withdrawn = session_withdrawn_ids(transcript_path, page_url)
    pushed = pushed_text(transcript_path)
    missing = []
    for _, doc_id in parked:
        if statuses.get(doc_id) in ("answered", "withdrawn") or doc_id in withdrawn:
            continue
        short = doc_id.split("-", 1)[0].lower()
        if short and short not in missing and not re.search(
                r"(?<![a-z0-9])" + re.escape(short) + r"(?![0-9])", pushed):
            missing.append(short)
    return missing


WAIT_CLAIM_NOTE = "Your {token} line was not accepted: {why}."

DECLARATION_PLACE = "work-products/<session>.json under %TEMP%\\claude (CLAUDE_CODE_TMPDIR when set)"

OPEN_ITEM_REASON = ("STOP GATE: your NEEDED: or question names none of the {count} open items you can work yourself"
                    " (first: {first}).\nWork them now with a tool call, or put four consecutive words of the item"
                    " in that line; all are in " + DECLARATION_PLACE + ".\n" + STOP_RULES_POINTER)

OPEN_ITEM_NAMING_WORDS = 4


def unworked_open_items(text, transcript_path, workspace=None):
    """The open items of this session's work-product declaration when the stop's NEEDED: lines and closing
    question name none of them, else []. A NEEDED: or question stop while declared work is open has to be
    about that work: a stop on someone else's pending answer while the session's own deliverable sits
    unfinished is the hand-off this veto exists to refuse. No declaration, or every item done, vetoes
    nothing, and like every veto it fails open on an error."""
    if not transcript_path:
        return []
    try:
        cadence = cadence_module()
        if cadence is None:
            return []
        parsed = cadence.read_work_product(cadence.work_product_path(
            transcript_path, cadence.WORKSPACE if workspace is None else workspace))
        if not parsed:
            return []
        declared, _ = parsed
        items = [item for entries in declared.values() for item, done, _ in entries if not done]
        if not items:
            return []
        stop_lines = RE_NEEDED_LINE.findall(RE_FENCE.sub("", text or "")) + [closing_question(text) or ""]
        spoken = " {0} ".format(" ".join(" ".join(stop_lines).lower().split()))

        def named(item):
            tokens = item.lower().split()
            size = min(OPEN_ITEM_NAMING_WORDS, len(tokens))
            return any(" {0} ".format(" ".join(tokens[i:i + size])) in spoken for i in range(len(tokens) - size + 1))

        return [] if any(named(item) for item in items) else items
    except Exception:
        return []


NEXT_ITEM_CLIP = 120
GATE_REASON_ACTION = "Keep working now, with a tool call."
NEXT_ITEM_ACTION = 'Your next open item, quoted from your declaration: "{item}". Work it now with a tool call.'


def first_open_item(transcript_path, workspace=None):
    """The first undone item of this session's work-product declaration, as one clipped line, or None. The item
    is session-written text, so it is flattened to one line and clipped before it is quoted into a block reason,
    and it can neither lengthen the reason nor read as an instruction. Fails open to None like every veto."""
    if not transcript_path:
        return None
    try:
        cadence = cadence_module()
        if cadence is None:
            return None
        parsed = cadence.read_work_product(cadence.work_product_path(
            transcript_path, cadence.WORKSPACE if workspace is None else workspace))
        if not parsed:
            return None
        declared, _ = parsed
        for entries in declared.values():
            for item, done, _ in entries:
                if not done:
                    printable = "".join(ch if ch.isprintable() else " " for ch in str(item))
                    flat = " ".join(printable.replace('"', "'").split())
                    return flat if len(flat) <= NEXT_ITEM_CLIP else flat[:NEXT_ITEM_CLIP - 3] + "..."
        return None
    except Exception:
        return None


RE_COMMIT_VERB = re.compile(r"\b(?:commit|push|check[- ]?in)\b(?!-)", re.IGNORECASE)
RE_COMMIT_NOUN_BEFORE = re.compile(
    r"(?<!\bhave\s)(?<!\blet\s)(?<!\bget\s)(?<!\bmake\s)"
    r"\b(?:the|a|an|this|that|its|each|every|which|whose|my|your|our|their)\s+"
    r"(?:(?!(?:to|and|or|then|please|should|can|could|will|would|must|may|might|shall|you|we|i|me|us|they|"
    r"operator|user|git)\b)[a-z'-]+\s+){0,2}\Z",
    re.IGNORECASE)
RE_COMMIT_NOUN_AFTER = re.compile(r"\s+(?:message|messages|block|blocks|hash|hashes|id|ids|sha|shas|log|history)\b",
                                  re.IGNORECASE)
RE_LANDING_CONDITION = re.compile(r"\b(?:until|once|after|when)\b(?P<rest>[^.\n]{0,80})", re.IGNORECASE)


def asks_to_land(text):
    """True when the text uses commit, push or check in as an action ("can you commit", "until you push"), never
    when the word is a noun ("the commit message"), part of a compound ("commit-block") or a past tense
    ("committed", "pushed"), which describe work rather than ask for it."""
    for match in RE_COMMIT_VERB.finditer(text or ""):
        if RE_COMMIT_NOUN_BEFORE.search(text, 0, match.start()) or RE_COMMIT_NOUN_AFTER.match(text, match.end()):
            continue
        return True
    return False
RE_QUESTION_SENTENCE = re.compile(r"[^.!?]*\?")

COMMIT_ASK_REASON = ("STOP GATE: your stop asks the operator to commit, push or check in, and carries no commit message"
                     " block.\nPut the drafted commit message for each repo in this reply, in a fenced block ending with"
                     " a line per file, so it is never a scroll back away; or drop the ask, since committing is the"
                     " operator's, and park a green repo's message with parking_lot.py commit <Repo> <message file>.\n"
                     + STOP_RULES_POINTER)


def commit_ask_without_message(text):
    """True when a NEEDED: line or the closing question asks the operator to commit, push or check in, and the message
    delivers no commit message block (a fenced block with file-list lines, not a pointer to one elsewhere). An ask
    to land work is only actionable with the message in hand. Agents never commit here, so an ask to push is an
    ask to commit first. Every question sentence of the closing line is an ask, so a trailing second question
    never hides the first; a status sentence on that line that mentions a commit is not one."""
    if not landing_asked(text):
        return False
    return not block_file_lines(text) or withholds_a_commit_block(text)


def landing_asked(text):
    """True when a NEEDED: line or a sentence of the closing question asks the operator to commit, push or check in,
    or a REFUSING:, QUEUED: or WAITING: line waits on one ("until you push")."""
    if not text:
        return False
    stripped = RE_FENCE.sub("", text)
    asks = RE_NEEDED_LINE.findall(stripped) + RE_QUESTION_SENTENCE.findall(closing_question(stripped) or "")
    conditions = RE_REFUSING_LINE.findall(stripped) + RE_QUEUED_LINE.findall(stripped) + RE_WAITING_LINE.findall(stripped)
    return (any(asks_to_land(ask) for ask in asks)
            or any(asks_to_land(found.group("rest")) for line in conditions
                   for found in RE_LANDING_CONDITION.finditer(line)))


LANDING_WORK_REASON = ("STOP GATE: your stop asks the operator to commit or push while your declaration holds {count}"
                       " open item(s) waiting on no parked question (first: \"{first}\"). Landing is the last step, never"
                       " a hand-off from work you can still do: finish those items, or park what blocks one and mark it"
                       " \"pending\" on that question, then ask.\n" + STOP_RULES_POINTER)
HANDOFF_CLOSURE_REASON = ("STOP GATE: your NEEDED: or question hands off while {reason}\nA hand-off is earned only when"
                          " nothing of your own is left: a repo counts as blocked only while one of its items waits on a"
                          " parked question, or while a peer's green change must land first (in another checkout, or in"
                          " the main checkout the work goes in) and your ask names that checkout.\n" + STOP_RULES_POINTER)


UNPARKED_QUESTION_REASON = ("STOP GATE: your closing question is not on your parking-lot page (stop-gate-rules.txt).\n"
                            "1. Park it: ArtifactData set, collection questions, doc_id qN-slug, with question, context,"
                            " recommendation, options (the recommended one first), askedBy, askedAt (from Get-Date -AsUTC"
                            " -Format o) and status open.\n2. Read the page after parking: ArtifactData query on questions"
                            " where status == answered.\n3. End on the question as you parked it, naming its qN (it must"
                            " share at least half the parked question's words)." + STOP_RULES_POINTER)
UNPARKED_NEEDED_REASON = ("STOP GATE: your NEEDED: line names no question on your parking-lot page:\n  \"{ask}\"\nA NEEDED:"
                          " is an ask of the operator, so it is parked first, as a closing question is: park it with the"
                          " parking-lot skill (ArtifactData set on questions, with its context and a recommendation),"
                          " read the page, then name its id (such as q6) on the NEEDED: line.\n" + STOP_RULES_POINTER)
UNREAD_PAGE_REASON = ("STOP GATE: your ask {problem}.\nThe operator answers on your parking-lot page as well as in chat."
                      " Before asking a parked question, read the page in the same turn (ArtifactData query on"
                      " questions where status == answered), and never ask one it shows answered."
                      + STOP_RULES_POINTER)
UNANSWERED_ASKS_REASON = ("STOP GATE: {problem}.\nThe operator asks the session questions on its parking-lot page so the"
                          " answer stays there; a reply in chat alone scrolls away.\n" + STOP_RULES_POINTER)
RE_QUOTE_SPAN = re.compile("[\"“]([^\"“”\\n]{8,}?)[\"”]")
REFUSAL_QUOTE_WORDS = 4
REFUSAL_BASIS_REASON = ("STOP GATE: your REFUSING: line {problem}.\nA refusal earns the stop only when it quotes, in double"
                        " quotes and verbatim, at least four words of an operator message: the request it declines or"
                        " the operator's words it rests on. A written rule alone declines nothing the operator asked,"
                        " and the stop-gate card's own rules decline nothing. Write it as REFUSING: <your reason>"
                        " \"<four or more consecutive words copied exactly from one operator message>\", or keep"
                        " working.\n" + STOP_RULES_POINTER)


def operator_corpus(transcript_path):
    """Every message the operator wrote in this session, normalised, read from the whole transcript: their turns,
    their mid-turn messages, the arguments of a slash command they ran (/goal <text>, goal_pin.command_args), and
    each answer they gave on this session's own parking-lot page that the session read back
    (invoke_artifact_gate.page_answers, the reader the cadence gate's operator_texts uses). Only the answer counts,
    never the question's own text or context, and no agent can write an answer, so nothing a session writes puts
    words in the operator's mouth."""
    corpus = []
    if not transcript_path or not os.path.isfile(transcript_path):
        return corpus
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"user"' not in line and "queued_command" not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            text = operator_text(record) or command_args(record)
            if text:
                corpus.append(normalised_words(text))
    try:
        cadence = cadence_module()
        artifact_gate = cadence.artifact_gate_module() if cadence is not None else None
    except Exception:
        artifact_gate = None
    if artifact_gate is not None:
        try:
            corpus.extend(normalised_words(answer) for _, answer in artifact_gate.page_answers(transcript_path)
                          if isinstance(answer, str) and answer.strip())
        except Exception:
            pass
    return corpus


TOOLS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_RULE_DOCS = ("AGENTS/GATES.md", "AGENTS/TOOLING.md", "AGENTS/WORKFLOWS.md", "AGENTS/CODE-STYLE.md",
                   "AGENTS/COMMENTS.md", "AGENTS/TRIAGE.md")


def rule_corpus(workspace):
    """The written rules a refusal may rest on, normalised: the global and workspace CLAUDE.md, the workspace
    AGENTS.md, every AGENTS doc, and the fleet docs that live in Tools (TOOLS_RULE_DOCS). The stop-gate rules card is
    not one: it governs how a turn ends, never what work the operator asked for, so quoting it declines nothing."""
    paths = [os.path.join(os.environ.get("USERPROFILE", ""), ".claude", "CLAUDE.md")]
    paths.extend(found for found in (session_home.workspace_doc("CLAUDE.md", workspace),
                                     session_home.workspace_doc("AGENTS.md", workspace)) if found)
    paths.extend(os.path.join(TOOLS_ROOT, *doc.split("/")) for doc in TOOLS_RULE_DOCS)
    docs = session_home.workspace_doc("AGENTS", workspace)
    for folder, _, files in os.walk(docs) if docs else ():
        paths.extend(os.path.join(folder, name) for name in files if name.lower().endswith(".md"))
    corpus = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                corpus.append(normalised_words(fh.read()))
        except OSError:
            continue
    return corpus


RE_OPERATOR_CLAIM = re.compile(
    r"\byou(?:'ve| have| had)? (?:paused|stopped|halted|asked|said|told|wanted|rejected|denied|approved|decided|"
    r"chose|instructed|directed|ruled)\b|\bper your\b|\bas you (?:said|asked|wanted)\b|\byour (?:pause|ruling)\b|"
    r"\byour (?:instruction|direction|decision)s?\s+(?:was|were|is|are|to|that)\b", FLAGS)
OPERATOR_CLAIM_REASON = ("STOP GATE: your ask {problem}.\nAn ask that says what the operator did or said quotes, in double"
                         " quotes and verbatim, at least four words of the operator message it rests on. Put four or"
                         " more consecutive words copied exactly from that message in double quotes on the line, or"
                         " rewrite the line without the claim (\"you asked\", \"you stopped\", \"as you said\").\n"
                         + STOP_RULES_POINTER)


def operator_claim_problem(text, signal, transcript_path):
    """Why a NEEDED: line or closing question resting on a claim about the operator does not quote them, else
    None. The claim ("since you stopped my tool calls", "as you asked") must carry a double-quoted span of at least
    four words found verbatim in the operator's own messages. Fails open on an error."""
    try:
        stripped = RE_FENCE.sub("", text or "")
        lines = RE_NEEDED_LINE.findall(stripped) if signal == "allow:blocker-token" else [
            closing_question(stripped) or ""]
        claims = [line for line in lines if RE_OPERATOR_CLAIM.search(line)]
        if not claims:
            return None
        quotes = [quote for line in claims for quote in RE_QUOTE_SPAN.findall(line)
                  if len(normalised_words(quote).split()) >= REFUSAL_QUOTE_WORDS]
        if not quotes:
            return "says what the operator did without quoting them"
        corpus = operator_corpus(transcript_path)
        if any(normalised_words(quote) in source for quote in quotes for source in corpus):
            return None
        return "quotes \"{0}\", which is in no operator message".format(quotes[0][:80])
    except Exception:
        return None


def refusal_basis_problem(text, transcript_path, workspace):
    """Why the message's REFUSING: line does not rest on anything checkable, else None. Its reason must quote, in
    double quotes, at least four words found verbatim (after normalisation) in one of the operator's own messages:
    the request it declines, or the operator's words it rests on. A written rule alone is no basis, since four words
    of it can be found for any refusal whether or not the operator asked for anything. Fails closed: a refusal the
    check cannot judge earns nothing."""
    try:
        lines = RE_REFUSING_LINE.findall(RE_FENCE.sub("", text or ""))
        quotes = [quote for line in lines for quote in RE_QUOTE_SPAN.findall(line)
                  if len(normalised_words(quote).split()) >= REFUSAL_QUOTE_WORDS]
        if not quotes:
            return "quotes nothing it rests on"
        sources = operator_corpus(transcript_path)
        if any(normalised_words(quote) in source for quote in quotes for source in sources):
            return None
        if any(normalised_words(quote) in source for quote in quotes for source in rule_corpus(workspace)):
            return "quotes \"{0}\", a written rule, and no operator message".format(quotes[0][:80])
        return "quotes \"{0}\", which is in no operator message".format(quotes[0][:80])
    except Exception as error:
        return "could not be checked ({0})".format(type(error).__name__)


DONE_OPEN_REASON = ("STOP GATE: your DONE: claims the whole ask while this session's work product is not closed"
                    " ({problem}).\nDeclare it in " + DECLARATION_PLACE + ", finish the edit each open item names,"
                    " and set that item's \"done\" to true; or report what you closed in plain prose without DONE:."
                    " A read-only ask that changed no file in any repo declares {{\"repos\": {{}}}} and nothing"
                    " more.\n" + STOP_RULES_POINTER)


def completion_work_product_problem(transcript_path, temp_root=None, workspace=None):
    """'<kind>: <detail>' when this session's work-product declaration is missing or holds an open item, else None.
    The same check QUEUED: and WAITING: owe, applied to DONE:. Fails open like every veto when it cannot judge."""
    if not transcript_path:
        return None
    try:
        cadence = cadence_module()
        if cadence is None:
            return None
        root = cadence.TEMP_CLAUDE_ROOT if temp_root is None else temp_root
        workspace = cadence.WORKSPACE if workspace is None else workspace
        problem = cadence.work_product_problem(transcript_path, workspace, root)
        if problem is not None:
            return "{0}: {1}".format(*problem)
        import work_product_closure
        reason = work_product_closure.done_problem(cadence, transcript_path, workspace)
        return None if reason is None else "closure: " + reason
    except Exception:
        return None


ACCEPTANCE_CHECK_TIMEOUT_SECONDS = 20
ACCEPTANCE_TOTAL_SECONDS = 45
ACCEPTANCE_MAX_CHECKS = 5
ACCEPTANCE_APPROVAL_MIN_WORDS = 8
DONE_UNACCEPTED_REASON = ("STOP GATE: your DONE: claims the whole ask while an acceptance check in this session's"
                          " work-product declaration fails ({problem}).\nThe declared edits are not the goal; the"
                          " acceptance list is. Run the check's command yourself, fix what keeps its output from"
                          " matching expect, and stop again; or report what you closed in plain prose without DONE:."
                          " Each check is {{\"check\", \"command\": [\"python\", \"<abs path>\"], \"expect\": \"<a Python"
                          " regex>\", \"approval\"}}, finishes within " + str(ACCEPTANCE_CHECK_TIMEOUT_SECONDS) + " s,"
                          " and its approval copies at least " + str(ACCEPTANCE_APPROVAL_MIN_WORDS) + " consecutive"
                          " words from one operator message asking for that state.\n" + STOP_RULES_POINTER)
UNPARKED_COMMIT_REASON = ("STOP GATE: a repo this session worked in or gated green is still uncommitted and its current"
                          " commit message is not in the parking lot, gated or not (parking-lot SKILL.md, \"Commit"
                          " messages\"):\n{problems}\n"
                          "For a missing or stale message: run git -C \"<checkout>\" -c core.quotePath=false status"
                          " --porcelain --no-renames --untracked-files=all, Write the message to a scratchpad file"
                          " ending in one line per path it lists (without the quotes git puts around a path holding a"
                          " space), run python"
                          " \"<workspace>\\Tools\\Claude\\skills\\parking-lot\\scripts\\parking_lot.py\" commit <Repo>"
                          " <message file>, and ArtifactData set the object it prints on commits/<Repo>, inline as"
                          " data. For a message still held after its commit: run the same script with committed <Repo>,"
                          " then ArtifactData delete commits/<Repo>.\n" + STOP_RULES_POINTER)


def command_written_files(commands, transcript_path, workspace):
    """{absolute path} of the dirty files this turn's repo-changing commands (git apply, git checkout, npm ci...;
    invoke_gate_cadence_gate.command_changed_checkout) left that are this session's: every dirty file in its own
    worktree, and elsewhere each one a declared item of its names (work_product_closure.command_changed_files). No
    edit tool records those bytes, so without this a turn that wrote files by command owes no commit message."""
    try:
        cadence = cadence_module()
        targets = {os.path.normcase(os.path.abspath(target)) for target in
                   (cadence.command_changed_checkout(command) for command in commands) if target}
        if not targets:
            return set()
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import work_product_closure
        checkouts = cadence.workspace_checkouts(workspace)
        hit = {checkout for checkout, _ in checkouts
               if any(target == checkout or target.startswith(checkout + os.sep) for target in targets)}
        view = types.SimpleNamespace(session_command_checkouts=lambda *_: hit,
                                     work_product_path=cadence.work_product_path,
                                     names_changed_path=cadence.names_changed_path)
        session_id = os.path.splitext(os.path.basename(transcript_path or ""))[0]
        found = work_product_closure.command_changed_files(view, transcript_path, workspace, {}, session_id, checkouts,
                                                           set())
    except Exception:
        return set()
    return {os.path.join(checkout, *relative.split("/")) for _, checkout, relative in found}


def worked_repos(facts, workspace, state_root, safe_id):
    """{repo: [checkouts]} this session has written files in: what the Stop gate's state remembers from earlier turns,
    plus this turn's writes located in a workspace repo's main checkout or one of its registered worktrees, which
    are remembered for the turns after this one."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import parking_lot_commits
    this_turn = {}
    for edited in facts.get("edited", ()):
        located = parking_lot_commits.checkout_of(edited, workspace)
        if located:
            this_turn.setdefault(located[0], [])
            if located[1] not in this_turn[located[0]]:
                this_turn[located[0]].append(located[1])
    return parking_lot_commits.remember_worked(safe_id, this_turn, state_root)


def unparked_commit_messages(workspace, session_id, worked=None, state_root=None):
    """parking_lot.problems() for this session: every dirty checkout the session worked in or gated green whose
    message is missing or stale, and every committed checkout whose message is still held. A failure to judge is
    itself a problem, never an allow."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import parking_lot_commits
        return parking_lot_commits.problems(workspace, session_id, worked=worked, state_root=state_root)
    except SystemExit as stop:
        return ["the parking-lot check could not run: {0}".format(stop.code)]
    except Exception as error:
        return ["the parking-lot check could not run: {0}".format(error)]


def merged_branch_problems(transcript_path, state_root, session_id, git_timeout, finishing=True):
    """merged_branches.problems() for this session, loaded only when a stop would otherwise be allowed."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import merged_branches
    return merged_branches.problems(transcript_path, state_root, session_id, git_timeout, finishing)


def acceptance_problem(transcript_path, workspace=None, runner=None):
    """'<check>: <why>' for the first acceptance check in this session's declaration that does not pass, else None.

    An entry is {"check": text, "command": [argv], "expect": regex, "approval": operator words}; it passes when
    its approval quotes at least ACCEPTANCE_APPROVAL_MIN_WORDS of the operator's own words, and the command exits 0
    within its share of ACCEPTANCE_TOTAL_SECONDS (at most ACCEPTANCE_CHECK_TIMEOUT_SECONDS) with stdout matching
    `expect`. The approval is what keeps a session from satisfying its own finish line with a check it wrote to
    pass. A timeout, a launch failure, a malformed or unapproved entry fails the check: a DONE: rests on the check
    having passed, so a check that could not run proves nothing. A declaration with no "acceptance" key owes none.
    `runner` replaces subprocess.run for the harness."""
    if not transcript_path:
        return None
    cadence = cadence_module()
    if cadence is None:
        return None
    path = cadence.work_product_path(transcript_path, cadence.WORKSPACE if workspace is None else workspace)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    checks = data.get("acceptance") if isinstance(data, dict) else None
    if checks is None:
        return None
    if not isinstance(checks, list) or len(checks) > ACCEPTANCE_MAX_CHECKS:
        return "acceptance: not a list of at most {0} checks".format(ACCEPTANCE_MAX_CHECKS)
    run = runner or subprocess.run
    said = None
    deadline = time.monotonic() + ACCEPTANCE_TOTAL_SECONDS
    for entry in checks:
        check = entry.get("check") if isinstance(entry, dict) else None
        command = entry.get("command") if isinstance(entry, dict) else None
        expect = entry.get("expect") if isinstance(entry, dict) else None
        approval = entry.get("approval") if isinstance(entry, dict) else None
        if not isinstance(check, str) or not check.strip() or not isinstance(expect, str) or not expect.strip() \
                or not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
            return "acceptance: an entry lacks check text, a command argv list or an expect pattern"
        if said is None:
            said = cadence.operator_texts(transcript_path)
        if not isinstance(approval, str) \
                or len(cadence.normalised_words(approval).split()) < ACCEPTANCE_APPROVAL_MIN_WORDS \
                or not cadence.operator_approved(approval, said):
            return "{0}: its approval does not quote at least {1} of the operator's own words".format(
                check, ACCEPTANCE_APPROVAL_MIN_WORDS)
        try:
            pattern = re.compile(expect)
        except re.error as error:
            return "{0}: expect is not a valid pattern ({1})".format(check, error)
        budget = min(ACCEPTANCE_CHECK_TIMEOUT_SECONDS, deadline - time.monotonic())
        if budget <= 0:
            return "{0}: not run, the {1} s acceptance budget is spent".format(check, ACCEPTANCE_TOTAL_SECONDS)
        try:
            result = run(command, capture_output=True, text=True, timeout=budget)
        except subprocess.TimeoutExpired:
            return "{0}: timed out after {1:.0f} s".format(check, budget)
        except OSError as error:
            return "{0}: could not run ({1})".format(check, error)
        if result.returncode != 0:
            return "{0}: exited {1}".format(check, result.returncode)
        if not pattern.search(result.stdout or ""):
            return "{0}: output did not match {1!r}".format(check, expect)
    return None


def wait_readings(transcript_path, temp_root=None, workspace=None):
    """What decided a refused wait, for facts.log: the open standing items with no "pending" (any one cancels the
    gate-wait excuse, stop-gate-rules.txt), the gate slot's holder and the live peer gate's transcript, each "none"
    when nothing was found and "error" when the read raised. Never decides anything."""
    readings = []
    try:
        cadence = cadence_module()
        root = cadence.TEMP_CLAUDE_ROOT if temp_root is None else temp_root
    except Exception:
        return "unparked=error;slot=error;peer=error"
    try:
        import work_product_closure
        data = work_product_closure.read_declaration(
            cadence.work_product_path(transcript_path, cadence.WORKSPACE if workspace is None else workspace)) or {}
        unparked = [entry for entry in work_product_closure.standing_entries(data)
                    if entry.get("done") is not True and not str(entry.get("pending") or "").strip()]
        readings.append("unparked=" + str(len(unparked)))
    except Exception:
        readings.append("unparked=error")
    try:
        holder = cadence.gate_slot_holder()
        readings.append("slot=" + (re.sub(r"[\t\r\n;]+", " ", str(holder.get("owner"))) if holder else "none"))
    except Exception:
        readings.append("slot=error")
    try:
        live = cadence.live_peer_gate(transcript_path, temp_root=root)
        readings.append("peer=" + (os.path.basename(str(live[1])) if live else "none"))
    except Exception:
        readings.append("peer=error")
    return ";".join(readings)


def unaccepted_wait_claim(text, messaged, transcript_path, temp_root=None, workspace=None, messaged_to=()):
    """(token, why) naming the first requirement a QUEUED: or WAITING: line in the message missed, or None when
    the message carries neither. Called only on a block, so it never decides a verdict: it turns the generic
    block into one that says which requirement the line missed, checked in the order STOP-HOOK.md lists them.
    QUEUED: is judged when both are present, since it is checked first among the allow paths."""
    stripped = RE_FENCE.sub("", text or "")
    queued = RE_QUEUED_LINE.findall(stripped)
    waiting = RE_WAITING_LINE.findall(stripped)
    if not queued and not waiting:
        return None
    token = "QUEUED:" if queued else "WAITING:"
    unreadable = (token, "the wait's state could not be read, so none of its requirements could be confirmed; keep "
                         "working, and tell the operator Harness/Hooks/invoke_gate_cadence_gate.py could not judge it")
    if not transcript_path:
        return unreadable
    try:
        cadence = cadence_module()
        if cadence is None:
            return unreadable
        root = cadence.TEMP_CLAUDE_ROOT if temp_root is None else temp_root
        problem = cadence.work_product_problem(transcript_path, cadence.WORKSPACE if workspace is None else workspace,
                                               root)
        if problem is not None:
            return token, ("this session's work-product declaration is not closed ({0}: {1}); finish every item and "
                           "set its \"done\" to true first").format(*problem)
        workspace = cadence.WORKSPACE if workspace is None else workspace

        def closed(waits_on_gate):
            reason = closure_problem(cadence, transcript_path, workspace, text, waits_on_gate)
            return None if reason is None else (token, reason)

        if queued:
            if cadence.gate_slot_holder() is not None:
                return closed(True)
            if not messaged:
                return token, ("no process holds this account's gate slot, and this turn sent no SendMessage to the "
                               "session whose work it waits on; SendMessage that session in this turn and name it "
                               "on the QUEUED: line")
            if cadence.live_peer_gate(transcript_path, temp_root=root) is not None:
                return closed(True)
            if queued_on_a_messaged_peer(text, messaged_to, transcript_path) is not None:
                return closed(False)
            return token, ("no peer's gate is running on this machine (an announced or planned gate is not one), no "
                           "process holds the gate slot, and the line names no session this turn messaged that is "
                           "running on this machine; nothing is ahead of you, so run your own gate now in the "
                           "foreground, or keep working")
        if own_work_running(cadence, transcript_path, root):
            return closed(own_gate_holds_slot(cadence, transcript_path) or own_gate_running(cadence, transcript_path, root))
        return token, ("nothing this session started is still running: no background task without its completion "
                       "notice, no gate of its own holding the slot, and no permission matrix it requested still "
                       "open; waiting on another session's work is QUEUED:. If you waited on your gate, read its "
                       "verdict with python Harness/Gates/gate_status.py <Repo> and act on it")
    except Exception:
        return unreadable


def withholds_a_commit_block(text):
    """True when a message that owes a commit message fobs the delivery off with a POINTER.

    Owed only on a turn that claims DONE: -- narrowed 2026-09-09 from every dirty turn, which the
    operator judged too aggressive; the caller checks the claim and, since the same day, whether a block
    names the files that are dirty now (judge_commit_blocks). This keeps the one shape that check cannot
    see: a real block for one repo beside "the commit messages given in my previous message" for another.
    The operator reads a terminal, so a pointer makes them go fetch the deliverable, which the standing
    rule calls out by name alongside "see COMMIT-MESSAGES.md" and "drafted in the scratchpad".
    """
    if not text:
        return True

    # Whether a block was delivered is a question about the RAW message: a fence is a fence.
    if not RE_ANY_FENCE.search(text):
        return True

    # Whether delivery was fobbed off with a pointer is a question about PROSE ONLY, and getting this
    # wrong is not hypothetical -- it fired on the very commit message introducing this veto, whose body
    # quotes the phrases it catches. Two strips, in order: fenced blocks, because a commit message that
    # documents the rule is not an instance of breaking it; then double-quoted spans, because naming a
    # phrase is not using it. A real violation reads "…with the commit messages given in my previous
    # message" unquoted and in prose, and survives both.
    prose = RE_FENCE.sub("", text)
    prose = RE_QUOTED_SPAN.sub(" ", prose)
    return bool(RE_COMMIT_POINTER.search(prose))


def work_is_complete(text, tool_calls):
    """Done AND proved AND a FLOOR: at least one tool call in the turn.

    The tool-call requirement is a floor, not corroboration. It was called corroboration for most of
    this gate's life and that overstates it in both directions. Unsound: an agent that reads one
    unrelated file and then writes a false VERIFIED: line passes. Incomplete: an agent that answers
    correctly from context alone fails. It checks nothing about the claim itself. All it rejects is a
    completion claim from a turn that ran nothing at all, which is the laziest forgery available and
    the only one a count can catch.

    It stays because it is nearly free and because that laziest forgery is observed rather than
    hypothetical: on 2026-08-31 a turn claimed VERIFIED: over a sentence conceding it had produced no
    fresh evidence, and the retry this floor forced re-read the files and quoted them. What the floor
    cannot see is a premature DONE: from a turn that ran thirty tools, which is the likelier failure
    and passes untouched.
    """
    if not text:
        return False
    if tool_calls < 1:
        return False
    prose = RE_FENCE.sub("", text)
    if not RE_DONE.search(prose):
        return False
    if not RE_VERIFIED.search(prose):
        return False
    return True


def write_decision(root, session_id, signal):
    """Instrumentation must never change a decision, so every failure here is swallowed."""
    try:
        line = "{0}\t{1}\t{2}\n".format(
            datetime.now(timezone.utc).isoformat(), session_id, signal
        )
        with open(os.path.join(root, "decisions.log"), "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def record_age_seconds(ts):
    """Seconds between a transcript record's own timestamp and now, or None if it cannot be read."""
    if not ts:
        return None
    try:
        text = str(ts)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        stamp = datetime.fromisoformat(text)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - stamp).total_seconds()
    except Exception:
        return None


def write_facts(root, session_id, signal, facts, path):
    """What the gate SAW, in its own file. Instrumentation must never change a decision.

    Kept out of decisions.log on purpose: readers of that file take the LAST tab-separated field as the
    signal, so appending a column there hands them a diagnostic where they expect the verdict and they
    keep working while reporting nonsense. decisions-log-last-field-is-still-the-signal pins the shape.

    The transcript is untrusted content. It is truncated, stripped of control characters and only
    ever written to a local file -- never parsed for instructions.
    """
    try:
        head = (facts.get("assistant_text") or "")[:80]
        head = re.sub(r"[\r\n\t]+", " ", head)
        newest = facts.get("newest") or {}
        # How far behind the transcript was. Diagnostic only now that the judged text comes from the
        # payload, but worth keeping: it is what shows the lag is still there and simply no longer
        # matters. Readings of 11.3s and 12.0s were recorded on live stops the same hour the payload
        # source landed, so a run with no misfires is not evidence the lag went away on its own.
        age = record_age_seconds(newest.get("ts"))
        line = "\t".join([
            datetime.now(timezone.utc).isoformat(),
            str(session_id),
            str(signal),
            "size=" + str(facts.get("size")),
            "endsnl=" + str(facts.get("endsnl")),
            "badjson=" + str(facts.get("badjson")),
            "tc=" + str(facts.get("tool_calls")),
            "newest=" + str(newest.get("type")) + "/" + str(newest.get("blocks")),
            "newest_ts=" + str(newest.get("ts")),
            "age=" + ("?" if age is None else "{0:.1f}s".format(age)),
            "file=" + os.path.basename(path or ""),
            # Which source the judged text came from. Without it a payload-fed gate and a
            # transcript-fed one leave identical rows, and there is no way to tell from the log
            # whether the platform actually sent the field on this host.
            "src=" + str(facts.get("text_source")),
            "goal=" + ("set" if facts.get("goal_in_force") else "-"),
            # What the commit-state veto owed and called stale, so a block:stale row can be read
            # without re-running git against a tree that has since changed.
            "repos=" + str(facts.get("repos")),
            "ahead=" + head,
            # What the wait checks read when a QUEUED: or WAITING: line was refused, so a refusal against a gate
            # that was running can be traced to the check that missed it.
            "wait=" + str(facts.get("wait") or "-"),
        ]) + "\n"
        with open(os.path.join(root, "facts.log"), "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


_CADENCE_MODULE = []
_PEER_MESSAGE_MODULE = []


def peer_reply_owed(transcript_path):
    """The reply a peer is owed, as invoke_peer_message_gate.py words it, else empty. Any failure is empty."""
    if not transcript_path:
        return ""
    try:
        if not _PEER_MESSAGE_MODULE:
            import importlib.util
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "invoke_peer_message_gate.py")
            spec = importlib.util.spec_from_file_location("peer_message_for_stop", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _PEER_MESSAGE_MODULE.append(module)
        module = _PEER_MESSAGE_MODULE[0]
        decision, reason = module.decide(transcript_path, "Stop")
        return reason if decision == module.DENY else ""
    except Exception:
        return ""


def cadence_module():
    """The cadence hook, loaded ONCE per process and cached.

    The first version executed the module on every call. In a live stop that is one import, but the
    harness calls decide() 216 times in a run, and `stop-hook-timeout-has-real-margin` measures this
    hook's own runtime against its 30 s budget - so an import per case is charged to the very thing the
    case is guarding. Caching costs one list lookup and removes the whole class.

    An unreadable module caches None rather than retrying: a hook that re-attempts a broken import on
    every call turns one failure into a per-call cost."""
    if not _CADENCE_MODULE:
        try:
            import importlib.util
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "invoke_gate_cadence_gate.py")
            spec = importlib.util.spec_from_file_location("gate_cadence_for_stop", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _CADENCE_MODULE.append(module)
        except Exception:
            _CADENCE_MODULE.append(None)
    return _CADENCE_MODULE[0]


def lines_newest_first(path, chunk_size=65536):
    """Yield (offset, line) for every complete line of a file, newest first, reading backwards in chunks.

    The same seam handling as read_facts: a line spanning chunks is carried into the next read, and bytes
    are only handed out once the line is whole, so a multi-byte character split by a boundary survives."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        position = fh.tell()
        pending = b""
        while position > 0:
            take = min(chunk_size, position)
            position -= take
            fh.seek(position)
            combined = fh.read(take) + pending
            end = len(combined)
            while True:
                newline = combined.rfind(b"\n", 0, end)
                if newline < 0:
                    break
                start = newline + 1
                if end > start:
                    yield position + start, combined[start:end]
                end = newline
            pending = combined[:end]
        if pending:
            yield 0, pending


TAIL_SCAN_BYTES = 8388608


def tail_start(path, cadence, max_scan_bytes=None):
    """Where the part of a transcript that can hold an unreported gate begins.

    A gate is owed only if it was launched after the session's last SendMessage, and its result, its
    task-notification and any TaskStop all follow its launch, so nothing before that message can change
    the verdict. The backward search is capped at max_scan_bytes: past the cap every gate still in view
    was launched after whatever message came earlier, so the verdict inside the window is unchanged and
    only a gate older than the window can be missed, which fails open rather than blocking."""
    if max_scan_bytes is None:
        max_scan_bytes = TAIL_SCAN_BYTES
    floor = max(0, os.path.getsize(path) - max_scan_bytes)
    oldest_in_window = 0
    for offset, line in lines_newest_first(path):
        if offset < floor:
            return oldest_in_window
        oldest_in_window = offset
        if b"SendMessage" not in line:
            continue
        try:
            record = json.loads(line.decode("utf-8", "replace"))
        except Exception:
            continue
        if isinstance(record, dict) and any(
                block.get("type") == "tool_use" and block.get("name") == "SendMessage"
                for block in cadence.message_blocks(record)):
            return offset
    return 0


SESSION_REGISTRY_DIRECTORY = "sessions"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ERROR_ACCESS_DENIED = 5


def registered_sessions(transcript_path):
    """Every entry in the Claude Code session registry beside the projects directory holding this
    transcript: <config>/sessions/<pid>.json, one per session the host has started."""
    projects_root = os.path.dirname(os.path.dirname(os.path.abspath(transcript_path)))
    registry = os.path.join(os.path.dirname(projects_root), SESSION_REGISTRY_DIRECTORY)
    for path in glob.glob(os.path.join(registry, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                entry = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict):
            yield entry


def process_running(pid, started):
    """Whether pid is a running process, and the same one the registry recorded: its creation time, as a
    FILETIME, must equal the entry's procStart, so a crashed session's entry never resurrects through a
    reused pid."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if started is not None and kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            if str((created.dwHighDateTime << 32) | created.dwLowDateTime) != str(started):
                return False
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def reachable_peers(transcript_path):
    """The other sessions running on THIS machine, which are the only ones a SendMessage can reach.

    The registry and the projects directory both sync across machines, so neither a fresh transcript nor a
    registry entry proves a session is here: the entry's pidDomain must equal this session's own and its
    process must be running. Without this session's own entry there is no domain to compare, so there
    are no peers."""
    own_id = os.path.splitext(os.path.basename(transcript_path))[0]
    entries = list(registered_sessions(transcript_path))
    domain = next((entry.get("pidDomain") for entry in entries if entry.get("sessionId") == own_id), None)
    if not domain:
        return []
    return [entry for entry in entries
            if entry.get("sessionId") != own_id and entry.get("pidDomain") == domain
            and isinstance(entry.get("pid"), int) and process_running(entry["pid"], entry.get("procStart"))]


def unreported_finished_gate(transcript_path):
    """The gate this session finished and never reported to a peer, or None.

    The cadence hook owns what a finished gate is, so this borrows its functions rather than
    reimplementing them and drifting. The import is LAZY because that module imports THIS one; at module
    level the pair would be circular.

    Peers are checked first because a SOLO session owes nothing and should read no transcript at all.
    Each transcript is then read only from tail_start(), copied to a scratch file so the cadence
    functions run unchanged on exactly the part that can matter.

    Fails open on any error, like every other read in this hook: a backstop that crashed a stop would
    cost more than the blocked peers it exists to protect."""
    try:
        cadence = cadence_module()
        if cadence is None:
            return None
        peers = reachable_peers(transcript_path)
        if not peers:
            return None
        tails, scratch = [], []
        try:
            for path in cadence.session_files(transcript_path):
                start = tail_start(path, cadence)
                if start <= 0:
                    tails.append(path)
                    continue
                import claude_temp
                with open(path, "rb") as source, tempfile.NamedTemporaryFile(
                        "wb", suffix=".jsonl", delete=False, dir=claude_temp.scratch_dir()) as tail:
                    source.seek(start)
                    shutil.copyfileobj(source, tail)
                scratch.append(tail.name)
                tails.append(tail.name)
            gates, _ = cadence.collect(tails, cadence.classify)
            return cadence.owes_a_finished_line(gates, cadence.message_stamps(tails), peers)
        finally:
            for name in scratch:
                try:
                    os.remove(name)
                except OSError:
                    pass
    except Exception:
        return None


UNREPORTED_GATE_BLOCK = (
    "STOP GATE: your gate {command} has FINISHED and no peer on this machine was told.\n"
    "Run ListAgents, then SendMessage each peer on this machine \"GATE FINISHED: <Repo> <GATE: PASSED or GATE:"
    " FAILED>\" with the step table copied from the gate's output; that line is owed the moment the gate exits.\n"
    "Rules: Harness/Hooks/stop-gate-rules.txt (in your context since session start)."
)


RE_GATE_CLAIM = re.compile(r"ready to (?:check ?in|commit|push)|gated green|green on (?:the )?current", FLAGS)
RE_CLAIM_NEGATION = re.compile(r"\b(?:not|no|nothing|none|never|neither|nor|yet to)\b|n't\b", FLAGS)
RE_CLAUSE_BREAK = re.compile(r"[.;:!?]")


def asserts_gate_claim(line):
    """True when some gate-claim phrase in the line is asserted rather than denied: "Nothing is gated green"
    and "App is not ready to check in" state the record's own verdict, so only a phrase with no negation
    earlier in its clause is a claim."""
    for match in RE_GATE_CLAIM.finditer(line):
        clause = RE_CLAUSE_BREAK.split(line[:match.start()])[-1]
        if not RE_CLAIM_NEGATION.search(clause):
            return True
    return False

GATE_CLAIM_REASON = ("STOP GATE: you claim {repos} gated green, ready or done, and the gate record says {states}.\n"
                     "Only GREEN-CURRENT or CURRENT-IMPLICIT from python Harness/Gates/gate_status.py <Repo> supports"
                     " that claim. If this session changed the repo, gate it: pwsh -File \"<workspace>\\<Repo>\\gate.ps1\""
                     " -Goal \"<the pinned goal, verbatim>\". Otherwise rewrite the line to state the state that"
                     " command prints, without claiming the repo green, ready or done.\nRules:"
                     " Harness/Hooks/stop-gate-rules.txt (in your context since session start).")


def gate_status_module():
    """Harness/Gates/gate_status.py owns what a repo's most recent gate record says and whether it still holds."""
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Gates")
    if here not in sys.path:
        sys.path.insert(0, here)
    import gate_status
    return gate_status


def unproven_gate_claims(text, workspace, goal=None):
    """Each gated repo a claim line names whose newest gate record, from its main checkout or a worktree of it, is
    neither GREEN-CURRENT nor CURRENT-IMPLICIT, mapped to its state. A claim line is a DONE:/VERIFIED: line or one saying a repo is gated
    green or ready to check in. The pinned goal's words on a line name nothing: the goal veto makes a DONE: restate
    them, so a goal naming a repo's file would otherwise convict every DONE: written for it."""
    lines = [without_goal_words(line, goal) for line in (text or "").splitlines()
             if RE_DONE.match(line) or RE_VERIFIED.match(line) or asserts_gate_claim(line)]
    if not lines:
        return {}
    statuses = gate_status_module()
    gated = [name for name in os.listdir(workspace) if os.path.isfile(os.path.join(workspace, name, "gate.ps1"))]
    named = sorted({repo for repo in gated for line in lines
                    if re.search(r"(?<![\w.-]){0}(?![\w-])".format(re.escape(repo)), line)})
    unproven = {}
    for repo in named:
        entry = statuses.latest_status(repo, workspace=workspace)
        if not statuses.is_green(entry):
            unproven[repo] = entry.get("state")
    return unproven


def without_goal_words(line, goal):
    """The line with the goal's words removed wherever they appear in order, matched as normalised_words matches
    them: case ignored and any run of punctuation or space between two words."""
    words = normalised_words(goal or "").split()
    if not words:
        return line
    return re.sub("[^a-z0-9]+".join(re.escape(word) for word in words), " ", line, flags=re.IGNORECASE)


def safe_session_id(session_id, transcript_path):
    """The session's name for its state files: its id, else its transcript's file name, made safe for a path."""
    if not session_id:
        session_id = os.path.splitext(os.path.basename(transcript_path))[0] if transcript_path else "unknown"
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id))


PAST_BUDGET_TAG = "past-budget:"
FILLER_STREAK_LIMIT = 4
FILLER_STREAK_TAG = "eject:filler-streak:"
FILLER_STREAK_REASON = ("Stop gate: this turn was ended by the gate, not earned. {count} stops in a row were refused"
                        " with no tool call between them, which outlasts the gate with filler instead of doing the"
                        " work or earning a stop; the last refusal was {tag}. It is logged as eject:filler-streak in"
                        " the Stop gate's decisions.log, the session audit catalogs it as a message owing a verdict,"
                        " and a second one with no operator message between quarantines the session. {next}")
FILLER_NEXT_ITEM = ('Your first open item, quoted from your declaration: "{item}". Work it with a tool call, or park'
                    ' the question that blocks it on your page and mark the item pending on that question.')
FILLER_NO_ITEM = ("Every declared item is done, so the honest end is a stop token the rules card allows, after this"
                  " session's audit; a wait on the operator is a NEEDED: naming a parked question, never prose.")
FAILED_REASON = ("STOP GATE: the stop gate itself failed ({error}), so this stop is unjudged and blocked. Tell the"
                 " operator in plain prose that Harness/Hooks/invoke_stop_gate.py raised this error, quoting it, and"
                 " what you were finishing; write no stop token, which cannot be judged either. Claude Code ends"
                 " the turn after its block limit.")


def decide(payload, transcript_path, state_root, max_blocks, workspace=None,
           git_timeout=None, gate_workspace=None, temp_root=None, quarantine_root=None,
           require_parked=True, acceptance_runner=None, prompt_rule_at=None, coordination_config=None):
    """The whole decision, free of argv and stdin, returning (signal, block_reason).

    The reason is PLAIN TEXT, empty when the stop is allowed. main() owns the wire format; a leaf that
    built its own envelope is how the peer-notification veto shipped unparseable and unenforced.

    Split out so the test suite can call it IN-PROCESS. The PowerShell harness spawned a fresh gate
    process per case, which is what made a full run take ~11 minutes and made every fixture tweak a
    coffee break. The alternative -- a harness that re-implements the decision order to test the
    detectors individually -- is the exact mistake checklist item 11 exists to forbid: it would be
    testing a reimplementation, not the gate. This keeps one copy of the order and makes it callable.
    """
    if workspace is None:
        workspace = WORKSPACE_ROOT
    facts = read_facts(transcript_path)

    # THE PLATFORM HANDS US THE TEXT. Every detector below judges the turn's final assistant message,
    # and the Stop payload carries exactly that as last_assistant_message. Scraping it out of the
    # transcript was always the wrong source: the file is written asynchronously and is documented as
    # possibly lagging the live conversation at the moment a hook fires. Session 036e4299 spent nine
    # minutes with its newest message record frozen at 05:01:34 while file-history-delta records kept
    # landing every ~590 bytes, so the file was demonstrably being written -- just not with the thing
    # the gate needed. No wait, re-read or retry inside the hook could have recovered it, because a
    # block extends the same turn whose records are unflushed.
    #
    # The transcript is still read: tool_calls and the operator's last turn have no payload equivalent.
    # Those lagging only ever makes the gate STRICTER -- an unseen tool call fails work-complete, an
    # unseen grant fails operator-grant -- so a stale file can cost a legitimate stop, never buy one.
    supplied = payload.get("last_assistant_message")
    supplied = supplied.strip() if isinstance(supplied, str) else ""
    facts["text_source"] = "payload" if supplied else "transcript"
    if supplied:
        facts["assistant_text"] = supplied

    session_id = payload.get("session_id")
    if not session_id:
        session_id = os.path.splitext(os.path.basename(transcript_path))[0] if transcript_path else "unknown"
    safe_id = safe_session_id(session_id, transcript_path)

    os.makedirs(state_root, exist_ok=True)

    halted = quarantine_halt(session_id, quarantine_root)
    if halted:
        write_decision(state_root, safe_id, halted[0])
        return halted

    # ALTERNATIVE ALLOW PATHS, NOT A SEQUENCE. Every check is pure and every one exits the same way,
    # so the verdict is a plain disjunction and reordering changes nothing. Anything that must hold for
    # EVERY stop has to be a veto applied after the disjunction, with carve-outs for the operator's
    # grant and for REFUSING:.
    if not facts["assistant_text"]:
        signal = "allow:no-assistant-text"
    elif operator_granted_stop(facts["last_user_text"]):
        signal = "allow:operator-grant"
    else:
        impossible = continuing_is_impossible(facts["assistant_text"])
        if impossible:
            signal = "allow:" + impossible
        elif work_is_complete(facts["assistant_text"], facts["tool_calls"]):
            signal = "allow:work-complete"
        elif queued_for_a_gate(facts["assistant_text"], facts["messaged"], transcript_path, temp_root,
                               gate_workspace, facts["messaged_to"]):
            signal = "allow:queued-for-gate"
        elif waiting_on_own_gate(facts["assistant_text"], transcript_path, temp_root, gate_workspace):
            signal = "allow:waiting-on-own-gate"
        elif master_idle(facts["assistant_text"], transcript_path, temp_root, gate_workspace):
            signal = MASTER_IDLE_SIGNAL
        elif master_wake_sent(facts["messaged"]):
            signal = MASTER_WAKE_SIGNAL
        elif poke_sent(facts["messaged"]):
            signal = POKE_SIGNAL
        else:
            signal = None

    # AN ASK EARNS ONE STOP PER OPERATOR MESSAGE, whether it is a question or a NEEDED: line. Asked again before
    # the operator writes, it is blocked.
    # A WAIT THE DECLARATION RECORDS is the exception: every open item, and every done item awaiting approval, pending
    # on a parked question, and a NEEDED: naming those questions, may stand again until the operator answers.
    page_url = recorded_parking_url(gate_workspace or workspace, session_id, transcript_path)
    goal = goal_in_force(transcript_path, state_root, safe_id)
    waiting = waits_on_parked_answers(facts["assistant_text"], signal, transcript_path, gate_workspace, page_url,
                                      goal)
    operator_marker = last_operator_marker(transcript_path)
    pending = load_pending_asks(state_root, safe_id, operator_marker)

    # A PAGE ANSWER IS AN ANSWER. An ask the page read this turn shows answered is no longer pending, and one naming a
    # parked question no read this turn covers is not known to be unanswered: a wake stop on it waits for that read.
    page_answered, page_unread = parked_ask_states(pending, transcript_path, page_url)
    pending = [ask for ask in pending if ask not in page_answered]

    # A REFUSED STOP STILL SPOKE. Its text reached the operator before the gate ran, so an ask it carried is pending
    # like any delivered ask: a later stop in the same turn may not put it again, and ends on a line that asks nothing.
    refused_pending = bool(pending) and pending_asks_from_refused_stop(state_root, safe_id, operator_marker)
    refused_turn = refused_pending and facts["last_user_text"] != ""

    # NO GOAL, NO TOOL. With no goal pinned the goal gate refuses every tool, so an ask for one is the only move left:
    # it may stand again, and no veto whose remedy is a tool call (a report, a cleanup, a parked message, an open
    # item, a peer's reply) can refuse it.
    goal_ask = asks_for_a_goal(facts["assistant_text"], signal, goal)

    # A TURN A PEER OR A TASK STARTED, with an ask already put to the operator since they last wrote and no work of
    # the session's own left, ends with no ask: re-asking is a repeat, and anything else would only fill the turn.
    # One stop reads which parked questions are settled once: each read scans the whole transcript.
    settled_once = []

    def settled():
        if not settled_once:
            try:
                settled_once.append(settled_question_ids(cadence_module(), transcript_path))
            except Exception:
                settled_once.append(set())
        return settled_once[0]

    woken = (signal is None and (facts["last_user_text"] == "" or refused_pending) and bool(pending)
             and not own_work_left(transcript_path, gate_workspace)
             and handoff_problem("", transcript_path, gate_workspace, settled) is None)
    woken_unread = woken and next(iter(page_unread), None)
    woken_loud = woken and not page_unread and loud_wake_problem(facts["assistant_text"], pending)
    woken_quietly = woken and not page_unread and not woken_loud

    # A CLOSING QUESTION BESIDE A NEEDED: IS AN ASK TOO. Recorded only as its NEEDED: lines, it would never be pending,
    # so a later wake could stand on it without reading the page and a later turn could ask it again.
    shown = earning_asks(facts["assistant_text"], signal)
    beside = closing_question(facts["assistant_text"]) if signal == "allow:blocker-token" else None
    if beside and beside not in shown:
        shown.append(beside)
    asks = [] if waiting and not refused_pending else list(shown)
    repeated = None
    asked = None
    if asks and goal_ask:
        asked = pending + [ask for ask in asks if not any(same_question(ask, earlier) for earlier in pending)]
    elif asks:
        parked_ids = ({doc_id.split("-", 1)[0].lower() for _, doc_id in parked_questions(transcript_path, page_url)}
                      if pending else set())
        repeated = next((earlier for ask in asks for earlier in pending
                         if same_question(ask, earlier) or same_parked_ask(ask, earlier, parked_ids)), None)
        if repeated:
            signal = None
        else:
            asked = pending + asks
    if woken_quietly:
        signal = QUIET_WAKE_SIGNAL

    # OWN WORK BEFORE A HAND-OFF. A NEEDED: or question stop while this session's declared work product has an open
    # item the stop does not name is refused: the session still has its own work to do.
    undone = []
    if signal in ("allow:blocker-token", "allow:question-mark") and not waiting and not goal_ask:
        undone = unworked_open_items(facts["assistant_text"], transcript_path, gate_workspace)
    if undone:
        signal = None

    # LANDING IS THE LAST STEP, AND A DONE FLAG IS A CLAIM. An ask to commit or push while an open item waits on no
    # parked question is refused, and a NEEDED: or question hand-off is held to work_product_closure, a repo counting
    # as blocked only by a measured block (gate_blocked).
    landing_left, handoff_left = 0, None
    if signal in ("allow:blocker-token", "allow:question-mark") and not goal_ask:
        if landing_asked(facts["assistant_text"]):
            landing_left = unblocked_open_items(transcript_path, gate_workspace or workspace, facts["assistant_text"],
                                                settled)
        if not landing_left:
            handoff_left = handoff_problem(facts["assistant_text"], transcript_path, gate_workspace, settled)
    if landing_left or handoff_left:
        signal = None

    # A REFUSAL RESTS ON SOMETHING CHECKABLE: the operator's words or a written rule, quoted.
    refusal_problem = None
    if signal and signal.startswith("allow:refusal"):
        refusal_problem = refusal_basis_problem(facts["assistant_text"], transcript_path, workspace)
    if refusal_problem:
        signal = None

    # AN ASK RESTING ON WHAT THE OPERATOR DID QUOTES THEM, so an invented premise earns nothing.
    claim_problem = None
    if signal in ("allow:blocker-token", "allow:question-mark"):
        claim_problem = operator_claim_problem(facts["assistant_text"], signal, transcript_path)
    if claim_problem:
        signal = None

    # A COMPLETION CLAIM OWES A CLOSED WORK PRODUCT, as QUEUED: and WAITING: always have: a DONE: with no
    # declaration, or with an open item, is a claim on a slice, so it is refused.
    done_open = None
    unaccepted = None
    if signal == "allow:work-complete":
        done_open = completion_work_product_problem(transcript_path, temp_root, gate_workspace)
        if not done_open:
            unaccepted = acceptance_problem(transcript_path, gate_workspace, acceptance_runner)
    if done_open or unaccepted:
        signal = None

    # AN ASK TO COMMIT CARRIES THE COMMIT MESSAGE, so the operator never scrolls back for it.
    claims_it_cannot_continue = (signal in ("allow:blocker-token", "allow:question-mark", "allow:queued-for-gate",
                                            "allow:waiting-on-own-gate", MASTER_IDLE_SIGNAL)
                                 or (signal or "").startswith("allow:refusal"))
    commit_ask = claims_it_cannot_continue and commit_ask_without_message(facts["assistant_text"])
    if commit_ask:
        signal = None

    # THE FILES THIS SESSION WROTE are remembered first, per repo and across turns, because the parking-lot check
    # below and the commit-block veto further down both read them: a session's message covers the files it wrote,
    # never a peer's in the same checkout.
    session_paths = {}
    written = {}
    facts["repos"] = "-"
    facts["edited"] |= command_written_files(facts.get("commands", ()), transcript_path, gate_workspace or workspace)
    if COMMIT_BLOCK_VETO_ENABLED:
        touched = {}
        digests = {}
        for edited in facts.get("edited", ()):
            located = repo_relative(edited, workspace)
            if located:
                touched.setdefault(located[0], set()).add(located[1])
                try:
                    with open(edited, "rb") as fh:
                        digest = content_digest(fh.read())
                except Exception:
                    digest = None
                digests.setdefault(located[0], {})[located[1]] = digest
        session_paths = remember_edited_paths(state_root, safe_id, touched)
        written = remember_written_digests(state_root, safe_id, digests)
        facts["repos"] = ",".join(sorted(session_paths)) or "-"

    # A DIRTY CHECKOUT THIS SESSION WORKED IN, OR GATED GREEN, HAS ITS COMMIT MESSAGE PARKED before the turn ends,
    # gated or not: the operator commits a tree without a gate to reach CURRENT-IMPLICIT, so a message owed only on
    # a green gate left the console as the only copy. Only the operator's grant and a refusal skip it.
    unparked_commits = []
    if signal and signal not in ("allow:operator-grant", "allow:no-assistant-text") \
            and not signal.startswith("allow:refusal") and not goal_ask:
        try:
            worked = worked_repos(facts, gate_workspace or workspace, state_root, safe_id)
        except Exception:
            worked = {}
        unparked_commits = unparked_commit_messages(gate_workspace or workspace, str(session_id), worked, state_root)
    if unparked_commits:
        signal = None

    merged_left = []
    if signal and signal not in ("allow:operator-grant", "allow:no-assistant-text") \
            and not signal.startswith("allow:refusal") and not goal_ask:
        merged_left = merged_branch_problems(transcript_path, state_root, safe_id, git_timeout,
                                             finishing=bool(RE_DONE_LINE.search(facts["assistant_text"])))
    if merged_left:
        signal = None

    # A QUESTION FOR THE GOAL is exempt from parking: the goal gate tells a session with none to ask, and lets no
    # work tool run until the operator answers, so the parking lot cannot be written first.
    goal_question = asks_for_the_goal(facts["assistant_text"], signal, goal, transcript_path)

    # A CLOSING QUESTION IS PARKED FIRST, so its context is on the session's page, one click away.
    unparked = (require_parked and not goal_question and signal == "allow:question-mark"
                and not is_parked(closing_question(facts["assistant_text"]), transcript_path, page_url))

    # A NEEDED: IS PARKED FIRST, as a closing question is: it is an ask of the operator, and an unparked one is a
    # blocker nobody can check. Exempt only where no page write can run: no goal pinned, or a hook refusing every tool.
    unparked_ask = None
    if require_parked and not goal_ask and not waiting and not told_to_end_on_needed(transcript_path):
        unparked_ask = unparked_needed(facts["assistant_text"], signal, transcript_path, page_url)

    # AN ASK NAMING A PARKED QUESTION READS THE PAGE FIRST, since the operator may have answered it there.
    unread_page = ""
    if require_parked and not goal_question and signal in ("allow:blocker-token", "allow:question-mark") + WAIT_SIGNALS:
        unread_page = unread_parked_asks(facts["assistant_text"], signal, transcript_path, page_url, goal)

    # A RE-ASK IN PROSE IS AN ASK TOO: a line anywhere in the message asking about a parked question reads the page
    # first, as a NEEDED: line does, and never asks about one the page shows answered.
    prose_reask = ""
    if require_parked and not goal_question and signal:
        prose_reask = prose_reasks(facts["assistant_text"], transcript_path, page_url)

    # THE OPERATOR'S OWN QUESTIONS ON THE PAGE ARE READ AND ANSWERED BEFORE ANY STOP, so an answer is on the page
    # instead of scrolled away in chat. Only the operator's grant, a refusal, a stop with no text and a messenger
    # process are exempt, and a session with no page yet has nothing to read.
    unanswered_asks = ""
    if (require_parked and page_url and signal and signal not in ("allow:operator-grant", "allow:no-assistant-text")
            and signal not in MESSENGER_SIGNALS and not signal.startswith("allow:refusal")):
        unanswered_asks = unanswered_operator_asks(transcript_path, page_url)

    # A SESSION THAT STARTED UNDER THE SESSION-START RULES HAS ITS PAGE BEFORE ANY STOP, and a DONE: never walks past a
    # decision its SessionStart context handed the operator. Only the operator's grant, a refusal, the ask for a goal
    # and a hook refusing every tool are exempt: none of them can be preceded by a page write.
    no_page = False
    start_decision = None
    if (require_parked and signal and signal not in ("allow:operator-grant", "allow:no-assistant-text")
            and signal not in MESSENGER_SIGNALS
            and not signal.startswith("allow:refusal") and not goal_ask and not goal_question
            and not told_to_end_on_needed(transcript_path)):
        start, start_text = session_start_context(transcript_path)
        if start is not None:
            no_page = page_url is None
            if not no_page and RE_DONE_LINE.search(facts["assistant_text"] or ""):
                start_decision = unparked_start_decision(start_text, transcript_path, page_url)

    # THE MASTER'S QUESTIONS REACH THE OPERATOR BY PUSH: nobody watches the master's conversation, so a stop that
    # leaves a parked question open is refused until a PushNotification has named it, once per question.
    unpushed = []
    if (signal and signal not in ("allow:operator-grant", "allow:no-assistant-text")
            and signal not in MESSENGER_SIGNALS and runs_as(MASTER_SESSION_VARIABLE)):
        unpushed = unpushed_parked_ids(transcript_path, page_url)
        unpushed = ["read"] if unpushed is None else unpushed

    # A SESSION REGISTERS BEFORE IT TAKES A GOAL, AND IS GRANTED ARTIFACT WRITES AT SESSION START. The prompt gate
    # holds back /goal until the session's row is on the coordination board, in any turn. Claude Code asks the
    # operator to allow artifact writes in a turn they did not type, or a slash command's, so a registered session's
    # stops are refused until an artifact write succeeded in such a turn: the continuation this refusal starts is one,
    # and the operator is asked now rather than when an unattended turn first writes. Every stop but the operator's
    # grant and a refusal waits on both.
    unregistered = unconsented = False
    if (signal not in ("allow:operator-grant", "allow:no-assistant-text") and signal not in MESSENGER_SIGNALS
            and not (signal or "").startswith("allow:refusal")
            and prompt_rule_applies(transcript_path, prompt_rule_at)):
        unregistered = not coordination.registered_latched(transcript_path, session_id, state_root, coordination_config)
        unconsented = not unregistered and not coordination.consented_latched(transcript_path, session_id, state_root)

    unaudited = ""
    if (signal in AUDITED_SIGNALS and not goal_ask and not goal_question and not told_to_end_on_needed(transcript_path)
            and prompt_rule_applies(transcript_path, prompt_rule_at)):
        try:
            unaudited = audit_required(transcript_path, session_id, gate_workspace or workspace, state_root, temp_root) or ""
        except Exception as error:
            unaudited = "its audit could not be read ({0}: {1}).".format(type(error).__name__, str(error)[:200])

    # THE PEER-NOTIFICATION VETO, and a veto rather than an allow path for the reason above: it must
    # hold for every stop, including one the operator granted implicitly by asking a question. A turn
    # that messaged a peer satisfies it outright, so reporting the table and stopping is one turn.
    #
    # It sets a flag and falls through to the single block exit rather than returning here. Returning
    # early skipped the decision log, the block budget and the JSON envelope in one step, which left
    # this veto emitting bare text the host could only read as advisory.
    unreported = None
    if signal != "allow:operator-grant" and not facts["messaged"] and not goal_ask:
        owed = unreported_finished_gate(transcript_path)
        if owed:
            unreported = owed.get("command", "?")

    # THE FIRST VETO, placed here for the reason the note above gives: a rule that must hold for EVERY
    # stop cannot be another allow path, because the disjunction would simply route around it.
    #
    # Two carve-outs, both about not trapping the operator. Their explicit grant always wins -- fighting
    # "you can stop" with a formatting complaint is worse than the trailer. And a REFUSING: turn must be
    # able to end, since the whole point of a refusal is that the work is not going to continue.
    # `no-assistant-text` is moot rather than carved out: no text cannot carry a trailer.
    carved_out = signal in ("allow:no-assistant-text", "allow:operator-grant") or (
        signal is not None and signal.startswith("allow:refusal")
    )
    workspace_missing = not carved_out and not os.path.isdir(workspace)
    trailer = (TRAILER_VETO_ENABLED and not carved_out
               and carries_attribution_trailer(facts["assistant_text"]))
    session_names = [] if carved_out else named_sessions(facts["assistant_text"])

    # A PEER WAITING ON A REPLY HOLDS THE TURN OPEN as it holds every tool but the reply, so ending the turn is
    # not a way around answering it.
    peer_owed = "" if carved_out or goal_ask or signal in MESSENGER_SIGNALS else peer_reply_owed(transcript_path)

    # THE SECOND VETO: an owed commit message block, judged against git's CURRENT answer. Scoped to the
    # files this SESSION wrote, accumulated across turns, so a conversational session costs nothing and
    # nobody is nagged about the operator's own dirty files; since 2026-09-09 scoped to turns that CLAIM
    # DONE:, so a diagnosis or a question mid-task owes nothing; and since the same day bound to
    # content: a block counts only if it names a file that is dirty now, and a block naming a file the
    # session wrote that is clean now is STALE and blocked as such. Presence of a fence used to be the
    # whole test, and agents learned to re-paste one turn after turn -- 50 subjects re-posted verbatim
    # within a session, 14 after the operator had already committed them. The session's written paths were
    # remembered above, before the parking-lot check.

    # THE THIRD VETO: a completion claim that does not restate the operator's pinned goal. Read from
    # the operator's own record, so the pin is as un-forgeable as the grant; enforced on every stop
    # that carries a DONE: line, whichever signal earned it, because the claim is what the operator
    # reads. Silent until the operator pins a goal -- a session with no GOAL: line is judged as before. The goal
    # in force was read above, with the parking exemption for a question asking for it.
    facts["goal_in_force"] = goal
    misstated = (not carved_out and not trailer
                 and (misstates_the_goal(facts["assistant_text"], goal)
                      or (signal in ("allow:blocker-token", "allow:question-mark")
                          and ask_misstates_the_goal(facts["assistant_text"], goal))))

    # THE FIFTH VETO: a claim that a repo is gated green, ready to check in or done, judged against that
    # repo's most recent gate record rather than the session's recollection of a run.
    unproven = {}
    if not carved_out and not trailer and not misstated:
        try:
            unproven = unproven_gate_claims(facts["assistant_text"], gate_workspace or workspace, goal)
        except Exception:
            unproven = {}

    missing_block = False
    owed, stale, missing, ambiguous = {}, {}, [], {}
    if (not carved_out and not trailer and not misstated and session_paths
            and completion_claims(facts["assistant_text"])):
        dirty = dirty_paths(session_paths, workspace, git_timeout)
        # Only a dirty file with a recorded write is worth a cat-file: HEAD holding that write means the
        # session's edit landed and whatever is dirty on the file now belongs to another session.
        landed = {}
        for repo, present in dirty.items():
            candidates = {rel: digest for rel, digest in written.get(repo, {}).items()
                          if any(same_file(p, rel) for p in present)}
            if candidates:
                repo_path = os.path.join(workspace, repo)
                landed[repo] = (landed_in_head(repo_path, candidates, git_timeout)
                                | superseded_after_landing(repo_path, candidates, git_timeout))
        owed, stale, missing, ambiguous = judge_commit_blocks(facts["assistant_text"], session_paths, dirty,
                                                              landed)
        if stale:
            missing_block = True
        elif missing or (owed and withholds_a_commit_block(facts["assistant_text"])):
            missing_block = True
        described = []
        if owed:
            described.append("owed:" + ";".join("{0}={1}".format(r, ",".join(v)) for r, v in sorted(owed.items())))
        if stale:
            described.append("stale:" + ";".join("{0}={1}".format(r, ",".join(v)) for r, v in sorted(stale.items())))
        if any(landed.values()):
            described.append("landed:" + ";".join("{0}={1}".format(r, ",".join(sorted(v)))
                                                   for r, v in sorted(landed.items()) if v))
        # A repo git did not answer for is never judged, so the veto passes it silently; the row says so,
        # or an unchecked turn is indistinguishable from one whose block was delivered.
        unjudged = sorted(repo for repo in session_paths if repo not in dirty)
        if unjudged:
            described.append("commitblock:unjudged=" + ",".join(unjudged))
        if described:
            facts["repos"] = " ".join(described)

    if (signal and not trailer and not session_names and not misstated and not unproven and not missing_block
            and not unreported
            and not peer_owed and not workspace_missing and not unparked and not unparked_ask and not unread_page
            and not prose_reask and not no_page and not start_decision and not unpushed and not unregistered
            and not unconsented and not unaudited and not unanswered_asks):
        unsaved = save_pending_asks(state_root, safe_id, operator_marker, asked) if asked is not None else None
        if unsaved is None or names_unsaved_path(facts["assistant_text"], unsaved):
            write_decision(state_root, safe_id, signal)
            write_facts(state_root, safe_id, signal, facts, transcript_path)
            return signal, ""
    else:
        unsaved = None
        # A refused stop's ask reached the operator too, unless what refused it was the ask's own shape (not parked,
        # not read against the page, a re-ask in prose): those asks were never ones to stand on.
        if (signal in ("allow:blocker-token", "allow:question-mark") and shown and not unparked and not unparked_ask
                and not unread_page and not prose_reask):
            delivered = pending + [ask for ask in shown if not any(same_question(ask, earlier) for earlier in pending)]
            save_pending_asks(state_root, safe_id, operator_marker, delivered, refused=True)

    # The turn boundary comes from the PLATFORM, not the transcript. Read as a BOUNDARY it allows
    # nothing; it only says when to reset the count.
    stop_hook_active = bool(payload.get("stop_hook_active", False))

    state_file = os.path.join(state_root, safe_id + ".json")
    blocks = 0
    idle_before = 0
    if stop_hook_active and os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            blocks = int(state.get("blocks", 0))
            idle_before = int(state.get("idle", 0))
        except Exception:
            blocks = 0
            idle_before = 0

    # A REFUSED STOP ANSWERED WITH NO TOOL CALL extends a run; anything else starts one. The transcript must show
    # the gate's own feedback with no tool call after it, so a run is never inferred from the counter alone.
    idle = idle_before + 1 if stop_hook_active and facts["tool_uses_since_block"] == 0 else 1

    # A trailer block says what is actually wrong. Handing back the generic four-question reason would
    # leave the agent re-deriving why a message that had earned its stop was refused.
    if unsaved:
        reason, tag = PENDING_UNSAVED_REASON.format(path=unsaved), "pending-unsaved:"
    elif workspace_missing:
        reason, tag = WORKSPACE_MISSING_REASON.format(path=workspace), "workspace-missing:"
    elif peer_owed:
        reason, tag = "STOP GATE: you cannot end the turn while a peer waits on you.\n" + peer_owed, "peer-owed:"
    elif repeated:
        reason, tag = REPEATED_QUESTION_REASON.format(question=repeated.strip()), "repeat-question:"
        if (facts["last_user_text"] == "" or refused_pending) and not own_work_left(transcript_path, gate_workspace):
            reason = turn_reason((UNREAD_ON_WAKE_REASON if repeated in page_unread
                                  else REPEATED_ON_WAKE_REASON).format(question=repeated.strip()), refused_turn)
    elif woken_unread:
        reason, tag = turn_reason(UNREAD_ON_WAKE_REASON.format(question=woken_unread.strip()), refused_turn), "wake-unread:"
    elif woken_loud:
        reason, tag = turn_reason(LOUD_WAKE_REASON.format(problem=woken_loud), refused_turn), "wake-loud:"
    elif undone:
        reason, tag = OPEN_ITEM_REASON.format(count=len(undone), first=undone[0]), "open-item:"
    elif landing_left:
        reason, tag = LANDING_WORK_REASON.format(count=landing_left, first=first_open_item(
            transcript_path, gate_workspace) or "unreadable"), "landing-work:"
    elif handoff_left:
        reason, tag = HANDOFF_CLOSURE_REASON.format(reason=handoff_left), "handoff-closure:"
    elif refusal_problem:
        reason, tag = REFUSAL_BASIS_REASON.format(problem=refusal_problem), "refusal-unquoted:"
    elif claim_problem:
        reason, tag = OPERATOR_CLAIM_REASON.format(problem=claim_problem), "operator-claim:"
    elif done_open:
        reason, tag = DONE_OPEN_REASON.format(problem=done_open), "done-open:"
    elif unaccepted:
        reason, tag = DONE_UNACCEPTED_REASON.format(problem=unaccepted), "done-unaccepted:"
    elif commit_ask:
        reason, tag = COMMIT_ASK_REASON, "commit-ask:"
    elif unparked_commits:
        reason, tag = UNPARKED_COMMIT_REASON.format(problems="\n".join(unparked_commits)), "unparked-commit:"
    elif merged_left:
        import merged_branches
        reason, tag = merged_branches.MERGED_BRANCH_REASON.format(problems="\n".join(merged_left)), "merged-branch:"
    elif unreported:
        reason, tag = UNREPORTED_GATE_BLOCK.format(command=unreported), "unreported-gate:"
    elif trailer:
        reason, tag = TRAILER_REASON, "trailer:"
    elif session_names:
        reason, tag = SESSION_NAME_REASON.format(names=", ".join(session_names)) + STOP_RULES_POINTER, "session-name:"
    elif misstated:
        reason, tag = goal_reason(goal), "goal:"
    elif unproven:
        reason = GATE_CLAIM_REASON.format(
            repos=", ".join(sorted(unproven)),
            states=", ".join("{0}: {1}".format(repo, state) for repo, state in sorted(unproven.items())))
        tag = "gate-claim:"
    elif missing_block:
        reason, tag = commit_state_reason(owed, stale, missing, ambiguous), ("stale:" if stale else "commitblock:")
    elif unparked:
        reason, tag = UNPARKED_QUESTION_REASON, "unparked-question:"
    elif unparked_ask:
        reason, tag = UNPARKED_NEEDED_REASON.format(ask=unparked_ask.strip()), "unparked-needed:"
    elif unread_page:
        reason, tag = UNREAD_PAGE_REASON.format(problem=unread_page), "unread-page:"
    elif prose_reask:
        reason, tag = UNREAD_PAGE_REASON.format(problem=prose_reask), "prose-reask:"
    elif no_page:
        reason, tag = NO_PAGE_STOP_REASON, "no-page:"
    elif start_decision:
        reason, tag = UNPARKED_START_DECISION_REASON.format(line=start_decision[:400]), "unparked-start-decision:"
    elif unregistered:
        reason = UNREGISTERED_REASON.format(problem=coordination.unregistered_reason(session_id, coordination_config))
        tag = "unregistered:"
    elif unconsented:
        reason = UNCONSENTED_REASON.format(problem=coordination.unconsented_reason(session_id, coordination_config,
                                                                                   asking=True))
        tag = "unconsented:"
    elif unaudited:
        reason, tag = UNAUDITED_REASON.format(problem=unaudited), "unaudited:"
    elif unpushed == ["read"]:
        reason, tag = MASTER_PUSH_READ_REASON, "master-push-read:"
    elif unpushed:
        reason, tag = MASTER_PUSH_REASON.format(ids=", ".join(unpushed)), "master-push:"
    elif unanswered_asks:
        reason, tag = UNANSWERED_ASKS_REASON.format(problem=unanswered_asks), "unanswered-asks:"
    else:
        reason, tag = GATE_REASON, ""
        next_item = first_open_item(transcript_path, gate_workspace)
        if next_item:
            reason = GATE_REASON.replace(GATE_REASON_ACTION, NEXT_ITEM_ACTION.format(item=next_item), 1)
        rejected = unaccepted_wait_claim(facts["assistant_text"], facts["messaged"], transcript_path, temp_root,
                                         gate_workspace, facts["messaged_to"])
        if rejected:
            facts["wait"] = wait_readings(transcript_path, temp_root, gate_workspace)
            reason = reason.replace("nothing in your last message earns a stop",
                                         WAIT_CLAIM_NOTE.format(token=rejected[0], why=rejected[1]).rstrip("."), 1)
            tag = "wait-claim:"
        else:
            missed = near_miss_note(facts["assistant_text"])
            if missed:
                reason = reason.replace(GATE_REASON_HEAD, GATE_REASON_HEAD[:-1] + ": " + missed + ".", 1)
    if blocks >= max_blocks:
        tag = PAST_BUDGET_TAG + tag

    with open(state_file, "w", encoding="utf-8") as fh:
        json.dump({"blocks": blocks + 1, "tag": tag, "idle": idle}, fh, separators=(",", ":"))

    # A RUN OF REFUSED STOPS WITH NO WORK BETWEEN THEM ENDS HERE, before Claude Code's nine-block cap ends it with no
    # trace. Each further block would only buy another line of filler; ending the turn with a reason the operator
    # sees makes it a recorded failure instead of a silent stop.
    if idle >= FILLER_STREAK_LIMIT:
        signal = "{0}{1}{2}".format(FILLER_STREAK_TAG, tag, idle)
        write_decision(state_root, safe_id, signal)
        write_facts(state_root, safe_id, signal, facts, transcript_path)
        if eject_strikes(state_root, safe_id, operator_marker) >= EJECT_STRIKE_LIMIT:
            halted = quarantine_for_filler(session_id, quarantine_root, signal)
            if halted:
                write_decision(state_root, safe_id, "eject:quarantined:filler-streak")
                return "eject:quarantined", halted
        next_item = first_open_item(transcript_path, gate_workspace)
        return signal, FILLER_STREAK_REASON.format(
            count=idle, tag=tag.rstrip(":") or "nothing earns a stop",
            next=FILLER_NEXT_ITEM.format(item=next_item) if next_item else FILLER_NO_ITEM)

    signal = "block:{0}{1}".format(tag, blocks + 1)
    write_decision(state_root, safe_id, signal)
    write_facts(state_root, safe_id, signal, facts, transcript_path)
    return signal, reason


def envelope(signal, reason, end_turn=False):
    """The ONE place the Stop hook's output is built, as text for stdout. An eject halts the session:
    `continue: false` stops Claude Code's processing, where `decision: block` would tell the session to keep
    working. A block writes the block envelope, and an allowed stop writes nothing, unless it ends over an active
    /goal, which writes `continue: false` so the /goal hook's own block cannot reopen a turn this gate ended. Stdout
    that does not parse as JSON is read as advisory text, so leaves return plain text and never decide the wire
    format."""
    if signal and signal.startswith("eject:"):
        return json.dumps({"continue": False, "stopReason": reason}, separators=(",", ":"))
    if reason:
        return json.dumps({"decision": "block", "reason": reason}, separators=(",", ":"))
    if end_turn:
        return json.dumps({"continue": False, "stopReason": GOAL_HELD_STOP_REASON.format(
            signal=signal[len("allow:"):])}, separators=(",", ":"))
    return ""


GOAL_HELD_STOP_REASON = ("Stop gate: the turn ended on an earned stop ({signal}) while /goal is not met, because"
                         " what is left of it waits on someone other than this session.")
WAITS_ON_SOMEONE_ELSE = ("allow:blocker-token", "allow:question-mark", "allow:queued-for-gate",
                         "allow:waiting-on-own-gate", QUIET_WAKE_SIGNAL, MASTER_IDLE_SIGNAL)


def ends_over_goal(signal, transcript_path, state_root, session_id, text=None):
    """True when an allowed stop waits on someone else (the operator, a peer's gate, its own gate) or declines the
    request, or is a DONE: the operator retired the goal for (goal_retired_by_operator), and an active /goal would
    otherwise block it. Claude Code's /goal is a Stop hook of its own that blocks every stop until its condition is
    met, and a block from either hook continues the turn, so without this every such stop is reopened and the agent
    can only re-ask or fill the turn until the nine-block cap ends it. Ending over the goal is safe for a wait because
    the goal cannot be met until that wait is over, and for a retired goal because the operator, who set it, said it
    is finished. Any other DONE: is the /goal evaluator's call to make."""
    retired = signal == "allow:work-complete"
    if signal not in WAITS_ON_SOMEONE_ELSE and not (signal or "").startswith("allow:refusal") and not retired:
        return False
    try:
        goal = active_goal_command(transcript_path, state_root, safe_session_id(session_id, transcript_path))
        if not goal:
            return False
        return not retired or goal_retired_by_operator(text, transcript_path, goal)
    except Exception:
        return False


GOAL_RETIRE_WORDS = frozenset({"finished", "complete", "completed", "done", "retired", "over"})
GOAL_RETIRE_NEGATIONS = frozenset({"not", "no", "never", "isn't", "isnt", "yet", "almost", "nearly"})
GOAL_RETIRE_MIN_WORDS = 4


def goal_retired_by_operator(text, transcript_path, goal):
    """True when a DONE: line quotes, verbatim and in double quotes, four or more words the operator said or answered
    on this session's page (operator_texts, a page answer only once read back) since the goal was pinned, and the
    quote names the goal and calls it finished with no negation ("The goal is finished"). The pinned goal's own words
    never count, since every DONE: carries them, and neither does a quote that retires nothing ("checked in and
    pushed")."""
    cadence = cadence_module()
    if cadence is None or not text:
        return False
    import goal_pin
    since = goal_pin.goal_set_time(transcript_path)
    said = None
    goal_words = " {0} ".format(normalised_words(goal))
    for payload in RE_DONE_LINE.findall(text):
        for quote in RE_QUOTE_SPAN.findall(payload):
            words = normalised_words(quote).split()
            if len(words) < GOAL_RETIRE_MIN_WORDS or " {0} ".format(" ".join(words)) in goal_words:
                continue
            if "goal" not in words or not GOAL_RETIRE_WORDS & set(words) or GOAL_RETIRE_NEGATIONS & set(words):
                continue
            if said is None:
                said = cadence.operator_texts(transcript_path, since)
            if cadence.operator_approved(quote, said):
                return True
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--transcript-path", default=None)
    parser.add_argument("--state-directory", default=None)
    parser.add_argument("--max-blocks", type=int, default=100)
    parser.add_argument("--skip-stdin", action="store_true")
    args = parser.parse_args()

    state_root = None

    try:
        payload = None
        if not args.skip_stdin:
            raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
            if raw:
                payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {}

        path = args.transcript_path or payload.get("transcript_path")
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import claude_temp
        state_root = args.state_directory or claude_temp.stop_gate_state_root()

        signal, reason = decide(payload, path, state_root, args.max_blocks)
        sys.stdout.write(envelope(signal, reason, not reason and ends_over_goal(
            signal, path, state_root, payload.get("session_id"), payload.get("last_assistant_message"))))
        return ALLOW_STOP

    except Exception as exc:
        if state_root:
            write_decision(state_root, "unknown", "block:error:" + type(exc).__name__)
        sys.stdout.write(envelope("block:error", FAILED_REASON.format(
            error=type(exc).__name__ + ": " + str(exc)[:200])))
        return ALLOW_STOP


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
