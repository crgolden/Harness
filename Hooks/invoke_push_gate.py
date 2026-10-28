r"""PreToolUse gate: a PushNotification reaches the operator only when it asks them something new.

A push lands on the operator's phone and desktop, so one that reports a status, an idle cycle or a question already
pushed is noise they must read and dismiss. The Stop gate demands a push only for a parked question no earlier push
named (invoke_stop_gate.unpushed_parked_ids), and MasterSession/RUNBOOK.md asks for exactly that, once per question.

  deny  a PushNotification whose message repeats an earlier push's text (case and spacing ignored);
  deny  one that names no question id (qN) absent from every earlier push and asks no landing or decision of the
        operator (commit, push, merge, approve, answer, decide, choose, sign in, run), such as "no open questions",
        "the gate is running" or "idle until the next wake"; a negated ask ("nothing to commit", "no questions to
        answer") is no ask;
  deny  one naming only question ids an earlier push already named, unless it asks a landing (commit, push,
        merge): approving or answering a question already pushed is done on the page, not re-asked.

Everything else defers. Earlier pushes are read from the session's own transcript through transcript_index. Wiring: PRETOOLUSE-HOOK.md, "The
push gate". Any change must be verified with Harness/Hooks/test_push_gate.py.
"""

import json
import os
import re
import sys

HOOKS = os.path.dirname(os.path.abspath(__file__))
if HOOKS not in sys.path:
    sys.path.insert(0, HOOKS)
import transcript_index  # noqa: E402

DENY = "deny"
DEFER = "defer"
PUSH_NEEDLES = (b'"PushNotification"',)
RE_NEGATED_ASK = re.compile(r"\b(nothing|no(?:\s+\w+){0,3})\s+to\s+(commit|push|merge|approve|answer|decide|choose|"
                            r"sign in|run)\b", re.IGNORECASE)

RE_QUESTION_ID = re.compile(r"(?<![a-z0-9])q(\d+)(?![0-9])", re.IGNORECASE)
RE_OPERATOR_ASK = re.compile(r"\b(commit|push|merge|approve|answer|decide|choose|sign in|run)\b", re.IGNORECASE)
RE_LANDING_ASK = re.compile(r"\b(commit|push|merge)\b", re.IGNORECASE)

REPEAT_REASON = (
    "Blocked: this push repeats an earlier one word for word. A push is for something the operator can act on now, "
    "once; they have already seen this one. End the turn without it.")
NOTHING_REASON = (
    "Blocked: this push asks the operator nothing: it names no newly parked question and no landing or decision only "
    "they can make. A status, a running gate or an idle cycle is not a push (RUNBOOK.md, \"Push every open question "
    "once\"). End the turn without it.")
REPEATED_IDS_REASON = (
    "Blocked: every question this push names ({ids}) was already named by an earlier push, so it re-asks. Push a "
    "question once; the operator answers it on the page. End the turn without it.")


def normalized(text):
    return " ".join(str(text or "").lower().split())


def earlier_pushes(transcript_path, current=None, cache_root=None):
    """The message of every PushNotification this session called before, in order, leaving out the call being judged
    (its tool_use id), which the transcript may already hold. The lines come from the transcript index, so a long
    transcript is read once and then only from where the last call left off."""
    messages = []
    root = cache_root if cache_root is not None else transcript_index.default_cache_root()
    for record in transcript_index.candidate_records(transcript_path, PUSH_NEEDLES, root):
        content = (record.get("message") or {}).get("content")
        for part in content if isinstance(content, list) else []:
            if (isinstance(part, dict) and part.get("type") == "tool_use"
                    and part.get("name") == "PushNotification" and isinstance(part.get("input"), dict)
                    and (current is None or part.get("id") != current)):
                messages.append(str(part["input"].get("message") or ""))
    return messages


def evaluate(tool_name, tool_input, transcript_path=None, tool_use_id=None, cache_root=None):
    """(decision, reason). Split out so the harness and the dispatcher call it in-process. An ask the message
    negates ("nothing to commit", "no questions to answer") is no ask."""
    if tool_name != "PushNotification" or not isinstance(tool_input, dict):
        return DEFER, ""
    raw = str(tool_input.get("message") or "")
    before = earlier_pushes(transcript_path, tool_use_id, cache_root) if transcript_path else []
    if normalized(raw) in {normalized(text) for text in before}:
        return DENY, REPEAT_REASON
    message = RE_NEGATED_ASK.sub(" ", raw)
    named = {match.lower() for match in RE_QUESTION_ID.findall(message)}
    pushed = {match.lower() for text in before for match in RE_QUESTION_ID.findall(text)}
    if named - pushed:
        return DEFER, ""
    if named:
        if RE_LANDING_ASK.search(message):
            return DEFER, ""
        return DENY, REPEATED_IDS_REASON.format(ids=", ".join("q" + i for i in sorted(named, key=int)))
    return (DEFER, "") if RE_OPERATOR_ASK.search(message) else (DENY, NOTHING_REASON)


def main():
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    decision, reason = evaluate(payload.get("tool_name"), payload.get("tool_input"), payload.get("transcript_path"),
                                payload.get("tool_use_id"))
    if decision == DENY:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": DENY,
                                                            "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
