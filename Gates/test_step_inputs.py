# GATE-STEP: Step inputs harness
"""Regression harness for Gates/step_inputs.py, the plan Tools' gate.ps1 takes its steps and their inputs from.
Exits non-zero on any failure.

    python Harness/Gates/test_step_inputs.py

A step carries its last pass while the files its inputs name are unchanged, so an input the plan leaves out is a pass
carried over a changed file. This step's own inputs are the whole repo, so it runs again whenever any harness or any
file one reaches changes. Every rule has a row on a fixture repo and a control.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(TOOLS_ROOT, "Hooks"))
import claude_temp  # noqa: E402
import step_inputs  # noqa: E402

failures = []


def record(name, ok, detail=""):
    print("{0}  {1}{2}".format("PASS" if ok else "FAIL", name, "" if ok else "  got: " + str(detail)))
    if not ok:
        failures.append(name)


FILES = {
    "Alpha/test_alpha.py": "# GATE-STEP: Alpha harness\nimport os\nimport sys\nHERE = os.path.dirname(os.path.abspath(__file__))\n"
                           "sys.path.insert(0, os.path.join(HERE, '..', 'Shared'))\nimport helper\n"
                           "DATA = os.path.join(HERE, '..', 'Shared', 'data.json')\nNOTE = 'AGENTS.md'\n",
    "Shared/helper.py": "import shared_core\n",
    "Shared/shared_core.py": "VALUE = 1\n",
    "Shared/data.json": "{}\n",
    "Shared/notes.md": "notes\n",
    "Shared/other.py": "Y = 2\n",
    "Beta/test_beta.py": "# GATE-STEP: Beta harness\nimport os\nHERE = os.path.dirname(os.path.abspath(__file__))\n"
                         "names = os.listdir(HERE)\nDOC = os.path.join(HERE, '..', 'Shared', 'notes.md')\n",
    "Beta/beta_tool.py": "X = 1\n",
    "Gamma/Test-Gamma.ps1": "# GATE-STEP: Gamma harness\n. \"$PSScriptRoot\\..\\Shared\\Helper.ps1\"\n",
    "Shared/Helper.ps1": "function Get-Value { 1 }\n",
    "Delta/test_delta_checks.py": "X = 1\n",
    "Epsilon/selfcheck.py": "# GATE-STEP: Epsilon selftest | args: selftest --quick\nX = 1\n",
    "Zeta/test_zeta.py": "# GATE-STEP: Zeta tests | pytest\nimport zeta_lib\n",
    "Zeta/zeta_lib.py": "Z = 1\n",
    "Eta/Test-Eta.ps1": "# GATE-STEP: none | a live check, never a gate step\nWrite-Host 1\n",
    "Theta/test_theta.py": "# GATE-STEP: Theta harness\nimport os\nimport claude_temp\nscratch = claude_temp.scratch_dir()\n"
                           "found = os.listdir(scratch)\n",
    "Iota/test_iota.py": "# GATE-STEP: Iota harness\nimport os\nfound = os.listdir(os.environ['SOMEWHERE'])\n",
    "Lambda/test_lambda.py": "# GATE-STEP: Lambda harness\nimport os\nHERE = os.path.dirname(os.path.abspath(__file__))\n"
                             "TEXT = open(os.path.join(HERE, '..', 'Shared', 'read_only.py'), encoding='utf-8').read()\n",
    "Mu/test_mu.py": "# GATE-STEP: Mu harness\nimport os\nimport sys\nHERE = os.path.dirname(os.path.abspath(__file__))\n"
                     "sys.path.insert(0, os.path.join(HERE, '..', 'Shared'))\nimport read_only\n",
    "Shared/read_only.py": "import read_dep\n",
    "Shared/read_dep.py": "X = 1\n",
    "Nu/Test-Nu.ps1": "# GATE-STEP: Nu harness\n$text = Get-Content -Raw (Join-Path $PSScriptRoot '..\\Shared\\Loader.ps1')\n",
    "Shared/Loader.ps1": ". \"$PSScriptRoot\\Loaded.ps1\"\n",
    "Shared/Loaded.ps1": "function Get-Loaded { 1 }\n",
    "Kappa/kappa_tool.py":"\"\"\"A tool whose docstring shows the marker as an example:\n\n    GATE-STEP: Not a step\n\"\"\"\n",
    "AGENTS.md": "fixture\n",
}


def fixture(root):
    for name, text in FILES.items():
        path = os.path.join(root, name.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    subprocess.run(["git", "-C", root, "-c", "core.fsmonitor=false", "init", "--quiet"], check=True, capture_output=True)
    subprocess.run(["git", "-C", root, "config", "core.fsmonitor", "false"], check=True, capture_output=True)


def clear_readonly_and_retry(func, path, _):
    os.chmod(path, 0o700)
    func(path)


def by_name(planned):
    return {step["name"]: step for step in planned["steps"]}


def main():
    real_gate = open(os.path.join(TOOLS_ROOT, "gate.ps1"), encoding="utf-8").read()
    record("must-pass: Tools' gate.ps1 takes its steps and their inputs from the plan, and lists none by hand",
           "step_inputs.py') $repo --plan" in real_gate and "Register-StepInputs $stepInputs" in real_gate
           and "Register-GateSteps @($harnesses.Keys)" in real_gate and "Join-Path $repo 'Hooks\\" not in real_gate)
    record("must-block: Tools' gate.ps1 hands -Probe to GateCommon's Initialize-GateState, whose probe mode writes no "
           "record (test_gate_common.py, probe rows), and keeps no probe branch of its own that could drift from it",
           "Initialize-GateState 'Tools' $repo $gateState -Standalone:$Standalone -Probe $Probe" in real_gate
           and "Enter-GateSlotForGate" not in real_gate)
    real = step_inputs.plan(TOOLS_ROOT, os.path.join(claude_temp.scratch_dir(), "step-inputs-harness.cache.json"))
    real_steps = by_name(real)
    record("control: the real repo plans its harness steps, each with inputs (it would pass vacuously on none)",
           len(real_steps) >= 50 and all(step["inputs"] for step in real_steps.values()) and "Step inputs harness" in real_steps,
           (len(real_steps), [name for name, step in real_steps.items() if not step["inputs"]]))
    record("must-pass: the real repo plans with no listing it cannot follow", not real["warnings"], real["warnings"])

    root = tempfile.mkdtemp(prefix="step-inputs-", dir=claude_temp.scratch_dir())
    cache = os.path.join(root + "-cache", "plan.json")
    try:
        fixture(root)
        planned = step_inputs.plan(root, cache)
        steps = by_name(planned)
        alpha = set(steps.get("Alpha harness", {}).get("inputs", []))
        record("must-pass: a module imported through a folder put on sys.path, then imported by that module, is an input",
               {"Shared/helper.py", "Shared/shared_core.py"} <= alpha, sorted(alpha))
        record("must-pass: a data file named by a path joined in parts from the harness's own folder is an input",
               "Shared/data.json" in alpha, sorted(alpha))
        record("control: a file the harness never reaches is no input", "Shared/other.py" not in alpha, sorted(alpha))
        record("control: a bare file name naming no sibling (AGENTS.md) is no input", "AGENTS.md" not in alpha, sorted(alpha))
        gamma = set(steps.get("Gamma harness", {}).get("inputs", []))
        record("must-pass: a script dot-sourced through \"$PSScriptRoot\\..\\\" is an input", "Shared/Helper.ps1" in gamma, sorted(gamma))
        beta = set(steps.get("Beta harness", {}).get("inputs", []))
        record("must-pass: a markdown file a harness names is an input, so the gate fingerprints it", "Shared/notes.md" in beta, sorted(beta))
        record("must-pass: a listing of the harness's own folder widens the step to that folder", "Beta/*" in beta, sorted(beta))
        record("must-pass: a conventionally named file with no marker is a step named from its file",
               "Delta checks harness" in steps, sorted(steps))
        epsilon = steps.get("Epsilon selftest", {})
        record("must-pass: a marker on a file outside the naming convention makes it a step, with its arguments",
               epsilon.get("command", [])[-2:] == ["selftest", "--quick"], epsilon)
        zeta = steps.get("Zeta tests", {})
        record("must-pass: a pytest marker runs pytest over the file's folder, and every file there is an input",
               zeta.get("command", [])[1:3] == ["-m", "pytest"] and zeta.get("command", [""])[-1].endswith("Zeta")
               and {"Zeta/test_zeta.py", "Zeta/zeta_lib.py"} <= set(zeta.get("inputs", [])), zeta)
        record("must-block: a file whose marker says none is never a step, even under the naming convention",
               not any(step["harness"] == "Eta/Test-Eta.ps1" for step in planned["steps"]), sorted(steps))
        record("must-block: a marker indented inside a docstring is an example, not a step", "Not a step" not in steps, sorted(steps))
        theta = steps.get("Theta harness", {}).get("inputs", [])
        record("control: a listing of a temp folder, through the variable it is assigned to, adds nothing",
               theta == ["Theta/test_theta.py"], theta)
        iota = steps.get("Iota harness", {}).get("inputs", [])
        record("must-pass: a listing the analysis cannot follow makes its step owe every edit, never fails the plan",
               iota == ["*"] and any("Iota/test_iota.py:3" in line for line in planned["warnings"]), (iota, planned["warnings"]))
        iota_path = os.path.join(root, "Iota", "test_iota.py")
        with open(iota_path, encoding="utf-8") as handle:
            iota_text = handle.read()
        with open(iota_path, "w", encoding="utf-8") as handle:
            handle.write("# GATE-LISTING: os.listdir(os.environ | none\n" + iota_text)
        narrowed = step_inputs.plan(root, None)
        with open(iota_path, "w", encoding="utf-8") as handle:
            handle.write(iota_text)
        record("control: a listing the harness classifies in a GATE-LISTING line takes that line's folders instead of '*'",
               by_name(narrowed)["Iota harness"]["inputs"] == ["Iota/test_iota.py"] and not narrowed["warnings"], narrowed["warnings"])
        with open(iota_path, "w", encoding="utf-8") as handle:
            handle.write("# GATE-LISTING: os.listdir(os.environ | none\n# GATE-LISTING: os.walk(gone) | none\n" + iota_text)
        stale = step_inputs.plan(root, None)
        with open(iota_path, "w", encoding="utf-8") as handle:
            handle.write(iota_text)
        record("must-warn: a GATE-LISTING line matching no listing in its harness is named in a warning",
               any("os.walk(gone)" in line and "matches no listing" in line for line in stale["warnings"]), stale["warnings"])
        record("control: the GATE-LISTING line that does match a listing draws no such warning",
               not any("os.listdir(os.environ" in line and "matches no listing" in line for line in stale["warnings"]), stale["warnings"])
        lam = set(steps.get("Lambda harness", {}).get("inputs", []))
        record("must-pass: a file a harness only reads as text (open) is an input",
               "Shared/read_only.py" in lam, sorted(lam))
        record("must-block: what a file read as text imports is no input of the harness reading it",
               "Shared/read_dep.py" not in lam, sorted(lam))
        mu = set(steps.get("Mu harness", {}).get("inputs", []))
        record("control: the same file imported is followed, so what it imports is an input",
               {"Shared/read_only.py", "Shared/read_dep.py"} <= mu, sorted(mu))
        nu = set(steps.get("Nu harness", {}).get("inputs", []))
        record("must-pass: a script a PowerShell harness reads with Get-Content is an input, and what it dot-sources is not",
               "Shared/Loader.ps1" in nu and "Shared/Loaded.ps1" not in nu, sorted(nu))

        warm = step_inputs.plan(root, cache)
        record("must-pass: a plan from the cache equals the plan it cached", warm == planned)
        with open(os.path.join(root, "Shared", "helper.py"), "a", encoding="utf-8") as handle:
            handle.write("import other\n")
        edited = by_name(step_inputs.plan(root, cache))
        record("must-pass: an edited file is read again, so a reach it gains reaches the plan through the cache",
               "Shared/other.py" in edited["Alpha harness"]["inputs"], edited["Alpha harness"]["inputs"])
        with open(os.path.join(root, "Shared", "dupe_test.py"), "w", encoding="utf-8") as handle:
            handle.write("# GATE-STEP: Alpha harness\n")
        try:
            step_inputs.plan(root, cache)
            clash = None
        except step_inputs.PlanError as error:
            clash = str(error)
        record("must-block: two harnesses claiming one step name refuse the plan, since a step's record is kept by name",
               clash is not None and "Alpha harness" in clash, clash)
        os.remove(os.path.join(root, "Shared", "dupe_test.py"))
        cli = subprocess.run([sys.executable, os.path.join(HERE, "step_inputs.py"), root, "--plan", "--cache", cache],
                             capture_output=True, text=True)
        parsed = json.loads(cli.stdout) if cli.returncode == 0 and cli.stdout else {}
        record("must-pass: --plan prints the plan as JSON with absolute command paths",
               cli.returncode == 0 and any(os.path.isabs(step["command"][1]) for step in parsed.get("steps", [])),
               (cli.returncode, cli.stderr[-300:]))
    finally:
        for folder in (root, root + "-cache"):
            if os.path.isdir(folder):
                shutil.rmtree(folder, onexc=clear_readonly_and_retry)

    if failures:
        print("\n{0} step-inputs case(s) failed.".format(len(failures)))
        return 1
    print("\nAll step-inputs cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
