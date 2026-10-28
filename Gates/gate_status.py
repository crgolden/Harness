"""The most recent gate result of each repository, and whether it still describes the tree.

GateCommon.ps1 writes <state_dir>/<Repo>.<COMPUTERNAME>.last.json when a gate starts (verdict RUNNING), again when
each step starts and after each row (still RUNNING, with currentStep and the gate's pid), and when it completes
(PASSED, FAILED or DRIFTED), with every step's row and a per-file hash manifest of the inputs
it read. This reads that record and compares its manifest with the tree as it is now, using the same file set
and hashes GateCommon uses, so a verdict can be checked later rather than remembered. Each checkout keeps its
records in its own folder under %TEMP%\\claude\\crgolden-gates\\records, named for its path (checkout_state_dir
below); a triage run keeps its records in the fenced folder beside the triage worktrees.

    python gate_status.py [Repo ...] [--json] [--all-checkouts]
    python gate_status.py --record-landing Repo

States: GREEN-CURRENT (passed on exactly the current inputs), CURRENT-IMPLICIT (no uncommitted change and HEAD is
origin's default branch HEAD: checked in and pushed, which stands in for a gate run, and is read as GREEN-CURRENT),
GREEN-STALE (passed, then inputs changed; the changed files are listed), RED (the last completed run failed),
DRIFTED (inputs changed while it ran), RUNNING (started, not completed, and its gate process still alive or on
another machine), STOPPED (started and not completed, and its gate process on this machine is gone: killed or
stopped, never a verdict), SUPERSEDED (a gate that stopped because its repo landed on main while it ran), NEVER-GATED
(no record).

A landing is written into the global record, the repo's main-checkout record, by --record-landing once the change is
checked in and pushed and the main checkout is pulled to origin's HEAD: verdict LANDED, the landed sha, when, any
uncommitted files there (a peer's work), and the record it replaced. It reads CURRENT-IMPLICIT while the checkout is
clean at that HEAD, and NEVER-GATED, naming the files, while anything there is uncommitted. A gate running on the old tree, in that
checkout or a worktree, reads the landing between steps and stops as SUPERSEDED rather than spend its time on code main
no longer holds (GateCommon.ps1, Get-GateSupersedingLanding). A RUNNING record on a checkout that is now clean and
pushed reads CURRENT-IMPLICIT even with no landing written, since a gate never runs on a clean tree: whatever it was
gating is either on main or gone. A landing grants no carried step: carries live in <Repo>.<COMPUTERNAME>.json, which
it never touches.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "Hooks")
if HOOKS not in sys.path:
    sys.path.insert(0, HOOKS)
import claude_temp  # noqa: E402
import session_home  # noqa: E402

TOOL_FILE_NAMES_ARE_DATA = "GATE-CADENCE: gate-step-strings-are-data"
WORKSPACE = session_home.workspace_root()
TOOLS_ROOT = os.path.join(WORKSPACE, "Tools")
TOOLS_REPO = "Tools"
RECORD_SUFFIX = ".last.json"
LEFTOVER = re.compile(r"(^|/)(StrykerOutput|\.sonarqube)/")
EXCLUDED = (
    re.compile(r"\.md$", re.IGNORECASE),
    re.compile(r"^\.github/"),
    LEFTOVER,
)

RECORD_ENCODING = "utf-8-sig"
FIELD_VERDICT = "verdict"
FIELD_STEPS = "steps"
FIELD_MANIFEST = "manifest"
FIELD_TOOLS = "tools"
FIELD_FINGERPRINT = "fingerprint"

VERDICT_RUNNING = "RUNNING"
VERDICT_PASSED = "PASSED"
VERDICT_FAILED = "FAILED"
VERDICT_DRIFTED = "DRIFTED"
VERDICT_LANDED = "LANDED"
VERDICT_SUPERSEDED = "SUPERSEDED"
STEP_PASS = "PASS"
STEP_CARRIED = "CARRIED"

STATUS_STATE = "state"
STATUS_CHANGED = "changed"

GREEN_CURRENT = "GREEN-CURRENT"
CURRENT_IMPLICIT = "CURRENT-IMPLICIT"
GREEN_STATES = (GREEN_CURRENT, CURRENT_IMPLICIT)
GREEN_STALE = "GREEN-STALE"
GREEN_STALE_AUDIT = "GREEN-STALE-AUDIT"
FIELD_VOLATILE = "volatile"
VOLATILE_MAX_AGE = timedelta(hours=24)
RED = "RED"
DRIFTED = "DRIFTED"
RUNNING = "RUNNING"
STOPPED = "STOPPED"
SUPERSEDED = "SUPERSEDED"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
ACCESS_DENIED = 5


def process_alive(pid) -> bool:
    """True while the Windows process `pid` is running; an unknown pid counts as alive, so it is never a false stop."""
    if not isinstance(pid, int) or pid <= 0 or os.name != "nt":
        return True
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return kernel.GetLastError() == ACCESS_DENIED
    try:
        code = ctypes.c_ulong()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def run_is_gone(record: dict, alive=process_alive) -> bool:
    """True when a RUNNING record was written on this machine and its gate process no longer runs. A record from
    another machine, or one with no pid, cannot be checked here and is never called gone."""
    machine = str(record.get("machine") or "")
    return machine.upper() == os.environ.get("COMPUTERNAME", "").upper() and not alive(record.get("pid"))
NEVER_GATED = "NEVER-GATED"
UNREADABLE = "UNREADABLE"


def file_hash(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest().upper()


RE_STEP_INPUTS = re.compile(r"Register-StepInputs @\{(?P<body>.*?)\n\}", re.DOTALL)
RE_MARKDOWN_INPUT = re.compile(r"'(?P<path>[^'*?\[\]]+\.md)'", re.IGNORECASE)


def declared_markdown(repo_path: str) -> set[str]:
    """The markdown files a step of the repo's gate.ps1 names as an input, path for path, never through a wildcard:
    a harness that parses one is judged by it, so the gate fingerprints it though every other markdown file stays
    excluded. GateCommon.ps1's Get-GateManifest applies the same rule to the inputs gate.ps1 registers, so every
    Register-StepInputs block is read and each such path must be a quoted literal: one built from a variable is
    invisible here and the two manifests would disagree."""
    try:
        with open(os.path.join(repo_path, "gate.ps1"), encoding="utf-8-sig") as handle:
            text = handle.read()
    except OSError:
        return set()
    if "step_inputs.py" in text and "--plan" in text:
        here = os.path.dirname(os.path.realpath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import step_inputs
        planned = step_inputs.plan(repo_path, step_inputs.default_cache_path(repo_path))
        return {path for step in planned["steps"] for path in step["inputs"]
                if path.lower().endswith(".md") and not any(char in path for char in "*?[]")}
    return {match.group("path") for block in RE_STEP_INPUTS.finditer(text)
            for match in RE_MARKDOWN_INPUT.finditer(block.group("body"))}


def fingerprinted(name: str, declared: set[str]) -> bool:
    """Whether a gate's manifest covers the repo-relative file: a markdown file a step declares, or any file no
    EXCLUDED pattern matches."""
    return name in declared or not any(pattern.search(name) for pattern in EXCLUDED)


def input_files(repo_path: str) -> list[str]:
    listed = subprocess.run(
        ["git", "-C", repo_path, "ls-files", "-co", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    declared = declared_markdown(repo_path)
    return sorted(name for name in listed if name and fingerprinted(name, declared))


def manifest(repo_path: str) -> dict[str, str]:
    hashes = {}
    for name in input_files(repo_path):
        full = os.path.join(repo_path, name)
        if os.path.isfile(full):
            hashes[name] = file_hash(full)
    return hashes


def tool_hashes(tools_root: str = TOOLS_ROOT, names=()) -> dict[str, str | None]:
    """The hash now of each Tools file named, None for one that is gone. The names are the ones the gate record lists
    under "tools": the gate library writes every Tools file its verdicts depend on there, so this reader keeps no list
    of its own to drift from it."""
    hashes = {}
    for name in names:
        full = os.path.join(tools_root, *name.split("/"))
        hashes[name] = file_hash(full) if os.path.isfile(full) else None
    return hashes


def fingerprint(files: dict[str, str], tools: dict[str, str]) -> str:
    text = "".join(f"{name}:{digest}\n" for name, digest in sorted(files.items()))
    text += "".join(f"Tools/{name}:{digest}\n" for name, digest in sorted(tools.items()))
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def gated_repos() -> list[str]:
    return sorted(
        name for name in os.listdir(WORKSPACE)
        if os.path.isfile(os.path.join(WORKSPACE, name, "gate.ps1"))
    )


def this_machine() -> str:
    """COMPUTERNAME, which names this machine's record. Empty, every such run would share one record name again, so it
    is refused."""
    machine = os.environ.get("COMPUTERNAME", "").strip()
    if not machine:
        raise RuntimeError("COMPUTERNAME is unset, so no gate record can be named for this machine")
    return machine


def checkout_state_dir(repo: str, checkout: str) -> str:
    """Where a checkout's gate writes its state, GateCommon's default for every repo: the checkout's own folder under
    the gate records in %TEMP%\\claude (claude_temp.gate_records_dir), never a .claude folder."""
    return claude_temp.gate_records_dir(checkout)


def repo_state_dir(repo: str, workspace: str = WORKSPACE) -> str:
    """The state directory of the repo's main checkout in the workspace."""
    return checkout_state_dir(repo, os.path.join(workspace, repo))


