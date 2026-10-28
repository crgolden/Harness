"""The coordination board: where every session registers before it takes a goal (Claude/skills/coordination).

A session is registered once its transcript holds a successful ArtifactData write of its own row in the board's
sessions collection, in any turn. The prompt gate holds back /goal and GOAL: lines until then, and the goal gate lets
the registration calls through before a goal exists.

Consent is a separate fact. Claude Code asks the operator once per session to allow artifact writes, in a turn the
operator's typed message did not start: one Stop hook feedback, a peer message, a task notification or a slash
command such as /goal started (Claude Code records a slash command's turn without an origin, and the first write in
one raised the prompt). A session has consent once its transcript holds a successful artifact write made in such a
turn. The Stop gate refuses a registered session's stops until it has consent, so the operator is asked at session
start rather than the first time an unattended turn writes an artifact."""

import json
import os
import re
from datetime import datetime

import goal_pin

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "coordination.json")
RE_GOAL_COMMAND = re.compile(r"\A\s*/goal\b")
RE_PEER_OR_TASK = re.compile(r"<cross-session-message\b|<task-notification\b", re.IGNORECASE)
STOP_FEEDBACK = "Stop hook feedback"
RE_COMMAND_RECORD = re.compile(r"\A\s*<command-name>")
OWN_ROW_ACTIONS = ("set", "update")
READ_ACTIONS = ("get", "list", "query")
ARTIFACT_READ_ACTIONS = ("read", "list", "open", "quickstart")


def config(path=None):
    """(board URL, sessions collection) from coordination.json, or (None, None) when it cannot be read."""
    try:
        with open(path or CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data.get("boardUrl") or None, data.get("sessionsCollection") or "sessions"
    except (OSError, ValueError, AttributeError):
        return None, None


def effective_at(path=None):
    """When the registration rule took effect: effectiveAt in coordination.json, a fixed time, so a later pull or
    edit of a gate file never exempts the sessions running at that moment. None when it cannot be read, and then
    the rule applies to every session."""
    try:
        with open(path or CONFIG_PATH, "r", encoding="utf-8") as handle:
            stamp = json.load(handle).get("effectiveAt")
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def is_goal_prompt(prompt):
    """True for a prompt that sets a goal: a /goal command or a GOAL: line. A peer message or task notification
    quoting one sets nothing, so it is never a goal prompt."""
    if not isinstance(prompt, str) or RE_PEER_OR_TASK.search(prompt):
        return False
    return bool(RE_GOAL_COMMAND.match(prompt)) or goal_pin.goal_pin(prompt) is not None


def starts_unprompted_turn(record):
    """True for a record that starts a turn the operator did not type: Stop hook feedback, a peer message or a task
    notification. A peer message that arrives mid-turn is a queued_command attachment and starts no turn."""
    if not isinstance(record, dict) or record.get("type") != "user":
        return False
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    if origin.get("kind") in ("peer", "task-notification"):
        return True
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    text = goal_pin.block_text(message.get("content"))
    return bool(record.get("isMeta")) and text.lstrip().startswith(STOP_FEEDBACK)


def starts_command_turn(record):
    """True for the record of a slash command the operator ran (/goal): a user record with no origin and no isMeta
    whose text opens with <command-name>. Claude Code writes it without a human origin, so starts_typed_turn never
    sees it, and it asks for artifact-write consent in that turn as it does after Stop hook feedback."""
    if not isinstance(record, dict) or record.get("type") != "user" or record.get("isMeta"):
        return False
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    if origin.get("kind"):
        return False
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    return bool(RE_COMMAND_RECORD.match(goal_pin.block_text(message.get("content"))))


def starts_asking_turn(record):
    """True for a record that starts a turn in which Claude Code asks the operator to allow artifact writes: one the
    operator did not type, or a slash command's."""
    return starts_unprompted_turn(record) or starts_command_turn(record)


def identifies_session(data, session_id):
    """True when a row's data names its writer by sessionId, the permanent id, equal to this session's. A session's
    name is never asked for: it is not unique on one machine and a restart or reset changes it."""
    return isinstance(data, dict) and data.get("sessionId") == session_id


def own_row_writes(call, board_url, collection, session_id, require_identity=False):
    """True when an ArtifactData input on the board writes and every write in it is a set or update of this
    session's own row in the sessions collection; with require_identity, each also carries its session id
    (identifies_session), which is what registration demands."""
    if not isinstance(call, dict) or call.get("url") != board_url or not session_id:
        return False
    writes = call.get("writes") if call.get("action") == "batch" else [call]
    if not isinstance(writes, list) or not writes:
        return False
    for write in writes:
        if not isinstance(write, dict):
            return False
        action = write.get("op") or write.get("action")
        if (action not in OWN_ROW_ACTIONS or write.get("collection") != collection
                or write.get("doc_id") != session_id
                or (require_identity and not identifies_session(write.get("data"), session_id))):
            return False
    return True


def starts_typed_turn(record):
    """True for a record that starts a turn the operator typed: a user record of human origin carrying no
    tool_result. A message the operator queued mid-turn starts none."""
    if not isinstance(record, dict) or record.get("type") != "user" or record.get("isMeta"):
        return False
    origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content")
    rides = isinstance(content, list) and any(isinstance(p, dict) and p.get("type") == "tool_result" for p in content)
    return origin.get("kind") == "human" and not rides


def latch_path(state_root, session_id, suffix=".registered"):
    return os.path.join(state_root, re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id)) + suffix)


def registered_latched(transcript_path, session_id, state_root, config_path=None):
    """registered() for one transcript, latched: once true, a marker in state_root answers every later call without
    reading the transcript again. A false answer is never cached, so a session registers the moment its row lands."""
    return _latched(transcript_path, session_id, state_root, ".registered",
                    lambda paths: registered(paths, session_id, config_path))


