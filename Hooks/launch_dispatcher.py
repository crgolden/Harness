"""What LAUNCHER (invoke_hook_dispatcher.py) runs for every PreToolUse and PostToolUse call: stdin is read first,
then the dispatcher beside this file runs, and a missing, broken or failing dispatcher denies every call but the
reply tools."""
import io
import json
import os
import runpy
import sys

raw = sys.stdin.buffer.read()
here = os.path.dirname(os.path.abspath(__file__))
tools = os.path.dirname(here)
path = os.path.join(here, "invoke_hook_dispatcher.py")
failure = None
try:
    sys.stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    runpy.run_path(path, run_name="__main__")
except SystemExit as stop:
    if stop.code not in (0, None):
        failure = RuntimeError("the dispatcher exited with code " + str(stop.code))
    else:
        raise
except BaseException as error:
    failure = error
if failure is not None:
    try:
        payload = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("hook_event_name") == "PreToolUse" and payload.get("tool_name") not in ("SendMessage", "ListAgents", "ToolSearch"):
        reason = ("Blocked: the hook dispatcher in the Tools checkout could not load (" + type(failure).__name__ + ": "
                  + str(failure)[:300] + "), and a gate that cannot run never lets a call through, so every tool call is "
                  "denied until the operator repairs it. Do not retry and do not work around it: end the turn with a NEEDED: "
                  'line asking the operator to run, outside Claude Code: git -C "' + tools + '" checkout main, then git -C "'
                  + tools + '" pull --ff-only.')
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                            "permissionDecisionReason": reason}}))
    elif payload.get("hook_event_name") == "PostToolUse":
        sys.stdout.write(json.dumps({"systemMessage": "The PostToolUse hook dispatcher could not load ("
                                     + type(failure).__name__ + "), so this edit was not checked."}))
