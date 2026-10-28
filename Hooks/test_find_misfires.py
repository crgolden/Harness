# GATE-STEP: Misfire finder harness
"""Prove find_stop_gate_misfires.py discriminates before trusting a clean run.

A detector that reports zero is indistinguishable from a detector that cannot report anything. These
build a transcript where the gate demonstrably judged the wrong message and require both findings to
fire, then build the honest case from the same file and require silence.
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "find_stop_gate_misfires.py")
sys.path.insert(0, HERE)
import claude_temp  # noqa: E402

failures = 0
passes = 0


def clear_readonly_and_retry(func, path, exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def load_finder():
    spec = importlib.util.spec_from_file_location("find_stop_gate_misfires", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(name, ok, detail):
    global failures, passes
    if ok:
        passes += 1
        print("PASS  {0:<46} {1}".format(name, detail))
    else:
        failures += 1
        print("FAIL  {0:<46} {1}".format(name, detail))


STALE = "A status report with nothing that earns a stop."
EARNED = ("Everything is green.\n\n"
          "DONE: fix the thing you asked for.\n"
          "VERIFIED: the suite printed 79 case(s), all passing.")
TOKENLESS = "Everything is green and the thing you asked for is fixed."


def build(root, final=EARNED):
    """A transcript whose last two assistant messages are a status report then `final`, an earned stop
    unless a case passes another."""
    projects = os.path.join(root, "projects")
    os.makedirs(projects, exist_ok=True)
    name = "sess-under-test.jsonl"
    path = os.path.join(projects, name)

    lines = [
        {"type": "user", "message": {"role": "user", "content": "please do the thing"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "PowerShell", "input": {}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
        {"type": "assistant", "timestamp": "2026-08-26T03:00:00.000Z",
         "message": {"content": [{"type": "text", "text": STALE}]}},
    ]
    with open(path, "wb") as fh:
        for r in lines:
            fh.write((json.dumps(r, separators=(",", ":")) + "\n").encode("utf-8"))
        after_stale = fh.tell()
        fh.write((json.dumps(
            {"type": "assistant", "timestamp": "2026-08-26T03:00:20.000Z",
             "message": {"content": [{"type": "text", "text": final}]}},
            separators=(",", ":")) + "\n").encode("utf-8"))
        after_earned = fh.tell()
    return projects, name, after_stale, after_earned


def facts_line(session, signal, size, ahead, name):
    return "\t".join([
        "2026-08-26T03:00:21.000000+00:00", session, signal,
        "size=" + str(size), "endsnl=True", "badjson=0", "tc=2",
        "newest=assistant/text", "newest_ts=2026-08-26T03:00:20.000Z", "age=0.5s",
        "file=" + name, "ahead=" + ahead,
    ]) + "\n"


def closed_workspace(root, name, done=True):
    """A workspace whose work-product declaration for the transcript `name` has every item done, or one open."""
    workspace = os.path.join(root, "workspace-" + ("closed" if done else "open"))
    products = os.path.join(workspace_temp(workspace), "claude", "work-products")
    os.makedirs(products, exist_ok=True)
    with open(os.path.join(products, os.path.splitext(name)[0] + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"repos": {"Tools": [{"item": "fix the thing", "done": done}]}}, fh)
    return workspace


def workspace_temp(workspace):
    """The temp root a replay against this workspace reads its declarations from (session_home.py)."""
    return os.path.join(workspace, "temp")


def run(facts, projects, workspace):
    proc = subprocess.run([sys.executable, TOOL, "--facts", facts, "--projects", projects, "--workspace", workspace],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=dict(os.environ, CLAUDE_CODE_TMPDIR=workspace_temp(workspace)))
    return proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")


def main():
    root = tempfile.mkdtemp(prefix="misfire-test-", dir=claude_temp.scratch_dir())
    os.environ["CLAUDE_CODE_TMPDIR"] = os.path.join(root, "temp")
    try:
        projects, name, after_stale, after_earned = build(root)
        closed = closed_workspace(root, name)

        # The defect: the gate read the whole file but judged the message before the last one.
        facts = os.path.join(root, "misfire.log")
        with open(facts, "w", encoding="utf-8") as fh:
            fh.write(facts_line("s-misfire", "block:1", after_earned, STALE[:80], name))
        out = run(facts, projects, closed)
        record("flags a block whose replay now allows",
               "MISFIRE" in out and "misfires): 1" in out,
               "replay line present" if "MISFIRE" in out else out.strip()[-90:])
        record("flags the judged text drifting from the file",
               "DRIFT" in out and "drifted       : 1" in out,
               "drift line present" if "DRIFT" in out else out.strip()[-90:])
        out = run(facts, projects, closed_workspace(root, name, done=False))
        record("a DONE: with an open work-product item replays as a block, not a misfire",
               "misfires): 0" in out, "no misfire" if "misfires): 0" in out else out.strip()[-90:])

        # The honest case: same file, same gate, judging the message it really read.
        facts = os.path.join(root, "clean.log")
        with open(facts, "w", encoding="utf-8") as fh:
            fh.write(facts_line("s-clean", "block:1", after_stale, STALE[:80], name))
        out = run(facts, projects, closed)
        record("stays silent on a correct block",
               "MISFIRE" not in out and "DRIFT" not in out and "nothing to report" in out,
               "silent" if "nothing to report" in out else out.strip()[-90:])

        # A closed declaration must not turn every replay into an allow: a finished-looking message with no
        # token still replays as a block.
        tokenless_root = os.path.join(root, "tokenless")
        tokenless, tokenless_name, _, after_tokenless = build(tokenless_root, final=TOKENLESS)
        facts = os.path.join(root, "tokenless.log")
        with open(facts, "w", encoding="utf-8") as fh:
            fh.write(facts_line("s-tokenless", "block:1", after_tokenless, TOKENLESS[:80], tokenless_name))
        out = run(facts, tokenless, closed_workspace(tokenless_root, tokenless_name))
        record("still blocks a finished-looking message with no token",
               "<<< MISFIRE" not in out and "misfires): 0" in out and "drifted       : 0" in out,
               "no misfire" if "misfires): 0" in out else out.strip()[-90:])

        # A truncated or rolled-over transcript must be skipped, never counted as a finding.
        facts = os.path.join(root, "short.log")
        with open(facts, "w", encoding="utf-8") as fh:
            fh.write(facts_line("s-short", "block:1", after_earned * 50, STALE[:80], name))
        out = run(facts, projects, closed)
        record("skips a decision whose transcript is shorter than recorded",
               "skipped 1" in out and "MISFIRE" not in out,
               "skipped" if "skipped 1" in out else out.strip()[-90:])
        record("must-block: a run that checked nothing never prints the all-clear",
               "NOTHING CHECKED" in out and "nothing to report" not in out,
               "flagged" if "NOTHING CHECKED" in out else out.strip()[-90:])

        # The default transcripts folder is the resolved workspace's under the given profile, and a worktree copy
        # of the finder resolves to its main checkout's workspace, never to the worktree's own folder.
        profile = os.path.join(root, "profile-" + uuid.uuid4().hex)
        finder = load_finder()
        folder = finder.default_projects(profile)
        workspace = finder.session_home.workspace_root()
        record("must-block: the default transcripts folder is the resolved workspace's slug under the profile",
               folder == finder.projects_dir(workspace, profile)
               and os.path.dirname(folder) == os.path.join(profile, ".claude", "projects")
               and "worktrees" not in os.path.basename(folder).lower()
               and os.path.isdir(os.path.join(workspace, "Tools", "Hooks")),
               folder)
    finally:
        shutil.rmtree(root, onexc=clear_readonly_and_retry)

    print()
    print("{0} case(s)".format(passes + failures))
    if failures:
        print("{0} case(s) failed.".format(failures))
        return 1
    print("Detector discriminates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
