"""PreToolUse gate: a session works only toward a goal the operator set.

A session opens with a goal: the operator sets one with /goal, or their original prompt or the message right after
it carries a GOAL: line, before the session's third operator message. Without one, every tool call is denied except
the reply tools, so the session can still answer a peer, AskUserQuestion, so it can ask for the goal, and the
calls that register it on the coordination board (is_registration_call), since the prompt gate takes no goal until
it has registered. The goal in
force is read through goal_pin.goal_in_force, the same fold the Stop gate holds every earned stop to.

A session already running when this checkout received the rule (its first record older than this file's
modification time, which git sets on the checkout or pull that brings it) is held only to having some goal. A goal
the session writes itself never counts: only the operator's records and Claude Code's own /goal records set one.

With a goal in force, a session whose transcript carries a SessionStart context is held to two setup steps before
any work. When that context was too large to show and Claude Code saved it to a file, only a Read of that file runs
until one has come back without an error; a compaction or resume saves a new one, which is read again. Then, until
the session's parking-lot page URL is recorded, only the calls that set the page up run. A SessionStart record older
than this file's modification time is not held to either.

Like every gate, a failure to judge is the dispatcher's to fail closed. Verified by the goal rows of
Harness/Hooks/test_hook_dispatcher.py.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

DENY = "deny"
DEFER = "defer"

EXEMPT_TOOLS = ("SendMessage", "ListAgents", "ToolSearch", "AskUserQuestion")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import coordination  # noqa: E402
import invoke_prompt_gate  # noqa: E402
import session_home  # noqa: E402

COORDINATION_SKILL = "coordination"
RE_GET_DATE = re.compile(r"\A\s*Get-Date(\s+-AsUTC)?(\s+-Format\s+o)?\s*\Z", re.IGNORECASE)
TOOLS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def registration_reads(profile=None):
    """The files a session may read to register before it has a goal: the coordination skill, in the Tools checkout,
    through its <workspace>\\.claude\\skills junction or a legacy ~/.claude/skills one, and coordination.json."""
    profile = profile if profile is not None else os.environ.get("USERPROFILE", "")
    skill_roots = [os.path.join(session_home.workspace_root(), "Tools", "Claude", "skills", COORDINATION_SKILL),
                   os.path.join(session_home.workspace_root(), ".claude", "skills", COORDINATION_SKILL)]
    if profile:
        skill_roots.append(os.path.join(profile, ".claude", "skills", COORDINATION_SKILL))
    return [os.path.normcase(os.path.normpath(root)) for root in skill_roots], \
        os.path.normcase(os.path.normpath(coordination.CONFIG_PATH))


def is_registration_call(tool_name, tool_input, session_id=None, config_path=None, profile=None):
    """True for the calls a session makes to register on the coordination board before it has a goal: loading the
    coordination skill, reading its files or coordination.json, reading the board or writing only its own row
    there, and a bare Get-Date for the row's time."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Skill":
        return tool_input.get("skill") == COORDINATION_SKILL
    if tool_name == "ArtifactData":
        board_url, collection = coordination.config(config_path)
        if not board_url or tool_input.get("url") != board_url:
            return False
        return (tool_input.get("action") in coordination.READ_ACTIONS
                or coordination.own_row_writes(tool_input, board_url, collection, session_id))
    if tool_name == "Read":
        path = os.path.normcase(os.path.normpath(str(tool_input.get("file_path") or "")))
        skill_roots, config_file = registration_reads(profile)
        return path == config_file or any(path.startswith(root + os.sep) for root in skill_roots)
    if tool_name == "PowerShell":
        return bool(RE_GET_DATE.match(str(tool_input.get("command") or "")))
    return False

STATE_ROOT = claude_temp.stop_gate_state_root()

REASON = (
    "Blocked: this session has no goal. No /goal is active and the operator's last GOAL: line is retired or absent, "
    "and no tool runs until they set one. Ask them for it: end the turn with a plain question asking them to run "
    "/goal with what this session is to finish."
)


