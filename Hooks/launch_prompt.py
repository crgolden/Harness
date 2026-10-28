"""What the UserPromptSubmit launcher in settings.json runs: stdin is read first, then the prompt gate beside this
file runs. A gate that cannot load holds back only a goal prompt (/goal or a GOAL: line), never any other, so the
operator can always reach the session to say the Tools checkout is broken."""
import io
import json
import os
import runpy
import sys

raw = sys.stdin.buffer.read()
here = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(here, "invoke_prompt_gate.py")
failure = None
try:
    sys.stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    runpy.run_path(path, run_name="__main__")
except SystemExit as stop:
    if stop.code not in (0, None):
        failure = RuntimeError("the prompt gate exited with code " + str(stop.code))
except BaseException as error:
    failure = error
if failure is not None:
    try:
        prompt = json.loads(raw.decode("utf-8", errors="replace") or "{}").get("prompt") or ""
    except (ValueError, AttributeError):
        prompt = ""
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import stop_tokens
        pins = bool(stop_tokens.token_line("GOAL").search(prompt))
    except BaseException:
        pins = any(line.lstrip().lstrip("*").startswith("GOAL:") for line in prompt.splitlines())
    if prompt.lstrip().startswith("/goal") or pins:
        reason = ("The prompt gate in the Tools checkout could not load (" + type(failure).__name__ + ": "
                  + str(failure)[:200] + "), so no goal can be accepted until the checkout is put back on main and "
                  "pulled. Other messages still reach the session.")
        sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
