# GATE-STEP: Session-name gate harness
"""Regression harness for invoke_session_name_gate.py. Exits non-zero on any regression.

    python Harness/Hooks/test_session_name_gate.py

In-process against evaluate(), with a planted session registry holding two live sessions under one name, so the
refusal is proven to name both ids. Must-block rows cover a session name in each judged field; must-pass controls
cover the crgolden names that are not sessions (an app, a folder, the workspace, the npm scope), session ids, and the
tools and actions the gate never judges.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import invoke_session_name_gate as gate  # noqa: E402

PEER = "uds:\\\\.\\pipe\\LOCAL\\cc-msg-0123456789abcdef0123456789abcdef"
FIRST_ID = "11111111-1111-4111-8111-111111111111"
SECOND_ID = "22222222-2222-4222-8222-222222222222"


def plant_registry(root):
    directory = os.path.join(root, "sessions")
    os.makedirs(directory)
    for pid, session_id in (("100", FIRST_ID), ("200", SECOND_ID)):
        with open(os.path.join(directory, pid + ".json"), "w", encoding="utf-8") as handle:
            json.dump({"pid": int(pid), "sessionId": session_id, "name": "crgolden-3c",
                       "pidDomain": "win32:fixture-host"}, handle)
    with open(os.path.join(directory, "broken.json"), "w", encoding="utf-8") as handle:
        handle.write("{not json")
    return directory


def main():
    root = tempfile.mkdtemp(prefix="session-name-gate-", dir=claude_temp.scratch_dir())
    failures = 0
    try:
        registry = plant_registry(root)
        board = "https://claude.ai/artifact/board-fixture"
        rows = [
            ("deny", "must-block: a SendMessage whose text names a session", "SendMessage",
             {"to": PEER, "message": "crgolden-3c, your gate is next", "summary": "next"}),
            ("deny", "must-block: a SendMessage signed with the sender's own name", "SendMessage",
             {"to": PEER, "message": "crgolden-7a: GATE FINISHED", "summary": "finished"}),
            ("deny", "must-block: a SendMessage addressed to a name", "SendMessage",
             {"to": "crgolden-3c", "message": "hello", "summary": "hello"}),
            ("deny", "must-block: a name with the ListAgents ref beside it", "SendMessage",
             {"to": PEER, "message": "for crgolden-3c [70d54b] only", "summary": "x"}),
            ("deny", "must-block: a name only in the summary", "SendMessage",
             {"to": PEER, "message": "ack", "summary": "ack to crgolden-cf"}),
            ("deny", "must-block: a name in an upper-case spelling", "SendMessage",
             {"to": PEER, "message": "CRGOLDEN-3C is done", "summary": "x"}),
            ("deny", "must-block: a board row whose data names a session", "ArtifactData",
             {"action": "set", "url": board, "collection": "handoffs", "doc_id": "x",
              "data": {"from": "crgolden-7a", "summary": "y"}}),
            ("deny", "must-block: a board row whose doc id carries a name", "ArtifactData",
             {"action": "set", "url": board, "collection": "gates", "doc_id": "crgolden-7a-tools-1", "data": {}}),
            ("deny", "must-block: a name nested in a batch write", "ArtifactData",
             {"action": "batch", "url": board, "writes": [{"op": "update", "collection": "claims", "doc_id": "c",
                                                           "data": {"overlaps": ["held by crgolden-76"]}}]}),
            ("deny", "must-block: a str_replace introducing a name", "ArtifactData",
             {"action": "str_replace", "url": board, "collection": "claims", "doc_id": "c", "field": "why",
              "old_str": "x", "new_str": "asked by crgolden-59"}),
            ("deny", "must-block: a push to the operator naming a session", "PushNotification",
             {"message": "q3 is open; crgolden-7a waits on it", "status": "proactive"}),
            ("defer", "control: a push naming a session by id", "PushNotification",
             {"message": "q3 is open; session 2569db39 waits on it", "status": "proactive"}),
            ("deny", "must-block: a poke passing -CallerName", "PowerShell",
             {"command": 'pwsh -File "C:\\Tools\\Coordination\\Send-SessionPoke.ps1" -SessionId '
                         + FIRST_ID + " -CallerName x -CallerSessionId " + SECOND_ID}),
            ("deny", "must-block: a poke naming a session", "PowerShell",
             {"command": 'pwsh -File "C:\\Tools\\Coordination\\Send-SessionPoke.ps1" -SessionId crgolden-3c'}),
            ("defer", "control: a SendMessage by address and session id", "SendMessage",
             {"to": PEER, "message": "2569db39: GATE FINISHED for " + FIRST_ID, "summary": "finished"}),
            ("defer", "control: app, folder, workspace and package names are not session names", "SendMessage",
             {"to": PEER, "message": "crgolden-curator, crgolden-inventory, crgolden-triage, crgolden-gates, "
                                     "crgolden.com, @crgolden/modules, source\\repos\\crgolden\\Tools and "
                                     "crgolden-3cx are no sessions", "summary": "names"}),
            ("defer", "control: a board row keyed and signed by session id", "ArtifactData",
             {"action": "set", "url": board, "collection": "handoffs", "doc_id": "2569db39-x",
              "data": {"from": FIRST_ID, "to": SECOND_ID}}),
            ("defer", "control: an ArtifactData read is never judged", "ArtifactData",
             {"action": "query", "url": board, "collection": "claims", "query": {"session": "crgolden-3c"}}),
            ("defer", "control: a PowerShell command that runs no poke is never judged", "PowerShell",
             {"command": "git -C C:\\repo log --grep crgolden-3c"}),
            ("defer", "control: a poke by session id alone", "PowerShell",
             {"command": 'pwsh -File "C:\\Tools\\Coordination\\Send-SessionPoke.ps1" -SessionId ' + FIRST_ID
                         + " -CallerSessionId " + SECOND_ID}),
            ("defer", "control: a tool the gate does not judge", "Read", {"file_path": "C:\\notes\\crgolden-3c.md"}),
        ]
        for expected, name, tool, tool_input in rows:
            decision, reason = gate.evaluate(tool, tool_input, registry)
            ok = decision == expected
            failures += not ok
            print("{0}  {1}{2}".format("PASS" if ok else "FAIL", name, "" if ok else "  got {0}: {1}".format(
                decision, reason)))
        decision, reason = gate.evaluate("SendMessage", {"to": PEER, "message": "crgolden-3c?", "summary": "q"},
                                         registry)
        named_both = FIRST_ID in reason and SECOND_ID in reason and "fixture-host" in reason
        failures += not named_both
        print("{0}  must-name: a refusal resolves a shared name to every session id that carries it{1}".format(
            "PASS" if named_both else "FAIL", "" if named_both else "  got " + reason))
        decision, reason = gate.evaluate("SendMessage", {"to": PEER, "message": "crgolden-ee", "summary": "q"},
                                         registry)
        unknown = "no live session" in reason
        failures += not unknown
        print("{0}  must-say: a name no registry entry carries is reported as such{1}".format(
            "PASS" if unknown else "FAIL", "" if unknown else "  got " + reason))
    finally:
        shutil.rmtree(root, ignore_errors=False)
    if failures:
        print("{0} case(s) failed.".format(failures))
        return 1
    print("All session-name gate cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
