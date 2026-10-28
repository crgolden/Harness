"""Regression harness for invoke_overlap_gate.py. Exits non-zero on any regression.

    python Harness/Hooks/test_overlap_gate.py

In-process against evaluate(), against a fixture worktrees folder holding REAL git repos, because dirty_paths shells
out to real git and a fake would test nothing. No row touches the real worktrees.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import invoke_overlap_gate as gate  # noqa: E402

MINE = "aaaaaaaa-1111-2222-3333-444444444444"
PEER = "bbbbbbbb-5555-6666-7777-888888888888"
OTHER = "cccccccc-9999-0000-1111-222222222222"


def clear_readonly_and_retry(func, path, exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def make_worktree(root, owner, files):
    """A REAL git repo at <root>/<owner>/Tools. `files` maps a repo-relative path to "clean" (committed, unchanged),
    "dirty" (committed, then modified) or "new" (untracked)."""
    path = os.path.join(root, owner, gate.CHECKOUT_NAME)
    os.makedirs(path, exist_ok=True)
    quiet = {"capture_output": True, "text": True, "cwd": path}
    subprocess.run(["git", "init"], **quiet)
    with open(os.path.join(path, ".git", "config"), "a", encoding="utf-8") as handle:
        handle.write("[user]\n\temail = t@t\n\tname = t\n[core]\n\tfsmonitor = false\n")
    for relative, state in files.items():
        target = os.path.join(path, *relative.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if state != "new":
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("committed\n")
    subprocess.run(["git", "add", "."], **quiet)
    subprocess.run(["git", "commit", "-m", "init", "--allow-empty"], **quiet)
    for relative, state in files.items():
        target = os.path.join(path, *relative.split("/"))
        if state == "dirty":
            with open(target, "a", encoding="utf-8") as handle:
                handle.write("uncommitted\n")
        elif state == "new":
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("untracked\n")
    return path


def transcript(root, name, messages):
    """A transcript holding one SendMessage per text in messages."""
    path = os.path.join(root, name + ".jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for number, text in enumerate(messages):
            record = {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "tu-{0}".format(number), "name": "SendMessage",
                 "input": {"to": "crgolden-16", "message": text}}]}}
            handle.write(json.dumps(record) + "\n")
    return path


def main():
    started = time.time()
    failures = 0
    count = 0
    base = tempfile.mkdtemp(prefix="overlap-gate-", dir=claude_temp.scratch_dir())
    root = os.path.join(base, "worktrees")
    try:
        mine = make_worktree(root, MINE, {"Hooks/invoke_stop_gate.py": "clean", "Hooks/quiet.py": "clean",
                                          "README.md": "clean"})
        make_worktree(root, PEER, {"Hooks/invoke_stop_gate.py": "dirty", "Gates/new_gate.ps1": "new",
                                   "README.md": "dirty", "Hooks/quiet.py": "clean", "Health/quiet.ps1": "clean"})
        silent = transcript(base, "silent", [])
        told = transcript(base, "told", ["tools-ce to {0} (crgolden-37): I am editing Hooks/invoke_stop_gate.py".format(
            PEER[:8])])
        elsewhere = transcript(base, "elsewhere", ["tools-ce to {0}: unrelated".format(OTHER[:8])])
        edit = lambda relative: ("Edit", {"file_path": os.path.join(mine, *relative.split("/"))})
        add = ("PowerShell", {"command": 'git -C "C:\\x\\Tools" worktree add -b {0} "C:\\w\\{0}\\Tools" origin/main'.format(
            MINE)})
        cases = [
            ("deny", "must-block: editing a hook a peer worktree holds dirty, unannounced",
             edit("Hooks/invoke_stop_gate.py"), silent),
            ("defer", "control: the same edit after a SendMessage naming the peer's session id",
             edit("Hooks/invoke_stop_gate.py"), told),
            ("deny", "must-block: a SendMessage naming a different session announces nothing",
             edit("Hooks/invoke_stop_gate.py"), elsewhere),
            ("defer", "control: a hook no other worktree has touched",
             edit("Hooks/quiet.py"), silent),
            ("deny", "must-block: a peer-dirty file outside Gates and Hooks, unannounced",
             edit("README.md"), silent),
            ("defer", "control: a file outside Gates and Hooks no other worktree has touched",
             edit("Health/quiet.ps1"), silent),
            ("deny", "must-block: git worktree add while a peer holds a dirty gate or hook",
             add, silent),
            ("defer", "control: the same worktree add once the peer is told",
             add, told),
            ("defer", "control: a shell command that creates no worktree",
             ("PowerShell", {"command": "git -C \"C:\\x\\Tools\" worktree list"}), silent),
            ("defer", "control: reading the peer-dirty hook",
             ("Read", {"file_path": os.path.join(mine, "Hooks", "invoke_stop_gate.py")}), silent),
        ]
        for expected, name, (tool, tool_input), transcript_path in cases:
            decision, reason = gate.evaluate(tool, tool_input, transcript_path, MINE, root=root)
            ok = decision == expected and (decision == gate.DEFER or PEER[:8] in reason)
            print("{0}  {1:<72} {2}".format("PASS" if ok else "FAIL", name, decision))
            failures += 0 if ok else 1
            count += 1
        _, reason = gate.evaluate(*add, silent, MINE, root=root)
        ok = all(path in reason for path in ("Hooks/invoke_stop_gate.py", "Gates/new_gate.ps1", "README.md"))
        ok = ok and "Hooks/quiet.py" not in reason
        print("{0}  {1:<72} {2}".format(
            "PASS" if ok else "FAIL", "the reason names every dirty peer path and no clean one", ok))
        failures += 0 if ok else 1
        count += 1
        decision, _ = gate.evaluate(*edit("Hooks/invoke_stop_gate.py"), silent, PEER, root=root)
        ok = decision == gate.DEFER
        print("{0}  {1:<72} {2}".format(
            "PASS" if ok else "FAIL", "control: a session's own dirty worktree never blocks it", decision))
        failures += 0 if ok else 1
        count += 1
    finally:
        shutil.rmtree(base, onexc=clear_readonly_and_retry)
    print("")
    print("{0} case(s) in {1:.2f}s".format(count, time.time() - started))
    if failures == 0:
        print("All overlap-gate cases passed.")
        return 0
    print("{0} case(s) failed.".format(failures))
    return 1


if __name__ == "__main__":
    sys.exit(main())
