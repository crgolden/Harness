# GATE-STEP: Doc-state hook harness
"""Regression harness for invoke_doc_state_gate.py. Run after ANY edit to that gate.

    python Harness\\Hooks\\test_doc_state_gate.py

Calls evaluate() in-process, so it is fast enough for the foreground.

BOTH DIRECTIONS ARE PINNED. A gate that denies every markdown edit passes every must-fire case and
would be turned off within a day; the must-not-fire half is what makes a green run mean something.

THE MUST-NOT-FIRE HALF CARRIES THE THREE CUT CHECKS. Probe rounds over the real corpus killed a
thousand-separator scan (200 hits on MC/DC truth-table rows and vendor quotas), an "N rows" scan
(81 hits on the same tables) and a bare verification-stamp scan (46 hits on the legitimate
"Measured <date>: that theory is wrong" genre). Those shapes are pinned as VALID below so nobody
re-adds them without a failing test to argue with.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import claude_temp  # noqa: E402
import invoke_doc_state_gate  # noqa: E402
from invoke_doc_state_gate import DEFER, DENY, evaluate  # noqa: E402
import session_home  # noqa: E402

WORKSPACE = invoke_doc_state_gate.WORKSPACE
CHECKOUT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_CHECKOUT = session_home.tools_checkout()
FLEET_ROOT = os.environ.get("CRGOLDEN_WORKSPACE")

TOOLS_DOCS = (("SearchConsole", "AGENTS.md"), ("PlayStation", "LIFECYCLE_AUDIT.md"))
FLEET_DOCS = (("AGENTS", "RETIRED", "AZURE_MONITOR.md"), ("AGENTS", "COVERAGE", "Identity.md"),
              (".claude", "skills", "playwright-cli", "SKILL.md"))

DOC = os.path.join(WORKSPACE, "Tools", "SearchConsole", "AGENTS.md")
AUDIT_LEDGER = os.path.join(WORKSPACE, "Tools", "PlayStation", "LIFECYCLE_AUDIT.md")
RETIRED_DOC = session_home.workspace_doc("AGENTS/RETIRED/AZURE_MONITOR.md", WORKSPACE, must_exist=False)
COVERAGE_DOC = session_home.workspace_doc("AGENTS/COVERAGE/Identity.md", WORKSPACE, must_exist=False)
CODE_FILE = os.path.join(WORKSPACE, "Tools", "Hooks", "invoke_doc_state_gate.py")
WORKSPACE_SKILL_DOC = os.path.join(WORKSPACE, ".claude", "skills", "playwright-cli", "SKILL.md")
HANDOFF = os.path.join(claude_temp.handoffs_root(), "session", "HANDOFF_001.md")
AUDIT_RUN_REPORT = os.path.join(claude_temp.audits_root(), "docs-audit", "run", "report.md")
TOOLS_PARKING_NOTE = os.path.join(invoke_doc_state_gate.claude_temp.claude_root(), "parking-lot", "session", "notes.md")
TOOLS_SKILL_DOC = os.path.join(invoke_doc_state_gate.TOOLS_CHECKOUT, "Claude", "skills", "parking-lot", "SKILL.md")

# The exact sentences that produced this gate, from Tools/SearchConsole/AGENTS.md on 2026-09-10.
REAL_VIOLATIONS = [
    ("the heading that shipped the status",
     "## Why Churches shows 245K unindexed (diagnosed 2026-09-07; crawl path FIXED and live 2026-09-10)"),
    ("STILL OPEN plus a verification stamp",
     "- **crgolden.com has no sitemap live, and no canonical. STILL OPEN - re-verified 2026-09-10.**"),
    ("a pasted Last-Modified header",
     "The deployed site was rebuilt that morning (Last-Modified: Thu, 10 Sep 2026 11:56:50 GMT)."),
    ("DONE beside a date",
     "~~**Fix /churches pagination**~~ **DONE, live 2026-09-10.** The paginator is anchors now."),
]

# Shapes that must stay legal. Every one appears in the real corpus.
VALID_PROSE = [
    ("a dated measurement anchoring a durable finding",
     "**Measured 2026-08-19: that theory is wrong.** The variable is present in the process."),
    ("a vendor's documented constant",
     "Google documents the concern for sites of 1M+ pages changing weekly, or 10,000+ daily."),
    ("lowercase narrative history",
     "That shipped on 2026-08-30 and returned 400 for every write in production."),
    ("the CLOSED design-log convention in the hook docs",
     "- **CLOSED 2026-09-09 - the commit-block veto rewarded a pasted block.**"),
    ("the word done in ordinary prose",
     "That has not been done. They were proven to discriminate on 2026-09-07 by swapping the getters."),
    ("CUT CHECK: a thousand-separated count in a truth table",
     "| 2 | reader yields 50,000 rows (homepage + rows = 50,001) | chunk 1 flushed at 50,000 |"),
    ("CUT CHECK: an N-rows reading in a truth table",
     "| 1 | conn open, reader yields 0 rows | logs no suggestions | true, empty scan |"),
    ("CUT CHECK: a coverage baseline percentage",
     "Baseline (2026-06-12, unit only, generated code excluded): 30.1% line / 26.2% branch."),
    ("a command that re-derives the state",
     "Read the pins from origin with git -C <consumer> show origin/<branch>:package.json."),
    ("a deploy mechanism described without asserting a state",
     "It uses a guard to skip the restart if the container is not yet deployed by the compose file."),
]


CURLY_QUOTED_STATUS = "The operator asked “is it DONE as of 2026-09-10?” and the answer was no."


def hook_process_decision(text):
    """The real script in a child with UTF-8 mode off, the condition the live hook runs under."""
    payload = {"tool_input": {"file_path": DOC, "new_string": text, "old_string": ""}}
    env = {key: value for key, value in os.environ.items() if key not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONUTF8"] = "0"
    result = subprocess.run([sys.executable, invoke_doc_state_gate.__file__],
                            input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), capture_output=True, env=env)
    return DENY if '"permissionDecision":"deny"' in result.stdout.decode("utf-8", "replace") else DEFER


def main():
    for parts in TOOLS_DOCS:
        if not os.path.isfile(os.path.join(TOOLS_CHECKOUT, *parts)):
            print("FAIL fixture missing from this checkout: {0}".format(os.path.join(TOOLS_CHECKOUT, *parts)))
            return 1
    if FLEET_ROOT:
        for parts in FLEET_DOCS:
            if not os.path.isfile(os.path.join(FLEET_ROOT, *parts)):
                print("FAIL fixture missing from the crgolden workspace: {0}".format(os.path.join(FLEET_ROOT, *parts)))
                return 1
    elif os.environ.get("TOOLS_GATE_STANDALONE") == "1":
        print("SKIPPED the crgolden exempt docs exist: a standalone run has no crgolden workspace")
    else:
        print("FAIL no crgolden workspace: set CRGOLDEN_WORKSPACE (gate.ps1 -Workspace does) or run gate.ps1 -Standalone")
        return 1

    # A hook run from a worktree lives outside the workspace, so the gate resolves its main checkout's folder.
    if os.path.normcase(invoke_doc_state_gate.WORKSPACE) != os.path.normcase(
            os.environ.get("CRGOLDEN_WORKSPACE") or os.path.dirname(session_home.main_checkout(CHECKOUT))) \
            or not os.path.isdir(os.path.join(invoke_doc_state_gate.WORKSPACE, "Tools", "Claude", "skills")):
        print("FAIL the gate's WORKSPACE is not the folder holding the main Tools checkout: {0}".format(
            invoke_doc_state_gate.WORKSPACE))
        return 1

    cases = []

    for label, text in REAL_VIOLATIONS:
        cases.append(("must-fire: " + label, DOC, text, None, DENY))

    for label, text in VALID_PROSE:
        cases.append(("must-not-fire: " + label, DOC, text, None, DEFER))

    cases += [
        ("must-fire: my own uncommitted git state",
         DOC, "The remedy exists in the **uncommitted working tree** and is not committed.", None, DENY),
        ("must-fire: has not shipped",
         DOC, "The portfolio rework carrying them has not shipped.", None, DENY),
        ("must-fire: a heading asserting a present condition",
         DOC, "### The catalog is currently failing", None, DENY),

        # Code context: every pattern fires inside an example if fences are not honoured.
        ("must-not-fire: a violation inside a fenced block",
         DOC, "Run it like this:\n\n```\ncurl -I / | grep Last-Modified: Thu\n```\n", None, DEFER),
        ("must-not-fire: a status token inside inline code",
         DOC, "The report prints `DONE 2026-09-10` in its summary column.", None, DEFER),
        ("must-not-fire: a status token inside a quoted string",
         DOC, "The operator asked \"is it DONE as of 2026-09-10?\" and the answer was no.", None, DEFER),

        # Exemptions: docs whose content IS status.
        ("must-not-fire: an audit ledger",
         AUDIT_LEDGER, "- **DONE 2026-09-10.** Shipped and verified.", None, DEFER),
        ("must-not-fire: a decommission inventory",
         RETIRED_DOC, "**DONE 2026-08-30: all four variables are deleted.**", None, DEFER),
        ("must-not-fire: a coverage baseline",
         COVERAGE_DOC, "**Seam (DONE, 2026-06-12):** the handlers were internal.", None, DEFER),
        ("must-not-fire: a session handoff under %TEMP%\\claude",
         HANDOFF, "- **DONE 2026-09-10.** Identity gate passed; Tools gate STILL OPEN.", None, DEFER),
        ("must-not-fire: an audit run report under %TEMP%\\claude",
         AUDIT_RUN_REPORT, "- **DONE 2026-09-10.** Ledger reconciled.", None, DEFER),
        ("must-fire: a workspace-scoped skill doc beside those session-state roots",
         WORKSPACE_SKILL_DOC, "- **DONE 2026-09-10.** Shipped and verified.", None, DENY),
        ("must-not-fire: a note in a session's parking lot under %TEMP%\\claude",
         TOOLS_PARKING_NOTE, "- **DONE 2026-09-10.** Ledger reconciled.", None, DEFER),
        ("must-fire: a skill doc in the Tools checkout beside its session-state roots",
         TOOLS_SKILL_DOC, "- **DONE 2026-09-10.** Shipped and verified.", None, DENY),

        # Scope.
        ("must-not-fire: a non-markdown file",
         CODE_FILE, "# DONE 2026-09-10 and live in production", None, DEFER),
        ("must-not-fire: empty replacement text",
         DOC, "   ", None, DEFER),
        ("must-not-fire: ordinary durable prose",
         DOC, "robots.txt rules apply only to the host, protocol and port serving that file.", None, DEFER),
    ]

    with tempfile.TemporaryDirectory(dir=claude_temp.scratch_dir()) as workdir:
        # An Edit whose new_string sits inside a fence opened earlier in the FILE cannot be judged
        # from the fragment alone. The gate reconstructs fence state from the file; pin both halves.
        fenced = os.path.join(workdir, "fenced.md")
        with open(fenced, "w", encoding="utf-8") as handle:
            handle.write("# Example\n\n```\nsome sample\nANCHOR-INSIDE\nmore sample\n```\n\n"
                         "Prose here.\n\nANCHOR-OUTSIDE\n")
        cases += [
            ("must-not-fire: Edit landing inside a fence opened earlier in the file",
             fenced, "DONE 2026-09-10", "ANCHOR-INSIDE", DEFER),
            ("must-fire: Edit landing in prose after that fence closed",
             fenced, "DONE 2026-09-10", "ANCHOR-OUTSIDE", DENY),
        ]

        failures = 0
        for label, path, text, old_string, expected in cases:
            decision, reason = evaluate(path, text, old_string)
            ok = decision == expected
            if not ok:
                failures += 1
            print("{0} {1} -> {2} (expected {3})".format(
                "PASS" if ok else "FAIL", label, decision, expected))
            if decision == DENY and not reason:
                failures += 1
                print("FAIL {0} -> denied with an empty reason".format(label))

    decision, _ = evaluate(DOC, CURLY_QUOTED_STATUS)
    failures += 0 if decision == DEFER else 1
    print("{0} must-not-fire: a status token inside curly quotes, in-process -> {1} (expected {2})".format(
        "PASS" if decision == DEFER else "FAIL", decision, DEFER))
    decision = hook_process_decision(CURLY_QUOTED_STATUS)
    failures += 0 if decision == DEFER else 1
    print("{0} must-not-fire: curly quotes through UTF-8 stdin under the ANSI code page -> {1} (expected {2})".format(
        "PASS" if decision == DEFER else "FAIL", decision, DEFER))

    print("\n{0} case(s) failed".format(failures) if failures else "\nall cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
