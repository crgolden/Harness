# GATE-STEP: Gate helpers harness
# GATE-LISTING: os.listdir(root) | none
"""Harness for GateCommon.ps1's bookkeeping, the steps, carry, record, landing and run modules every repo's gate.ps1
loads. Each checker module has its own harness beside it (Test-GateExit.ps1, Test-GateTrx.ps1, Test-GateSarif.ps1,
Test-GateSonar.ps1, Test-GateNode.ps1), which loads only that module against gate-check-double.ps1, and the gate
slot has Test-GateSlot.ps1, so an edit to one of them reruns that harness and not this one.

These properties are pinned, each case in its own pwsh process as a real gate would run:
  fail-fast: a failing check ends the gate on the spot, names every declared step it never reached, and a
             passing check lets the script continue.
  delta:     a step is CARRIED only when it last PASSED on byte-identical inputs; an edit, a new untracked file,
             a previous FAIL, or an edit made while the gate ran and then reverted each force a rerun, while a
             markdown-only edit and the gate's own Stryker output do not.
  worktree:  a linked worktree starts from its main worktree's step verdicts, on the same byte-identical terms,
             its own entry winning and a failure carrying nothing.
  step-inputs: those inputs are the step's own: its declared patterns, else the whole repo, plus only the
             shared Tools scripts it reads.
  dependency: a consumer's gate stops while the workspace holds a newer @crgolden/modules or Shared than it
             pins, and fails closed when the pinned package's source is missing.
  origin:    an operator gate fast-forwards a checkout that is only behind, clean or with unstaged edits git can keep,
             on the default branch or the session's own branch, moves a clean squash-merged session branch to
             origin, before reading its inputs, and stops one with staged changes, an overwritten local file or
             real unmerged commits with the remedy; a
             triage gate compares against the refs its runner fetched, and a missing origin/HEAD, an unreachable
             origin or an unknown run kind fails closed.
Exits 1 on any mismatch.
"""
import argparse
import json
import os
import queue
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import dependency_constants
import git_constants

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Hooks"))
import claude_temp  # noqa: E402

PROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate-common-probe.ps1")
PROBE_HOST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate-common-probe-host.ps1")
HOST_END = "@@PROBE-END "
PARALLEL_PROBES = 2

FIXTURES = {
    "passed.trx": '<TestRun><ResultSummary><Counters total="1" executed="1" passed="1" failed="0" />'
                  '</ResultSummary></TestRun>',
    "failed.trx": '<TestRun><Results><UnitTestResult testName="Planted.Fails" outcome="Failed"><Output><ErrorInfo>'
                  '<Message>planted failure message</Message></ErrorInfo></Output></UnitTestResult></Results>'
                  '<ResultSummary><Counters total="1" executed="1" passed="0" failed="1" /></ResultSummary></TestRun>',
    "passed-two.trx": '<TestRun><ResultSummary><Counters total="2" executed="2" passed="2" failed="0" />'
                      '</ResultSummary></TestRun>',
}

FAIL_FAST_CASES = {
    "exit": (False, ["exit 3", "Second step", "NOT REACHED", "GATE: FAILED"], []),
    "never": (False, ["command never ran", "NOT REACHED"], []),
    "pass": (True, ["PASS"], ["GATE: FAILED"]),
    "gate-output-dir": (True, ["\\claude\\crgolden-gates\\Probe"], ["\\temp\\crgolden-gates\\Probe", "GATE: FAILED"]),
    "registry": (False, ["Third step", "NOT REACHED"], ["First step                                                   NOT REACHED"]),
    "overlapping-inputs": (True, ["PATTERNS dotnet-tools.json"], ["PATTERNS *", "GATE: FAILED"]),
}


SELECTED_CASES = {
    "selected-exact": (0, ["TESTS: PASSED (executed 1 of 1 requested"]),
    "selected-fewer": (1, ["TESTS: FAILED (executed 1 of 2 requested"]),
    "selected-more": (1, ["same-named method in another class", "TESTS: FAILED (executed 2 of 1 requested"]),
    "selected-failed": (1, ["Planted.Fails: planted failure message", "TESTS: FAILED"]),
    "selected-none": (1, ["TESTS: FAILED (no TRX (exit 8))"]),
    "selected-empty": (1, ["TESTS: FAILED (no test names given)"]),
    "selected-wildcard": (1, ["TESTS: FAILED (not a test method name: Widget_*)"]),
    "selected-no-project": (1, ["TESTS: FAILED (no test project at"]),
}