def checkouts(repo: str, workspace: str = WORKSPACE) -> list[str]:
    """The repo's main checkout in the workspace, then each of its worktrees that still exists, read from the
    gitdir files git keeps in <main>/.git/worktrees/<name>, so no git process runs."""
    main = os.path.join(workspace, repo)
    found = [main] if os.path.isdir(main) else []
    registry = os.path.join(main, ".git", "worktrees")
    if not os.path.isdir(registry):
        return found
    for name in sorted(os.listdir(registry)):
        try:
            with open(os.path.join(registry, name, "gitdir"), encoding="utf-8") as handle:
                gitfile = handle.read().strip()
        except OSError:
            continue
        checkout = os.path.dirname(os.path.normpath(gitfile))
        if gitfile and os.path.isdir(checkout):
            found.append(checkout)
    return found


def record_moment(record: dict | None) -> str:
    return (record or {}).get("completedAt") or (record or {}).get("startedAt") or ""


def latest_checkout(repo: str, workspace: str = WORKSPACE) -> tuple[str, dict | None]:
    """The checkout whose record is newest among the repo's checkouts, with that record; the main checkout and
    None when no checkout has one. A record this account cannot read is no record."""
    best = (os.path.join(workspace, repo), None)
    for checkout in checkouts(repo, workspace):
        try:
            record = read_record(repo, checkout_state_dir(repo, checkout))
        except (OSError, ValueError):
            continue
        if record and (best[1] is None or record_moment(record) > record_moment(best[1])):
            best = (checkout, record)
    return best


