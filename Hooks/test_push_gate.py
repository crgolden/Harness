"""Regression harness for invoke_push_gate.py. Exits non-zero on any regression.

The fixture pushes are the master's own, taken from its transcript: the must-block rows come first, since a gate
proven only by what it lets through is indistinguishable from one that never runs.

    python Harness/Hooks/test_push_gate.py
"""

import json
import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import invoke_push_gate as gate  # noqa: E402

PAGE = "https://claude.ai/artifact/LpfURxeSEvMcZZUSyrvYV4"


def clear_readonly_and_retry(func, path, _):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def transcript(root, name, pushes):
    path = os.path.join(root, name + ".jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for index, message in enumerate(pushes):
            handle.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "toolu_{0}".format(index), "name": "PushNotification",
                 "input": {"message": message, "status": "proactive"}}]}}) + "\n")
    return path


def main():
    root = tempfile.mkdtemp(prefix="push-gate-", dir=claude_temp.scratch_dir())
    index = os.path.join(root, "index")
    failures = 0
    try:
        asked = transcript(root, "asked", ["master q36: may I carry the 2-line Step inputs fix in my A1/C change? "
                                           + PAGE])
        empty = transcript(root, "empty", [])
        rows = [
            ("deny", "must-block: a status push asks nothing",
             "master: no open questions; nothing of mine is running.", empty, gate.NOTHING_REASON),
            ("deny", "must-block: a running gate is not a push",
             "master: no open questions; the Tools gate on A1+C is running.", empty, gate.NOTHING_REASON),
            ("deny", "must-block: an idle cycle is not a push",
             "master cycle: no blocked sessions, no open questions; A1+C landed. Idle until the next wake.", empty,
             gate.NOTHING_REASON),
            ("deny", "must-block: a status negating a landing verb asks nothing",
             "master: A1+C landed, nothing to commit; idle until the next wake.", empty, gate.NOTHING_REASON),
            ("deny", "must-block: a status negating an ask with words between asks nothing",
             "master: no open questions, no changes left to push.", empty, gate.NOTHING_REASON),
            ("deny", "must-block: a question id an earlier push named is a re-ask",
             "master q36 open: may I carry the 2-line Step inputs fix in my A1/C change, so the Tools gate can pass?",
             asked, "already named by an earlier push"),
            ("deny", "must-block: the same text pushed twice, even with an operator verb",
             "Master q36: may I carry the 2-line Step inputs fix in my A1/C change?  " + PAGE, asked,
             gate.REPEAT_REASON),
            ("deny", "must-block: a pushed question re-asked with a decision verb",
             "master q36 still open: approve carrying the fix so I can gate.", asked, "already named by an earlier push"),
            ("defer", "control: a pushed question with a landing ask reaches the operator",
             "master q36 done; please commit and push worktree 51a78bde (message on my page).", asked, ""),
            ("defer", "control: a question id no earlier push named reaches the operator",
             "master q37: may I drop a duplicate item wording? " + PAGE, asked, ""),
            ("defer", "control: a first landing ask reaches the operator",
             "master: A1+C plus the Step inputs fix are GREEN in worktree 51a78bde. Commit card on my page, "
             "ready to commit and push.", asked, ""),
            ("defer", "control: another tool is not judged", None, empty, ""),
        ]
        for expected, name, message, path, needle in rows:
            tool = "PushNotification" if message is not None else "SendMessage"
            decision, reason = gate.evaluate(tool, {"message": message or "hello"}, path, cache_root=index)
            ok = decision == expected and (needle in reason if needle else reason == "")
            print("{0}  {1}".format("PASS" if ok else "FAIL", name))
            if not ok:
                print("        expected={0} got={1} reason={2}".format(expected, decision, reason[:160]))
                failures += 1
        current = "master q38: may I add the nested .npmrc row? " + PAGE
        holding = transcript(root, "holding", ["master q36: may I carry the fix? " + PAGE, current])
        decision, reason = gate.evaluate("PushNotification", {"message": current}, holding, "toolu_1", index)
        ok = decision == "defer"
        print("{0}  control: the call being judged, already in the transcript, is not its own earlier push".format(
            "PASS" if ok else "FAIL"))
        failures += 0 if ok else 1
        decision, _ = gate.evaluate("PushNotification", {"message": current}, holding, "toolu_other", index)
        ok = decision == "deny"
        print("{0}  must-block: the same text under another call id is a repeat".format("PASS" if ok else "FAIL"))
        failures += 0 if ok else 1
    finally:
        shutil.rmtree(root, onexc=clear_readonly_and_retry)
    print("")
    if failures:
        print("{0} push-gate case(s) failed.".format(failures))
        return 1
    print("All push-gate cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
