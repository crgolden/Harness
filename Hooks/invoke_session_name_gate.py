"""PreToolUse gate: a session refers to and messages another session by its session id, never by its name.

  deny  a SendMessage whose to, message or summary names a session by a crgolden-* name; an ArtifactData write
        (set, update, str_replace, batch) whose doc id or data names one; a PushNotification naming one; a
        PowerShell command that runs Send-SessionPoke.ps1 naming one or passing -CallerName.

A session's name (crgolden-7a) is not unique: two live sessions on one machine can carry the same one. And a restart
or reset gives a session a new name, so a name written down goes stale. The session id never changes and is unique,
so audits, worktrees, gate records and declarations key on it. The refusal resolves every name it
refused to the session ids that carry it now, read from the session registry (~/.claude/sessions), so the caller can
say the right id. A name that is not crgolden- plus two hex characters (crgolden-curator, crgolden-gates, the
crgolden workspace) is not a session name and passes.
"""

import glob
import json
import os
import re
import sys

DENY = "deny"
DEFER = "defer"

RE_SESSION_NAME = re.compile(r"(?<![\w.-])crgolden-[0-9a-f]{2}(?!\w)", re.I)
RE_POKE = re.compile(r"Send-SessionPoke(?:\.ps1)?", re.I)
RE_CALLER_NAME = re.compile(r"-CallerName\b", re.I)
WRITE_ACTIONS = ("set", "update", "str_replace", "batch")

REASON = (
    "Blocked: refer to and message another session by its session id, never by its name ({names}). A session name "
    "is not unique (two live sessions can carry the same one) and a restart or reset changes it, so it can reach the "
    "wrong session or none; the session id is unique and permanent, so audits, worktrees and gate records key on "
    "it. Use the id instead, in the message text, the summary and any board row: {resolved}. Sign your own "
    "messages with your session id, and address SendMessage by the peer's address (uds:...) from its message."
)
POKE_REASON = (
    "Blocked: Send-SessionPoke.ps1 identifies the caller by -CallerSessionId alone; drop -CallerName, since a "
    "session name is neither unique nor stable."
)


def registry_names(registry_directory=None):
    """{name: [session id (machine), ...]} from every session registry file this account can read."""
    directory = registry_directory or os.path.join(os.environ.get("USERPROFILE", ""), ".claude", "sessions")
    found = {}
    for path in glob.glob(os.path.join(directory, "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry.get("sessionId"):
            continue
        machine = str(entry.get("pidDomain", "")).split(":")[-1] or "unknown machine"
        found.setdefault(entry["name"].lower(), []).append("{0} ({1})".format(entry["sessionId"], machine))
    return found


def strings(value):
    """Every string inside a JSON-shaped value, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def inspected_text(tool_name, tool_input):
    """The text of a call this gate judges, or None for a call it does not."""
    if tool_name == "SendMessage":
        return "\n".join(s for s in (tool_input.get(k) for k in ("to", "message", "summary")) if isinstance(s, str))
    if tool_name == "ArtifactData":
        if tool_input.get("action") not in WRITE_ACTIONS:
            return None
        return "\n".join(strings({k: tool_input.get(k) for k in ("doc_id", "data", "writes", "old_str", "new_str")}))
    if tool_name == "PushNotification":
        return "\n".join(strings(tool_input))
    if tool_name == "PowerShell":
        command = tool_input.get("command")
        return command if isinstance(command, str) and RE_POKE.search(command) else None
    return None


def evaluate(tool_name, tool_input, registry_directory=None):
    """(decision, reason) for one tool call."""
    if not isinstance(tool_input, dict):
        return DEFER, ""
    text = inspected_text(tool_name, tool_input)
    if text is None:
        return DEFER, ""
    if tool_name == "PowerShell" and RE_CALLER_NAME.search(text):
        return DENY, POKE_REASON
    names = sorted({match.group(0).lower() for match in RE_SESSION_NAME.finditer(text)})
    if not names:
        return DEFER, ""
    registry = registry_names(registry_directory)
    resolved = "; ".join("{0} is {1}".format(name, ", ".join(registry[name]) if name in registry
                                             else "no live session in the registry")
                         for name in names)
    return DENY, REASON.format(names=", ".join(names), resolved=resolved)


def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    decision, reason = evaluate(payload.get("tool_name"), payload.get("tool_input"))
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
