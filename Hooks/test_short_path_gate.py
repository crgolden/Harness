# GATE-STEP: Short-path hook harness
"""Regression harness for invoke_short_path_gate.py. Exits non-zero on any regression.

    python Harness/Hooks/test_short_path_gate.py

In-process against evaluate(). The real rows make a folder with a long name, ask Windows for its 8.3 spelling and
require a deny naming the long one for every file tool, beside a must-defer control of the long spelling itself. The
planted rows replace long_path, so the server's C:\\Users\\ADMINI~1 shape is judged on any machine, and a must-defer
row covers a tilde segment that expands to nothing different.
"""

import ctypes
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import invoke_short_path_gate as gate  # noqa: E402


def short_path(path):
    """The 8.3 spelling of an existing path, or the path unchanged."""
    if os.name != "nt":
        return path
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer))
    return buffer.value if 0 < length < len(buffer) else path


def planted(expansion):
    def fake(path):
        return expansion.get(path, path)
    return fake


def main():
    started = time.time()
    root = tempfile.mkdtemp(prefix="short-path-gate-", dir=claude_temp.scratch_dir())
    long_folder = os.path.join(root, "AFolderNameLongerThanEight")
    os.makedirs(long_folder)
    long_file = os.path.join(long_folder, "declaration.json")
    short_folder = short_path(long_folder)
    short_file = os.path.join(short_folder, "declaration.json")
    rows = []
    failures = 0
    try:
        if short_folder == long_folder:
            print("FAIL  this volume gives the fixture folder no 8.3 spelling, so the real rows cannot run")
            failures += 1
        else:
            for tool, key in (("Read", "file_path"), ("Write", "file_path"), ("Edit", "file_path"),
                              ("NotebookEdit", "notebook_path"), ("Grep", "path"), ("Glob", "path")):
                rows.append(("deny", "{0} of a file named through its 8.3 folder".format(tool),
                             tool, {key: short_file if key != "path" else short_folder}, None))
            rows.append(("deny", "must-block: Glob whose absolute pattern names the 8.3 folder, with no path",
                         "Glob", {"pattern": short_folder.replace("\\", "/") + "/**/*.json"}, None))
            rows.append(("defer", "control: the same Glob pattern through the long spelling", "Glob",
                         {"pattern": long_folder.replace("\\", "/") + "/**/*.json"}, None))
            rows.append(("deny", "Write of a file not yet written, under an 8.3 folder", "Write",
                         {"file_path": os.path.join(short_folder, "new", "x.json")}, None))
            rows.append(("defer", "control: the same Write through the long spelling", "Write",
                         {"file_path": long_file}, None))
            rows.append(("defer", "control: the same Read through the long spelling", "Read",
                         {"file_path": long_file}, None))
        server = "C:\\Users\\ADMINI~1\\AppData\\Local\\Temp\\claude\\work-products\\s.json"
        server_long = "C:\\Users\\Administrator\\AppData\\Local\\Temp\\claude\\work-products\\s.json"
        rows.append(("deny", "planted: the server's ADMINI~1 TEMP spelling of a declaration", "Write",
                     {"file_path": server}, {os.path.abspath(server): server_long}))
        rows.append(("defer", "planted control: a tilde segment that expands to the same path", "Read",
                     {"file_path": "C:\\data\\REPORT~1.txt"}, {}))
        rows.append(("defer", "control: a name with a tilde that is no 8.3 segment", "Read",
                     {"file_path": "C:\\data\\notes~draft.md"}, {}))
        rows.append(("defer", "control: a shell command is not a file tool", "PowerShell",
                     {"command": "Get-Content " + server}, {}))
        rows.append(("defer", "garbage input never raises", "Write", None, {}))

        original = gate.long_path
        for expected, name, tool, tool_input, expansion in rows:
            gate.long_path = original if expansion is None else planted(expansion)
            try:
                decision, reason = gate.evaluate(tool, tool_input)
                ok = decision == expected
                if ok and decision == gate.DENY:
                    wanted = server_long if expansion else long_folder
                    ok = wanted.lower() in reason.lower()
            except Exception as error:
                decision, ok = "raised {0}: {1}".format(type(error).__name__, error), False
            finally:
                gate.long_path = original
            print("{0}  {1:<66} {2}".format("PASS" if ok else "FAIL", name, decision))
            failures += 0 if ok else 1
    finally:
        claude_temp.remove_fixture_tree(root)
    print("")
    print("{0} case(s) in {1:.2f}s".format(len(rows), time.time() - started))
    if failures == 0:
        print("All short-path-gate cases passed.")
        return 0
    print("{0} case(s) failed.".format(failures))
    return 1


if __name__ == "__main__":
    sys.exit(main())