class ProbeHost:
    """One pwsh per worker: each case runs in a fresh runspace with the environment restored to the
    host's start, so a case behaves as it would in its own process at a fraction of the cost of a launch."""

    def __init__(self):
        self.process = subprocess.Popen(["pwsh", "-File", PROBE_HOST], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", bufsize=1)

    def run(self, case, root):
        self.process.stdin.write(f"{case}\t{root}\n")
        self.process.stdin.flush()
        lines = []
        for line in self.process.stdout:
            if line.startswith(HOST_END):
                return subprocess.CompletedProcess([case], int(line[len(HOST_END):]), "".join(lines), "")
            lines.append(line)
        raise RuntimeError(f"the probe host exited during {case}: {''.join(lines)[-500:]}")

    def close(self):
        self.process.stdin.close()
        self.process.wait()


HOST = []


def run_process(case, root):
    return subprocess.run(["pwsh", "-File", PROBE, "-Case", case, "-Root", root], capture_output=True, text=True)


def run(case, root):
    return HOST[0].run(case, root) if HOST else run_process(case, root)


def run_independent(cases, root):
    """Probe runs for cases that share no state, PARALLEL_PROBES at a time, each on a probe host of its own worker,
    returned in the order given."""
    hosts = queue.Queue()
    for _ in range(max(1, min(PARALLEL_PROBES, len(cases)))):
        hosts.put(ProbeHost())

    def on_host(case):
        host = hosts.get()
        try:
            return host.run(case, root)
        finally:
            hosts.put(host)

    try:
        with ThreadPoolExecutor(max_workers=PARALLEL_PROBES) as pool:
            return dict(zip(cases, pool.map(on_host, cases)))
    finally:
        while not hosts.empty():
            hosts.get().close()


def fail_fast(root, chosen=lambda case: True):
    mismatches = 0
    results = run_independent([case for case in FAIL_FAST_CASES if chosen(case)], root)
    for case, (reaches, wanted, unwanted) in FAIL_FAST_CASES.items():
        if not chosen(case):
            continue
        result = results[case]
        output = result.stdout + result.stderr
        reached = "REACHED" in [line.strip() for line in output.splitlines()]
        problems = []
        if reached != reaches:
            problems.append(f"reached={reached}, expected {reaches}")
        if (result.returncode == 0) != reaches:
            problems.append(f"exit {result.returncode}")
        problems += [f"missing {w!r}" for w in wanted if w not in output]
        problems += [f"unexpected {u!r}" for u in unwanted if u in output]
        mismatches += bool(problems)
        print(f"{'OK  ' if not problems else 'FAIL'} fail-fast: {case:<12} {'; '.join(problems)}")
    return mismatches


def selected(root, chosen=lambda case: True):
    """A tests-only run passes only when it executed exactly the tests it was asked for, and never prints a
    GATE: line, which wait scripts and gate readers take for a gate verdict."""
    mismatches = 0
    results = run_independent([case for case in SELECTED_CASES if chosen(case)], root)
    for case, (exit_code, wanted) in SELECTED_CASES.items():
        if not chosen(case):
            continue
        result = results[case]
        output = result.stdout + result.stderr
        problems = [] if result.returncode == exit_code else [f"exit {result.returncode}, expected {exit_code}"]
        problems += [f"missing {w!r}" for w in wanted if w not in output]
        problems += ["printed a GATE: line"] if "GATE:" in output else []
        mismatches += bool(problems)
        print(f"{'OK  ' if not problems else 'FAIL'} selected: {case:<20} {'; '.join(problems)}")
    return mismatches


def delta(root):
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
    subprocess.run(["git", "-C", repo, git_constants.CONFIG, git_constants.FSMONITOR, git_constants.DISABLED],
                   check=True)
    snapshots = {}

    def write(name, text):
        with open(os.path.join(repo, name), "a", encoding="utf-8") as handle:
            handle.write(text)

    def edit_and_snapshot(name, text):
        write(name, text)
        with open(os.path.join(repo, name), "rb") as handle:
            snapshots[name] = handle.read()

    def restore(name):
        with open(os.path.join(repo, name), "wb") as handle:
            handle.write(snapshots[name])

    write("code.cs", "class A {}\n")
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    steps = [
        ("first run on fresh inputs runs", lambda: None, "run-pass", "ran"),
        ("second run on identical inputs carries", lambda: None, "run-pass", "carried"),
        ("a code edit forces a rerun", lambda: write("code.cs", "class B {}\n"), "run-pass", "ran"),
        ("identical again carries", lambda: None, "run-pass", "carried"),
        ("an untracked new file forces a rerun", lambda: write("new.cs", "class C {}\n"), "run-pass", "ran"),
        ("a markdown-only edit still carries", lambda: write("README.md", "notes\n"), "run-pass", "carried"),
        ("a failing run is recorded", lambda: write("code.cs", "class D {}\n"), "run-fail", "ran"),
        ("a step that last FAILED is never carried", lambda: None, "run-fail", "ran"),
        ("a completed clean run is recorded", lambda: write("code.cs", "class E {}\n"), "run-pass-complete", "ran"),
        ("and then carries (no drift, no revoke)", lambda: None, "run-pass-complete", "carried"),
        ("a pass with an edit mid-gate is recorded", lambda: edit_and_snapshot("code.cs", "class F {}\n"),
         "run-pass-drift", "ran"),
        ("reverted to the gate's start tree, it reruns", lambda: restore("code.cs"), "run-pass-complete", "ran"),
        ("a gate writing Stryker output is recorded", lambda: write("code.cs", "class G {}\n"),
         "run-pass-writes-output", "ran"),
        ("its own output is not drift, so it carries", lambda: None, "run-pass-complete", "carried"),
    ]
    mismatches = 0
    for name, prepare, case, expected in steps:
        prepare()
        result = run(case, root)
        got = next((line.split(" ", 1)[1] for line in result.stdout.splitlines() if line.startswith("RESULT ")),
                   "no result: " + result.stdout[-300:] + result.stderr[-300:])
        ok = got == expected
        mismatches += not ok
        print(f"{'OK  ' if ok else 'FAIL'} delta: {name:<46} expected={expected} got={got}")
    return mismatches


TOOL_FILES = ("Claude/Get-CodeStyleCatalog.ps1", "CommentAudit/Get-CommentCatalog.ps1", "Shared/ScanScopeHelper.ps1",
              "Gates/GateExit.ps1", "Gates/GateRun.ps1", "Gates/GateFixtureUnused.ps1")


def step_inputs():
    """Per-step fingerprints. A step carries while its OWN inputs are unchanged: a declared step ignores edits
    outside its patterns, an undeclared step reads the whole repo, and of the shared Tools scripts every step
    reads GateCommon while only a catalog step also reads its own catalog and ScanScopeHelper. Mid-gate drift
    revokes only the verdicts whose inputs moved. Runs in its own root so its tools/ copy cannot leak."""
    root = tempfile.mkdtemp(prefix="gate-step-inputs-", dir=claude_temp.scratch_dir())
    try:
        repo = os.path.join(root, "repo")
        os.makedirs(os.path.join(repo, "src"))
        os.makedirs(os.path.join(repo, "docs"))
        subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
        subprocess.run(["git", "-C", repo, git_constants.CONFIG, git_constants.FSMONITOR, git_constants.DISABLED],
                       check=True)

        def write(*parts, text):
            path = os.path.join(root, *parts)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text)

        write("repo", "src", "code.cs", text="class A {}\n")
        write("repo", "docs", "notes.txt", text="notes\n")
        for name in TOOL_FILES:
            write("tools", *name.split("/"), text="# tool\n")

        snapshot = {}

        def write_and_snapshot_code(text):
            write("repo", "src", "code.cs", text=text)
            with open(os.path.join(repo, "src", "code.cs"), "rb") as handle:
                snapshot["code"] = handle.read()

        def restore_code():
            with open(os.path.join(repo, "src", "code.cs"), "wb") as handle:
                handle.write(snapshot["code"])

        def result(case):
            output = run(case, root)
            return next((line.split(" ", 1)[1] for line in output.stdout.splitlines() if line.startswith("RESULT ")),
                        "no result: " + output.stdout[-300:] + output.stderr[-300:])

        steps = [
            ("a declared step runs on fresh inputs", lambda: None, "run-step-inputs", "ran"),
            ("and carries on identical inputs", lambda: None, "run-step-inputs", "carried"),
            ("an edit outside its patterns still carries", lambda: write("repo", "docs", "notes.txt", text="more\n"),
             "run-step-inputs", "carried"),
            ("an edit inside its patterns reruns it", lambda: write("repo", "src", "code.cs", text="class B {}\n"),
             "run-step-inputs", "ran"),
            ("drift outside its inputs is recorded", lambda: write("repo", "src", "code.cs", text="class C {}\n"),
             "run-step-inputs-drift-outside", "ran"),
            ("and does not revoke its verdict", lambda: None, "run-step-inputs", "carried"),
            ("drift inside its inputs is recorded", lambda: write_and_snapshot_code("class D {}\n"),
             "run-step-inputs-drift-inside", "ran"),
            ("reverted to the gate's start tree, the revoked verdict reruns", restore_code, "run-step-inputs", "ran"),
            ("both steps run on fresh tools", lambda: None, "run-tools", "first=ran catalog=ran"),
            ("and carry on identical tools", lambda: None, "run-tools", "first=carried catalog=carried"),
            ("a code-style catalog edit reruns only its catalog step",
             lambda: write("tools", "Claude", "Get-CodeStyleCatalog.ps1", text="# edit\n"),
             "run-tools", "first=carried catalog=ran"),
            ("a comment catalog edit reruns neither of them",
             lambda: write("tools", "CommentAudit", "Get-CommentCatalog.ps1", text="# edit\n"),
             "run-tools", "first=carried catalog=carried"),
            ("a ScanScopeHelper edit reruns only the catalog step",
             lambda: write("tools", "Shared", "ScanScopeHelper.ps1", text="# edit\n"),
             "run-tools", "first=carried catalog=ran"),
            ("must-block: a bookkeeping edit (GateRun.ps1) reruns neither, its bookkeeping decides no verdict",
             lambda: write("tools", "Gates", "GateRun.ps1", text="# edit\n"), "run-tools", "first=carried catalog=carried"),
            ("must-block: an edit to a module neither step called reruns neither",
             lambda: write("tools", "Gates", "GateFixtureUnused.ps1", text="# edit\n"), "run-tools", "first=carried catalog=carried"),
            ("control: an edit to the module both steps called (Test-Exit) reruns both",
             lambda: write("tools", "Gates", "GateExit.ps1", text="# edit\n"), "run-tools", "first=ran catalog=ran"),
            ("a repo edit reruns both", lambda: write("repo", "docs", "notes.txt", text="again\n"),
             "run-tools", "first=ran catalog=ran"),
            ("a step passes in one checkout under the account's gate records", lambda: None, "sibling-first", "ran"),
            ("must-pass: another checkout of the repo with the same inputs carries that pass",
             lambda: None, "sibling-copy", "carried"),
            ("control: that checkout with its own edit to the step's inputs runs it",
             lambda: None, "sibling-edited", "ran"),
            ("must-pass: reverted to the other checkout's inputs, its own pass on other inputs gives way to that pass",
             lambda: None, "sibling-reverted", "carried"),
            ("that checkout's step then fails on those inputs", lambda: None, "sibling-fail", "no result"),
            ("must-block: its own failure outvotes the other checkout's pass on the same inputs",
             lambda: None, "sibling-reverted", "ran"),
        ]
        mismatches = 0
        for name, prepare, case, expected in steps:
            prepare()
            got = result(case)
            ok = got.startswith(expected) if expected == "no result" else got == expected
            mismatches += not ok
            print(f"{'OK  ' if ok else 'FAIL'} step-inputs: {name:<54} expected={expected} got={got}")
        refused = "gate state never goes under a .claude folder"
        placements = [
            ("must-block: gate state in a .claude folder in the checkout is refused", "state-dir-checkout-claude",
             lambda got: got.startswith(refused)),
            ("must-block: gate state in the workspace's .claude folder is refused", "state-dir-workspace-claude",
             lambda got: got.startswith(refused)),
            ("must-block: a triage run's gate state in a .claude folder is refused too", "state-dir-triage-claude",
             lambda got: got.startswith(refused)),
            ("control: a triage run's default, the gates folder beside its worktrees, is accepted",
             "state-dir-triage-default", lambda got: got == "none"),
            ("control: the default, the checkout's folder under the temp root's gate records, is accepted",
             "state-dir-default", lambda got: got == "none"),
            ("control: a folder whose name only contains .claude is not a .claude folder", "state-dir-claudeish",
             lambda got: got == "none"),
        ]
        for name, case, accept in placements:
            got = result(case)
            ok = accept(got)
            mismatches += not ok
            print(f"{'OK  ' if ok else 'FAIL'} state-placement: {name:<54} got={got}")
        triage_default = run("state-dir-triage-default", root).stdout
        ok = "STATE triage-beside-worktrees=True" in triage_default
        mismatches += not ok
        print(f"{'OK  ' if ok else 'FAIL'} state-placement: {'must-pass: a triage run defaults to the gates folder beside its worktrees, outside .claude':<54} "
              f"got={triage_default.strip()[-200:]}")
        declarations = [
            ("every step declared, every pattern live", "inputs-complete", 0,
             ["RESULT initialized", "Step inputs declared", "GATE: PASSED"], ["GATE: FAILED"]),
            ("a registered step with no inputs fails", "inputs-undeclared", 1,
             ["'Second step' declares no inputs", "GATE: FAILED"], ["RESULT initialized"]),
            ("a pattern matching no file fails", "inputs-dead-pattern", 1,
             ["'First step' input 'absent/*' matches no file", "GATE: FAILED"], ["RESULT initialized"]),
            ("inputs for an unregistered step fail", "inputs-unregistered", 1,
             ["'Frist step' declares inputs but is not a registered step", "GATE: FAILED"], ["RESULT initialized"]),
            ("must-block: a volatile name matching no registered step fails, so a renamed audit step cannot carry",
             "inputs-volatile-unregistered", 1,
             ["'Frist step' is declared volatile but is not a registered step", "GATE: FAILED"], ["RESULT initialized"]),
            ("control: a volatile name matching a registered step passes the declaration check", "inputs-volatile-registered", 0,
             ["RESULT initialized", "GATE: PASSED"], ["GATE: FAILED"]),
        ]
        requested = [
            ("no -Steps fails before any step", lambda: None, "steps-none", 1,
             ["no -Steps given", "GATE: FAILED"], ["STEP "]),
            ("a step the gate does not have fails", lambda: None, "steps-unknown", 1,
             ["'Third step' is not a step of this gate", "GATE: FAILED"], ["STEP "]),
            ("a first run naming every step runs them", lambda: None, "steps-both", 0,
             ["STEP First step ran", "STEP Second step ran", "GATE: PASSED"], ["GATE: FAILED"]),
            ("naming steps that would return the same green fails", lambda: None, "steps-both", 1,
             ["'First step' does not qualify", "'Second step' does not qualify", "GATE: FAILED"], ["STEP "]),
            ("leaving out an edited step fails", lambda: write("repo", "src", "code.cs", text="class S {}\n"),
             "steps-second", 1, ["'First step' must be passed", "'Second step' does not qualify", "GATE: FAILED"],
             ["STEP "]),
            ("naming exactly the edited step runs it and carries the other", lambda: None, "steps-first", 0,
             ["STEP First step ran", "STEP Second step carried", "GATE: PASSED"], ["GATE: FAILED"]),
            ("owed: a first run owes and runs every step", lambda: None, "steps-owed", 0,
             ["Owed steps: Current with origin, Dependency order, First step, Second step", "STEP First step ran",
              "STEP Second step ran", "GATE: PASSED"],
             ["GATE: FAILED", "carried"]),
            ("owed: an unedited rerun owes only the always-run steps and carries every other",
             lambda: None, "steps-owed", 0,
             ["Owed steps: Current with origin, Dependency order\n", "STEP First step carried",
              "STEP Second step carried", "GATE: PASSED"],
             ["GATE: FAILED", " ran"]),
            ("must-block owed: an edit to one step's input owes and runs only that step",
             lambda: write("repo", "docs", "notes.txt", text="owed\n"), "steps-owed", 0,
             ["Owed steps: Current with origin, Dependency order, Second step\n", "STEP First step carried",
              "STEP Second step ran", "GATE: PASSED"],
             ["First step ran", "GATE: FAILED"]),
            ("must-block owed: a bookkeeping edit (GateRun.ps1) owes no step but the always-run ones",
             lambda: write("tools", "Gates", "GateRun.ps1", text="# owed\n"), "steps-owed", 0,
             ["Owed steps: Current with origin, Dependency order\n", "STEP First step carried",
              "STEP Second step carried", "GATE: PASSED"],
             [" ran", "GATE: FAILED"]),
            ("control owed: an edit to the module every step called owes and runs every step",
             lambda: write("tools", "Gates", "GateExit.ps1", text="# owed\n"), "steps-owed", 0,
             ["Owed steps: Current with origin, Dependency order, First step, Second step", "STEP First step ran",
              "STEP Second step ran", "GATE: PASSED"],
             ["carried", "GATE: FAILED"]),
        ]
        for name, prepare, case, exit_code, wanted, unwanted in requested:
            prepare()
            output = run(case, root)
            text = output.stdout + output.stderr
            problems = [] if output.returncode == exit_code else [f"exit {output.returncode}, expected {exit_code}"]
            problems += [f"missing {w!r}" for w in wanted if w not in text]
            problems += [f"unexpected {u!r}" for u in unwanted if u in text]
            mismatches += bool(problems)
            print(f"{'OK  ' if not problems else 'FAIL'} requested-steps: {name:<54} {'; '.join(problems)}")
        probe_state = os.path.join(root, "state-probe")
        machine = os.environ.get("COMPUTERNAME", "")
        probe_files = [os.path.join(probe_state, "repo.{0}.json".format(machine)),
                       os.path.join(probe_state, "repo.{0}.last.json".format(machine))]

        def recorded():
            return [path for path in probe_files if os.path.exists(path)]

        def snapshot():
            found = {}
            for path in recorded():
                with open(path, "rb") as handle:
                    found[path] = handle.read()
            return found

        probes = [
            ("must-block: a probe runs only its named step and writes no gate record, verdict or carry", "probe-first", 0,
             ["STEP First step ran", "STEP Second step skipped", "PROBE: PASSED"], ["GATE:"], lambda: not recorded()),
            ("must-block: a failing probe still writes no record and prints no GATE: line", "probe-fail", 1,
             ["STEP First step skipped", "PROBE: FAILED"], ["GATE:"], lambda: not recorded()),
            ("must-block: a probe naming a step the gate does not have fails before any step", "probe-unknown", 1,
             ["PROBE: FAILED", "'Third step'"], ["STEP "], lambda: not recorded()),
            ("control: the same fixture run as a gate writes the record, so the probe rows discriminate", "probe-gate", 0,
             ["STEP First step ran", "GATE: PASSED"], ["PROBE:"], lambda: len(recorded()) == 2),
        ]
        for name, case, exit_code, wanted, unwanted, files_ok in probes:
            output = run(case, root)
            text = output.stdout + output.stderr
            problems = [] if output.returncode == exit_code else [f"exit {output.returncode}, expected {exit_code}"]
            problems += [f"missing {w!r}" for w in wanted if w not in text]
            problems += [f"unexpected {u!r}" for u in unwanted if u in text]
            problems += [] if files_ok() else [f"record files {recorded()}"]
            mismatches += bool(problems)
            print(f"{'OK  ' if not problems else 'FAIL'} probe: {name:<54} {'; '.join(problems)}")
        before = snapshot()
        output = run("probe-first", root)
        unchanged = output.returncode == 0 and len(before) == 2 and snapshot() == before
        mismatches += not unchanged
        print(f"{'OK  ' if unchanged else 'FAIL'} probe: {'must-block: a probe after a passing gate leaves its record and verdicts byte for byte':<54} "
              f"exit {output.returncode}, {len(before)} file(s)")
        for name, case, exit_code, wanted, unwanted in declarations:
            output = run(case, root)
            text = output.stdout + output.stderr
            problems = [] if output.returncode == exit_code else [f"exit {output.returncode}, expected {exit_code}"]
            problems += [f"missing {w!r}" for w in wanted if w not in text]
            problems += [f"unexpected {u!r}" for u in unwanted if u in text]
            mismatches += bool(problems)
            print(f"{'OK  ' if not problems else 'FAIL'} step-inputs: {name:<54} {'; '.join(problems)}")
        return mismatches
    finally:
        shutil.rmtree(root, onexc=clear_readonly_and_retry)


