"""Every harness tears its fixture folder down loudly. A recursive delete that swallows its own failure
(Remove-Item -Recurse ... -ErrorAction SilentlyContinue, shutil.rmtree(..., ignore_errors=True)) leaves a folder
behind whenever a file is still held or read-only, and nothing reports it, so fixtures pile up in the temp root and
are carried to the state share. A harness removes its folder with Remove-FixtureTree (Hooks/ClaudeTemp.ps1) or
claude_temp.remove_fixture_tree, which fail the run on anything left.

A harness is a file named Test-*.ps1, Test-*.py, test_*.py or mutation_controls.py, or any file whose first lines
carry a GATE-STEP marker. Verified by Harness/Gates/test_fixture_teardown.py.
"""

import os
import re

RE_HARNESS_NAME = re.compile(r"^(?:Test-[^.]+\.(?:ps1|py)|test_\w+\.py|mutation_controls\.py)$")
RE_STEP_MARKER = re.compile(r"^(?:#\s*)?GATE-STEP:", re.MULTILINE)
RE_SILENT_TEARDOWN = re.compile(
    r"\bRemove-Item\b[^#\r\n]*-Recurse\b[^#\r\n]*-ErrorAction\s+SilentlyContinue"
    r"|\brmtree\([^#\r\n]*\bignore_errors\s*=\s*True")
MARKER_LINES = 5


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def is_harness(path, text):
    """True for a file the gate runs as a check: named as one, or carrying a GATE-STEP marker near its top."""
    head = "\n".join(text.splitlines()[:MARKER_LINES])
    return bool(RE_HARNESS_NAME.match(os.path.basename(path)) or RE_STEP_MARKER.search(head))


def silent_teardowns(text):
    """[(line number, line)] for every recursive delete in the text that swallows its own failure, comment lines
    aside."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if RE_SILENT_TEARDOWN.search(line):
            found.append((number, line.strip()))
    return found


def file_findings(root, path):
    """[(relative path, line number, line)] for every silent teardown in the file when it is a harness."""
    if not path.endswith((".ps1", ".py")):
        return []
    text = read_text(path)
    if not is_harness(path, text):
        return []
    relative = os.path.relpath(path, root).replace("\\", "/")
    return [(relative, number, line) for number, line in silent_teardowns(text)]