def git_in(checkout: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", checkout, *args], capture_output=True, text=True)


def origin_head(checkout: str) -> str | None:
    """The checkout's HEAD when it is origin's default branch HEAD as last fetched, else None."""
    head, origin = git_in(checkout, "rev-parse", "HEAD"), git_in(checkout, "rev-parse", "origin/HEAD")
    if head.returncode != 0 or origin.returncode != 0 or head.stdout.strip() != origin.stdout.strip():
        return None
    return head.stdout.strip()


def uncommitted(checkout: str) -> list[str] | None:
    """The checkout's uncommitted paths, a gate's own build leftovers aside, or None when git cannot say."""
    status_run = git_in(checkout, "status", "--porcelain", "--untracked-files=all")
    if status_run.returncode != 0:
        return None
    return [line[3:].strip('"') for line in status_run.stdout.splitlines()
            if line and not LEFTOVER.search(line[3:].strip('"'))]


def pushed_and_clean(checkout: str) -> bool:
    """No uncommitted change and HEAD is origin's default branch HEAD: a change checked in and pushed, which stands in
    for a gate run."""
    return uncommitted(checkout) == [] and origin_head(checkout) is not None


def with_implicit(entry: dict, checkout: str) -> dict:
    """GREEN-CURRENT stands as it is; any other state of a checkout that is checked in and pushed is CURRENT-IMPLICIT,
    which every reader treats as GREEN-CURRENT. RUNNING included: a gate never runs on a clean tree, so a run still
    recorded on a checkout that is now clean at origin's HEAD was gating code that has since landed or gone, and it
    describes nothing there now."""
    if entry.get(STATUS_STATE) == GREEN_CURRENT or not os.path.isdir(checkout) or not pushed_and_clean(checkout):
        return entry
    implicit = {**entry, STATUS_STATE: CURRENT_IMPLICIT}
    if entry.get(STATUS_STATE) in (RUNNING, STOPPED):
        implicit["superseded_run"] = {"started_at": entry.get("started_at"), "session": entry.get("session")}
    return implicit


def write_record(path: str, record: dict) -> None:
    """Through a temporary file and a rename, as GateCommon writes, so a reader never sees half a record."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    staging = "{0}.{1}.tmp".format(path, os.getpid())
    with open(staging, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
    os.replace(staging, path)


def record_landing(repo: str, workspace: str = WORKSPACE, now: datetime | None = None) -> dict:
    """Writes the landing into the repo's main-checkout record and returns it. Refused, with ValueError, unless the
    main checkout's HEAD is origin's HEAD: a landing is a fact about main, never a claim. Uncommitted files there, a
    peer's work not yet gated, do not stop it: they are listed in the record and keep the state from reading green,
    while every gate on the old tree still learns that main moved."""
    checkout = os.path.join(workspace, repo)
    head = origin_head(checkout) if os.path.isdir(checkout) else None
    if head is None:
        raise ValueError("{0} is not at origin's HEAD: pull it first, so the record describes what landed".format(checkout))
    pending = uncommitted(checkout)
    state_dir = repo_state_dir(repo, workspace)
    try:
        replaced = read_record(repo, state_dir)
    except (OSError, ValueError):
        replaced = None
    moment = now or datetime.now(timezone.utc)
    record = {
        "repo": repo,
        FIELD_VERDICT: VERDICT_LANDED,
        "startedAt": moment.isoformat().replace("+00:00", "Z"),
        "completedAt": moment.isoformat().replace("+00:00", "Z"),
        "landedAt": moment.isoformat().replace("+00:00", "Z"),
        "landedAtMs": int(moment.timestamp() * 1000),
        "head": head,
        "uncommitted": pending,
        "machine": this_machine(),
        "session": os.environ.get("CLAUDE_CODE_SESSION_ID"),
        "repoPath": checkout,
        "replaced": None if not isinstance(replaced, dict) else {
            key: replaced.get(key) for key in (FIELD_VERDICT, "startedAt", "completedAt", "session", "pid")},
        FIELD_STEPS: [],
    }
    write_record(record_path(repo, state_dir), record)
    return record


def is_green(entry: dict | None) -> bool:
    """The states a ready-to-ship claim, a commit message and a deploy rest on."""
    return bool(entry) and entry.get(STATUS_STATE) in GREEN_STATES


def latest_status(repo: str, workspace: str = WORKSPACE, now: datetime | None = None) -> dict:
    """status() of the repo's checkout with the newest record, so a gate run on a branch worktree is the one read."""
    checkout, _ = latest_checkout(repo, workspace)
    return with_implicit({**status(repo, workspace, checkout_state_dir(repo, checkout), now), "checkout": checkout}, checkout)


def checkout_statuses(repo: str, workspace: str = WORKSPACE, now: datetime | None = None) -> list[dict]:
    """status() of every checkout of the repo, each judged on its own record and its own files, newest record first.
    A deploy needs a green record on the tree it deploys, whichever checkout ran it, so a newer run in another
    worktree (a peer's, still dirty) never hides one."""
    entries = []
    for checkout in checkouts(repo, workspace):
        try:
            entries.append(with_implicit({**status(repo, workspace, checkout_state_dir(repo, checkout), now), "checkout": checkout}, checkout))
        except (OSError, ValueError, subprocess.CalledProcessError) as error:
            entries.append({"repo": repo, STATUS_STATE: UNREADABLE, "checkout": checkout, "error": str(error)})
    return sorted(entries, key=lambda entry: entry.get("completed_at") or entry.get("started_at") or "", reverse=True)


LONG_PATH_THRESHOLD = 240


def long_path_form(path: str) -> str:
    """The path in its \\\\?\\ form on Windows once it nears MAX_PATH (260), with room for a staging suffix: a
    checkout's records folder is named for its whole path, and with long paths off Python reports such a record as
    missing (isfile False, open FileNotFoundError), so a deep checkout read as never gated."""
    if os.name != "nt" or len(path) < LONG_PATH_THRESHOLD or path.startswith("\\\\?\\"):
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def record_path(repo: str, state_dir: str, machine: str | None = None) -> str:
    """This machine's record of a repo's last gate. The name carries the machine, so a record copied from another
    machine (the triage fence's folder, an older synced layout) never stands for this tree."""
    return long_path_form(os.path.join(state_dir, "{0}.{1}{2}".format(repo, machine or this_machine(), RECORD_SUFFIX)))


def read_record(repo: str, state_dir: str, machine: str | None = None) -> dict | None:
    """This machine's record, else None."""
    path = record_path(repo, state_dir, machine)
    if not os.path.isfile(path):
        return None
    with open(path, encoding=RECORD_ENCODING) as handle:
        return json.load(handle)


def step_verdicts(record: dict) -> dict[str, str]:
    return {row["name"]: row[FIELD_VERDICT] for row in record.get(FIELD_STEPS, [])}


def recorded_fingerprint(record: dict) -> str:
    """The fingerprint recomputed from the record's own manifest and tool hashes; equal to the one GateCommon
    wrote only while the two implementations agree."""
    return fingerprint(record.get(FIELD_MANIFEST, {}), record.get(FIELD_TOOLS, {}))


def recorded_path(record: dict, field: str, fallback: str) -> str:
    """A checkout the record says its gate used (the gated repo, the Tools tree GateCommon hashed), while it still
    exists; else `fallback`, as for a record written before the field existed or a worktree since removed."""
    path = record.get(field)
    return path if isinstance(path, str) and os.path.isdir(path) else fallback


def volatile_age(record: dict, now: datetime) -> timedelta | None:
    """How old the record's volatile steps (an audit whose verdict depends on an advisory database, never carried)
    are: they ran in the recorded run itself, so their age is the run's. None when the record declares none."""
    completed = record.get("completedAt")
    if not record.get(FIELD_VOLATILE) or not completed:
        return None
    return now - datetime.fromisoformat(completed.replace("Z", "+00:00"))


def progress(record: dict) -> dict:
    """A running gate's progress from its record: the rows finished so far, the step in flight and since when, and
    the process writing it. GateCommon rewrites the record when each step starts and after each row."""
    rows = [row for row in record.get("steps") or [] if isinstance(row, dict)]
    return {
        "finished": len(rows),
        "last": f"{rows[-1].get('name')} {rows[-1].get('verdict')}" if rows else None,
        "current": record.get("currentStep"),
        "current_since": record.get("currentStepStartedAt"),
        "pid": record.get("pid"),
    }


def status(repo: str, workspace: str = WORKSPACE, state_dir: str | None = None, now: datetime | None = None,
           tools_root: str | None = None, alive=process_alive) -> dict:
    """The repo's state now. The record comes from `state_dir`, else the repo's own state directory, and the tool
    hashes from `tools_root`, else the Tools checkout the record says its GateCommon hashed, else TOOLS_ROOT."""
    record = read_record(repo, state_dir or repo_state_dir(repo, workspace))
    if record is None:
        return {"repo": repo, STATUS_STATE: NEVER_GATED}
    verdict = record.get(FIELD_VERDICT)
    result = {
        "repo": repo,
        FIELD_VERDICT: verdict,
        "started_at": record.get("startedAt"),
        "completed_at": record.get("completedAt"),
        "machine": record.get("machine"),
        "session": record.get("session"),
        "repo_path": record.get("repoPath"),
        "not_passed": [f"{name}: {step}" for name, step in step_verdicts(record).items()
                       if step not in (STEP_PASS, STEP_CARRIED)],
    }
    if verdict == VERDICT_RUNNING:
        state = STOPPED if run_is_gone(record, alive) else RUNNING
        return {**result, STATUS_STATE: state, "progress": progress(record)}
    if verdict == VERDICT_LANDED:
        return {**result, STATUS_STATE: NEVER_GATED, "landed": record.get("head"),
                STATUS_CHANGED: uncommitted(recorded_path(record, "repoPath", os.path.join(workspace, repo))) or []}
    if verdict == VERDICT_SUPERSEDED:
        return {**result, STATUS_STATE: SUPERSEDED}
    if verdict == VERDICT_DRIFTED:
        return {**result, STATUS_STATE: DRIFTED}
    if verdict != VERDICT_PASSED:
        return {**result, STATUS_STATE: RED}
    recorded = record.get(FIELD_MANIFEST, {})
    current = manifest(recorded_path(record, "repoPath", os.path.join(workspace, repo)))
    changed = sorted(name for name in set(recorded) | set(current) if recorded.get(name) != current.get(name))
    tools_then = record.get(FIELD_TOOLS, {})
    tools_now = tool_hashes(tools_root or recorded_path(record, "toolsRoot", TOOLS_ROOT), sorted(tools_then))
    changed += [f"Tools/{name}" for name in sorted(tools_then) if tools_then.get(name) != tools_now.get(name)]
    if changed:
        return {**result, STATUS_STATE: GREEN_STALE, STATUS_CHANGED: changed}
    age = volatile_age(record, now or datetime.now(timezone.utc))
    if age is not None and age > VOLATILE_MAX_AGE:
        return {**result, STATUS_STATE: GREEN_STALE_AUDIT, "volatile_age_hours": round(age.total_seconds() / 3600, 1),
                "volatile": record.get(FIELD_VOLATILE)}
    return {**result, STATUS_STATE: GREEN_CURRENT,
            "fingerprint_matches": fingerprint(current, tools_now) == record.get(FIELD_FINGERPRINT)}


def render(entry: dict) -> str:
    line = f"{entry['repo']:<16} {entry['state']}"
    if entry.get("completed_at") or entry.get("started_at"):
        line += f"  ({entry.get('verdict')} {entry.get('completed_at') or entry.get('started_at')}, {entry.get('machine')})"
    if entry.get("checkout") and os.path.normcase(entry["checkout"]) != os.path.normcase(os.path.join(WORKSPACE, entry["repo"])):
        line += f"\n{'':<17}from the worktree {entry['checkout']}"
    running = entry.get("progress")
    if running:
        line += f"\n{'':<17}{running['finished']} step(s) finished"
        if running.get("last"):
            line += f", last {running['last']}"
        if entry.get("state") == STOPPED:
            line += f"; its gate process {running.get('pid')} is gone, so this run never reaches a verdict"
        elif running.get("current"):
            line += f"; running {running['current']} since {running['current_since']}"
    if entry.get("not_passed"):
        line += "\n" + "\n".join(f"{'':<17}not passed: {row}" for row in entry["not_passed"])
    if entry.get("changed"):
        line += "\n" + "\n".join(f"{'':<17}changed: {name}" for name in entry["changed"])
    if entry.get("volatile_age_hours") is not None:
        line += f"\n{'':<17}volatile steps {', '.join(entry.get('volatile', []))} ran {entry['volatile_age_hours']} h ago; re-run them"
    return line


def main(argv: list[str]) -> int:
    if "--record-landing" in argv:
        repos = [arg for arg in argv if not arg.startswith("--")]
        if len(repos) != 1:
            print("--record-landing takes exactly one repo")
            return 2
        try:
            landed = record_landing(repos[0])
        except ValueError as error:
            print("NOT RECORDED: {0}".format(error))
            return 1
        replaced = landed["replaced"] or {}
        print("LANDED: {0} at {1} recorded {2}, replacing {3}".format(
            landed["repo"], landed["head"][:12], landed["landedAt"], replaced.get(FIELD_VERDICT) or "no record"))
        print(render(latest_status(repos[0])))
        return 0
    as_json = "--json" in argv
    repos = [arg for arg in argv if not arg.startswith("--")] or gated_repos()
    if "--all-checkouts" in argv:
        entries = [entry for repo in repos for entry in checkout_statuses(repo)]
    else:
        entries = [latest_status(repo) for repo in repos]
    if as_json:
        print(json.dumps(entries, indent=2))
    else:
        for entry in entries:
            print(render(entry))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
