# GATE-STEP: Stop hook harness
# GATE-LISTING: os.listdir(hooks_directory) | Hooks/*
# GATE-LISTING: os.listdir(source) | Hooks/*.py Gates/*.py
# GATE-LISTING: os.listdir(os.path.join(tools, directory)) | Hooks/*.py Hooks/*.txt Gates/*.py
# GATE-LISTING: os.listdir(mixed_root) | none
"""Regression harness for invoke_stop_gate.py. Exits non-zero on any regression.

Runs IN-PROCESS against decide(), which is the whole point. The PowerShell harness spawned a fresh gate
process per case at ~3.7s each, so a full run took about eleven minutes -- and that cost is not
academic. It made every fixture tweak a coffee break, it discouraged the break-testing that is this
gate's entire verification story, and a run was once killed by a ten-minute tool timeout partway
through. Validation being slow is what let two defects sit in a live hook while multiple sessions hit
them.

The few things that genuinely cannot be tested in-process -- that the registered command form actually
runs, and what it costs -- still spawn a real process, deliberately and only a handful of times.

    python Harness/Hooks/test_stop_gate.py
    python Harness/Hooks/test_stop_gate.py --only question

PRETOOLUSE-GATE: git-write-strings-are-test-data

`make_repo` runs `git init` / `git add` / `git commit` against a throwaway directory under the harness's
own temp root, to give the commit-state veto a REAL repo to ask about -- `dirty_paths` shells out to
real git, so a stub would be testing the stub. Nothing here touches a repo in the workspace.
"""

import argparse
import atexit
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import invoke_stop_gate as gate  # noqa: E402
import session_home  # noqa: E402

FENCE = "```"

results = []
skipped = []
selected = 0


def record(name, ok, detail):
    results.append((name, ok, detail))


def skip(name, why):
    """A case whose veto is switched off in the gate. Listed, never counted as a pass: a skipped block
    case says nothing about the detector, and pretending otherwise is the check-that-cannot-fail."""
    skipped.append((name, why))


TRAILER_OFF = "trailer veto disabled (gate.TRAILER_VETO_ENABLED is False)"
COMMIT_BLOCK_OFF = "commit-block veto disabled (gate.COMMIT_BLOCK_VETO_ENABLED is False)"


class Harness:
    def __init__(self, only):
        self.only = only
        self.root = tempfile.mkdtemp(prefix="stopgate-py-", dir=claude_temp.scratch_dir())
        atexit.register(self.cleanup)
        self.n = 0

    def wanted(self, name):
        global selected
        if not self.only:
            return True
        if any(o.lower() in name.lower() for o in self.only):
            selected += 1
            return True
        return False

    def transcript(self, name, records):
        self.n += 1
        path = os.path.join(self.root, "{0}-{1}.jsonl".format(self.n, name))
        with open(path, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        return path

    def state_dir(self, name):
        """A row's Stop gate state folder, named by a short digest of the row name. The gate names its files in it
        after the session, which here is the row name, so a folder named for the row too put the longest rows past
        MAX_PATH under a long temp root, and the gate's pending-ask write failed unseen. A path that would still cross
        it fails the run here, loudly."""
        folder = os.path.join(self.root, "state-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:12])
        longest = os.path.join(folder, name + gate.PENDING_QUESTION_SUFFIX)
        if len(longest) >= 260:
            raise AssertionError("{0} characters, past MAX_PATH: {1}".format(len(longest), longest))
        return folder

    def run(self, name, records, stop_hook_active=False, max_blocks=100, state=None,
            last_message=None, require_parked=False):
        path = self.transcript(name, records)
        return self.run_path(name, path, stop_hook_active, max_blocks, state, last_message, require_parked)

    def run_path(self, name, path, stop_hook_active=False, max_blocks=100, state=None,
                 last_message=None, require_parked=False):
        """Rows judging other rules run with the parking check off, as if the ask had already been parked; the
        parking rows turn it on."""
        if state is None:
            state = os.path.join(self.root, "state-{0}".format(self.n))
        payload = {
            "hook_event_name": "Stop",
            "transcript_path": path,
            "session_id": name,
            "stop_hook_active": stop_hook_active,
        }
        # Omitted rather than set to None when a case does not supply one, so the default fixtures
        # exercise the fallback exactly as an old host would.
        if last_message is not None:
            payload["last_assistant_message"] = last_message
        self.declare_closed_for_done(path, last_message)
        signal, out = gate.decide(payload, path, state, max_blocks, git_timeout=None,
                                  gate_workspace=self.no_gated_repos(),
                                  temp_root=os.path.join(self.root, "temp-claude"),
                                  quarantine_root=self.quarantine_root(), require_parked=require_parked)
        verdict = "block" if out else "allow"
        return verdict, signal, out

    def no_gated_repos(self):
        """An empty workspace for the gate-claim veto, so no case outside its own reads live gate records."""
        path = os.path.join(self.root, "no-gated-repos")
        os.makedirs(path, exist_ok=True)
        return path

    def declare_closed_for_done(self, path, last_message, workspace=None):
        """A DONE: owes a closed work product, so a transcript that claims one gets a declaration with every item
        done, and its case judges what it was written to judge. The rows on the work-product rule itself use
        isolated_stop, which declares exactly what each row needs."""
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        if "DONE:" not in text and "DONE:" not in (last_message or ""):
            return
        directory = session_home.work_products_dir()
        os.makedirs(directory, exist_ok=True)
        name = os.path.splitext(os.path.basename(path))[0] + ".json"
        with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
            json.dump({"repos": {"Tools": [{"item": "the fixture's work", "done": True}]}}, fh)

    def quarantine_root(self):
        """Where this run's ejects record their sessions, never the live quarantine folder."""
        return os.path.join(self.root, "quarantine")

    def cleanup(self):
        """Removes this run's root and its gate records with claude_temp.remove_tree, whose POSIX delete survives a
        scanner still holding a just-written file (a plain rmtree fails "The directory is not empty" and leaves the
        whole root). Registered with atexit when the root is made, so a row that raises or a run that stops early
        still removes it; a second call finds nothing left. Returns the paths still present."""
        left = []
        for records in claude_temp.gate_records_under(self.root):
            left += claude_temp.remove_tree(records)
        return left + claude_temp.remove_tree(self.root)


def user(text, prompt_id="p-1"):
    """An operator's typed message, shaped as Claude Code records one."""
    return {"type": "user", "uuid": "uuid-" + prompt_id, "promptId": prompt_id, "origin": {"kind": "human"},
            "promptSource": "typed", "message": {"role": "user", "content": text}}


def assistant(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def append(path, records):
    """Grow a transcript BETWEEN gate calls, the way Claude Code does between stops.

    A static fixture cannot see the defect this exists for: the platform feeds a block reason back as a
    prose user record, so the file the gate re-reads has grown by one user-shaped record each round.
    """
    with open(path, "a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")


def goal_status(condition, met=False):
    """The record Claude Code writes when the operator runs /goal, and again at every stop its evaluator judges."""
    return {"type": "attachment", "attachment": {"type": "goal_status", "met": met, "condition": condition}}


def tool_use():
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu-1", "name": "PowerShell", "input": {}}]}}


def edit_use(file_path, tool="Edit"):
    """A tool_use that WRITES a file, which is how the commit-state veto learns what was touched."""
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu-e", "name": tool,
         "input": {"file_path": file_path, "old_string": "a", "new_string": "b"}}]}}


PEER_SESSION_ID = "4f1c2a9b-0e1d-4c2b-8a3f-5d6e7f8a9b0c"
PEER_SOCKET = "\\\\.\\pipe\\LOCAL\\cc-msg-peer"
PEER_ADDRESS = "uds:" + PEER_SOCKET


def send_use(to=PEER_ADDRESS):
    """A SendMessage to a peer session's address, which is what lets a QUEUED: stop be woken again."""
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu-s", "name": "SendMessage",
         "input": {"to": to, "message": "Message me when your gate prints its GATE line."}}]}}


def background_launch(root, command, tool_id="tu-g", stale=False):
    """A shell call run in the background, shaped like the real records: the tool_result names the task's
    output file and carries backgroundTaskId. The output file is created under the harness root, and
    backdated past the cadence hook's staleness window when `stale`."""
    task_id = "b" + tool_id.replace("-", "")
    tasks = os.path.join(root, "tasks")
    os.makedirs(tasks, exist_ok=True)
    output_file = os.path.join(tasks, task_id + ".output")
    with open(output_file, "w", encoding="utf-8") as fh:
        fh.write("running\n")
    if stale:
        old = time.time() - gate.cadence_module().BACKGROUND_STALE_SECONDS - 60
        os.utime(output_file, (old, old))
    text = ("Command running in background with ID: {0}. Output is being written to: {1}. "
            "You will be notified when it completes.").format(task_id, output_file)
    return [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tool_id, "name": "PowerShell",
             "input": {"command": command, "run_in_background": True}}]}},
        {"type": "user", "toolUseResult": {"stdout": "", "backgroundTaskId": task_id},
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": tool_id, "content": text}]}},
    ], task_id


def task_notification(task_id):
    return {"type": "queue-operation", "operation": "enqueue",
            "content": "<task-notification>\n<task-id>{0}</task-id>\n<status>completed</status>\n"
                       "</task-notification>".format(task_id)}


def task_stop(task_id):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu-x", "name": "TaskStop", "input": {"task_id": task_id}}]}}


def gate_script(root):
    """A gate script the cadence hook classifies as a gate: its body runs a gate step."""
    path = os.path.join(root, "gate.ps1")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("dotnet test\n")
    return 'pwsh -File "{0}"'.format(path)


def read_use(file_path):
    """A Read carries a file_path too, and must NOT count as touching the file."""
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu-r", "name": "Read", "input": {"file_path": file_path}}]}}


def make_repo(root, name, files):
    """A REAL git repo, because dirty_paths shells out to real git and a fake would test nothing.

    `files` maps a repo-relative path to its state: "clean" (committed, unchanged), "dirty" (committed,
    then modified) or "new" (untracked). Returns the repo path and {relative: absolute}.
    """
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    quiet = {"capture_output": True, "text": True, "cwd": path}
    subprocess.run(["git", "init"], **quiet)
    with open(os.path.join(path, ".git", "config"), "a", encoding="utf-8") as fh:
        fh.write("[user]\n\temail = t@t\n\tname = t\n[core]\n\tfsmonitor = false\n")
    absolute = {}
    for relative, state in files.items():
        target = os.path.join(path, *relative.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        absolute[relative] = target
        if state != "new":
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("committed\n")
    subprocess.run(["git", "add", "."], **quiet)
    subprocess.run(["git", "commit", "-m", "init", "--allow-empty"], **quiet)
    for relative, state in files.items():
        if state == "dirty":
            with open(absolute[relative], "a", encoding="utf-8") as fh:
                fh.write("uncommitted\n")
        elif state == "new":
            with open(absolute[relative], "w", encoding="utf-8") as fh:
                fh.write("untracked\n")
    return path, absolute


def git_in(path, *arguments):
    done = subprocess.run(["git", "-C", path] + list(arguments), capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError("fixture git {0} failed: {1}".format(" ".join(arguments), done.stderr.strip()))
    return done.stdout.strip()


def shell_use(command):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu-sh", "name": "PowerShell", "input": {"command": command}}]}}


def branch_fixture(root, name, commit=True, merge=True, by_this_session=True, form="worktree", branch=None,
                   folder=None, last="NEEDED: your decision on which repo comes next."):
    """A real repo whose origin/main is a local ref, a branch made the way a session makes one, optionally committed
    on and merged, and a transcript in which this session (or not) made it."""
    repo, _ = make_repo(root, "branch-" + name, {"a.txt": "clean"})
    git_in(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    branch = branch or "feat-" + name
    if form == "worktree":
        worktree = os.path.join(root, folder, "Repo") if folder else os.path.join(root, "wt-" + name)
        command = 'git -C "{0}" worktree add -b {1} "{2}" origin/main'.format(repo, branch, worktree)
        git_in(repo, "worktree", "add", "-b", branch, worktree, "origin/main")
        git_in(worktree, "config", "core.fsmonitor", "false")
        work = worktree
    else:
        worktree = None
        command = 'git -C "{0}" checkout -b {1}'.format(repo, branch)
        git_in(repo, "checkout", "-b", branch)
        work = repo
    if commit:
        with open(os.path.join(work, "b.txt"), "w", encoding="utf-8") as fh:
            fh.write("branch work\n")
        git_in(work, "add", "b.txt")
        git_in(work, "commit", "-m", "branch work")
    if form != "worktree":
        git_in(repo, "checkout", "-")
    if merge:
        git_in(repo, "update-ref", "refs/remotes/origin/main", "refs/heads/" + branch)
    records = [user("do the work")]
    if by_this_session:
        records += [shell_use(command), tool_result()]
    records.append(assistant(last))
    return repo, branch, worktree, records


def merged_branch_cases(h):
    """The merged-branch veto against real repos: a branch this session made, merged and still present, blocks the
    stop with its cleanup commands; every other state allows."""
    import merged_branches
    root = os.path.join(h.root, "merged-branch")
    os.makedirs(root, exist_ok=True)

    def verdict(name, **fixture):
        repo, branch, worktree, records = branch_fixture(root, name, **fixture)
        return h.run("merged-branch-" + name, records) + (repo, branch, worktree)

    _, got, out, repo, branch, worktree = verdict("left")
    record("merged-branch-left-in-place-blocks", got == "block:merged-branch:1", got)
    record("merged-branch-reason-names-the-cleanup-commands",
           'worktree remove "{0}"'.format(worktree) in out and "branch -d {0}".format(branch) in out, out[:200])
    record("merged-branch-reason-ends-by-recording-the-landing",
           "--record-landing {0}".format(os.path.basename(os.path.normpath(repo))) in out
           and out.index("branch -d") < out.index("--record-landing"), out[:400])
    _, got, _, _, _, _ = verdict("checkout", form="checkout")
    record("merged-branch-made-by-checkout-b-blocks", got == "block:merged-branch:1", got)
    for name, short, fixture in (("control-unmerged-branch-allows", "unmerged", {"merge": False}),
                                 ("control-branch-with-no-commit-allows", "uncommitted", {"commit": False}),
                                 ("control-branch-another-session-made-allows", "other", {"by_this_session": False})):
        _, got, _, _, _, _ = verdict(short, **fixture)
        record("merged-branch-" + name, got == "allow:blocker-token", got)

    finished = "DONE: migrate all four repos.\nVERIFIED: 812 passed 0 failed."
    own = "merged-branch-own-done"
    _, got, out, _, branch, worktree = verdict("own-done", branch=own, folder=own, last=finished)
    record("merged-branch-own-session-worktree-blocks-a-done",
           got == "block:merged-branch:1" and 'worktree remove "{0}"'.format(worktree) in out, got)
    own = "merged-branch-own-done-and-needed"
    _, got, _, _, _, _ = verdict("own-done-and-needed", branch=own, folder=own,
                                 last=finished + "\nNEEDED: your decision on which repo comes next.")
    record("merged-branch-own-session-worktree-done-plus-needed-still-blocks", got == "block:merged-branch:1", got)
    uuid = "dddbc42f-11ce-42c4-8ead-b6e1ae7d44dc"
    record("merged-branch-a-real-session-id-survives-the-state-file-name", gate.safe_session_id(uuid, None) == uuid,
           gate.safe_session_id(uuid, None))
    own = "merged-branch-own-kept"
    _, got, _, _, _, _ = verdict("own-kept", branch=own, folder=own)
    record("merged-branch-own-session-worktree-is-kept-mid-session", got == "allow:blocker-token", got)
    _, got, _, _, _, _ = verdict("own-elsewhere", branch="merged-branch-own-elsewhere")
    record("merged-branch-control-session-named-branch-outside-its-folder-blocks", got == "block:merged-branch:1", got)
    other = "merged-branch-another-session"
    _, got, _, _, _, _ = verdict("own-other", branch=other, folder=other)
    record("merged-branch-control-another-session-ids-worktree-blocks", got == "block:merged-branch:1", got)
    _, got, _, _, _, _ = verdict("own-done-control", last=finished)
    record("merged-branch-control-feature-branch-blocks-a-done", got == "block:merged-branch:1", got)

    repo, branch, worktree, records = branch_fixture(root, "dirty")
    with open(os.path.join(worktree, "b.txt"), "a", encoding="utf-8") as fh:
        fh.write("more work\n")
    _, got, _ = h.run("merged-branch-dirty", records)
    record("merged-branch-control-merged-branch-with-uncommitted-work-allows", got == "allow:blocker-token", got)

    # With no goal pinned the goal gate refuses the cleanup commands themselves, so an ask for the goal stands; the
    # same branch left behind under a pinned goal still blocks, since the cleanup can then run.
    goal_ask = "NEEDED: you to run /goal, since the goal gate refuses every tool, the cleanup included."
    for name, pinned, expect in (("no-goal-an-ask-for-a-goal-stands-past-a-merged-branch", [], "allow:blocker-token"),
                                 ("control-a-pinned-goal-keeps-the-cleanup-owed",
                                  [goal_status("run /goal cleanup")], "block:merged-branch:1")):
        repo, branch, worktree, records = branch_fixture(root, name[:12] + str(len(pinned)))
        records = records[:1] + pinned + records[1:-1] + [assistant(goal_ask + " Goal: run /goal cleanup.")]
        _, got, _ = h.run("merged-branch-" + name, records)
        record("merged-branch-" + name, got == expect, got)

    repo, branch, worktree, records = branch_fixture(root, "cleaned")
    git_in(repo, "merge", "--ff-only", "origin/main")
    git_in(repo, "worktree", "remove", worktree)
    git_in(repo, "branch", "-d", branch)
    _, got, _ = h.run("merged-branch-cleaned", records)
    record("merged-branch-a-cleaned-up-branch-allows", got == "allow:blocker-token", got)

    for name, command, expected in (
            ("worktree-add-with-a-quoted-path",
             'git -C "C:\\work space\\Tools" worktree add -b fix "C:\\work space\\wt\\fix\\Tools" origin/main',
             ("C:\\work space\\Tools", "fix", "C:\\work space\\wt\\fix\\Tools")),
            ("worktree-add-with-a-capital-b-flag", "git -C repo worktree add -B fix wt main", ("repo", "fix", "wt")),
            ("switch-c", "git -C repo switch -c fix", ("repo", "fix", None)),
            ("control-a-worktree-add-with-no-new-branch", "git -C repo worktree add wt fix", None),
            ("control-a-plain-checkout", "git -C repo checkout main", None),
            ("control-git-with-no-repo-argument", "git worktree add -b fix wt main", None)):
        got = merged_branches.branch_made(command)
        record("merged-branch-parses-" + name, got == expected, repr(got))


RE_HAND_ROLLED_TOKEN = re.compile(
    r"""(?<![\w'])r["'][^"']*(?:GOAL|NEEDED|REFUSING|QUEUED|WAITING|IDLE|DONE|VERIFIED|goal)\)?:""")


def hand_rolled_token_patterns(hooks_directory):
    """file:line for every raw-string pattern naming a token in a hook outside stop_tokens.py, which is where every
    token's grammar is built."""
    found = []
    for name in sorted(os.listdir(hooks_directory)):
        if not name.endswith(".py") or name.startswith("test_") or name == "stop_tokens.py":
            continue
        with open(os.path.join(hooks_directory, name), encoding="utf-8") as fh:
            found += ["{0}:{1}".format(name, number) for number, line in enumerate(fh, 1)
                      if RE_HAND_ROLLED_TOKEN.search(line)]
    return found


def tool_result(text=None):
    blocks = [{"type": "tool_result", "tool_use_id": "tu-1", "content": "ok"}]
    if text:
        blocks.append({"type": "text", "text": text})
    return {"type": "user", "promptId": "p-turn", "message": {"role": "user", "content": blocks}}


# Behavioural cases. `signal` is asserted so a case proves it passed through the detector it was written
# for: three question fixtures once passed their whole lives through the prose blocker path, whose
# alternation contained `which (one )?do you want`, and the check they existed to pin could have been
# deleted unnoticed. That path is gone (2026-09-09), and the signal column is what shows it stays gone.
OPERATOR_RULES = ("do the work. never push without an explicit instruction. the version archive is Syncthing's "
                  "own storage and not ours.")

CASES = [
    ("ends-with-question", "allow", "allow:question-mark",
     ["I found two options and both compile. Which ordering matches your intent?"]),
    ("status-report-no-question", "block", None,
     ["Tests are green, the gate is clean, and the docs are updated."]),
    ("announces-next-step-without-taking-it", "block", None,
     ["Next I'll port the remaining three workers."]),
    # THE PROSE EXITS ARE RETIRED. Each phrase below earned a stop until 2026-09-09; each was also
    # observed buying a stop for a message that quoted it, discussed the gate, or used it as an aside
    # under a promise to report back. The token twin of each case shows the path that replaced it.
    ("a-prose-blocker-phrase-earns-nothing", "block", None,
     ["The batch is blocked on your decision about the schema."]),
    ("the-same-blocker-as-a-needed-line-allows", "allow", "allow:blocker-token",
     ["The batch cannot run yet.\nNEEDED: your decision about the schema, which only you can make."]),
    ("standing-in-the-way-in-prose-earns-nothing", "block", None,
     ["The production credential is standing in the way and I have no way to obtain it myself."]),
    ("your-call-in-prose-earns-nothing", "block", None,
     ["Two options, and it's your call: keep the caveats or re-baseline the phrase list."]),
    # The live case that retired the path: a genuine ask beside a promise to report back. The prose
    # blocker was tested ahead of the announce veto, so the promise could never disqualify it.
    ("needs-your-decision-under-a-promise-to-report-earns-nothing", "block", None,
     ["The re-run is in progress. I'll report the outcome when it lands.\n\n"
      "The catalog is still synthetic litter. That one genuinely needs your decision."]),
    ("quoting-a-blocker-phrase-earns-nothing", "block", None,
     ["That message was allowed on the phrase \"needs your decision\", which sits ahead of the "
      "announce check. The fix is ordering, not vocabulary."]),
    ("blocker-declared-with-the-taught-token", "allow", "allow:blocker-token",
     ["Partly done.\nNEEDED: the production API key, which only you can supply."]),
    # An agent ended a turn on "NEEDED: nothing from you" while announcing its own next steps, and
    # earned the stop. The token asserts the operator is required; this asserted they are not, and
    # the announce-then-stop disqualifier never ran because the token is tested before it. The three
    # cases below the first are the over-narrowing guard: a narrowing cannot be proved by the thing
    # it removes, only by what it must leave alone.
    ("needed-declaring-nothing-is-needed-earns-nothing", "block", None,
     ["Progress is compounding across all three agents.\n\n"
      "NEEDED: nothing from you - the agents are running unattended and I'll keep auditing.\n\n"
      "Next I'll verify the large repo docs against the code."]),
    ("needed-none-earns-nothing", "block", None,
     ["The sweep is progressing.\n\nNEEDED: none at this point."]),
    ("needed-naming-a-thing-still-allows", "allow", "allow:blocker-token",
     ["Staged and gated.\n\nNEEDED: your go-ahead to run the backfill against production, "
      "which is admin-only and mutates the catalog."]),
    # Documented and deliberate (STOP-HOOK.md, prose-exit retirement): the token is tested ahead of the
    # announce veto, so a NEEDED: line beside a promise to report still allows. A declaration is not an
    # accident; this case exists so moving the veto is a decision that fails a test, not a drift.
    ("a-needed-line-beside-a-promise-to-report-still-allows", "allow", "allow:blocker-token",
     ["The re-run is in progress. I'll report the outcome when it lands.\n\n"
      "NEEDED: your decision on whether catalog rows may be deleted by the sweep."]),
    ("a-lowercase-needed-line-earns-nothing", "block", None,
     ["needed: your decision on whether catalog rows may be deleted by the sweep."]),
    ("one-real-blocker-among-negated-lines-still-allows", "allow", "allow:blocker-token",
     ["Two threads here.\n\nNEEDED: nothing from you on the docs pass.\n"
      "NEEDED: your credentials in the headed browser, which only you can supply."]),
    # "Nothing is waiting on you" once earned a stop while asserting the opposite; with the prose path
    # gone a denied phrase and an asserted one are the same non-event, and only the token is read.
    ("a-denied-blocker-phrase-earns-nothing", "block", None,
     ["All four repos are in sync and the walkers are green.\nNothing is waiting on you."]),
    ("an-asserted-blocker-phrase-earns-nothing-either", "block", None,
     ["I cannot finish the backfill.\nThis is blocked on your decision about the schema."]),
    ("incidental-obstacle-does-not-bypass-the-completion-contract", "block", None,
     ["DONE: ported the three clients.\nThe sweep is blocked on the migration."]),
    ("needed-token-inside-a-code-fence-does-not-count", "block", None,
     ["The doc says:\n\n{0} text\nNEEDED: an API key\n{0}".format(FENCE)]),
    ("refusing-token-inside-a-code-fence-does-not-count", "block", None,
     ["The doc says:\n\n{0} text\nREFUSING: to push\n{0}".format(FENCE)]),
    # A refusal in prose, even leading the message, is no longer read: "I'm not going to keep polling
    # it" bought a stop for a message whose substance was waiting on a build and promising to report.
    ("a-prose-refusal-earns-nothing", "block", None,
     ["I will not push this without an explicit instruction from you. I staged it instead."]),
    ("refusal-declared-with-the-taught-token", "allow", "allow:refusal-token",
     ["REFUSING: to push, since \"never push without an explicit instruction\".\nHere is the alternative."]),
    ("a-refusing-line-anywhere-in-the-message-allows", "allow", "allow:refusal-token",
     ["The sweep is finished and every gate is green.\n\n"
      "REFUSING: to edit the version archive: \"the version archive is Syncthing's own storage\"."]),
    ("a-refusal-quoting-nothing-earns-nothing", "block", "block:refusal-unquoted:1",
     ["REFUSING: that would push without an explicit instruction.\nHere is the alternative."]),
    # Session 590c41f6 declined nothing the operator asked: it quoted the card's re-ask rule to buy the stop the
    # rule had just refused it.
    ("a-refusal-resting-on-the-stop-gate-card-earns-nothing", "block", "block:refusal-unquoted:1",
     ["REFUSING: to ask q3 a third time, because stop-gate-rules.txt says \"A question earns one stop per operator"
      " message: asking it again before they write is blocked\"."]),
    ("a-refusal-quoting-words-the-operator-never-wrote-earns-nothing", "block", "block:refusal-unquoted:1",
     ["REFUSING: to resume, since \"you have paused my work for the audit\"."]),
    # Session 93185fd1 refused nothing the operator asked: four words of a written rule bought the stop. A rule the
    # operator did not invoke declines nothing they asked, so only their own words count.
    ("a-refusal-quoting-only-a-written-rule-earns-nothing", "block", "block:refusal-unquoted:1",
     ["REFUSING: to push: \"Never commit or push unless explicitly asked\"."]),
    ("an-ask-claiming-what-the-operator-did-without-a-quote-earns-nothing", "block", "block:operator-claim:1",
     ["NEEDED: your direction on how to proceed, since you stopped my tool calls."]),
    ("an-ask-quoting-words-the-operator-never-wrote-earns-nothing", "block", "block:operator-claim:1",
     ["NEEDED: your direction, since you paused me: \"stop all work for the audit\"."]),
    ("an-ask-quoting-the-operator-message-it-rests-on-allows", "allow", "allow:blocker-token",
     ["NEEDED: the archive path, since you said \"the version archive is Syncthing's own storage\"."]),
    ("a-question-claiming-what-the-operator-asked-without-a-quote-earns-nothing", "block", "block:operator-claim:1",
     ["The copy is built.\n\nAs you asked, should I deploy it to the server now?"]),
    ("an-ask-asserting-the-operators-instruction-without-a-quote-earns-nothing", "block", "block:operator-claim:1",
     ["NEEDED: the next step, since your instruction was to stop all work."]),
    ("an-ask-asserting-the-operators-direction-without-a-quote-earns-nothing", "block", "block:operator-claim:1",
     ["NEEDED: the release, since your direction was to hold everything."]),
    ("control-an-ask-for-the-operators-decision-is-no-claim", "allow", "allow:blocker-token",
     ["NEEDED: your decision on the deploy root."]),
    ("control-an-ask-making-no-claim-about-the-operator-needs-no-quote", "allow", "allow:blocker-token",
     ["NEEDED: the release name, which only the operator chooses."]),
    ("refusal-buried-in-a-report-does-not-count", "block", None,
     ["The sweep is finished and every gate is green across all four repos. The tables below list "
      "each file, the rule it tripped, the fix applied, and the run that proved it, together with "
      "the coverage delta and the reviewer who signed each one off after the second pass landed. "
      "Nothing outstanding remains on any of them. One note on the version archive, which "
      "I won't edit because it is Syncthing's own storage and not ours to touch."]),
    ("question-mid-message-only", "block", None,
     ["Why was the cache empty? Because the placeholder masked it. I have now fixed that."]),
    ("uses-last-prose-turn-not-first", "block", None,
     ["Should I start with the migration?", "Migration written, tests green, gate clean."]),
    ("trailing-question-on-an-announcement-is-not-an-ask", "block", None,
     ["Proceeding now with the remaining surfaces. Sound good?"]),
    # The live misfire that scoped the announce veto to the question line: a genuine question, with an
    # aside about waiting on a peer two paragraphs above it, was blocked on the aside's "I'll start".
    ("an-announcement-above-the-question-does-not-cancel-it", "allow", "allow:question-mark",
     ["Annotations are the documented fit for members read by the serializer.\n\n"
      "Separately, I gave my gate slot to a peer, and I'll start the Functions gate once it reports.\n\n"
      "Do you count the annotation as allowed, or as suppression?"]),
    ("quoting-an-announce-phrase-in-an-analysis-does-not-cancel-the-question", "allow",
     "allow:question-mark",
     ["The veto matched the phrase `continuing now` quoted in the analysis.\n\n"
      "Should the veto be scoped to the question line?"]),
    ("a-promise-on-the-question-line-still-blocks", "block", None,
     ["Two runs are in flight and I'll report when they land. Should I also trigger the rebuild?"]),
    # The evasion the scoping opens, and its over-narrowing guard beside it.
    ("a-tag-question-under-an-announcement-still-blocks", "block", None,
     ["Proceeding now with the remaining surfaces.\n\nSound good?"]),
    ("a-short-question-with-no-announcement-still-allows", "allow", "allow:question-mark",
     ["Two schemas compile and both migrate cleanly.\n\nWhich one?"]),
    ("missing-critical-information", "allow", "allow:question-mark",
     ["Two schemas are possible here and the choice changes the migration. Which did you intend?"]),
    ("question-followed-by-findings-still-allows", "allow", "allow:question-mark",
     ["Should the proxy target port 4100 or 4200?\n\n- the nav wraps at 1037px\n"
      "- the avatar falls back to initials\n- the footer is fine"]),
    ("proceeding-now", "block", None, ["Proceeding now with the remaining surfaces."]),
    ("continuing-now", "block", None, ["Continuing now with the next repo."]),
    ("ill-report-back-when-it-lands", "block", None, ["I'll report back when the run lands."]),
    ("working-on-it", "block", None, ["Working on it."]),
    ("standing-by", "block", None, ["Standing by for the run to finish."]),
    # A decision request without a question mark was the third prose exit. The same ask ending in "?"
    # is what the block reason teaches, and the pair shows the punctuation is now the whole difference.
    ("tell-me-how-without-a-question-mark-earns-nothing", "block", None,
     ["The migration is ready. Tell me how you want the rows ordered."]),
    ("do-you-want-me-to-without-a-question-mark-earns-nothing", "block", None,
     ["The old rows conflict with the new index. Do you want me to delete them or keep them."]),
    ("do-you-want-me-to-with-a-question-mark-allows", "allow", "allow:question-mark",
     ["The old rows conflict with the new index. Do you want me to delete them or keep them?"]),
    ("please-confirm-without-a-question-mark-earns-nothing", "block", None,
     ["This will drop the staging table. Please confirm the table name is right."]),
    ("should-i-mid-message-only-still-blocks", "block", None,
     ["Should I have cached this? Yes.\nSo I did.\nCache added.\nTests green.\nGate clean."]),
]

GRANT_CASES = [
    ("grant-stopping-is-earned", "allow", "stopping is earned here, wrap up"),
    ("grant-you-can-stop", "allow", "that is enough for now, you can stop"),
    ("grant-supersedes-the-gate", "allow", "this supersedes the stop gate"),
    ("grant-no-need-to-continue", "allow", "no need to continue"),
    ("grant-stand-down", "allow", "stand down"),
    ("no-grant-ordinary-request", "block", "please add the missing tests"),
    ("no-grant-asks-why-stopped", "block", "why did you stop?"),
    ("no-grant-stop-working-on-x", "block", "stop working on the tests and switch to the docs"),
    ("no-grant-thats-all-the-rows", "block", "that's all the rows in that file"),
    ("no-grant-when-youre-done", "block", "let me know when you're done"),
    ("no-grant-stop-here-and-run", "block", "stop here and run the gate"),
    ("no-grant-done-with-x-now-y", "block", "we're done with Curator, now move to Functions"),
    ("no-grant-stop-stopping", "block", "stop stopping"),
]

COMPLETION_CASES = [
    ("complete-with-proof-and-tool-calls", "allow", 1,
     "The sweep is finished.\n\nDONE: migrate all four repos off the deprecated helper.\n"
     "VERIFIED: the full gate across all four, 812 passed 0 failed."),
    ("complete-claim-with-no-tool-calls", "block", 0,
     "DONE: migrate all four repos.\nVERIFIED: 812 passed 0 failed."),
    ("done-without-verified", "block", 1, "DONE: migrate all four repos. It should be fine."),
    ("verified-without-done", "block", 1, "VERIFIED: 812 passed 0 failed."),
    ("prose-completion-claim-still-blocks", "block", 1,
     "I am done with everything you asked and I verified it all. Tests are green."),
    ("sentinels-mid-line-do-not-count", "block", 1,
     "Everything is DONE: the sweep, the tests, the docs. All VERIFIED: nothing outstanding."),
    ("tokens-inside-a-code-fence-do-not-count", "block", 1,
     "Here is what the script printed:\n\n{0}text\nDONE: 4 repos\nVERIFIED: 0 failures\n{0}".format(FENCE)),
    ("lowercase-tokens-earn-nothing", "block", 1,
     "done: migrated the workers.\nverified: gate green, 1089 passed."),
    ("title-case-prose-opening-done-earns-nothing", "block", 1,
     "Done: migrated the workers.\nVerified: gate green, 1089 passed."),
]

# QUEUED: is the wait on something another session started once the work product is finished. Every
# block case below is the allow case with one requirement removed: the peer message in THIS turn, a
# recipient, or the token standing on its own line outside a fence. The payload need not name a gate: QUEUED:
# covers anything another session started.
QUEUED = ("Functions is finished and its commit message is drafted.\n\n"
          "QUEUED: behind session 4f1c2a9b's crgolden.com gate, which I asked to message me when it prints.")
QUEUED_CASES = [
    ("queued-for-a-gate-with-a-peer-message-allows", "allow", "allow:queued-for-gate",
     [user("fix the title prefix"), send_use(), assistant(QUEUED)]),
    ("queued-with-no-peer-message-in-the-turn-blocks", "block", None,
     [user("fix the title prefix"), tool_use(), assistant(QUEUED)]),
    ("a-peer-message-from-an-earlier-turn-does-not-count", "block", None,
     [user("fix the title prefix"), send_use(), assistant("Asked session 4f1c2a9b about its gate."),
      user("any news?", "p-2"), tool_use(), assistant(QUEUED)]),
    ("a-peer-message-with-no-recipient-does-not-count", "block", None,
     [user("fix the title prefix"), send_use(to=""), assistant(QUEUED)]),
    ("queued-on-a-peer-run-the-line-does-not-call-a-gate-allows", "allow", "allow:queued-for-gate",
     [user("fix the title prefix"), send_use(),
      assistant("Functions is finished.\n\nQUEUED: behind session 4f1c2a9b's run on main.")]),
    ("queued-inside-a-code-fence-does-not-count", "block", None,
     [user("fix the title prefix"), send_use(),
      assistant("The token looks like this:\n\n{0}\nQUEUED: behind session 4f1c2a9b's gate\n{0}".format(FENCE))]),
    ("queued-mid-line-does-not-count", "block", None,
     [user("fix the title prefix"), send_use(),
      assistant("Functions is finished and I am QUEUED: behind session 4f1c2a9b's gate.")]),
]


class RealWaitChecks:
    """The cases that exercise QUEUED:/WAITING: for another concern (commit blocks, a pinned goal, token
    parsing) run against the harness's shared root, where no peer gate is live and no work product is
    declared. They get both requirements stubbed as met; inside this context the real cadence functions
    are back, for the cases that pin those requirements themselves."""

    def __enter__(self):
        cadence = gate.cadence_module()
        self.stubbed = (cadence.work_product_problem, cadence.live_peer_gate)
        cadence.work_product_problem, cadence.live_peer_gate = REAL_WAIT_CHECKS
        return self

    def __exit__(self, *exc):
        cadence = gate.cadence_module()
        cadence.work_product_problem, cadence.live_peer_gate = self.stubbed
        return False


REAL_WAIT_CHECKS = (gate.cadence_module().work_product_problem, gate.cadence_module().live_peer_gate)


def hold_exclusively(path):
    """A handle on `path` opened with no sharing, the way Harness/Gates/GateSlot.ps1 holds the gate slot."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(path, 0xC0000000, 0, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "could not hold {0} exclusively".format(path))
    return handle


def release(handle):
    import ctypes
    if handle is not None:
        ctypes.WinDLL("kernel32").CloseHandle(handle)


def stub_wait_checks():
    cadence = gate.cadence_module()
    cadence.work_product_problem = lambda *args, **kwargs: None
    cadence.live_peer_gate = lambda *args, **kwargs: ("a peer's gate", "peer.jsonl")


def isolated_stop(h, name, records, peer_records=None, declaration=None, with_reason=False, pending_asks=None,
                  acceptance=None, acceptance_runner=None, prepare=None, one_workspace=False):
    """A Stop decision in a tree of its own: projects\\<slug>\\ holding this session's transcript and any
    peer's, a workspace carrying this session's work-product declaration, and a temp root. The peer scan
    reads every transcript beside the session's, so a shared directory would leak other cases' gates."""
    base = tempfile.mkdtemp(prefix="iso-", dir=h.root)
    workspace = os.path.join(base, "workspace")
    records = records(workspace) if callable(records) else records
    slug = os.path.join(base, "projects", "C--ws")
    os.makedirs(slug)
    path = os.path.join(slug, name + ".jsonl")
    for target, rows in ((path, records), (os.path.join(slug, "peer-" + name + ".jsonl"), peer_records)):
        if rows is None:
            continue
        with open(target, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    os.makedirs(workspace)
    if declaration is not None:
        directory = session_home.work_products_dir()
        os.makedirs(directory, exist_ok=True)
        data = {"repos": {repo: [dict({"item": entry[0], "done": entry[1]},
                                       **({"pending": entry[2]} if len(entry) > 2 else {}))
                                  for entry in items]
                          for repo, items in declaration.items()}}
        if acceptance is not None:
            data["acceptance"] = acceptance
        with open(os.path.join(directory, name + ".json"), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    state = os.path.join(base, "state")
    if pending_asks is not None:
        os.makedirs(state)
        gate.save_pending_asks(state, name, gate.last_operator_marker(path), pending_asks)
    if prepare is not None:
        prepare(workspace)
    payload = {"hook_event_name": "Stop", "transcript_path": path, "session_id": name,
               "stop_hook_active": False, "last_assistant_message": records[-1]["message"]["content"][0]["text"]}
    signal, out = gate.decide(payload, path, state, 100, workspace=workspace if one_workspace else None, git_timeout=None,
                              gate_workspace=workspace, temp_root=os.path.join(base, "temp"),
                              quarantine_root=os.path.join(base, "quarantine"),
                              require_parked=False, acceptance_runner=acceptance_runner)
    if with_reason:
        return ("block" if out else "allow"), signal, out
    return ("block" if out else "allow"), signal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args()

    h = Harness(args.only)
    started = time.time()
    empty_temp = tempfile.mkdtemp(prefix="empty-temp-", dir=h.root)
    os.environ["CLAUDE_CODE_TMPDIR"] = empty_temp
    os.environ.pop("CRGOLDEN_GATE_SLOT_DIRECTORY", None)
    for marker in ("MASTER_SESSION", "MASTER_WAKE", gate.POKE_VARIABLE):
        os.environ.pop(marker, None)
    board_no_fixture_reaches = os.path.join(h.root, "coordination-unreached.json")
    with open(board_no_fixture_reaches, "w", encoding="utf-8") as fh:
        json.dump({"boardUrl": "https://claude.ai/artifact/board-unreached", "sessionsCollection": "sessions",
                   "effectiveAt": "9999-12-31T00:00:00Z"}, fh)
    gate.coordination.CONFIG_PATH = board_no_fixture_reaches
    stub_wait_checks()

    # QUEUED: and WAITING: against the REAL requirements. QUEUED: needs another session's work live on this
    # machine and this session's declared work product closed; WAITING: needs the declaration closed. Each block
    # case is the allow case with one of those removed. The first block case is the shape the operator
    # caught: a message to a peer and a QUEUED: line while nothing was running.
    with RealWaitChecks():
        closed = {"Functions": [("the whole deliverable", True)]}
        open_item = {"Functions": [("the journeys", True), ("the integration move", False)]}
        queued_text = ("Functions is finished and its commit message is drafted.\n\n"
                       "QUEUED: behind session 4f1c2a9b's crgolden.com gate, which I asked to message me.")
        waiting_text = "Functions is finished.\n\nWAITING: on my Functions gate, running in the background."
        iso_root = tempfile.mkdtemp(prefix="iso-gates-", dir=h.root)
        peer_command = gate_script(iso_root)
        peer_live, peer_task = background_launch(iso_root, peer_command, tool_id="tu-peer")
        own_live, _ = background_launch(iso_root, peer_command, tool_id="tu-own")
        own_turn = [user("fix the title prefix"), send_use(), assistant(queued_text)]
        real_cases = [
            ("queued-behind-a-live-peer-gate-with-a-closed-work-product-allows", "allow",
             own_turn, [user("gate crgolden.com")] + peer_live, closed),
            ("queued-with-a-peer-message-but-no-peer-gate-running-blocks", "block", own_turn, None, closed),
            ("queued-behind-a-peer-gate-that-already-finished-blocks", "block", own_turn,
             [user("gate crgolden.com")] + peer_live + [task_notification(peer_task)], closed),
            ("queued-with-an-open-work-product-item-blocks", "block",
             own_turn, [user("gate crgolden.com")] + peer_live, open_item),
            ("queued-with-no-work-product-declared-blocks", "block",
             own_turn, [user("gate crgolden.com")] + peer_live, None),
            ("waiting-on-a-live-own-gate-with-a-closed-work-product-allows", "allow",
             [user("fix the title prefix")] + own_live + [assistant(waiting_text)], None, closed),
            ("waiting-on-a-live-own-gate-with-an-open-work-product-item-blocks", "block",
             [user("fix the title prefix")] + own_live + [assistant(waiting_text)], None, open_item),
        ]
        for name, expect, records, peer_records, declaration in real_cases:
            if not h.wanted(name):
                continue
            verdict, got_signal = isolated_stop(h, name, records, peer_records, declaration)
            record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))

        # QUEUED: behind the machine's gate slot: its holder may be no session (the alert-triage runner's
        # gate), so no message is owed, and only the slot's file lock counts, never the record beside it.
        slot_text = ("Functions is finished and its commit message is drafted.\n\n"
                     "QUEUED: behind the gate slot, held by the alert-triage runner's gate.")
        slot_turn = [user("fix the title prefix"), tool_use(), assistant(slot_text)]
        slot_cases = [
            ("queued-behind-a-held-gate-slot-with-a-closed-work-product-allows", "allow", True, closed),
            ("queued-behind-a-free-gate-slot-blocks", "block", False, closed),
            ("queued-behind-a-held-gate-slot-with-an-open-work-product-item-blocks", "block", True, open_item),
        ]
        for name, expect, locked, declaration in slot_cases:
            if not h.wanted(name):
                continue
            temp = tempfile.mkdtemp(prefix="temp-", dir=h.root)
            slot = os.path.join(temp, "claude", "crgolden-gates")
            os.makedirs(slot)
            lock = os.path.join(slot, "gate-slot.lock")
            with open(lock, "wb"):
                pass
            with open(os.path.join(slot, "gate-slot.json"), "w", encoding="utf-8") as fh:
                json.dump({"owner": "alert-triage harness", "pid": os.getpid(), "acquiredAt": "harness"}, fh)
            held = hold_exclusively(lock) if locked else None
            os.environ["CLAUDE_CODE_TMPDIR"] = temp
            try:
                verdict, got_signal = isolated_stop(h, name, slot_turn, None, declaration)
            finally:
                os.environ["CLAUDE_CODE_TMPDIR"] = empty_temp
                release(held)
            record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))

        own_slot_turn = [user("fix the title prefix"), tool_use(),
                         assistant("Functions is finished and its commit message is drafted.\n\n"
                                   "WAITING: my own Functions gate, which holds the gate slot.")]
        own_slot_cases = [
            ("waiting-on-own-gate-that-holds-the-slot-allows", "allow", True, True),
            ("waiting-control-a-slot-held-by-another-session-blocks", "block", True, False),
            ("waiting-control-a-free-slot-naming-this-session-blocks", "block", False, True),
        ]
        for name, expect, locked, mine in own_slot_cases:
            if not h.wanted(name):
                continue
            temp = tempfile.mkdtemp(prefix="temp-", dir=h.root)
            slot = os.path.join(temp, "claude", "crgolden-gates")
            os.makedirs(slot)
            lock = os.path.join(slot, "gate-slot.lock")
            with open(lock, "wb"):
                pass
            owner = "gate Functions (harness, session {0})".format(name if mine else "another-session")
            with open(os.path.join(slot, "gate-slot.json"), "w", encoding="utf-8") as fh:
                json.dump({"owner": owner, "pid": os.getpid(), "acquiredAt": "harness"}, fh)
            held = hold_exclusively(lock) if locked else None
            os.environ["CLAUDE_CODE_TMPDIR"] = temp
            try:
                verdict, got_signal = isolated_stop(h, name, own_slot_turn, None, closed)
            finally:
                os.environ["CLAUDE_CODE_TMPDIR"] = empty_temp
                release(held)
            record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))

        # A closed declaration is the session's own claim: QUEUED: and WAITING: also call work_product_closure
        # (closure_problem), telling it whether the line waits on a gate, and refuse on its reason. The cadence
        # surface is stubbed so each row differs in one fact: the closure verdict, or which wait earned the line.
        saved_wiring = (list(gate._CADENCE_MODULE), gate.closure_problem, gate.queued_on_a_messaged_peer,
                        gate.own_work_running, gate.own_gate_holds_slot, gate.own_gate_running)
        seen_waits = []
        seen_blocked = []
        wiring = {"problem": None, "slot": True, "peer": None}

        def stub_closure(cadence, transcript_path, workspace, text, waits_on_gate, blocked=None):
            seen_waits.append(waits_on_gate)
            seen_blocked.append(blocked)
            return wiring["problem"]

        stub = types.SimpleNamespace(
            TEMP_CLAUDE_ROOT=h.root, WORKSPACE=h.root, work_product_problem=lambda *a, **k: None,
            live_peer_gate=lambda *a, **k: None, gate_slot_holder=lambda *a, **k: {"owner": "x"} if wiring["slot"] else None)
        gate._CADENCE_MODULE[:] = [stub]
        gate.closure_problem = stub_closure
        gate.queued_on_a_messaged_peer = lambda *a, **k: wiring["peer"]
        gate.own_work_running = lambda *a, **k: True
        gate.own_gate_holds_slot = lambda *a, **k: True
        gate.own_gate_running = lambda *a, **k: False
        transcript_stub = os.path.join(h.root, "closure-wiring.jsonl")
        try:
            wiring.update(problem="your work product is not closed: fixture", slot=True)
            if h.wanted("closure-queued-behind-a-held-slot-with-an-unnamed-edit-blocks"):
                record("closure-queued-behind-a-held-slot-with-an-unnamed-edit-blocks",
                       not gate.queued_for_a_gate(slot_text, False, transcript_path=transcript_stub)
                       and gate.unaccepted_wait_claim(slot_text, False, transcript_stub)[1].startswith(
                           "your work product is not closed"), "earned")
            wiring.update(problem=None)
            del seen_waits[:]
            if h.wanted("closure-control-the-same-held-slot-with-the-work-product-closed-allows"):
                record("closure-control-the-same-held-slot-with-the-work-product-closed-allows",
                       gate.queued_for_a_gate(slot_text, False, transcript_path=transcript_stub)
                       and seen_waits == [True], seen_waits)
            wiring.update(slot=False, peer="crgolden-07")
            del seen_waits[:]
            del seen_blocked[:]
            if h.wanted("closure-queued-on-a-messaged-peer-is-judged-as-no-gate-wait"):
                record("closure-queued-on-a-messaged-peer-is-judged-as-no-gate-wait",
                       gate.queued_for_a_gate(queued_text, True, transcript_path=transcript_stub,
                                              messaged_to=("crgolden-07",)) and seen_waits == [False], seen_waits)
            if h.wanted("closure-queued-on-a-messaged-peer-hands-closure-a-blocked-check"):
                record("closure-queued-on-a-messaged-peer-hands-closure-a-blocked-check",
                       len(seen_blocked) == 1 and callable(seen_blocked[-1]), seen_blocked)
            del seen_blocked[:]
            wiring.update(slot=True, peer=None)
            gate.queued_for_a_gate(slot_text, False, transcript_path=transcript_stub)
            if h.wanted("closure-control-a-held-slot-hands-closure-no-blocked-check"):
                record("closure-control-a-held-slot-hands-closure-no-blocked-check", seen_blocked == [None],
                       seen_blocked)
            wiring.update(slot=False, peer="crgolden-07")
            wiring.update(problem="your work product is not closed: fixture")
            if h.wanted("closure-waiting-on-own-gate-with-a-done-item-unproven-blocks"):
                record("closure-waiting-on-own-gate-with-a-done-item-unproven-blocks",
                       not gate.waiting_on_own_gate(waiting_text, transcript_stub), "earned")
            wiring.update(problem=None)
            del seen_waits[:]
            if h.wanted("closure-control-waiting-on-own-gate-holding-the-slot-is-a-gate-wait-and-allows"):
                record("closure-control-waiting-on-own-gate-holding-the-slot-is-a-gate-wait-and-allows",
                       gate.waiting_on_own_gate(waiting_text, transcript_stub) and seen_waits == [True], seen_waits)
            import work_product_closure
            saved_done = work_product_closure.done_problem
            try:
                work_product_closure.done_problem = lambda *a, **k: "an edit no item names: fixture"
                got = gate.completion_work_product_problem(transcript_stub)
                if h.wanted("closure-done-with-an-unnamed-edit-is-refused"):
                    record("closure-done-with-an-unnamed-edit-is-refused",
                           got == "closure: an edit no item names: fixture", got)
                work_product_closure.done_problem = lambda *a, **k: None
                got = gate.completion_work_product_problem(transcript_stub)
                if h.wanted("closure-control-done-with-the-work-product-closed-passes"):
                    record("closure-control-done-with-the-work-product-closed-passes", got is None, got)
            finally:
                work_product_closure.done_problem = saved_done
        finally:
            (gate._CADENCE_MODULE[:], gate.closure_problem, gate.queued_on_a_messaged_peer, gate.own_work_running,
             gate.own_gate_holds_slot, gate.own_gate_running) = saved_wiring

        # A QUEUED: line on a messaged peer excuses an ungated checkout only while that peer's own worktree of the repo
        # holds a change origin lacks, read from real git: dirty, or committed and unpushed. A peer whose worktree has
        # landed, a peer no reachable session names, and this session's own checkout excuse nothing.
        peer_root = tempfile.mkdtemp(prefix="peer-worktrees-", dir=h.root)
        quiet = {"check": True, "capture_output": True}
        origin = os.path.join(h.root, "peer-origin.git")
        subprocess.run(["git", "-c", "core.fsmonitor=false", "init", "--bare", "--quiet", origin], **quiet)
        peer_checkout = os.path.join(peer_root, "peer-session", "Tools")
        subprocess.run(["git", "-c", "core.fsmonitor=false", "clone", "--quiet", origin, peer_checkout], **quiet)
        peer_git = ["git", "-C", peer_checkout, "-c", "core.fsmonitor=false", "-c", "user.name=harness",
                    "-c", "user.email=harness@example.invalid"]
        subprocess.run(peer_git + ["config", "core.fsmonitor", "false"], **quiet)
        with open(os.path.join(peer_checkout, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("one\n")
        subprocess.run(peer_git + ["add", "--all"], **quiet)
        subprocess.run(peer_git + ["commit", "--quiet", "--message", "landed"], **quiet)
        subprocess.run(peer_git + ["push", "--quiet", "origin", "HEAD"], **quiet)
        own_checkout = os.path.join(h.root, "own-worktree", "Tools")
        saved_peers = gate.reachable_peers
        gate.reachable_peers = lambda *a, **k: [{"sessionId": "peer-session"}]
        import work_product_closure
        declared = {"repos": {"Tools": [{"item": "fixture", "done": True}]}}
        own_edits = [("Tools", own_checkout, "Hooks/x.py")]

        def ungated(blocked):
            return work_product_closure.ungated_problem(declared, own_edits, h.root, "own-session",
                                                        lambda *a: ("RED",), [], blocked=blocked)

        try:
            blocked = gate.peer_change_blocks("peer-session", transcript_stub, root=peer_root)
            if h.wanted("peer-block-a-peer-worktree-landed-on-origin-excuses-nothing"):
                record("peer-block-a-peer-worktree-landed-on-origin-excuses-nothing",
                       not blocked("Tools", own_checkout) and ungated(blocked) is not None, ungated(blocked))
            with open(os.path.join(peer_checkout, "a.txt"), "w", encoding="utf-8") as fh:
                fh.write("two\n")
            if h.wanted("peer-block-control-a-peer-worktree-with-an-uncommitted-change-excuses-the-checkout"):
                record("peer-block-control-a-peer-worktree-with-an-uncommitted-change-excuses-the-checkout",
                       blocked("Tools", own_checkout) and ungated(blocked) is None, ungated(blocked))
            subprocess.run(peer_git + ["commit", "--quiet", "--all", "--message", "unpushed"], **quiet)
            if h.wanted("peer-block-control-a-peer-commit-not-on-origin-excuses-the-checkout"):
                record("peer-block-control-a-peer-commit-not-on-origin-excuses-the-checkout",
                       blocked("Tools", own_checkout), "not excused")
            if h.wanted("peer-block-the-same-unlanded-change-excuses-nothing-without-a-blocked-check"):
                record("peer-block-the-same-unlanded-change-excuses-nothing-without-a-blocked-check",
                       ungated(None) is not None, ungated(None))
            stranger = gate.peer_change_blocks("stranger-session", transcript_stub, root=peer_root)
            if h.wanted("peer-block-a-peer-no-reachable-session-names-excuses-nothing"):
                record("peer-block-a-peer-no-reachable-session-names-excuses-nothing",
                       not stranger("Tools", own_checkout), "excused")
            if h.wanted("peer-block-the-peer-checkout-itself-is-never-its-own-excuse"):
                record("peer-block-the-peer-checkout-itself-is-never-its-own-excuse",
                       not blocked("Tools", peer_checkout), "excused")
        finally:
            gate.reachable_peers = saved_peers

        # A rejected QUEUED: or WAITING: line names the first requirement it missed, ahead of the generic
        # reason, so a session fixes that one thing instead of re-posting the same line. Each case is a block
        # the requirement cases above already pin; these pin what the block says. The control: a block whose
        # message carries neither line gets the generic reason alone.
        note = "line was not accepted"
        generic = gate.TOKEN_MENU
        reason_cases = [
            ("wait-claim-reason-names-nothing-of-its-own-running",
             [user("fix the title prefix"), tool_use(), assistant(waiting_text)], closed,
             "nothing this session started is still running"),
            ("wait-claim-reason-names-nothing-running-and-the-verdict-command",
             [user("fix the title prefix"), tool_use(), assistant(waiting_text)], closed,
             "read its verdict with python Harness/Gates/gate_status.py <Repo>"),
            ("wait-claim-reason-names-an-open-work-product",
             [user("fix the title prefix")] + own_live + [assistant(waiting_text)], open_item,
             "work-product declaration is not closed (Functions: "),
            ("wait-claim-reason-names-an-open-work-product-and-how-to-close-it",
             [user("fix the title prefix")] + own_live + [assistant(waiting_text)], open_item,
             "set its \"done\" to true first"),
            ("wait-claim-reason-names-no-peer-gate", own_turn, closed, "no peer's gate is running"),
            ("wait-claim-reason-names-no-peer-gate-and-says-to-gate-now", own_turn, closed,
             "run your own gate now in the foreground"),
            ("wait-claim-reason-names-no-message-and-a-free-slot", slot_turn, closed, "sent no SendMessage"),
            ("wait-claim-reason-names-no-message-and-says-to-send-one", slot_turn, closed,
             "SendMessage that session in this turn"),
            ("wait-claim-reason-points-a-wait-on-others-at-queued",
             [user("fix the title prefix"), tool_use(), assistant("Functions is finished.\n\nWAITING: on the CI run.")],
             closed, "waiting on another session's work is QUEUED:"),
        ]
        for name, records, declaration, expected in reason_cases:
            if not h.wanted(name):
                continue
            verdict, got_signal, out = isolated_stop(h, name, records, None, declaration, with_reason=True)
            record(name, verdict == "block" and note in out and expected in out and generic in out
                   and out.index(note) < out.index(generic), "{0} {1}".format(verdict, got_signal))
        # WAITING: is earned by anything this session started that is still running, gate or not: a background
        # task until its completion notice arrives, or a permission matrix it requested until the poll scores
        # the run. Each allow case has a block twin with the one fact removed.
        build_text = "Functions is finished.\n\nWAITING: on my build, running in the background."
        build_live, build_task = background_launch(iso_root, "npm run build --prefix C:\\work\\app", tool_id="tu-build")
        own_task_cases = [
            ("waiting-on-a-live-own-background-task-that-is-not-a-gate-allows", "allow",
             [user("fix the title prefix")] + build_live + [assistant(build_text)]),
            ("waiting-on-an-own-background-task-whose-notice-arrived-blocks", "block",
             [user("fix the title prefix")] + build_live + [task_notification(build_task), assistant(build_text)]),
        ]
        for name, expect, records in own_task_cases:
            if not h.wanted(name):
                continue
            verdict, got_signal = isolated_stop(h, name, records, None, closed)
            record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))

        matrix_text = "Functions is finished.\n\nWAITING: on the permission matrix I requested."

        def matrix_request(state, stamp="20261022062621"):
            request = os.path.join(state, "dispatch", "matrix-{0}.json".format(stamp))
            return request, {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu-1",
                 "content": "requested the permission matrix in {0}; the next poll starts it once the agent is "
                            "idle and finishes it on a later poll.".format(request)}]}}

        matrix_cases = [
            ("waiting-on-an-own-permission-matrix-whose-request-waits-allows", "allow", True, None, False),
            ("waiting-on-an-own-permission-matrix-whose-run-is-open-allows", "allow", False,
             "2026-10-22T06:28:15.6312133+00:00", False),
            ("waiting-on-an-own-permission-matrix-that-finished-blocks", "block", False, None, False),
            ("waiting-control-a-matrix-run-older-than-the-request-blocks", "block", False,
             "2026-10-22T06:20:00.0000000+00:00", False),
            ("waiting-on-a-permission-matrix-another-session-requested-blocks", "block", True, None, True),
        ]
        for name, expect, waiting_request, run_started, peers_request in matrix_cases:
            if not h.wanted(name):
                continue
            state = os.path.join(tempfile.mkdtemp(prefix="matrix-", dir=h.root), "state")
            request, requested = matrix_request(state)
            os.makedirs(os.path.dirname(request))
            if waiting_request:
                with open(request, "w", encoding="utf-8") as fh:
                    json.dump({"Kind": "matrix"}, fh)
            if run_started:
                with open(os.path.join(state, gate.MATRIX_RUN_FILE), "w", encoding="utf-8") as fh:
                    json.dump({"StartedAt": run_started, "OwnHold": True, "AppSha": "harness"}, fh)
            own = [user("run the matrix"), tool_use()] + ([] if peers_request else [requested])
            verdict, got_signal = isolated_stop(h, name, own + [assistant(matrix_text)],
                                                [user("run the matrix"), requested] if peers_request else None, closed)
            record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))

        # QUEUED: is earned by anything another session started: beside a peer's live gate and the gate slot, a
        # peer the line names, that this turn messaged, and whose session the registry shows running here.
        def registry_with(case, peer_pid):
            """A session registry beside the case's projects directory: this session's own entry, whose sessionId
            is the transcript stem isolated_stop names after the case, and the peer's, with its address."""
            def prepare(workspace):
                registry = os.path.join(os.path.dirname(workspace), "sessions")
                os.makedirs(registry)
                for filename, entry in (("own.json", {"pid": os.getpid(), "sessionId": case}),
                                        ("peer.json", {"pid": peer_pid, "sessionId": PEER_SESSION_ID,
                                                       "name": "crgolden-07",
                                                       "messagingSocketPath": PEER_SOCKET})):
                    with open(os.path.join(registry, filename), "w", encoding="utf-8") as fh:
                        json.dump(dict(entry, pidDomain="win32:harness-here"), fh)
            return prepare

        def exited_pid():
            child = subprocess.Popen([sys.executable, "-c", "pass"])
            child.wait()
            return child.pid

        named = ("Functions is finished.\n\nQUEUED: on session " + PEER_SESSION_ID[:8]
                 + "'s Identity fix, which I asked to message me.")
        full_id = ("Functions is finished.\n\nQUEUED: on session " + PEER_SESSION_ID
                   + "'s Identity fix, which I asked to message me.")
        by_name = "Functions is finished.\n\nQUEUED: on crgolden-07's Identity fix, which I asked to message me."
        unnamed = "Functions is finished.\n\nQUEUED: on the Identity fix, which I asked to message me."
        peer_cases = [
            ("queued-on-a-messaged-live-peer-named-in-the-line-allows", "allow", PEER_ADDRESS, named, os.getpid()),
            ("queued-on-a-messaged-live-peer-named-by-its-full-session-id-allows", "allow", PEER_ADDRESS, full_id,
             os.getpid()),
            ("must-block: queued-on-a-messaged-live-peer-named-by-its-session-name-blocks", "block", PEER_ADDRESS,
             by_name, os.getpid()),
            ("queued-on-a-messaged-peer-whose-session-exited-blocks", "block", PEER_ADDRESS, named, exited_pid()),
            ("queued-on-a-live-peer-this-turn-did-not-message-blocks", "block", "uds:\\\\.\\pipe\\LOCAL\\cc-msg-other",
             named, os.getpid()),
            ("queued-on-a-messaged-live-peer-the-line-does-not-name-blocks", "block", PEER_ADDRESS, unnamed,
             os.getpid()),
        ]
        for name, expect, to, text, peer_pid in peer_cases:
            if not h.wanted(name):
                continue
            verdict, got_signal = isolated_stop(h, name, [user("fix the title prefix"), send_use(to), assistant(text)],
                                                None, closed, prepare=registry_with(name, peer_pid))
            record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))

        name = "wait-claim-reason-absent-when-no-claim-line"
        if h.wanted(name):
            verdict, got_signal, out = isolated_stop(
                h, name, [user("fix the title prefix"), tool_use(), assistant("Functions is half done.")], None,
                closed, with_reason=True)
            record(name, verdict == "block" and note not in out and generic in out,
                   "{0} {1}".format(verdict, got_signal))

        # OWN WORK BEFORE A HAND-OFF: a NEEDED: or question stop while a declared item is open is refused unless
        # the stop names that item. Each block case is an allow case with the naming removed or the item reopened.
        handoff = "Functions is waiting on the peer.\n\nNEEDED: your answer to the peer's question about the matrix."
        asked = "Functions is waiting on the peer.\n\nShould the peer let its permission matrix finish first?"
        naming = "Functions is waiting on the operator.\n\nNEEDED: your sign-off on the integration move before it lands."
        refusal = ("Functions is waiting on the peer.\n\n"
                   "REFUSING: to touch it: \"the peer's matrix change is out of this session's scope\".")
        open_item_cases = [
            ("open-item-needed-about-something-else-blocks", "block", handoff, open_item, True),
            ("open-item-question-about-something-else-blocks", "block", asked, open_item, True),
            ("open-item-needed-naming-the-item-allows", "allow", naming, open_item, False),
            ("open-item-needed-with-every-item-done-allows", "allow", handoff, closed, False),
            ("open-item-needed-with-no-declaration-allows", "allow", handoff, None, False),
            ("open-item-refusing-is-not-vetoed", "allow", refusal, open_item, False),
        ]
        for name, expect, text, declaration, lists_item in open_item_cases:
            if not h.wanted(name):
                continue
            verdict, got_signal, out = isolated_stop(
                h, name, [user("fix the title prefix. the peer's matrix change is out of this session's scope."),
                          tool_use(), assistant(text)], None, declaration, with_reason=True)
            shows = "open items you can work yourself" in out and "the integration move" in out
            record(name, verdict == expect and shows == lists_item, "{0} {1}".format(verdict, got_signal))

        operator_asked = {"type": "user", "uuid": "op-q", "origin": {"kind": "human"},
                          "message": {"role": "user", "content": "fix the title prefix"}}
        peer_woke = {"type": "user", "uuid": "peer-q", "isMeta": True, "origin": {"kind": "peer"},
                     "message": {"role": "user",
                                 "content": '<cross-session-message from="uds:peer">ack</cross-session-message>'}}
        status_after_wake = [operator_asked, tool_use(), peer_woke, assistant("Functions is waiting on the peer.")]
        for name, declaration, expect, want in (
                ("a-quiet-line-after-a-peer-wake-with-an-ask-pending-and-every-item-done-stops", closed, "allow",
                 gate.QUIET_WAKE_SIGNAL),
                ("quiet-wake-control-an-open-item-of-its-own-keeps-the-turn-open", open_item, "block", "block:1")):
            if h.wanted(name):
                verdict, got_signal = isolated_stop(h, name, status_after_wake, None, declaration,
                                                    pending_asks=["Should the peer let its permission matrix finish first?"])
                record(name, verdict == expect and got_signal == want, "{0} {1}".format(verdict, got_signal))
        name = "quiet-wake-an-uncommitted-edit-no-item-names-keeps-the-turn-open"
        if h.wanted(name):
            def dirty_repo(workspace):
                repo = os.path.join(workspace, "Functions")
                os.makedirs(repo)
                git = ["git", "-C", repo, "-c", "core.fsmonitor=false", "-c", "user.name=harness",
                       "-c", "user.email=harness@example.invalid"]
                subprocess.run(git + ["init", "--quiet"], check=True, capture_output=True)
                subprocess.run(git + ["config", "core.fsmonitor", "false"], check=True, capture_output=True)
                with open(os.path.join(repo, "undeclared.cs"), "w", encoding="utf-8") as fh:
                    fh.write("one\n")
                subprocess.run(git + ["add", "--all"], check=True, capture_output=True)
                subprocess.run(git + ["commit", "--quiet", "--message", "fixture"], check=True, capture_output=True)
                with open(os.path.join(repo, "undeclared.cs"), "w", encoding="utf-8") as fh:
                    fh.write("two\n")

            def edited_then_woken(workspace):
                return [operator_asked, edit_use(os.path.join(workspace, "Functions", "undeclared.cs")), tool_result(),
                        peer_woke, assistant("Functions is waiting on the peer.")]

            with RealWaitChecks():
                verdict, got_signal = isolated_stop(h, name, edited_then_woken, None, closed, prepare=dirty_repo,
                                                    pending_asks=["Should the peer let its permission matrix finish first?"],
                                                    one_workspace=True)
            record(name, verdict == "block" and got_signal != gate.QUIET_WAKE_SIGNAL,
                   "{0} {1}".format(verdict, got_signal))
        name = "quiet-wake-the-repeat-reason-says-to-end-with-no-ask"
        if h.wanted(name):
            asked = "Should the peer let its permission matrix finish first?"
            verdict, got_signal, out = isolated_stop(
                h, name, [operator_asked, tool_use(), peer_woke, assistant(asked)], None, closed, with_reason=True,
                pending_asks=[asked])
            record(name, verdict == "block" and "a peer or a task started this turn" in out
                   and "Do not ask it again" in out, "{0} {1}".format(verdict, got_signal))

        name = "open-item-naming-matches-whole-words-only"
        if h.wanted(name):
            inside_words = "Functions is waiting.\n\nNEEDED: your call on whether to bathe integration movers first."
            verdict, got_signal, out = isolated_stop(
                h, name, [user("fix the title prefix"), tool_use(), assistant(inside_words)], None, open_item,
                with_reason=True)
            record(name, verdict == "block" and "open items you can work yourself" in out,
                   "{0} {1}".format(verdict, got_signal))

    for name, expect, signal, texts in CASES:
        if not h.wanted(name):
            continue
        records = [user(OPERATOR_RULES)] + [assistant(t) for t in texts]
        verdict, got_signal, _ = h.run(name, records)
        record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))
        if signal:
            record("via-" + name, got_signal == signal,
                   "expected={0} actual={1}".format(signal, got_signal))

    for name, expect, signal, records in QUEUED_CASES:
        if not h.wanted(name):
            continue
        verdict, got_signal, _ = h.run(name, records)
        record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))
        if signal:
            record("via-" + name, got_signal == signal,
                   "expected={0} actual={1}".format(signal, got_signal))

    # Input waiting on the session earns no stop: the reply that ends the turn still owes a closing line, and a
    # status report is not one, whether an operator command or message is queued, a peer's is, or nothing is.
    queued_command = {"type": "queue-operation", "operation": "enqueue", "content": "/memory-audit"}
    peer_queued = {"type": "queue-operation", "operation": "enqueue",
                   "content": '<cross-session-message from="uds:peer">GATE FINISHED</cross-session-message>'}
    mid_turn = {"type": "attachment", "attachment": {"type": "queued_command", "prompt": "stop and read this",
                                                     "origin": {"kind": "human"}}}
    status = assistant("Working through the audit; the next file is next.")
    answered = assistant("Working through the audit; the next file is next.\n\n"
                         "Should I stop the audit and read what you sent first?")
    for name, expect, signal, records in [
        ("a-queued-operator-command-earns-nothing", "block", None,
         [user("do the work"), tool_use(), queued_command, status]),
        ("an-operator-message-this-turn-earns-nothing", "block", None,
         [user("do the work"), tool_use(), mid_turn, status]),
        ("an-operator-message-this-turn-answered-with-a-question-stops", "allow", "allow:question-mark",
         [user("do the work"), tool_use(), mid_turn, answered]),
        ("a-queued-peer-message-earns-nothing", "block", None, [user("do the work"), tool_use(), peer_queued, status]),
        ("the-same-status-with-no-operator-input-blocks", "block", None, [user("do the work"), tool_use(), status]),
    ]:
        if not h.wanted(name):
            continue
        verdict, got_signal, _ = h.run(name, records)
        record(name, verdict == expect and (signal is None or got_signal == signal),
               "{0} {1}".format(verdict, got_signal))

    # An ask earns one stop per operator message, whether it is a question or a NEEDED: line. The same ask again
    # after only peer or task traffic is blocked; a turn that traffic woke may stop with no new ask while the ask
    # is unanswered; the operator writing again or a different ask resets it. Each scenario is two or three stops
    # in one session, so they share the state root.
    question = "Should I build the peer-message hook, so this stops depending on my discipline?"
    reworded = "Should I build the peer-message hook, so that this stops depending on my discipline?"
    inventory_block = "```\nFix the npx call in CI\n\nInventory/.github/workflows/ci.yml\n```\n"
    bare_push = "NEEDED: a git push of Inventory, so its CI run with the npx fix can start."
    needed = inventory_block + bare_push
    needed_reworded = inventory_block + "NEEDED: a git push of Inventory, so that its CI run with the npx fix can start."
    if h.wanted("commit-ask-a-bare-needed-push-with-no-commit-message-blocks"):
        _, signal, _ = h.run("bare-push", [user("go"), tool_use(), tool_result(), assistant(bare_push)])
        record("commit-ask-a-bare-needed-push-with-no-commit-message-blocks", signal == "block:commit-ask:1", signal)
    needed_other = "NEEDED: your decision on rotating the Sonar token before the Manuals gate runs."
    question_as_needed = "NEEDED: your answer: should I build the peer-message hook, so this stops depending on my discipline?"
    goal_needed = "NEEDED: a /goal from you; the goal gate runs no tool until one is set."
    status = "The gate is running."
    long_status = "The gate is running. " * 20
    parked_ask = "q80: should the keyword test wait on the index's population state?"
    operator_turn ={"type": "user", "uuid": "op-1", "origin": {"kind": "human"},
                     "message": {"role": "user", "content": "build the enforcement"}}
    operator_again = {"type": "user", "uuid": "op-2", "origin": {"kind": "human"},
                      "message": {"role": "user", "content": "what did you find?"}}
    peer_ping = {"type": "user", "uuid": "peer-1", "isMeta": True, "origin": {"kind": "peer"},
                 "message": {"role": "user",
                             "content": '<cross-session-message from="uds:peer">ack</cross-session-message>'}}
    task_done = {"type": "user", "uuid": "task-1", "origin": {"kind": "task-notification"},
                 "message": {"role": "user", "content": ("<task-notification>\n<task-id>b1</task-id>\n"
                                                         "<status>completed</status>\n</task-notification>")}}
    peer_without_origin = {"type": "user", "uuid": "peer-2", "isMeta": True, "message": {
        "role": "user", "content": '<cross-session-message from="uds:peer">ack</cross-session-message>'}}
    idle_text = ('[Cross-session idle notice] "uds:peer", which you asked to be notified about, is idle now. This is an '
                 'automated notice from that session\'s harness, not a message from a person.')
    idle_notice = {"type": "user", "uuid": "idle-1", "isMeta": True, "message": {"role": "user", "content": idle_text}}
    idle_typed = {"type": "user", "uuid": "op-3", "origin": {"kind": "human"},
                  "message": {"role": "user", "content": idle_text}}
    idle_prose_no_meta = {"type": "user", "uuid": "op-4", "message": {"role": "user", "content": idle_text}}
    gate_feedback = {"type": "user", "uuid": "gate-fb-1", "isMeta": True, "message": {
        "role": "user", "content": "Stop hook feedback:\n" + gate.REPEATED_ON_WAKE_REASON.format(question=needed)}}
    goal_feedback = {"type": "user", "uuid": "goal-fb-1", "isMeta": True, "message": {
        "role": "user", "content": "Stop hook feedback:\n[build the enforcement]: The condition is not met yet."}}
    skill_body = {"type": "user", "uuid": "skill-1", "isMeta": True, "message": {
        "role": "user", "content": [{"type": "text", "text": "Base directory for this skill: C:\\skills\\parking-lot"}]}}
    for name, first_ask, later_turns, expect, signal in [
        ("the-same-needed-line-after-a-peer-wake-and-goal-feedback-still-blocks", needed,
         [[peer_ping, goal_feedback, assistant(needed)]], "block", "block:repeat-question:1"),
        ("a-quiet-line-after-a-wake-the-goal-hook-also-blocked-stops", needed,
         [[peer_ping, assistant(needed)], [gate_feedback, goal_feedback, assistant(status)]],
         "allow", gate.QUIET_WAKE_SIGNAL),
        ("a-quiet-line-after-a-skill-body-on-a-wake-stops", needed,
         [[peer_ping, skill_body, assistant(status)]], "allow", gate.QUIET_WAKE_SIGNAL),
        ("goal-feedback-after-an-operator-message-leaves-their-turn-so-a-quiet-line-blocks", needed,
         [[operator_again, goal_feedback, assistant(status)]], "block", "block:1"),
        ("the-same-question-asked-again-after-peer-traffic-blocks", question, [[peer_ping, assistant(question)]],
         "block", "block:repeat-question:1"),
        ("the-question-reworded-slightly-still-blocks", question, [[peer_ping, assistant(reworded)]],
         "block", "block:repeat-question:1"),
        ("the-same-needed-line-after-peer-traffic-blocks", needed, [[peer_ping, assistant(needed)]],
         "block", "block:repeat-question:1"),
        ("the-needed-line-reworded-slightly-still-blocks", needed, [[peer_ping, assistant(needed_reworded)]],
         "block", "block:repeat-question:1"),
        ("the-same-needed-line-after-a-task-notification-blocks", needed, [[task_done, assistant(needed)]],
         "block", "block:repeat-question:1"),
        ("a-pending-question-restated-as-a-needed-line-blocks", question, [[peer_ping, assistant(question_as_needed)]],
         "block", "block:repeat-question:1"),
        ("two-needed-lines-asked-apart-then-restated-together-block", needed,
         [[peer_ping, assistant(needed_other)], [peer_ping, assistant(needed + "\n" + needed_other)]],
         "block", "block:repeat-question:1"),
        ("an-earlier-ask-repeated-after-a-different-one-still-blocks", question,
         [[peer_ping, assistant("Should the Curator gate run before the Identity one?")],
          [peer_ping, assistant(question)]],
         "block", "block:repeat-question:1"),
        ("a-status-on-a-turn-the-operator-started-blocks", question, [[operator_again, assistant(status)]],
         "block", "block:1"),
        ("a-status-after-prose-with-no-origin-and-no-envelope-blocks", question,
         [[user("what did you find?"), assistant(status)]], "block", "block:1"),
        ("a-quiet-line-after-a-peer-wake-with-a-question-pending-stops", question, [[peer_ping, assistant(status)]],
         "allow", gate.QUIET_WAKE_SIGNAL),
        ("a-quiet-line-after-a-peer-wake-with-a-needed-line-pending-stops", needed, [[peer_ping, assistant(status)]],
         "allow", gate.QUIET_WAKE_SIGNAL),
        ("a-quiet-line-after-a-task-notification-with-an-ask-pending-stops", needed, [[task_done, assistant(status)]],
         "allow", gate.QUIET_WAKE_SIGNAL),
        ("a-quiet-line-after-a-peer-envelope-with-no-origin-stops", question,
         [[peer_without_origin, assistant(status)]], "allow", gate.QUIET_WAKE_SIGNAL),
        ("a-quiet-line-after-a-cross-session-idle-notice-with-an-ask-pending-stops", needed,
         [[idle_notice, assistant(status)]], "allow", gate.QUIET_WAKE_SIGNAL),
        ("the-idle-notice-text-typed-by-the-operator-starts-their-turn-so-a-quiet-line-blocks", needed,
         [[idle_typed, assistant(status)]], "block", "block:1"),
        ("the-idle-notice-text-in-a-record-that-is-not-meta-is-prose-so-a-quiet-line-blocks", needed,
         [[idle_prose_no_meta, assistant(status)]], "block", "block:1"),
        ("a-quiet-line-on-every-later-wake-stops-too", needed,
         [[peer_ping, assistant(status)], [task_done, assistant(status)]], "allow", gate.QUIET_WAKE_SIGNAL),
        ("a-quiet-line-after-a-peer-wake-with-a-goal-ask-pending-stops", goal_needed,
         [[peer_ping, assistant(status)]], "allow", gate.QUIET_WAKE_SIGNAL),
        ("the-goal-ask-again-after-peer-traffic-still-stands", goal_needed,
         [[peer_ping, assistant(goal_needed)]], "allow", "allow:blocker-token"),
        ("a-status-on-a-turn-the-operator-started-after-a-goal-ask-blocks", goal_needed,
         [[operator_again, assistant(status)]], "block", "block:1"),
        ("a-needed-line-while-a-question-is-pending-allows", question,
         [[peer_ping, assistant("NEEDED: whether to build the peer-message hook, a choice only you can make.")]],
         "allow", "allow:blocker-token"),
        ("the-same-question-after-the-operator-writes-again-allows", question,
         [[operator_again, assistant(question)]], "allow", "allow:question-mark"),
        ("the-same-needed-line-after-the-operator-writes-again-allows", needed,
         [[operator_again, assistant(needed)]], "allow", "allow:blocker-token"),
        ("a-different-question-after-peer-traffic-allows", question,
         [[peer_ping, assistant("Should the Curator gate run before the Identity one?")]],
         "allow", "allow:question-mark"),
        ("must-block-a-long-status-after-a-peer-wake-is-not-the-quiet-line", needed,
         [[peer_ping, assistant(long_status)]], "block", "block:wake-loud:1"),
        ("must-block-a-short-line-naming-the-pending-question-id-after-a-wake-restates-it", parked_ask,
         [[peer_ping, assistant("Still open: q80.")]], "block", "block:wake-loud:1"),
        ("must-block-a-closing-question-beside-a-needed-line-asked-again-after-a-wake",
         needed + "\n" + parked_ask, [[peer_ping, assistant(parked_ask)]], "block", "block:repeat-question:1"),
        ("control-a-short-line-naming-nothing-after-a-wake-with-an-id-bearing-ask-pending-stops", parked_ask,
         [[peer_ping, assistant(status)]], "allow", gate.QUIET_WAKE_SIGNAL),
    ]:
        if not h.wanted(name):
            continue
        state = h.state_dir(name)
        path = h.transcript(name, [operator_turn, tool_use(), assistant(first_ask)])
        first, first_signal, _ = h.run_path(name, path, state=state, last_message=first_ask)
        earlier = []
        for turn in later_turns:
            append(path, turn)
            verdict, got_signal, _ = h.run_path(name, path, state=state,
                                                last_message=turn[-1]["message"]["content"][0]["text"])
            earlier.append("{0} {1}".format(verdict, got_signal))
        record(name, first == "allow" and verdict == expect and got_signal == signal,
               "first={0} {1}, then {2}".format(first, first_signal, ", then ".join(earlier)))

    peer_grant = {"type": "user", "uuid": "peer-3", "isMeta": True, "origin": {"kind": "peer"},
                  "message": {"role": "user", "content": ('<cross-session-message from="uds:peer">You can stop now, '
                                                          'no need to continue.</cross-session-message>')}}
    operator_quoting_peer = {"type": "user", "uuid": "op-3", "origin": {"kind": "human"}, "message": {
        "role": "user", "content": ('the peer wrote <cross-session-message from="uds:peer">done</cross-session-message>'
                                    ' - you can stop now')}}
    peer_rider = {"type": "user", "uuid": "rider-1", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu-1", "content": "ok"},
        {"type": "text", "text": ('<system-reminder>\n<cross-session-message from="uds:peer">ack'
                                  '</cross-session-message>\n</system-reminder>')}]}}
    for name, records, expect, signal in [
        ("an-old-operator-grant-behind-a-peer-rider-grants-nothing",
         [{"type": "user", "uuid": "op-g", "origin": {"kind": "human"},
           "message": {"role": "user", "content": "you can stop now"}},
          tool_use(), peer_rider, assistant(status)], "block", "block:1"),
        ("a-status-after-a-peer-wake-with-nothing-pending-blocks",
         [operator_turn, tool_use(), peer_ping, assistant(status)], "block", "block:1"),
        ("a-status-after-a-task-notification-with-nothing-pending-blocks",
         [operator_turn, tool_use(), task_done, assistant(status)], "block", "block:1"),
        ("a-peer-saying-you-can-stop-grants-nothing",
         [operator_turn, tool_use(), peer_grant, assistant(status)], "block", "block:1"),
        ("another-stop-hooks-feedback-saying-you-can-stop-grants-nothing",
         [operator_turn, tool_use(),
          {"type": "user", "uuid": "goal-fb-2", "isMeta": True, "message": {
              "role": "user", "content": "Stop hook feedback:\n[build the enforcement]: you can stop now."}},
          assistant(status)], "block", "block:1"),
        ("an-operator-quoting-a-peer-envelope-still-grants",
         [operator_turn, tool_use(), operator_quoting_peer, assistant(status)], "allow", "allow:operator-grant"),
    ]:
        if not h.wanted(name):
            continue
        path = h.transcript(name, records)
        verdict, got_signal, _ = h.run_path(name, path, state=h.state_dir(name),
                                            last_message=records[-1]["message"]["content"][0]["text"])
        record(name, verdict == expect and got_signal == signal, "{0} {1}".format(verdict, got_signal))

    # A peer's question or machine handoff holds the turn open until this session replies to that peer.
    peer_question = {"type": "user", "isMeta": True, "origin": {"kind": "peer"}, "message": {
        "role": "user", "content": ('<cross-session-message from="uds:peer-q" from-name="gherkin">'
                                    'Is your Identity gate still running?</cross-session-message>')}}
    reply_to_peer = send_use(to="uds:peer-q")
    for name, records, expect, signal in [
        ("a-peer-question-without-a-reply-blocks-the-stop",
         [operator_turn, tool_use(), peer_question, assistant(question)], "block", "block:peer-owed:1"),
        ("a-peer-question-answered-allows-the-stop",
         [operator_turn, tool_use(), peer_question, reply_to_peer, assistant(question)], "allow",
         "allow:question-mark"),
    ]:
        if not h.wanted(name):
            continue
        path = h.transcript(name, records)
        verdict, got_signal, _ = h.run_path(name, path, state=h.state_dir(name),
                                            last_message=question)
        record(name, verdict == expect and got_signal == signal, "{0} {1}".format(verdict, got_signal))

    # The quiet stop on a wake never skips a peer's question: the reply the peer is owed still holds the turn open.
    name = "quiet-wake-control-a-peer-question-still-owed-a-reply-blocks"
    if h.wanted(name):
        state = h.state_dir(name)
        path = h.transcript(name, [operator_turn, tool_use(), assistant(question)])
        h.run_path(name, path, state=state, last_message=question)
        append(path, [peer_question, assistant(status)])
        verdict, got_signal, _ = h.run_path(name, path, state=state, last_message=status)
        record(name, verdict == "block" and got_signal == "block:peer-owed:1", "{0} {1}".format(verdict, got_signal))

    # NO GOAL, NO TOOL: the goal gate runs no tool until a goal is pinned, so an ask for one stands past every veto
    # whose remedy is a tool call, and stands again. Pinned or not about the goal, the vetoes keep their teeth.
    goal_ask = "NEEDED: you to run /goal, since the goal gate refuses every tool until a goal is pinned."
    other_ask = "NEEDED: your decision on the deploy root, which only you can make."
    for name, records, text, expect, want in (
            ("no-goal-an-ask-for-a-goal-stands-past-a-peer-owed-reply",
             [operator_turn, tool_use(), peer_question], goal_ask, "allow", "allow:blocker-token"),
            ("no-goal-control-another-ask-is-still-held-for-the-peer",
             [operator_turn, tool_use(), peer_question], other_ask, "block", "block:peer-owed:1"),
            ("no-goal-control-a-pinned-goal-keeps-the-peer-owed-veto",
             [goal_status("build the enforcement"), operator_turn, tool_use(), peer_question],
             goal_ask + " Goal: build the enforcement.", "block", "block:peer-owed:1")):
        if h.wanted(name):
            path = h.transcript(name, records + [assistant(text)])
            verdict, got_signal, _ = h.run_path(name, path, state=h.state_dir(name),
                                                last_message=text)
            record(name, verdict == expect and got_signal == want, "{0} {1}".format(verdict, got_signal))
    name = "no-goal-the-ask-for-a-goal-stands-again-before-the-operator-writes"
    if h.wanted(name):
        state = h.state_dir(name)
        path = h.transcript(name, [operator_turn, tool_use(), assistant(goal_ask)])
        first, first_signal, _ = h.run_path(name, path, state=state, last_message=goal_ask)
        append(path, [peer_ping, assistant(goal_ask)])
        verdict, got_signal, _ = h.run_path(name, path, state=state, last_message=goal_ask)
        record(name, first == "allow" and verdict == "allow" and got_signal == "allow:blocker-token",
               "first={0} {1}, then {2} {3}".format(first, first_signal, verdict, got_signal))
    name = "no-goal-unit-only-an-ask-naming-the-goal-counts"
    if h.wanted(name):
        record(name, gate.asks_for_a_goal(goal_ask, "allow:blocker-token", None)
               and not gate.asks_for_a_goal(other_ask, "allow:blocker-token", None)
               and not gate.asks_for_a_goal(goal_ask, "allow:blocker-token", "build it")
               and not gate.asks_for_a_goal(goal_ask + "\n" + other_ask, "allow:blocker-token", None), "ok")

    # A SESSION REGISTERS BEFORE IT TAKES A GOAL: under the prompt gate's rule, the ask for a goal is refused until a
    # write of the session's own coordination-board row succeeded, in any turn, and then until an artifact write
    # succeeded in a turn where Claude Code asks them to allow artifact writes: one the operator did not type, or a
    # slash command's (/goal), which Claude Code records without an origin.
    board = "https://claude.ai/artifact/board-fixture"
    board_config = os.path.join(h.root, "coordination.json")
    with open(board_config, "w", encoding="utf-8") as fh:
        json.dump({"boardUrl": board, "sessionsCollection": "sessions"}, fh)
    rule_at = datetime(2026, 10, 23, 11, 0, tzinfo=timezone.utc)
    registering = dict(user("register"), timestamp="2026-10-23T12:00:00Z")
    old_start = dict(user("register"), timestamp="2026-10-23T09:00:00Z")
    stop_turn = {"type": "user", "isMeta": True, "message": {"role": "user",
                                                             "content": "Stop hook feedback:\nSTOP GATE: register."}}
    slash_turn = {"type": "user", "uuid": "uuid-slash", "message": {"role": "user", "content": (
        "<command-name>/goal</command-name>\n<command-message>goal</command-message>\n"
        "<command-args>register first</command-args>")}}
    slash_stdout = {"type": "user", "uuid": "uuid-slash-out", "message": {"role": "user", "content": (
        "<local-command-stdout>Goal set: register first</local-command-stdout>")}}
    slash_refusal = {"type": "user", "uuid": "uuid-slash-refusal", "message": {"role": "user", "content": (
        "<command-name>/goal</command-name>\n<command-message>goal</command-message>\n"
        "<command-args>never push the registration hooks without my word</command-args>")}}

    def artifact_call(tool, call, call_id, error=False):
        result = {"type": "tool_result", "tool_use_id": call_id, "content": "refused" if error else "committed"}
        if error:
            result["is_error"] = True
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": call_id, "name": tool, "input": call}]}},
                {"type": "user", "message": {"role": "user", "content": [result]}}]

    def row_write(doc_id, data=None):
        data = {"sessionId": doc_id} if data is None else data
        return artifact_call("ArtifactData", {"action": "set", "url": board, "collection": "sessions",
                                              "doc_id": doc_id, "data": data}, "tu-reg")

    page = "https://claude.ai/artifact/page-fixture"
    page_write = artifact_call("ArtifactData", {"action": "set", "url": page, "collection": "log", "doc_id": "01-x",
                                                "data": {"summary": "x"}}, "tu-page")
    page_read = artifact_call("ArtifactData", {"action": "query", "url": page, "collection": "questions"}, "tu-read")
    page_refused = artifact_call("ArtifactData", {"action": "set", "url": page, "collection": "log", "doc_id": "01-x",
                                                  "data": {"summary": "x"}}, "tu-refused", error=True)
    publish = artifact_call("Artifact", {"file_path": "parking-lot.html"}, "tu-publish")
    artifact_read = artifact_call("Artifact", {"action": "read", "url": page}, "tu-artifact-read")

    refusal_start = dict(user("never push the registration hooks without my word"), timestamp="2026-10-23T12:00:00Z")
    refusal = "REFUSING: to push: \"never push the registration hooks without my word\"."
    for name, records, text, expect, want in (
            ("registration-must-block-an-unregistered-session-asking-for-a-goal",
             [registering], goal_ask, "block", "block:unregistered:1"),
            ("consent-must-block-a-row-written-only-in-the-typed-turn-for-consent-not-registration",
             [registering] + row_write("consent-must-block-a-row-written-only-in-the-typed-turn-for-consent-not-registration"),
             goal_ask, "block", "block:unconsented:1"),
            ("consent-any-artifact-write-after-stop-feedback-grants-it",
             [registering] + row_write("consent-any-artifact-write-after-stop-feedback-grants-it") + [stop_turn]
             + page_write, goal_ask, "allow", "allow:blocker-token"),
            ("consent-a-publish-after-stop-feedback-grants-it",
             [registering] + row_write("consent-a-publish-after-stop-feedback-grants-it") + [stop_turn] + publish,
             goal_ask, "allow", "allow:blocker-token"),
            ("consent-must-block-a-database-read-after-stop-feedback",
             [registering] + row_write("consent-must-block-a-database-read-after-stop-feedback") + [stop_turn]
             + page_read, goal_ask, "block", "block:unconsented:1"),
            ("consent-must-block-an-artifact-read-after-stop-feedback",
             [registering] + row_write("consent-must-block-an-artifact-read-after-stop-feedback") + [stop_turn]
             + artifact_read, goal_ask, "block", "block:unconsented:1"),
            ("consent-must-block-a-refused-write-after-stop-feedback",
             [registering] + row_write("consent-must-block-a-refused-write-after-stop-feedback") + [stop_turn]
             + page_refused, goal_ask, "block", "block:unconsented:1"),
            ("consent-an-operator-message-queued-mid-turn-leaves-a-stop-started-turn-unprompted",
             [registering] + row_write("consent-an-operator-message-queued-mid-turn-leaves-a-stop-started-turn-unprompted")
             + [stop_turn, {"type": "attachment", "attachment": {"type": "queued_command", "origin": {"kind": "human"},
                                                                 "prompt": "keep going"}}] + page_write,
             goal_ask, "allow", "allow:blocker-token"),
            ("consent-must-block-a-peer-message-queued-mid-turn-in-a-typed-turn",
             [registering] + row_write("consent-must-block-a-peer-message-queued-mid-turn-in-a-typed-turn")
             + [{"type": "attachment", "attachment": {"type": "queued_command", "origin": {"kind": "peer"},
                                                      "prompt": '<cross-session-message from="uds:x">hi'
                                                                '</cross-session-message>'}}] + page_write,
             goal_ask, "block", "block:unconsented:1"),
            ("consent-must-block-a-write-in-a-later-typed-turn",
             [registering] + row_write("consent-must-block-a-write-in-a-later-typed-turn") + [stop_turn,
                                                                                               registering] + page_write,
             goal_ask, "block", "block:unconsented:1"),
            ("consent-a-row-written-in-a-slash-command-turn-grants-it",
             [registering, slash_turn, slash_stdout]
             + row_write("consent-a-row-written-in-a-slash-command-turn-grants-it"),
             goal_ask, "allow", "allow:blocker-token"),
            ("consent-must-block-a-write-in-the-typed-turn-after-a-slash-command-turn",
             [registering, slash_turn, slash_stdout, registering]
             + row_write("consent-must-block-a-write-in-the-typed-turn-after-a-slash-command-turn"),
             goal_ask, "block", "block:unconsented:1"),
            ("registration-a-row-written-after-stop-feedback-lets-the-ask-for-a-goal-stand",
             [registering, stop_turn] + row_write("registration-a-row-written-after-stop-feedback-lets-the-ask-for-a-goal-stand"),
             goal_ask, "allow", "allow:blocker-token"),
            ("registration-must-block-another-sessions-row",
             [registering, stop_turn] + row_write("some-other-session"), goal_ask, "block", "block:unregistered:1"),
            ("registration-must-block-a-row-carrying-no-session-id",
             [registering, stop_turn] + row_write("registration-must-block-a-row-carrying-no-session-id",
                                                  {"machine": "HOST"}), goal_ask, "block", "block:unregistered:1"),
            ("registration-a-row-carrying-only-the-session-id-registers-since-a-name-is-never-asked-for",
             [registering, stop_turn] + row_write(
                 "registration-a-row-carrying-only-the-session-id-registers-since-a-name-is-never-asked-for",
                 {"sessionId": "registration-a-row-carrying-only-the-session-id-registers-since-a-name-is-never-asked-for"}),
             goal_ask, "allow", "allow:blocker-token"),
            ("registration-a-session-older-than-the-rule-is-not-asked-to-register",
             [old_start], goal_ask, "allow", "allow:blocker-token"),
            ("registration-must-block-any-other-ask-while-unregistered",
             [registering], other_ask, "block", "block:unregistered:1"),
            ("registration-an-unregistered-session-may-still-refuse",
             [refusal_start, tool_use()], refusal, "allow", "allow:refusal-token"),
            ("refusal-quoting-the-arguments-of-the-operators-slash-command-stands",
             [registering, slash_refusal, tool_use()], refusal, "allow", "allow:refusal-token"),
            ("refusal-must-block-quoting-only-the-slash-commands-printed-output",
             [registering, slash_turn, slash_stdout, tool_use()],
             "REFUSING: to register twice: \"Goal set: register first\".", "block", "block:refusal-unquoted:1"),
            ("registration-an-unregistered-session-may-still-stop-on-the-operators-grant",
             [registering, tool_use(), tool_result("that is enough for now, you can stop")],
             "Here is where the port stands so far.", "allow", "allow:operator-grant"),
            ("registration-an-unregistered-session-may-still-end-a-turn-with-no-text",
             [registering, tool_use(), tool_result()], None, "allow", "allow:no-assistant-text")):
        if h.wanted(name):
            path = h.transcript(name, records + ([assistant(text)] if text is not None else []))
            payload = {"hook_event_name": "Stop", "transcript_path": path, "session_id": name,
                       "stop_hook_active": False}
            if text is not None:
                payload["last_assistant_message"] = text
            got_signal, out = gate.decide(payload, path, h.state_dir(name), 100, git_timeout=None,
                                          gate_workspace=h.no_gated_repos(),
                                          temp_root=os.path.join(h.root, "temp-claude"), require_parked=False,
                                          prompt_rule_at=rule_at, coordination_config=board_config)
            verdict = "block" if out else "allow"
            ok = verdict == expect and got_signal == want and (
                expect == "allow" or got_signal.startswith("block:refusal") or (board in out and name in out))
            record(name, ok, "{0} {1}".format(verdict, got_signal))

    # A STOP THAT CLAIMS TO BE HONEST IS AUDITED FIRST: under the session-start rules, a blocker, a question or any
    # other AUDITED_SIGNALS stop earns nothing until the session's own audit cache shows a sync, a matching reconcile,
    # no operator message after the sync, every eject cataloged and a verdict on every message but the newest. The
    # cache is written here the way session_audit.py writes it; the gate reads only the files.
    audit_root = os.path.join(h.root, "temp-claude", "audits", "session-audit")
    question = "Which port should the BFF listen on?"

    def declare_for(name, path, data=None):
        directory = session_home.work_products_dir()
        os.makedirs(directory, exist_ok=True)
        stem = os.path.splitext(os.path.basename(path))[0]
        with open(os.path.join(directory, stem + ".json"), "w", encoding="utf-8") as fh:
            json.dump({"repos": {}} if data is None else data, fh)

    def audit_cache(name, path, synced=True, reconciled=True, catalog=(), verdicts=()):
        folder = os.path.join(audit_root, name)
        os.makedirs(folder, exist_ok=True)
        offset = os.path.getsize(path) if synced else 0
        state = {"transcript": path, "chain": [{"path": path, "limit": None}], "files": {path: {"offset": offset}}}
        if reconciled:
            state["reconciled"] = {path: offset}
        with open(os.path.join(folder, "state.json"), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        with open(os.path.join(folder, "messages.jsonl"), "w", encoding="utf-8") as fh:
            for seq, (message_id, ts) in enumerate(catalog, 1):
                fh.write(json.dumps({"id": message_id, "seq": seq, "ts": ts, "channel": "typed", "text": "ask",
                                     "file": path, "start": 0, "end": 0}) + "\n")
        with open(os.path.join(folder, "verdicts.jsonl"), "w", encoding="utf-8") as fh:
            for message_id, status, evidence in verdicts:
                fh.write(json.dumps({"id": message_id, "status": status, "evidence": evidence,
                                     "at": "2026-10-23T12:40:00+00:00"}) + "\n")

    def eject_logged(name, stamp="2026-10-23T11:30:00+00:00"):
        state = h.state_dir(name)
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "decisions.log"), "a", encoding="utf-8") as fh:
            fh.write("{0}\t{1}\teject:filler-streak:4\n".format(stamp, name))

    import declared_items

    def audit_skill_problem():
        try:
            audit = declared_items.session_audit_module()
        except Exception as error:
            return "session_audit.py at {0} did not load ({1})".format(declared_items.SESSION_AUDIT_SCRIPT, error)
        if hasattr(audit, "session_ejects") and hasattr(audit, "eject_id"):
            return None
        return ("session_audit.py at {0} predates the eject catalog (session_ejects, eject_id): the row runs once the "
                "workspace's Tools carries it".format(declared_items.SESSION_AUDIT_SCRIPT))

    skill_problem = audit_skill_problem()
    rows_before_the_skill_check = {"audit-must-block-a-blocker-with-no-session-audit",
                                   "audit-must-block-a-closing-question-with-no-session-audit",
                                   "audit-must-block-a-blocker-with-no-work-product-declaration",
                                   "audit-control-the-ask-for-a-goal-needs-no-audit",
                                   "idle-from-the-master-without-its-session-audit-blocks"}
    register_id = "uuid-p-1"
    registered_at = "2026-10-23T12:00:00Z"
    eject_stamp = "2026-10-23T11:30:00+00:00"
    for name, text, prepare, expect, want, said in (
            ("audit-must-block-a-blocker-with-no-session-audit", other_ask,
             lambda name, path: declare_for(name, path), "block", "block:unaudited:1", "never run its session audit"),
            ("audit-must-block-a-closing-question-with-no-session-audit", question,
             lambda name, path: declare_for(name, path), "block", "block:unaudited:1", "never run its session audit"),
            ("audit-must-block-a-blocker-with-no-work-product-declaration", other_ask,
             lambda name, path: audit_cache(name, path), "block", "block:unaudited:1", "no work-product declaration"),
            ("audit-a-blocker-after-sync-and-reconcile-stands-when-only-the-newest-message-is-cataloged", other_ask,
             lambda name, path: (declare_for(name, path), audit_cache(name, path, catalog=[(register_id, registered_at)])),
             "allow", "allow:blocker-token", ""),
            ("audit-must-block-a-reconcile-not-matched-at-the-current-sync", other_ask,
             lambda name, path: (declare_for(name, path), audit_cache(name, path, reconciled=False)),
             "block", "block:unaudited:1", "reconcile has not matched"),
            ("audit-must-block-an-operator-message-written-after-the-sync", other_ask,
             lambda name, path: (declare_for(name, path), audit_cache(name, path, synced=False)),
             "block", "block:unaudited:1", "written after the session audit's last sync"),
            ("audit-must-block-an-eject-the-audit-has-not-cataloged", other_ask,
             lambda name, path: (declare_for(name, path), audit_cache(name, path, catalog=[(register_id, registered_at)]),
                                 eject_logged(name)),
             "block", "block:unaudited:1", "ended 1 of this session's turn(s)"),
            ("audit-must-block-a-cataloged-eject-with-no-verdict", other_ask,
             lambda name, path: (declare_for(name, path), eject_logged(name), audit_cache(
                 name, path, catalog=[("eject:" + eject_stamp, eject_stamp), (register_id, registered_at)])),
             "block", "block:unaudited:1", "have no audit verdict: eject:20"),
            ("audit-control-a-cataloged-eject-with-a-verdict-stands", other_ask,
             lambda name, path: (declare_for(name, path), eject_logged(name), audit_cache(
                 name, path, catalog=[("eject:" + eject_stamp, eject_stamp), (register_id, registered_at)],
                 verdicts=[("eject:" + eject_stamp, "answered", "a-explained")])),
             "allow", "allow:blocker-token", ""),
            ("audit-must-block-an-open-verdict-naming-no-declared-item", other_ask,
             lambda name, path: (declare_for(name, path), audit_cache(
                 name, path, catalog=[("uuid-p-0", "2026-10-23T11:00:00Z"), (register_id, registered_at)],
                 verdicts=[("uuid-p-0", "outstanding", "the port fix is unverified")])),
             "block", "block:unaudited:1", "names no open declared item"),
            ("audit-control-the-ask-for-a-goal-needs-no-audit", goal_ask,
             lambda name, path: None, "allow", "allow:blocker-token", "")):
        if skill_problem and name not in rows_before_the_skill_check:
            skip(name, skill_problem)
            continue
        if h.wanted(name):
            path = h.transcript(name, [registering, stop_turn] + row_write(name) + [assistant(text)])
            prepare(name, path)
            payload = {"hook_event_name": "Stop", "transcript_path": path, "session_id": name,
                       "stop_hook_active": False, "last_assistant_message": text}
            got_signal, out = gate.decide(payload, path, h.state_dir(name), 100, git_timeout=None,
                                          gate_workspace=h.no_gated_repos(),
                                          temp_root=os.path.join(h.root, "temp-claude"), require_parked=False,
                                          prompt_rule_at=rule_at, coordination_config=board_config)
            verdict = "block" if out else "allow"
            record(name, verdict == expect and got_signal == want and said in (out or ""),
                   "{0} {1} {2}".format(verdict, got_signal, (out or "")[:160]))
    name = "audit-must-block-an-outdated-session-audit-script-is-named-never-passed"
    if h.wanted(name):
        stub = os.path.join(h.root, "outdated_session_audit.py")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write("def reconciled_now(state):\n    return True\n")
        kept_script = declared_items.SESSION_AUDIT_SCRIPT
        declared_items.SESSION_AUDIT_SCRIPT = stub
        try:
            path = h.transcript(name, [registering, stop_turn] + row_write(name) + [assistant(other_ask)])
            declare_for(name, path)
            audit_cache(name, path, catalog=[(register_id, registered_at)])
            payload = {"hook_event_name": "Stop", "transcript_path": path, "session_id": name,
                       "stop_hook_active": False, "last_assistant_message": other_ask}
            got_signal, out = gate.decide(payload, path, h.state_dir(name), 100, git_timeout=None,
                                          gate_workspace=h.no_gated_repos(),
                                          temp_root=os.path.join(h.root, "temp-claude"), require_parked=False,
                                          prompt_rule_at=rule_at, coordination_config=board_config)
        finally:
            declared_items.SESSION_AUDIT_SCRIPT = kept_script
        record(name, bool(out) and got_signal == "block:unaudited:1" and stub in (out or ""),
               "{0} {1}".format(got_signal, (out or "")[:160]))
    if h.wanted("audit-control-a-session-older-than-the-rule-is-not-audited"):
        name = "audit-control-a-session-older-than-the-rule-is-not-audited"
        path = h.transcript(name, [old_start, tool_use(), assistant(other_ask)])
        payload = {"hook_event_name": "Stop", "transcript_path": path, "session_id": name,
                   "stop_hook_active": False, "last_assistant_message": other_ask}
        got_signal, out = gate.decide(payload, path, h.state_dir(name), 100, git_timeout=None,
                                      gate_workspace=h.no_gated_repos(),
                                      temp_root=os.path.join(h.root, "temp-claude"), require_parked=False,
                                      prompt_rule_at=rule_at, coordination_config=board_config)
        record(name, not out and got_signal == "allow:blocker-token", "{0} {1}".format(got_signal, (out or "")[:120]))

    # An ask the gate cannot record is one it would let be asked again, so a failed save refuses the stop and names
    # the path. A folder planted where the record goes makes the write fail for real.
    # The retry that names the path is let through, so a failure that persists never traps the session.
    for name, plant, names, expect in (
            ("must-block-an-ask-the-gate-cannot-record-refuses-the-stop", True, False, "block"),
            ("must-pass-the-retry-naming-the-unrecorded-path-earns-its-stop", True, True, "allow"),
            ("control-the-same-ask-recorded-earns-its-stop", False, False, "allow")):
        if not h.wanted(name):
            continue
        state = h.state_dir(name)
        ask = "Which deploy root should the copy use?"
        path = h.transcript(name, [user("pick the deploy root"), tool_use(), assistant(ask)])
        unrecorded = os.path.join(state, gate.safe_session_id(name, path) + gate.PENDING_QUESTION_SUFFIX)
        if plant:
            os.makedirs(unrecorded)
        if names:
            ask = "The gate could not record this ask at {0}, where a folder sits.\n{1}".format(unrecorded, ask)
            path = h.transcript(name, [user("pick the deploy root"), tool_use(), assistant(ask)])
        verdict, got_signal, out = h.run_path(name, path, state=state, last_message=ask)
        ok = verdict == expect and (expect == "allow" or (got_signal.startswith("block:pending-unsaved:")
                                                          and gate.PENDING_QUESTION_SUFFIX in out))
        record(name, ok, "{0} {1}".format(verdict, got_signal))

    # A poke process never registers: it exists to deliver one message and exit, so under the prompt rule its stop
    # after the SendMessage is earned anyway. The same transcript without its launcher's marker is still refused.
    # The master's wake process is the same kind of messenger and never registers either.
    messenger_keys = (gate.POKE_VARIABLE, gate.MASTER_WAKE_VARIABLE, "COMPUTERNAME")
    kept_messenger = {key: os.environ.get(key) for key in messenger_keys}
    try:
        for name, environment, expect, want in (
                ("poke-process-under-the-prompt-rule-stops-unregistered", {gate.POKE_VARIABLE: "1"}, "allow",
                 gate.POKE_SIGNAL),
                ("wake-process-under-the-prompt-rule-stops-unregistered",
                 {gate.MASTER_WAKE_VARIABLE: "1", "COMPUTERNAME": gate.MASTER_HOST}, "allow", gate.MASTER_WAKE_SIGNAL),
                ("messenger-control-the-same-stop-without-a-marker-is-refused-as-unregistered", {}, "block",
                 "block:unregistered:1")):
            if not h.wanted(name):
                continue
            for key in messenger_keys:
                os.environ.pop(key, None)
            os.environ["COMPUTERNAME"] = kept_messenger["COMPUTERNAME"] or "DESKTOP-TEST"
            os.environ.update(environment)
            path = h.transcript(name, [registering, send_use("tools-34"), assistant("Sent.")])
            payload = {"hook_event_name": "Stop", "transcript_path": path, "session_id": name,
                       "stop_hook_active": False, "last_assistant_message": "Sent."}
            got_signal, out = gate.decide(payload, path, h.state_dir(name), 100, git_timeout=None,
                                          gate_workspace=h.no_gated_repos(),
                                          temp_root=os.path.join(h.root, "temp-claude"), require_parked=True,
                                          prompt_rule_at=rule_at, coordination_config=board_config)
            verdict = "block" if out else "allow"
            record(name, verdict == expect and got_signal == want, "{0} {1}".format(verdict, got_signal))
    finally:
        for key, value in kept_messenger.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    refused_rows = ("refused-ask-must-block-an-ask-a-refused-stop-showed-the-operator-is-a-repeat-when-asked-again",
                    "refused-ask-a-quiet-line-after-it-stops",
                    "refused-ask-control-a-new-ask-after-a-refused-one-still-earns-the-stop")
    trailed = question + "\n\nCo-Authored-By: Claude Opus 5 <x@y>"
    quiet_line = "Waiting on the operator's answer."
    trailer_veto_was = gate.TRAILER_VETO_ENABLED
    gate.TRAILER_VETO_ENABLED = True
    try:
        if any(h.wanted(row) for row in refused_rows[:2]):
            state = h.state_dir(refused_rows[0])
            path = h.transcript(refused_rows[0], [operator_turn, tool_use(), assistant(trailed)])
            _, first_signal, _ = h.run_path(refused_rows[0], path, state=state, last_message=trailed)
            append(path, [tool_use(), assistant(question)])
            _, again, again_out = h.run_path(refused_rows[0], path, state=state, stop_hook_active=True,
                                             last_message=question)
            record(refused_rows[0], first_signal == "block:trailer:1" and again.startswith("block:repeat-question:")
                   and gate.REFUSED_OPENING in again_out, "first={0}, then {1}".format(first_signal, again))
            append(path, [assistant(quiet_line)])
            _, quiet, _ = h.run_path(refused_rows[0], path, state=state, stop_hook_active=True,
                                     last_message=quiet_line)
            record(refused_rows[1], quiet == gate.QUIET_WAKE_SIGNAL, quiet)
        if h.wanted(refused_rows[2]):
            other = "Which branch should the triage drafts land on?"
            state = h.state_dir(refused_rows[2])
            path = h.transcript(refused_rows[2], [operator_turn, tool_use(), assistant(trailed)])
            _, first_signal, _ = h.run_path(refused_rows[2], path, state=state, last_message=trailed)
            append(path, [tool_use(), assistant(other)])
            _, fresh, _ = h.run_path(refused_rows[2], path, state=state, stop_hook_active=True, last_message=other)
            record(refused_rows[2], first_signal == "block:trailer:1" and fresh == "allow:question-mark",
                   "first={0}, then {1}".format(first_signal, fresh))
    finally:
        gate.TRAILER_VETO_ENABLED = trailer_veto_was

    if h.wanted("a-status-with-no-pending-question-still-blocks"):
        path = h.transcript("no-pending", [operator_turn, tool_use(), assistant("The gate is running.")])
        verdict, got_signal, _ = h.run_path("no-pending", path, state=os.path.join(h.root, "state-no-pending"),
                                            last_message="The gate is running.")
        record("a-status-with-no-pending-question-still-blocks", verdict == "block",
               "{0} {1}".format(verdict, got_signal))

    for name, expect, prompt in GRANT_CASES:
        if not h.wanted(name):
            continue
        records = [user(prompt), assistant("Here is where things stand so far.")]
        verdict, got_signal, _ = h.run(name, records)
        record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))

    # WAITING: is the wait for something this session started, and it earns the stop only while that is
    # live. Every block case is the allow case with one requirement removed: still running (no completion
    # notice, no TaskStop, output fresh), or the token on its own line outside a fence. A background task that
    # is not a gate counts, and the payload need not name a gate.
    waiting = ("Functions is finished and its commit message is drafted.\n\n"
               "WAITING: on my Functions gate, running in the background.")
    gate_command = gate_script(h.root)
    launch, task_id = background_launch(h.root, gate_command)
    other, _ = background_launch(h.root, "Start-Sleep -Seconds 600", tool_id="tu-o")
    stale_launch, _ = background_launch(h.root, gate_command, tool_id="tu-s", stale=True)
    waiting_cases = [
        ("waiting-on-a-live-own-gate-allows", "allow", "allow:waiting-on-own-gate",
         [user("fix the title prefix")] + launch + [assistant(waiting)]),
        ("waiting-on-a-gate-that-already-finished-blocks", "block", None,
         [user("fix the title prefix")] + launch + [task_notification(task_id), assistant(waiting)]),
        ("waiting-on-a-gate-that-was-stopped-blocks", "block", None,
         [user("fix the title prefix")] + launch + [task_stop(task_id), assistant(waiting)]),
        ("waiting-on-a-background-task-that-is-not-a-gate-allows", "allow", "allow:waiting-on-own-gate",
         [user("fix the title prefix")] + other + [assistant(waiting)]),
        ("waiting-on-a-gate-whose-output-went-stale-blocks", "block", None,
         [user("fix the title prefix")] + stale_launch + [assistant(waiting)]),
        ("waiting-with-no-background-gate-at-all-blocks", "block", None,
         [user("fix the title prefix"), tool_use(), assistant(waiting)]),
        ("waiting-on-own-work-the-line-does-not-call-a-gate-allows", "allow", "allow:waiting-on-own-gate",
         [user("fix the title prefix")] + launch +
         [assistant("Functions is finished.\n\nWAITING: on the run I started in the background.")]),
        ("waiting-inside-a-code-fence-does-not-count", "block", None,
         [user("fix the title prefix")] + launch +
         [assistant("The token looks like this:\n\n{0}\nWAITING: on my gate\n{0}".format(FENCE))]),
        ("waiting-mid-line-does-not-count", "block", None,
         [user("fix the title prefix")] + launch +
         [assistant("Functions is finished and I am WAITING: on my gate.")]),
    ]
    for name, expect, signal, records in waiting_cases:
        if not h.wanted(name):
            continue
        verdict, got_signal, _ = h.run(name, records)
        record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))
        if signal:
            record("via-" + name, got_signal == signal,
                   "expected={0} actual={1}".format(signal, got_signal))

    # A WAIT LINE IS NO ASK: a long gate is waited on by the same WAITING: stop again and again with no operator
    # message between, and the repeat check must never read the second as an ask asked twice.
    name = "waiting-twice-on-a-live-own-gate-with-no-operator-message-between-allows-both"
    if h.wanted(name):
        state = h.state_dir(name)
        path = h.transcript(name, [user("fix the title prefix")] + launch + [assistant(waiting)])
        first, first_signal, _ = h.run_path(name, path, state=state, last_message=waiting)
        append(path, [tool_use(), assistant(waiting)])
        verdict, got_signal, _ = h.run_path(name, path, state=state, last_message=waiting)
        record(name, first == "allow" and first_signal == "allow:waiting-on-own-gate" and verdict == "allow"
               and got_signal == "allow:waiting-on-own-gate",
               "first={0} {1}, then {2} {3}".format(first, first_signal, verdict, got_signal))

    # IDLE: is the master session's stop between runbook cycles, and only the master may use it: MASTER_SESSION set
    # by its watchdog, on the master's machine, with the wake task scheduled to start the next cycle. Every block case
    # is the allow case with one of those removed. The wake process's own stop is the turn's SendMessage under
    # MASTER_WAKE; another session's SendMessage earns nothing by it.
    idle = "The cycle found nothing blocked.\n\nIDLE: until the next wake."
    master = {"MASTER_SESSION": "1", "COMPUTERNAME": gate.MASTER_HOST}
    wake = {"MASTER_WAKE": "1", "COMPUTERNAME": gate.MASTER_HOST}
    idle_cases = [
        ("idle-from-the-master-with-its-wake-scheduled-allows", "allow", gate.MASTER_IDLE_SIGNAL, master, True,
         [user("GOAL: read and follow the runbook"), tool_use(), assistant(idle)]),
        ("idle-from-a-session-without-master-session-blocks", "block", None, {}, True,
         [user("GOAL: read and follow the runbook"), tool_use(), assistant(idle)]),
        ("idle-from-master-session-on-another-machine-blocks", "block", None,
         {"MASTER_SESSION": "1", "COMPUTERNAME": "DESKTOP-HO4IRUI"}, True,
         [user("GOAL: read and follow the runbook"), tool_use(), assistant(idle)]),
        ("idle-from-the-master-with-its-wake-unscheduled-blocks", "block", None, master, False,
         [user("GOAL: read and follow the runbook"), tool_use(), assistant(idle)]),
        ("idle-mid-line-does-not-count", "block", None, master, True,
         [user("GOAL: read and follow the runbook"), tool_use(),
          assistant("The cycle found nothing blocked, so I am IDLE: until the next wake.")]),
        ("idle-inside-a-code-fence-does-not-count", "block", None, master, True,
         [user("GOAL: read and follow the runbook"), tool_use(),
          assistant("The token looks like this:\n\n{0}\nIDLE: until the next wake\n{0}".format(FENCE))]),
        ("wake-process-after-its-sendmessage-allows", "allow", gate.MASTER_WAKE_SIGNAL, wake, True,
         [user("Send the master its cycle message."), send_use("crgolden-92"), assistant("Sent.")]),
        ("wake-process-without-a-sendmessage-blocks", "block", None, wake, True,
         [user("Send the master its cycle message."), tool_use(), assistant("Sent.")]),
        ("sendmessage-without-master-wake-earns-no-wake-stop", "block", None, {}, True,
         [user("tell the peer"), send_use("crgolden-92"), assistant("Sent.")]),
        ("poke-process-after-its-sendmessage-allows-on-any-machine", "allow", gate.POKE_SIGNAL,
         {gate.POKE_VARIABLE: "1", "COMPUTERNAME": "DESKTOP-HO4IRUI"}, False,
         [user("Send tools-34 its read-the-board message."), send_use("tools-34"), assistant("Sent.")]),
        ("poke-process-without-a-sendmessage-blocks", "block", None,
         {gate.POKE_VARIABLE: "1", "COMPUTERNAME": "DESKTOP-HO4IRUI"}, False,
         [user("Send tools-34 its read-the-board message."), tool_use(), assistant("Sent.")]),
        ("poke-marker-other-than-1-earns-no-poke-stop", "block", None,
         {gate.POKE_VARIABLE: "true", "COMPUTERNAME": "DESKTOP-HO4IRUI"}, False,
         [user("Send tools-34 its read-the-board message."), send_use("tools-34"), assistant("Sent.")]),
    ]
    kept_check = gate.WAKE_TASK_CHECK
    kept_environment = {key: os.environ.get(key)
                        for key in ("MASTER_SESSION", "MASTER_WAKE", gate.POKE_VARIABLE, "COMPUTERNAME")}
    try:
        for name, expect, signal, environment, scheduled, records in idle_cases:
            if not h.wanted(name):
                continue
            for key in kept_environment:
                os.environ.pop(key, None)
            os.environ["COMPUTERNAME"] = kept_environment["COMPUTERNAME"] or "DESKTOP-TEST"
            os.environ.update(environment)
            gate.WAKE_TASK_CHECK = (lambda: True) if scheduled else (lambda: False)
            verdict, got_signal, _ = h.run(name, records)
            record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))
            if signal:
                record("via-" + name, got_signal == signal, "expected={0} actual={1}".format(signal, got_signal))
    finally:
        gate.WAKE_TASK_CHECK = kept_check
        for key, value in kept_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    for name, prepare, expect, want, said in (
            ("idle-from-the-master-without-its-session-audit-blocks",
             lambda name, path: declare_for(name, path), "block", "block:unaudited:1", "never run its session audit"),
            ("idle-from-the-master-with-its-audit-synced-allows",
             lambda name, path: (declare_for(name, path), audit_cache(name, path, catalog=[(register_id, registered_at)])),
             "allow", gate.MASTER_IDLE_SIGNAL, "")):
        if skill_problem and name not in rows_before_the_skill_check:
            skip(name, skill_problem)
            continue
        if not h.wanted(name):
            continue
        for key in kept_environment:
            os.environ.pop(key, None)
        os.environ["COMPUTERNAME"] = gate.MASTER_HOST
        os.environ["MASTER_SESSION"] = "1"
        gate.WAKE_TASK_CHECK = lambda: True
        try:
            path = h.transcript(name, [registering, stop_turn] + row_write(name) + [tool_use(), assistant(idle)])
            prepare(name, path)
            payload = {"hook_event_name": "Stop", "transcript_path": path, "session_id": name,
                       "stop_hook_active": False, "last_assistant_message": idle}
            got_signal, out = gate.decide(payload, path, h.state_dir(name), 100, git_timeout=None,
                                          gate_workspace=h.no_gated_repos(),
                                          temp_root=os.path.join(h.root, "temp-claude"), require_parked=False,
                                          prompt_rule_at=rule_at, coordination_config=board_config)
        finally:
            gate.WAKE_TASK_CHECK = kept_check
            for key, value in kept_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        verdict = "block" if out else "allow"
        record(name, verdict == expect and got_signal == want and said in (out or ""),
               "{0} {1} {2}".format(verdict, got_signal, (out or "")[:160]))

    # Nobody watches the master's conversation, so its stop is refused while a question it parked is open on the page
    # and no PushNotification has named it. Open is read from the page this turn: an answered question owes no push,
    # and with no read the gate demands one rather than pushing questions the operator may have answered.
    def master_park(doc_id="q7-root"):
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tu-park", "name": "ArtifactData",
                     "input": {"action": "set", "url": "https://claude.ai/artifact/x", "collection": "questions",
                               "doc_id": doc_id, "data": {"question": "Which root should the copy use?",
                                                          "status": "open"}}}]}},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu-park", "content": "committed"}]}}]

    def master_read(documents=()):
        printed = "\n".join(json.dumps({"id": doc_id, "data": {"status": status}}) for doc_id, status in documents)
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tu-read", "name": "ArtifactData",
                     "input": {"action": "query", "url": "https://claude.ai/artifact/x", "collection": "questions",
                               "query": {"where": [["status", "==", "answered"]]}}}]}},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu-read", "content": "{0} documents from collection"
                     " \"questions\":\n{1}".format(len(documents), printed)}]}}]

    def master_push(message):
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tu-push", "name": "PushNotification",
                     "input": {"message": message, "status": "proactive"}}]}},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu-push", "content": "Not sent: the user is active."}]}}]

    goal_turn = [user("GOAL: read and follow the runbook"), tool_use()]
    ask_q7 = "Which root should the copy use (q7)?\nGOAL: read and follow the runbook"
    push_cases = [
        ("master-push-must-block-idle-with-an-open-question-never-pushed", "block", "block:master-push:1", master,
         goal_turn + master_park() + master_read() + [assistant(idle)]),
        ("master-push-idle-after-a-push-naming-the-open-question-allows", "allow", gate.MASTER_IDLE_SIGNAL, master,
         goal_turn + master_park() + master_read() + master_push("q7 waits on you: https://claude.ai/artifact/x")
         + [assistant(idle)]),
        ("master-push-a-push-naming-another-question-blocks", "block", "block:master-push:1", master,
         goal_turn + master_park() + master_read() + master_push("q70 waits on you") + [assistant(idle)]),
        ("master-push-with-no-page-read-this-turn-demands-the-read", "block", "block:master-push-read:1", master,
         goal_turn + master_park() + [assistant(idle)]),
        ("master-push-a-question-the-page-shows-answered-owes-no-push", "allow", gate.MASTER_IDLE_SIGNAL, master,
         goal_turn + master_park() + master_read([("q7-root", "answered")]) + [assistant(idle)]),
        ("master-push-must-block-a-closing-question-too", "block", "block:master-push:1", master,
         goal_turn + master_park() + master_read() + [assistant(ask_q7)]),
        ("master-push-control-another-session-asking-owes-no-push", "allow", "allow:question-mark", {},
         goal_turn + master_park() + master_read() + [assistant(ask_q7)]),
    ]
    try:
        for name, expect, signal, environment, records in push_cases:
            if not h.wanted(name):
                continue
            for key in kept_environment:
                os.environ.pop(key, None)
            os.environ["COMPUTERNAME"] = kept_environment["COMPUTERNAME"] or "DESKTOP-TEST"
            os.environ.update(environment)
            gate.WAKE_TASK_CHECK = lambda: True
            verdict, got_signal, _ = h.run(name, records)
            record(name, verdict == expect and got_signal == signal, "{0} {1}".format(verdict, got_signal))
    finally:
        gate.WAKE_TASK_CHECK = kept_check
        for key, value in kept_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    name = "the-wake-task-check-reads-status-and-next-run-time"
    if h.wanted(name):
        class Done:
            def __init__(self, returncode, stdout):
                self.returncode, self.stdout = returncode, stdout

        def answering(returncode, stdout):
            return lambda *args, **kwargs: Done(returncode, stdout)

        ready = '"HOST","\\Claude Code - Master Wake","10/22/2026 9:00:00 AM","Ready","Interactive/Background"'
        disabled = ready.replace('"Ready"', '"Disabled"')
        no_next = ready.replace('"10/22/2026 9:00:00 AM"', '"N/A"')
        readings = (gate.wake_task_scheduled(answering(0, ready)),
                    gate.wake_task_scheduled(answering(0, disabled)),
                    gate.wake_task_scheduled(answering(0, no_next)),
                    gate.wake_task_scheduled(answering(1, "")))
        record(name, readings == (True, False, False, False),
               "ready, disabled, no next run, missing -> {0}".format(readings))

    for name, expect, calls, text in COMPLETION_CASES:
        if not h.wanted(name):
            continue
        records = [user("migrate all four repos")]
        records += [tool_use()] * calls
        records.append(assistant(text))
        verdict, got_signal, _ = h.run(name, records)
        record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))

    # The corroboration must survive an INTERRUPTED turn: prose arriving mid-turn once stranded every
    # tool call before it. This is a WIDENING, so the positive cases cannot carry the load -- a fix
    # that simply never halted would turn them all green. `fresh-prompt-with-no-work` is the one that
    # discriminates, and it is the only one an over-widening breaks.
    complete = ("Finished.\nDONE: migrated all six workers.\nVERIFIED: gate green, 1089 passed.")
    MIDTURN = [
        ("interjection-riding-a-tool-result-keeps-tool-calls", "allow",
         [user("migrate"), tool_use(), tool_result("the session is named close-parking-lot"),
          assistant(complete)]),
        ("system-reminder-does-not-strand-tool-calls", "allow",
         [user("migrate"), tool_use(),
          user("<system-reminder>a file was modified</system-reminder>"), assistant(complete)]),
        ("grant-sent-mid-turn-is-still-read", "allow",
         [user("migrate"), tool_use(), tool_result("that is enough for now, you can stop"),
          assistant("Here is where the port stands so far.")]),
        ("fresh-prompt-with-no-work-still-blocks", "block",
         [user("migrate"), tool_use(), tool_result(), assistant("That is done."),
          user("now summarise what you changed", "p-2"), assistant(complete)]),
    ]
    for name, expect, records in MIDTURN:
        if not h.wanted(name):
            continue
        verdict, got_signal, _ = h.run(name, records)
        record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))

    # The attribution-trailer veto. Every positive case is a message that would OTHERWISE allow, because
    # overriding an earned stop is the entire point -- a veto the disjunction can route around is not a
    # veto. The negatives carry the real load: this detector reads raw text with fences left in, so the
    # risk is not that it misses a trailer but that it fires while the rule is merely being discussed,
    # which is exactly what a session correcting the mistake spends its time doing.
    committed = FENCE + "\nFix the thing\n\nA body line.\n\nCo-Authored-By: Claude Opus 5 <x@y>\n" + FENCE
    session_url = (FENCE + "\nSubject line\n\nClaude-Session: https://claude.ai/code/session_abc\n"
                   + FENCE)
    TRAILERS = [
        ("trailer-in-a-fence-overrides-an-earned-stop", "block", "block:trailer:1",
         "Here is the message.\n\n" + committed + "\n\nShall I continue?"),
        ("session-url-trailer-is-caught-too", "block", "block:trailer:1",
         "Message:\n\n" + session_url + "\n\nWant me to go on?"),
        ("indented-trailer-still-counts", "block", "block:trailer:1",
         "Message:\n\n    Co-Authored-By: Claude Opus 5 <x@y>\n\nShall I go on?"),
        ("discussing-the-rule-inline-is-not-a-trailer", "allow", "allow:question-mark",
         "The memory says never append `Co-Authored-By: Claude ...` or "
         "`Claude-Session: https://...` here. Should I strip them?"),
        ("a-list-item-naming-the-trailer-is-not-one", "allow", "allow:question-mark",
         "Findings:\n- `Co-Authored-By: Claude` appeared in four messages.\nWhat next?"),
        ("an-unrelated-coauthor-line-is-not-ours", "allow", "allow:question-mark",
         "The upstream commit reads:\n\nCo-Authored-By: Dependabot <bot@github.com>\n\nMerge it?"),
    ]
    for name, expect, want_signal, text in TRAILERS:
        if not h.wanted(name):
            continue
        if expect == "block" and not gate.TRAILER_VETO_ENABLED:
            skip(name, TRAILER_OFF)
            skip("via-" + name, TRAILER_OFF)
            continue
        verdict, got_signal, _ = h.run(name, [user("draft the commit message"), assistant(text)])
        record(name, verdict == expect, "{0} {1}".format(verdict, got_signal))
        record("via-" + name, got_signal == want_signal,
               "expected={0} actual={1}".format(want_signal, got_signal))

    # The session-name veto: a stop that names a session by its crgolden-* name instead of its session id is refused
    # even when it would otherwise allow, and a crgolden name that is no session's, a session id, a double-quoted
    # quote of someone's words and the operator's grant all pass.
    SESSION_NAMES = [
        ("session-name-must-block-a-peer-named-by-its-name", "block", "block:session-name:1",
         "crgolden-3c's gate passed. Shall I run mine?"),
        ("session-name-must-block-a-name-in-backticks", "block", "block:session-name:1",
         "The peer `crgolden-76` is queued. Shall I wait?"),
        ("session-name-must-block-the-sessions-own-name-as-a-signature", "block", "block:session-name:1",
         "crgolden-7a here: finished. Want me to go on?"),
        ("session-name-must-block-a-name-a-hyphen-follows", "block", "block:session-name:1",
         "The crgolden-3c-tools gate passed. Shall I run mine?"),
        ("session-name-control-a-session-id-passes", "allow", "allow:question-mark",
         "Session 20f7e801's gate passed. Shall I run mine?"),
        ("session-name-control-app-and-folder-names-are-no-session-names", "allow", "allow:question-mark",
         "crgolden-curator and crgolden-gates are fine, and so is crgolden.com. Shall I deploy?"),
        ("session-name-control-a-double-quoted-quote-of-the-operator-keeps-its-words", "allow", "allow:question-mark",
         "You wrote \"crgolden-3c is auditing you\", and session 220995fd is the reviewer. Shall I reply?"),
    ]
    for name, expect, want_signal, text in SESSION_NAMES:
        if not h.wanted(name):
            continue
        verdict, got_signal, _ = h.run(name, [user("report the peers"), assistant(text)])
        record(name, verdict == expect and got_signal == want_signal, "{0} {1}".format(verdict, got_signal))
    if h.wanted("session-name-the-operators-grant-outranks-the-veto"):
        verdict, got_signal, _ = h.run("session-name-grant",
                                       [user("that is enough for now, you can stop"),
                                        assistant("crgolden-3c is still gating.")])
        record("session-name-the-operators-grant-outranks-the-veto", verdict == "allow", got_signal)

    # The control for the OFF state, so the flag discriminates in both directions: with the veto off a
    # trailer must pass straight through on the stop it otherwise earned. Flipping the flag on turns
    # this case red and the three skipped block cases live.
    if not gate.TRAILER_VETO_ENABLED and h.wanted("trailer-veto-disabled-lets-a-trailer-through"):
        verdict, got_signal, _ = h.run(
            "trailer-veto-off",
            [user("draft the commit message"),
             assistant("Here is the message.\n\n" + committed + "\n\nShall I continue?")])
        record("trailer-veto-disabled-lets-a-trailer-through",
               verdict == "allow" and got_signal == "allow:question-mark",
               "{0} {1}".format(verdict, got_signal))

    # The commit-state veto. It needs REAL repos because dirty_paths shells out to real git, and a
    # stubbed one would be testing the stub. Every case is a message that would otherwise allow, and
    # since 2026-09-09 every block case CLAIMS DONE: -- the veto is owed on a completion claim, not on
    # every dirty turn, and the first two negatives below pin that narrowing from both sides. Since the
    # same day the block is judged by CONTENT against git's current answer: it must name a file that is
    # dirty now, and naming a file the session wrote that is clean now is stale. Presence of a fence was
    # the whole test before, and agents learned to re-paste one turn after turn.
    if h.wanted("merged-branch"):
        merged_branch_cases(h)

    if h.wanted("commitblock"):
        ws = os.path.join(h.root, "ws")
        os.makedirs(ws, exist_ok=True)
        dirty_path, dirty = make_repo(ws, "DirtyRepo", {"file.txt": "dirty"})
        clean_path, clean = make_repo(ws, "CleanRepo", {"file.txt": "clean"})
        # One file the session wrote and the operator has since committed, one still dirty.
        mixed_path, mixed = make_repo(ws, "MixedRepo", {"src/landed.py": "clean", "src/open.py": "dirty"})
        # The operator's own dirty file beside a clean one the session wrote.
        shared_path, shared = make_repo(ws, "SharedRepo", {"theirs.md": "dirty", "mine.md": "clean"})
        second_path, second = make_repo(ws, "SecondRepo", {"lib/other.ts": "dirty"})
        fresh_path, fresh = make_repo(ws, "FreshRepo", {"brand-new.txt": "new"})
        _, new_dir = make_repo(ws, "NewDirRepo", {"skills/parking-lot/SKILL.md": "new"})
        # One file name in three repos the session edits: still dirty, committed, and dirty again.
        _, named_dirty = make_repo(ws, "ChurchesLike", {"TESTING.md": "dirty"})
        _, named_clean = make_repo(ws, "LibrarianLike", {"TESTING.md": "clean"})
        _, named_other = make_repo(ws, "ModulesLike", {"TESTING.md": "dirty"})

        # The session's edit to both files was committed, and a peer has dirtied them since.
        _, landed = make_repo(ws, "LandedRepo", {"peer.ts": "dirty", "other.ts": "dirty"})

        def commit_case(name, edited, text, expect, want_prefix, seed_state=None, tool_records=None,
                        seed_written=None):
            if expect == "block" and not gate.COMMIT_BLOCK_VETO_ENABLED:
                skip(name, COMMIT_BLOCK_OFF)
                return None, None
            # A tool call either way, so a DONE:/VERIFIED: message clears the work-complete floor and
            # the verdict is the veto's alone.
            if tool_records is None:
                files = edited if isinstance(edited, (list, tuple)) else ([edited] if edited else [])
                tool_records = [edit_use(f) for f in files] or [tool_use()]
            recs = [user("do the work")] + tool_records + [assistant(text)]
            path = h.transcript(name, recs)
            state = h.state_dir(name)
            if seed_state is not None:
                os.makedirs(state, exist_ok=True)
                with open(os.path.join(state, name + ".repos.json"), "w", encoding="utf-8") as fh:
                    json.dump(seed_state, fh)
            if seed_written is not None:
                os.makedirs(state, exist_ok=True)
                with open(os.path.join(state, name + ".written.json"), "w", encoding="utf-8") as fh:
                    json.dump(seed_written, fh)
            payload = {"hook_event_name": "Stop", "transcript_path": path, "session_id": name,
                       "stop_hook_active": False, "last_assistant_message": text}
            h.declare_closed_for_done(path, text)
            signal, out = gate.decide(payload, path, state, 100, workspace=ws, git_timeout=None,
                                      gate_workspace=h.no_gated_repos(), temp_root=os.path.join(h.root, "temp-claude"),
                                      require_parked=False)
            verdict = "block" if out else "allow"
            ok = verdict == expect and signal.startswith(want_prefix)
            record(name, ok, "{0} {1}".format(verdict, signal))
            return out, state

        def block_for(*paths):
            listed = "\n".join("  " + p for p in paths)
            return "```\nFix the thing\n\nA body line.\n\nFiles:\n" + listed + "\n```"

        done = "DONE: the work is finished.\nVERIFIED: the harness printed all cases passed."
        fenced = done + "\n\nHere it is.\n\n" + block_for("file.txt")
        # The narrowing itself. A dirty repo and no block at all, but no DONE: line either: the stop
        # earned by the question stands. "done" in prose is not the token.
        commit_case("dirty-repo-with-no-done-claim-owes-nothing", dirty["file.txt"],
                    "All done here, nothing left. Anything else?", "allow", "allow:question-mark")
        # A DONE: quoted inside a fence is not a claim, so the unquoted pointer in the prose -- which
        # would convict a real claim -- convicts nothing.
        commit_case("a-done-claim-inside-a-fence-does-not-make-the-block-due", dirty["file.txt"],
                    "The gate wants this shape:\n\n```\n" + done + "\n```\n\nThe commit message is in "
                    "the scratchpad. Shall I write it that way?", "allow", "allow:question-mark")
        commit_case("dirty-repo-claiming-done-with-no-commit-block-blocks", dirty["file.txt"],
                    done, "block", "block:commitblock:")
        commit_case("a-block-naming-the-dirty-file-satisfies-the-owed-message", dirty["file.txt"],
                    fenced, "allow", "allow:work-complete")
        # A fence used to be enough. This is the fixture that passed before and must not again: a block
        # with no file-list line names nothing, so it covers nothing.
        commit_case("a-block-that-names-no-file-is-not-a-delivery", dirty["file.txt"],
                    done + "\n\nHere it is.\n\n```\nFix the thing\n\nA body line.\n```",
                    "block", "block:commitblock:")
        commit_case("a-path-inside-a-sentence-is-not-a-file-list", dirty["file.txt"],
                    done + "\n\n```\nFix the thing\n\nThis touches file.txt in passing.\n```",
                    "block", "block:commitblock:")
        commit_case("a-workspace-relative-path-still-names-the-file", dirty["file.txt"],
                    done + "\n\n" + block_for("DirtyRepo/file.txt"), "allow", "allow:work-complete")
        commit_case("a-files-label-on-the-same-line-still-counts", dirty["file.txt"],
                    done + "\n\n```\nFix the thing\n\nFiles: file.txt\n```", "allow", "allow:work-complete")
        commit_case("pointing-at-a-previous-message-is-not-a-delivery", dirty["file.txt"],
                    fenced + "\n\nTools still holds work, with the commit messages given in my "
                             "previous message.", "block", "block:commitblock:")
        commit_case("scratchpad-pointer-is-not-a-delivery", dirty["file.txt"],
                    fenced + "\n\nThe commit message is in the scratchpad.",
                    "block", "block:commitblock:")
        # The false positive this veto shipped with, caught in production one turn after it went live:
        # the commit message INTRODUCING the veto quotes the phrases it catches, and the raw-text search
        # convicted it. Naming a phrase is not using it.
        commit_case("a-pointer-quoted-inside-the-commit-body-is-not-a-pointer", dirty["file.txt"],
                    done + "\n\nHere it is.\n\n```\nBlock a stop that owes a commit message\n\n"
                    "Catches delivery by reference -- \"the commit messages given in my previous "
                    "message\", \"drafted in the scratchpad\" -- which the rule already names.\n\n"
                    "Files:\n  file.txt\n```", "allow", "allow:work-complete")
        commit_case("a-pointer-quoted-in-prose-is-not-a-pointer", dirty["file.txt"],
                    fenced + "\n\nThe veto catches \"the commit message is in the scratchpad\" "
                             "and similar phrasings.", "allow", "allow:work-complete")
        commit_case("a-pointer-in-curly-quotes-is-not-a-pointer", dirty["file.txt"],
                    fenced + "\n\nThe veto catches “the commit message is in the scratchpad” "
                             "and similar phrasings.", "allow", "allow:work-complete")
        commit_case("an-unquoted-pointer-in-prose-still-blocks", dirty["file.txt"],
                    fenced + "\n\nTools still holds work, with the commit messages given in my "
                             "previous message.", "block", "block:commitblock:")
        # QUEUED: owes no commit block (narrowed to DONE: alone 2026-09-20): a queued session re-posting
        # its blocks on every wait turn was the re-paste the content check exists to stop.
        queued = "Finished.\n\nQUEUED: behind session 4f1c2a9b's gate, which will message me when it prints."
        commit_case("queued-with-a-dirty-repo-and-no-commit-message-allows", None, queued, "allow",
                    "allow:queued-for-gate", tool_records=[edit_use(dirty["file.txt"]), send_use()])
        commit_case("queued-with-a-stale-commit-message-is-not-judged", None,
                    queued + "\n\n" + block_for("file.txt"), "allow", "allow:queued-for-gate",
                    tool_records=[edit_use(clean["file.txt"]), send_use()])
        commit_case("queued-with-the-commit-message-delivered-allows", None,
                    queued + "\n\n" + block_for("file.txt"), "allow", "allow:queued-for-gate",
                    tool_records=[edit_use(dirty["file.txt"]), send_use()])
        commit_case("waiting-with-a-dirty-repo-and-no-commit-message-allows", None,
                    "Finished.\n\nWAITING: on my own gate, running in the background.", "allow",
                    "allow:waiting-on-own-gate", tool_records=[edit_use(dirty["file.txt"])] + launch)
        commit_case("a-clean-repo-owes-no-message", clean["file.txt"],
                    done, "allow", "allow:work-complete")
        commit_case("a-repo-this-session-never-touched-is-not-checked", None,
                    done, "allow", "allow:work-complete")

        # STALENESS. The session wrote landed.py and open.py; the operator has committed landed.py.
        # A block that still lists it is the re-posted block the operator keeps receiving.
        both = [mixed["src/landed.py"], mixed["src/open.py"]]
        stale_out, _ = commit_case("a-block-for-a-committed-file-is-stale", both,
                                   done + "\n\n" + block_for("src/landed.py"), "block", "block:stale:")
        commit_case("a-block-naming-a-committed-file-beside-a-dirty-one-is-stale", both,
                    done + "\n\n" + block_for("src/landed.py", "src/open.py"), "block", "block:stale:")
        commit_case("a-block-naming-only-the-still-dirty-file-allows", both,
                    done + "\n\n" + block_for("src/open.py"), "allow", "allow:work-complete")
        commit_case("dropping-the-block-for-the-still-dirty-file-is-missing-not-stale", both,
                    done, "block", "block:commitblock:")
        # The whole repo is clean now: the session wrote file.txt, the operator committed it, and the
        # next DONE: turn pastes the block again. Nothing is owed, and the block is stale.
        commit_case("a-block-for-a-repo-that-is-clean-now-is-stale", clean["file.txt"],
                    done + "\n\n" + block_for("file.txt"), "block", "block:stale:")
        if stale_out:
            record("the-stale-reason-names-the-committed-file",
                   "src/landed.py" in stale_out and "MixedRepo" in stale_out and "clean now" in stale_out,
                   "reason names the file and the repo")
            record("the-stale-reason-also-lists-what-is-owed-now",
                   "src/open.py" in stale_out and "owed now" in stale_out, "reason lists the owed file")

        # WHOSE FILES. The operator's own dirty file in a tree the session only read, or wrote
        # elsewhere in, is not the session's to message.
        commit_case("the-operators-dirty-file-is-not-owed", shared["mine.md"],
                    done, "allow", "allow:work-complete")
        commit_case("a-file-only-read-this-session-owes-nothing", None, done, "allow",
                    "allow:work-complete", tool_records=[read_use(dirty["file.txt"])])
        commit_case("a-write-tool-counts-like-an-edit", None, done, "block", "block:commitblock:",
                    tool_records=[edit_use(dirty["file.txt"], tool="Write")])
        commit_case("an-untracked-new-file-is-owed-too", fresh["brand-new.txt"],
                    done, "block", "block:commitblock:")
        commit_case("a-block-naming-the-untracked-file-allows", fresh["brand-new.txt"],
                    done + "\n\n" + block_for("brand-new.txt"), "allow", "allow:work-complete")
        commit_case("an-untracked-file-inside-a-new-directory-is-owed", new_dir["skills/parking-lot/SKILL.md"],
                    done, "block", "block:commitblock:")
        commit_case("a-block-naming-a-file-inside-a-new-directory-allows-and-is-not-stale",
                    new_dir["skills/parking-lot/SKILL.md"],
                    done + "\n\n" + block_for("skills/parking-lot/SKILL.md"), "allow", "allow:work-complete")

        # LANDED WORK. The session's edit was committed and a peer has dirtied the same file since: the
        # dirt is the peer's, so a DONE: owes nothing for it. Before 2026-09-17 every file the session ever
        # wrote was owed whenever it was dirty, and a session whose Librarian edit was already in HEAD was
        # blocked on DONE: for another session's uncommitted diff with no truthful block it could write.
        landed_seed = {"repos": ["LandedRepo"], "paths": {"LandedRepo": ["peer.ts"]}}
        head_written = {"LandedRepo": {"peer.ts": hashlib.sha256(b"committed\n").hexdigest()}}
        commit_case("a-landed-edit-a-peer-dirtied-since-owes-nothing", None, done, "allow",
                    "allow:work-complete", seed_state=landed_seed, seed_written=head_written)
        commit_case("the-same-dirt-with-no-recorded-write-is-still-owed", None, done, "block",
                    "block:commitblock:", seed_state=landed_seed)
        commit_case("a-recorded-write-that-never-landed-is-still-owed", None, done, "block",
                    "block:commitblock:", seed_state=landed_seed,
                    seed_written={"LandedRepo": {"peer.ts": hashlib.sha256(b"not in head\n").hexdigest()}})
        _, again_state = commit_case("a-landed-file-the-session-edits-again-is-owed-again", None, done, "block",
                                     "block:commitblock:", seed_state=landed_seed, seed_written=head_written,
                                     tool_records=[edit_use(landed["peer.ts"])])
        if again_state:
            with open(landed["peer.ts"], "rb") as fh:
                working = hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()
            stored = {}
            recorded = os.path.join(again_state, "a-landed-file-the-session-edits-again-is-owed-again.written.json")
            if os.path.exists(recorded):
                with open(recorded, encoding="utf-8") as fh:
                    stored = json.load(fh)
            record("the-written-state-records-what-the-new-edit-holds",
                   stored.get("LandedRepo", {}).get("peer.ts") == working, json.dumps(stored))
        history_path, history = make_repo(ws, "HistoryRepo", {"gate.ps1": "clean", "keep.ps1": "clean"})
        quiet = {"capture_output": True, "text": True, "cwd": history_path}
        for name in ("gate.ps1", "keep.ps1"):
            with open(history[name], "a", encoding="utf-8") as fh:
                fh.write("a later commit\n")
        subprocess.run(["git", "commit", "-am", "later"], **quiet)
        with open(history["gate.ps1"], "a", encoding="utf-8") as fh:
            fh.write("a third writer's uncommitted line\n")
        with open(history["keep.ps1"], "w", encoding="utf-8") as fh:
            fh.write("committed\n")
        session_write = {"HistoryRepo": {"gate.ps1": hashlib.sha256(b"committed\n").hexdigest()}}
        commit_case("a-write-that-landed-before-a-later-commit-owes-a-third-writers-dirt", None, done, "allow",
                    "allow:work-complete", seed_state={"repos": ["HistoryRepo"], "paths": {"HistoryRepo": ["gate.ps1"]}},
                    seed_written=session_write)
        commit_case("control-a-working-copy-still-holding-the-sessions-write-is-owed", None, done, "block",
                    "block:commitblock:", seed_state={"repos": ["HistoryRepo"], "paths": {"HistoryRepo": ["keep.ps1"]}},
                    seed_written={"HistoryRepo": {"keep.ps1": hashlib.sha256(b"committed\n").hexdigest()}})
        neighbour_out, _ = commit_case(
            "a-landed-file-does-not-discharge-its-neighbour", None, done, "block", "block:commitblock:",
            seed_state={"repos": ["LandedRepo"], "paths": {"LandedRepo": ["peer.ts", "other.ts"]}},
            seed_written=head_written)
        if neighbour_out:
            record("the-owed-reason-leaves-out-the-landed-file",
                   "other.ts" in neighbour_out and "peer.ts" not in neighbour_out, "reason names other.ts only")

        # SEVERAL REPOS. One block per repo, and the reason names the one that is missing.
        two = [dirty["file.txt"], second["lib/other.ts"]]
        missing_out, _ = commit_case("two-dirty-repos-with-one-block-still-owe-the-other", two,
                                     fenced, "block", "block:commitblock:")
        commit_case("two-dirty-repos-with-both-blocks-allow", two,
                    fenced + "\n\n" + block_for("lib/other.ts"), "allow", "allow:work-complete")
        if missing_out:
            record("the-missing-reason-names-the-uncovered-repo-and-file",
                   "SecondRepo" in missing_out and "lib/other.ts" in missing_out
                   and "no block names them" in missing_out and "DirtyRepo" in missing_out,
                   "reason names SecondRepo and lib/other.ts")

        # STATE. A session remembered by an older gate has repo names and no paths: the whole tree is
        # owed as before and nothing can be called stale. A repo git cannot read yields no verdict,
        # never a "clean" that would convict a fresh block as stale.
        commit_case("legacy-state-without-paths-owes-the-whole-repo", None, done, "block",
                    "block:commitblock:", seed_state={"repos": ["DirtyRepo"]})
        commit_case("legacy-state-is-satisfied-by-a-block-naming-any-dirty-file", None,
                    done + "\n\n" + block_for("file.txt"), "allow", "allow:work-complete",
                    seed_state={"repos": ["DirtyRepo"]})
        _, ghost_state = commit_case("a-repo-git-cannot-read-is-neither-owed-nor-stale", None,
                                     done + "\n\n" + block_for("file.txt"), "allow", "allow:work-complete",
                                     seed_state={"repos": ["GhostRepo"], "paths": {"GhostRepo": ["file.txt"]}})
        # Passing it silently is deliberate; passing it without a trace is not.
        if ghost_state:
            facts_log = os.path.join(ghost_state, "facts.log")
            logged = ""
            if os.path.exists(facts_log):
                with open(facts_log, encoding="utf-8") as fh:
                    logged = fh.read()
            record("a-repo-git-cannot-read-is-logged-as-unjudged", "commitblock:unjudged=GhostRepo" in logged,
                   "facts.log names GhostRepo as unjudged")

        # WHICH REPO. Path endings alone cannot say which repo a bare name means, so a block listing
        # TESTING.md for the dirty repo was convicted as stale for another repo holding a committed one.
        pair = [named_dirty["TESTING.md"], named_clean["TESTING.md"]]
        ambiguous_out, _ = commit_case("a-bare-name-in-two-edited-repos-counts-for-neither", pair,
                                       done + "\n\n" + block_for("TESTING.md"), "block", "block:commitblock:")
        commit_case("a-repo-qualified-name-covers-its-repo-and-is-not-stale-for-another", pair,
                    done + "\n\n" + block_for("ChurchesLike/TESTING.md"), "allow", "allow:work-complete")
        commit_case("a-block-for-one-repo-does-not-cover-the-same-name-in-another",
                    [named_dirty["TESTING.md"], named_other["TESTING.md"]],
                    done + "\n\n" + block_for("ChurchesLike/TESTING.md"), "block", "block:commitblock:")
        # The workspace root is a container with no .git, so its AGENTS docs belong to no repo: an edit there
        # owes nothing and cannot become an ambiguous candidate beside a repo's file of the same name.
        container_doc = session_home.workspace_doc("AGENTS/TESTING.md", ws, must_exist=False)
        os.makedirs(os.path.dirname(container_doc), exist_ok=True)
        with open(container_doc, "w", encoding="utf-8") as fh:
            fh.write("workspace doc\n")
        commit_case("a-file-outside-any-repo-does-not-make-a-name-ambiguous",
                    [container_doc, named_dirty["TESTING.md"]],
                    done + "\n\n" + block_for("TESTING.md"), "allow", "allow:work-complete")
        # The opposite-sign control from the same edits: a gate that stopped recognising repos at all would
        # pass the case above, and must fail this one, which still owes the real repo's block.
        container_out, _ = commit_case("the-same-edits-with-no-block-still-owe-only-the-real-repo",
                                       [container_doc, named_dirty["TESTING.md"]],
                                       done, "block", "block:commitblock:")
        if container_out:
            record("the-owed-reason-names-the-repo-and-not-the-container",
                   "ChurchesLike" in container_out and "AGENTS" not in container_out,
                   "reason names ChurchesLike only")
        if ambiguous_out:
            record("the-ambiguous-reason-names-the-qualified-spelling",
                   "ambiguous" in ambiguous_out and "ChurchesLike/TESTING.md" in ambiguous_out,
                   "reason asks for ChurchesLike/TESTING.md")
        commit_case("paths-remembered-in-an-earlier-turn-are-still-judged", None,
                    done + "\n\n" + block_for("src/landed.py"), "block", "block:stale:",
                    seed_state={"repos": ["MixedRepo"], "paths": {"MixedRepo": ["src/landed.py"]}})
        _, state_dir = commit_case("the-state-file-records-the-paths-written", both,
                                   done + "\n\n" + block_for("src/open.py"), "allow", "allow:work-complete")
        if state_dir:
            try:
                with open(os.path.join(state_dir, "the-state-file-records-the-paths-written.repos.json"),
                          encoding="utf-8") as fh:
                    stored = json.load(fh)
                paths = stored.get("paths", {}).get("MixedRepo", [])
                record("the-state-file-records-the-paths-written-check",
                       sorted(paths) == ["src/landed.py", "src/open.py"] and stored.get("repos") == ["MixedRepo"],
                       json.dumps(stored))
            except Exception as exc:
                record("the-state-file-records-the-paths-written-check", False, repr(exc))

        # PURE FUNCTIONS. porcelain parsing and path matching, where a wrong answer would surface only
        # as a mysterious stale or missing verdict.
        parsed = gate.porcelain_paths(' M Claude/a.py\n?? new dir/b.txt\nR  old.md -> docs/new.md\n'
                                      'D  gone.py\n M "sp ace/c.txt"\n')
        record("porcelain-paths-parse-modified-untracked-renamed-deleted-quoted",
               parsed == ["Claude/a.py", "new dir/b.txt", "docs/new.md", "gone.py", "sp ace/c.txt"],
               str(parsed))
        record("same-file-matches-repo-relative-workspace-relative-and-bare-names",
               gate.same_file("Claude/a.py", "Tools/Claude/a.py") and gate.same_file("a.py", "Claude/a.py")
               and gate.same_file("Claude\\a.py", "Claude/a.py") and not gate.same_file("Claude/b.py", "Claude/a.py")
               and not gate.same_file("x/a.py", "y/a.py"), "component suffix match")
        named = gate.block_file_lines("```\nSubject\n\nBody mentions Claude/a.py here.\n\nFiles:\n  Claude/b.py\n"
                                      "- docs/c.md, docs/d.md\n`e.py`\nCovers: f/g.ts\n```\n\nprose Claude/h.py")
        record("block-file-lines-take-list-lines-and-skip-prose-and-sentences",
               sorted(named) == ["Claude/b.py", "docs/c.md", "docs/d.md", "e.py", "f/g.ts"], str(sorted(named)))
        # An extensionless dotfile is a real path. block_file_lines drops a whole line when ONE token
        # fails, so before this was accepted a list line naming `.gitattributes` beside a real path
        # contributed nothing and the gate reported "no block names them" about a file the block named.
        dotted = gate.block_file_lines("```\nSubject\n\nFiles: .gitattributes, e2e/layout.ts\n```")
        record("block-file-lines-keep-the-line-when-it-names-an-extensionless-dotfile",
               sorted(dotted) == [".gitattributes", "e2e/layout.ts"], str(sorted(dotted)))
        # The control of opposite sign: prose must still be rejected, so the widening cannot be a
        # blanket that turns every sentence into a file list.
        prose = gate.block_file_lines("```\nSubject\n\nThe branch omitted left, failing typecheck:e2e.\n```")
        record("block-file-lines-still-reject-a-prose-sentence", prose == [], str(prose))

        # The OFF-state control: the strongest positive fixture -- a dirty repo this session edited and
        # a DONE: with no block at all -- must allow while the flag is off. Flipping the flag on turns
        # this red and the skipped block cases live.
        if not gate.COMMIT_BLOCK_VETO_ENABLED:
            commit_case("commit-block-veto-disabled-lets-a-dirty-repo-through", dirty["file.txt"],
                        done, "allow", "allow:work-complete")

    # The goal veto. The operator pins the goal with a GOAL: line in THEIR message, and from then on a
    # DONE: line must restate it verbatim or the stop is vetoed whichever signal earned it. Every
    # positive case is a message that would otherwise allow. The negatives carry the load in the other
    # direction: a faithful echo must pass whatever its case and punctuation, a message with no DONE:
    # is not judged, and only the operator's own records can pin -- never gate feedback or a reminder.
    if h.wanted("goal"):
        # This goal opens with "nothing" ON PURPOSE. The first cut reused the NEEDED: negation pattern
        # to spot "GOAL: none" and read this whole pin as a retirement, so every block case allowed.
        pinned = user("GOAL: nothing outstanding from this session\n"
                      "let's address the items you called outstanding")
        subtask_done = ("Cleared the Tools divergence.\n\n"
                        "DONE: cleared the Tools divergence and confirmed no line-ending damage.\n"
                        "VERIFIED: `git merge-tree --write-tree` exit 0, CRLF=0 in both copies.")
        goal_done = ("Everything is closed.\n\n"
                     "DONE: nothing outstanding from this session -- all seven items shipped.\n"
                     "VERIFIED: `git status --porcelain` empty in all four repos, walkers green.")
        goal_command_status = {"type": "attachment", "attachment": {
            "type": "goal_status", "met": False, "condition": "nothing outstanding from this session"}}

        def goal_case(name, records, expect, want_prefix, **kw):
            verdict, got_signal, _ = h.run(name, records, **kw)
            ok = verdict == expect and got_signal.startswith(want_prefix)
            record(name, ok, "{0} {1}".format(verdict, got_signal))

        goal_case("goal-pinned-and-done-names-a-subtask-blocks",
                  [pinned, tool_use(), assistant(subtask_done)], "block", "block:goal:")
        goal_case("goal-pinned-and-done-echoes-the-goal-allows",
                  [pinned, tool_use(), assistant(goal_done)], "allow", "allow:work-complete")
        goal_case("the-echo-survives-case-quotes-and-punctuation",
                  [pinned, tool_use(), assistant(
                      "Closed.\n\nDONE: \"Nothing Outstanding, from this session!\" -- checked twice.\n"
                      "VERIFIED: `git status` clean.")], "allow", "allow:work-complete")
        goal_case("a-partial-echo-is-not-an-echo",
                  [pinned, tool_use(), assistant(
                      "Closed.\n\nDONE: nothing outstanding on the Tools side.\n"
                      "VERIFIED: `git status` clean.")], "block", "block:goal:")
        goal_case("no-goal-pinned-leaves-completion-unchanged",
                  [user("do the work"), tool_use(), assistant(subtask_done)],
                  "allow", "allow:work-complete")
        # The veto property: a mismatched claim is caught even when a question earned the stop.
        goal_case("goal-veto-overrides-a-stop-earned-elsewhere",
                  [pinned, tool_use(), assistant(subtask_done + "\n\nShall I continue with the next repo?")],
                  "block", "block:goal:")
        goal_case("a-question-that-omits-the-pinned-goal-is-judged-against-it",
                  [pinned, tool_use(), assistant(
                      "Cleared the Tools divergence; CRLF=0 in both copies.\n\nShall I continue with the next repo?")],
                  "block", "block:goal:")
        goal_case("a-question-restating-the-pinned-goal-allows",
                  [pinned, tool_use(), assistant(
                      "Cleared the Tools divergence; CRLF=0 in both copies.\n\n"
                      "GOAL: nothing outstanding from this session\nShall I continue with the next repo?")],
                  "allow", "allow:question-mark")
        goal_case("commit-ask-a-closing-shall-i-push-it-with-no-commit-message-blocks",
                  [user("do the work"), tool_use(), assistant(
                      "Cleared the Tools divergence; CRLF=0 in both copies.\n\nShall I push it?")],
                  "block", "block:commit-ask:")
        goal_case("goal-in-gate-feedback-is-not-the-operators",
                  [user("do the work"), assistant("Working."),
                   user("Stop hook feedback: STOP GATE: keep working.\nGOAL: nothing outstanding"),
                   tool_use(), assistant(subtask_done)], "allow", "allow:work-complete")
        goal_case("goal-in-a-system-reminder-is-not-the-operators",
                  [user("do the work"), tool_use(),
                   user("<system-reminder>GOAL: nothing outstanding</system-reminder>"),
                   assistant(subtask_done)], "allow", "allow:work-complete")
        goal_case("goal-riding-a-tool-result-mid-turn-still-pins",
                  [user("do the work"), tool_use(),
                   tool_result("GOAL: nothing outstanding from this session"),
                   assistant(subtask_done)], "block", "block:goal:")
        goal_case("newest-goal-wins",
                  [user("GOAL: ship the walker fix"), assistant("Working."), pinned, tool_use(),
                   assistant("Shipped.\n\nDONE: ship the walker fix.\nVERIFIED: run 1 green.")],
                  "block", "block:goal:")
        goal_case("a-goal-mid-sentence-is-not-a-pin",
                  [user("your GOAL: nothing outstanding from this session"), tool_use(), assistant(subtask_done)],
                  "allow", "allow:work-complete")
        goal_case("a-lowercase-goal-opening-a-line-is-not-a-pin",
                  [user("goal: nothing outstanding from this session"), tool_use(), assistant(subtask_done)],
                  "allow", "allow:work-complete")
        goal_case("a-goal-line-ending-in-crlf-pins",
                  [user("GOAL: nothing outstanding from this session\r\nlet's address the items"), tool_use(),
                   assistant(subtask_done)], "block", "block:goal:")
        goal_case("a-done-with-the-goal-only-on-a-goal-line-beside-it-blocks",
                  [user("GOAL: ship the walker fix"), tool_use(),
                   assistant("GOAL: ship the walker fix\n\nShipped.\n\nDONE: the walker.\nVERIFIED: run 1 green.")],
                  "block", "block:goal:")
        goal_case("control-a-done-restating-the-goal-in-its-own-line-allows",
                  [user("GOAL: ship the walker fix"), tool_use(),
                   assistant("Shipped.\n\nDONE: ship the walker fix.\nVERIFIED: run 1 green.")],
                  "allow", "allow:work-complete")
        goal_set = [dict(goal_command_status, attachment=dict(goal_command_status["attachment"], sentinel=True)),
                    dict(user("A session-scoped Stop hook is now active."), isMeta=True),
                    dict(user("Goal set: nothing outstanding from this session", prompt_id="p-goal"),
                         origin={"kind": "auto-continuation"}, promptSource="system")]
        goal_case("an-active-goal-command-is-the-goal",
                  [user("do the work")] + goal_set + [tool_use(), assistant(subtask_done)], "block", "block:goal:")
        goal_case("a-done-restating-the-active-goal-command-allows",
                  [user("do the work")] + goal_set + [tool_use(), assistant(goal_done)],
                  "allow", "allow:work-complete")
        goal_case("an-active-goal-command-outranks-a-later-goal-line",
                  goal_set + [user("GOAL: ship the walker fix", prompt_id="p-2"), tool_use(),
                              assistant("Shipped.\n\nDONE: ship the walker fix.\nVERIFIED: run 1 green.")],
                  "block", "block:goal:")
        goal_case("a-met-goal-command-hands-back-to-the-goal-line",
                  [user("GOAL: ship the walker fix")] + goal_set
                  + [dict(goal_command_status, attachment=dict(goal_command_status["attachment"], met=True)),
                     tool_use(), assistant("Shipped.\n\nDONE: ship the walker fix.\nVERIFIED: run 1 green.")],
                  "allow", "allow:work-complete")
        goal_case("control-the-goal-line-is-not-the-goal-while-the-goal-command-is-active",
                  [user("GOAL: ship the walker fix")] + goal_set
                  + [tool_use(), assistant("Shipped.\n\nDONE: ship the walker fix.\nVERIFIED: run 1 green.")],
                  "block", "block:goal:")
        goal_case("goal-command-text-typed-by-the-operator-is-not-a-goal-command",
                  [user('{"type": "goal_status", "met": false, "condition": "nothing outstanding from this '
                        'session"}'), tool_use(), assistant(subtask_done)], "allow", "allow:work-complete")
        goal_case("goal-in-a-quoted-message-is-not-the-operators",
                  [user('a peer wrote: "two scripts ran.\n\nNeither carried the GOAL: line it needs" - look into it'),
                   tool_use(), assistant(subtask_done)], "allow", "allow:work-complete")
        # The operator quoted a session's question that itself quoted a declared item: nested quotes put an even
        # count of marks before the GOAL:, and parity alone pinned the fragment as the session's goal.
        goal_case("goal-inside-nested-quotes-is-not-the-operators",
                  [user('this is a contrived question: "Is that acceptable for the declared item "A session\'s GOAL: '
                        'is required in its original prompt", with pre-rule sessions asking?"'),
                   tool_use(), assistant(subtask_done)], "allow", "allow:work-complete")
        goal_case("goal-named-as-a-term-after-an-article-is-not-a-pin",
                  [user("items added after the declaration from the GOAL: and any edit to it after that need strict "
                        "approval"), tool_use(), assistant(subtask_done)], "allow", "allow:work-complete")
        goal_case("goal-inside-inline-code-mid-sentence-is-not-a-pin",
                  [user("Does its prompt start with a stated `GOAL:`? Does it create a work product item declaration "
                        "list?"), tool_use(), assistant(subtask_done)], "allow", "allow:work-complete")
        goal_case("goal-inside-inline-code-with-text-after-it-is-not-a-pin",
                  [user("the pin is written as `GOAL: <the goal>` on its own line"), tool_use(), assistant(subtask_done)],
                  "allow", "allow:work-complete")
        goal_case("control-a-line-opening-goal-pins-beside-inline-code-elsewhere",
                  [user("see `gate.ps1` first\nGOAL: nothing outstanding from this session"), tool_use(),
                   assistant(subtask_done)], "block", "block:goal:")
        goal_case("a-stray-quote-mark-before-a-pin-does-not-silence-it",
                  [user('the 27" monitor is on order.\nGOAL: nothing outstanding from this session'),
                   tool_use(), assistant(subtask_done)], "block", "block:goal:")
        goal_case("goal-in-a-peer-session-message-is-not-the-operators",
                  [user('<cross-session-message from="uds:peer" from-name="crgolden-22">\nwhile one is pinned, '
                        'a gate script must restate it on a GOAL: line. I will say when it finishes.\n'
                        '</cross-session-message>'), tool_use(), assistant(subtask_done)],
                  "allow", "allow:work-complete")
        goal_case("goal-in-a-compaction-summary-is-not-the-operators",
                  [dict(user("Summary:\nGOAL: nothing outstanding from this session"), isCompactSummary=True),
                   tool_use(), assistant(subtask_done)], "allow", "allow:work-complete")
        goal_case("lowercase-goal-mid-sentence-is-not-a-pin",
                  [user("the goal: nothing outstanding from this session"), tool_use(), assistant(subtask_done)],
                  "allow", "allow:work-complete")
        # QUEUED: claims the whole work product is finished, so a pinned goal must be restated on it too.
        goal_case("queued-under-a-pinned-goal-it-does-not-restate-blocks",
                  [pinned, send_use(), assistant(
                      "Cleared the Tools divergence.\n\nQUEUED: behind session 4f1c2a9b's gate.")],
                  "block", "block:goal:")
        goal_case("queued-restating-the-pinned-goal-allows",
                  [pinned, send_use(), assistant(
                      "Everything is closed.\n\nQUEUED: nothing outstanding from this session but my own "
                      "gate, behind session 4f1c2a9b's.")], "allow", "allow:queued-for-gate")
        # WAITING: makes the same claim about the work product, so it owes the same restatement.
        goal_case("waiting-under-a-pinned-goal-it-does-not-restate-blocks",
                  [pinned] + launch + [assistant(
                      "Cleared the Tools divergence.\n\nWAITING: on my own Tools gate.")],
                  "block", "block:goal:")
        goal_case("waiting-restating-the-pinned-goal-allows",
                  [pinned] + launch + [assistant(
                      "Everything is closed.\n\nWAITING: nothing outstanding from this session but my own "
                      "gate, running in the background.")], "allow", "allow:waiting-on-own-gate")
        goal_case("grant-outranks-the-goal-veto",
                  [user("GOAL: nothing outstanding from this session\n"
                        "that is enough for now, you can stop"), tool_use(), assistant(subtask_done)],
                  "allow", "allow:operator-grant")
        goal_case("refusal-outranks-the-goal-veto",
                  [user("GOAL: nothing outstanding from this session\nlet's address the items you called outstanding\n"
                        "pushing is not authorised without my word"), tool_use(),
                   assistant("REFUSING: to push: \"pushing is not authorised without my word\".\n\n" + subtask_done)],
                  "allow", "allow:refusal-token")

        # The pin has to outlive the scan window, which halts at the newest prompt: pinned in one
        # turn, enforced in the next, through the session's own state file.
        state = os.path.join(h.root, "state-goal-persists")
        path = h.transcript("goal-persists", [pinned, assistant("Working through the list.")])
        first, _, _ = h.run_path("goal-persists", path, state=state)
        append(path, [user("what else is still outstanding?"), tool_use(), assistant(subtask_done)])
        second, got_signal, _ = h.run_path("goal-persists", path, state=state)
        record("goal-persists-across-turns-through-state",
               first == "block" and second == "block" and got_signal.startswith("block:goal:"),
               "{0},{1} {2}".format(first, second, got_signal))

        restated = "Finished.\n\nDONE: nothing outstanding from this session.\nVERIFIED: the suite printed all passed."
        for name, reader, expected in (
                ("goal-latched-by-an-older-pin-reader-is-re-derived", "older-reader", "allow:work-complete"),
                ("control-goal-latched-by-this-pin-reader-is-trusted", gate.pin_reader_digest(), "block:goal:")):
            if not h.wanted(name):
                continue
            latch_state = h.state_dir(name)
            os.makedirs(latch_state, exist_ok=True)
            latch_path = h.transcript(name, [user("GOAL: nothing outstanding from this session"), assistant("Working."),
                                             user("what else is left?", prompt_id="p-2"), tool_use(),
                                             assistant(restated)])
            with open(os.path.join(latch_state, name + ".goal.json"), "w", encoding="utf-8") as fh:
                json.dump({"reader": reader, "offset": os.path.getsize(latch_path), "command": None,
                           "text": "is required in its original prompt"}, fh)
            _, latch_signal, _ = h.run_path(name, latch_path, state=latch_state)
            record(name, latch_signal.startswith(expected), latch_signal)

        state = os.path.join(h.root, "state-goal-none")
        path = h.transcript("goal-none", [pinned, assistant("Working through the list.")])
        h.run_path("goal-none", path, state=state)
        append(path, [user("GOAL: none"), tool_use(), assistant(subtask_done)])
        verdict, got_signal, _ = h.run_path("goal-none", path, state=state)
        record("goal-none-retires-the-pin", verdict == "allow" and got_signal == "allow:work-complete",
               "{0} {1}".format(verdict, got_signal))

        path = h.transcript("goal-budget", [pinned, tool_use(), assistant(subtask_done)])
        state = os.path.join(h.root, "state-goal-budget")
        seq = []
        for _ in range(3):
            verdict, got_signal, _ = h.run_path("goal-budget", path, stop_hook_active=True, max_blocks=2,
                                                state=state)
            seq.append(verdict)
        record("goal-blocks-past-the-budget-still-block",
               seq == ["block", "block", "block"] and got_signal.startswith("block:" + gate.PAST_BUDGET_TAG),
               ",".join(seq) + " " + got_signal)

        # The reason quotes the goal mid-sentence and names no token at a line start, so an agent
        # echoing it earns nothing and the scan cannot mistake it for the operator pinning a goal.
        verdict, got_signal, _ = h.run("goal-echo", [
            pinned, tool_use(), assistant(gate.goal_reason("nothing outstanding from this session"))])
        record("echoing-the-goal-reason-earns-nothing", verdict == "block",
               "{0} {1}".format(verdict, got_signal))
        record("goal-reason-carries-the-operators-goal",
               "\"nothing outstanding from this session\"" in
               gate.goal_reason("nothing   outstanding\nfrom this session"), "normalised whitespace")
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stop-gate-rules.txt"),
                  encoding="utf-8") as fh:
            teaches_goal = "GOAL:" in fh.read()
        record("card-teaches-the-goal-token", teaches_goal, "ok" if teaches_goal else "not satisfied")
        own_grammar = hand_rolled_token_patterns(os.path.dirname(os.path.abspath(__file__)))
        record("every-token-pattern-comes-from-stop-tokens", not own_grammar, ", ".join(own_grammar) or "ok")

    # The gate-claim veto, against a REAL git repo and a gate record written the way GateCommon writes one,
    # because the record's manifest is compared with git's file list.
    if h.wanted("gateclaim"):
        statuses = gate.gate_status_module()

        def gated_workspace(name, verdict="PASSED", edit_after_record=False):
            ws = os.path.join(h.root, "gated-" + name)
            repo, files = make_repo(ws, "App", {"code.cs": "clean"})
            with open(os.path.join(repo, "gate.ps1"), "w", encoding="utf-8") as fh:
                fh.write("npm test\n")
            manifest = statuses.manifest(repo)
            tools = statuses.tool_hashes()
            os.makedirs(statuses.checkout_state_dir("App", repo))
            with open(statuses.record_path("App", statuses.checkout_state_dir("App", repo)), "w", encoding="utf-8") as fh:
                json.dump({"repo": "App", "verdict": verdict, "completedAt": "2026-01-01T00:05:00Z",
                           "machine": "fixture", "steps": [], "manifest": manifest, "tools": tools,
                           "fingerprint": statuses.fingerprint(manifest, tools)}, fh)
            if edit_after_record:
                with open(files["code.cs"], "a", encoding="utf-8") as fh:
                    fh.write("class B {}\n")
            return ws

        def claim_case(name, text, ws, expect, want_prefix, goal=None):
            pinned_goal = [goal_status(goal)] if goal else []
            path = h.transcript(name, [user("do the work")] + pinned_goal + [tool_use(), assistant(text)])
            payload = {"hook_event_name": "Stop", "transcript_path": path, "session_id": name,
                       "stop_hook_active": False, "last_assistant_message": text}
            h.declare_closed_for_done(path, text, ws)
            signal, out = gate.decide(payload, path, h.state_dir(name), 100,
                                      workspace=h.no_gated_repos(), git_timeout=None, gate_workspace=ws,
                                      require_parked=False)
            verdict = "block" if out else "allow"
            record(name, verdict == expect and signal.startswith(want_prefix), "{0} {1}".format(verdict, signal))
            return out

        claim = "DONE: App is gated green and ready to check in.\nVERIFIED: its gate printed GATE: PASSED."
        claim_case("gateclaim-green-current-record-allows-the-claim", claim, gated_workspace("green"),
                   "allow", "allow:work-complete")
        stale_out = claim_case("gateclaim-stale-record-blocks-the-claim", claim,
                               gated_workspace("stale", edit_after_record=True), "block", "block:gate-claim:")
        record("gateclaim-reason-names-the-repo-and-its-state",
               bool(stale_out) and "App: GREEN-STALE" in stale_out, "state named" if stale_out else "no reason")
        claim_case("gateclaim-failed-record-blocks-the-claim", claim, gated_workspace("red", verdict="FAILED"),
                   "block", "block:gate-claim:")
        repo_goal = "Evaluate the stop gate in App/Hooks/STOP-HOOK.md and propose changes"
        claim_case("gateclaim-a-done-restating-a-goal-that-names-the-repo-is-not-judged",
                   "DONE: Evaluate the stop gate in App/Hooks/STOP-HOOK.md and propose changes.\n"
                   "VERIFIED: the replay printed waits=True.",
                   gated_workspace("goal-named", edit_after_record=True), "allow", "allow:work-complete", repo_goal)
        claim_case("gateclaim-a-done-naming-the-repo-beyond-the-goal-still-blocks",
                   "DONE: Evaluate the stop gate in App/Hooks/STOP-HOOK.md and propose changes; App is ready to "
                   "check in.\nVERIFIED: the replay printed waits=True.",
                   gated_workspace("goal-and-claim", edit_after_record=True), "block", "block:gate-claim:", repo_goal)
        claim_case("gateclaim-a-claim-naming-no-gated-repo-is-not-judged",
                   "DONE: the notes are written.\nVERIFIED: the file reads back.",
                   gated_workspace("unnamed", edit_after_record=True), "allow", "allow:work-complete")
        claim_case("gateclaim-a-stale-repo-mentioned-without-a-claim-is-not-judged",
                   "App has edits since its last gate. Shall I run it?",
                   gated_workspace("mention", edit_after_record=True), "allow", "allow:question-mark")
        claim_case("gateclaim-a-denied-claim-is-not-judged",
                   "Nothing is gated green yet: App's gate is still running, and App is not ready to check in. "
                   "Shall I wait for it?",
                   gated_workspace("denied", edit_after_record=True), "allow", "allow:question-mark")
        claim_case("gateclaim-the-same-shape-asserted-blocks",
                   "App is gated green now: its gate is finished, and App is ready to check in. "
                   "Shall I wait for it?",
                   gated_workspace("asserted", edit_after_record=True), "block", "block:gate-claim:")
        claim_case("gateclaim-ready-to-push-is-the-same-claim-as-ready-to-commit-and-blocks",
                   "Its gate is finished, and App is ready to push. Shall I wait for it?",
                   gated_workspace("push", edit_after_record=True), "block", "block:gate-claim:")
        claim_case("gateclaim-control-not-ready-to-push-is-denied-and-allows",
                   "App's gate is still running, so App is not ready to push. Shall I wait for it?",
                   gated_workspace("push-denied", edit_after_record=True), "allow", "allow:question-mark")
        claim_case("gateclaim-one-asserted-phrase-beside-a-denied-one-blocks",
                   "App is not stale; App is gated green. Shall I wait for it?",
                   gated_workspace("mixed", edit_after_record=True), "block", "block:gate-claim:")

        def gated_worktree(name, completed):
            """A workspace whose main App record is stale, beside a worktree of App outside it whose own record
            passed on its exact tree at `completed`."""
            ws = gated_workspace(name, edit_after_record=True)
            main = os.path.join(ws, "App")
            subprocess.run(["git", "-C", main, "add", "--all"], capture_output=True)
            subprocess.run(["git", "-C", main, "commit", "-q", "-m", "fixture"], capture_output=True)
            branch = os.path.join(h.root, "worktree-" + name, "app-branch")
            subprocess.run(["git", "-C", main, "worktree", "add", "-q", "-b", "branch-" + name, branch], capture_output=True)
            subprocess.run(["git", "-C", branch, "config", "core.fsmonitor", "false"], capture_output=True)
            manifest = statuses.manifest(branch)
            tools = statuses.tool_hashes()
            state_dir = statuses.checkout_state_dir("App", branch)
            os.makedirs(state_dir, exist_ok=True)
            with open(statuses.record_path("App", state_dir), "w", encoding="utf-8") as fh:
                json.dump({"repo": "App", "verdict": "PASSED", "completedAt": completed, "machine": "fixture",
                           "steps": [], "manifest": manifest, "tools": tools, "repoPath": branch,
                           "fingerprint": statuses.fingerprint(manifest, tools)}, fh)
            return ws

        claim_case("gateclaim-a-worktree-record-newer-than-the-stale-main-one-proves-the-claim", claim,
                   gated_worktree("newer", "2026-01-02T00:00:00Z"), "allow", "allow:work-complete")
        claim_case("gateclaim-control-an-older-worktree-record-leaves-the-stale-main-one-in-force", claim,
                   gated_worktree("older", "2025-12-31T00:00:00Z"), "block", "block:gate-claim:")

    # Both carve-outs, which exist so the veto can never trap the operator.
    if h.wanted("grant-outranks-the-trailer-veto"):
        verdict, got_signal, _ = h.run(
            "grant-trailer",
            [user("that is enough for now, you can stop"),
             assistant("Done.\n\nCo-Authored-By: Claude Opus 5 <x@y>")])
        record("grant-outranks-the-trailer-veto", verdict == "allow", got_signal)

    if h.wanted("refusal-a-check-that-cannot-judge-refuses-the-refusal"):
        problem = gate.refusal_basis_problem(123, None, h.no_gated_repos())
        record("refusal-a-check-that-cannot-judge-refuses-the-refusal",
               isinstance(problem, str) and problem.startswith("could not be checked"), repr(problem))
    if h.wanted("refusal-a-rule-quote-is-named-as-a-rule-and-no-operator-message"):
        rules = os.path.join(h.root, "refusal-rules")
        os.makedirs(os.path.join(rules, "AGENTS"), exist_ok=True)
        with open(os.path.join(rules, "AGENTS", "RULE.md"), "w", encoding="utf-8") as fh:
            fh.write("A worktree is created only for a gates or hooks change.\n")
        path = h.transcript("refusal-rule-only", [user("make the worktree"), tool_use()])
        problem = gate.refusal_basis_problem(
            "REFUSING: to make it: \"a worktree is created only for a gates or hooks change\".", path, rules)
        record("refusal-a-rule-quote-is-named-as-a-rule-and-no-operator-message",
               isinstance(problem, str) and "a written rule, and no operator message" in problem, repr(problem))
    if h.wanted("refusal-a-quote-of-a-tools-doc-rule-is-named-as-a-rule"):
        empty_rules = os.path.join(h.root, "refusal-rules-empty")
        os.makedirs(empty_rules, exist_ok=True)
        with open(os.path.join(gate.TOOLS_ROOT, "AGENTS", "GATES.md"), encoding="utf-8") as fh:
            tools_rule = next(line.strip(" -*") for line in fh if len(line.split()) > 8 and '"' not in line)
        quoted = " ".join(tools_rule.split()[:8])
        path = h.transcript("refusal-tools-rule", [user("run the gate now"), tool_use()])
        problem = gate.refusal_basis_problem("REFUSING: to run it: \"{0}\".".format(quoted), path, empty_rules)
        record("refusal-a-quote-of-a-tools-doc-rule-is-named-as-a-rule",
               isinstance(problem, str) and "a written rule, and no operator message" in problem, repr(problem))
    if h.wanted("refusal-control-the-operator-quoting-a-rule-is-a-basis"):
        path = h.transcript("refusal-rule-said", [user("no: a worktree is created only for a gates or hooks change"),
                                                  tool_use()])
        problem = gate.refusal_basis_problem(
            "REFUSING: to make it: \"a worktree is created only for a gates or hooks change\".", path, h.no_gated_repos())
        record("refusal-control-the-operator-quoting-a-rule-is-a-basis", problem is None, repr(problem))

    if h.wanted("refusal-outranks-the-trailer-veto"):
        verdict, got_signal, _ = h.run(
            "refusal-trailer",
            [user("push it to main. no, wait: pushing to main is not authorized today"),
             assistant("REFUSING: to push: \"pushing to main is not authorized today\".\n\n"
                       "Co-Authored-By: Claude Opus 5 <x@y>")])
        record("refusal-outranks-the-trailer-veto", verdict == "allow", got_signal)

    if h.wanted("trailer-blocks-past-the-budget-still-block") and not gate.TRAILER_VETO_ENABLED:
        skip("trailer-blocks-past-the-budget-still-block", TRAILER_OFF)
    elif h.wanted("trailer-blocks-past-the-budget-still-block"):
        records = [user("go"), assistant("Message.\n\nCo-Authored-By: Claude Opus 5 <x@y>\n\nOk?")]
        path = h.transcript("trailer-budget", records)
        state = os.path.join(h.root, "state-trailer-budget")
        seq = []
        for _ in range(3):
            verdict, _, _ = h.run_path("trailer-budget", path, stop_hook_active=True,
                                       max_blocks=2, state=state)
            seq.append(verdict)
        record("trailer-blocks-past-the-budget-still-block", seq == ["block", "block", "block"],
               ",".join(seq))

    if h.wanted("blocks-past-the-budget-never-yield"):
        records = [user("go"), tool_use(), assistant("Tests are green and the gate is clean.")]
        path = h.transcript("budget", records)
        state = os.path.join(h.root, "state-budget")
        seq = []
        for i in range(4):
            verdict, _, _ = h.run_path("budget", path, stop_hook_active=(i > 0),
                                       max_blocks=3, state=state)
            seq.append(verdict)
        record("blocks-past-the-budget-never-yield", seq == ["block", "block", "block", "block"],
               ",".join(seq))

    if h.wanted("a-forged-block-counter-earns-no-stop"):
        path = h.transcript("forged-budget", [user("go"), tool_use(), assistant("Tests are green.")])
        state = os.path.join(h.root, "state-forged-budget")
        os.makedirs(state, exist_ok=True)
        with open(os.path.join(state, "forged-budget.json"), "w", encoding="utf-8") as fh:
            json.dump({"blocks": 100}, fh)
        verdict, got_signal, _ = h.run_path("forged-budget", path, stop_hook_active=True, max_blocks=100,
                                            state=state)
        record("a-forged-block-counter-earns-no-stop", verdict == "block", "{0} {1}".format(verdict, got_signal))

    # The case above uses a STATIC transcript, which is not what the gate faces. Claude Code appends the
    # block reason as a prose user record before the next Stop, so the transcript grows by one
    # user-shaped record each round. A turn key derived by COUNTING prose user records moves under the
    # gate's own feet: the stored key never matches, the budget resets every invocation, and MaxBlocks
    # never engages. That shipped green against a static fixture while every state file on this machine
    # read {"blocks":1} and real sessions blocked nine times under a single prompt.
    if h.wanted("budget-survives-gate-feedback"):
        prompt_id = "p-11111111-1111-1111-1111-111111111111"
        path = h.transcript("gate-feedback-loop", [
            user("do the thing", prompt_id),
            assistant("Tests are green and the gate is clean.")])
        state = os.path.join(h.root, "state-feedback-loop")
        seq = []
        for i in range(4):
            verdict, got_signal, _ = h.run_path("feedback-loop", path, stop_hook_active=(i > 0),
                                                max_blocks=3, state=state)
            seq.append(got_signal)
            if verdict == "block":
                append(path, [user("Stop hook feedback: STOP GATE: keep working.", prompt_id), tool_use(),
                              assistant("Still green. Nothing further to add.")])
        record("budget-survives-gate-feedback",
               seq[:3] == ["block:1", "block:2", "block:3"] and seq[3] == "block:" + gate.PAST_BUDGET_TAG + "4",
               ",".join(seq))

    # A RUN OF FILLER ENDS AT FILLER_STREAK_LIMIT, before Claude Code's nine-block cap, with a reason the operator
    # sees. Each round appends the gate's feedback the way Claude Code does, then the session's reply.
    def filler_run(session, reply_records, rounds):
        prompt_id = "p-55555555-5555-5555-5555-555555555555"
        path = h.transcript(session, [user("do the thing", prompt_id), tool_use(), assistant("Status only.")])
        state = h.state_dir(session)
        signals, outs = [], []
        for i in range(rounds):
            _, got, out = h.run_path(session, path, stop_hook_active=(i > 0), state=state)
            signals.append(got)
            outs.append(out)
            append(path, [user("Stop hook feedback: STOP GATE: keep working.", prompt_id)] + reply_records)
        return signals, outs

    if h.wanted("filler-a-run-of-prose-only-replies-ends-the-turn-with-a-reason"):
        signals, outs = filler_run("filler-prose", [assistant("I'm still waiting on you.")], gate.FILLER_STREAK_LIMIT)
        ended = signals[-1]
        envelope = json.loads(gate.envelope(ended, outs[-1]))
        record("filler-a-run-of-prose-only-replies-ends-the-turn-with-a-reason",
               all(s.startswith("block:") for s in signals[:-1]) and ended.startswith(gate.FILLER_STREAK_TAG)
               and envelope.get("continue") is False and "no tool call between them" in envelope.get("stopReason", ""),
               ",".join(signals))
    if h.wanted("filler-the-run-ends-before-claude-codes-nine-block-cap"):
        record("filler-the-run-ends-before-claude-codes-nine-block-cap", 1 < gate.FILLER_STREAK_LIMIT < 9,
               str(gate.FILLER_STREAK_LIMIT))
    if h.wanted("filler-the-ended-run-is-in-the-decision-log"):
        signals, _ = filler_run("filler-logged", [assistant("Still nothing to add.")], gate.FILLER_STREAK_LIMIT)
        with open(os.path.join(h.state_dir("filler-logged"), "decisions.log"), "r", encoding="utf-8") as fh:
            logged = fh.read()
        record("filler-the-ended-run-is-in-the-decision-log", "\t" + signals[-1] + "\n" in logged
               and signals[-1].startswith(gate.FILLER_STREAK_TAG), signals[-1])
    if h.wanted("filler-control-a-tool-call-between-refusals-restarts-the-run"):
        signals, _ = filler_run("filler-working", [tool_use(), assistant("Status only.")],
                                gate.FILLER_STREAK_LIMIT + 2)
        record("filler-control-a-tool-call-between-refusals-restarts-the-run",
               all(s.startswith("block:") for s in signals), ",".join(signals))
    if h.wanted("filler-control-one-tool-call-mid-run-restarts-the-count"):
        prompt_id = "p-66666666-6666-6666-6666-666666666666"
        session = "filler-interrupted"
        path = h.transcript(session, [user("do the thing", prompt_id), tool_use(), assistant("Status only.")])
        state = h.state_dir(session)
        signals = []
        for i in range(gate.FILLER_STREAK_LIMIT + 1):
            _, got, _ = h.run_path(session, path, stop_hook_active=(i > 0), state=state)
            signals.append(got)
            reply = [tool_use()] if i == 1 else []
            append(path, [user("Stop hook feedback: STOP GATE: keep working.", prompt_id)] + reply
                   + [assistant("Status only.")])
        record("filler-control-one-tool-call-mid-run-restarts-the-count",
               all(s.startswith("block:") for s in signals), ",".join(signals))
    if h.wanted("filler-control-a-static-transcript-is-never-a-run"):
        path = h.transcript("filler-static", [user("go"), tool_use(), assistant("Status only.")])
        state = os.path.join(h.root, "state-filler-static")
        signals = [h.run_path("filler-static", path, stop_hook_active=(i > 0), state=state)[1]
                   for i in range(gate.FILLER_STREAK_LIMIT + 1)]
        record("filler-control-a-static-transcript-is-never-a-run", all(s.startswith("block:") for s in signals),
               ",".join(signals))

    # A SECOND EJECT WITH NO OPERATOR MESSAGE BETWEEN QUARANTINES THE SESSION. The first run ejects as above; the next
    # turn a peer message starts fills the same way and its eject is the strike. An operator message between the two
    # runs starts the count over, so a session the operator keeps talking to is never quarantined for one lapse each.
    import session_quarantine

    def ejected_twice(session, between, second_prompt_id="p-77777777-7777-7777-7777-777777777777"):
        prompt_ids = ("p-77777777-7777-7777-7777-777777777777", second_prompt_id)
        path = h.transcript(session, [user("do the thing", prompt_ids[0]), tool_use(), assistant("Status only.")])
        state = h.state_dir(session)
        signals = []
        for turn in range(2):
            for i in range(gate.FILLER_STREAK_LIMIT):
                _, got, _ = h.run_path(session, path, stop_hook_active=(i > 0), state=state)
                signals.append(got)
                append(path, [user("Stop hook feedback: STOP GATE: keep working.", prompt_ids[turn]),
                              assistant("Still waiting.")])
            if turn == 0:
                append(path, between)
        return signals, os.path.exists(session_quarantine.record_path(session, h.quarantine_root()))

    peer_wake = {"type": "user", "origin": {"kind": "peer"}, "message": {"role": "user", "content":
                 '<cross-session-message from="uds:x" from-name="peer">read the board</cross-session-message>'}}
    if h.wanted("filler-a-second-eject-with-no-operator-message-between-quarantines-the-session"):
        signals, recorded = ejected_twice("filler-strike", [peer_wake, assistant("Still waiting.")])
        record("filler-a-second-eject-with-no-operator-message-between-quarantines-the-session",
               signals[gate.FILLER_STREAK_LIMIT - 1].startswith(gate.FILLER_STREAK_TAG)
               and signals[-1] == "eject:quarantined" and recorded, ",".join(signals))
    if h.wanted("filler-control-an-operator-message-between-two-ejects-starts-the-count-over"):
        signals, recorded = ejected_twice("filler-strike-reset",
                                          [user("carry on", "p-88888888-8888-8888-8888-888888888888"),
                                           assistant("Still waiting.")],
                                          second_prompt_id="p-88888888-8888-8888-8888-888888888888")
        record("filler-control-an-operator-message-between-two-ejects-starts-the-count-over",
               signals[-1].startswith(gate.FILLER_STREAK_TAG) and not recorded, ",".join(signals))
    if h.wanted("filler-the-quarantined-session-is-halted-at-its-next-stop-with-the-filler-reason"):
        session = "filler-strike-halted"
        ejected_twice(session, [peer_wake, assistant("Still waiting.")])
        path = h.transcript(session + "-next", [user("go"), tool_use(), assistant("DONE: nothing.")])
        _, got, out = h.run_path(session, path, stop_hook_active=False)
        record("filler-the-quarantined-session-is-halted-at-its-next-stop-with-the-filler-reason",
               got == "eject:quarantined" and "ended two turns in a row" in out and "tried to change" not in out,
               "{0} {1}".format(got, out[:160]))
    if h.wanted("eject-a-prose-only-reply-to-a-block-is-blocked-never-ejected"):
        prompt_id = "p-33333333-3333-3333-3333-333333333333"
        session = "eject-prose-only"
        path = h.transcript(session, [user("do the thing", prompt_id), assistant("Tests are green and the gate is clean.")])
        state = h.state_dir(session)
        _, first, _ = h.run_path(session, path, stop_hook_active=False, state=state)
        append(path, [user("Stop hook feedback: STOP GATE: keep working.", prompt_id),
                      assistant("Still green. Nothing further to add.")])
        _, second, _ = h.run_path(session, path, stop_hook_active=True, state=state)
        recorded = os.path.exists(session_quarantine.record_path(session, h.quarantine_root()))
        record("eject-a-prose-only-reply-to-a-block-is-blocked-never-ejected",
               first == "block:1" and second == "block:2" and not recorded,
               "{0}, then {1}; record={2}".format(first, second, recorded))
    if h.wanted("eject-a-session-past-its-block-budget-is-blocked-never-ejected"):
        prompt_id = "p-44444444-4444-4444-4444-444444444444"
        session = "eject-past-budget"
        path = h.transcript(session, [user("do the thing", prompt_id), tool_use(), assistant("Status only.")])
        state = h.state_dir(session)
        signals = []
        for i in range(12):
            _, got, _ = h.run_path(session, path, stop_hook_active=(i > 0), max_blocks=9, state=state)
            signals.append(got)
            append(path, [user("Stop hook feedback: STOP GATE: keep working.", prompt_id), tool_use(),
                          assistant("Status only.")])
        recorded = os.path.exists(session_quarantine.record_path(session, h.quarantine_root()))
        record("eject-a-session-past-its-block-budget-is-blocked-never-ejected",
               all(s.startswith("block:") for s in signals) and not recorded,
               "{0}; record={1}".format(",".join(signals[-3:]), recorded))
    if h.wanted("eject-a-session-quarantined-for-changing-its-gate-is-halted-at-its-stop"):
        session = "eject-quarantined"
        session_quarantine.quarantine(session, "wrote to the deployed hook copy", "Edit ...claude-hooks...",
                                      h.quarantine_root())
        _, signal, out = h.run(session, [user("do the thing"), tool_use(), assistant("Should I continue?")])
        record("eject-a-session-quarantined-for-changing-its-gate-is-halted-at-its-stop",
               signal == "eject:quarantined" and "EJECTED" in out
               and json.loads(gate.envelope(signal, out)).get("continue") is False, signal)
    if h.wanted("eject-control-a-session-with-no-record-is-judged-as-usual"):
        _, signal, _ = h.run("eject-no-record", [user("do the thing"), tool_use(), assistant("Should I continue?")])
        record("eject-control-a-session-with-no-record-is-judged-as-usual", signal == "allow:question-mark", signal)
    if h.wanted("eject-an-unloadable-quarantine-module-blocks-the-stop"):
        saved = sys.modules.get("session_quarantine")
        sys.modules["session_quarantine"] = None
        try:
            _, signal, out = h.run("eject-unloadable", [user("do the thing"), tool_use(), assistant("Should I continue?")])
        finally:
            sys.modules["session_quarantine"] = saved
        record("eject-an-unloadable-quarantine-module-blocks-the-stop",
               signal == "block:quarantine-unloadable:1" and "could not load" in out, signal)

    subtask_claim = "DONE: the dispatcher rows\nVERIFIED: python test_hook_dispatcher.py printed All cases passed."
    injected = dict(user("the operator's pinned GOAL: whatever the skill says"), isMeta=True)
    queued_goal = {"type": "attachment", "attachment": {"type": "queued_command", "origin": {"kind": "human"},
                                                        "prompt": "GOAL: ship the whole release"}}
    for name, records, expected in (
            ("goal-a-skill-injection-carrying-goal-pins-nothing",
             [user("do the thing"), injected, tool_use(), tool_result(), assistant(subtask_claim)], "allow:work-complete"),
            ("goal-a-goal-the-operator-sent-mid-turn-pins",
             [user("do the thing"), tool_use(), queued_goal, tool_result(), assistant(subtask_claim)], "block:goal:1")):
        if h.wanted(name):
            _, signal, _ = h.run(name, records)
            record(name, signal == expected, signal)

    finished = [user("do the thing"), tool_use(), tool_result(),
                assistant("DONE: the thing\nVERIFIED: python test.py printed All cases passed.")]
    for name, declaration, expected in (
            ("done-with-no-work-product-declared-blocks", None, "block:done-open:1"),
            ("done-with-an-open-work-product-item-blocks", {"Tools": [("Ship it", True), ("Test it", False)]},
             "block:done-open:1"),
            ("done-control-with-every-item-done-allows", {"Tools": [("Ship it", True), ("Test it", True)]},
             "allow:work-complete")):
        if h.wanted(name):
            with RealWaitChecks():
                verdict, signal = isolated_stop(h, name, finished, declaration=declaration)
            record(name, signal == expected, signal)

    def read_only_session(edits):
        """A session whose ask changed no repo: the operator's ask, the edits the row plants, one tool call and a
        DONE:, built once the isolated workspace path is known so an edit can land inside its repo."""
        return lambda workspace: ([user("review the thing")] + edits(workspace) + [tool_use(), tool_result(),
                                  assistant("DONE: the thing\nVERIFIED: python test.py printed All cases passed.")])

    def edit_with_result(tool_id, file_path, rejected=False):
        result = {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}
        if rejected:
            result = dict(result, content="The user doesn't want to proceed with this tool use.", is_error=True)
        return [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "Edit",
                                                                "input": {"file_path": file_path, "old_string": "a",
                                                                          "new_string": "b"}}]}},
                {"type": "user", "promptId": "p-turn", "message": {"role": "user", "content": [result]}}]

    def tools_repo(workspace):
        repo = os.path.join(workspace, "Tools")
        os.makedirs(repo)
        subprocess.run(["git", "-c", "core.fsmonitor=false", "init", "-q", repo], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "config", "core.fsmonitor", "false"], check=True, capture_output=True)

    for name, edits, expected in (
            ("done-with-an-empty-declaration-after-a-repo-edit-blocks",
             lambda ws: edit_with_result("tu-e1", os.path.join(ws, "Tools", "Hooks", "x.py")), "block:done-open:1"),
            ("done-control-an-empty-declaration-after-a-rejected-repo-edit-allows",
             lambda ws: edit_with_result("tu-e1", os.path.join(ws, "Tools", "Hooks", "x.py"), rejected=True),
             "allow:work-complete"),
            ("done-control-an-empty-declaration-with-edits-outside-every-repo-allows",
             lambda ws: (edit_with_result("tu-e1", os.path.join(ws, "notes", "x.py"))
                         + edit_with_result("tu-e2", os.path.join(os.path.dirname(ws), "elsewhere", "x.html"))),
             "allow:work-complete"),
            ("done-control-an-empty-declaration-with-no-edit-allows", lambda ws: [], "allow:work-complete")):
        if h.wanted(name):
            with RealWaitChecks():
                verdict, signal, reason = isolated_stop(h, name, read_only_session(edits), declaration={},
                                                        with_reason=True, prepare=tools_repo)
            names_the_edit = expected.startswith("allow") or ("Tools" in reason and "x.py" in reason)
            record(name, signal == expected and names_the_edit, signal if signal != expected else reason[:160])

    def answers(stdout, returncode=0):
        return lambda command, **kwargs: types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    def never_answers(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    def cannot_launch(command, **kwargs):
        raise FileNotFoundError(command[0])

    asked = "do not call it done until the poll task reads Ready again"
    accepted_by_operator = [user(asked), tool_use(), tool_result(),
                            assistant("DONE: the thing\nVERIFIED: python test.py printed All cases passed.")]
    poll_ready = [{"check": "the poll task is enabled", "command": ["probe"], "expect": r"State\s+Ready",
                   "approval": asked}]
    all_done = {"Tools": [("Ship it", True)]}
    for name, acceptance, runner, expected in (
            ("acceptance-an-entry-without-an-approval-blocks-done",
             [{"check": "the poll task is enabled", "command": ["probe"], "expect": "Ready"}], answers("State Ready"),
             "block:done-unaccepted:1"),
            ("acceptance-an-entry-approved-only-by-the-agent-blocks-done",
             [dict(poll_ready[0], approval="the agent decided this check is enough to prove it")],
             answers("State Ready"), "block:done-unaccepted:1"),
            ("acceptance-a-genuine-operator-quote-shorter-than-the-minimum-blocks-done",
             [dict(poll_ready[0], approval="the poll task reads")], answers("State Ready"), "block:done-unaccepted:1"),
            ("acceptance-a-check-whose-output-does-not-match-blocks-done", poll_ready, answers("State Disabled"),
             "block:done-unaccepted:1"),
            ("acceptance-a-check-that-exits-nonzero-blocks-done", poll_ready, answers("State Ready", 1),
             "block:done-unaccepted:1"),
            ("acceptance-a-check-that-times-out-blocks-done", poll_ready, never_answers, "block:done-unaccepted:1"),
            ("acceptance-a-check-that-cannot-launch-blocks-done", poll_ready, cannot_launch, "block:done-unaccepted:1"),
            ("acceptance-an-entry-without-a-command-blocks-done",
             [{"check": "the poll task is enabled", "expect": "Ready", "approval": asked}], answers("State Ready"),
             "block:done-unaccepted:1"),
            ("acceptance-control-a-check-that-passes-allows-done", poll_ready, answers("State Ready"),
             "allow:work-complete"),
            ("acceptance-control-a-real-command-that-passes-allows-done",
             [{"check": "a real process answers", "command": [sys.executable, "-c", "print('State Ready')"],
               "expect": r"State\s+Ready", "approval": asked}], None, "allow:work-complete")):
        if h.wanted(name):
            with RealWaitChecks():
                verdict, signal = isolated_stop(h, name, accepted_by_operator, declaration=all_done,
                                                acceptance=acceptance, acceptance_runner=runner)
            record(name, signal == expected, signal)

    name = "acceptance-a-spent-budget-blocks-done-even-when-the-check-would-pass"
    if h.wanted(name):
        saved = gate.ACCEPTANCE_TOTAL_SECONDS
        gate.ACCEPTANCE_TOTAL_SECONDS = 0
        try:
            with RealWaitChecks():
                verdict, signal, reason = isolated_stop(h, name, accepted_by_operator, declaration=all_done,
                                                        acceptance=poll_ready, acceptance_runner=answers("State Ready"),
                                                        with_reason=True)
        finally:
            gate.ACCEPTANCE_TOTAL_SECONDS = saved
        record(name, signal == "block:done-unaccepted:1" and "budget is spent" in reason, signal)

    def green_dirty_repo(session, parked=False, broken=False, verdict="PASSED", steps=None,
                         parked_message="Change a\n\na.txt"):
        def prepare(workspace):
            repo = os.path.join(workspace, "Repo")
            os.makedirs(os.path.join(repo, ".git") if broken else repo)
            if not broken:
                git = ["git", "-C", repo, "-c", "core.fsmonitor=false", "-c", "user.name=harness",
                       "-c", "user.email=harness@example.invalid"]
                subprocess.run(git + ["init", "--quiet"], check=True, capture_output=True)
                subprocess.run(git + ["config", "core.fsmonitor", "false"], check=True, capture_output=True)
                with open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as fh:
                    fh.write("one\n")
                subprocess.run(git + ["add", "--all"], check=True, capture_output=True)
                subprocess.run(git + ["commit", "--quiet", "--message", "fixture"], check=True, capture_output=True)
                with open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as fh:
                    fh.write("two\n")
            gates = gate.gate_status_module().checkout_state_dir("Repo", repo)
            os.makedirs(gates)
            with open(os.path.join(gates, "Repo.{0}.last.json".format(os.environ.get("COMPUTERNAME", ""))), "w",
                      encoding="utf-8") as fh:
                record = {"repo": "Repo", "verdict": verdict, "session": session}
                if steps is not None:
                    record["steps"] = steps
                if not broken:
                    status_module = gate.gate_status_module()
                    record.update({"manifest": status_module.manifest(repo), "tools": status_module.tool_hashes()})
                json.dump(record, fh)
            if parked:
                commits = os.path.join(session_home.parking_dir(session_home.state_root(), session), "commits")
                os.makedirs(commits)
                with open(os.path.join(commits, "Repo.json"), "w", encoding="utf-8") as fh:
                    json.dump({"repo": "Repo", "message": parked_message, "files": ["a.txt"]}, fh)
        return prepare

    red_steps = [{"name": "First step", "verdict": "FAIL", "detail": "exit 1"},
                 {"name": "Second step", "verdict": "NOT REACHED", "detail": "fail-fast"}]

    for name, prepare, expected in (
            ("parking-a-done-with-a-repo-green-here-and-dirty-and-no-message-parked-blocks",
             lambda: green_dirty_repo("parking-a-done-with-a-repo-green-here-and-dirty-and-no-message-parked-blocks"),
             "block:unparked-commit:1"),
            ("parking-a-done-whose-green-repo-cannot-be-read-blocks-by-name",
             lambda: green_dirty_repo("parking-a-done-whose-green-repo-cannot-be-read-blocks-by-name", broken=True),
             "block:unparked-commit:1"),
            ("parking-control-the-same-done-with-the-message-parked-allows",
             lambda: green_dirty_repo("parking-control-the-same-done-with-the-message-parked-allows", parked=True),
             "allow:work-complete"),
            ("parking-control-a-repo-green-in-another-session-owes-this-one-nothing",
             lambda: green_dirty_repo("another-session"), "allow:work-complete"),
            # A parked message for a red tree reads as ready to land unless its UNGATED line names what the gate
            # left unproven; the operator: "an ungated tree can never read as ready to land".
            ("parking-a-parked-message-for-a-red-repo-without-its-ungated-line-blocks",
             lambda: green_dirty_repo("parking-a-parked-message-for-a-red-repo-without-its-ungated-line-blocks",
                                      parked=True, verdict="FAILED", steps=red_steps),
             "block:unparked-commit:1"),
            ("parking-a-parked-ungated-line-naming-only-the-failed-step-blocks",
             lambda: green_dirty_repo("parking-a-parked-ungated-line-naming-only-the-failed-step-blocks",
                                      parked=True, verdict="FAILED", steps=red_steps,
                                      parked_message="UNGATED: Repo RED: failed: First step\n\nChange a\n\na.txt"),
             "block:unparked-commit:1"),
            ("parking-control-a-parked-message-opening-with-its-full-ungated-line-allows",
             lambda: green_dirty_repo("parking-control-a-parked-message-opening-with-its-full-ungated-line-allows",
                                      parked=True, verdict="FAILED", steps=red_steps,
                                      parked_message="UNGATED: Repo RED: failed: First step; not reached: Second step"
                                                     "\n\nChange a\n\na.txt"),
             "allow:work-complete")):
        if h.wanted(name):
            with RealWaitChecks():
                verdict, signal = isolated_stop(h, name, finished, declaration=all_done, prepare=prepare())
            record(name, signal == expected, signal)

    def worked_dirty_repo(parked=False, repo_name="Repo"):
        """A dirty repo with NO gate record, written by the session this turn; parked holds a matching message."""
        def prepare(workspace):
            repo = os.path.join(workspace, repo_name)
            os.makedirs(repo)
            git = ["git", "-C", repo, "-c", "core.fsmonitor=false", "-c", "user.name=harness",
                   "-c", "user.email=harness@example.invalid"]
            subprocess.run(git + ["init", "--quiet"], check=True, capture_output=True)
            subprocess.run(git + ["config", "core.fsmonitor", "false"], check=True, capture_output=True)
            with open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as fh:
                fh.write("one\n")
            subprocess.run(git + ["add", "--all"], check=True, capture_output=True)
            subprocess.run(git + ["commit", "--quiet", "--message", "fixture"], check=True, capture_output=True)
            with open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as fh:
                fh.write("two\n")
            if parked:
                commits = os.path.join(session_home.parking_dir(session_home.state_root(), prepare.session), "commits")
                os.makedirs(commits)
                with open(os.path.join(commits, repo_name + ".json"), "w", encoding="utf-8") as fh:
                    json.dump({"repo": repo_name, "files": ["a.txt"], "message": "UNGATED: {0} UNKNOWN: no gate record "
                               "from this session\n\nChange a\n\na.txt".format(repo_name)}, fh)
        return prepare

    def wrote_in(repo_name, closing):
        """Records whose write lands in <workspace>\\<repo_name>, so the veto learns the session worked there."""
        return lambda workspace: [user("do the thing"), edit_use(os.path.join(workspace, repo_name, "a.txt")),
                                  tool_result(), assistant(closing)]

    def worked_worktree(parked=False):
        """A clean main checkout of Repo with a registered worktree the session wrote in, no gate record; parked
        holds a message naming that worktree."""
        def prepare(workspace):
            repo = os.path.join(workspace, "Repo")
            os.makedirs(repo)
            git = ["git", "-C", repo, "-c", "core.fsmonitor=false", "-c", "user.name=harness",
                   "-c", "user.email=harness@example.invalid"]
            subprocess.run(git + ["init", "--quiet"], check=True, capture_output=True)
            subprocess.run(git + ["config", "core.fsmonitor", "false"], check=True, capture_output=True)
            with open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as fh:
                fh.write("one\n")
            subprocess.run(git + ["add", "--all"], check=True, capture_output=True)
            subprocess.run(git + ["commit", "--quiet", "--message", "fixture"], check=True, capture_output=True)
            worktree = os.path.join(os.path.dirname(workspace), "worktrees", prepare.session, "Repo")
            subprocess.run(git + ["worktree", "add", "--quiet", "-b", prepare.session, worktree], check=True,
                           capture_output=True)
            with open(os.path.join(worktree, "a.txt"), "w", encoding="utf-8") as fh:
                fh.write("two\n")
            if parked:
                commits = os.path.join(session_home.parking_dir(session_home.state_root(), prepare.session), "commits")
                os.makedirs(commits)
                with open(os.path.join(commits, "Repo@" + prepare.session + ".json"), "w", encoding="utf-8") as fh:
                    json.dump({"repo": "Repo", "checkout": worktree, "files": ["a.txt"], "message": "UNGATED: Repo UNKNOWN: "
                               "no gate record from this session\n\nChange a\n\na.txt"}, fh)
        return prepare

    def wrote_in_worktree(closing):
        return lambda workspace: [
            user("do the thing"),
            edit_use(os.path.join(os.path.dirname(workspace), "worktrees", wrote_in_worktree.session, "Repo", "a.txt")),
            tool_result(), assistant(closing)]

    needed = "NEEDED: your choice of deploy root."
    for name, parked, expected in (
            ("parking-an-ungated-worktree-the-session-wrote-in-with-no-message-parked-blocks", False,
             "block:unparked-commit:1"),
            ("parking-control-the-same-worktree-with-its-message-parked-allows", True, "allow:blocker-token")):
        if h.wanted(name):
            prepare = worked_worktree(parked=parked)
            prepare.session = name
            wrote_in_worktree.session = name
            with RealWaitChecks():
                verdict, signal, out = isolated_stop(h, name, wrote_in_worktree(needed), prepare=prepare, with_reason=True)
            record(name, signal == expected and (not out or "worktrees" in out), signal + " " + out[:160])

    for name, parked, written_in, expected in (
            ("parking-an-ungated-dirty-repo-the-session-wrote-in-with-no-message-parked-blocks", False, "Repo",
             "block:unparked-commit:1"),
            ("parking-control-the-same-ungated-repo-with-the-message-parked-allows", True, "Repo",
             "allow:blocker-token"),
            ("parking-control-a-dirty-repo-the-session-never-wrote-in-owes-nothing", False, "Other",
             "allow:blocker-token")):
        if h.wanted(name):
            prepare = worked_dirty_repo(parked=parked)
            prepare.session = name
            with RealWaitChecks():
                verdict, signal = isolated_stop(h, name, wrote_in(written_in, needed), prepare=prepare, one_workspace=True)
            record(name, signal == expected, signal)

    # A NEEDED: or question hand-off is held to work_product_closure: a done flag is the session's claim, and an
    # ungated repo is excused only by a measured block (an item there waiting on a parked question). An ask to push
    # while an open item waits on nothing is refused before it, since landing is the last step.
    block = "\n\n```\nChange a\n\na.txt\n```"
    waits = "NEEDED: your choice of deploy root, which Repo/c.txt waits on the choice of deploy root for."
    land = "NEEDED: push Repo once c.txt adds the deploy root here" + block
    for name, declaration, closing, expected in (
            ("handoff-a-needed-with-an-edit-no-item-names-blocks",
             {"Repo": [("b.txt holds the change", True)]}, needed, "block:handoff-closure"),
            ("handoff-a-needed-with-a-done-repo-ungated-blocks",
             {"Repo": [("a.txt holds the change", True)]}, needed, "block:handoff-closure"),
            ("handoff-control-the-ungated-repo-waits-on-a-parked-answer-and-allows",
             {"Repo": [("a.txt holds the change", True),
                       ("c.txt waits on the choice of deploy root", False, "q9-deploy-root")]},
             waits, "allow:blocker-token"),
            ("landing-a-push-ask-while-an-item-waits-on-nothing-blocks",
             {"Repo": [("a.txt holds the change", True), ("c.txt adds the deploy root", False)]},
             land, "block:landing-work"),
            ("landing-control-the-same-push-ask-with-the-item-pending-on-a-parked-answer-allows",
             {"Repo": [("a.txt holds the change", True),
                       ("c.txt adds the deploy root", False, "q9-deploy-root")]},
             land, "allow:blocker-token")):
        if h.wanted(name):
            prepare = worked_dirty_repo(parked=True)
            prepare.session = name
            with RealWaitChecks():
                verdict, signal, out = isolated_stop(h, name, wrote_in("Repo", closing), declaration=declaration,
                                                     prepare=prepare, one_workspace=True, with_reason=True)
            because = {"handoff-a-needed-with-an-edit-no-item-names-blocks": "no declared item names them",
                       "handoff-a-needed-with-a-done-repo-ungated-blocks": "uncommitted file(s) a gate covers"}.get(name, "")
            record(name, signal.startswith(expected) and because in out, "{0} | {1}".format(signal, out[:240]))

    # A pending item waits only while its question is open: once a read of the page shows it answered or withdrawn,
    # the answer is work, and the item excuses neither a landing ask nor an ungated repo.
    def wrote_in_after_read(repo_name, closing, status):
        def rows(workspace):
            read = {"action": "query", "url": "https://claude.ai/artifact/x", "collection": "questions",
                    "query": {"where": [["status", "==", "answered"]]}}
            printed = json.dumps({"id": "q9-deploy-root", "data": {"status": status, "question": "Pick the root"}})
            return [user("do the thing"), edit_use(os.path.join(workspace, repo_name, "a.txt")), tool_result(),
                    {"type": "assistant", "message": {"content": [
                        {"type": "tool_use", "id": "tu-r", "name": "ArtifactData", "input": read}]}},
                    {"type": "user", "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "tu-r",
                         "content": "1 documents from collection \"questions\":\n" + printed}]}},
                    assistant(closing)]
        return rows

    pended = {"Repo": [("a.txt holds the change", True),
                       ("c.txt adds the deploy root", False, "q9-deploy-root")]}
    for name, status, expected in (
            ("landing-a-push-ask-with-the-item-pending-on-an-answered-question-blocks", "answered",
             "block:landing-work"),
            ("landing-a-push-ask-with-the-item-pending-on-a-withdrawn-question-blocks", "withdrawn",
             "block:landing-work"),
            ("landing-control-a-push-ask-with-the-item-pending-on-a-question-the-read-shows-open-allows", "open",
             "allow:blocker-token")):
        if h.wanted(name):
            prepare = worked_dirty_repo(parked=True)
            prepare.session = name
            with RealWaitChecks():
                verdict, signal, out = isolated_stop(h, name, wrote_in_after_read("Repo", land, status),
                                                     declaration=pended, prepare=prepare, one_workspace=True,
                                                     with_reason=True)
            record(name, signal.startswith(expected), "{0} | {1}".format(signal, out[:240]))

    # A settled question stays settled past its stop attempt: an answer read back from the session's own published
    # page in an earlier attempt, or the session's own withdrawal, still leaves the pending item waiting on nothing.
    page_url = "https://claude.ai/artifact/ownpage"
    stale_feedback = {"type": "user", "message": {"role": "user",
                                                  "content": gate.STOP_FEEDBACK_PREFIX + "\nSTOP GATE: keep working."}}

    def earlier_attempt(kind):
        def rows(workspace):
            publish = {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "tu-p", "name": "Artifact",
                 "input": {"file_path": os.path.join(workspace, "parking-lot.html")}}]}}
            published = {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu-p", "content": "Published " + page_url}]}}
            if kind == "answered":
                call = {"action": "query", "url": page_url, "collection": "questions",
                        "query": {"where": [["status", "==", "answered"]]}}
                result = "1 documents from collection \"questions\":\n" + json.dumps(
                    {"id": "q9-deploy-root", "data": {"status": "answered", "answer": "D:\\deploy",
                                                      "answeredAt": "2026-01-01T00:00:00Z",
                                                      "question": "Pick the root"}})
            else:
                call = {"action": "update", "url": page_url, "collection": "questions", "doc_id": "q9-deploy-root",
                        "data": {"status": "withdrawn", "withdrawnReason": "superseded"}, "if_version": 1}
                result = "Database update committed."
            act = {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "tu-a", "name": "ArtifactData", "input": call}]}}
            acted = {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu-a", "content": result}]}}
            return [user("do the thing"), edit_use(os.path.join(workspace, "Repo", "a.txt")), tool_result(),
                    publish, published, act, acted, assistant("Working on it."), stale_feedback, tool_use(),
                    assistant(land)]
        return rows

    for name, kind in (
            ("landing-a-push-ask-pending-on-a-question-an-earlier-attempt-read-answered-blocks", "answered"),
            ("landing-a-push-ask-pending-on-a-question-this-session-withdrew-blocks", "withdrawn")):
        if h.wanted(name):
            prepare = worked_dirty_repo(parked=True)
            prepare.session = name
            with RealWaitChecks():
                verdict, signal = isolated_stop(h, name, earlier_attempt(kind), declaration=pended,
                                                prepare=prepare, one_workspace=True)
            record(name, signal.startswith("block:landing-work"), signal)

    # A QUEUED: or WAITING: landing wait is refused while an item is open by the wait check itself, which demands every
    # item done; the landing check reads only NEEDED: and question stops, so this row pins that coupling.
    name = "landing-a-queued-wait-until-you-push-with-an-open-item-blocks"
    if h.wanted(name):
        prepare = worked_dirty_repo(parked=True)
        prepare.session = name
        with RealWaitChecks():
            verdict, signal, out = isolated_stop(
                h, name, wrote_in("Repo", "QUEUED: until you push Repo, session 4f1c2a9b's gate first." + block),
                declaration={"Repo": [("a.txt holds the change", True), ("c.txt adds the deploy root", False)]},
                prepare=prepare, one_workspace=True, with_reason=True)
        record(name, signal.startswith("block:wait-claim") and "declaration is not closed" in out,
               "{0} | {1}".format(signal, out[:160]))

    # A landing ask is honest while the open item waits on that landing: the repo's main checkout holds another
    # session's PASSED record that is not on origin, so no work can go in there without staling it, and the ask names
    # that checkout. origin_head and landed are stubbed (the fixture has no origin); the record is read for real.
    statuses_for_landing = gate.gate_status_module()
    cadence_for_landing = gate.cadence_module()
    for name, peer_record, named, slashes, expected in (
            ("landing-a-push-ask-naming-a-main-checkout-no-peer-green-holds-blocks", False, "Repo", False,
             "block:landing-work"),
            ("landing-a-push-ask-naming-a-longer-path-that-starts-with-the-held-checkout-blocks", True, "RepoArchive",
             False, "block:landing-work"),
            ("landing-control-a-push-ask-naming-the-main-checkout-a-peer-green-holds-allows", True, "Repo", False,
             "allow:blocker-token"),
            ("landing-control-the-held-checkout-written-with-forward-slashes-allows", True, "Repo", True,
             "allow:blocker-token")):
        if not h.wanted(name):
            continue
        dirty = worked_dirty_repo(parked=True)
        dirty.session = name

        def prepare(workspace, dirty=dirty, peer_record=peer_record):
            dirty(workspace)
            if peer_record:
                state_dir = statuses_for_landing.checkout_state_dir("Repo", os.path.join(workspace, "Repo"))
                os.makedirs(state_dir, exist_ok=True)
                with open(statuses_for_landing.record_path("Repo", state_dir), "w", encoding="utf-8") as fh:
                    json.dump({"repo": "Repo", "verdict": "PASSED", "completedAt": "2099-01-01T00:00:00Z",
                               "session": "another-session", "machine": "fixture", "steps": []}, fh)

        def asks_to_land(workspace, named=named, slashes=slashes):
            path = os.path.join(workspace, named)
            return [user("do the thing"), edit_use(os.path.join(workspace, "Repo", "a.txt")), tool_result(),
                    assistant("NEEDED: push {0} so Repo/c.txt adds the deploy root can start.".format(
                        path.replace("\\", "/") if slashes else path) + block)]

        saved = (cadence_for_landing.origin_head, cadence_for_landing.landed)
        cadence_for_landing.origin_head = lambda checkout: ("0" * 40, 0.0, "fixture")
        cadence_for_landing.landed = lambda checkout, head: False
        try:
            with RealWaitChecks():
                verdict, signal, out = isolated_stop(
                    h, name, asks_to_land,
                    declaration={"Repo": [("a.txt holds the change", True), ("c.txt adds the deploy root", False)]},
                    prepare=prepare, one_workspace=True, with_reason=True)
        finally:
            cadence_for_landing.origin_head, cadence_for_landing.landed = saved
        record(name, signal.startswith(expected), "{0} | {1}".format(signal, out[:240]))

    # A session in its own worktree whose Tools gate the cadence hook refuses because a peer's green worktree is not on
    # origin: a NEEDED: landing ask naming that peer worktree's path counts the session's worktree as blocked.
    peer_tree = os.path.join(h.root, "worktrees", "peer-session", "Tools")
    own_tree = os.path.join(h.root, "worktrees", "own-session", "Tools")
    peer_cadence = types.SimpleNamespace(peer_green_unlanded=lambda command, workspace: [peer_tree])
    for name, text, expected in (
            ("landing-a-push-ask-naming-a-peer-green-worktree-counts-the-own-worktree-blocked",
             "NEEDED: you to commit and push the master's green Tools change in {0}. The cadence hook refuses."
             .format(peer_tree), True),
            ("landing-control-a-push-ask-naming-no-peer-worktree-leaves-the-own-worktree-unblocked",
             "NEEDED: you to commit and push the master's green Tools change. The cadence hook refuses.", False),
            ("landing-control-a-wait-naming-the-peer-worktree-without-a-landing-ask-leaves-it-unblocked",
             "NEEDED: your review of {0}.".format(peer_tree), False)):
        if h.wanted(name):
            blocked = gate.gate_blocked(peer_cadence, os.path.join(h.root, "own-session.jsonl"), h.root, text)
            got = blocked("Tools", own_tree)
            record(name, got is expected, got)

    # Files a turn wrote by command (git apply, npm ci) owe a commit message like edited ones: the real git status of a
    # real repo decides what is dirty, and only a file an item of the session names is the session's.
    command_root = os.path.join(h.root, "command-written")
    repo_path, files = make_repo(command_root, "Repo", {"applied.txt": "dirty", "peer.txt": "dirty"})
    declaration = os.path.join(command_root, "declaration.json")
    with open(declaration, "w", encoding="utf-8") as fh:
        json.dump({"repos": {"Repo": [{"item": "applied.txt holds the kept draft", "done": True}]}}, fh)
    real_cadence = gate.cadence_module()
    stub = types.SimpleNamespace(
        command_changed_checkout=real_cadence.command_changed_checkout,
        workspace_checkouts=lambda _ws: [(os.path.normcase(os.path.abspath(repo_path)), "Repo")],
        work_product_path=lambda *_: declaration, names_changed_path=real_cadence.names_changed_path)
    saved_cadence = gate.cadence_module
    gate.cadence_module = lambda: stub
    try:
        for name, commands, wanted, unwanted in (
                ("commit-owed-must-block-a-file-a-git-apply-wrote-is-the-sessions",
                 ['git -C "{0}" apply "draft.diff"'.format(repo_path)], ["applied.txt"], ["peer.txt"]),
                ("commit-owed-must-block-a-file-an-npm-ci-wrote-is-the-sessions",
                 ['npm ci --prefix "{0}"'.format(repo_path)], ["applied.txt"], ["peer.txt"]),
                ("commit-owed-control-a-dry-run-apply-writes-nothing",
                 ['git -C "{0}" apply --check "draft.diff"'.format(repo_path)], [], ["applied.txt", "peer.txt"]),
                ("commit-owed-must-block-a-file-a-git-merge-wrote-is-the-sessions",
                 ['git -C "{0}" merge --ff-only origin/main'.format(repo_path)], ["applied.txt"], ["peer.txt"]),
                ("commit-owed-control-a-merge-base-query-writes-nothing",
                 ['git -C "{0}" merge-base --is-ancestor 3ba86a7 HEAD'.format(repo_path)], [],
                 ["applied.txt", "peer.txt"]),
                ("commit-owed-control-no-command-owes-nothing", [], [], ["applied.txt", "peer.txt"])):
            if not h.wanted(name):
                continue
            got = {os.path.basename(path) for path in
                   gate.command_written_files(commands, os.path.join(command_root, "s.jsonl"), command_root)}
            record(name, all(item in got for item in wanted) and not any(item in got for item in unwanted), sorted(got))
    finally:
        gate.cadence_module = saved_cadence

    # A pull the session cannot finish: the checkout holds an unconcluded merge, whose commit is the operator's, so a
    # landing ask naming it is blocked on exactly that, and its RED-at-origin record asks for no fix of the session's.
    merge_root = os.path.join(h.root, "mid-merge")
    merging_repo, _ = make_repo(merge_root, "Repo", {"a.txt": "clean"})
    quiet = {"capture_output": True, "text": True, "cwd": merging_repo}
    base = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], **quiet).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "-b", "incoming"], **quiet)
    with open(os.path.join(merging_repo, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("incoming\n")
    subprocess.run(["git", "commit", "-qam", "incoming"], **quiet)
    subprocess.run(["git", "checkout", "-q", base], **quiet)
    with open(os.path.join(merging_repo, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("local\n")
    subprocess.run(["git", "commit", "-qam", "local"], **quiet)
    subprocess.run(["git", "merge", "--no-ff", "--no-commit", "incoming"], **quiet)
    no_peer = types.SimpleNamespace(peer_green_unlanded=lambda command, workspace: [])
    for name, text, expected in (
            ("merge-pending-a-landing-ask-naming-the-mid-merge-checkout-counts-it-blocked",
             "NEEDED: you to commit the merge in {0}, since git writes are yours.".format(merging_repo), True),
            ("merge-pending-control-the-same-ask-naming-no-checkout-leaves-it-unblocked",
             "NEEDED: you to commit the merge, since git writes are yours.", False)):
        if h.wanted(name):
            blocked = gate.gate_blocked(no_peer, os.path.join(merge_root, "s.jsonl"), merge_root, text)
            got = blocked("Repo", merging_repo)
            record(name, got is expected, got)
    name = "merge-pending-a-red-record-mid-merge-asks-no-fix"
    if h.wanted(name):
        got = gate.failed_on_this_tree("Repo", merge_root, "s", merging_repo)
        record(name, got is False, got)
    name = "merge-pending-control-the-merge-aborted-is-no-merge"
    if h.wanted(name):
        subprocess.run(["git", "merge", "--abort"], **quiet)
        got = gate.merge_pending(merging_repo)
        record(name, got is False, got)

    name = "parking-the-veto-imports-from-a-copy-holding-only-hooks-and-gates"
    if h.wanted(name):
        hooks_dir = os.path.dirname(os.path.abspath(__file__))
        deployed = tempfile.mkdtemp(prefix="hooks-only-", dir=h.root)
        for folder in ("Hooks", "Gates"):
            source = os.path.join(os.path.dirname(hooks_dir), folder)
            os.makedirs(os.path.join(deployed, folder))
            for entry in os.listdir(source):
                if entry.endswith(".py") and os.path.isfile(os.path.join(source, entry)):
                    shutil.copy2(os.path.join(source, entry), os.path.join(deployed, folder, entry))
        probe = os.path.join(deployed, "probe.py")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("import sys\nsys.path.insert(0, sys.argv[1])\nimport parking_lot_commits\n"
                     "print('problems:', parking_lot_commits.problems(sys.argv[2], 'probe-session'))\n")
        empty = os.path.join(deployed, "workspace")
        os.makedirs(empty)
        ran = subprocess.run([sys.executable, "-I", probe, os.path.join(deployed, "Hooks"), empty],
                             capture_output=True, text=True)
        record(name, ran.returncode == 0 and "problems: []" in ran.stdout, (ran.stdout + ran.stderr).strip()[-300:])

    goal_pinned = [user("GOAL: ship the dispatcher"), tool_use(), tool_result()]
    for name, closing, expected in (
            ("goal-a-needed-line-that-omits-the-goal-blocks", "NEEDED: the operator's choice of deploy root.",
             "block:goal:1"),
            ("goal-a-needed-line-restating-the-goal-allows",
             "NEEDED: the operator's choice of deploy root to ship the dispatcher.", "allow:blocker-token"),
            ("goal-a-question-that-omits-the-goal-blocks", "Which deploy root should I use?", "block:goal:1"),
            ("goal-a-question-with-a-goal-line-allows", "GOAL: ship the dispatcher\nWhich deploy root should I use?",
             "allow:question-mark"),
            ("goal-control-a-question-with-no-goal-pinned-allows", None, "allow:question-mark"),
            ("goal-a-restated-goal-ending-in-a-question-mark-is-not-a-question",
             "Status of the work, nothing asked.\nGOAL: ship the dispatcher?", "block:1"),
            ("parking-a-parked-question-and-its-link-earn-no-stop",
             "GOAL: ship the dispatcher\nParked in the lot: https://claude.ai/artifact/0123456789abcdef", "block:1")):
        if h.wanted(name):
            records = (goal_pinned + [assistant(closing)]) if closing else \
                [user("do the thing"), tool_use(), tool_result(), assistant("Which deploy root should I use?")]
            verdict, signal = isolated_stop(h, name, records)
            record(name, signal == expected, signal)

    block = "```\nAdd the dispatcher\n\nClaude/invoke_hook_dispatcher.py\n```"
    for name, closing, expected in (
            ("commit-ask-a-needed-commit-with-no-message-blocks", "NEEDED: the operator to commit Tools.",
             "block:commit-ask:1"),
            ("commit-ask-a-closing-question-to-commit-and-push-with-no-message-blocks",
             "The work is ready.\nCan you commit and push?", "block:commit-ask:1"),
            ("commit-ask-a-trailing-second-question-never-hides-the-first",
             "The work is ready.\nShould I commit Tools? Or keep going?", "block:commit-ask:1"),
            ("commit-ask-control-a-status-sentence-mentioning-a-commit-owes-no-block",
             "The work is not ready to commit yet. Which deploy root should the copy use?", "allow:question-mark"),
            ("commit-ask-a-pointer-to-an-earlier-message-is-no-message",
             "NEEDED: the operator to commit Tools with the commit messages given in my previous message.",
             "block:commit-ask:1"),
            ("commit-ask-control-the-same-needed-with-its-commit-block-allows",
             block + "\nNEEDED: the operator to commit Tools.", "allow:blocker-token"),
            ("commit-ask-control-a-needed-about-something-else-owes-no-block",
             "NEEDED: the operator's choice of deploy root.", "allow:blocker-token"),
            ("commit-ask-control-a-past-tense-commit-is-no-ask",
             "NEEDED: your approval of the fix for a file whose earlier edit was committed.", "allow:blocker-token"),
            ("commit-ask-control-a-compound-noun-is-no-ask",
             "NEEDED: your yes or no on the commit-block check blaming a later writer.", "allow:blocker-token"),
            ("commit-ask-control-a-commit-message-noun-is-no-ask",
             "NEEDED: your approval of the scope; the commit message waits for the gate.", "allow:blocker-token"),
            ("commit-ask-control-reverting-pushed-work-is-no-ask",
             "NEEDED: your call, since striking them means reverting pushed work.", "allow:blocker-token"),
            ("commit-ask-control-a-noun-after-an-adjective-is-no-ask",
             "Should I stay idle until session 36a1b2c3 sends the final commit?", "allow:question-mark"),
            ("commit-ask-control-a-noun-after-two-adjectives-is-no-ask",
             "NEEDED: your review of the next clean push before the deploy.", "allow:blocker-token"),
            ("commit-ask-a-verb-before-an-adjective-and-noun-still-blocks",
             "The fix is ready.\nCan you commit the final fix?", "block:commit-ask:1"),
            ("commit-ask-the-operator-to-commit-through-two-words-still-blocks",
             "NEEDED: the operator to please commit Tools.", "block:commit-ask:1"),
            ("commit-ask-control-have-before-a-commit-noun-is-no-ask",
             "The tests pass.\nHave the commit messages been drafted?", "allow:question-mark"),
            ("commit-ask-control-let-before-a-commit-noun-is-no-ask",
             "NEEDED: your yes; let the commit message wait for the gate.", "allow:blocker-token"),
            ("commit-ask-control-make-before-a-commit-noun-is-no-ask",
             "NEEDED: your yes to make the commit block name every file.", "allow:blocker-token"),
            ("commit-ask-control-a-commit-message-with-no-article-is-no-ask",
             "NEEDED: your approval to draft one commit message per repo.", "allow:blocker-token"),
            ("commit-ask-having-someone-commit-still-blocks",
             "The fix is ready.\nCan you please have the agents commit Tools?", "block:commit-ask:1"),
            ("commit-ask-a-second-verb-after-a-noun-phrase-still-blocks",
             "The fix is ready.\nCan you push the branch and commit?", "block:commit-ask:1"),
            ("commit-ask-a-refusal-until-the-operator-commits-with-no-message-blocks",
             "REFUSING: to go further: \"stop here until I commit Tools\".", "block:commit-ask:1"),
            ("commit-ask-control-the-same-refusal-with-its-commit-block-allows",
             block + "\nREFUSING: to go further: \"stop here until I commit Tools\".", "allow:refusal-token"),
            ("commit-ask-control-a-refusal-naming-no-landing-allows",
             "REFUSING: to delete it: \"the production table is mine to delete\".", "allow:refusal-token"),
            ("commit-ask-control-a-refusal-to-push-unasked-is-no-commit-ask",
             "REFUSING: \"never push without an explicit instruction\".", "allow:refusal-token"),
            ("commit-ask-control-a-refusal-to-push-before-review-is-no-commit-ask",
             "REFUSING: \"no push before I review it\".", "allow:refusal-token")):
        if h.wanted(name):
            operator_words = ("do the thing. stop here until I commit Tools. the production table is mine to delete. "
                              "never push without an explicit instruction. no push before I review it.")
            _, signal, _ = h.run(name, [user(operator_words), tool_use(), tool_result(), assistant(closing)])
            record(name, signal == expected, signal)

    ask = "NEEDED: your choice of deploy root, which only you can make."
    question = "Which deploy root should the copy use?"
    for name, closing, expected in (
            ("no-bounce-the-first-needed-after-an-operator-message-earns-the-stop", ask, "allow:blocker-token"),
            ("no-bounce-the-first-question-after-an-operator-message-earns-the-stop", question, "allow:question-mark")):
        if h.wanted(name):
            _, signal, out = h.run(name, [user("do the thing"), tool_use(), assistant(closing)])
            record(name, signal == expected and not out, signal)
    if h.wanted("an-ask-allowed-once-is-blocked-as-a-repeat"):
        path = h.transcript("ask-repeat", [user("do the thing"), tool_use(), assistant(ask)])
        state = os.path.join(h.root, "state-ask-repeat")
        _, first, _ = h.run_path("ask-repeat", path, state=state)
        append(path, [tool_use(), assistant(ask)])
        _, second, _ = h.run_path("ask-repeat", path, state=state)
        record("an-ask-allowed-once-is-blocked-as-a-repeat",
               first == "allow:blocker-token" and second.startswith("block:repeat"), "{0}, then {1}".format(first, second))
    if h.wanted("an-ask-naming-no-open-item-gets-the-open-item-block"):
        name = "an-ask-naming-no-open-item-gets-the-open-item-block"
        base = os.path.join(h.root, "open-item-" + name)
        workspace = os.path.join(base, "workspace")
        os.makedirs(workspace)
        os.makedirs(session_home.work_products_dir(), exist_ok=True)
        path = h.transcript(name, [user("do the thing"), tool_use(), assistant(ask)])
        stem = os.path.splitext(os.path.basename(path))[0]
        with open(session_home.declaration_path(workspace, stem), "w", encoding="utf-8") as fh:
            json.dump({"repos": {"Functions": [{"item": "the integration move", "done": False}]}}, fh)
        payload = {"hook_event_name": "Stop", "transcript_path": path, "session_id": name, "stop_hook_active": False,
                   "last_assistant_message": ask}
        signal, _ = gate.decide(payload, path, os.path.join(base, "state"), 100, git_timeout=None,
                                gate_workspace=workspace, temp_root=os.path.join(base, "temp"),
                                quarantine_root=os.path.join(base, "quarantine"))
        record(name, signal.startswith("block:open-item:"), signal)

    goal_ask = "What GOAL should this session pin?"
    pinned_ask = "Should the GOAL \"tidy the hooks\" also cover the deploy?"
    for name, records, expected in (
            ("goal-question-with-no-goal-pinned-earns-the-stop",
             [user("help me with the hooks"), assistant(goal_ask)], "allow:question-mark"),
            ("goal-question-on-the-first-operator-message-earns-the-stop-even-with-a-goal",
             [user("GOAL: tidy the hooks"), assistant(pinned_ask)], "allow:question-mark"),
            ("goal-question-control-a-question-not-asking-for-the-goal-must-be-parked",
             [user("help me with the hooks"), assistant(question)], "block:unparked-question:1")):
        if h.wanted(name):
            _, signal, _ = h.run(name, records, require_parked=True)
            record(name, signal == expected, signal)
    for name, closing in (
            ("goal-question-control-a-later-message-with-a-goal-in-force-must-be-parked", pinned_ask),
            ("goal-question-control-a-question-only-mentioning-the-goal-later-must-be-parked",
             "Does this meet your goal \"tidy the hooks\"?")):
        if not h.wanted(name):
            continue
        path = h.transcript(name, [user("GOAL: tidy the hooks"), tool_use(), assistant("Working on it.")])
        state = h.state_dir(name)
        h.run_path(name, path, state=state, require_parked=True)
        append(path, [user("keep going", prompt_id="p-2"), tool_use(), assistant(closing)])
        _, second, _ = h.run_path(name, path, state=state, require_parked=True)
        record(name, second == "block:unparked-question:1", second)

    # A CLOSING QUESTION IS PARKED FIRST: an ArtifactData write to the page's questions store, matched by its words
    # or by its id.
    def parking(doc_id, text, batch=False, collection="questions", error=False, context=None):
        data = {"question": text, "status": "open"} if context is None else {"question": text, "context": context,
                                                                             "status": "open"}
        write = {"collection": collection, "doc_id": doc_id, "data": data}
        call = ({"action": "batch", "url": "https://claude.ai/artifact/x", "writes": [write]} if batch
                else dict(write, action="set", url="https://claude.ai/artifact/x"))
        result = {"type": "tool_result", "tool_use_id": "tu-p", "content": "Permission denied" if error else "committed"}
        if error:
            result["is_error"] = True
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tu-p", "name": "ArtifactData", "input": call}]}},
                {"type": "user", "message": {"role": "user", "content": [result]}}]

    def open_read():
        """A query for answered questions that returns none: the page read that shows every parked question open."""
        call = {"action": "query", "url": "https://claude.ai/artifact/x", "collection": "questions",
                "query": {"where": [["status", "==", "answered"]]}}
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tu-o", "name": "ArtifactData", "input": call}]}},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu-o", "content": "No documents matched in collection \"questions\"."}]}}]

    root_ask = "Which deploy root should the copy use (q6)?"
    unrelated = "Do you want me to delete the Redis cache now?"
    if h.wanted("parking-the-unparked-reason-spells-out-the-park-the-read-and-the-id"):
        reason = gate.UNPARKED_QUESTION_REASON
        record("parking-the-unparked-reason-spells-out-the-park-the-read-and-the-id",
               all(step in reason for step in ("doc_id qN-slug", "askedAt", "status == answered", "naming its qN")),
               "ok")
    name = "parking-following-the-unparked-reason-park-read-then-ask-by-id-earns-the-stop"
    if h.wanted(name):
        _, got, _ = h.run(name, [user("help me with the hooks"), tool_use()]
                          + parking("q6-deploy-root", "Which deploy root should the copy use?") + open_read()
                          + [assistant(root_ask)], require_parked=True)
        record(name, got == "allow:question-mark", got)
    name = "refused-ask-control-an-ask-refused-as-unparked-was-never-one-to-stand-on-so-parked-it-earns-the-stop"
    if h.wanted(name):
        state = h.state_dir(name)
        path = h.transcript(name, [user("help me with the hooks"), tool_use(), assistant(root_ask)])
        _, first, _ = h.run_path(name, path, state=state, require_parked=True)
        append(path, [tool_use()] + parking("q6-deploy-root", "Which deploy root should the copy use?") + open_read()
               + [assistant(root_ask)])
        _, second, _ = h.run_path(name, path, state=state, stop_hook_active=True, require_parked=True)
        record(name, first == "block:unparked-question:1" and second == "allow:question-mark",
               "first={0}, then {1}".format(first, second))
    for name, parked, closing, expected in (
            ("parking-a-question-never-parked-is-blocked", [], question, "block:unparked-question:1"),
            ("parking-a-question-parked-in-its-own-words-earns-the-stop",
             parking("deploy-root", question), question, "allow:question-mark"),
            ("parking-a-question-naming-its-parked-id-earns-the-stop",
             parking("q6-deploy-root", "Which deploy root should the copy use?", batch=True), root_ask,
             "allow:question-mark"),
            ("parking-control-a-placeholder-row-named-by-id-parks-nothing",
             parking("q99-placeholder", "Placeholder."), "Do you want me to delete the Redis cache now (q99)?",
             "block:unparked-question:1"),
            ("parking-a-row-holding-the-question-named-by-id-earns-the-stop",
             parking("q99-redis-cache", "Do you want me to delete the Redis cache now?"),
             "Do you want me to delete the Redis cache now (q99)?", "allow:question-mark"),
            ("parking-a-question-naming-its-id-in-the-possessive-earns-the-stop",
             parking("q10-review-findings", "Do you approve the three review findings?"),
             "Do you approve q10's three findings?", "allow:question-mark"),
            ("parking-control-a-slug-id-named-whole-parks-nothing",
             parking("can-do", "Pick the copy's install folder"), "Is this a can-do plan for today?",
             "block:unparked-question:1"),
            ("parking-control-a-one-word-parked-text-parks-nothing",
             parking("notes", "you?"), unrelated, "block:unparked-question:1"),
            ("parking-control-a-few-word-parked-text-parks-nothing",
             parking("notes", "Do you want?"), unrelated, "block:unparked-question:1"),
            ("parking-the-same-question-parked-verbatim-earns-the-stop",
             parking("notes", unrelated), unrelated, "allow:question-mark"),
            ("parking-control-a-hyphen-id-inside-a-word-parks-nothing",
             parking("-", "Pick the copy's install folder"), "Should I re-run the gate on the fixed tree?",
             "block:unparked-question:1"),
            ("parking-control-a-hyphen-fragment-id-parks-nothing",
             parking("e-", "Pick the copy's install folder"), "Should I re-run the gate on the fixed tree?",
             "block:unparked-question:1"),
            ("parking-control-a-bare-word-id-unlocks-nothing",
             parking("you", "Pick the copy's install folder"), unrelated, "block:unparked-question:1"),
            ("parking-control-a-short-id-parks-only-the-question-naming-it",
             parking("q7-matrix-config", "Pick the copy's install folder"), unrelated, "block:unparked-question:1"),
            ("parking-control-a-write-whose-result-is-an-error-parks-nothing",
             parking("q8-matrix", question, error=True), question, "block:unparked-question:1"),
            ("parking-control-a-write-to-another-collection-is-not-parking",
             parking("deploy-root", question, collection="log"), question, "block:unparked-question:1"),
            # A NEEDED: IS PARKED FIRST too: session crgolden-6f ended turns on a NEEDED: q4 it had invented.
            ("parking-must-block-a-needed-line-never-parked", [], ask, "block:unparked-needed:1"),
            ("parking-a-needed-naming-its-parked-id-earns-the-stop", parking("q6-deploy-root", question),
             "NEEDED: your answer on q6, which only you can choose.", "allow:blocker-token"),
            ("parking-a-needed-asking-the-parked-question-again-earns-the-stop", parking("deploy-root", question),
             "NEEDED: " + question, "allow:blocker-token"),
            ("parking-control-a-needed-naming-a-placeholder-row-parks-nothing", parking("q99-placeholder", "Placeholder."),
             "NEEDED: your answer on q99, which only you can choose.", "block:unparked-needed:1"),
            ("parking-control-a-needed-naming-an-id-never-parked-parks-nothing", parking("q6-deploy-root", question),
             "NEEDED: your answer on q7, which only you can choose.", "block:unparked-needed:1"),
            ("parking-control-a-needed-whose-park-write-failed-parks-nothing",
             parking("q6-deploy-root", question, error=True), "NEEDED: your answer on q6, which only you can choose.",
             "block:unparked-needed:1"),
            ("parking-control-one-unparked-needed-beside-a-parked-one-is-blocked", parking("q6-deploy-root", question),
             "NEEDED: your answer on q6, which only you can choose.\nNEEDED: the Redis password, which only you hold.",
             "block:unparked-needed:1"),
            ("parking-a-needed-asking-for-a-goal-with-none-pinned-earns-the-stop", [],
             "NEEDED: a goal for this session, set with /goal, which only you can pin.", "allow:blocker-token")):
        if h.wanted(name):
            _, signal, _ = h.run(name, [user("do the thing"), tool_use()] + parked + open_read() + [assistant(closing)],
                                 require_parked=True)
            record(name, signal == expected, signal)

    # A HOOK REFUSING EVERY TOOL tells the session to end on a NEEDED:, which no page write can park first.
    def denial(text, error=True):
        result = {"type": "tool_result", "tool_use_id": "tu-d", "content": text}
        if error:
            result["is_error"] = True
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tu-d", "name": "PowerShell", "input": {"command": "git status"}}]}},
                {"type": "user", "message": {"role": "user", "content": [result]}}]

    gate_dispatcher_reason = ("Blocked: a hook gate could not judge this call. Do not retry and do not work around it:"
                              " end the turn with a NEEDED: line naming the failing gate.")
    dispatcher_down = denial(gate_dispatcher_reason)
    for name, rows, expected in (
            ("denied-a-needed-after-a-hook-said-to-end-on-one-earns-the-stop", dispatcher_down, "allow:blocker-token"),
            ("denied-control-a-tool-that-ran-after-the-denial-must-park-it",
             dispatcher_down + denial("ok", error=False), "block:unparked-needed:1"),
            ("denied-control-the-same-words-in-a-successful-result-exempt-nothing",
             denial(gate_dispatcher_reason, error=False), "block:unparked-needed:1")):
        if h.wanted(name):
            _, signal, _ = h.run(name, [user("do the thing"), tool_use()] + rows
                                 + [assistant("NEEDED: the failing gate repaired, which only you can do.")],
                                 require_parked=True)
            record(name, signal == expected, signal)

    # A WAIT THE DECLARATION RECORDS: every open item pending on a parked question, and a NEEDED: naming it, may
    # stand again until the operator answers; any open item not pending, or an unparked id, keeps the repeat block.
    def mark_pending(pending):
        return [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "tu-m", "name": "Edit",
             "input": {"file_path": "C:\\t\\claude\\work-products\\session.json", "old_string": "\"done\": false",
                       "new_string": "\"done\": false, \"pending\": \"" + pending + "\""}}]}}]

    wait_line = "NEEDED: your answer on q13, parked; every open item waits on it."
    waiting_on_q13 = {"Tools": [("Fix the teardown", False, "q13-teardown")]}
    parked_then_marked = parking("q13-teardown", "Pick the teardown") + mark_pending("q13-teardown")
    for name, parked_rows, declaration, line, expected in (
            ("pending-a-needed-naming-the-parked-question-every-item-waits-on-stands-again",
             parked_then_marked, waiting_on_q13, wait_line, "allow:blocker-token"),
            ("pending-control-an-open-item-not-pending-keeps-the-repeat-block",
             parked_then_marked, {"Tools": [("Fix the teardown", False, "q13-teardown"), ("Write the docs", False)]},
             wait_line, "block:repeat-question:1"),
            ("pending-control-a-pending-id-never-parked-keeps-the-repeat-block",
             mark_pending("q13-teardown"), waiting_on_q13, wait_line, "block:repeat-question:1"),
            ("pending-control-a-question-parked-after-the-item-was-marked-keeps-the-repeat-block",
             mark_pending("q13-teardown") + parking("q13-teardown", "Pick the teardown"), waiting_on_q13, wait_line,
             "block:repeat-question:1"),
            ("pending-control-a-pending-id-not-named-keeps-the-repeat-block",
             parking("q13-teardown", "Pick the teardown") + mark_pending("q14-other"),
             {"Tools": [("Fix the teardown", False, "q14-other")]}, wait_line, "block:repeat-question:1"),
            ("pending-control-a-wait-line-asking-something-new-keeps-the-repeat-block",
             parked_then_marked, waiting_on_q13, "NEEDED: your answer on q13, parked; should I also fix the docs?",
             "block:repeat-question:1"),
            ("pending-control-a-wait-line-naming-no-short-id-keeps-the-repeat-block",
             parked_then_marked, waiting_on_q13, "NEEDED: your answer on the teardown question, parked.",
             "block:repeat-question:1"),
            # Session 590c41f6 finished its edits and marked them done while their approval waited on q3, which
            # left no open item and so no honest wait at all.
            ("pending-a-done-item-whose-approval-waits-on-the-parked-question-stands-again",
             parked_then_marked, {"Tools": [("Fix the teardown", True, "q13-teardown")]}, wait_line,
             "allow:blocker-token"),
            ("pending-control-a-done-item-naming-no-parked-question-keeps-the-repeat-block",
             parked_then_marked, {"Tools": [("Fix the teardown", True)]}, wait_line, "block:repeat-question:1"),
            ("pending-control-a-done-item-waiting-beside-an-open-one-that-is-not-keeps-the-repeat-block",
             parked_then_marked, {"Tools": [("Fix the teardown", True, "q13-teardown"), ("Write the docs", False)]},
             wait_line, "block:repeat-question:1")):
        if h.wanted(name):
            verdict, signal = isolated_stop(h, name, [user("do the thing"), tool_use()] + parked_rows
                                            + [assistant(line)], declaration=declaration,
                                            pending_asks=[line[len("NEEDED: "):]])
            record(name, signal == expected, signal)

    if h.wanted("pending-the-repeat-reason-names-the-declared-wait-the-rows-above-allow"):
        reason = gate.REPEATED_QUESTION_REASON
        record("pending-the-repeat-reason-names-the-declared-wait-the-rows-above-allow",
               '"pending": "<qN-slug>"' in reason and "read the page" in reason and "NEEDED: naming that qN" in reason,
               "ok")

    # The goal veto asks a NEEDED: to carry the pinned goal, so a goal ending in a question mark, quoted on the wait
    # line, is the operator's words and not a new question; any other question mark still is.
    teardown_goal = "Should the teardown move to the new host?"
    for name, line, expected in (
            ("pending-a-wait-line-quoting-a-goal-that-ends-in-a-question-mark-stands-again",
             "NEEDED: your answer on q13, parked, for \"" + teardown_goal + "\"", "allow:blocker-token"),
            ("pending-control-a-wait-line-quoting-the-goal-and-asking-something-new-keeps-the-repeat-block",
             "NEEDED: your answer on q13, parked, for \"" + teardown_goal + "\" Should I also fix the docs?",
             "block:repeat-question:1"),
            ("pending-control-a-quoted-question-that-is-not-the-goal-keeps-the-repeat-block",
             "NEEDED: your answer on q13, parked, for \"" + teardown_goal + "\" and \"Should the docs move?\"",
             "block:repeat-question:1")):
        if h.wanted(name):
            verdict, signal = isolated_stop(h, name, [user("do the thing"), goal_status(teardown_goal), tool_use()]
                                            + parked_then_marked + [assistant(line)], declaration=waiting_on_q13,
                                            pending_asks=[line[len("NEEDED: "):]])
            record(name, signal == expected, signal)

    # A parked question is one ask however it is worded: naming its id again before the operator writes is a repeat.
    # Session 590c41f6 restated q3 in three wordings, and the word-overlap check let the first two through.
    asked_q13 = "your answer on q13, parked; every open item waits on it."
    both_parked = parking("q13-teardown", "Pick the teardown") + parking("q14-docs", "Pick the docs home")
    for name, parked_rows, line, expected in (
            ("repeat-a-reworded-needed-naming-the-same-parked-id-is-a-repeat", both_parked,
             "NEEDED: q13 still decides the teardown, and nothing moves until you answer it.",
             "block:repeat-question:1"),
            ("repeat-control-a-needed-naming-another-parked-id-is-a-new-ask", both_parked,
             "NEEDED: q14 decides where the docs live, which only you can choose.", "allow:blocker-token"),
            ("repeat-control-an-id-that-was-never-parked-is-judged-by-its-words", [],
             "NEEDED: q13 still decides the teardown, and nothing moves until you answer it.",
             "allow:blocker-token")):
        if h.wanted(name):
            verdict, signal = isolated_stop(h, name, [user("do the thing"), tool_use()] + parked_rows
                                            + [assistant(line)], pending_asks=[asked_q13])
            record(name, signal == expected, signal)

    # AN EARNED STOP ENDS THE TURN OVER AN ACTIVE /goal. Claude Code's /goal is a Stop hook of its own that blocks
    # every stop until its condition is met; in session 590c41f6 it reopened each NEEDED: this gate allowed, and the
    # turn ran to the nine-block cap. Only the /goal evaluator can say a DONE: is finished, so a DONE: ends over it
    # only once the operator retired the goal (the goal-retired rows below), and a GOAL: line has no evaluator to
    # override.
    held_goal = "Add the ignore entry and move the worktree"
    for name, records, signal, expected in (
            ("goal-held-a-needed-ends-the-turn-over-an-active-goal",
             [user("do the thing"), goal_status(held_goal), tool_use()], "allow:blocker-token", True),
            ("goal-held-a-refusal-ends-the-turn-over-an-active-goal",
             [user("do the thing"), goal_status(held_goal), tool_use()], "allow:refusal-token", True),
            ("goal-held-a-queued-stop-ends-the-turn-over-an-active-goal",
             [user("do the thing"), goal_status(held_goal), tool_use()], "allow:queued-for-gate", True),
            ("goal-held-control-a-done-never-ends-over-the-goal",
             [user("do the thing"), goal_status(held_goal), tool_use()], "allow:work-complete", False),
            ("goal-held-control-a-goal-line-has-no-evaluator-to-override",
             [user("GOAL: " + held_goal), tool_use()], "allow:blocker-token", False),
            ("goal-held-control-a-met-goal-is-no-longer-active",
             [user("do the thing"), goal_status(held_goal), tool_use(), goal_status(held_goal, met=True)],
             "allow:blocker-token", False),
            ("goal-held-control-a-blocked-stop-is-not-ended",
             [user("do the thing"), goal_status(held_goal), tool_use()], "block:repeat-question:1", False)):
        if h.wanted(name):
            path = h.transcript(name, records)
            got = gate.ends_over_goal(signal, path, h.state_dir(name), name)
            record(name, got == expected, "ends_over_goal={0}".format(got))

    # A GOAL THE OPERATOR RETIRED ENDS A DONE: OVER IT. In session 93185fd1 the operator answered "The goal is finished"
    # on the parking-lot page, the DONE: quoting it was allowed, and the /goal evaluator, which reads neither the page
    # nor a goal whose steps had become impossible, reopened every stop until the nine-block cap. Only the operator's
    # own words retiring the goal count: never the pinned goal's text, a quote that retires nothing, a negated one, an
    # answer never read back, or words said before the goal was pinned.
    def page_read_back(answer, read=True):
        publish = [{"type": "assistant", "message": {"content": [
                       {"type": "tool_use", "id": "tu-pub", "name": "Artifact",
                        "input": {"file_path": "C:\\scratch\\parking-lot.html"}}]}},
                   {"type": "user", "message": {"role": "user", "content": [
                       {"type": "tool_result", "tool_use_id": "tu-pub",
                        "content": "Published at https://claude.ai/artifact/retire-page"}]}}]
        row = json.dumps({"id": "q9", "data": {"status": "answered", "answer": answer,
                                               "answeredAt": "2026-10-27T17:11:04Z", "question": "Finished?"}})
        return publish + ([{"type": "assistant", "message": {"content": [
                               {"type": "tool_use", "id": "tu-read", "name": "ArtifactData",
                                "input": {"action": "get", "url": "https://claude.ai/artifact/retire-page",
                                          "collection": "questions", "doc_id": "q9"}}]}},
                           {"type": "user", "message": {"role": "user", "content": [
                               {"type": "tool_result", "tool_use_id": "tu-read",
                                "content": "1 document from collection \"questions\":\n" + row + "\n"}]}}]
                          if read else [])

    def at(record_, stamp):
        return dict(record_, timestamp=stamp)

    retire_goal = "pair with the peer until the work is reviewed"
    pinned = [at(user("do the thing"), "2026-10-27T10:00:00Z"), at(goal_status(retire_goal), "2026-10-27T10:00:01Z"),
              tool_use()]
    done_quoting = "DONE: the goal \"{0}\" is finished, as you answered \"{1}\".\nVERIFIED: git status printed clean."
    goal_words_goal = "keep going until the goal is finished everywhere"
    for name, records, text, expected in (
            ("goal-retired-must-pass-a-done-quoting-the-operator-retiring-the-goal-ends-over-it",
             pinned + [at(user("The goal is finished", "p-2"), "2026-10-27T11:00:00Z")],
             done_quoting.format(retire_goal, "The goal is finished"), True),
            ("goal-retired-must-pass-a-page-answer-read-back-retiring-the-goal-ends-over-it",
             pinned + page_read_back("The goal is finished"),
             done_quoting.format(retire_goal, "The goal is finished"), True),
            ("goal-retired-must-block-a-page-answer-never-read-back-counts-for-nothing",
             pinned + page_read_back("The goal is finished", read=False),
             done_quoting.format(retire_goal, "The goal is finished"), False),
            ("goal-retired-must-block-a-quote-that-retires-nothing-leaves-the-goal-to-its-evaluator",
             pinned + [at(user("checked in and pushed to main", "p-2"), "2026-10-27T11:00:00Z")],
             done_quoting.format(retire_goal, "checked in and pushed to main"), False),
            ("goal-retired-must-block-a-negated-quote-retires-nothing",
             pinned + [at(user("The goal is not finished", "p-2"), "2026-10-27T11:00:00Z")],
             done_quoting.format(retire_goal, "The goal is not finished"), False),
            ("goal-retired-must-block-words-said-before-the-goal-was-pinned-retire-nothing",
             [at(user("The goal is finished"), "2026-10-27T09:00:00Z"),
              at(goal_status(retire_goal), "2026-10-27T10:00:01Z"), tool_use()],
             done_quoting.format(retire_goal, "The goal is finished"), False),
            ("goal-retired-must-block-the-pinned-goals-own-words-never-retire-it",
             [at(user("/goal " + goal_words_goal), "2026-10-27T10:00:00Z"),
              at(goal_status(goal_words_goal), "2026-10-27T10:00:01Z"), tool_use()],
             done_quoting.format(goal_words_goal, "the goal is finished everywhere"), False),
            ("goal-retired-must-block-a-retiring-quote-outside-the-done-line-retires-nothing",
             pinned + [at(user("The goal is finished", "p-2"), "2026-10-27T11:00:00Z")],
             "You said \"The goal is finished\".\nDONE: \"" + retire_goal + "\" is finished.\nVERIFIED: clean.",
             False),
            ("goal-retired-control-with-no-active-goal-there-is-nothing-to-end-over",
             [user("do the thing"), tool_use(), at(user("The goal is finished", "p-2"), "2026-10-27T11:00:00Z")],
             done_quoting.format(retire_goal, "The goal is finished"), False)):
        if h.wanted(name):
            path = h.transcript(name, records)
            got = gate.ends_over_goal("allow:work-complete", path, h.state_dir(name), name, text)
            record(name, got == expected, "ends_over_goal={0}".format(got))

    page_question = "Should the origin step stay as it is for now?"
    page_answer = "Leave the origin step as it is for now"

    def page_answered(read=True, published=True):
        url = "https://claude.ai/artifact/corpus-page"
        publish = [{"type": "assistant", "message": {"content": [
                       {"type": "tool_use", "id": "tu-cpub", "name": "Artifact",
                        "input": {"file_path": "C:\\scratch\\parking-lot.html"}}]}},
                   {"type": "user", "message": {"role": "user", "content": [
                       {"type": "tool_result", "tool_use_id": "tu-cpub", "content": "Published at " + url}]}}]
        row = json.dumps({"id": "q3-origin", "data": {"status": "answered", "answer": page_answer,
                                                      "answeredAt": "2026-10-28T04:32:15Z", "question": page_question}})
        read_back = [{"type": "assistant", "message": {"content": [
                         {"type": "tool_use", "id": "tu-cread", "name": "ArtifactData",
                          "input": {"action": "get", "url": url, "collection": "questions", "doc_id": "q3-origin"}}]}},
                     {"type": "user", "message": {"role": "user", "content": [
                         {"type": "tool_result", "tool_use_id": "tu-cread",
                          "content": "1 document from collection \"questions\":\n" + row + "\n"}]}}]
        return [user("do the thing"), tool_use()] + (publish if published else []) + (read_back if read else [])

    refusing = "REFUSING: to run the gate before the first push, resting on your answer \"{0}\"."
    claiming = "NEEDED: the first push, since you said \"{0}\"."
    for name, records, check, quote, expected in (
            ("page-corpus-must-pass-a-refusal-resting-on-a-page-answer-read-back-is-checkable",
             page_answered(), "refusal", page_answer, None),
            ("page-corpus-must-block-a-refusal-resting-on-an-answer-never-read-back-earns-nothing",
             page_answered(read=False), "refusal", page_answer, "in no operator message"),
            ("page-corpus-must-block-a-refusal-resting-on-another-sessions-page-earns-nothing",
             page_answered(published=False), "refusal", page_answer, "in no operator message"),
            ("page-corpus-must-block-a-refusal-quoting-the-questions-own-text-earns-nothing",
             page_answered(), "refusal", page_question.rstrip("?"), "in no operator message"),
            ("page-corpus-must-pass-an-operator-claim-quoting-a-page-answer-read-back-is-checkable",
             page_answered(), "claim", page_answer, None),
            ("page-corpus-must-block-an-operator-claim-quoting-an-answer-never-read-back-earns-nothing",
             page_answered(read=False), "claim", page_answer, "in no operator message")):
        if h.wanted(name):
            path = h.transcript(name, records)
            if check == "refusal":
                got = gate.refusal_basis_problem(refusing.format(quote), path, h.root)
            else:
                got = gate.operator_claim_problem(claiming.format(quote), "allow:blocker-token", path)
            ok = got is None if expected is None else (got is not None and expected in got)
            record(name, ok, repr(got))
    if h.wanted("goal-held-envelope"):
        ended = gate.envelope("allow:blocker-token", "", end_turn=True)
        parsed = json.loads(ended) if ended else {}
        record("goal-held-envelope-an-ended-stop-writes-continue-false",
               parsed.get("continue") is False and "blocker-token" in parsed.get("stopReason", "")
               and "decision" not in parsed, ended[:120])
        record("goal-held-envelope-control-a-stop-not-ended-writes-nothing",
               gate.envelope("allow:blocker-token", "") == "", "ok")
        blocked = json.loads(gate.envelope("block:repeat-question:1", "STOP GATE: asked already",
                                           end_turn=True))
        record("goal-held-envelope-control-a-block-still-blocks", blocked.get("decision") == "block", str(blocked))

    def asks_read(documents=(), url="https://claude.ai/artifact/x", action="query", where=(("status", "==", "open"),)):
        call = {"action": action, "url": url, "collection": "asks"}
        if action == "query":
            call["query"] = {"where": [list(clause) for clause in where]}
        printed = "\n".join(json.dumps({"id": doc_id, "data": {"status": status, "question": "Why?"}})
                            for doc_id, status in documents)
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tu-asks", "name": "ArtifactData", "input": call}]}},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu-asks",
                     "content": "{0} documents from collection \"asks\":\n{1}".format(len(documents), printed)}]}}]

    def ask_reply(doc_id, url="https://claude.ai/artifact/x", reply="Because the gate reads it."):
        call = {"action": "update", "url": url, "collection": "asks", "doc_id": doc_id, "if_version": 1,
                "data": {"reply": reply, "repliedAt": "2026-10-26T01:00:00Z", "status": "replied"}}
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tu-reply-" + doc_id, "name": "ArtifactData", "input": call}]}},
                {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "tu-reply-" + doc_id, "content": "committed"}]}}]

    # A recorded page pins parking to it: a write to another artifact's questions store parks nothing.
    for name, recorded, expected in (
            ("parking-a-write-to-the-recorded-page-earns-the-stop", "https://claude.ai/artifact/x", "allow:question-mark"),
            ("parking-control-a-write-to-another-artifact-parks-nothing", "https://claude.ai/artifact/own-page",
             "block:unparked-question:1")):
        if h.wanted(name):
            page_state = session_home.parking_dir(None, name)
            os.makedirs(page_state, exist_ok=True)
            with open(os.path.join(page_state, "state.json"), "w", encoding="utf-8") as fh:
                json.dump({"url": recorded}, fh)
            _, signal, _ = h.run(name, [user("do the thing"), tool_use()] + parking("deploy-root", question)
                                 + open_read() + asks_read(url=recorded) + [assistant(question)], require_parked=True)
            record(name, signal == expected, signal)

    # THE OPERATOR'S OWN QUESTIONS on the page's asks collection are read and replied to before any stop: a read this
    # attempt must cover every open ask, and each open ask it shows needs a replied update.
    stale_turn = {"type": "user", "message": {"role": "user",
                                              "content": gate.STOP_FEEDBACK_PREFIX + "\nSTOP GATE: keep working."}}
    for name, extra, expected in (
            ("asks-must-block-a-stop-with-no-read-of-the-asks", [], "block:unanswered-asks:1"),
            ("asks-control-a-read-showing-no-open-ask-earns-the-stop", asks_read(), "allow:question-mark"),
            ("asks-control-a-list-showing-no-open-ask-earns-the-stop", asks_read(action="list"),
             "allow:question-mark"),
            ("asks-must-block-an-open-ask-the-read-shows-with-no-reply", asks_read([("p-1", "open")]),
             "block:unanswered-asks:1"),
            ("asks-control-an-open-ask-replied-earns-the-stop", asks_read([("p-1", "open")]) + ask_reply("p-1"),
             "allow:question-mark"),
            ("asks-must-block-one-of-two-open-asks-left-unreplied",
             asks_read([("p-1", "open"), ("p-2", "open")]) + ask_reply("p-1"), "block:unanswered-asks:1"),
            ("asks-must-block-a-query-for-another-status", asks_read(where=(("status", "==", "replied"),)),
             "block:unanswered-asks:1"),
            ("asks-must-block-a-read-of-another-artifact", asks_read(url="https://claude.ai/artifact/other"),
             "block:unanswered-asks:1"),
            ("asks-must-block-a-read-from-an-earlier-attempt", asks_read() + [stale_turn, tool_use()] + open_read(),
             "block:unanswered-asks:1"),
            ("asks-must-block-a-reply-with-no-text", asks_read([("p-1", "open")]) + ask_reply("p-1", reply="  "),
             "block:unanswered-asks:1")):
        if h.wanted(name):
            page_state = session_home.parking_dir(None, name)
            os.makedirs(page_state, exist_ok=True)
            with open(os.path.join(page_state, "state.json"), "w", encoding="utf-8") as fh:
                json.dump({"url": "https://claude.ai/artifact/x"}, fh)
            _, signal, _ = h.run(name, [user("do the thing"), tool_use()] + parking("deploy-root", question)
                                 + open_read() + extra + [assistant(question)], require_parked=True)
            record(name, signal == expected, signal)

    # A SESSION THAT STARTED UNDER THE SESSION-START RULES earns no stop before its page exists, and its DONE: owes a
    # parked question for every decision its SessionStart context handed the operator.
    def session_started(text, stamp=None):
        return {"type": "attachment", "timestamp": stamp or datetime.now(timezone.utc).isoformat(), "attachment": {
            "type": "hook_additional_context", "hookEvent": "SessionStart", "hookName": "SessionStart:startup",
            "content": [text]}}

    def with_page(name):
        page_state = session_home.parking_dir(None, name)
        os.makedirs(page_state, exist_ok=True)
        with open(os.path.join(page_state, "state.json"), "w", encoding="utf-8") as fh:
            json.dump({"url": "https://claude.ai/artifact/x"}, fh)
        return name

    kept = ("OPERATOR ACTION: CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN is not set in this session: the operator must restart "
            "Claude Code from a new window to get scrollback.")
    kept_start = session_started("AGENT_MACHINE_TYPE=fixture\n" + kept)
    asked = [user("do the thing"), tool_use()] + parking("deploy-root", question) + open_read()
    finished = "DONE: the thing\nVERIFIED: python check.py printed OK."
    for name, records, page, expected in (
            ("no-page-must-block-a-question-before-the-page-exists", [kept_start] + asked + [assistant(question)], False,
             "block:no-page:1"),
            ("no-page-control-the-same-question-with-the-page-recorded-earns-the-stop",
             [session_started("AGENT_MACHINE_TYPE=fixture")] + asked + [assistant(question)], True,
             "allow:question-mark"),
            ("no-page-control-a-transcript-with-no-session-start-is-held-to-nothing", asked + [assistant(question)],
             False, "allow:question-mark"),
            ("no-page-control-a-session-start-older-than-the-rule-is-held-to-nothing",
             [session_started("AGENT_MACHINE_TYPE=fixture", "2020-01-01T00:00:00Z")] + asked + [assistant(question)],
             False, "allow:question-mark"),
            ("no-page-must-allow-the-ask-for-a-goal-with-none-pinned",
             [kept_start, user("hello")] + [assistant("No goal is pinned yet. Could you run /goal with what this "
                                                   "session should finish?")], False, "allow:question-mark"),
            ("start-decision-must-block-a-done-past-an-unparked-operator-decision",
             [kept_start, user("do the thing"), tool_use(), assistant(finished)], True,
             "block:unparked-start-decision:1"),
            ("start-decision-control-a-parked-question-quoting-the-line-allows-the-done",
             [kept_start, user("do the thing"), tool_use()]
             + parking("q2-restart", "Will you restart for scrollback?", context="The hook printed: " + kept)
             + [assistant(finished)], True, "allow:work-complete"),
            ("start-decision-must-block-a-parked-question-quoting-only-part-of-the-line",
             [kept_start, user("do the thing"), tool_use()]
             + parking("q2-restart", "Will you restart for scrollback?", context="the operator must restart Claude Code")
             + [assistant(finished)], True, "block:unparked-start-decision:1"),
            ("start-decision-control-a-question-stop-owes-no-parked-decision",
             [kept_start] + asked + [assistant(question)], True, "allow:question-mark")):
        if h.wanted(name):
            if page:
                with_page(name)
                records = records[:-1] + asks_read() + records[-1:]
            _, signal, _ = h.run(name, records, require_parked=True)
            record(name, signal == expected, signal)

    # The live SessionStart context carries the rules card and the artifact-usage skill, which describe decision lines
    # and saved output without being either. Neither may read as one.
    hooks_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(hooks_dir, "stop-gate-rules.txt"), "r", encoding="utf-8") as fh:
        live_context = "AGENT_MACHINE_TYPE=fixture\nStop-gate rules:\n" + fh.read()
    with open(os.path.join(session_home.tools_checkout(), "Claude", "skills", "artifact-usage", "SKILL.md"), "r",
              encoding="utf-8") as fh:
        live_context += "\nThe artifact-usage skill:\n" + fh.read()
    if h.wanted("start-decision-control-the-live-card-and-skill-hand-the-operator-nothing"):
        found = gate.unparked_start_decision(live_context, h.transcript("live-card", [user("go")]))
        record("start-decision-control-the-live-card-and-skill-hand-the-operator-nothing", found is None, repr(found))
    if h.wanted("start-control-the-live-card-shown-inline-names-no-saved-file"):
        import invoke_goal_gate
        path = h.transcript("live-card-inline", [session_started(
            live_context + "\nA quoted line: Full output saved to: C:\\not\\the\\saved\\file.txt\n")])
        start = invoke_goal_gate.session_start(path)
        record("start-control-the-live-card-shown-inline-names-no-saved-file",
               start is not None and start["saved"] is None, repr(start and start["saved"]))

    # A SESSION'S PAGE lives under %TEMP%\claude (session_home.py): the gate finds a page recorded there.
    for name, expected in (("page-recorded-under-temp-claude-is-found", "https://claude.ai/artifact/temphome"),):
        if h.wanted(name):
            fixture = tempfile.mkdtemp(prefix="stopgate-page-home-", dir=claude_temp.scratch_dir())
            try:
                workspace = os.path.join(fixture, "workspace")
                page_state = session_home.parking_dir(None, name)
                os.makedirs(page_state, exist_ok=True)
                with open(os.path.join(page_state, "state.json"), "w", encoding="utf-8") as fh:
                    json.dump({"url": "https://claude.ai/artifact/temphome"}, fh)
                transcript = os.path.join(fixture, name + ".jsonl")
                with open(transcript, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps({"type": "attachment", "cwd": workspace}) + "\n")
                found = gate.recorded_parking_url(workspace, name, transcript)
                record(name, found == expected, str(found))
            finally:
                claude_temp.remove_tree(fixture)

    # AN ASK NAMING A PARKED QUESTION READS THE PAGE FIRST: the operator answers there too, so an ask in a later turn
    # stands only after a read since their newest message that shows the question still open.
    def page_read(action="query", documents=(), error=False, collection="questions"):
        call = {"action": action, "url": "https://claude.ai/artifact/x", "collection": collection}
        if action == "query":
            call["query"] = {"where": [["status", "==", "answered"]]}
        printed = "\n".join(json.dumps({"id": doc_id, "data": {"status": status, "question": "Pick the root"}})
                            for doc_id, status in documents)
        content = "Permission denied" if error else "{0} documents from collection \"{1}\":\n{2}".format(
            len(documents), collection, printed)
        result = {"type": "tool_result", "tool_use_id": "tu-r", "content": content}
        if error:
            result["is_error"] = True
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tu-r", "name": "ArtifactData", "input": call}]}},
                {"type": "user", "message": {"role": "user", "content": [result]}}]

    earlier_turn = [user("do the thing"), tool_use()] + parking("q6-deploy-root", root_ask)
    later = user("keep going", prompt_id="p-2")
    for name, rows, expected in (
            ("unread-page-must-block-a-parked-question-asked-in-a-later-turn-with-no-read",
             earlier_turn + [later, tool_use()], "block:unread-page:1"),
            ("unread-page-a-query-for-answered-questions-that-omits-it-earns-the-stop",
             earlier_turn + [later, tool_use()] + page_read(), "allow:question-mark"),
            ("unread-page-a-list-showing-it-open-earns-the-stop",
             earlier_turn + [later, tool_use()] + page_read("list", [("q6-deploy-root", "open")]), "allow:question-mark"),
            ("unread-page-must-block-asking-a-question-the-read-shows-answered",
             earlier_turn + [later, tool_use()] + page_read(documents=[("q6-deploy-root", "answered")]),
             "block:unread-page:1"),
            ("unread-page-control-a-read-before-the-operator-wrote-is-stale",
             earlier_turn + page_read() + [later, tool_use()], "block:unread-page:1"),
            ("unread-page-control-a-failed-read-reads-nothing",
             earlier_turn + [later, tool_use()] + page_read(error=True), "block:unread-page:1"),
            ("unread-page-control-a-read-of-another-collection-reads-nothing",
             earlier_turn + [later, tool_use()] + page_read(collection="log"), "block:unread-page:1"),
            ("unread-page-control-a-get-of-another-question-does-not-cover-it",
             earlier_turn + [later, tool_use()] + page_read("get", [("q7-other", "open")]), "block:unread-page:1"),
            ("unread-page-control-a-needed-line-naming-no-parked-question-is-blocked-as-unparked",
             earlier_turn + [later, tool_use()], "block:unparked-needed:1")):
        if h.wanted(name):
            closing = "NEEDED: the Redis password, which only you hold." if "naming-no-parked" in name else root_ask
            _, signal, _ = h.run(name, rows + [assistant(closing)], require_parked=True)
            record(name, signal == expected, signal)
    # A RE-ASK IN PROSE IS AN ASK TOO: a line anywhere in the message asking about a parked question, beside an
    # ask that earns the stop on its own (the closing question here is q7, read open), is held to the same read.
    port_ask = "Which port should the probe use (q7)?"
    both_parked = earlier_turn + parking("q7-probe-port", port_ask) + [later, tool_use()]
    for name, rows, prose, expected in (
            ("prose-reask-must-block-a-prose-line-re-asking-a-question-the-read-shows-answered",
             both_parked + page_read("list", [("q6-deploy-root", "answered"), ("q7-probe-port", "open")]),
             "q6 still needs your answer.", "block:prose-reask:1"),
            ("prose-reask-must-block-a-prose-line-asking-about-a-question-no-read-covers",
             both_parked + page_read("get", [("q7-probe-port", "open")]),
             "Answer q6 here when you can.", "block:prose-reask:1"),
            ("prose-reask-control-a-line-reporting-an-answer-asks-nothing",
             both_parked + page_read("list", [("q6-deploy-root", "answered"), ("q7-probe-port", "open")]),
             "q6 was answered: the deploy root is the main checkout.", "allow:question-mark"),
            ("prose-reask-must-block-a-prose-question-asking-whether-an-answered-question-can-be-approved",
             both_parked + page_read("list", [("q6-deploy-root", "answered"), ("q7-probe-port", "open")]),
             "Can q6 be approved?", "block:prose-reask:1"),
            ("prose-reask-control-a-report-quoting-the-parked-question-asks-nothing",
             both_parked + page_read("list", [("q6-deploy-root", "answered"), ("q7-probe-port", "open")]),
             "q6 asked: keep the item?", "allow:question-mark"),
            ("prose-reask-control-a-question-mark-inside-a-url-asks-nothing",
             both_parked + page_read("list", [("q6-deploy-root", "answered"), ("q7-probe-port", "open")]),
             "The page row is https://claude.ai/artifact/x?id=q6 for the record.", "allow:question-mark"),
            ("prose-reask-control-a-report-quoting-the-earlier-ask-with-its-answer-asks-nothing",
             both_parked + page_read("list", [("q6-deploy-root", "answered"), ("q7-probe-port", "open")]),
             "q6 asked whether to keep the item; your answer was yes.", "allow:question-mark"),
            ("prose-reask-control-a-prose-ask-about-a-question-the-read-shows-open-earns-the-stop",
             both_parked + page_read("list", [("q6-deploy-root", "open"), ("q7-probe-port", "open")]),
             "q6 still needs your answer.", "allow:question-mark")):
        if h.wanted(name):
            _, signal, _ = h.run(name, rows + [assistant(prose + "\n" + port_ask)], require_parked=True)
            record(name, signal == expected, signal)
    if h.wanted("unread-page-must-block-a-needed-line-naming-an-answered-question"):
        name = "unread-page-must-block-a-needed-line-naming-an-answered-question"
        _, signal, _ = h.run(name, earlier_turn + [later, tool_use()] + page_read(documents=[("q6-deploy-root", "answered")])
                             + [assistant("NEEDED: your answer on q6, the deploy root.")], require_parked=True)
        record(name, signal == "block:unread-page:1", signal)

    # A WAIT LINE NAMING A PARKED QUESTION READS THE PAGE TOO, and only a read that returns every question covers one
    # it did not return: an answered query ordered and cut to a few rows says nothing about the rest.
    wait_line = "WAITING: my gate, with q6 still on you."
    for name, got, expected in (
            ("wait-line-must-block-a-waiting-line-is-read-for-the-page-check",
             gate.wait_lines("Status.\n" + wait_line, "allow:waiting-on-own-gate"), [wait_line]),
            ("wait-line-must-block-a-queued-line-is-read-for-the-page-check",
             gate.wait_lines("QUEUED: on q6.", "allow:queued-for-gate"), ["QUEUED: on q6."]),
            ("wait-line-control-a-wait-line-is-never-an-ask-the-repeat-check-holds",
             gate.earning_asks(wait_line, "allow:waiting-on-own-gate"), []),
            ("wait-line-control-another-signal-reads-no-wait-line",
             gate.wait_lines(wait_line, "allow:work-complete"), []),
            ("full-read-must-block-an-ordered-answered-query-covers-only-what-it-returned",
             gate.reads_every_question("query", {"where": [["status", "==", "answered"]],
                                                 "order_by": {"field": "answeredAt", "direction": "desc"}, "limit": 3}),
             False),
            ("full-read-must-block-a-small-limit-covers-only-what-it-returned",
             gate.reads_every_question("query", {"where": [["status", "==", "answered"]], "limit": 3}), False),
            ("full-read-control-an-unordered-answered-query-covers-every-question",
             gate.reads_every_question("query", {"where": [["status", "==", "answered"]]}), True),
            ("full-read-control-a-list-covers-every-question", gate.reads_every_question("list", {}), True)):
        if h.wanted(name):
            record(name, got == expected, repr(got))
    for name, reads, expected in (
            ("wait-line-must-block-a-waiting-line-naming-an-answered-question",
             page_read(documents=[("q6-deploy-root", "answered")]), "answered or withdrawn"),
            ("wait-line-must-block-a-waiting-line-naming-a-withdrawn-question",
             page_read("get", [("q6-deploy-root", "withdrawn")]), "answered or withdrawn"),
            ("wait-line-must-block-a-waiting-line-naming-a-question-no-read-covers", [], "no read of your page"),
            ("wait-line-control-a-waiting-line-after-a-read-showing-it-open", page_read("get", [("q6-deploy-root", "open")]),
             "")):
        if h.wanted(name):
            fixture = tempfile.mkdtemp(prefix="stopgate-wait-line-", dir=claude_temp.scratch_dir())
            try:
                transcript = os.path.join(fixture, "s.jsonl")
                with open(transcript, "w", encoding="utf-8") as fh:
                    for row in earlier_turn + [later, tool_use()] + reads:
                        fh.write(json.dumps(row) + "\n")
                got = gate.unread_parked_asks(wait_line, "allow:waiting-on-own-gate", transcript)
                record(name, (expected in got) if expected else got == "", got)
            finally:
                claude_temp.remove_tree(fixture)

    # THE RESTATED GOAL NAMES NO PARKED QUESTION: the goal veto has the wait line carry the pinned goal, so a goal
    # reading "q6 approved" must not make every wait stop name an answered q6, while the same id outside it still does.
    pinned = "q6 approved. ship the deploy copy and watch the gate"
    for name, line, expected in (
            ("goal-ids-must-pass-a-wait-line-whose-only-id-is-inside-the-restated-goal",
             "WAITING: my gate. GOAL: q6 approved. ship the deploy copy and watch the gate", ""),
            ("goal-ids-must-pass-the-restated-goal-in-another-case-and-quoting",
             "QUEUED: session 37b2c3d4's gate. \"Q6 approved, ship the deploy copy and watch the gate\"", ""),
            ("goal-ids-control-the-same-id-outside-the-goal-still-refuses",
             "WAITING: my gate, q6 still on you. GOAL: q6 approved. ship the deploy copy and watch the gate",
             "answered or withdrawn"),
            ("goal-ids-control-a-partial-goal-is-no-restatement-so-its-id-counts",
             "WAITING: my gate. GOAL: q6 approved.", "answered or withdrawn")):
        if h.wanted(name):
            fixture = tempfile.mkdtemp(prefix="stopgate-goal-ids-", dir=claude_temp.scratch_dir())
            try:
                transcript = os.path.join(fixture, "s.jsonl")
                with open(transcript, "w", encoding="utf-8") as fh:
                    for row in earlier_turn + [later, tool_use()] + page_read(documents=[("q6-deploy-root", "answered")]):
                        fh.write(json.dumps(row) + "\n")
                got = gate.unread_parked_asks(line, "allow:waiting-on-own-gate", transcript, goal=pinned)
                record(name, (expected in got) if expected else got == "", got)
            finally:
                claude_temp.remove_tree(fixture)

    # A READ COUNTS ONLY IN ITS OWN STOP ATTEMPT, and the session's own park write is no read: the operator may answer
    # on the page any time after either, which no transcript record shows.
    feedback = {"type": "user", "message": {"role": "user",
                                            "content": gate.STOP_FEEDBACK_PREFIX + "\nSTOP GATE: keep working."}}
    goal_feedback = {"type": "user", "message": {"role": "user", "content": gate.STOP_FEEDBACK_PREFIX
                                                 + "\n[ship the deploy copy]: The goal is not met."}}
    goal_and_gate_feedback = {"type": "user", "message": {"role": "user", "content": gate.STOP_FEEDBACK_PREFIX
                                                          + "\n[ship the deploy copy]: The goal is not met.\n"
                                                          + gate.STOP_FEEDBACK_PREFIX + "\nSTOP GATE: keep working."}}
    parked_now =[user("do the thing"), tool_use()] + parking("q6-deploy-root", "Which deploy root should the copy use?")
    for name, rows, expected in (
            ("stale-read-must-block-an-ask-right-after-parking-with-no-read",
             parked_now, "block:unread-page:1"),
            ("stale-read-must-block-a-re-ask-after-a-refused-stop-whose-read-came-before-it",
             parked_now + open_read() + [assistant("Working through the rest."), feedback, tool_use()],
             "block:unread-page:1"),
            ("stale-read-control-park-then-read-then-ask-earns-the-stop",
             parked_now + open_read(), "allow:question-mark"),
            ("stale-read-control-a-fresh-read-after-the-refused-stop-earns-the-stop",
             parked_now + open_read() + [assistant("Working through the rest."), feedback, tool_use()] + open_read(),
             "allow:question-mark"),
            ("stale-read-must-not-block-a-read-followed-only-by-the-goal-hooks-feedback-for-the-same-stop",
             parked_now + open_read() + [goal_feedback], "allow:question-mark"),
            ("stale-read-must-block-a-read-followed-by-goal-and-stop-gate-feedback-in-one-record",
             parked_now + open_read() + [goal_and_gate_feedback, tool_use()], "block:unread-page:1"),
            ("stale-read-must-block-goal-feedback-with-no-read-after-parking",
             parked_now + [goal_feedback], "block:unread-page:1")):
        if h.wanted(name):
            _, signal, _ = h.run(name, rows + [assistant(root_ask)], require_parked=True)
            record(name, signal == expected, signal)

    # A WAKE READS THE PAGE BEFORE IT CALLS AN ASK UNANSWERED: the operator answers parked questions on the page, which
    # the transcript never shows, so the quiet stop on a wake holds only for an ask a read this turn shows still open.
    wake = {"type": "user", "uuid": "peer-w", "isMeta": True, "origin": {"kind": "peer"}, "message": {
        "role": "user", "content": '<cross-session-message from="uds:peer">ack</cross-session-message>'}}
    quiet = assistant("The walker job is still running.")
    asked_turn = earlier_turn + [later, tool_use()]
    for name, rows, expected, must_say, must_not_say in (
            ("wake-page-must-block-a-quiet-stop-on-an-ask-the-page-shows-answered",
             asked_turn + [wake] + page_read(documents=[("q6-deploy-root", "answered")]) + [quiet], "block:1",
             None, "has not answered"),
            ("wake-page-control-a-quiet-stop-on-an-ask-the-page-shows-open-stops",
             asked_turn + [wake] + page_read() + [quiet], gate.QUIET_WAKE_SIGNAL, None, None),
            ("wake-page-must-block-a-quiet-stop-no-read-this-turn-covers",
             asked_turn + [wake, quiet], "block:wake-unread:1", "no read of your page", "has not answered"),
            ("wake-page-must-block-a-quiet-stop-whose-only-read-came-before-the-wake",
             asked_turn + page_read() + [wake, quiet], "block:wake-unread:1", "no read of your page",
             "has not answered"),
            ("wake-page-must-block-a-repeat-on-a-wake-with-the-read-reason-when-no-read-covers-it",
             asked_turn + [wake, assistant(root_ask)], "block:repeat-question:1", "no read of your page",
             "has not answered"),
            ("wake-page-control-a-repeat-on-a-wake-the-page-shows-open-says-it-is-unanswered",
             asked_turn + [wake] + page_read() + [assistant(root_ask)], "block:repeat-question:1", "has not answered",
             None),
            ("wake-page-control-an-ask-naming-no-parked-question-keeps-the-quiet-stop",
             [user("do the thing"), tool_use(), wake, quiet], gate.QUIET_WAKE_SIGNAL, None, None)):
        if h.wanted(name):
            pending = ["Do you want me to delete the Redis cache now?"] if "no-parked" in name else [root_ask]
            verdict, signal, out = isolated_stop(h, name, rows, pending_asks=pending, with_reason=True)
            record(name, signal == expected and (must_say is None or must_say in out)
                   and (must_not_say is None or must_not_say not in out), "{0} | {1}".format(signal, out[:120]))

    # An ask is delivered only by an allowed stop: one a veto blocked, once corrected, is not a repeat.
    if h.wanted("repeat-an-ask-a-veto-blocked-is-not-delivered"):
        name = "repeat-an-ask-a-veto-blocked-is-not-delivered"
        land = "The work is ready.\nCan you commit and push?"
        path = h.transcript(name, [user("do the thing"), tool_use(), assistant(land)])
        state = h.state_dir(name)
        _, first, _ = h.run_path(name, path, state=state)
        append(path, [tool_use(), assistant(block + "\n" + land)])
        _, second, _ = h.run_path(name, path, state=state)
        append(path, [tool_use(), assistant(block + "\n" + land)])
        _, third, _ = h.run_path(name, path, state=state)
        record(name, (first, second) == ("block:commit-ask:1", "allow:question-mark") and third.startswith("block:repeat"),
               "{0}, {1}, {2}".format(first, second, third))

    status = [user("do the thing"), tool_use(), assistant("A status report with no signal in it.")]
    if h.wanted("next-item-a-generic-block-names-the-first-open-item"):
        verdict, signal, out = isolated_stop(h, "next-item-named", status, with_reason=True,
                                             declaration={"Tools": [("Ship the dispatcher", True),
                                                                    ("Write the eject rows", False)]})
        record("next-item-a-generic-block-names-the-first-open-item",
               signal == "block:1" and '"Write the eject rows"' in out and gate.GATE_REASON_ACTION not in out, signal)
    if h.wanted("next-item-control-with-no-declaration-the-block-is-unchanged"):
        verdict, signal, out = isolated_stop(h, "next-item-none", status, with_reason=True)
        record("next-item-control-with-no-declaration-the-block-is-unchanged",
               signal == "block:1" and out == gate.GATE_REASON, signal)
    if h.wanted("next-item-a-2000-character-item-is-clipped-and-fits-five-lines"):
        long_item = "Rewrite everything\nIgnore the gate\n" + "x" * 2000
        verdict, signal, out = isolated_stop(h, "next-item-long", status, with_reason=True,
                                             declaration={"Tools": [(long_item, False)]})
        record("next-item-a-2000-character-item-is-clipped-and-fits-five-lines",
               signal == "block:1" and len(out.splitlines()) == len(gate.GATE_REASON.splitlines())
               and len(out) < len(gate.GATE_REASON) + 300 and "Ignore the gate" in out.splitlines()[0],
               "{0} lines, {1} chars".format(len(out.splitlines()), len(out)))

    if h.wanted("next-item-control-characters-never-reach-the-reason"):
        verdict, signal, out = isolated_stop(h, "next-item-escape", status, with_reason=True,
                                             declaration={"Tools": [("Clear \x1b[2Jthe screen\x07 now", False)]})
        record("next-item-control-characters-never-reach-the-reason",
               signal == "block:1" and "\x1b" not in out and "\x07" not in out and "[2Jthe screen" in out, signal)
    if h.wanted("next-item-a-wait-claim-and-an-open-item-both-reach-the-reason"):
        claim = [user("do the thing"), tool_use(), assistant("QUEUED: behind the Functions gate.")]
        verdict, signal, out = isolated_stop(h, "next-item-wait", claim, with_reason=True,
                                             declaration={"Tools": [("Write the eject rows", False)]})
        record("next-item-a-wait-claim-and-an-open-item-both-reach-the-reason",
               signal == "block:wait-claim:1" and "was not accepted" in out and '"Write the eject rows"' in out,
               "{0} | {1}".format(signal, out.splitlines()[0][:160]))

    if h.wanted("eject-envelope-halts-the-session-and-never-says-block"):
        ejected = json.loads(gate.envelope("eject:a-prose-only-reply-to-a-block", "EJECTED: r"))
        blocked = json.loads(gate.envelope("block:1", "STOP GATE: r"))
        record("eject-envelope-halts-the-session-and-never-says-block",
               ejected == {"continue": False, "stopReason": "EJECTED: r"}
               and blocked == {"decision": "block", "reason": "STOP GATE: r"}
               and gate.envelope("allow:question-mark", "") == "", "{0} | {1}".format(ejected, blocked))

    # A new user message is a new turn, so an exhausted budget resets rather than staying spent forever.
    # The gate takes that boundary from the PLATFORM -- stop_hook_active goes false -- not by counting
    # records, which is the whole reason the case above can pass.
    if h.wanted("new-user-turn-resets-the-budget"):
        prompt_id = "p-11111111-1111-1111-1111-111111111111"
        path = h.transcript("turn-reset", [
            user("do the thing", prompt_id),
            assistant("Tests are green and the gate is clean.")])
        state = os.path.join(h.root, "state-turn-reset")
        for i in range(4):
            h.run_path("turn-reset", path, stop_hook_active=(i > 0), max_blocks=3, state=state)
        append(path, [user("now do the next thing", "p-22222222-2222-2222-2222-222222222222"),
                      assistant("Done. All checks pass.")])
        verdict, got_signal, _ = h.run_path("turn-reset", path, stop_hook_active=False,
                                            max_blocks=3, state=state)
        record("new-user-turn-resets-the-budget", verdict == "block",
               "{0} {1}".format(verdict, got_signal))

    # stop_hook_active is deliberately NOT honoured as an escape. Honouring it would cap the gate at one
    # challenge, which any agent could then walk straight past. The block counter is the loop guard.
    if h.wanted("stop-hook-active-does-not-buy-a-stop"):
        verdict, got_signal, _ = h.run("sha-no-stop", [
            user("go"),
            assistant("Tests are green: 75 passed. The migration is written.")],
            stop_hook_active=True)
        record("stop-hook-active-does-not-buy-a-stop", verdict == "block",
               "{0} {1}".format(verdict, got_signal))

    # The operator's grant must survive the gate's own feedback landing between it and the stop. The
    # feedback arrives as a prose user record, so a gate reading only the LAST user record sees the
    # feedback rather than the grant, and overrides an explicit instruction to stop.
    if h.wanted("grant-survives-gate-feedback"):
        verdict, got_signal, _ = h.run("grant-feedback", [
            user("stopping was earned once you wrote the handoff", "p-behind"),
            assistant("Handoff written."),
            user("Stop hook feedback: STOP GATE: nothing shows continuing is impossible.", "p-behind"),
            assistant("Stopping.")])
        record("grant-survives-gate-feedback", verdict == "allow",
               "{0} {1}".format(verdict, got_signal))

    # Echoing the gate's own reason must earn NOTHING: the reason names every signal in order to teach
    # it, so any trigger phrase left in that text is an escape hatch handed to the agent for free.
    if h.wanted("echoing-the-gate-reason-earns-nothing"):
        records = [user("go"), tool_use(), assistant(gate.GATE_REASON)]
        verdict, got_signal, _ = h.run("echo", records)
        record("echoing-the-gate-reason-earns-nothing", verdict == "block",
               "{0} {1}".format(verdict, got_signal))

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "stop-gate-rules.txt"), encoding="utf-8") as fh:
        card = fh.read()
    card_lower = " ".join(card.lower().split())
    reasons = [gate.GATE_REASON, gate.REPEATED_QUESTION_REASON, gate.REPEATED_ON_WAKE_REASON, gate.OPEN_ITEM_REASON,
               gate.GATE_CLAIM_REASON,
               gate.GOAL_REASON_TEMPLATE, gate.UNREPORTED_GATE_BLOCK, gate.TRAILER_REASON, gate.WORKSPACE_MISSING_REASON,
               gate.NO_PAGE_STOP_REASON, gate.UNPARKED_START_DECISION_REASON, gate.REFUSAL_BASIS_REASON,
               gate.commit_state_reason({"A": ["a.py"], "B": ["b.py"], "C": ["c.py"], "D": ["d.py"]},
                                        {"E": ["e.py"]}, ["A"], {"x.md": ["A", "B"]})]
    for name, ok in (
        ("card-admits-the-gate-can-miss-a-question", "can miss a real question" in card_lower),
        ("card-forbids-self-answering", "answer the question yourself" in card_lower),
        ("card-teaches-every-token", all(token in card for token in
                                         ("NEEDED:", "REFUSING:", "QUEUED:", "WAITING:", "DONE:", "VERIFIED:", "GOAL:"))),
        ("card-has-a-completion-branch", "fully finished with empirical proof" in card_lower),
        ("card-says-restate-and-end-turn", "restate the question" in card_lower and "end the turn" in card_lower),
        ("card-names-the-announce-veto", "promise your next step" in card_lower),
        ("card-teaches-queued-needs-a-message-and-a-running-peer-gate",
         "SendMessage" in card and "actually running on this machine" in card_lower),
        ("card-teaches-waiting-needs-a-live-own-gate", "still running" in card_lower),
        ("card-teaches-the-work-product-declaration", "%TEMP%\\claude\\work-products\\" in card),
        ("card-teaches-the-open-item-rule", "four consecutive words" in card_lower),
        ("card-teaches-one-stop-per-ask", "one stop per operator message" in card_lower),
        ("card-keeps-reporting", "silently working without reporting" in card_lower),
        ("card-says-a-completion-claim-is-checkable", "can check against the transcript" in card_lower),
        ("card-teaches-an-ask-quotes-the-operator-it-rests-on", "quotes, in double quotes and verbatim, at least four"
         " words of the operator message it rests on" in card_lower),
        ("card-teaches-no-ask-bounce", "costs work first" not in card_lower and "bounce" not in card_lower),
        ("card-teaches-a-refusal-quotes-what-it-rests-on", "a refusal resting on a premise nobody wrote down earns nothing"
         in card_lower),
        ("card-teaches-an-earned-stop-ends-over-an-active-goal",
         "`/goal` is a stop hook of its own" in card_lower and "ends the turn anyway" in card_lower),
        ("card-teaches-a-goal-ask-stands-when-no-tool-can-run",
         "with no goal pinned, a `needed:` or closing question asking them to run `/goal` earns the stop" in card_lower),
        ("card-teaches-a-woken-turn-ends-quietly", "ends with a short line that asks nothing" in card_lower
         and "a peer's question still owes its reply first" in card_lower),
        ("card-teaches-the-card-is-no-refusal-basis", "this card's rules govern how a turn ends and decline nothing"
         in card_lower),
        ("card-teaches-a-refusal-quotes-an-operator-message",
         "at least four words of an operator message: the request it declines" in card_lower
         and "a written rule alone (claude.md, agents) declines nothing the operator asked" in card_lower),
        ("card-teaches-the-saved-session-start-is-read-first",
         "full output saved to:" in card_lower and "no tool but a read of that file runs until it is read" in card_lower),
        ("card-teaches-no-stop-before-the-page", "until your parking-lot page exists" in card_lower),
        ("card-teaches-a-done-owes-the-session-start-decisions",
         "hands the operator a decision or an action" in card_lower),
        ("card-teaches-the-filler-streak-at-the-gate-limit",
         "four refused stops in a row with no tool call between them" in card_lower and gate.FILLER_STREAK_LIMIT == 4),
        ("card-teaches-a-done-item-awaiting-approval-can-wait", "every done item whose approval waits on an answer"
         in card_lower),
        ("card-teaches-every-stop-restates-the-goal", "every earned stop then carries that goal's words verbatim" in card_lower),
        ("card-teaches-no-tool-runs-without-a-goal", "run no tool until the operator has set a goal with `/goal`" in card_lower),
        ("card-teaches-tokens-open-their-line-in-capitals",
         "counts only in capitals and opening its own line; mid-sentence it is a word" in card_lower),
        ("card-teaches-done-owes-a-closed-work-product", "owes a closed work-product declaration" in card_lower),
        ("card-teaches-a-commit-ask-carries-the-messages",
         "line or closing question that asks the operator to commit, push or check in carries the drafted commit"
         " messages" in card_lower),
        ("card-teaches-a-commit-message-in-the-reply-in-full", "in the reply text, in full" in card_lower),
        ("card-teaches-a-pointer-is-not-a-delivery", "a pointer to an earlier message, a scratchpad or any file is not a"
                                                     " delivery" in card_lower),
        ("card-teaches-a-carried-block-is-not-a-delivery",
         "carried forward from an earlier turn is not a delivery" in card_lower and "git status --porcelain" in card_lower),
        ("card-teaches-nothing-for-a-clean-repo", "nothing for a clean repo" in card_lower),
        ("card-forbids-attribution-trailers", "Co-Authored-By:" in card and "Claude-Session:" in card),
        ("card-teaches-plain-prose-for-an-unfinished-ask", "report what you closed in plain prose" in card_lower),
        ("every-block-reason-fits-five-lines", all(len(r.strip().splitlines()) <= 5 for r in reasons)),
        ("the-generic-reason-names-every-token-and-the-card",
         all(token in gate.GATE_REASON for token in ("NEEDED:", "REFUSING:", "QUEUED:", "WAITING:", "DONE:", "VERIFIED:"))
         and "stop-gate-rules.txt" in gate.GATE_REASON),
        ("reason-offers-no-quiet-stop", "no new ask" not in gate.GATE_REASON.lower()),
        ("repeat-reason-says-a-status-earns-no-stop",
         "a status without an ask earns no" in gate.REPEATED_QUESTION_REASON
         and "is allowed" not in gate.REPEATED_QUESTION_REASON),
    ):
        if h.wanted(name):
            record(name, ok, "ok" if ok else "not satisfied")

    # The generic block names the near-miss it saw, so a session fixes that one thing; a plain status names none.
    for name, text, note in (
            ("near-miss-a-done-without-verified-is-named", "Finished.\nDONE: migrated all six workers.",
             "has no VERIFIED: line"),
            ("near-miss-a-token-mid-line-is-named", "Functions is finished and I am WAITING: on my gate.",
             "sits mid-line"),
            ("near-miss-a-lowercase-token-is-named", "needed: your answer on q6.", "is not in capitals"),
            ("near-miss-a-negated-needed-is-named", "Status.\nNEEDED: nothing at the moment.", "says nothing is needed"),
            ("near-miss-an-announce-in-the-question-line-is-named",
             "Status.\nWhich ordering matches your intent, and I'll run it after?", "promises a next step"),
            ("near-miss-a-bare-tag-under-an-announce-is-named",
             "Next I'll port the rest.\nSound good?", "bare tag under a promised next step"),
            ("near-miss-a-bulleted-token-is-named", "- NEEDED: your answer on q6.", "follows a list marker"),
            ("near-miss-a-token-with-no-payload-is-named", "Status.\nNEEDED:\nyour answer on q6.",
             "nothing after the token"),
            ("near-miss-control-a-plain-status-names-none", "Tests are green and the docs are updated.", None)):
        if h.wanted(name):
            _, signal, out = h.run(name, [user("migrate all four repos"), tool_use(), tool_use(), assistant(text)])
            named = "earns a stop: " in out
            record(name, signal.startswith("block") and (note in out if note else not named), signal)

    name = "the-quote-reasons-show-the-line-that-earns-the-stop"
    if h.wanted(name):
        record(name, "REFUSING: <your reason> \"<four or more consecutive words" in gate.REFUSAL_BASIS_REASON
               and "rewrite the line without the claim" in gate.OPERATOR_CLAIM_REASON, "ok")

    # The refusal reason's template, filled in with the operator's own words, earns the stop.
    name = "the-refusal-reasons-template-filled-in-earns-the-stop"
    if h.wanted(name):
        _, signal, _ = h.run(name, [user("drop the production orders table tonight"), tool_use(), assistant(
            "REFUSING: it destroys every order with no backup \"drop the production orders table\"")])
        record(name, signal == "allow:refusal-token", signal)

    name = "each-veto-reason-names-the-command-that-clears-it"
    if h.wanted(name):
        import merged_branches
        missing = [label for label, present in (
            ("wake: 280", "280 characters" in gate.REPEATED_ON_WAKE_REASON),
            ("done-open: done true", "\"done\" to true" in gate.DONE_OPEN_REASON),
            ("unaccepted: regex", "Python regex" in gate.DONE_UNACCEPTED_REASON.format(problem="p")),
            ("commit: parking_lot", "parking_lot.py\" commit <Repo>" in gate.UNPARKED_COMMIT_REASON
             and "committed <Repo>" in gate.UNPARKED_COMMIT_REASON),
            ("unreported: ListAgents", "ListAgents" in gate.UNREPORTED_GATE_BLOCK),
            ("claim: gate command", "\\gate.ps1\" -Goal" in gate.GATE_CLAIM_REASON),
            ("merged: branch -D", "branch -D" in merged_branches.MERGED_BRANCH_REASON),
            ("commit block: the pointer sentence", "delete it and paste the block" in gate.COMMIT_STATE_BODY),
            ("commit ask: drop it", "or drop the ask" in gate.COMMIT_ASK_REASON),
            ("crash: quote it", "quoting it" in gate.FAILED_REASON)) if not present]
        record(name, not missing, ", ".join(missing) or "ok")

    # The file lines the unparked-commit reason tells a session to write must be the paths the checker compares:
    # untracked folders expanded, renames as two paths, a path with a space unquoted. Plain --porcelain is the control.
    name = "the-status-command-the-commit-reasons-advise-lists-the-checkers-paths"
    if h.wanted(name):
        import parking_lot_commits
        base = tempfile.mkdtemp(prefix="status-lines-", dir=claude_temp.scratch_dir())
        try:
            repo, _ = make_repo(base, "Repo", {"a.py": "clean", "new/deep/y.py": "new", "new/x.py": "new",
                                               "space name.txt": "new"})
            git_in(repo, "mv", "a.py", "b.py")

            def listed(*flags):
                out = git_in(repo, "-c", "core.quotePath=false", "status", "--porcelain", *flags)
                return sorted(line[3:].strip('"') for line in out.splitlines() if len(line) > 3)

            checker = parking_lot_commits.dirty_files(repo)
            advised = listed("--no-renames", "--untracked-files=all")
            record(name, advised == checker and listed() != checker
                   and "--no-renames --untracked-files=all" in parking_lot_commits.STATUS_COMMAND
                   and "--no-renames --untracked-files=all" in gate.UNPARKED_COMMIT_REASON,
                   "advised {0}, checker {1}".format(advised, checker))
        finally:
            claude_temp.remove_tree(base)

    name = "near-miss-a-done-and-verified-with-no-tool-call-is-named"
    if h.wanted(name):
        _, signal, out = h.run(name, [user("migrate all four repos"), assistant("DONE: migrated.\nVERIFIED: gate green.")])
        record(name, signal.startswith("block") and "needs a tool call in this turn" in out, signal)

    # Fail-open, always. A gate that throws must never become a blocked turn.
    if h.wanted("missing-transcript-fails-open"):
        state = os.path.join(h.root, "state-missing")
        signal, out = gate.decide({"session_id": "missing"},
                                  os.path.join(h.root, "nope.jsonl"), state, 100, gate_workspace=h.no_gated_repos())
        record("missing-transcript-fails-open", out == "", signal)

    if h.wanted("a-missing-workspace-blocks-even-an-earned-stop-and-keeps-blocking"):
        absent = os.path.join(h.root, "no-such-workspace")
        state = os.path.join(h.root, "state-no-workspace")
        path = h.transcript("no-workspace", [user("go"), tool_use(), assistant("Should the gate run first?")])
        payload = {"session_id": "no-workspace", "transcript_path": path, "stop_hook_active": False,
                   "last_assistant_message": "Should the gate run first?"}
        first, out = gate.decide(payload, path, state, 100, workspace=absent, gate_workspace=h.no_gated_repos())
        payload["stop_hook_active"] = True
        second, again = gate.decide(payload, path, state, 100, workspace=absent, gate_workspace=h.no_gated_repos())
        record("a-missing-workspace-blocks-even-an-earned-stop-and-keeps-blocking",
               first == "block:workspace-missing:1" and second == "block:workspace-missing:2"
               and absent in out and absent in again, "{0}, then {1}".format(first, second))

    # The block's own advice must earn the stop: a REFUSING: line quoting the operator, written the way the
    # reason shows, is allowed while the workspace is missing, and a NEEDED: line is still refused.
    absent_workspace = os.path.join(h.root, "no-such-workspace")
    refusal = ('The workspace is gone.\n\nREFUSING: to continue "fix the title prefix in every page" until the'
               ' crgolden workspace exists at {0}.'.format(absent_workspace))
    for name, expect, text in (
            ("a-missing-workspace-allows-the-refusal-its-reason-advises", "allow:refusal-token", refusal),
            ("a-missing-workspace-control-a-needed-line-still-blocks", "block:workspace-missing:1",
             "The workspace is gone.\n\nNEEDED: the crgolden workspace restored at {0}.".format(absent_workspace))):
        if h.wanted(name):
            path = h.transcript(name, [user("fix the title prefix in every page"), tool_use(), assistant(text)])
            signal, out = gate.decide({"session_id": name, "transcript_path": path, "stop_hook_active": False,
                                       "last_assistant_message": text}, path,
                                      h.state_dir(name), 100, workspace=absent_workspace,
                                      gate_workspace=h.no_gated_repos())
            record(name, signal == expect, signal)
    if h.wanted("a-missing-workspace-reason-shows-the-refusal-that-earns-the-stop"):
        reason = gate.WORKSPACE_MISSING_REASON.format(path=absent_workspace)
        record("a-missing-workspace-reason-shows-the-refusal-that-earns-the-stop",
               "REFUSING:" in reason and "End the turn with a NEEDED: line" not in reason, "ok")

    if h.wanted("a-missing-workspace-still-honours-the-operator-grant"):
        path = h.transcript("no-workspace-grant", [user("you can stop now"), assistant("Stopping.")])
        signal, out = gate.decide({"session_id": "no-workspace-grant", "transcript_path": path,
                                   "last_assistant_message": "Stopping."}, path,
                                  os.path.join(h.root, "state-no-workspace-grant"), 100,
                                  workspace=os.path.join(h.root, "no-such-workspace"), gate_workspace=h.no_gated_repos())
        record("a-missing-workspace-still-honours-the-operator-grant", signal == "allow:operator-grant" and out == "",
               signal)

    if h.wanted("corrupt-transcript-fails-open"):
        path = os.path.join(h.root, "corrupt.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json at all\n\x00\x01\n")
        signal, out = gate.decide({"session_id": "corrupt"}, path,
                                  os.path.join(h.root, "state-corrupt"), 100, gate_workspace=h.no_gated_repos())
        record("corrupt-transcript-fails-open", out == "", signal)

    if h.wanted("a-gate-that-raises-blocks-the-stop"):
        real_decide, real_argv, real_stdout = gate.decide, sys.argv, sys.stdout
        captured = io.StringIO()

        def raising_decide(*args, **kwargs):
            raise RuntimeError("planted")

        try:
            gate.decide = raising_decide
            sys.argv = ["invoke_stop_gate.py", "--skip-stdin", "--state-directory",
                        os.path.join(h.root, "state-raises")]
            sys.stdout = captured
            code = gate.main()
        finally:
            gate.decide, sys.argv, sys.stdout = real_decide, real_argv, real_stdout
        out = captured.getvalue()
        blocked = bool(out) and json.loads(out).get("decision") == "block" and "planted" in out
        record("a-gate-that-raises-blocks-the-stop", blocked and code == gate.ALLOW_STOP, out[:120])

    if h.wanted("a-gate-that-raises-keeps-blocking-every-retry"):
        real_decide, real_argv, real_stdout = gate.decide, sys.argv, sys.stdout

        def raising_decide(*args, **kwargs):
            raise RuntimeError("planted")

        verdicts = []
        try:
            gate.decide = raising_decide
            for _ in range(12):
                captured = io.StringIO()
                sys.argv = ["invoke_stop_gate.py", "--skip-stdin", "--state-directory",
                            os.path.join(h.root, "state-raises-again")]
                sys.stdout = captured
                gate.main()
                text = captured.getvalue()
                verdicts.append(bool(text) and json.loads(text).get("decision") == "block")
        finally:
            gate.decide, sys.argv, sys.stdout = real_decide, real_argv, real_stdout
        record("a-gate-that-raises-keeps-blocking-every-retry", all(verdicts) and len(verdicts) == 12,
               "{0} of 12 blocked".format(sum(verdicts)))

    # Diagnostics. 45 of 99 blocks in session 94edf861 replayed as ALLOW, and the reason could not be
    # settled because nothing recorded what the gate had actually read. These pin that record.
    if h.wanted("facts-log-records-what-the-gate-saw"):
        state = os.path.join(h.root, "state-facts")
        h.run("facts-seen", [user("go"), tool_use(), tool_result(),
                             assistant("A status report with no signal in it.")], state=state)
        rows = []
        log = os.path.join(state, "facts.log")
        if os.path.exists(log):
            with open(log, "r", encoding="utf-8") as fh:
                rows = [ln.rstrip("\n").split("\t") for ln in fh if ln.strip()]
        fields = dict(f.split("=", 1) for f in rows[-1][3:] if "=" in f) if rows else {}
        ok = (bool(rows)
              and fields.get("ahead", "").startswith("A status report")
              and fields.get("newest") == "assistant/text"
              and fields.get("endsnl") == "True"
              and fields.get("badjson") == "0"
              and str(fields.get("size", "0")).isdigit() and int(fields["size"]) > 0)
        record("facts-log-records-what-the-gate-saw", ok,
               "newest={0} ahead={1!r}".format(fields.get("newest"), fields.get("ahead", "")[:40]))

    # A refused QUEUED: line records what decided it: the open standing items with no "pending" (any one cancels the
    # gate-wait excuse), and what the slot and live-peer checks read; a stop with no wait line records none.
    if h.wanted("a-refused-queued-line-logs-the-wait-readings"):
        state = os.path.join(h.root, "state-wait-readings")
        h.run("wait-readings", [user("fix the title prefix"), tool_use(), assistant(QUEUED)], state=state)
        h.run("no-wait-line", [user("go"), tool_use(), tool_result(), assistant("A status report, no wait line.")],
              state=state)
        rows = []
        log = os.path.join(state, "facts.log")
        if os.path.exists(log):
            with open(log, "r", encoding="utf-8") as fh:
                rows = [dict(f.split("=", 1) for f in ln.rstrip("\n").split("\t")[3:] if "=" in f) for ln in fh if ln.strip()]
        queued_wait = rows[0].get("wait", "") if rows else ""
        plain_wait = rows[-1].get("wait", "") if len(rows) > 1 else ""
        record("a-refused-queued-line-logs-the-wait-readings",
               re.fullmatch(r"unparked=[^;]+;slot=[^;]+;peer=[^;]+", queued_wait) is not None and plain_wait == "-",
               "queued wait={0!r} plain wait={1!r}".format(queued_wait, plain_wait))

    # Readers of decisions.log take the LAST tab-separated field as the signal. Appending a column
    # there would hand them a diagnostic where they expect the signal, and they would keep passing
    # while reporting nonsense. That is why every diagnostic lives in facts.log instead, and this case
    # is what holds decisions.log to three fields now that the harnesses which relied on it are gone.
    if h.wanted("decisions-log-last-field-is-still-the-signal"):
        state = os.path.join(h.root, "state-shape")
        _, signal, _ = h.run("shape", [user("go"), tool_use(), tool_result(),
                                       assistant("No signal here either.")], state=state)
        log = os.path.join(state, "decisions.log")
        last = ""
        if os.path.exists(log):
            with open(log, "r", encoding="utf-8") as fh:
                lines = [ln.rstrip("\n") for ln in fh if ln.strip()]
            last = lines[-1] if lines else ""
        parts = last.split("\t")
        ok = len(parts) == 3 and parts[-1] == signal
        record("decisions-log-last-field-is-still-the-signal", ok,
               "{0} field(s), last={1!r}".format(len(parts), parts[-1] if parts else ""))

    # THE PAYLOAD IS THE SOURCE. Stop hands the gate last_assistant_message, which is the turn's final
    # text by construction, so the transcript is no longer read for the thing the detectors judge.
    # This first case is the one that discriminates: the transcript holds a message that WOULD earn a
    # stop and the payload holds one that must not, so a gate still reading the file answers
    # allow:work-complete here and every other case in this file still passes.
    if h.wanted("payload-message-outranks-the-transcript"):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        _, signal, out = h.run("payload-wins", [
            dict(user("go"), timestamp=now),
            dict(tool_use(), timestamp=now),
            dict(tool_result(), timestamp=now),
            dict(assistant("DONE: shipped it\nVERIFIED: the suite is green"), timestamp=now)],
            last_message="A status report with no signal in it.")
        record("payload-message-outranks-the-transcript", out != "" and signal == "block:1", signal)

    # The mirror, or the case above is satisfied by a gate that has simply stopped allowing anything.
    if h.wanted("payload-message-can-also-earn-the-stop"):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        _, signal, out = h.run("payload-earns", [
            dict(user("go"), timestamp=now),
            dict(tool_use(), timestamp=now),
            dict(tool_result(), timestamp=now),
            dict(assistant("A status report with no signal in it."), timestamp=now)],
            last_message="Which of the two branches should I land first?")
        record("payload-message-can-also-earn-the-stop",
               out == "" and signal == "allow:question-mark", signal)

    # The live failure of 2026-08-30, in one case. Session 036e4299 at 05:11:02 read a transcript whose
    # newest message record was nine minutes old, blocked three times over a view no restatement could
    # refresh, then let an unearned stop through unjudged. Sourced from the payload the file's age
    # stops mattering, and the message is simply judged.
    if h.wanted("payload-text-makes-a-stale-transcript-irrelevant"):
        old = "2020-01-01T00:00:00.000Z"
        _, signal, out = h.run("payload-vs-stale", [
            dict(user("go"), timestamp=old),
            dict(tool_use(), timestamp=old),
            dict(tool_result(), timestamp=old),
            # A question, so the ancient text EARNS a stop and the fresh one does not. With both
            # earning nothing the case returned block:1 whether or not the payload was read, and a
            # mutation run showed it passing for the wrong reason.
            dict(assistant("Should I land the branch first?"), timestamp=old)],
            last_message="A status report with no signal in it.")
        record("payload-text-makes-a-stale-transcript-irrelevant",
               out != "" and signal == "block:1", signal)

    # An empty or whitespace field is not "the agent said nothing". Treating it as supplied would blank
    # assistant_text and return allow:no-assistant-text, which is a silent unconditional allow -- the
    # worst failure this gate has, and one that would leave no trace in the turn.
    if h.wanted("empty-payload-message-falls-back-rather-than-blanking"):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        _, signal, out = h.run("empty-field", [
            dict(user("go"), timestamp=now),
            dict(tool_use(), timestamp=now),
            dict(tool_result(), timestamp=now),
            dict(assistant("A status report with no signal in it."), timestamp=now)],
            last_message="   ")
        record("empty-payload-message-falls-back-rather-than-blanking",
               out != "" and signal == "block:1", signal)

    # An older host sends no such field, so the transcript path has to keep working exactly as before.
    if h.wanted("absent-payload-field-falls-back-to-the-transcript"):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        _, signal, out = h.run("no-field", [
            dict(user("go"), timestamp=now),
            dict(tool_use(), timestamp=now),
            dict(tool_result(), timestamp=now),
            dict(assistant("Should I land the branch first?"), timestamp=now)])
        record("absent-payload-field-falls-back-to-the-transcript",
               out == "" and signal == "allow:question-mark", signal)

    # A transcript whose records are ancient is no longer a special case. The gate judges the payload
    # text, so an old file is simply an old file: nothing about the record ages changes the verdict,
    # and a replay tool needs no flag to disable a guard that no longer exists.
    if h.wanted("ancient-transcript-is-not-a-special-case"):
        old = "2020-01-01T00:00:00.000Z"
        _, signal, out = h.run("ancient", [
            dict(user("go"), timestamp=old),
            dict(tool_use(), timestamp=old),
            dict(tool_result(), timestamp=old),
            dict(assistant("A status report with no signal in it."), timestamp=old)])
        record("ancient-transcript-is-not-a-special-case", out != "" and signal == "block:1", signal)

    # The chunked backward read: every other fixture is a few hundred bytes, so the seam between
    # chunks -- carrying a partial line across, decoding UTF-8 across the boundary -- would otherwise
    # never execute. Padded with multi-byte characters so a naive byte split would corrupt a line.
    if h.wanted("tail-read-spans-chunk-boundaries"):
        path = os.path.join(h.root, "big.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(user("go"), separators=(",", ":")) + "\n")
            filler = assistant("padding " + ("éü中" * 300))
            for _ in range(700):
                fh.write(json.dumps(filler, separators=(",", ":")) + "\n")
            fh.write(json.dumps(
                assistant("Which ordering matches your intent?"), separators=(",", ":")) + "\n")
        size = os.path.getsize(path)
        signal, out = gate.decide({"session_id": "big"}, path,
                                  os.path.join(h.root, "state-big"), 100, gate_workspace=h.no_gated_repos(),
                                  require_parked=False)
        record("tail-read-spans-chunk-boundaries",
               out == "" and signal == "allow:question-mark",
               "{0} ({1:,} bytes, >1 chunk)".format(signal, size))

    # The peer-notification veto, through decide() and against real transcripts. Each case gets its own
    # config root because the session registry sits beside the projects directory: sharing one would
    # make every case's registry entries peers of every other and the solo case could not be solo.
    veto_command = "dotnet test --project App.Tests.Unit --no-build --configuration Release"
    here_domain = "win32:harness-here"

    def creation_filetime(pid):
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(gate.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        created, exited, kernel, user_time = (wintypes.FILETIME() for _ in range(4))
        kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                 ctypes.byref(kernel), ctypes.byref(user_time))
        kernel32.CloseHandle(handle)
        return str((created.dwHighDateTime << 32) | created.dwLowDateTime)

    def exited_pid():
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        return child.pid

    live_start = creation_filetime(os.getpid())
    peer_entries = {
        "live": lambda: {"pid": os.getpid(), "procStart": live_start, "pidDomain": here_domain},
        "remote": lambda: {"pid": os.getpid(), "procStart": live_start, "pidDomain": "win32:another-machine"},
        "exited": lambda: {"pid": exited_pid(), "procStart": live_start, "pidDomain": here_domain},
        "reused": lambda: {"pid": os.getpid(), "procStart": "1", "pidDomain": here_domain},
    }

    def veto_session(name, records, peer="live", mirror=False):
        config = os.path.join(h.root, "veto-" + name)
        project = os.path.join(config, "projects", "proj")
        registry = os.path.join(config, "sessions")
        os.makedirs(project, exist_ok=True)
        os.makedirs(registry, exist_ok=True)
        own = os.path.join(project, "own.jsonl")
        transcripts = [own]
        if mirror:
            mirrored = os.path.join(config, "projects", "proj-mirror")
            os.makedirs(mirrored, exist_ok=True)
            transcripts.append(os.path.join(mirrored, "own.jsonl"))
        for path in transcripts:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                for r in records:
                    fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        entries = {"1.json": {"pid": 1, "sessionId": "own", "pidDomain": here_domain}}
        if peer:
            with open(os.path.join(project, "peer.jsonl"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(user("peer session"), separators=(",", ":")) + "\n")
            entries["2.json"] = dict(peer_entries[peer](), sessionId="peer")
        for filename, entry in entries.items():
            with open(os.path.join(registry, filename), "w", encoding="utf-8", newline="\n") as fh:
                json.dump(entry, fh)
        return own

    def gate_run(task, launched, exited):
        return [
            {"type": "assistant", "timestamp": launched, "message": {"content": [
                {"type": "tool_use", "id": "tu-" + task, "name": "PowerShell",
                 "input": {"command": veto_command, "run_in_background": True}}]}},
            {"type": "user", "timestamp": launched, "toolUseResult": {"backgroundTaskId": task},
             "message": {"content": [{"type": "tool_result", "tool_use_id": "tu-" + task,
                                      "content": "Command running in background with ID: " + task}]}},
            {"type": "user", "timestamp": exited, "message": {"content":
                "<task-notification><task-id>" + task + "</task-id><status>completed</status></task-notification>"}},
        ]

    def message_at(stamp):
        record = send_use()
        record["timestamp"] = stamp
        return record

    ask = "Which ordering matches your intent?"
    veto_cadence = gate.cadence_module()

    if h.wanted("veto-blocks-a-finished-gate-no-peer-was-told"):
        own = veto_session("unreported", [user("go")] + gate_run("b1", "2026-09-21T10:00:00Z",
                                                                  "2026-09-21T10:05:00Z") + [assistant(ask)])
        verdict, got_signal, _ = h.run_path("veto-unreported", own, last_message=ask)
        # The signal carries a block count like every other veto's because this one now falls through
        # to the shared block exit instead of returning early. Asserting the bare tag pinned the
        # bypassing shape, the one that reached neither the decision log nor the budget.
        record("veto-blocks-a-finished-gate-no-peer-was-told",
               verdict == "block" and got_signal.startswith("block:unreported-gate:")
               and got_signal.rsplit(":", 1)[-1].isdigit(), got_signal)

    # A gate call a PreToolUse hook refused never ran, so no peer is owed a FINISHED line for it. Its refused twin of
    # the must-block row above allows, and a call that ran and errored still counts.
    def refused_run(task, stamp, refusal):
        return [
            {"type": "assistant", "timestamp": stamp, "message": {"content": [
                {"type": "tool_use", "id": "tu-" + task, "name": "PowerShell", "input": {"command": veto_command}}]}},
            {"type": "user", "timestamp": stamp, "toolUseResult": "Error: " + refusal,
             "message": {"content": [{"type": "tool_result", "tool_use_id": "tu-" + task, "is_error": True,
                                      "content": refusal}]}},
        ]

    for name, refusal, expect in (
            ("veto-owes-nothing-for-a-gate-a-hook-refused",
             "PreToolUse:PowerShell hook error: Blocked by the gate protocol (Harness/AGENTS/GATES.md): a gate is in flight.",
             "allow"),
            ("veto-control-a-gate-that-ran-and-failed-is-still-owed", "Exit code 1\nGATE: FAILED (1 failing step(s))",
             "block")):
        if h.wanted(name):
            own = veto_session(name, [user("go")] + refused_run("r1", "2026-09-21T10:00:00Z", refusal) + [assistant(ask)])
            verdict, got_signal, _ = h.run_path(name, own, last_message=ask)
            record(name, verdict == expect, got_signal)

    if h.wanted("veto-allows-once-a-message-follows-the-gate"):
        own = veto_session("reported", [user("go")] + gate_run("b2", "2026-09-21T10:00:00Z", "2026-09-21T10:05:00Z")
                           + [message_at("2026-09-21T10:06:00Z"), user("carry on", "p-2"), assistant(ask)])
        verdict, got_signal, _ = h.run_path("veto-reported", own, last_message=ask)
        record("veto-allows-once-a-message-follows-the-gate",
               verdict == "allow" and got_signal == "allow:question-mark", got_signal)

    if h.wanted("veto-owes-nothing-in-a-solo-session"):
        own = veto_session("solo", [user("go")] + gate_run("b3", "2026-09-21T10:00:00Z", "2026-09-21T10:05:00Z")
                           + [assistant(ask)], peer=None)
        verdict, got_signal, _ = h.run_path("veto-solo", own, last_message=ask)
        record("veto-owes-nothing-in-a-solo-session",
               verdict == "allow" and got_signal == "allow:question-mark", got_signal)

    # A peer SendMessage cannot reach is nobody to notify. Each row differs from the blocking row above
    # in one registry fact, so a filter that stopped discriminating would flip exactly one of them.
    unreachable = [
        ("veto-owes-nothing-to-a-peer-on-another-machine", "remote", False),
        ("veto-owes-nothing-to-a-peer-whose-process-exited", "exited", False),
        ("veto-owes-nothing-to-a-reused-pid", "reused", False),
        ("veto-owes-nothing-to-its-own-mirrored-transcript", None, True),
    ]
    for case, peer, mirror in unreachable:
        if h.wanted(case):
            own = veto_session(case, [user("go")] + gate_run(case, "2026-09-21T10:00:00Z", "2026-09-21T10:05:00Z")
                               + [assistant(ask)], peer=peer, mirror=mirror)
            verdict, got_signal, _ = h.run_path(case, own, last_message=ask)
            record(case, verdict == "allow" and got_signal == "allow:question-mark", got_signal)

    # The read starts at the last SendMessage, not at byte 0: a gate before it is out of view and one
    # after it is caught. Discriminates a scan that ignores the bound, which would also have to read
    # the filler, from one that honours it.
    if h.wanted("veto-reads-only-after-the-last-message"):
        filler = [assistant("padding " + ("éü中" * 400)) for _ in range(400)]
        before = [user("go")] + gate_run("old", "2026-09-21T09:00:00Z", "2026-09-21T09:05:00Z") + filler
        own = veto_session("tail", before + [message_at("2026-09-21T10:00:00Z"), user("continue", "p-2")]
                           + gate_run("new", "2026-09-21T10:01:00Z", "2026-09-21T10:05:00Z") + [assistant(ask)])
        before_bytes = sum(len(json.dumps(r, separators=(",", ":")).encode("utf-8")) + 1 for r in before)
        start = gate.tail_start(own, veto_cadence)
        owed = gate.unreported_finished_gate(own)
        record("veto-reads-only-after-the-last-message",
               start == before_bytes and owed is not None and owed.get("id") == "tu-new",
               "tail starts at byte {0:,}, the message is at {1:,}; owed={2}".format(
                   start, before_bytes, owed.get("id") if owed else None))

    # With the message older than the scan window, every gate still in view was launched after it, so a
    # finished one is genuinely owed. The cap must neither hide it nor turn an in-window report into a
    # block; the second half is the opposite-sign control.
    if h.wanted("veto-cap-keeps-the-verdict-inside-the-window"):
        saved = gate.TAIL_SCAN_BYTES
        gate.TAIL_SCAN_BYTES = 4096
        try:
            filler = [assistant("padding " + ("x" * 200)) for _ in range(60)]
            past = veto_session("cap-owed", [user("go"), message_at("2026-09-21T09:00:00Z")] + filler
                                + gate_run("late", "2026-09-21T10:00:00Z", "2026-09-21T10:05:00Z"))
            told = veto_session("cap-told", [user("go")] + filler
                                + gate_run("told", "2026-09-21T10:00:00Z", "2026-09-21T10:05:00Z")
                                + [message_at("2026-09-21T10:06:00Z")])
            owed_past = gate.unreported_finished_gate(past)
            owed_told = gate.unreported_finished_gate(told)
        finally:
            gate.TAIL_SCAN_BYTES = saved
        record("veto-cap-keeps-the-verdict-inside-the-window",
               owed_past is not None and owed_told is None,
               "message past the window -> {0}; message inside it -> {1}".format(
                   "owed" if owed_past else "None", "owed" if owed_told else "None"))

    def profile_relative(args_text):
        return (("USERPROFILE" in args_text.upper() or "LOCALAPPDATA" in args_text.upper())
                and not re.search(r"(?i)[A-Za-z]:\\+Users\\+", args_text))

    if h.wanted("stop-launcher-is-profile-relative"):
        record("stop-launcher-is-profile-relative", profile_relative(gate.STOP_LAUNCHER),
               "STOP_LAUNCHER names no profile variable, or a hardcoded profile path")
        record("must-block: a hardcoded profile path is not profile-relative",
               not profile_relative(gate.STOP_LAUNCHER + "\n# C:\\Users\\someone\\AppData\\Local"),
               "the check accepts a hardcoded C:\\Users\\ path")

    def registered_stop_script(args):
        """The gate a registered Stop hook runs: the gate itself named in its arguments, or the launcher, whose
        stop-launcher rows prove it runs the checkout's gate; None for anything else."""
        if args == ["-c", gate.STOP_LAUNCHER]:
            return "launch_stop.py"
        if len(args) == 2 and args[0] == "-c" and "launch_stop.py" in args[1] \
                and "os.environ.get('CLAUDE_TOOLS_ROOT')" not in args[1] \
                and "runpy.run_path(p,run_name='__main__')" in args[1]:
            return "launch_stop.py (the previous launcher, naming the Tools path)"
        joined = " ".join(a for a in args if not a.startswith("-"))
        return "invoke_stop_gate.py" if "invoke_stop_gate.py" in joined else None

    if h.wanted("stop-hook-registered"):
        record("must-block: a Stop hook naming another script is not the stop gate",
               registered_stop_script(["C:\\hooks\\some_other_hook.py"]) is None, "another script was accepted")
        record("must-block: a launcher whose code differs from STOP_LAUNCHER is not the stop gate",
               registered_stop_script(["-c", gate.STOP_LAUNCHER + "\nprint(1)"]) is None, "an altered launcher was accepted")
        record("control: the registered launcher is the stop gate",
               registered_stop_script(["-c", gate.STOP_LAUNCHER]) == "launch_stop.py", "the launcher was refused")
        record("control: the previous launcher, naming the Tools path, is the stop gate until the switch",
               (registered_stop_script(["-c", gate.STOP_LAUNCHER.replace("os.environ.get('CLAUDE_TOOLS_ROOT') or '?'",
                                                                           "'source','repos','workspace','Tools'")])
                or "").startswith("launch_stop.py"), "the previous launcher was refused")
        record("control: a hook naming invoke_stop_gate.py is the stop gate",
               registered_stop_script(["C:\\Tools\\Hooks\\invoke_stop_gate.py"]) == "invoke_stop_gate.py",
               "the gate script was refused")

    # Not testable in-process: that the command actually registered in settings.json runs and blocks.
    if h.wanted("registered-command-form-actually-runs"):
        settings = os.path.join(os.environ["USERPROFILE"], ".claude", "settings.json")
        try:
            with open(settings, "r", encoding="utf-8") as fh:
                hook = json.load(fh)["hooks"]["Stop"][0]["hooks"][0]
            joined = " ".join(a for a in hook.get("args", []) if not a.startswith("-"))
            script = registered_stop_script(hook.get("args", []))
            record("stop-hook-registered-in-settings", script is not None, str(script))
            record("stop-hook-path-is-profile-relative", profile_relative(joined), "no hardcoded profile path")

            records = [user("go"), tool_use(), assistant("Tests are green and the gate is clean.")]
            path = h.transcript("live", records)
            live_state = os.path.join(claude_temp.stop_gate_state_root(), "harness-live-invocation.json")
            if os.path.exists(live_state):
                os.remove(live_state)
            payload = json.dumps({"hook_event_name": "Stop", "transcript_path": path,
                                  "session_id": "harness-live-invocation",
                                  "stop_hook_active": False})
            t0 = time.time()
            proc = subprocess.run([hook["command"]] + hook.get("args", []),
                                  input=payload, capture_output=True, text=True)
            elapsed = int((time.time() - t0) * 1000)
            record("registered-command-form-actually-runs",
                   '"decision":"block"' in proc.stdout.replace(" ", ""),
                   "{0}ms".format(elapsed))
            # An absent key means the platform's default, not a zero budget. Modelling it as zero made
            # this row unpassable for any hook that states no timeout, so it sat red on the config's
            # silence rather than on the hook's real margin. The margin this measures is against the
            # documented default, which is a claim about the platform rather than a reading of it: an
            # Edit|Write hook has been seen enforced at 15s against a configured 60s.
            documented_default_seconds = 600
            budget = int(hook.get("timeout", documented_default_seconds)) * 1000
            record("stop-hook-timeout-has-real-margin", budget >= elapsed * 3,
                   "{0}ms run vs {1}ms budget".format(elapsed, budget))
        except Exception as exc:
            record("registered-command-form-actually-runs", False,
                   "{0}: {1}".format(type(exc).__name__, exc))

    if h.wanted("stop-launcher"):
        def fixture_profile(name):
            profile = os.path.join(h.root, "launcher-" + name)
            return profile, os.path.join(profile, "source", "repos", "crgolden", "Tools")

        present, present_tools = fixture_profile("present")
        tools = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for directory in ("Hooks", "Gates"):
            os.makedirs(os.path.join(present_tools, directory))
            for name in os.listdir(os.path.join(tools, directory)):
                if name.endswith((".py", ".txt")):
                    shutil.copy(os.path.join(tools, directory, name), os.path.join(present_tools, directory, name))
        empty, _ = fixture_profile("empty")
        os.makedirs(empty)
        path = h.transcript("launcher", [user("go"), tool_use(), assistant("Tests are green and the gate is clean.")])

        def launch(profile, active, tools_root=os.path.relpath(present_tools, present)):
            payload = json.dumps({"hook_event_name": "Stop", "transcript_path": path,
                                  "session_id": "harness-stop-launcher", "stop_hook_active": active})
            env = dict(os.environ, USERPROFILE=profile)
            env.pop("CRGOLDEN_WORKSPACE", None)
            env.pop("CLAUDE_TOOLS_ROOT", None)
            if tools_root is not None:
                env["CLAUDE_TOOLS_ROOT"] = tools_root
            return subprocess.run([sys.executable, "-c", gate.STOP_LAUNCHER], input=payload, capture_output=True,
                                  text=True, env=env)

        ran = launch(present, False)
        record("stop-launcher-runs-the-checkout-gate",
               ran.returncode == 0 and '"decision":"block"' in ran.stdout.replace(" ", "")
               and "could not load" not in ran.stdout, ran.stdout[:120] + ran.stderr[-200:])
        unset = launch(present, False, tools_root=None)
        record("must-block: stop-launcher-blocks-with-no-CLAUDE_TOOLS_ROOT-even-beside-a-present-checkout",
               unset.returncode == 0 and '"decision":"block"' in unset.stdout.replace(" ", "")
               and "stop hook launcher is missing" in unset.stdout, unset.stdout[:160])
        missing = launch(empty, False)
        record("must-block: stop-launcher-blocks-the-first-stop-when-the-launcher-file-is-missing",
               missing.returncode == 0 and '"decision":"block"' in missing.stdout.replace(" ", "")
               and "stop hook launcher is missing" in missing.stdout, missing.stdout[:160])
        retry = launch(empty, True)
        record("must-block: stop-launcher-blocks-the-retry-too-so-a-broken-checkout-buys-no-stop",
               retry.returncode == 0 and '"decision":"block"' in retry.stdout.replace(" ", "")
               and "is missing" in retry.stdout, retry.stdout[:120])
        gateless, gateless_tools = fixture_profile("gateless")
        os.makedirs(os.path.join(gateless_tools, "Hooks"))
        shutil.copy(os.path.join(tools, "Hooks", "launch_stop.py"), os.path.join(gateless_tools, "Hooks", "launch_stop.py"))
        no_gate = launch(gateless, False)
        record("must-block: stop-launcher-blocks-when-the-launcher-file-is-there-but-the-gate-is-missing",
               no_gate.returncode == 0 and '"decision":"block"' in no_gate.stdout.replace(" ", "")
               and "could not load" in no_gate.stdout, no_gate.stdout[:160] + no_gate.stderr[-200:])
        exiting, exiting_tools = fixture_profile("exiting")
        os.makedirs(os.path.join(exiting_tools, "Hooks"))
        shutil.copy(os.path.join(tools, "Hooks", "launch_stop.py"), os.path.join(exiting_tools, "Hooks", "launch_stop.py"))
        with open(os.path.join(exiting_tools, "Hooks", "invoke_stop_gate.py"), "w", encoding="utf-8") as fh:
            fh.write("import sys\nsys.exit(1)\n")
        exited = launch(exiting, False)
        record("must-block: stop-launcher-blocks-when-the-gate-exits-nonzero",
               exited.returncode == 0 and '"decision":"block"' in exited.stdout.replace(" ", "")
               and "exited with code 1" in exited.stdout, exited.stdout[:160] + exited.stderr[-200:])
        settings = os.path.join(os.environ["USERPROFILE"], ".claude", "settings.json")
        with open(settings, "r", encoding="utf-8") as fh:
            stop_hook = json.load(fh)["hooks"]["Stop"][0]["hooks"][0]
        registered = " ".join(stop_hook.get("args") or [])
        if "launch_stop.py" in registered and "os.environ.get('CLAUDE_TOOLS_ROOT')" not in registered:
            skip("stop-launcher-is-what-settings-registers", "settings.json registers the previous STOP_LAUNCHER, which "
                 "names the Tools path; the operator switches it, with CLAUDE_TOOLS_ROOT in the env block, only after "
                 "every machine's Tools checkout holds this one")
        elif "launch_stop.py" in registered:
            record("stop-launcher-is-what-settings-registers", stop_hook.get("args") == ["-c", gate.STOP_LAUNCHER],
                   "args differ from STOP_LAUNCHER")
        elif "raw=sys.stdin.buffer.read()" in registered:
            skip("stop-launcher-is-what-settings-registers", "settings.json registers the previous STOP_LAUNCHER; it is "
                 "switched only after every machine's Tools checkout holds launch_stop.py")
        else:
            skip("stop-launcher-is-what-settings-registers", "settings.json does not register STOP_LAUNCHER yet")

    # THE ENVELOPE, per block path, through main(), asserting the BYTES rather than the signal. The
    # gate shipped disarmed for a whole session because the two halves above were never crossed: the
    # veto cases assert the signal string and never look at stdout, and registered-command-form
    # asserts the JSON but on a fixture that takes the generic path, which still built its envelope.
    # A leaf returning a bare reason satisfied both while the host read it as advisory text.
    if h.wanted("block-paths-emit-parseable-json"):
        def wire_run(name, own_path, active=False):
            state = os.path.join(h.root, "state-wire-" + name)
            payload = json.dumps({"hook_event_name": "Stop", "transcript_path": own_path,
                                  "session_id": "wire-" + name, "stop_hook_active": active,
                                  "last_assistant_message": ask})
            proc = subprocess.run([sys.executable, os.path.abspath(gate.__file__),
                                   "--state-directory", state],
                                  input=payload, capture_output=True, text=True)
            return proc.returncode, proc.stdout

        def blocks_as_json(text):
            try:
                parsed = json.loads(text)
            except Exception:
                return False
            return parsed.get("decision") == "block" and bool(parsed.get("reason"))

        # The must-BLOCK row, written first: this is the exact path that shipped unenforced.
        owed_own = veto_session("wire-unreported", [user("go")]
                                + gate_run("w1", "2026-09-21T10:00:00Z", "2026-09-21T10:05:00Z")
                                + [assistant(ask)])
        owed_code, owed_out = wire_run("unreported", owed_own)
        record("unreported-gate-block-reaches-stdout-as-json",
               owed_code == 0 and blocks_as_json(owed_out),
               "exit {0}, stdout opens {1!r}".format(owed_code, owed_out[:24]))

        # The opposite sign, without which a hook that emitted a block unconditionally would pass the
        # row above. An allowed stop has to leave stdout EMPTY, not merely non-blocking.
        # The shipped gate requires a closing question parked and the page read after parking, so the session parks
        # the question, reads the page, then asks.
        parked = [{"type": "assistant", "message": {"content": [
                      {"type": "tool_use", "id": "tu-p", "name": "ArtifactData",
                       "input": {"action": "set", "url": "https://claude.ai/artifact/x", "collection": "questions",
                                 "doc_id": "q1-ordering", "data": {"question": ask, "status": "open"}}}]}},
                  {"type": "user", "message": {"role": "user", "content": [
                      {"type": "tool_result", "tool_use_id": "tu-p", "content": "committed"}]}}]
        solo_own = veto_session("wire-solo", [user("go")]
                                + gate_run("w2", "2026-09-21T10:00:00Z", "2026-09-21T10:05:00Z")
                                + parked + open_read() + [assistant(ask)], peer=None)
        solo_code, solo_out = wire_run("solo", solo_own)
        record("an-allowed-stop-emits-no-envelope", solo_code == 0 and solo_out.strip() == "",
               "exit {0}, {1} bytes: {2!r}".format(solo_code, len(solo_out), solo_out[:60]))

        # The same allowed stop under an active /goal, through main(): the wire must carry continue:false, or the
        # /goal hook's block reopens the turn whatever this gate decided.
        goal_own = veto_session("wire-goal", [user("go"), goal_status("ordering matches your intent")]
                                + parked + open_read() + [assistant(ask)], peer=None)
        goal_code, goal_out = wire_run("goal", goal_own)
        try:
            goal_wire = json.loads(goal_out)
        except ValueError:
            goal_wire = {}
        record("an-allowed-ask-under-an-active-goal-emits-continue-false",
               goal_code == 0 and goal_wire.get("continue") is False and "decision" not in goal_wire,
               "exit {0}, stdout {1!r}".format(goal_code, goal_out[:80]))

    # The payload's last_assistant_message arrives as UTF-8 on a stdin whose default is the ANSI code
    # page. main() must hand decide() the message intact; the child swaps decide() for a recorder.
    if h.wanted("utf-8-stdin-reaches-decide-intact"):
        message = "The veto catches “the commit message is in the scratchpad” — quoted."
        env = {key: value for key, value in os.environ.items() if key not in ("PYTHONUTF8", "PYTHONIOENCODING")}
        env["PYTHONUTF8"] = "0"
        recorder = ("import json, sys\n"
                    "sys.path.insert(0, sys.argv[1])\n"
                    "import invoke_stop_gate as gate\n"
                    "seen = []\n"
                    "gate.decide = lambda payload, *rest: (seen.append(payload.get('last_assistant_message')), ('allow:recorded', ''))[1]\n"
                    "sys.argv = sys.argv[:1] + ['--state-directory', sys.argv[2]]\n"
                    "gate.main()\n"
                    "sys.stdout.buffer.write(json.dumps(seen).encode('utf-8'))\n")
        payload = json.dumps({"hook_event_name": "Stop", "transcript_path": "", "session_id": "utf8",
                              "stop_hook_active": False, "last_assistant_message": message},
                             ensure_ascii=False).encode("utf-8")
        proc = subprocess.run([sys.executable, "-c", recorder, os.path.dirname(os.path.abspath(gate.__file__)),
                               os.path.join(h.root, "state-utf8")], input=payload, capture_output=True, env=env)
        try:
            seen = json.loads(proc.stdout.decode("utf-8"))
        except Exception:
            seen = []
        record("utf-8-stdin-reaches-decide-intact", seen == [message],
               "intact" if seen == [message] else "garbled or missing: {0!r}".format(seen)[:120])

    # A harness whose run raises before its cleanup line still leaves no root behind.
    crasher = ("import os, sys\nsys.path.insert(0, sys.argv[1])\nimport test_stop_gate\n"
               "harness = test_stop_gate.Harness(None)\nprint(harness.root, flush=True)\nraise RuntimeError('row crashed')\n")
    if h.wanted("a-harness-that-raises-leaves-no-root"):
        proc = subprocess.run([sys.executable, "-c", crasher, os.path.dirname(os.path.abspath(__file__))],
                              capture_output=True, text=True, env=dict(os.environ))
        crashed_root = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
        record("a-harness-that-raises-leaves-no-root",
               proc.returncode != 0 and bool(crashed_root) and not os.path.exists(crashed_root),
               "exit {0}, root {1!r} {2}".format(proc.returncode, crashed_root,
                                                 "still exists" if crashed_root and os.path.exists(crashed_root) else "gone"))

    # A tree whose file another process still holds open (as a scanner does just after a write): the POSIX delete
    # removes it, while a legacy (non-POSIX) delete, which a plain rmtree is on older builds, leaves a pending entry
    # and the folder's removal fails "The directory is not empty". The control calls the legacy disposition directly,
    # so it holds on every Windows build.
    if h.wanted("remove-tree-deletes-past-an-open-handle") and os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = ctypes.c_void_p

        def held_tree(label):
            folder = os.path.join(h.root, label, "inner")
            os.makedirs(folder)
            target = os.path.join(folder, "held.txt")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("held")
            handle = kernel32.CreateFileW(ctypes.c_wchar_p(target), 0x80000000, 7, None, 3, 0, None)
            return os.path.join(h.root, label), handle

        posix_root, posix_handle = held_tree("posix-held")
        try:
            posix_left = claude_temp.remove_tree(posix_root)
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(posix_handle))
        plain_root, plain_handle = held_tree("plain-held")
        plain_file = os.path.join(plain_root, "inner", "held.txt")
        try:
            deleter = kernel32.CreateFileW(ctypes.c_wchar_p(plain_file), claude_temp.DELETE_ACCESS, claude_temp.SHARE_ALL,
                                           None, claude_temp.OPEN_EXISTING, 0, None)
            delete_file = ctypes.c_ubyte(1)
            marked = bool(kernel32.SetFileInformationByHandle(ctypes.c_void_p(deleter), 4, ctypes.byref(delete_file), 1))
            kernel32.CloseHandle(ctypes.c_void_p(deleter))
            try:
                os.rmdir(os.path.dirname(plain_file))
                plain_failed = False
            except OSError:
                plain_failed = marked
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(plain_handle))
        claude_temp.remove_tree(plain_root)
        record("remove-tree-deletes-past-an-open-handle", posix_left == [] and not os.path.exists(posix_root),
               "left={0!r}".format(posix_left))
        mixed_root = os.path.join(h.root, "one-held-among-siblings")
        os.makedirs(os.path.join(mixed_root, "a"))
        os.makedirs(os.path.join(mixed_root, "z"))
        for relative in (os.path.join("a", "before.txt"), "held.txt", os.path.join("z", "after.txt"), "zz-after.txt"):
            with open(os.path.join(mixed_root, relative), "w", encoding="utf-8") as fh:
                fh.write("x")
        locked = kernel32.CreateFileW(ctypes.c_wchar_p(os.path.join(mixed_root, "held.txt")), 0x80000000, 1, None, 3, 0,
                                      None)
        try:
            mixed_left = claude_temp.remove_tree(mixed_root)
            survivors = sorted(os.listdir(mixed_root)) if os.path.isdir(mixed_root) else []
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(locked))
        record("remove-tree-keeps-going-past-one-held-file", survivors == ["held.txt"]
               and sorted(os.path.basename(p) for p in mixed_left) == sorted(["held.txt", os.path.basename(mixed_root)]),
               "survivors={0!r} left={1!r}".format(survivors, [os.path.basename(p) for p in mixed_left]))
        claude_temp.remove_tree(mixed_root)
        deep_root = os.path.join(h.root, "deep-path")
        deep_file = os.path.join(claude_temp.extended_length_path(deep_root), "d" * 120, "e" * 120, "past-max-path.json")
        os.makedirs(os.path.dirname(deep_file))
        with open(deep_file, "w", encoding="utf-8") as fh:
            fh.write("{}")
        deep_left = claude_temp.remove_tree(deep_root)
        record("remove-tree-deletes-a-file-past-max-path", len(deep_file) > 260 and deep_left == []
               and not os.path.lexists(claude_temp.extended_length_path(deep_root)),
               "path {0} characters, left={1!r}".format(len(deep_file), deep_left[:2]))
        record("control-a-legacy-delete-leaves-the-folder-undeletable-past-the-same-open-handle", plain_failed,
               "legacy delete {0}".format("left a pending entry" if plain_failed
                                          else "removed the name or never marked it, so the row above proves nothing"))

    left = h.cleanup()
    record("the-run-root-is-removed-at-the-end", left == [], "left={0!r}".format(left[:3]))

    failures = 0
    for name, ok, detail in results:
        print("{0}  {1:<46} {2}".format("PASS" if ok else "FAIL", name, detail))
        if not ok:
            failures += 1
    for name, why in skipped:
        print("SKIP  {0:<46} {1}".format(name, why))

    print("")
    print("{0} case(s) in {1:.2f}s, {2} skipped".format(
        len(results), time.time() - started, len(skipped)))
    if args.only and selected == 0:
        print("--only matched no cases; refusing to report success on an empty selection.")
        return 1
    if failures == 0:
        print("All stop-gate cases passed.")
        return 0
    print("{0} case(s) failed.".format(failures))
    return 1


if __name__ == "__main__":
    sys.exit(main())
