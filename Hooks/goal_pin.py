"""The operator's goal for a session: which records are the operator's own, the two channels that set a goal, and
the per-session latch that folds the transcript once. The goal gate, the Stop hook and the gate-cadence hook all
read the goal through goal_in_force, so no two of them can disagree about it.

Claude Code's /goal is the source: while one is active, its condition is the goal. Without one, the newest GOAL:
line in the operator's own text is the goal, and GOAL: none retires it."""

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime

from stop_tokens import token_line

FLAGS = re.IGNORECASE
RE_STOP_GATE = re.compile(r"STOP GATE:", FLAGS)
RE_SYSTEM_REMINDER = re.compile(r"<system-reminder>", FLAGS)
RE_GOAL_PIN = token_line("GOAL")
RE_QUOTE_MARK = re.compile("[\"“”]")
COMPACT_SUMMARY_FLAG = "isCompactSummary"
RE_PEER_MESSAGE = re.compile(r"<cross-session-message\b", FLAGS)
RE_GOAL_RETIRED = re.compile(r"^[\s*_`\-]*(none|clear(ed)?|retired?|unset|off)[\s.!]*$", FLAGS)
RE_NOT_ALNUM = re.compile(r"[^a-z0-9]+")
GOAL_STATUS = "goal_status"
LINE_NEEDLES = (b"GOAL:", b"goal_status")
RE_COMMAND_RECORD = re.compile(r"\A\s*<command-name>")
RE_COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)


def normalised_words(text):
    """Everything that is not a letter or digit collapses to one space, so quotes, dashes, case and punctuation
    cannot make a faithful restatement of the goal look unfaithful."""
    return " " + RE_NOT_ALNUM.sub(" ", text.lower()).strip() + " "


def block_text(content):
    """The concatenated `text` blocks of a message content array."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def operator_text(record):
    """The text the operator wrote in this record, or None. Only two shapes carry it: a user record whose origin
    kind is "human", and a queued_command attachment (a message sent mid-turn) whose attachment origin kind is
    "human". Peer messages, task notifications, skill injections, compaction summaries, gate feedback and system
    reminders are never the operator's, so nothing they say can pin a goal."""
    if not isinstance(record, dict) or record.get(COMPACT_SUMMARY_FLAG) or record.get("isMeta"):
        return None
    if record.get("type") == "attachment":
        attachment = record.get("attachment") if isinstance(record.get("attachment"), dict) else {}
        origin = attachment.get("origin") if isinstance(attachment.get("origin"), dict) else {}
        prompt = attachment.get("prompt")
        if attachment.get("type") == "queued_command" and origin.get("kind") == "human" and isinstance(prompt, str):
            return prompt
        return None
    if record.get("type") != "user":
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    if isinstance(content, list) and not any(b.get("type") == "text" for b in blocks):
        return None
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    rides_a_tool_result = any(b.get("type") == "tool_result" for b in blocks)
    if origin.get("kind") != "human" and not (rides_a_tool_result and not origin.get("kind")):
        return None
    if rides_a_tool_result:
        content = [b for b in blocks if b.get("type") == "text"]
    text = block_text(content)
    if not text or RE_STOP_GATE.search(text) or RE_SYSTEM_REMINDER.search(text) or RE_PEER_MESSAGE.search(text):
        return None
    return text


def command_args(record):
    """The arguments the operator typed after a slash command (/goal <text>), else None: Claude Code records the
    command as a user record with no origin whose text opens with <command-name>, so operator_text never sees it,
    while the words are the operator's own and no tool can write such a record. The <local-command-stdout> record
    that follows it, a record with any origin and a command run with no arguments carry none."""
    if not isinstance(record, dict) or record.get("type") != "user" or record.get("isMeta") \
            or record.get(COMPACT_SUMMARY_FLAG):
        return None
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    if origin.get("kind"):
        return None
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    text = block_text(message.get("content"))
    if not RE_COMMAND_RECORD.match(text):
        return None
    match = RE_COMMAND_ARGS.search(text)
    args = match.group(1).strip() if match else ""
    return args or None


def goal_pin(text):
    """The payload of the first GOAL: line in an operator's text, else None. A GOAL: inside a double-quoted span is
    someone else's words pasted in and pins nothing: the marks are counted from the start of the text, so a pasted
    message may run over several paragraphs, and the span must also close after the GOAL:, so one stray mark
    earlier in the message cannot silence a real pin."""
    for match in RE_GOAL_PIN.finditer(text or ""):
        opened = len(RE_QUOTE_MARK.findall(text, 0, match.start())) % 2 == 1
        if not (opened and RE_QUOTE_MARK.search(text, match.start(1))):
            return match.group(1)
    return None