OPENING_REASON = (
    "Blocked: this session did not open with a goal. The operator must set one with /goal (or a GOAL: line) before "
    "their third message, and no tool runs without one. If they have sent fewer than two messages, ask them for it: "
    "end the turn with a plain question asking them to run /goal with what this session is to finish (AskUserQuestion "
    "also runs). Once two have gone by without one, no later /goal opens this session: say so in plain words, and "
    "that the work needs a new session whose first message is /goal or carries a GOAL: line."
)


def stop_gate():
    """The pin reader the Stop hook also uses (goal_pin.py), not the whole Stop hook."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import goal_pin
    return goal_pin


def goal_in_force(transcript_path, session_id, state_root=None):
    """The operator's goal for this session, or None."""
    return stop_gate().goal_in_force(transcript_path, state_root or STATE_ROOT, session_id or "unknown")


OPENING_MESSAGES = 2


def opening_goal(transcript_path):
    """The goal the session opened with: a /goal, or a GOAL: line in the original prompt or the operator message
    right after it, before the third operator message, else None. Read from the head of the transcript, so a long
    session costs no more than a short one."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    gate = stop_gate()
    seen = 0
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            command = gate.goal_command(record)
            if command:
                return command
            text = gate.operator_text(record)
            if text is None:
                continue
            seen += 1
            pin = gate.goal_pin(text)
            if pin:
                return pin
            if seen >= OPENING_MESSAGES:
                return None
    return None


def rule_effective_at(gate_file=None):
    """When the opening-goal rule took effect in this checkout: the modification time of the gate file, which the
    checkout or pull that brought it set. None when it cannot be read, and then the rule applies."""
    try:
        return datetime.fromtimestamp(os.path.getmtime(gate_file or os.path.abspath(__file__)), timezone.utc)
    except (OSError, ValueError, OverflowError):
        return None


def opened_under_the_rule(transcript_path, effective_at=None):
    """True when the session's first record is no older than the deploy that brought the opening-goal rule in, so
    a session already running at the switch is held only to having some operator pin, not to an opening one.
    With no readable time the rule applies, the stricter case."""
    effective_at = effective_at or rule_effective_at()
    if effective_at is None:
        return True
    if not transcript_path or not os.path.isfile(transcript_path):
        return True
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                stamp = json.loads(line).get("timestamp")
            except (ValueError, AttributeError):
                continue
            if isinstance(stamp, str):
                try:
                    return datetime.fromisoformat(stamp.replace("Z", "+00:00")) >= effective_at
                except ValueError:
                    return True
    return True


WORKSPACE = session_home.workspace_root()
SESSION_START = "SessionStart"
SAVED_OUTPUT = re.compile(r"\A\s*<persisted-output>\s*\r?\n[^\r\n]*Full output saved to: ([^\r\n]+)")
PAGE_SETUP_TOOLS = ("Read", "Skill", "Artifact", "ArtifactData")
PAGE_SETUP_COMMAND = re.compile(r"^\s*(python\s+\S*parking_lot\.py[\"']?\s+(render|record-url|show)\b|Get-Date\b)",
                                re.IGNORECASE)

UNREAD_START_REASON = (
    "Blocked: this session has not read its session-start instructions. The SessionStart hook's output was too "
    "large to show and was saved to {path}. Read that file in full with the Read tool before any other call: it "
    "holds the Stop-gate rules, the artifact-usage rules and any decision the hook hands the operator."
)

NO_PAGE_REASON = (
    "Blocked: this session has no parking-lot page yet, and no work runs before it does. Set it up with the "
    "parking-lot skill: parking_lot.py render <session id>, publish the file with the Artifact tool, then "
    "parking_lot.py record-url <url> and the session/goal write. Until the URL is recorded only those setup calls "
    "run: Read, Skill, Artifact, ArtifactData, and the parking_lot.py render, record-url and show and Get-Date "
    "commands."
)


def same_path(left, right):
    return os.path.normcase(os.path.normpath(left.strip())) == os.path.normcase(os.path.normpath(right.strip()))


def load_start_cache(cache_path, transcript_path, size):
    """(offset, start, reads) a previous scan of this transcript saved, or a fresh scan's (0, None, {}) when there is
    none, it cannot be read, it scanned another transcript, or the transcript is now shorter than the offset it
    reached."""
    try:
        with open(cache_path, "r", encoding="utf-8") as handle:
            cached = json.load(handle)
        offset = int(cached["offset"])
        start = cached["start"]
        reads = {str(use_id): True for use_id in cached["reads"]}
        if (same_path(cached["transcript"], transcript_path) and 0 <= offset <= size
                and (start is None or isinstance(start, dict))):
            return offset, start, reads
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return 0, None, {}


def save_start_cache(cache_path, transcript_path, offset, start, reads):
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as handle:
            json.dump({"transcript": transcript_path, "offset": offset, "start": start, "reads": sorted(reads)},
                      handle)
    except OSError:
        pass


def session_start(transcript_path, cache_path=None):
    """The newest SessionStart context in the transcript as {"at", "saved", "read", "text"}, or None when there is
    none. text is the context as the transcript holds it (only a preview when it was saved to a file); saved is the
    file Claude Code saved the context to when it was too large to show, or None; read is True once a
    Read of that file came back without an error after that record. A compaction or resume adds a new record, and
    its file must be read again. With cache_path the scan resumes where the last one stopped, at its last complete
    line, since this runs on every tool call and a transcript can run to hundreds of megabytes."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    size = os.path.getsize(transcript_path)
    offset, start, reads = load_start_cache(cache_path, transcript_path, size) if cache_path else (0, None, {})
    with open(transcript_path, "rb") as handle:
        handle.seek(offset)
        for raw in handle:
            if not raw.endswith(b"\n"):
                break
            offset += len(raw)
            pending_read = start is not None and start["saved"] and not start["read"]
            if b"hook_additional_context" not in raw and not (
                    pending_read and (b'"Read"' in raw or b'"tool_result"' in raw)):
                continue
            try:
                record = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            attachment = record.get("attachment")
            if (isinstance(attachment, dict) and attachment.get("type") == "hook_additional_context"
                    and attachment.get("hookEvent") == SESSION_START):
                content = attachment.get("content")
                text = "\n".join(c for c in content if isinstance(c, str)) if isinstance(content, list) else str(content)
                found = SAVED_OUTPUT.search(text)
                start = {"at": record.get("timestamp"), "saved": found.group(1).strip() if found else None,
                         "read": False, "text": text}
                reads = {}
                continue
            if start is None or not start["saved"] or start["read"]:
                continue
            message = record.get("message")
            parts = message.get("content") if isinstance(message, dict) else None
            for part in parts if isinstance(parts, list) else ():
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_use" and part.get("name") == "Read":
                    path = (part.get("input") or {}).get("file_path")
                    if isinstance(path, str) and same_path(path, start["saved"]):
                        reads[part.get("id")] = True
                elif (part.get("type") == "tool_result" and reads.get(part.get("tool_use_id"))
                      and not part.get("is_error")):
                    start["read"] = True
    if cache_path:
        save_start_cache(cache_path, transcript_path, offset, start, reads)
    return start