def consented_latched(transcript_path, session_id, state_root):
    """consented() for one transcript, latched the same way as registered_latched."""
    return _latched(transcript_path, session_id, state_root, ".consented", consented)


def _latched(transcript_path, session_id, state_root, suffix, check):
    if not session_id:
        return False
    latch = latch_path(state_root, session_id, suffix)
    if os.path.isfile(latch):
        return True
    if not check([transcript_path] if transcript_path else []):
        return False
    try:
        os.makedirs(state_root, exist_ok=True)
        with open(latch, "w", encoding="utf-8") as handle:
            handle.write(str(transcript_path))
    except OSError:
        pass
    return True


def unregistered_reason(session_id, config_path=None):
    """What a session not yet registered is to do, with the values its registration row needs."""
    board_url, collection = config(config_path)
    return ("this session is not registered on the coordination board. Write your row now: ArtifactData set, url "
            "{url}, collection {collection}, doc_id {session}, inline data with sessionId {session} (required; "
            "never a session name, which is neither unique nor stable), plus machine {machine}, goal (the goal in force "
            "verbatim, or none pinned yet) and lastSeen (from a bare Get-Date -AsUTC -Format o). It counts in any "
            "turn; the coordination skill has the rest.").format(url=board_url, collection=collection,
                                                                  session=session_id,
                                                                  machine=os.environ.get("COMPUTERNAME", "unknown"))


ASKING_TURNS = ("a turn Stop hook feedback, a peer's message, a task notification or a slash command such as /goal "
                "started, never one the operator's typed message started")
ROW_UPDATE = ("update your row (ArtifactData update, url {url}, collection {collection}, doc_id {session}, inline "
              "data with lastSeen from a bare Get-Date -AsUTC -Format o, pinned with if_version), so the operator "
              "approves artifact writes at session start rather than the first time an unattended turn writes one")


def unconsented_reason(session_id, config_path=None, asking=None):
    """What a registered session that has not yet been granted artifact writes is to do. `asking` says whether the
    turn being judged is one where Claude Code asks (starts_asking_turn): True tells the session to update its row
    now, False tells it to end the turn so the Stop gate's refusal starts one where the update asks, and None, when
    the turn is not known, names the kind of turn the update must be made in."""
    board_url, collection = config(config_path)
    update = ROW_UPDATE.format(url=board_url, collection=collection, session=session_id)
    head = ("the operator has not yet been asked to allow this session's artifact writes. Claude Code asks in "
            + ASKING_TURNS + ". ")
    if asking is True:
        return head + "This turn is one: " + update + " now."
    if asking is False:
        return (head + "This turn is one the operator typed, where no write asks them: end the turn with a NEEDED: "
                "line asking nothing new, and in the turn the Stop gate's refusal starts, " + update + ".")
    return head + "In such a turn, " + update + "."


def is_artifact_write(name, call):
    """True for a tool call that writes an artifact: an ArtifactData action other than a read, or an Artifact
    publish, delete, pin or unpin."""
    if not isinstance(call, dict):
        return False
    if name == "ArtifactData":
        return call.get("action") not in READ_ACTIONS + ("profiles",)
    if name == "Artifact":
        return (call.get("action") or "publish") not in ARTIFACT_READ_ACTIONS
    return False


def _scan(transcript_paths, wanted):
    """(found, asking): found once a tool call for which wanted(name, input, asking) holds got a result that is no
    error, and asking whether the newest turn-starting record read is one where Claude Code asks for consent
    (starts_asking_turn), None when no record started a turn or the scan ended at the first match. The paths are the
    session's transcripts oldest first, a compaction chain included."""
    pending = {}
    asking = None
    for path in transcript_paths:
        try:
            with open(path, "rb") as handle:
                for raw in handle:
                    if (b'"user"' not in raw and b'"attachment"' not in raw and b'"Artifact' not in raw
                            and b'"tool_result"' not in raw):
                        continue
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        continue
                    if starts_typed_turn(record):
                        asking = False
                    elif starts_asking_turn(record):
                        asking = True
                    message = record.get("message") if isinstance(record, dict) else None
                    content = message.get("content") if isinstance(message, dict) else None
                    for part in content if isinstance(content, list) else []:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "tool_use" and wanted(part.get("name"), part.get("input"), bool(asking)):
                            pending[part.get("id")] = True
                        elif (part.get("type") == "tool_result" and pending.get(part.get("tool_use_id"))
                              and not part.get("is_error")):
                            return True, None
        except OSError:
            continue
    return False, asking


def _successful_calls(transcript_paths, wanted):
    """True once a tool call for which wanted(name, input, asking) holds got a result that is no error."""
    return _scan(transcript_paths, wanted)[0]


def registered(transcript_paths, session_id, config_path=None):
    """True once a write of this session's own board row, carrying its session id and current name, succeeded in
    any turn."""
    board_url, collection = config(config_path)
    if not board_url or not session_id:
        return False
    return _successful_calls(transcript_paths, lambda name, call, unprompted: name == "ArtifactData" and own_row_writes(
        call, board_url, collection, session_id, require_identity=True))


def consent_state(transcript_paths):
    """(consented, asking): consented once an artifact write succeeded in a turn where Claude Code asks the operator
    to allow artifact writes (starts_asking_turn), so the approval has been given; asking whether the newest turn is
    such a turn (None once consented, or with no turn read), which unconsented_reason words its instruction on."""
    return _scan(transcript_paths, lambda name, call, asking: asking and is_artifact_write(name, call))


def consented(transcript_paths):
    """True once an artifact write succeeded in a turn where Claude Code asks the operator to allow artifact writes
    for the session (consent_state)."""
    return consent_state(transcript_paths)[0]