def step_order():
    """A gate runs the steps whose declared inputs the session's change touches first, then the rest in their
    registered order, so a fail-fast on an untouched step leaves the session's own steps judged. The change is what
    HEAD holds beyond origin's default branch plus every dirty file. The operator, on a branch that failed at an
    inherited red step with its own steps never reached: a fix that does not pass the trap to a future session."""
    root = tempfile.mkdtemp(prefix="gate-step-order-", dir=claude_temp.scratch_dir())
    try:
        repo = os.path.join(root, "repo")
        os.makedirs(os.path.join(repo, "src"))
        os.makedirs(os.path.join(repo, "docs"))
        git("init", "-q", repo)
        git("-C", repo, git_constants.CONFIG, git_constants.FSMONITOR, git_constants.DISABLED)
        commit_file(repo, os.path.join("src", "code.cs"), "class A {}\n")
        commit_file(repo, os.path.join("docs", "notes.txt"), "notes\n")

        def result(case):
            output = run_process(case, root)
            return next((line.split(" ", 1)[1].strip() for line in output.stdout.splitlines() if line.startswith("RESULT ")),
                        "no result: " + output.stdout[-300:] + output.stderr[-300:])

        def dirty_docs():
            with open(os.path.join(repo, "docs", "notes.txt"), "a", encoding="utf-8") as handle:
                handle.write("more\n")

        def publish_then_commit_docs():
            give_origin(root, repo)
            commit_file(repo, os.path.join("docs", "notes.txt"), "committed beyond origin\n")

        rows = [
            ("a step whose inputs the change touches runs before an untouched one", lambda: None, "order-touched-docs",
             "Second step|First step"),
            ("control: a change touching every step keeps the registered order", lambda: None, "order-touched-both",
             "First step|Second step"),
            ("control: no change keeps the registered order", lambda: None, "order-touched-none",
             "First step|Second step"),
            ("control: a clean tree with no origin keeps the registered order", lambda: None, "order-from-git",
             "First step|Second step"),
            ("a dirty file moves its step first", dirty_docs, "order-from-git", "Second step|First step"),
            ("a commit beyond origin's default branch moves its step first, tree clean", publish_then_commit_docs,
             "order-from-git", "Second step|First step"),
        ]
        mismatches = 0
        for name, arrange, case, wanted in rows:
            arrange()
            got = result(case)
            ok = got == wanted
            mismatches += not ok
            print(f"{'OK  ' if ok else 'FAIL'} step-order: {name}: {got}")
        return mismatches
    finally:
        shutil.rmtree(root, onerror=clear_readonly_and_retry)


def worktrees():
    """A linked worktree starts from its main worktree's step verdicts: a step main passed on byte-identical inputs
    carries, an edited one is owed, the worktree's own entry wins over main's, a step main failed carries nothing,
    and a triage run (GATE_RUN_KIND=triage) borrows nothing, since main's passes ran in the operator's environment. Runs in its own root, since a worktree needs a commit and its own directory beside the main one."""
    root = tempfile.mkdtemp(prefix="gate-worktree-", dir=claude_temp.scratch_dir())
    try:
        main = os.path.join(root, "ws", "repo")
        os.makedirs(main)
        git = ["git", "-c", "user.name=probe", "-c", "user.email=probe@example.invalid"]
        subprocess.run(git + ["-C", main, "init", "-q"], check=True)
        subprocess.run(["git", "-C", main, git_constants.CONFIG, git_constants.FSMONITOR, git_constants.DISABLED],
                       check=True)
        with open(os.path.join(main, "code.cs"), "w", encoding="utf-8") as handle:
            handle.write("class A {}\n")
        subprocess.run(git + ["-C", main, "add", "."], check=True)
        subprocess.run(git + ["-C", main, "commit", "-q", "-m", "base"], check=True)

        def leave_uncommitted(checkout):
            with open(os.path.join(checkout, "pending.txt"), "w", encoding="utf-8") as handle:
                handle.write("uncommitted, so a gate runs\n")

        leave_uncommitted(main)

        def add_worktree(name):
            path = os.path.join(root, name, "repo")
            subprocess.run(git + ["-C", main, "worktree", "add", "-q", "-b", name, path], check=True,
                           capture_output=True)
            leave_uncommitted(path)
            return os.path.join(root, name)

        def edit(base, text):
            with open(os.path.join(base, "repo", "code.cs"), "a", encoding="utf-8") as handle:
                handle.write(text)

        def revert(base):
            subprocess.run(["git", "-C", os.path.join(base, "repo"), "checkout", "--", "code.cs"], check=True)

        def result(case, base):
            output = run(case, base)
            return next((line.split(" ", 1)[1] for line in output.stdout.splitlines() if line.startswith("RESULT ")),
                        "no result")

        workspace = os.path.join(root, "ws")
        first = add_worktree("first")
        steps = [
            ("main runs its step on fresh inputs", lambda: None, "worktree-pass", workspace, "ran"),
            ("must-carry: a new worktree carries main's pass on identical inputs", lambda: None, "worktree-pass", first,
             "carried"),
            ("must-block: an edit in the worktree owes the step again", lambda: edit(first, "class B {}\n"),
             "worktree-pass", first, "ran"),
            ("the worktree's own failure is recorded", lambda: edit(first, "class C {}\n"), "worktree-fail", first,
             "no result"),
            ("must-block: the worktree's own failure wins over main's pass on the same inputs", lambda: revert(first),
             "worktree-pass", first, "ran"),
            ("main's failure is recorded", lambda: None, "worktree-fail", workspace, "no result"),
        ]
        mismatches = 0
        for name, prepare, case, base, expected in steps:
            prepare()
            got = result(case, base)
            ok = got == expected
            mismatches += not ok
            print(f"{'OK  ' if ok else 'FAIL'} worktree: {name:<72} expected={expected} got={got}")
        second = add_worktree("second")
        got = result("worktree-pass", second)
        mismatches += got != "ran"
        print(f"{'OK  ' if got == 'ran' else 'FAIL'} worktree: {'must-block: a step main last failed carries nothing':<72} "
              f"expected=ran got={got}")
        result("worktree-pass", workspace)
        for name, case, expected in [
            ("must-block: a triage run borrows none of main's passes, even on identical inputs", "worktree-triage-pass",
             "ran"),
            ("control: the same fresh worktree outside triage carries main's pass", "worktree-pass", "carried"),
        ]:
            got = result(case, add_worktree(case.replace("worktree-", "fresh-")))
            mismatches += got != expected
            print(f"{'OK  ' if got == expected else 'FAIL'} worktree: {name:<72} expected={expected} got={got}")
        return mismatches
    finally:
        shutil.rmtree(root, onexc=clear_readonly_and_retry)


