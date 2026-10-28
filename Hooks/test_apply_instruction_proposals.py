# GATE-STEP: Audit applier harness
"""Prove apply_instruction_proposals.py tells every kind of exported audit-edit row apart and writes nothing.

Each verdict is reached by a row built to reach only it, beside a READY control from the same target, so a checker
that answered READY for everything, or nothing for anything, fails here.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "apply_instruction_proposals.py")
sys.path.insert(0, HERE)
import claude_temp  # noqa: E402

failures = 0
passes = 0


def clear_readonly_and_retry(func, path, exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def record(name, ok, detail):
    global failures, passes
    if ok:
        passes += 1
        print("PASS  {0:<62} {1}".format(name, detail))
    else:
        failures += 1
        print("FAIL  {0:<62} {1}".format(name, detail))


def sha256(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def export(folder, doc_id, row, envelope=True):
    with open(os.path.join(folder, doc_id + ".json"), "w", encoding="utf-8") as handle:
        json.dump({"id": doc_id, "data": row, "version": 1} if envelope else row, handle)


def run(folder):
    result = subprocess.run([sys.executable, TOOL, folder], capture_output=True, text=True)
    verdicts = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isupper():
            verdicts[parts[1]] = parts[0]
    return result, verdicts


def main():
    root = tempfile.mkdtemp(prefix="applier-", dir=claude_temp.scratch_dir())
    try:
        target = os.path.join(root, "MEMORY.md")
        with open(target, "wb") as handle:
            handle.write(b"- one line\r\n- a second line\r\n- a second line\r\n")
        rows = os.path.join(root, "rows")
        os.makedirs(rows)
        good = sha256(target)
        base = {"kind": "audit-edit", "target": target, "sha256": good, "why": "the line is stale"}
        export(rows, "ready", dict(base, edit={"old": "- one line", "new": "- one corrected line"}))
        export(rows, "ready-bare", dict(base, edit={"old": "- one line", "new": "- one corrected line"}), envelope=False)
        export(rows, "append", dict(base, edit={"old": "", "new": "- an added line\n"}))
        export(rows, "stale", dict(base, sha256="0" * 64, edit={"old": "- one line", "new": "x"}))
        export(rows, "absent", dict(base, edit={"old": "- no such line", "new": "x"}))
        export(rows, "twice", dict(base, edit={"old": "- a second line", "new": "x"}))
        export(rows, "missing", dict(base, target=os.path.join(root, "gone.md"), edit={"old": "a", "new": "b"}))
        export(rows, "free-text", dict(base, edit="replace the first line with a corrected one"))
        export(rows, "reply", {"kind": "audit-edit-reply", "status": "claimed"})

        result, verdicts = run(rows)
        record("control: a structured edit on an unchanged target is READY", verdicts.get("ready") == "READY", str(verdicts.get("ready")))
        record("control: a row exported without the envelope is read the same", verdicts.get("ready-bare") == "READY", str(verdicts.get("ready-bare")))
        record("control: an empty old text appends and is READY", verdicts.get("append") == "READY", str(verdicts.get("append")))
        record("must-report: a target whose sha256 differs is STALE", verdicts.get("stale") == "STALE", str(verdicts.get("stale")))
        record("must-report: old text that does not occur is NOT-FOUND", verdicts.get("absent") == "NOT-FOUND", str(verdicts.get("absent")))
        record("must-report: old text that occurs twice is AMBIGUOUS", verdicts.get("twice") == "AMBIGUOUS", str(verdicts.get("twice")))
        record("must-report: a target that does not exist is MISSING", verdicts.get("missing") == "MISSING", str(verdicts.get("missing")))
        record("must-report: a free-text edit is BY-HAND, never READY", verdicts.get("free-text") == "BY-HAND", str(verdicts.get("free-text")))
        record("must-report: a reply row is SKIPPED", verdicts.get("reply") == "SKIPPED", str(verdicts.get("reply")))
        record("the READY diff shows the replacement", "+- one corrected line" in result.stdout and "--- live" in result.stdout, "diff printed")
        record("CRLF in the target does not hide the old text", verdicts.get("ready") == "READY", "matched across \\r\\n")
        record("the checker writes nothing: the target's bytes are unchanged", sha256(target) == good, good[:12])
        record("the checker exits 0 over a folder of rows", result.returncode == 0, "exit {0}".format(result.returncode))

        usage = subprocess.run([sys.executable, TOOL, os.path.join(root, "no-such-folder")], capture_output=True, text=True)
        record("must-report: a folder that does not exist is a usage error", usage.returncode == 2, "exit {0}".format(usage.returncode))
    finally:
        shutil.rmtree(root, onexc=clear_readonly_and_retry)

    print()
    print("{0} case(s)".format(passes + failures))
    if failures:
        print("{0} case(s) failed.".format(failures))
        return 1
    print("Checker discriminates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
