# GATE-STEP: Doc-prose hook harness
"""Regression harness for invoke_doc_prose_reminder.py. Exits non-zero on any regression.

In-process against reminder(), with the REAL prose_lint.py loaded, so a move or a rename of the docs-audit
linter fails here instead of silently dropping the tells. Every decision is pinned in both directions: "remind"
must return a message naming the rule file, "silent" must return None. "tells" additionally requires the
prose_lint section, "plain" requires its absence.

    python Harness/Hooks/test_doc_prose_reminder.py
    python Harness/Hooks/test_doc_prose_reminder.py --only memory
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import invoke_doc_prose_reminder as hook  # noqa: E402

from invoke_doc_state_gate import WORKSPACE  # noqa: E402
HOME = os.path.expanduser("~")
TEMP = tempfile.gettempdir()


def doc(*parts):
    return os.path.join(WORKSPACE, *parts)


def edit(file_path, old, new):
    return {"tool_name": "Edit", "tool_response": {},
            "tool_input": {"file_path": file_path, "old_string": old, "new_string": new}}


def write_new(file_path, content):
    return {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content},
            "tool_response": {"type": "create", "structuredPatch": [], "originalFile": None}}


def write_unchanged(file_path, content):
    return {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content},
            "tool_response": {"type": "update", "structuredPatch": [], "originalFile": content}}


def patched_edit(file_path, lines):
    return {"tool_name": "Edit",
            "tool_input": {"file_path": file_path, "old_string": "a", "new_string": "b"},
            "tool_response": {"structuredPatch": [
                {"oldStart": 1, "oldLines": 1, "newStart": 1, "newLines": 1, "lines": lines}]}}


def cases():
    fleet = doc("AGENTS", "GATES.md")
    repo_guide = doc("AGENTS", "REPOS", "Identity.md")
    retired = doc("AGENTS", "RETIRED", "AZURE_MONITOR.md")
    repo_readme = doc("Identity", "README.md")
    return [
        # --- prose added to a permanent doc: remind -------------------------------------------------------
        ("tells", "a dated history line in a fleet doc",
         edit(fleet, "", "The rule was added on 2026-09-19 after the gate broke.")),
        ("tells", "a used-to tell in a repo guide",
         edit(repo_guide, "", "The handler used to push by default.")),
        ("tells", "a first-person story in a RETIRED doc",
         edit(retired, "", "I found the bug while reading the diff.")),
        ("plain", "a clean rule still gets the reminder",
         edit(fleet, "", "Run the gate from the repo root, never the workspace root.")),
        ("plain", "a repo README is permanent",
         write_new(repo_readme, "# Identity\n\nThe OIDC provider for the fleet.\n")),
        ("plain", "the global CLAUDE.md is permanent",
         edit(os.path.join(HOME, ".claude", "CLAUDE.md"), "", "Use python, never python3.")),
        ("tells", "a workspace-scoped skill doc is permanent",
         edit(doc(".claude", "skills", "playwright-cli", "SKILL.md"), "", "The flag used to be required.")),
        ("tells", "an added line in a structured patch",
         patched_edit(fleet, [" kept context", "+See commit b8906c1 for the removal."])),

        # --- nothing new, not prose, or not permanent: silent ------------------------------------------------
        ("silent", "prose only as patch context",
         patched_edit(fleet, [" The handler used to push.", "-old", "+"])),
        ("silent", "a Write that changed nothing", write_unchanged(fleet, "Unchanged.\n")),
        ("silent", "only a fenced code block added",
         edit(fleet, "", "```powershell\ngit log --since 2026-09-19\n```")),
        ("silent", "the session scratchpad",
         write_new(os.path.join(TEMP, "claude", "slug", "session", "scratchpad", "notes.md"),
                   "Measured on 2026-09-19: 12 hits.\n")),
        ("silent", "anywhere under the temp tree", write_new(os.path.join(TEMP, "progress.md"), "DONE step 3.\n")),
        ("silent", "a plan file", write_new(os.path.join(HOME, ".claude", "plans", "plan.md"), "Step one.\n")),
        ("silent", "a memory file",
         write_new(os.path.join(HOME, ".claude", "projects", "slug", "memory", "fact.md"), "A fact.\n")),
        ("silent", "a session handoff under %TEMP%\\claude",
         write_new(os.path.join(hook.claude_temp.handoffs_root(), "session", "HANDOFF_001.md"),
                   "Measured on 2026-09-19: the gate used to pass.\n")),
        ("silent", "an audit run report under %TEMP%\\claude",
         write_new(os.path.join(hook.claude_temp.audits_root(), "docs-audit", "run", "report.md"),
                   "Reconciled on 2026-09-19.\n")),
        ("silent", "a status-bearing ledger by name",
         edit(doc("AGENTS", "CONFIG_AUDIT.md"), "", "Rotated on 2026-09-19.")),
        ("silent", "a changelog by prefix", edit(doc("Tools", "CHANGELOG.md"), "", "Fixed on 2026-09-19.")),
        ("silent", "not markdown", edit(doc("Tools", "Claude", "x.py"), "", "# used to push")),
        ("silent", "not an edit tool", {"tool_name": "PowerShell", "tool_input": {"command": "x"}}),
    ]


def hook_process_sees_emdash():
    """Claude Code pipes UTF-8 into a hook whose stdin defaults to the ANSI code page, so the check runs the
    real script in a child with UTF-8 mode off, the condition the live hook runs under."""
    payload = {"tool_name": "Edit", "tool_response": {},
               "tool_input": {"file_path": doc("AGENTS", "GATES.md"), "old_string": "",
                              "new_string": "The proxy — the Express one — strips it."}}
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONUTF8"] = "0"
    result = subprocess.run([sys.executable, hook.__file__], input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            capture_output=True, env=env)
    return "[emdash]" in result.stdout.decode("utf-8", "replace")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args()

    started = time.time()
    failures = 0
    ran = 0

    scan_line = hook.load_lint()
    lint_ok = scan_line is not None and bool(scan_line("The handler used to push."))
    if not args.only or any(o.lower() in "real prose_lint loads" for o in args.only):
        ran += 1
        print("{0}  {1:<52} {2}".format("PASS" if lint_ok else "FAIL", "real prose_lint loads",
                                        "loaded" if scan_line else "None"))
        failures += 0 if lint_ok else 1

    if not args.only or any(o.lower() in "utf-8 stdin under the ansi code page" for o in args.only):
        ran += 1
        stdin_ok = hook_process_sees_emdash()
        print("{0}  {1:<52} {2}".format("PASS" if stdin_ok else "FAIL", "utf-8 stdin under the ansi code page",
                                        "emdash quoted" if stdin_ok else "emdash lost"))
        failures += 0 if stdin_ok else 1

    for expected, name, payload in cases():
        if args.only and not any(o.lower() in name.lower() for o in args.only):
            continue
        ran += 1
        result = hook.reminder(payload, scan_line)
        if result is None:
            got = "silent"
        else:
            got = "tells" if hook.TELLS_HEADER in result else "plain"
        ok = got == expected and (result is None or hook.RULE_FILE in result)
        print("{0}  {1:<52} {2}".format("PASS" if ok else "FAIL", name, got))
        if not ok:
            print("        expected={0} got={1}".format(expected, got))
            failures += 1

    print("")
    print("{0} case(s) in {1:.2f}s".format(ran, time.time() - started))
    if ran == 0:
        print("No cases ran; refusing to report success on an empty selection.")
        return 1
    if failures == 0:
        print("All doc-prose-reminder cases passed.")
        return 0
    print("{0} case(s) failed.".format(failures))
    return 1


if __name__ == "__main__":
    sys.exit(main())
