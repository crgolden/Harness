"""Regression harness for invoke_comment_reminder.py. Exits non-zero on any regression.

In-process against reminder(), with a fixture workspace for path resolution and the REAL catalog files for
scope, so a change to the extension map, the exempt list or an allowed pattern shows up here. Every decision
is pinned in both directions: "remind" must return a message naming COMMENTS.md, "silent" must return None.

    python Harness/Hooks/test_comment_reminder.py
    python Harness/Hooks/test_comment_reminder.py --only yaml
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import invoke_comment_reminder as hook  # noqa: E402

WORKSPACE = os.path.join(claude_temp.scratch_dir(), "comment-reminder-workspace")
OUTSIDE = os.path.join(claude_temp.scratch_dir(), "comment-reminder-elsewhere", "Identity", "src", "Program.cs")


def path(repo, relative):
    return os.path.join(WORKSPACE, repo, *relative.split("/"))


def edit(repo, relative, old, new):
    return {"tool_name": "Edit", "tool_response": {},
            "tool_input": {"file_path": path(repo, relative), "old_string": old, "new_string": new}}


def patched_edit(repo, relative, lines):
    return {"tool_name": "Edit",
            "tool_input": {"file_path": path(repo, relative), "old_string": "a", "new_string": "b"},
            "tool_response": {"structuredPatch": [
                {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": lines}]}}


def write_new(repo, relative, content):
    return {"tool_name": "Write", "tool_input": {"file_path": path(repo, relative), "content": content},
            "tool_response": {"type": "create", "structuredPatch": [], "originalFile": None}}


def write_unchanged(repo, relative, content):
    return {"tool_name": "Write", "tool_input": {"file_path": path(repo, relative), "content": content},
            "tool_response": {"type": "update", "structuredPatch": [], "originalFile": content}}


def cases():
    return [
        # --- a comment added in scope: remind ---------------------------------------------------------
        ("remind", "C# line comment", edit("Identity", "src/Program.cs", "var a = 1;", "// explains the loop\nvar a = 1;")),
        ("remind", "C# XML doc", edit("Identity", "src/Program.cs", "void Run() { }", "/// <summary>Runs.</summary>\nvoid Run() { }")),
        ("remind", "trailing comment after a statement", edit("Directory", "src/Api.cs", "Run();", "Run(); // note")),
        ("remind", "TypeScript block comment", edit("Librarian", "src/app/app.ts", "x();", "/* narrates */\nx();")),
        ("remind", "AAA marker followed by text", edit("Identity", "tests/Unit.cs", "", "// Arrange the widget")),
        ("remind", "Python comment via a Write that creates the file", write_new("Curator", "src/app.py", "# narrates\nx = 1\n")),
        ("remind", "PowerShell comment", edit("Functions", "scripts/run.ps1", "", "# explains the step")),
        ("remind", "PowerShell block comment with text", edit("Functions", "scripts/run.ps1", "", "<# explains #>")),
        ("remind", "YAML comment", edit("Identity", ".github/workflows/ci.yml", "", "  # explains the job")),
        ("remind", "YAML trailing comment", edit("Identity", ".github/workflows/ci.yml", "", "  node-version: 22 # pinned")),
        ("remind", "SQL comment outside a migration", edit("Directory", "sql/query.sql", "", "-- explains the join")),
        ("remind", "CSS block comment", edit("Churches", "src/styles.css", "", "/* spacing */")),
        ("remind", "added line in a structured patch", patched_edit("Identity", "src/Program.cs", [" var a = 1;", "+// new note"])),

        # --- nothing new, or an allowed shape: silent -----------------------------------------------------
        ("silent", "code only", edit("Identity", "src/Program.cs", "var a = 1;", "var a = 2;")),
        ("silent", "a comment already in old_string", edit("Identity", "src/Program.cs", "// kept\nvar a = 1;", "// kept\nvar a = 2;")),
        ("silent", "a comment only as patch context", patched_edit("Identity", "src/Program.cs", [" // kept", "-var a = 1;", "+var a = 2;"])),
        ("silent", "a Write that changed nothing", write_unchanged("Curator", "src/app.py", "# old\nx = 1\n")),
        ("silent", "a URL in a string", edit("Librarian", "src/app/api.ts", "", 'const url = "https://example.com/a";')),
        ("silent", "C# preprocessor directive", edit("Identity", "src/Program.cs", "", "#if DEBUG")),
        ("silent", "suppression with its reason", edit("Identity", "src/Program.cs", "", "#pragma warning disable CA1034 // nested by design")),
        ("silent", "eslint suppression", edit("Librarian", "src/app/app.ts", "", "// eslint-disable-next-line no-console -- startup banner")),
        ("silent", "noqa suppression", edit("Curator", "src/app.py", "", "import x  # noqa: F401")),
        ("silent", "bare AAA marker", edit("Identity", "tests/Unit.cs", "", "// Arrange")),
        ("silent", "SHA-pinned action version", edit("Identity", ".github/workflows/ci.yml", "",
                                                    "      - uses: actions/checkout@" + "0" * 40 + " # v4.2.1")),
        ("silent", "tooling directive ts-check", edit("Librarian", "src/app/app.js", "", "// @ts-check")),
        ("silent", "tooling directive Requires", edit("Functions", "scripts/run.ps1", "", "#Requires -Version 7")),
        ("silent", "PowerShell help block delimiters", edit("Functions", "scripts/run.ps1", "", "<#\n.SYNOPSIS\nRuns.\n#>")),
        ("silent", "shebang", write_new("Functions", "scripts/run.sh", "#!/usr/bin/env bash\necho hi\n")),
        ("silent", "numbered migration header", write_new("Curator", "migrations/0042_add_index.sql", "-- why the index\nCREATE INDEX x;\n")),
        ("silent", "exempt repo Tools", edit("Tools", "Claude/x.py", "", "# explains")),
        ("silent", "exempt repo psnpy", edit("psnpy", "src/x.py", "", "# explains")),
        ("silent", "markdown is out of scope", edit("Identity", "README.md", "", "# Heading")),
        ("silent", "a path outside the workspace", {"tool_name": "Edit", "tool_response": {},
                                                   "tool_input": {"file_path": OUTSIDE, "old_string": "", "new_string": "// x"}}),
        ("silent", "not an edit tool", {"tool_name": "PowerShell", "tool_input": {"command": "# x"}}),
    ]


def hook_process_quotes_emdash():
    """The real script in a child with UTF-8 mode off, the condition the live hook runs under."""
    comment = "// explains — the loop"
    payload = {"tool_name": "Edit", "tool_response": {},
               "tool_input": {"file_path": os.path.join(hook.WORKSPACE, "Identity", "src", "Program.cs"),
                              "old_string": "", "new_string": comment}}
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONUTF8"] = "0"
    result = subprocess.run([sys.executable, hook.__file__], input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            capture_output=True, env=env)
    try:
        context = json.loads(result.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
    except Exception:
        return False
    return comment in context


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args()

    started = time.time()
    failures = 0
    ran = 0

    scope = hook.load_scope(hook.TOOLS_ROOT)
    scope_ok = scope is not None and ".cs" in scope[0] and "tools" in scope[1] and len(scope[2]) == 4
    if not args.only or any(o.lower() in "real catalog files parse" for o in args.only):
        ran += 1
        print("{0}  {1:<52} {2}".format("PASS" if scope_ok else "FAIL", "real catalog files parse",
                                        "loaded" if scope else "None"))
        failures += 0 if scope_ok else 1

    if not args.only or any(o.lower() in "utf-8 stdin under the ansi code page" for o in args.only):
        ran += 1
        stdin_ok = hook_process_quotes_emdash()
        print("{0}  {1:<52} {2}".format("PASS" if stdin_ok else "FAIL", "utf-8 stdin under the ansi code page",
                                        "comment quoted intact" if stdin_ok else "comment garbled or missing"))
        failures += 0 if stdin_ok else 1

    for expected, name, payload in cases():
        if args.only and not any(o.lower() in name.lower() for o in args.only):
            continue
        ran += 1
        result = hook.reminder(payload, workspace=WORKSPACE)
        decision = "silent" if result is None else "remind"
        ok = decision == expected and (result is None or "COMMENTS.md" in result)
        print("{0}  {1:<52} {2}".format("PASS" if ok else "FAIL", name, decision))
        if not ok:
            print("        expected={0} got={1}".format(expected, decision))
            failures += 1

    print("")
    print("{0} case(s) in {1:.2f}s".format(ran, time.time() - started))
    if ran == 0:
        print("No cases ran; refusing to report success on an empty selection.")
        return 1
    if failures == 0:
        print("All comment-reminder cases passed.")
        return 0
    print("{0} case(s) failed.".format(failures))
    return 1


if __name__ == "__main__":
    sys.exit(main())