def records(root):
    """The run record GateCommon writes and gate_status.py reads, against the repo delta() left behind. The
    fingerprint the reader recomputes from the record's own manifest must equal the one PowerShell wrote,
    which is what keeps the two implementations from drifting apart."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gate_status

    repo = os.path.join(root, "repo")
    state = os.path.join(root, "state")

    def edit(text):
        with open(os.path.join(repo, "code.cs"), "a", encoding="utf-8") as handle:
            handle.write(text)

    def record():
        return gate_status.read_record("repo", state)

    def verdict():
        return record().get(gate_status.FIELD_VERDICT)

    def state_now():
        return gate_status.status("repo", workspace=root, state_dir=state)

    checks = []
    edit("class R1 {}\n")
    run("run-pass-complete", root)
    written = record()
    checks.append(("a completed passing gate records PASSED",
                   written.get(gate_status.FIELD_VERDICT) == gate_status.VERDICT_PASSED))
    checks.append(("the reader recomputes the writer's fingerprint",
                   gate_status.recorded_fingerprint(written) == written.get(gate_status.FIELD_FINGERPRINT)))
    checks.append(("the manifest holds the tracked source file",
                   "code.cs" in written.get(gate_status.FIELD_MANIFEST, {})))
    checks.append(("an unchanged tree reads GREEN-CURRENT",
                   state_now().get(gate_status.STATUS_STATE) == gate_status.GREEN_CURRENT))
    completed = datetime.fromisoformat(written["completedAt"].replace("Z", "+00:00"))
    past_max = gate_status.VOLATILE_MAX_AGE + timedelta(minutes=random.randint(1, 600))
    within_max = gate_status.VOLATILE_MAX_AGE - timedelta(minutes=random.randint(1, 600))
    with open(gate_status.record_path("repo", state), "w", encoding=gate_status.RECORD_ENCODING) as handle:
        json.dump({**written, gate_status.FIELD_VOLATILE: ["Audit step"]}, handle)
    checks.append(("must-block: a volatile step older than its max age reads GREEN-STALE-AUDIT, never GREEN-CURRENT",
                   gate_status.status("repo", workspace=root, state_dir=state, now=completed + past_max)
                   .get(gate_status.STATUS_STATE) == gate_status.GREEN_STALE_AUDIT))
    checks.append(("control: a volatile step within its max age stays GREEN-CURRENT",
                   gate_status.status("repo", workspace=root, state_dir=state, now=completed + within_max)
                   .get(gate_status.STATUS_STATE) == gate_status.GREEN_CURRENT))
    with open(gate_status.record_path("repo", state), "w", encoding=gate_status.RECORD_ENCODING) as handle:
        json.dump(written, handle)
    checks.append(("control: a record with no volatile step never decays with age",
                   gate_status.status("repo", workspace=root, state_dir=state, now=completed + past_max)
                   .get(gate_status.STATUS_STATE) == gate_status.GREEN_CURRENT))
    checks.append(("the record names the Tools checkout its GateCommon hashed and the repo it gated",
                   os.path.normcase(written.get("toolsRoot", "")) == os.path.normcase(TOOLS_ROOT)
                   and os.path.normcase(written.get("repoPath", "")) == os.path.normcase(repo)))
    elsewhere = os.path.join(root, "elsewhere-tools")
    for name in written.get(gate_status.FIELD_TOOLS, {}):
        os.makedirs(os.path.dirname(os.path.join(elsewhere, *name.split("/"))), exist_ok=True)
        with open(os.path.join(elsewhere, *name.split("/")), "w", encoding="utf-8") as handle:
            handle.write(name + "\n")
    with open(gate_status.record_path("repo", state), "w", encoding=gate_status.RECORD_ENCODING) as handle:
        json.dump({**written, "toolsRoot": elsewhere}, handle)
    checks.append(("must-block: the reader hashes the Tools checkout the record names, so other tool files read GREEN-STALE",
                   state_now().get(gate_status.STATUS_STATE) == gate_status.GREEN_STALE))
    with open(gate_status.record_path("repo", state), "w", encoding=gate_status.RECORD_ENCODING) as handle:
        json.dump({**written, "repoPath": os.path.join(root, "removed-worktree")}, handle)
    checks.append(("control: a recorded repo path that no longer exists falls back to the workspace's repo",
                   state_now().get(gate_status.STATUS_STATE) == gate_status.GREEN_CURRENT))
    with open(gate_status.record_path("repo", state), "w", encoding=gate_status.RECORD_ENCODING) as handle:
        json.dump(written, handle)
    deep_state = os.path.join(root, "d" * max(1, 240 - len(root) - 1))
    deep_record = os.path.join(deep_state, "repo.{0}{1}".format(gate_status.this_machine(), gate_status.RECORD_SUFFIX))
    gate_status.write_record(gate_status.record_path("repo", deep_state), written)
    checks.append(("must-pass: a record whose path passes MAX_PATH ({0} characters) is written and read back".format(
                       len(deep_record)),
                   len(deep_record) >= 260 and gate_status.read_record("repo", deep_state) == written))
    planted_gate = os.path.join(root, "markdown-gate")
    os.makedirs(planted_gate, exist_ok=True)
    with open(os.path.join(planted_gate, "gate.ps1"), "w", encoding="utf-8") as handle:
        handle.write("Register-StepInputs @{\n    'Doc harness' = @('Lib/*', 'AGENTS/TOOLING.md')\n"
                     "    'Glob harness' = @('Docs/*.md')\n}\n$note = 'README.md'\n"
                     "Register-StepInputs @{\n    'Late harness' = @('AGENTS/LATE.md')\n}\n")
    declared = gate_status.declared_markdown(planted_gate)
    checks.append(("must-pass: a markdown file a step names as an input is declared, in every Register-StepInputs "
                   "block, while a wildcard and a path outside them are not",
                   declared == {"AGENTS/TOOLING.md", "AGENTS/LATE.md"}))
    checks.append(("must-pass: a declared markdown input is fingerprinted",
                   gate_status.fingerprinted("AGENTS/TOOLING.md", declared)))
    checks.append(("must-block: an undeclared markdown file stays out of the fingerprint",
                   not gate_status.fingerprinted("AGENTS/OTHER.md", declared)))
    checks.append(("control: a code file is fingerprinted with no declaration",
                   gate_status.fingerprinted("Lib/tool.py", set())))
    tools_declared = gate_status.declared_markdown(TOOLS_ROOT)
    checks.append(("must-pass: Tools' gate.ps1 declares the docs its harnesses parse",
                   {"AGENTS/TOOLING.md", "AGENTS/CODE-STYLE.md", "AGENTS/GATES.md"} <= tools_declared))
    checks.append(("control: a record path short of the threshold keeps its own spelling",
                   len(root) < 150 and gate_status.record_path("repo", root) == os.path.join(
                       root, "repo.{0}{1}".format(gate_status.this_machine(), gate_status.RECORD_SUFFIX))))
    records_root = os.path.normcase(claude_temp.gate_records_root())
    for name in ("Tools", "repo"):
        folder = os.path.normcase(gate_status.repo_state_dir(name, root))
        checks.append((f"must-block: {name}'s state lives in its own folder under the temp root's gate records, "
                       "outside every .claude folder",
                       os.path.dirname(folder) == records_root and os.sep + ".claude" + os.sep not in folder + os.sep))
    checks.append(("control: two checkouts of one repo keep their records in different folders",
                   gate_status.checkout_state_dir("repo", os.path.join(root, "repo"))
                   != gate_status.checkout_state_dir("repo", os.path.join(root, "elsewhere", "repo"))))
    probe_default = next((line.split(" ", 1)[1] for line in run("state-dir-default", root).stdout.splitlines()
                          if line.startswith("DEFAULT ")), "")
    checks.append(("GateCommon's default folder for a checkout is the one gate_status.py reads",
                   os.path.normcase(probe_default) == os.path.normcase(gate_status.checkout_state_dir("repo", repo))))
    fleet = os.path.join(root, "checkouts-fixture")
    main_checkout = os.path.join(fleet, "App")
    branch = os.path.join(root, "elsewhere", "app-branch")
    os.makedirs(branch)
    for name, target in (("live", os.path.join(branch, ".git")),
                         ("gone", os.path.join(root, "elsewhere", "removed", ".git"))):
        os.makedirs(os.path.join(main_checkout, ".git", "worktrees", name))
        with open(os.path.join(main_checkout, ".git", "worktrees", name, "gitdir"), "w", encoding="utf-8") as handle:
            handle.write(target + "\n")
    found = [os.path.normcase(path) for path in gate_status.checkouts("App", fleet)]
    checks.append(("a repo's checkouts are its main checkout, then each worktree git registered that still exists",
                   found == [os.path.normcase(main_checkout), os.path.normcase(branch)]))
    for checkout, completed in ((main_checkout, "2026-01-01T00:00:00Z"), (branch, "2026-01-02T00:00:00Z")):
        state_dir = gate_status.checkout_state_dir("App", checkout)
        os.makedirs(state_dir, exist_ok=True)
        with open(gate_status.record_path("App", state_dir), "w", encoding=gate_status.RECORD_ENCODING) as handle:
            json.dump({"repo": "App", "verdict": gate_status.VERDICT_PASSED, "completedAt": completed,
                       "repoPath": checkout}, handle)
    newest, newest_record = gate_status.latest_checkout("App", fleet)
    checks.append(("must-block: the newest record among the checkouts wins, so a branch worktree's gate is the one read",
                   os.path.normcase(newest) == os.path.normcase(branch)
                   and newest_record.get("completedAt") == "2026-01-02T00:00:00Z"))
    undated = os.path.join(root, "undated-fixture")
    os.makedirs(os.path.join(undated, "App"))
    undated_state = gate_status.checkout_state_dir("App", os.path.join(undated, "App"))
    os.makedirs(undated_state)
    with open(gate_status.record_path("App", undated_state), "w",
              encoding=gate_status.RECORD_ENCODING) as handle:
        json.dump({"repo": "App", "verdict": gate_status.VERDICT_PASSED}, handle)
    checks.append(("must-block: a record carrying no timestamp is still the repo's record, never dropped as none",
                   (gate_status.latest_checkout("App", undated)[1] or {}).get("verdict") == gate_status.VERDICT_PASSED))
    checks.append(("control: a repo no checkout has gated reads as its main checkout with no record",
                   gate_status.latest_checkout("Absent", fleet) == (os.path.join(fleet, "Absent"), None)))
    for checkout, completed, checkout_verdict in ((main_checkout, "2026-01-01T00:00:00Z", gate_status.VERDICT_FAILED),
                                                  (branch, "2026-01-02T00:00:00Z", gate_status.VERDICT_RUNNING)):
        with open(gate_status.record_path("App", gate_status.checkout_state_dir("App", checkout)), "w",
                  encoding=gate_status.RECORD_ENCODING) as handle:
            json.dump({"repo": "App", "verdict": checkout_verdict, "completedAt": completed, "repoPath": checkout},
                      handle)
    every = gate_status.checkout_statuses("App", fleet)
    checks.append(("must-block: --all-checkouts lists every checkout's own record, so a newer run elsewhere hides none",
                   [os.path.normcase(entry["checkout"]) for entry in every]
                   == [os.path.normcase(branch), os.path.normcase(main_checkout)]
                   and [entry["state"] for entry in every] == [gate_status.RUNNING, gate_status.RED]))
    checks.append(("control: a repo with no checkout lists nothing",
                   gate_status.checkout_statuses("Absent", fleet) == []))
    with open(gate_status.record_path("App", gate_status.checkout_state_dir("App", branch)), "w",
              encoding=gate_status.RECORD_ENCODING) as handle:
        json.dump({"repo": "App", "verdict": gate_status.VERDICT_PASSED, "completedAt": "2026-01-02T00:00:00Z",
                   "repoPath": branch}, handle)
    broken = {os.path.normcase(entry["checkout"]): entry for entry in gate_status.checkout_statuses("App", fleet)}
    checks.append(("must-block: a passed record in a checkout git cannot read is listed as unreadable, never a crash",
                   broken.get(os.path.normcase(branch), {}).get(gate_status.STATUS_STATE) == gate_status.UNREADABLE
                   and broken.get(os.path.normcase(main_checkout), {}).get(gate_status.STATUS_STATE) == gate_status.RED))
    checks.append(("the writer names its record for this machine",
                   os.path.isfile(gate_status.record_path("repo", state))
                   and gate_status.this_machine() in os.path.basename(gate_status.record_path("repo", state))))
    foreign = gate_status.record_path("repo", state, "ELSEWHERE")
    with open(foreign, "w", encoding=gate_status.RECORD_ENCODING) as handle:
        json.dump({**written, "machine": "ELSEWHERE", gate_status.FIELD_VERDICT: gate_status.VERDICT_FAILED}, handle)
    checks.append(("must-block: another machine's record never replaces this machine's",
                   verdict() == gate_status.VERDICT_PASSED
                   and state_now().get(gate_status.STATUS_STATE) == gate_status.GREEN_CURRENT))
    os.remove(foreign)
    shared_name = os.path.join(state, "repo" + gate_status.RECORD_SUFFIX)
    with open(shared_name, "w", encoding=gate_status.RECORD_ENCODING) as handle:
        json.dump({**written, "machine": "THIRD"}, handle)
    checks.append(("must-block: a record whose name carries no machine is never read, even one naming this machine",
                   gate_status.read_record("repo", state, machine="THIRD") is None))
    os.remove(shared_name)
    saved_machine = os.environ.get("COMPUTERNAME")
    os.environ["COMPUTERNAME"] = ""
    try:
        gate_status.record_path("repo", state)
        refused = False
    except RuntimeError:
        refused = True
    finally:
        if saved_machine is None:
            os.environ.pop("COMPUTERNAME", None)
        else:
            os.environ["COMPUTERNAME"] = saved_machine
    checks.append(("must-block: an unset COMPUTERNAME names no shared record", refused))
    run("run-pass-complete", root)
    checks.append(("a carried step is recorded as CARRIED",
                   gate_status.step_verdicts(record()).get("First step") == gate_status.STEP_CARRIED))
    run("run-volatile-complete", root)
    checks.append(("must-block: a volatile step on unchanged inputs runs again instead of carrying",
                   gate_status.step_verdicts(record()).get("First step") == gate_status.STEP_PASS))
    checks.append(("the record lists its volatile steps", record().get(gate_status.FIELD_VOLATILE) == ["First step"]))
    edit("class R2 {}\n")
    stale = state_now()
    checks.append(("an edit after the pass reads GREEN-STALE naming the file",
                   stale.get(gate_status.STATUS_STATE) == gate_status.GREEN_STALE
                   and "code.cs" in stale.get(gate_status.STATUS_CHANGED, [])))
    run("run-fail", root)
    failed = record()
    checks.append(("a failing gate records FAILED",
                   failed.get(gate_status.FIELD_VERDICT) == gate_status.VERDICT_FAILED))
    checks.append(("the steps it never reached are recorded",
                   gate_status.step_verdicts(failed).get("Second step") == "NOT REACHED"))
    checks.append(("a failed record reads RED", state_now().get(gate_status.STATUS_STATE) == gate_status.RED))
    edit("class R3 {}\n")
    run("run-pass", root)
    checks.append(("a gate that never completes leaves RUNNING", verdict() == gate_status.VERDICT_RUNNING))
    edit("class R4 {}\n")
    run("run-pass-drift", root)
    checks.append(("an edit while the gate ran records DRIFTED", verdict() == gate_status.VERDICT_DRIFTED))
    for name, ok in checks:
        print(f"{'OK  ' if ok else 'FAIL'} record: {name}")
    return sum(not ok for _, ok in checks)


GIT_IDENTITY = ["-c", "user.name=gate-probe", "-c", "user.email=gate-probe@example.invalid"]


def git(*args):
    return subprocess.run(["git", *GIT_IDENTITY, *args], check=True, capture_output=True, text=True).stdout.strip()


def new_clone(upstream, path):
    git("clone", "-q", upstream, path)
    git("-C", path, git_constants.CONFIG, git_constants.FSMONITOR, git_constants.DISABLED)


def commit_file(repo, name, text):
    with open(os.path.join(repo, name), "a", encoding="utf-8") as handle:
        handle.write(text)
    git("-C", repo, "add", name)
    git("-C", repo, "commit", "-q", "-m", name)


def give_origin(root, repo):
    """Commits the shared harness repo and publishes it to a bare origin, so a step that reads origin sees a
    checkout exactly as current as its remote."""
    upstream = os.path.join(root, "catalog-origin.git")
    git("-C", repo, "commit", "-q", "--allow-empty", "-m", "harness baseline")
    branch = git("-C", repo, "symbolic-ref", "--short", "HEAD")
    git("init", "--bare", "-q", "-b", branch, upstream)
    git("-C", repo, "remote", "add", "origin", upstream)
    git("-C", repo, "push", "-q", "origin", branch)
    git("-C", repo, "remote", "set-head", "origin", branch)


def origin():
    """Invoke-OriginCurrencyStep fails an operator gate whose checkout is behind its origin's default branch,
    which it fetches first, and a triage gate behind the refs its runner fetched, which it never fetches. The
    default branch is trunk so a step that assumed main or master cannot pass. Must-block rows come first."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gate_status

    root = tempfile.mkdtemp(prefix="gate-origin-", dir=claude_temp.scratch_dir())
    try:
        upstream = os.path.join(root, "upstream.git")
        seed = os.path.join(root, "seed")
        repo = os.path.join(root, "repo")
        other = os.path.join(root, "other")
        state = os.path.join(root, "state")
        git("init", "--bare", "-q", "-b", "trunk", upstream)
        git("init", "-q", "-b", "trunk", seed)
        git("-C", seed, git_constants.CONFIG, git_constants.FSMONITOR, git_constants.DISABLED)
        commit_file(seed, "code.cs", "class Seed {}\n")
        git("-C", seed, "remote", "add", "origin", upstream)
        git("-C", seed, "push", "-q", "origin", "trunk")
        new_clone(upstream, repo)
        new_clone(upstream, other)
        with open(os.path.join(repo, "pending.txt"), "w", encoding="utf-8") as handle:
            handle.write("uncommitted, so a gate runs\n")

        def gate(case):
            result = run(case, root)
            step = gate_status.step_verdicts(gate_status.read_record("repo", state)).get("Current with origin")
            return step, result.stdout + result.stderr

        checks = []
        commit_file(other, "upstream.cs", "class Upstream {}\n")
        git("-C", other, "push", "-q", "origin", "trunk")
        step, output = gate("run-origin-triage")
        checks.append(("a triage gate never fetches, so an upstream commit its runner has not fetched passes",
                       step == gate_status.STEP_PASS and "against the refs the triage runner fetched" in output))
        with open(os.path.join(repo, "code.cs"), "a", encoding="utf-8") as handle:
            handle.write("// uncommitted\n")
        git("-C", repo, "add", "code.cs")
        step, output = gate("run-origin")
        checks.append(("must-block: a checkout with staged changes behind origin is never moved, only told the remedy",
                       step == "FAIL" and "1 commit(s) behind origin/trunk" in output and "merge --ff-only origin/trunk" in output
                       and "NOT REACHED" in output and git("-C", repo, "rev-list", "--count", "HEAD..origin/trunk") == "1"))
        git("-C", repo, "reset", "-q", "--hard", "HEAD")
        with open(os.path.join(repo, "upstream.cs"), "w", encoding="utf-8") as handle:
            handle.write("class Mine {}\n")
        step, output = gate("run-origin")
        checks.append(("must-block: a fast-forward over a local file the merge would overwrite stops, naming it",
                       step == "FAIL" and "git merge --ff-only origin/trunk failed" in output and "upstream.cs" in output
                       and git("-C", repo, "rev-list", "--count", "HEAD..origin/trunk") == "1"))
        os.remove(os.path.join(repo, "upstream.cs"))
        git("-C", repo, "switch", "-q", "-c", "feature")
        step, output = gate("run-origin")
        checks.append(("must-block: a checkout behind origin on another branch is never moved",
                       step == "FAIL" and "1 commit(s) behind origin/trunk" in output
                       and git("-C", repo, "rev-list", "--count", "feature..origin/trunk") == "1"))
        git("-C", repo, "switch", "-q", "trunk")
        git("-C", repo, "branch", "-q", "-D", "feature")
        lock = os.path.join(repo, ".git", "index.lock")
        with open(lock, "w", encoding="utf-8") as handle:
            handle.write("")
        step, output = gate("run-origin")
        os.remove(lock)
        checks.append(("must-block: a fast-forward that fails is a red row naming the merge, never a pass",
                       step == "FAIL" and "git merge --ff-only origin/trunk failed" in output))
        step, output = gate("run-origin")
        checks.append(("a checkout behind origin holding only an untracked file is fast-forwarded before its inputs are read, and passes",
                       step == gate_status.STEP_PASS and "fast-forwarded 1 commit(s) to origin/trunk" in output
                       and git("-C", repo, "rev-list", "--count", "HEAD..origin/trunk") == "0"))
        git("-C", repo, "reset", "-q", "--hard", "HEAD~1")
        with open(os.path.join(repo, "code.cs"), "a", encoding="utf-8") as handle:
            handle.write("// uncommitted\n")
        step, output = gate("run-origin")
        with open(os.path.join(repo, "code.cs"), encoding="utf-8") as handle:
            kept = "// uncommitted" in handle.read()
        checks.append(("a checkout behind origin with unstaged edits elsewhere is fast-forwarded, keeping the edits",
                       step == gate_status.STEP_PASS and "fast-forwarded 1 commit(s) to origin/trunk" in output and kept
                       and git("-C", repo, "rev-list", "--count", "HEAD..origin/trunk") == "0"))
        git("-C", repo, "checkout", "-q", "--", "code.cs")
        git("-C", repo, "reset", "-q", "--hard", "HEAD~1")
        git("-C", repo, "switch", "-q", "-c", "session-under-test")
        commit_file(repo, "real.cs", "class Real {}\n")
        step, output = gate("run-origin-session")
        checks.append(("must-block: a session branch holding a commit origin lacks is never moved",
                       step == "FAIL" and "holds 1 commit(s) origin/trunk lacks" in output
                       and git("-C", repo, "rev-list", "--count", "HEAD..origin/trunk") == "1"))
        git("-C", repo, "reset", "-q", "--hard", "trunk")
        git("-C", repo, "cherry-pick", "origin/trunk")
        with open(os.path.join(repo, "code.cs"), "a", encoding="utf-8") as handle:
            handle.write("// uncommitted\n")
        step, output = gate("run-origin-session")
        checks.append(("must-block: a squash-merged session branch with uncommitted edits is never moved",
                       step == "FAIL" and git("-C", repo, "rev-list", "--count", "HEAD..origin/trunk") == "1"))
        git("-C", repo, "checkout", "-q", "--", "code.cs")
        step, output = gate("run-origin")
        checks.append(("must-block: a squash-merged branch is never moved without the session's id naming it",
                       step == "FAIL" and git("-C", repo, "rev-list", "--count", "HEAD..origin/trunk") == "1"))
        step, output = gate("run-origin-session")
        checks.append(("must-block: a squash-merged session branch is never moved, even named by the session",
                       step == "FAIL" and "moved session-under-test" not in output
                       and git("-C", repo, "rev-list", "--count", "HEAD..origin/trunk") == "1"))
        git("-C", repo, "switch", "-q", "trunk")
        git("-C", repo, "branch", "-q", "-D", "session-under-test")
        commit_file(repo, "diverged.cs", "class Diverged {}\n")
        step, output = gate("run-origin")
        checks.append(("must-block: a diverged checkout is never merged, only told to merge or rebase",
                       step == "FAIL" and "holds 1 commit(s) origin/trunk lacks, so merge or rebase onto origin/trunk" in output
                       and "--ff-only" not in output and git("-C", repo, "rev-list", "--count", "HEAD..origin/trunk") == "1"))
        git("-C", repo, "reset", "-q", "--hard", "HEAD~1")
        step, output = gate("run-origin-triage")
        checks.append(("a triage gate stops a checkout behind the refs already fetched",
                       step == "FAIL" and "1 commit(s) behind origin/trunk" in output))
        step, output = gate("run-origin-unknown")
        checks.append(("an unknown GATE_RUN_KIND fails rather than guessing",
                       step == "FAIL" and "neither unset nor triage" in output))
        git("-C", repo, "remote", "set-head", "origin", "-d")
        step, output = gate("run-origin-triage")
        checks.append(("a triage gate on a repo with no origin/HEAD fails closed naming the remedy",
                       step == "FAIL" and "remote set-head origin --auto" in output))
        git("-C", repo, "remote", "set-head", "origin", "trunk")
        git("-C", repo, "remote", "set-url", "origin", os.path.join(root, "absent.git"))
        step, output = gate("run-origin")
        checks.append(("an unreachable origin fails closed", step == "FAIL" and "git fetch origin failed" in output))
        git("-C", repo, "remote", "set-url", "origin", upstream)
        git("-C", repo, "merge", "-q", "--ff-only", "origin/trunk")
        step, output = gate("run-origin")
        checks.append(("a fast-forwarded checkout passes", step == gate_status.STEP_PASS
                       and "HEAD contains origin/trunk (fetched now)" in output))
        commit_file(repo, "local.cs", "class Local {}\n")
        step, _ = gate("run-origin")
        checks.append(("a checkout ahead of origin passes", step == gate_status.STEP_PASS))
        for name, ok in checks:
            print(f"{'OK  ' if ok else 'FAIL'} origin: {name}")
        return sum(not ok for _, ok in checks)
    finally:
        shutil.rmtree(root, onexc=clear_readonly_and_retry)


