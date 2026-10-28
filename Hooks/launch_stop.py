"""What STOP_LAUNCHER (invoke_stop_gate.py) runs for every Stop: stdin is read first, then the stop hook beside
this file runs, and a missing, broken or failing stop hook blocks every stop."""
import io
import json
import os
import runpy
import sys

raw = sys.stdin.buffer.read()
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "invoke_stop_gate.py")
failure = None
try:
    sys.stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    runpy.run_path(path, run_name="__main__")
except SystemExit as stop:
    if stop.code not in (0, None):
        failure = RuntimeError("the stop hook exited with code " + str(stop.code))
    else:
        raise
except BaseException as error:
    failure = error
if failure is not None:
    reason = ("STOP GATE: the stop hook in the Tools checkout could not load (" + type(failure).__name__ + ": "
              + str(failure)[:200] + "), so no stop can be judged and every stop is blocked until the operator puts the "
              "checkout back on main and pulls it. Tell the operator in plain prose; Claude Code ends the turn after its "
              "block limit.")
    sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))
