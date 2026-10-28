"""PreToolUse gate on every tool: a peer session's question or machine handoff is answered before other work.

  deny  any tool call other than a reply while a peer message that asks this session something, or hands it
        the machine, has had no SendMessage back to that peer since it arrived.

A peer message is a `user` record with `origin.kind == "peer"`, or a `queued_command` attachment with that
origin, carrying `<cross-session-message from="..." from-name="...">`. It needs a reply when its body holds a
question mark or hands over the machine ("the machine is yours", "go ahead", "you're next"). A SendMessage
whose `to` is the sender's address answers every earlier message from that sender, whether or not it is
delivered, so a sender whose session has ended is cleared by one reply attempt. A sender is named by its
address and the session id the registry gives that address, never by its name, which is neither unique nor
stable across a restart. The
reply tools themselves (SendMessage, ListAgents, ToolSearch) are never denied, so a pending message can
always be answered. An unreadable payload or transcript defers: this gate never blocks a session it cannot
read.
"""

import hashlib
import json
import os
import re
import sys

DENY = "deny"
DEFER = "defer"

REPLY_TOOLS = ("SendMessage", "ListAgents", "ToolSearch")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402

CACHE_DIRECTORY = os.path.join(claude_temp.claude_root(), "peer-message-cache")
HEAD_BYTES = 4096
QUOTE_LIMIT = 300

RE_PEER_MESSAGE = re.compile(
    r'<cross-session-message\s+from="(?P<address>[^"]+)"(?:\s+from-name="(?P<name>[^"]*)")?[^>]*>'
    r'(?P<body>.*?)</cross-session-message>', re.S)
RE_NEEDS_REPLY = re.compile(
    r"\?|\b(?:machine|box|slot)\s+is\s+yours\b|\bgo ahead\b|\byou(?:'re| are) next\b|\byour (?:turn|slot)\b",
    re.I)
RE_CODE_SPAN = re.compile(r"`[^`\n]*`")

PENDING_REASON = (
    "Blocked: a peer session is waiting on you and has had no reply. Answer it with SendMessage before any "
    "other tool call: answer the question directly, and if nothing of yours is running, say so and release "
    "the machine in the same message. A peer that ListAgents no longer lists is gone: one SendMessage "
    "attempt to it still clears its message, whether or not the send is delivered.\n{pending}"
)


def peer_messages(record):
    """(address, name, body) for each cross-session message a peer delivered in this record."""
    attachment = record.get("attachment") if isinstance(record.get("attachment"), dict) else {}
    origin = record.get("origin") or attachment.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != "peer":
        return []
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    texts = [message.get("content"), attachment.get("prompt")]
    found = []
    for text in texts:
        if isinstance(text, str):
            found.extend((m.group("address"), m.group("name") or "", m.group("body").strip())
                         for m in RE_PEER_MESSAGE.finditer(text))
    return found


def reply_targets(record):
    """The `to` of every SendMessage this assistant record makes."""
    if record.get("type") != "assistant":
        return []
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content")
    blocks = content if isinstance(content, list) else []
    return [block["input"]["to"] for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "SendMessage"
            and isinstance(block.get("input"), dict) and isinstance(block["input"].get("to"), str)]


def answers(target, address):
    """True when a SendMessage `to` is the peer's address."""
    return target.strip() == address


def session_ids_by_address(registry_directory=None):
    """{messaging address: session id} from every session registry file this account can read."""
    directory = registry_directory or os.path.join(os.environ.get("USERPROFILE", ""), ".claude", "sessions")
    found = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return found
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, ValueError):
            continue
        socket = entry.get("messagingSocketPath") if isinstance(entry, dict) else None
        if isinstance(socket, str) and entry.get("sessionId"):
            found["uds:" + socket] = entry["sessionId"]
    return found


def apply(state, record):
    """Advance the replay state by one transcript record."""
    for address, name, body in peer_messages(record):
        if RE_NEEDS_REPLY.search(RE_CODE_SPAN.sub("", body)):
            state["pending"][address] = {"name": name, "text": body}
    for target in reply_targets(record):
        for address in [a for a in state["pending"] if answers(target, a)]:
            del state["pending"][address]


def head_hash(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read(HEAD_BYTES)).hexdigest()


def cache_path(transcript_path, cache_directory):
    key = hashlib.sha256(os.path.normcase(os.path.abspath(transcript_path)).encode("utf-8")).hexdigest()
    return os.path.join(cache_directory, key + ".json")


def scan(transcript_path, cache_directory=CACHE_DIRECTORY):
    """The replay state after every complete line of the transcript: each peer still owed a reply."""
    fresh = {"offset": 0, "head": None, "pending": {}}
    cached_file = cache_path(transcript_path, cache_directory)
    try:
        with open(cached_file, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = dict(fresh)
    size = os.path.getsize(transcript_path)
    head = head_hash(transcript_path)
    if state.get("head") != head or state.get("offset", 0) > size:
        state = dict(fresh, head=head, pending={})
    with open(transcript_path, "rb") as handle:
        handle.seek(state["offset"])
        chunk = handle.read()
    complete = chunk[:chunk.rfind(b"\n") + 1] if b"\n" in chunk else b""
    for line in complete.decode("utf-8", errors="replace").splitlines():
        if "cross-session-message" not in line and '"SendMessage"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            apply(state, record)
    state["offset"] += len(complete)
    try:
        os.makedirs(cache_directory, exist_ok=True)
        with open(cached_file, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
    except OSError:
        pass
    return state


def quote(text):
    flat = " ".join(text.split())
    return flat if len(flat) <= QUOTE_LIMIT else flat[:QUOTE_LIMIT] + "..."


def decide(transcript_path, tool_name, cache_directory=CACHE_DIRECTORY):
    """(decision, reason) for a tool call in the session that writes `transcript_path`."""
    if tool_name in REPLY_TOOLS:
        return DEFER, ""
    if not isinstance(transcript_path, str) or not os.path.isfile(transcript_path):
        return DEFER, ""
    try:
        state = scan(transcript_path, cache_directory)
    except OSError:
        return DEFER, ""
    if not state["pending"]:
        return DEFER, ""
    sessions = session_ids_by_address()
    pending = "\n".join('  session {0} (SendMessage to "{1}"): "{2}"'.format(
                            sessions.get(address, "unknown to the registry"), address, quote(entry["text"]))
                        for address, entry in state["pending"].items())
    return DENY, PENDING_REASON.format(pending=pending)


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    decision, reason = decide(payload.get("transcript_path"), payload.get("tool_name"))
    if decision == DEFER:
        return 0
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
