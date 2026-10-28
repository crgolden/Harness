# GATE-STEP: Fixture teardown harness
# GATE-LISTING: os.walk(root) | *.py *.ps1
"""Regression harness for fixture_teardown.py, then the check itself over the Tools tree. Exits non-zero on any
regression or on any harness that deletes its fixture folder silently.

    python Harness/Gates/test_fixture_teardown.py
"""

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(TOOLS, "Hooks"))
import claude_temp  # noqa: E402
import fixture_teardown  # noqa: E402

failures = []
SKIPPED_FOLDERS = frozenset({".git", "node_modules", "__pycache__"})


def tree_findings(root):
    """[(relative path, line number, line)] for every silent teardown in a harness under root."""
    findings = []
    for folder, folders, names in os.walk(root):
        folders[:] = [name for name in folders if name not in SKIPPED_FOLDERS]
        for name in sorted(names):
            findings.extend(fixture_teardown.file_findings(root, os.path.join(folder, name)))
    return findings


def record(name, ok, detail=""):
    print("{0}  {1}{2}".format("PASS" if ok else "FAIL", name, "" if ok else "  got: " + str(detail)))
    if not ok:
        failures.append(name)


root = tempfile.mkdtemp(prefix="fixture-teardown-", dir=claude_temp.scratch_dir())
try:
    def plant(relative, text):
        path = os.path.join(root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return relative

    silent_ps = plant("Gates/Test-Planted.ps1",
                      "try { }\nfinally {\n    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue\n}\n")
    silent_py = plant("Hooks/test_planted.py", "import shutil\nshutil.rmtree(root, ignore_errors=True)\n")
    marked = plant("Claude/selftest.py", "# GATE-STEP: Planted selftest\nshutil.rmtree(ws, ignore_errors=True)\n")
    loud_ps = plant("Gates/Test-Loud.ps1", "finally {\n    Remove-FixtureTree $work\n}\n")
    loud_py = plant("Hooks/test_loud.py", "claude_temp.remove_fixture_tree(root)\n")
    production = plant("Grafana/Invoke-Planted.ps1", "Remove-Item $destDir -Recurse -Force -ErrorAction SilentlyContinue\n")
    commented = plant("Gates/Test-Commented.ps1",
                      "# Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue\n")
    file_only = plant("Gates/Test-FileOnly.ps1", "Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue\n")

    reported = {relative for relative, _, _ in tree_findings(root)}
    record("must-block: a harness's silent recursive Remove-Item is reported", silent_ps in reported, reported)
    record("must-block: a harness's rmtree with ignore_errors=True is reported", silent_py in reported, reported)
    record("must-block: a file carrying a GATE-STEP marker is a harness whatever its name", marked in reported, reported)
    record("control: Remove-FixtureTree is no silent teardown", loud_ps not in reported, reported)
    record("control: claude_temp.remove_fixture_tree is no silent teardown", loud_py not in reported, reported)
    record("control: a script that is no harness is not judged", production not in reported, reported)
    record("control: a commented-out delete is not reported", commented not in reported, reported)
    record("control: a single-file delete is not a fixture-tree teardown", file_only not in reported, reported)
finally:
    claude_temp.remove_fixture_tree(root)
record("control: the harness's own fixture folder is gone after its teardown", not os.path.exists(root), root)

SELF = os.path.relpath(os.path.abspath(__file__), TOOLS).replace("\\", "/")
live = [finding for finding in tree_findings(TOOLS) if finding[0] != SELF]
for relative, number, line in live:
    print("    {0}:{1}  {2}".format(relative, number, line))
record("must-block: no harness in Tools deletes its fixture folder silently", not live,
       "{0} silent teardown(s); use Remove-FixtureTree or claude_temp.remove_fixture_tree".format(len(live)))

if failures:
    print("\n{0} fixture-teardown case(s) failed.".format(len(failures)))
    sys.exit(1)
print("\nAll fixture-teardown cases passed.")