def catalogs(root):
    """Invoke-CatalogSteps runs both real catalogs scoped to the gate's repo and fails on any row, each side
    shown by a file that is clean for one catalog and dirty for the other."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gate_status

    repo = os.path.join(root, "repo")
    state = os.path.join(root, "state")

    def verdicts():
        record = gate_status.read_record("repo", state)
        return record.get(gate_status.FIELD_VERDICT), gate_status.step_verdicts(record)

    def plant(name, text):
        with open(os.path.join(repo, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    checks = []
    give_origin(root, repo)
    run("run-catalog", root)
    verdict, steps = verdicts()
    checks.append(("a clean repo passes both catalog steps",
                   verdict == gate_status.VERDICT_PASSED and steps.get("Current with origin") == gate_status.STEP_PASS
                   and steps.get("Code-style catalog") == gate_status.STEP_PASS
                   and steps.get("Comment catalog") == gate_status.STEP_PASS))
    plant("Clock.cs", "class Clock { System.DateTime When = System.DateTime.Now; }\n")
    run("run-catalog", root)
    verdict, steps = verdicts()
    checks.append(("a code-style row fails the gate at the code-style step",
                   verdict == gate_status.VERDICT_FAILED and steps.get("Code-style catalog") == "FAIL"
                   and steps.get("Comment catalog") == "NOT REACHED"))
    os.remove(os.path.join(repo, "Clock.cs"))
    plant("Narrated.cs", "// This class exists because the widget needed somewhere to live.\nclass Narrated {}\n")
    run("run-catalog", root)
    verdict, steps = verdicts()
    checks.append(("a comment row fails the gate at the comment step",
                   verdict == gate_status.VERDICT_FAILED and steps.get("Code-style catalog") == gate_status.STEP_PASS
                   and steps.get("Comment catalog") == "FAIL"))
    os.remove(os.path.join(repo, "Narrated.cs"))
    for name, ok in checks:
        print(f"{'OK  ' if ok else 'FAIL'} catalog: {name}")
    return sum(not ok for _, ok in checks)


def dependencies(root):
    """Invoke-DependencyOrderStep stops a consumer's gate while the workspace holds a newer version of a shared
    package than the consumer pins, so a consumer is never gated against a pin its next bump will invalidate. A
    shared package is one the consumer's own .npmrc or NuGet.Config routes to a private feed, and its producer is any
    workspace folder whose manifest names it. The must-block rows come first: a veto proven only by passes is
    indistinguishable from one that never runs."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gate_status

    constants = dependency_constants
    repo = os.path.join(root, "repo")
    state = os.path.join(root, "state")
    npm_producer = os.path.join(root, constants.NPM_PRODUCER_DIRECTORY, constants.PACKAGE_MANIFEST)
    nuget_producer = os.path.join(root, constants.NUGET_PRODUCER_DIRECTORY, constants.NUGET_PRODUCER_PROJECT_DIRECTORY,
                                  constants.NUGET_PRODUCER_PROJECT)
    consumer_manifest = os.path.join(repo, constants.PACKAGE_MANIFEST)
    consumer_project = os.path.join(repo, constants.CONSUMER_PROJECT)
    consumer_npmrc = os.path.join(repo, constants.NPMRC)
    consumer_nuget_config = os.path.join(repo, constants.NUGET_CONFIG)

    def write(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def remove(path):
        if os.path.exists(path):
            os.remove(path)

    def npm_producer_at(version):
        write(npm_producer, '{"name":"%s","version":"%s"}' % (constants.NPM_PACKAGE, version))

    def nuget_producer_at(package, version):
        write(nuget_producer, "<Project><PropertyGroup><PackageId>%s</PackageId><Version>%s</Version></PropertyGroup>"
                              "</Project>" % (package, version))

    def consumer_pins_npm(package, version):
        write(consumer_manifest, '{"devDependencies":{"%s":"%s"}}' % (package, version))

    def consumer_pins_nuget(package, version):
        write(consumer_project, '<Project><ItemGroup><PackageReference Include="%s" Version="%s" />'
                                '</ItemGroup></Project>' % (package, version))

    write(consumer_npmrc, "%s:registry=%s\n" % (constants.NPM_SCOPE, constants.PRIVATE_FEED))
    write(consumer_nuget_config, '<configuration><packageSources><clear /><add key="nuget.org" '
                                 'value="https://api.nuget.org/v3/index.json" /><add key="Private" value="%s" />'
                                 '</packageSources><packageSourceMapping><packageSource key="Private"><package '
                                 'pattern="%s" /></packageSource><packageSource key="nuget.org"><package pattern="*" />'
                                 '</packageSource></packageSourceMapping></configuration>'
                                 % (constants.PRIVATE_FEED, constants.NUGET_PACKAGE))

    def gate():
        result = run("run-dependency", root)
        step = gate_status.step_verdicts(gate_status.read_record("repo", state)).get("Dependency order")
        return step, result.stdout + result.stderr

    checks = []
    npm_producer_at("0.15.0")
    consumer_pins_npm(constants.NPM_PACKAGE, "0.14.3")
    step, output = gate()
    checks.append(("a pin older than its local npm producer stops the gate",
                   step == "FAIL" and f"pins {constants.NPM_PACKAGE} 0.14.3 but the local package is at 0.15.0" in output
                   and "NOT REACHED" in output))
    remove(consumer_manifest)
    nuget_producer_at(constants.NUGET_PACKAGE, "1.6.0")
    consumer_pins_nuget(constants.NUGET_PACKAGE, "1.5.0")
    step, output = gate()
    checks.append(("a pin older than its local NuGet producer stops the gate",
                   step == "FAIL" and f"pins {constants.NUGET_PACKAGE} 1.5.0 but the local package is at 1.6.0" in output))
    remove(consumer_project)
    remove(npm_producer)
    consumer_pins_npm(constants.NPM_PACKAGE, "0.15.0")
    step, output = gate()
    checks.append(("a privately routed pin with no producer in the workspace fails closed",
                   step == "FAIL" and "not in the workspace" in output))
    npm_producer_at("0.15.0")
    step, _ = gate()
    checks.append(("a pin equal to the local package passes", step == gate_status.STEP_PASS))
    remove(consumer_manifest)
    nuget_producer_at(constants.NUGET_PACKAGE, "1.5.0")
    consumer_pins_nuget(constants.NUGET_PACKAGE, "1.5.1")
    step, _ = gate()
    checks.append(("a pin newer than a lagging local checkout passes", step == gate_status.STEP_PASS))
    nuget_producer_at(constants.NUGET_PUBLIC_PACKAGE, "9.0.0")
    consumer_pins_nuget(constants.NUGET_PUBLIC_PACKAGE, "1.0.0")
    step, output = gate()
    checks.append(("a NuGet pin routed to nuget.org is not a shared pin", step == gate_status.STEP_PASS
                   and "pins no shared package" in output))
    remove(consumer_project)
    consumer_pins_npm(constants.NPM_PUBLIC_PACKAGE, "1.0.0")
    step, output = gate()
    checks.append(("an npm pin outside a private scope is not a shared pin", step == gate_status.STEP_PASS
                   and "pins no shared package" in output))
    remove(consumer_manifest)
    step, output = gate()
    checks.append(("a repo pinning no shared package passes", step == gate_status.STEP_PASS
                   and "pins no shared package" in output))
    remove(consumer_npmrc)
    remove(consumer_nuget_config)
    shutil.rmtree(os.path.join(root, constants.NPM_PRODUCER_DIRECTORY), onexc=clear_readonly_and_retry)
    shutil.rmtree(os.path.join(root, constants.NUGET_PRODUCER_DIRECTORY), onexc=clear_readonly_and_retry)
    for name, ok in checks:
        print(f"{'OK  ' if ok else 'FAIL'} dependency: {name}")
    return sum(not ok for _, ok in checks)


def clear_readonly_and_retry(func, path, exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def long_path_form(path):
    """The \\\\?\\ form of an absolute path on Windows, which every file call accepts past MAX_PATH even with long
    paths off: a fixture checkout's gate records nest its full path inside the fixture root, and rmtree's unlink of a
    260-character record fails as not found, so the folder is left and its rmdir fails."""
    full = os.path.abspath(path)
    return "\\\\?\\" + full if os.name == "nt" and not full.startswith("\\\\") else full


TOOLS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RE_STATE_WRITE_HELPER = re.compile(r"function Write-GateStateFile\b.*?\[IO\.File\]::Move\(\$staging, \$path, \$true\)", re.DOTALL)
RE_STATE_WRITE_IN_PLACE = re.compile(r"Move-Item|Set-Content -(?:Literal)?Path \$script:gate(?:Record|Verdicts)File")


def state_write_problems(text):
    """Why GateCommon's record and verdicts writes are not each one atomic replace through Write-GateStateFile, else
    an empty list. A Move-Item over an existing file deletes it first, and Set-Content truncates in place, so a reader
    in either window sees no record or half of one."""
    problems = [] if RE_STATE_WRITE_HELPER.search(text) else ["Write-GateStateFile does not replace with File.Move(..., true)"]
    problems += [f"writes in place: {match.group(0)}" for match in RE_STATE_WRITE_IN_PLACE.finditer(text)]
    calls = len(re.findall(r"Write-GateStateFile \$script:gate(?:Record|Verdicts)File ", text))
    return problems + ([] if calls == 4 else [f"{calls} state writes go through Write-GateStateFile, expected 4"])


def state_write_rows():
    text = ""
    for module in ("GateRecord.ps1", "GateCarry.ps1", "GateRun.ps1"):
        with open(os.path.join(TOOLS_ROOT, "Gates", module), encoding="utf-8") as handle:
            text += handle.read()
    checks = [
        ("must-block: the record and both verdicts writes are each one atomic File.Move replace", state_write_problems(text) == []),
        ("control: a record moved with Move-Item fails the same check",
         state_write_problems(text.replace("[IO.File]::Move($staging, $path, $true)", "Move-Item -LiteralPath $staging -Destination $path -Force")) != []),
        ("control: a verdicts file written in place fails the same check",
         state_write_problems(text.replace("Write-GateStateFile $script:gateVerdictsFile ", "Set-Content -Path $script:gateVerdictsFile ", 1)) != []),
    ]
    checks += held_target_rows()
    mismatches = 0
    for name, ok in checks:
        mismatches += not ok
        print(f"{'OK  ' if ok else 'FAIL'} state-write: {name}")
    return mismatches


def held_target_rows():
    """Write-GateStateFile against a target a reader holds open the way Python opens it, with no share-delete, so
    the replace is refused while the handle lives: a handle closed within the retry window is waited out and the
    write lands; one held past it fails the gate naming the file. Neither leaves its staging file behind."""
    root = tempfile.mkdtemp(prefix="gate-state-write-", dir=claude_temp.scratch_dir())
    target = os.path.join(root, "held.json")
    checks = []

    def staging_left():
        return [name for name in os.listdir(root) if name.endswith(".tmp")]

    try:
        with open(target, "w", encoding="utf-8") as handle:
            handle.write('{"written": false}\n')
        reader = open(target, encoding="utf-8")
        closer = threading.Timer(0.5, reader.close)
        closer.start()
        brief = run_process("state-write", root)
        closer.join()
        with open(target, encoding="utf-8-sig") as handle:
            landed = json.load(handle).get("written") is True
        checks.append(("must-block: a reader that lets go within the retry window is waited out and the write lands",
                       brief.returncode == 0 and "RESULT written" in brief.stdout and landed))
        checks.append(("control: no staging file is left after a write that landed", staging_left() == []))
        with open(target, encoding="utf-8") as reader:
            held = run_process("state-write", root)
        checks.append(("must-block: a reader held past the retry window fails the gate, naming the file",
                       held.returncode == 1 and f"GATE: FAILED (could not write {target}" in held.stdout
                       and "RESULT written" not in held.stdout))
        checks.append(("control: no staging file is left after a write that failed", staging_left() == []))
    finally:
        shutil.rmtree(root, onexc=clear_readonly_and_retry)
    return checks


def implicit_rows():
    """A checkout with no uncommitted change whose HEAD is origin's default branch HEAD reads CURRENT-IMPLICIT, which
    is_green accepts beside GREEN-CURRENT. Must-not rows first: an uncommitted file, and a commit origin lacks."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gate_status

    root = tempfile.mkdtemp(prefix="gate-implicit-", dir=claude_temp.scratch_dir())
    checks = []
    try:
        upstream = os.path.join(root, "upstream.git")
        repo = os.path.join(root, "repo")
        git("init", "--bare", "-q", "-b", "trunk", upstream)
        git("init", "-q", "-b", "trunk", repo)
        git("-C", repo, git_constants.CONFIG, git_constants.FSMONITOR, git_constants.DISABLED)
        commit_file(repo, "code.cs", "class A {}\n")
        git("-C", repo, "remote", "add", "origin", upstream)
        git("-C", repo, "push", "-q", "origin", "trunk")
        git("-C", repo, "remote", "set-head", "origin", "trunk")
        never = {"repo": "repo", gate_status.STATUS_STATE: gate_status.NEVER_GATED}

        with open(os.path.join(repo, "pending.cs"), "w", encoding="utf-8") as handle:
            handle.write("class Pending {}\n")
        dirty = gate_status.with_implicit(dict(never), repo)
        checks.append(("must-not: a checkout holding an uncommitted file keeps its own state",
                       dirty[gate_status.STATUS_STATE] == gate_status.NEVER_GATED and not gate_status.is_green(dirty)))
        os.remove(os.path.join(repo, "pending.cs"))
        commit_file(repo, "ahead.cs", "class Ahead {}\n")
        ahead = gate_status.with_implicit(dict(never), repo)
        checks.append(("must-not: a commit origin lacks is not checked in and pushed",
                       ahead[gate_status.STATUS_STATE] == gate_status.NEVER_GATED))
        git("-C", repo, "push", "-q", "origin", "trunk")
        os.makedirs(os.path.join(repo, "StrykerOutput"))
        with open(os.path.join(repo, "StrykerOutput", "report.json"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        pushed = gate_status.with_implicit(dict(never), repo)
        checks.append(("must-report: a clean checkout at origin's HEAD reads CURRENT-IMPLICIT, a gate's leftovers aside",
                       pushed[gate_status.STATUS_STATE] == gate_status.CURRENT_IMPLICIT and gate_status.is_green(pushed)))
        green = gate_status.with_implicit({"repo": "repo", gate_status.STATUS_STATE: gate_status.GREEN_CURRENT}, repo)
        checks.append(("control: a GREEN-CURRENT record stays GREEN-CURRENT",
                       green[gate_status.STATUS_STATE] == gate_status.GREEN_CURRENT and gate_status.is_green(green)))
        checks.append(("control: a red state is not green", not gate_status.is_green({gate_status.STATUS_STATE: gate_status.RED})))
        running = {"repo": "repo", gate_status.STATUS_STATE: gate_status.RUNNING, "started_at": "2026-01-01T00:00:00Z",
                   "session": "peer"}
        superseded = gate_status.with_implicit(dict(running), repo)
        checks.append(("must-report: a RUNNING record on a clean checkout at origin's HEAD reads CURRENT-IMPLICIT, naming the run it replaced",
                       superseded[gate_status.STATUS_STATE] == gate_status.CURRENT_IMPLICIT
                       and superseded.get("superseded_run", {}).get("session") == "peer"))

        state_dir = gate_status.repo_state_dir("repo", root)
        carry = os.path.join(state_dir, "repo.{0}.json".format(gate_status.this_machine()))
        landed = gate_status.record_landing("repo", root)
        on_disk = gate_status.read_record("repo", state_dir)
        checks.append(("must-report: a landing on a clean checkout at origin's HEAD writes LANDED and its sha into the global record",
                       on_disk is not None and on_disk.get("verdict") == gate_status.VERDICT_LANDED
                       and on_disk.get("head") == landed["head"] and isinstance(on_disk.get("landedAtMs"), int)))
        checks.append(("must-report: the landed record reads CURRENT-IMPLICIT",
                       gate_status.latest_status("repo", root)[gate_status.STATUS_STATE] == gate_status.CURRENT_IMPLICIT))
        checks.append(("must-not: a landing grants no carried step (the carry file is never written)", not os.path.exists(carry)))
        with open(os.path.join(repo, "after.cs"), "w", encoding="utf-8") as handle:
            handle.write("class After {}\n")
        edited = gate_status.latest_status("repo", root)
        checks.append(("must-not: an edit after the landing is not green", not gate_status.is_green(edited)))
        dirty_landing = gate_status.record_landing("repo", root)
        dirty_state = gate_status.latest_status("repo", root)
        checks.append(("must-report: a landing beside a peer's uncommitted file is recorded, naming the file, and is not green",
                       dirty_landing["uncommitted"] == ["after.cs"] and not gate_status.is_green(dirty_state)
                       and "after.cs" in dirty_state.get(gate_status.STATUS_CHANGED, [])))
        still = gate_status.with_implicit(dict(running), repo)
        checks.append(("control: a RUNNING record on a dirty checkout stays RUNNING",
                       still[gate_status.STATUS_STATE] == gate_status.RUNNING))
        os.remove(os.path.join(repo, "after.cs"))
        commit_file(repo, "unpushed.cs", "class Unpushed {}\n")
        try:
            gate_status.record_landing("repo", root)
            refused = False
        except ValueError:
            refused = True
        checks.append(("must-block: a landing with a commit origin lacks is refused", refused))
        checks.append(("control: a SUPERSEDED record is not green",
                       not gate_status.is_green({gate_status.STATUS_STATE: gate_status.SUPERSEDED})))
    finally:
        shutil.rmtree(root, onexc=clear_readonly_and_retry)
    mismatches = 0
    for name, ok in checks:
        mismatches += not ok
        print(f"{'OK  ' if ok else 'FAIL'} implicit: {name}")
    return mismatches + landing_supersede_rows()


def landing_supersede_rows():
    """A running gate reads its repo's global record between steps: a landing written after it started stops it as
    SUPERSEDED, slot released, and the landing is never overwritten by its own record write. Must-block rows first;
    a landing older than the gate is the control."""
    root = tempfile.mkdtemp(prefix="gate-supersede-", dir=claude_temp.scratch_dir())
    checks = []
    try:
        repo = os.path.join(root, "repo")
        git("init", "-q", repo)
        git("-C", repo, git_constants.CONFIG, git_constants.FSMONITOR, git_constants.DISABLED)
        commit_file(repo, "code.cs", "class A {}\n")
        with open(os.path.join(repo, "pending.txt"), "w", encoding="utf-8") as handle:
            handle.write("uncommitted, so a gate runs\n")
        record = os.path.join(root, "state-land", "repo.{0}.last.json".format(os.environ["COMPUTERNAME"]))

        def verdict():
            with open(record, encoding="utf-8-sig") as handle:
                return json.load(handle).get("verdict")

        stopped = run_process("land-after-start", root)
        checks.append(("must-block: a landing written after the gate started stops it before its next step as SUPERSEDED",
                       stopped.returncode == 1 and "GATE: SUPERSEDED" in stopped.stdout and "RESULT ran" not in stopped.stdout))
        checks.append(("must-block: the stopped gate never overwrites the landing in the global record", verdict() == "LANDED"))
        checks.append(("must-block: the stopped gate releases the slot",
                       "SLOT-AFTER holder=False" in run_process("held-slot", root).stdout))
        triage = run_process("land-after-start-triage", root)
        checks.append(("must-block: a triage gate (GATE_RUN_KIND=triage) never reads the operator's main-checkout landing, "
                       "so a landing written after it started neither stops it nor is read",
                       "LANDING-FILE True" in triage.stdout and "GATE: SUPERSEDED" not in triage.stdout
                       and "RESULT ran" in triage.stdout))
        mid = run_process("land-mid-step", root)
        checks.append(("must-block: a landing written while a step runs survives that step's row and stops the gate at completion",
                       mid.returncode == 1 and "GATE: SUPERSEDED" in mid.stdout and "GATE: PASSED" not in mid.stdout
                       and verdict() == "LANDED"))
        older = run_process("land-before-start", root)
        checks.append(("control: a landing older than the gate does not stop it",
                       older.returncode == 0 and "RESULT ran" in older.stdout and "GATE: PASSED" in older.stdout
                       and verdict() == "PASSED"))
    finally:
        shutil.rmtree(root, onexc=clear_readonly_and_retry)
    mismatches = 0
    for name, ok in checks:
        mismatches += not ok
        print(f"{'OK  ' if ok else 'FAIL'} supersede: {name}")
    return mismatches


def progress_rows():
    """The record carries a gate's progress while it runs, and watch_gate.py turns it into one line per event.
    Must-report rows first: the step in flight, the finished row, a dead gate's RUNNING record, and an earlier run's
    finished record ignored rather than reported."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gate_status
    import watch_gate

    root = tempfile.mkdtemp(prefix="gate-progress-", dir=claude_temp.scratch_dir())
    checks = []
    try:
        repo = os.path.join(root, "repo")
        git("init", "-q", repo)
        git("-C", repo, git_constants.CONFIG, git_constants.FSMONITOR, git_constants.DISABLED)
        commit_file(repo, "code.cs", "class A {}\n")
        state = os.path.join(root, "state-progress")
        refused = run("progress", root)
        checks.append(("must-block: a gate on a repo with no uncommitted changes does not run and writes no record",
                       refused.returncode != 0 and "GATE: NOT RUN" in refused.stdout and "no uncommitted changes" in refused.stdout
                       and gate_status.read_record("repo", state) is None))
        os.makedirs(os.path.join(repo, "StrykerOutput"))
        with open(os.path.join(repo, "StrykerOutput", "report.json"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        refused = run("progress", root)
        checks.append(("must-block: a gate's own build leftovers do not make a committed repo dirty",
                       refused.returncode != 0 and "GATE: NOT RUN" in refused.stdout
                       and gate_status.read_record("repo", state) is None))
        with open(os.path.join(repo, "pending.txt"), "w", encoding="utf-8") as handle:
            handle.write("uncommitted, so a gate runs\n")
        output = run("progress", root).stdout.splitlines()
        snaps = {line.split(" ", 1)[0]: json.loads(line.split(" ", 1)[1]) for line in output if line.startswith("SNAP-")}
        running, row = snaps.get("SNAP-RUNNING", {}), snaps.get("SNAP-ROW", {})
        checks.append(("must-report: a step that starts is in the record as currentStep while it runs",
                       running.get("verdict") == "RUNNING" and running.get("currentStep") == "First step"
                       and running.get("currentStepStartedAt") and isinstance(running.get("pid"), int)))
        row_names = [r.get("name") for r in row.get("steps") or []]
        checks.append(("must-report: a finished row is in the record before the gate completes, and the step leaves currentStep",
                       row.get("verdict") == "RUNNING" and "First step" in row_names and row.get("currentStep") is None))
        shown = gate_status.render({"repo": "repo", "state": "RUNNING", "progress": gate_status.progress(running)})
        checks.append(("gate_status prints a running gate's step in flight", "running First step since" in shown))
        finished = gate_status.read_record("repo", os.path.join(root, "state-progress"))
        checks.append(("control: the completed record carries no step in flight",
                       finished is not None and finished.get("verdict") == "PASSED" and finished.get("currentStep") is None))

        path = os.path.join(root, "watched.last.json")
        frames = [
            {"verdict": "RUNNING", "pid": 1, "steps": [], "currentStep": "First step"},
            {"verdict": "RUNNING", "pid": 1, "steps": [{"name": "First step", "verdict": "PASS", "detail": ""}],
             "currentStep": None},
            {"verdict": "PASSED", "pid": 1, "completedAt": "2999-01-01T00:00:00Z",
             "steps": [{"name": "First step", "verdict": "PASS", "detail": ""}]},
        ]

        def play(sequence, alive):
            lines = []
            queue = list(sequence)

            def write(frame):
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(frame, handle)

            def sleep(_):
                if not queue:
                    raise TimeoutError("the watcher never ended")
                write(queue.pop(0))

            write(queue.pop(0))
            try:
                code = watch_gate.watch(path, interval=0, alive=alive, out=lines.append, sleep=sleep)
            except TimeoutError:
                code = None
            return code, lines

        code, lines = play(frames, lambda pid: True)
        checks.append(("must-report: the watcher prints the step start, the finished row and the verdict, then exits 0",
                       code == 0 and lines == ["running First step", "First step  PASS", "GATE: PASSED"]))
        code, lines = play(frames[:1] + frames[:1], lambda pid: False)
        checks.append(("must-report: a RUNNING record whose gate process is gone ends the watch with exit 2",
                       code == 2 and lines[-1].startswith("GATE: STOPPED")))
        old = {"verdict": "FAILED", "pid": 1, "completedAt": "2000-01-01T00:00:00Z", "steps": []}
        code, lines = play([old, old] + frames, lambda pid: True)
        checks.append(("must-not-report: an earlier run's finished record is waited past, not reported as this run",
                       code == 0 and lines[0].startswith("waiting for") and "GATE: FAILED" not in lines
                       and lines[-1] == "GATE: PASSED"))
        code, lines = play([dict(frames[2], verdict="FAILED")], lambda pid: True)
        checks.append(("a failed verdict exits 1", code == 1 and lines[-1] == "GATE: FAILED"))

        stopped_state = os.path.join(root, "state-stopped")
        here = os.environ.get("COMPUTERNAME", "HOST")
        gate_status.write_record(gate_status.record_path("repo", stopped_state),
                                 {"repo": "repo", "verdict": "RUNNING", "pid": 424242, "machine": here,
                                  "startedAt": "2026-01-01T00:00:00Z", "steps": [], "currentStep": "First step"})
        gone = gate_status.status("repo", root, stopped_state, alive=lambda pid: False)
        checks.append(("must-report: a RUNNING record whose gate process on this machine is gone reads STOPPED, never RUNNING",
                       gone[gate_status.STATUS_STATE] == gate_status.STOPPED))
        checks.append(("must-report: gate_status says a stopped run's process is gone instead of a step in flight",
                       "is gone" in gate_status.render({**gone, "repo": "repo"}) and "running First step" not in gate_status.render({**gone, "repo": "repo"})))
        alive = gate_status.status("repo", root, stopped_state, alive=lambda pid: True)
        checks.append(("control: a RUNNING record whose process is alive stays RUNNING",
                       alive[gate_status.STATUS_STATE] == gate_status.RUNNING))
        gate_status.write_record(gate_status.record_path("repo", stopped_state),
                                 {"repo": "repo", "verdict": "RUNNING", "pid": 424242, "machine": "SOME-OTHER-BOX",
                                  "startedAt": "2026-01-01T00:00:00Z", "steps": []})
        elsewhere = gate_status.status("repo", root, stopped_state, alive=lambda pid: False)
        checks.append(("control: another machine's RUNNING record, whose process cannot be checked here, stays RUNNING",
                       elsewhere[gate_status.STATUS_STATE] == gate_status.RUNNING))
        checks.append(("control: watch_gate and gate_status share one process check",
                       watch_gate.process_alive is gate_status.process_alive))
    finally:
        shutil.rmtree(root, onexc=clear_readonly_and_retry)
    mismatches = 0
    for name, ok in checks:
        mismatches += not ok
        print(f"{'OK  ' if ok else 'FAIL'} progress: {name}")
    return mismatches


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None,
                        help="run only the probe cases, or the delta/record/catalog sections, naming a term")
    args = parser.parse_args()

    def chosen(name):
        return not args.only or any(term.lower() in name.lower() for term in args.only)

    root = tempfile.mkdtemp(prefix="gate-common-", dir=claude_temp.scratch_dir())
    os.environ["GATE_PROBE_TEMP"] = os.path.join(root, "temp")
    os.makedirs(os.environ["GATE_PROBE_TEMP"])
    os.environ.pop("CLAUDE_CODE_TMPDIR", None)
    os.environ["TEMP"] = os.environ["TMP"] = os.environ["GATE_PROBE_TEMP"]
    os.environ.pop("CRGOLDEN_GATE_SLOT_DIRECTORY", None)
    os.environ.pop("CRGOLDEN_GATE_SLOT_TOKEN", None)
    ran = [0]

    def section(name, check):
        if not chosen(name):
            return 0
        ran[0] += 1
        return check(root)

    try:
        fixtures = os.path.join(root, "fixtures")
        os.makedirs(fixtures)
        for name, content in FIXTURES.items():
            with open(os.path.join(fixtures, name), "w", encoding="utf-8") as handle:
                handle.write(content)
        probes = [case for case in list(FAIL_FAST_CASES) + list(SELECTED_CASES) if chosen(case)]
        ran[0] += len(probes)
        mismatches = fail_fast(root, chosen) + selected(root, chosen)
        HOST.append(ProbeHost())
        if any(chosen(name) for name in ("delta", "record", "catalog", "dependency")):
            ran[0] += 1
            mismatches += delta(root)
        mismatches += section("record", records) + section("catalog", catalogs) + section("dependency", dependencies)
        if chosen("step-inputs"):
            ran[0] += 1
            mismatches += step_inputs()
        if chosen("step-order"):
            ran[0] += 1
            mismatches += step_order()
        if chosen("worktree"):
            ran[0] += 1
            mismatches += worktrees()
        if chosen("origin"):
            ran[0] += 1
            mismatches += origin()
        if chosen("progress"):
            ran[0] += 1
            mismatches += progress_rows()
        if chosen("implicit"):
            ran[0] += 1
            mismatches += implicit_rows()
        if chosen("state-write"):
            ran[0] += 1
            mismatches += state_write_rows()
    finally:
        while HOST:
            HOST.pop().close()
        shutil.rmtree(long_path_form(root), onexc=clear_readonly_and_retry)
    if ran[0] == 0:
        print("No cases ran; refusing to report success on an empty selection.")
        return 1
    print(f"\n{mismatches} mismatch(es)")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