def started_under_the_rule(start, effective_at=None):
    """True when the SessionStart record is no older than the deploy that brought the session-start rule in, so a
    session already past its start when the rule landed is not stopped mid-work. With no readable time the rule
    applies."""
    effective_at = effective_at or rule_effective_at()
    stamp = start.get("at")
    if effective_at is None or not isinstance(stamp, str):
        return True
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")) >= effective_at
    except ValueError:
        return True


def recorded_page(workspace, session_id):
    """The session's parking-lot page URL as parking_lot.py record-url wrote it, or None."""
    for directory in session_home.parking_dirs(workspace, session_id):
        try:
            with open(os.path.join(directory, "state.json"), "r", encoding="utf-8") as handle:
                url = json.load(handle).get("url")
        except (OSError, ValueError, AttributeError, TypeError):
            continue
        if isinstance(url, str) and url:
            return url
    return None


def is_page_setup(tool_name, tool_input):
    if tool_name in PAGE_SETUP_TOOLS:
        return True
    command = (tool_input or {}).get("command")
    return tool_name == "PowerShell" and isinstance(command, str) and bool(PAGE_SETUP_COMMAND.match(command))


def start_cache_path(state_root, session_id):
    """Where the session-start scan keeps its offset: beside the goal latch, in the Stop gate's state, which no
    session's tools may write."""
    return os.path.join(state_root or STATE_ROOT, re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id)) + ".start.json")