def goal_command(record):
    """What a /goal record says: its condition while the goal is active, "" once the goal is met, else None (not a
    goal record, or a shape this reader does not know, which leaves the goal as it was). Claude Code writes this
    goal_status attachment itself, when the operator sets a goal and at every stop it judges."""
    if not isinstance(record, dict) or record.get("type") != "attachment":
        return None
    attachment = record.get("attachment")
    if not isinstance(attachment, dict) or attachment.get("type") != GOAL_STATUS:
        return None
    if attachment.get("met") is True:
        return ""
    condition = attachment.get("condition")
    if attachment.get("met") is False and isinstance(condition, str) and condition.strip():
        return condition.strip()
    return None


def absorb(state, record):
    """Fold one record into the goal state: a /goal record sets or clears the command, an operator GOAL: line sets
    or retires the text pin."""
    command = goal_command(record)
    if command is not None:
        state["command"] = command or None
        return
    text = operator_text(record)
    pin = goal_pin(text) if text else None
    if pin:
        state["text"] = None if RE_GOAL_RETIRED.match(pin) else pin


def in_force(state):
    """An active /goal outranks a GOAL: line."""
    return state.get("command") or state.get("text")


def pin_reader_digest():
    """A digest of the code that reads the goal, so a latch written by another reader is folded again."""
    digest = hashlib.sha256()
    for function in (goal_pin, goal_command, absorb, in_force, operator_text):
        digest.update(function.__code__.co_code)
        digest.update(repr(function.__code__.co_consts).encode("utf-8"))
    for pattern in (RE_GOAL_PIN, RE_GOAL_RETIRED, RE_QUOTE_MARK):
        digest.update(pattern.pattern.encode("utf-8"))
    digest.update(repr(LINE_NEEDLES).encode("utf-8"))
    return digest.hexdigest()[:16]


def latch_path(state_root, session_id, transcript_path):
    """<state_root>/<session>.goal.json, the session named by its id or else its transcript's file name."""
    if not state_root:
        return None
    name = session_id or os.path.splitext(os.path.basename(transcript_path or ""))[0] or "unknown"
    return os.path.join(state_root, re.sub(r"[^A-Za-z0-9._-]", "_", str(name)) + ".goal.json")


def read_latch(path):
    """The latched fold, or None when there is none or another reader wrote it."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(state, dict) or state.get("reader") != pin_reader_digest() \
            or not isinstance(state.get("offset"), int):
        return None
    return state


def write_latch(path, state):
    """Written to a temporary file and renamed over the latch, so a concurrent reader sees one whole fold."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, separators=(",", ":"))
        os.replace(temporary, path)
    except OSError:
        return


def goal_in_force(transcript_path, state_root=None, session_id=None):
    """The goal in force for the session, or None."""
    state = goal_state(transcript_path, state_root, session_id)
    return in_force(state) if state else None


def record_seconds(record):
    """The record's timestamp as POSIX seconds, else None."""
    stamp = record.get("timestamp") if isinstance(record, dict) else None
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def goal_set_time(transcript_path):
    """POSIX seconds of the record that set the goal now in force, else None: the moment the operator asked for the
    work the session's newest declaration answers. A /goal judged again at a stop restates its condition and moves
    nothing."""
    state = {"command": None, "text": None}
    current, since = None, None
    for record in goal_records(transcript_path):
        absorb(state, record)
        goal = in_force(state)
        if goal != current:
            current = goal
            since = record_seconds(record)
    return since if current else None


def goal_records(transcript_path):
    """The transcript's records that can set a goal, through the shared resumable index (transcript_index.py)."""
    import transcript_index
    return transcript_index.candidate_records(transcript_path, LINE_NEEDLES, transcript_index.default_cache_root())


def active_goal_command(transcript_path, state_root=None, session_id=None):
    """The condition of the session's active /goal, or None when no /goal is active. Claude Code judges a /goal at
    every stop with a Stop hook of its own, which a GOAL: line does not have."""
    state = goal_state(transcript_path, state_root, session_id)
    return state.get("command") if state else None


def goal_state(transcript_path, state_root=None, session_id=None):
    """The folded goal state for the session, or None. With a state root the fold is latched with the byte offset
    it reached, so each call reads only what the transcript gained since; a latch from another reader, or past the
    end of the file, is folded again from the start. Only complete lines are read, so a line still being written
    is read whole next time. Without a transcript, the latch alone answers."""
    path = latch_path(state_root, session_id, transcript_path)
    state = read_latch(path) if path else None
    if not transcript_path or not os.path.isfile(transcript_path):
        return state
    if not path:
        state = {"command": None, "text": None}
        for record in goal_records(transcript_path):
            absorb(state, record)
        return state
    with open(transcript_path, "rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if state is None or state["offset"] > size:
            state = {"reader": pin_reader_digest(), "offset": 0, "command": None, "text": None}
        handle.seek(state["offset"])
        data = handle.read()
    end = data.rfind(b"\n") + 1
    if end:
        for raw in data[:end].split(b"\n"):
            if not any(needle in raw for needle in LINE_NEEDLES):
                continue
            try:
                absorb(state, json.loads(raw.decode("utf-8", errors="replace")))
            except ValueError:
                continue
        state["offset"] += end
        if path:
            write_latch(path, state)
    return state
