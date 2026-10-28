"""Regression harness for invoke_prompt_gate.py and launch_prompt.py. Exits non-zero on any regression.

    python Harness/Hooks/test_prompt_gate.py

A goal prompt (/goal, or a GOAL: line) from a session not yet registered on the coordination board is held back;
a plain prompt, a peer message and a task notification quoting a GOAL: line pass. Registration counts only for this
session's own row, written without an error, in any turn: the artifact-write consent Claude Code asks for in a turn
the operator's typed message did not start is the Stop gate's separate check (test_stop_gate.py, the consent rows). A session that
started before the rule took effect is not held. The launcher, with the gate missing, holds back only goal prompts.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import claude_temp  # noqa: E402
import invoke_prompt_gate as gate  # noqa: E402

BOARD = "https://claude.ai/artifact/board-fixture"
SESSION = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"
failures = []


def record(name, ok, detail=""):
    print("{0}  {1}{2}".format("PASS" if ok else "FAIL", name, "" if ok else "  got: " + str(detail)))
    if not ok:
        failures.append(name)


def stamp(minutes):
    return (datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes)).isoformat()


def operator(text, minutes=0):
    return {"type": "user", "timestamp": stamp(minutes), "origin": {"kind": "human"}, "promptSource": "typed",
            "message": {"role": "user", "content": text}}


def stop_feedback(minutes=1):
    return {"type": "user", "timestamp": stamp(minutes), "isMeta": True,
            "message": {"role": "user", "content": "Stop hook feedback:\nSTOP GATE: register first."}}


def peer(minutes=1):
    return {"type": "user", "timestamp": stamp(minutes), "isMeta": True, "origin": {"kind": "peer"},
            "promptSource": "system",
            "message": {"role": "user", "content": "Another Claude session sent a message:\n"
                                                   '<cross-session-message from="uds:x">hi</cross-session-message>'}}


def task_notice(minutes=1):
    return {"type": "user", "timestamp": stamp(minutes), "origin": {"kind": "task-notification"},
            "promptSource": "system", "message": {"role": "user", "content": "<task-notification>\n</task-notification>"}}


def peer_mid_turn(minutes=1):
    return {"type": "attachment", "timestamp": stamp(minutes), "attachment": {
        "type": "queued_command", "origin": {"kind": "peer"},
        "prompt": '<cross-session-message from="uds:x">hi</cross-session-message>'}}


def board_write(doc_id=SESSION, error=False, url=BOARD, minutes=2, data=None):
    data = {"sessionId": doc_id} if data is None else data
    use = {"type": "assistant", "timestamp": stamp(minutes), "message": {"content": [
        {"type": "tool_use", "id": "tu-reg", "name": "ArtifactData",
         "input": {"action": "set", "url": url, "collection": "sessions", "doc_id": doc_id, "data": data}}]}}
    result = {"type": "user", "timestamp": stamp(minutes), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu-reg", "content": "Permission denied" if error else "committed",
         "is_error": error}]}}
    return [use, result]


root = tempfile.mkdtemp(prefix="prompt-gate-", dir=claude_temp.scratch_dir())
try:
    config_path = os.path.join(root, "coordination.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump({"boardUrl": BOARD, "sessionsCollection": "sessions"}, handle)

    def transcript(name, records):
        path = os.path.join(root, name + ".jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for item in records:
                handle.write(json.dumps(item) + "\n")
        return path

    effective = datetime(2026, 10, 23, 11, 0, tzinfo=timezone.utc)

    def held(prompt, records=None, name="t", session=SESSION, when=effective):
        path = transcript(name, records) if records is not None else os.path.join(root, "absent-" + name + ".jsonl")
        payload = {"prompt": prompt, "transcript_path": path, "session_id": session}
        return gate.evaluate(payload, effective_at=when, config_path=config_path) is not None

    record("must-block: /goal in a new session with no transcript yet", held("/goal ship it", name="new"))
    record("must-block: a GOAL: line in a new session", held("do this\nGOAL: ship it", name="new2"))
    record("control: a plain prompt passes", not held("register", name="plain"))
    record("a peer message quoting a GOAL: line passes",
           not held('<cross-session-message from="uds:x">\nGOAL: ship it\n</cross-session-message>', name="peer"))
    record("a task notification quoting /goal passes",
           not held("<task-notification>\n/goal ship it\n</task-notification>", name="task"))
    registered = [operator("register")] + [stop_feedback()] + board_write()
    record("a row written after Stop hook feedback registers the session",
           not held("/goal ship it", registered, name="reg-stop"))
    record("a row written after a peer message registers the session",
           not held("/goal ship it", [operator("register"), peer()] + board_write(), name="reg-peer"))
    record("a row written after a task notification registers the session",
           not held("/goal ship it", [operator("register"), task_notice()] + board_write(), name="reg-task"))
    record("a row written after a peer message that arrived mid-turn registers the session",
           not held("/goal ship it", [operator("register"), peer_mid_turn()] + board_write(), name="mid-turn"))
    record("a row written in the operator's own typed turn registers the session",
           not held("/goal ship it", [operator("register")] + board_write(), name="typed-only"))
    record("must-block: a typed turn with no row write",
           held("/goal ship it", [operator("register"), stop_feedback()], name="no-write"))
    record("must-block: a row write that came back with an error",
           held("/goal ship it", [operator("register"), stop_feedback()] + board_write(error=True), name="errored"))
    record("must-block: another session's row",
           held("/goal ship it", [operator("register"), stop_feedback()] + board_write(doc_id=OTHER), name="other"))
    record("must-block: a row with no sessionId",
           held("/goal ship it", [operator("register"), stop_feedback()] + board_write(data={"machine": "HOST"}),
                name="no-session-id"))
    record("must-block: a row whose sessionId is another session's",
           held("/goal ship it", [operator("register"), stop_feedback()]
                + board_write(data={"sessionId": OTHER}), name="other-session-id"))
    record("must-pass: a row carrying its sessionId and no name registers, since a name is never asked for",
           not held("/goal ship it", [operator("register"), stop_feedback()] + board_write(data={"sessionId": SESSION}),
                    name="no-name"))
    record("must-block: a row on some other artifact",
           held("/goal ship it", [operator("register"), stop_feedback()] + board_write(url=BOARD + "-not"),
                name="wrong-board"))
    record("a row written in a later typed turn registers the session",
           not held("/goal ship it", [operator("register"), stop_feedback(), operator("again", 3)]
                    + board_write(minutes=4), name="typed-again"))
    sdk_operator = dict(operator("again", 3), promptSource="sdk")
    record("a row written in a Remote Control (sdk) turn registers the session",
           not held("/goal ship it", [operator("register"), stop_feedback(), sdk_operator] + board_write(minutes=4),
                    name="sdk-typed"))

    dated_config = os.path.join(root, "dated.json")
    with open(dated_config, "w", encoding="utf-8") as handle:
        json.dump({"boardUrl": BOARD, "sessionsCollection": "sessions", "effectiveAt": "2026-10-23T11:00:00Z"}, handle)
    record("the cutoff is coordination.json's effectiveAt",
           gate.rule_effective_at(dated_config) == effective, gate.rule_effective_at(dated_config))
    gate_copy = os.path.join(root, "invoke_prompt_gate.py")
    shutil.copy(os.path.join(HERE, "invoke_prompt_gate.py"), gate_copy)
    os.utime(gate_copy, None)
    running = transcript("running", [operator("hello", 0)])
    record("must-block: touching the gate file after a session started never exempts it",
           gate.evaluate({"prompt": "/goal ship it", "transcript_path": running, "session_id": SESSION},
                         config_path=dated_config) is not None)

    record("a session that started before the rule took effect is not held",
           not held("/goal ship it", [operator("hello", -120)], name="old"))
    record("control: the same session started after the rule took effect is held",
           held("/goal ship it", [operator("hello", 0)], name="newer"))

    launcher_dir = os.path.join(root, "launcher")
    os.makedirs(launcher_dir)
    shutil.copy(os.path.join(HERE, "launch_prompt.py"), launcher_dir)

    def launch(prompt):
        done = subprocess.run([sys.executable, os.path.join(launcher_dir, "launch_prompt.py")],
                              input=json.dumps({"prompt": prompt}), capture_output=True, text=True, timeout=60)
        return done.stdout

    record("launcher with the gate and stop_tokens missing holds back /goal",
           '"decision": "block"' in launch("/goal ship it"))
    record("launcher with the gate and stop_tokens missing holds back a bold GOAL: line",
           '"decision": "block"' in launch("do this\n**GOAL:** ship it"))
    record("control: launcher with the gate missing lets a plain prompt through", launch("register").strip() == "")
    shutil.copy(os.path.join(HERE, "stop_tokens.py"), launcher_dir)
    record("launcher with stop_tokens beside it holds back a bold GOAL: line",
           '"decision": "block"' in launch("do this\n**GOAL:** ship it"))
    record("control: launcher with stop_tokens beside it lets a GOAL: with no payload through",
           launch("GOAL:").strip() == "")

    empty_profile = os.path.join(root, "empty-profile")
    os.makedirs(empty_profile)

    def bootstrap(prompt):
        environ = dict(os.environ, USERPROFILE=empty_profile)
        environ.pop("CLAUDE_TOOLS_ROOT", None)
        done = subprocess.run([sys.executable, "-c", gate.PROMPT_LAUNCHER], input=json.dumps({"prompt": prompt}),
                              capture_output=True, text=True, timeout=60, env=environ)
        return done.returncode, done.stdout

    code, out = bootstrap("/goal ship it")
    record("bootstrap with no launcher in the profile holds back /goal", code == 0 and '"decision":"block"' in out,
           (code, out))
    code, out = bootstrap("do this\n**GOAL:** ship it")
    record("bootstrap with no launcher in the profile holds back a bold GOAL: line",
           code == 0 and '"decision":"block"' in out, (code, out))
    code, out = bootstrap("register")
    record("control: bootstrap with no launcher in the profile lets a plain prompt through",
           code == 0 and out.strip() == "", (code, out))
    record("bootstrap names the launcher under USERPROFILE, never a fixed profile",
           "os.environ.get('USERPROFILE'" in gate.PROMPT_LAUNCHER and "C:\\Users" not in gate.PROMPT_LAUNCHER,
           gate.PROMPT_LAUNCHER)

    settings_path = os.path.join(os.environ.get("USERPROFILE", ""), ".claude", "settings.json")
    try:
        with open(settings_path, "r", encoding="utf-8") as handle:
            registered_hooks = (json.load(handle).get("hooks") or {}).get("UserPromptSubmit")
    except (OSError, ValueError):
        registered_hooks = None
    if registered_hooks is None:
        print("SKIPPED  bootstrap-is-what-settings-registers: settings.json registers no UserPromptSubmit hook yet")
    else:
        args = [h.get("args") for entry in registered_hooks for h in entry.get("hooks", [])]
        if args and all("launch_prompt.py" in " ".join(a or [])
                        and "os.environ.get('CLAUDE_TOOLS_ROOT')" not in " ".join(a or [])
                        for a in args):
            print("SKIPPED  bootstrap-is-what-settings-registers: settings.json registers the previous launcher, which "
                  "names the Tools path; the operator switches it, with CLAUDE_TOOLS_ROOT in the env block, only after "
                  "every machine's Tools checkout holds this one")
        else:
            record("bootstrap-is-what-settings-registers", args == [["-c", gate.PROMPT_LAUNCHER]], args)
finally:
    shutil.rmtree(root, ignore_errors=False)

if failures:
    print("\n{0} prompt-gate case(s) failed.".format(len(failures)))
    sys.exit(1)
print("\nAll prompt-gate cases passed.")
