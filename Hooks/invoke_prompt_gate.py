"""UserPromptSubmit gate: a session takes no goal until it has registered on the coordination board.

A prompt that sets a goal (/goal, or a GOAL: line) is held back while the session's transcript shows no
registration (coordination.registered). Every other prompt passes, so the operator can always talk to the session,
and a session that started before coordination.json's effectiveAt is not held to it. A held prompt leaves no user
record in the transcript, so it neither pins a goal nor counts toward the goal gate's opening messages.

Verified by Harness/Hooks/test_prompt_gate.py.
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coordination  # noqa: E402

PROMPT_LAUNCHER = r"""import json,os,runpy,sys
p=os.path.join(os.environ.get('USERPROFILE',''),os.environ.get('CLAUDE_TOOLS_ROOT') or '?','Hooks','launch_prompt.py')
try:runpy.run_path(p,run_name='__main__')
except OSError:
 t=str(json.loads(sys.stdin.read() or '{}').get('prompt') or '')
 if t.lstrip().startswith('/goal') or any(l.lstrip().lstrip('*').startswith('GOAL:') for l in t.splitlines()):sys.stdout.write('{"decision":"block","reason":"The Tools prompt gate launcher is missing, so no goal can be accepted until the Tools checkout is pulled on main. Other messages still reach the session."}')
"""

REASON = (
    "This session has not registered on the coordination board, so it takes no goal yet. Send it one plain message "
    "first (any text, such as: register). The session registers while replying, and in the turn its Stop hook "
    "starts next Claude Code asks you once to allow its artifact writes for the session; approve that. Then make "
    "your very next message the /goal: the session takes its goal only from your first two messages."
)


def rule_effective_at(config_path=None):
    """When this rule took effect: coordination.json's fixed effectiveAt, never a file's modification time, which
    every later pull would move forward and so exempt every session running at that moment."""
    return coordination.effective_at(config_path)


def first_timestamp(transcript_path):
    """The time of the session's first timestamped record, or None when there is no such record yet."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                stamp = json.loads(line).get("timestamp")
            except (ValueError, AttributeError):
                continue
            if isinstance(stamp, str):
                try:
                    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    return None
    return None


def started_before(transcript_path, effective_at):
    """True when the session's first timestamped record is older than effective_at. A session with no record yet
    is a new one, so it is not."""
    started = first_timestamp(transcript_path)
    return effective_at is not None and started is not None and started < effective_at


def started_since(transcript_path, effective_at):
    """True when the session's first timestamped record is no older than effective_at: the Stop gate's test, which
    always has records to read."""
    started = first_timestamp(transcript_path)
    return started is not None and (effective_at is None or started >= effective_at)


def evaluate(payload, effective_at=None, config_path=None):
    """None to let the prompt through, else the reason it is held back."""
    if not isinstance(payload, dict) or not coordination.is_goal_prompt(payload.get("prompt")):
        return None
    transcript_path = payload.get("transcript_path")
    if started_before(transcript_path, effective_at if effective_at is not None else rule_effective_at(config_path)):
        return None
    paths = [transcript_path] if isinstance(transcript_path, str) and os.path.isfile(transcript_path) else []
    if coordination.registered(paths, payload.get("session_id"), config_path):
        return None
    return REASON


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    reason = evaluate(payload)
    if reason:
        sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