def session_start_problem(tool_name, tool_input, transcript_path, session_id, workspace=None, effective_at=None,
                          state_root=None):
    """The deny reason for a call the session-start rules hold back, or None. They apply only to a session whose
    transcript carries a SessionStart context: first the saved output must be read, then the parking-lot page
    must exist before any work."""
    start = session_start(transcript_path, start_cache_path(state_root, session_id) if session_id else None)
    if start is None or not started_under_the_rule(start, effective_at):
        return None
    if start["saved"] and not start["read"]:
        read_of_it = (tool_name == "Read" and isinstance((tool_input or {}).get("file_path"), str)
                      and same_path(tool_input["file_path"], start["saved"]))
        return None if read_of_it else UNREAD_START_REASON.format(path=start["saved"])
    if recorded_page(workspace or WORKSPACE, session_id) or is_page_setup(tool_name, tool_input):
        return None
    return NO_PAGE_REASON


def unregistered_problem(transcript_path, session_id, state_root=None, prompt_rule_at=None, config_path=None):
    """The reason a session the prompt gate's rule holds is denied work while it is not registered, or registered
    without the operator's artifact-write consent, else None. A goal pinned some way the prompt gate never saw is still
    no way past registering, and work waits on the consent so the operator is asked at session start, never first in
    an unattended turn; the own-row write that obtains it is a registration call, so it still runs. The consent
    reason says whether this turn is one where Claude Code asks (a slash command's, Stop hook feedback's, a peer's or
    a task's), so a typed turn is ended rather than filled with writes that cannot count."""
    prompt_rule_at = prompt_rule_at if prompt_rule_at is not None else invoke_prompt_gate.rule_effective_at()
    if not invoke_prompt_gate.started_since(transcript_path, prompt_rule_at):
        return None
    root = state_root or STATE_ROOT
    if not coordination.registered_latched(transcript_path, session_id, root, config_path):
        return "Blocked: " + coordination.unregistered_reason(session_id, config_path)
    if not coordination.consented_latched(transcript_path, session_id, root):
        asking = coordination.consent_state([transcript_path] if transcript_path else [])[1]
        return ("Blocked: " + coordination.unconsented_reason(session_id, config_path, asking)
                + " Until then no other tool runs.")
    return None


def evaluate(tool_name, transcript_path, session_id, state_root=None, tool_input=None, workspace=None,
             effective_at=None, prompt_rule_at=None, config_path=None):
    """(decision, reason). A session must open with a goal, and a tool runs only while one is in force and the
    session is registered on the coordination board. A session that started under a SessionStart context then reads
    it and sets up its parking-lot page before any work."""
    if tool_name in EXEMPT_TOOLS:
        return DEFER, ""
    if is_registration_call(tool_name, tool_input, session_id, config_path):
        return DEFER, ""
    if opened_under_the_rule(transcript_path) and opening_goal(transcript_path) is None:
        return DENY, OPENING_REASON
    if not goal_in_force(transcript_path, session_id, state_root):
        return DENY, REASON
    problem = unregistered_problem(transcript_path, session_id, state_root, prompt_rule_at, config_path)
    if problem:
        return DENY, problem
    problem = session_start_problem(tool_name, tool_input, transcript_path, session_id, workspace, effective_at,
                                    state_root)
    return (DENY, problem) if problem else (DEFER, "")
