# GATE-STEP: AGENTS-doc hook harness
"""Regression harness for invoke_agents_doc_gate.py. Run after ANY edit to that gate.

    python Harness\\Hooks\\test_agents_doc_gate.py

Calls evaluate() in-process, so it is fast enough to run in the foreground.

BOTH DIRECTIONS ARE PINNED ON PURPOSE. A gate that denies everything passes every must-fire case and
is useless; a gate that denies nothing passes every must-not-fire case and is worse than useless
because it looks installed. The must-not-fire half is what makes a green run mean something -- see
CODE-STYLE.md, "a check must discriminate".
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from invoke_agents_doc_gate import DEFER, DENY, evaluate, find_workspace_root  # noqa: E402
import claude_temp  # noqa: E402
import session_home  # noqa: E402

GUIDE_LINES = 150


def fixture_workspace(root):
    """A workspace of three repos (Identity, Curator, Tools) whose docs folder holds guides for the first two only,
    Identity's GUIDE_LINES long, so the gate is judged against docs this harness owns, never the caller's."""
    for repo in ("Identity", "Curator", "Tools"):
        os.makedirs(os.path.join(root, repo, ".git"))
    guides = os.path.join(root, "AGENTS", "REPOS")
    os.makedirs(guides)
    with open(os.path.join(guides, "Identity.md"), "w", encoding="utf-8") as handle:
        handle.write("".join("line {0}\n".format(number) for number in range(1, GUIDE_LINES + 1)))
    with open(os.path.join(guides, "Curator.md"), "w", encoding="utf-8") as handle:
        handle.write("# Curator\n")
    return root


def build_transcript(directory, name, tool_uses):
    """Write a JSONL transcript containing the given tool_use blocks, one record each.

    An entry carrying `__record__` is written verbatim instead, which is how a compaction boundary
    gets into a fixture: it is a top-level flag on a user record, not a tool_use block.
    """
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        for block in tool_uses:
            if "__record__" in block:
                handle.write(json.dumps(block["__record__"]) + "\n")
                continue
            handle.write(json.dumps({
                "type": "assistant",
                "message": {"role": "assistant", "content": [block]},
            }) + "\n")
    return path


def compaction_block():
    """A context compaction, shaped as the real transcript writes it."""
    return {"__record__": {
        "type": "user",
        "isCompactSummary": True,
        "message": {"role": "user", "content": "(compacted summary)"},
    }}


def read_block(file_path, **extra):
    tool_input = {"file_path": file_path}
    tool_input.update(extra)
    return {"type": "tool_use", "name": "Read", "input": tool_input}


def grep_block(path):
    return {
        "type": "tool_use",
        "name": "Grep",
        "input": {"pattern": "home page", "path": path},
    }


def main():
    with tempfile.TemporaryDirectory(dir=claude_temp.scratch_dir()) as workdir:
        workspace = fixture_workspace(os.path.join(workdir, "workspace"))
        identity_doc = session_home.workspace_doc("AGENTS/REPOS/Identity.md", workspace)
        identity_file = os.path.join(workspace, "Identity", "Identity", "Pages", "Index.cshtml")
        curator_file = os.path.join(workspace, "Curator", "app", "main.py")
        tools_file = os.path.join(workspace, "Tools", "Hooks", "invoke_agents_doc_gate.py")
        outside_file = os.path.join(workdir, "elsewhere", "scratch", "notes.md")
        identity_lines = GUIDE_LINES

        empty = build_transcript(workdir, "empty.jsonl", [])
        full_read = build_transcript(workdir, "full.jsonl", [read_block(identity_doc)])
        limited = build_transcript(workdir, "limited.jsonl", [read_block(identity_doc, limit=1)])
        offsetted = build_transcript(workdir, "offset.jsonl", [read_block(identity_doc, offset=40)])
        grepped = build_transcript(workdir, "grep.jsonl", [grep_block(identity_doc)])
        wrong_doc = build_transcript(workdir, "wrong.jsonl", [read_block(identity_doc)])
        missing = os.path.join(workdir, "does-not-exist.jsonl")

        # The real 2026-09-01 shape: three chunks that stop short of the end.
        short_chunks = build_transcript(workdir, "short.jsonl", [
            read_block(identity_doc, limit=36),
            read_block(identity_doc, offset=36, limit=60),
            read_block(identity_doc, offset=95, limit=identity_lines - 95),
        ])
        # Same shape, carried to the last line.
        covering_chunks = build_transcript(workdir, "covering.jsonl", [
            read_block(identity_doc, limit=36),
            read_block(identity_doc, offset=36, limit=60),
            read_block(identity_doc, offset=95, limit=identity_lines),
        ])
        # Chunks that skip the middle must not add up to coverage.
        gapped_chunks = build_transcript(workdir, "gapped.jsonl", [
            read_block(identity_doc, limit=20),
            read_block(identity_doc, offset=identity_lines // 2, limit=identity_lines),
        ])
        # An open-ended tail read that starts past line 1 leaves the head unread.
        tail_only = build_transcript(workdir, "tail.jsonl", [read_block(identity_doc, offset=100)])

        read_then_compacted = build_transcript(workdir, "compacted.jsonl", [
            read_block(identity_doc),
            compaction_block(),
        ])
        read_then_compacted_twice = build_transcript(workdir, "compacted-twice.jsonl", [
            read_block(identity_doc),
            compaction_block(),
            read_block(identity_doc, offset=identity_lines - 10, limit=10),
            compaction_block(),
        ])
        chunks_then_compacted = build_transcript(workdir, "chunks-compacted.jsonl", [
            read_block(identity_doc, limit=36),
            read_block(identity_doc, offset=36, limit=identity_lines),
            compaction_block(),
        ])
        compacted_then_read = build_transcript(workdir, "recovered.jsonl", [
            read_block(identity_doc),
            compaction_block(),
            read_block(identity_doc),
        ])

        # An open-ended read and a bounded one starting on the same line: sorting the ranges compares their
        # ends, one of which is None.
        same_start_gapped = build_transcript(workdir, "same-start-gapped.jsonl", [
            read_block(identity_doc, limit=40),
            read_block(identity_doc, offset=51),
            read_block(identity_doc, offset=51, limit=10),
        ])
        same_start_covering = build_transcript(workdir, "same-start-covering.jsonl", [
            read_block(identity_doc, limit=50),
            read_block(identity_doc, offset=51),
            read_block(identity_doc, offset=51, limit=10),
        ])

        cases = [
            # (label, file_path, transcript, expected)
            ("must-fire: a hole before an open-ended and a bounded read sharing a start", identity_file,
             same_start_gapped, DENY),
            ("must-not-fire: an open-ended and a bounded read sharing a start, no hole", identity_file,
             same_start_covering, DEFER),
            ("must-fire: edit Identity with no read", identity_file, empty, DENY),
            ("must-fire: a Grep of the doc is not a read", identity_file, grepped, DENY),
            ("must-fire: a single limited Read", identity_file, limited, DENY),
            ("must-fire: a single offset Read", identity_file, offsetted, DENY),
            ("must-fire: chunks that stop short (the real 2026-09-01 shape)", identity_file, short_chunks, DENY),
            ("must-fire: chunks with a hole in the middle", identity_file, gapped_chunks, DENY),
            ("must-fire: open-ended read starting past line 1", identity_file, tail_only, DENY),
            ("must-fire: reading Identity.md does not unlock Curator", curator_file, wrong_doc, DENY),
            ("must-fire: a full read that a later compaction summarised away", identity_file, read_then_compacted, DENY),
            ("must-fire: two compactions after the read (the real 2026-09-08 shape)", identity_file, read_then_compacted_twice, DENY),
            ("must-fire: chunked coverage destroyed by a compaction", identity_file, chunks_then_compacted, DENY),
            ("must-not-fire: re-read after the compaction restores the credit", identity_file, compacted_then_read, DEFER),
            ("must-not-fire: full read of the repo doc", identity_file, full_read, DEFER),
            ("must-not-fire: chunked reads that cover the file", identity_file, covering_chunks, DEFER),
            ("must-not-fire: AGENTS/ itself has no AGENTS/REPOS/AGENTS.md", identity_doc, empty, DEFER),
            ("must-not-fire: Tools has no AGENTS/REPOS/Tools.md", tools_file, empty, DEFER),
            ("must-not-fire: path outside the workspace", outside_file, empty, DEFER),
            ("must-not-fire: unreadable transcript fails open", identity_file, missing, DEFER),
        ]

        failures = 0
        for label, file_path, transcript, expected in cases:
            try:
                decision, reason = evaluate(file_path, workspace, transcript, workspace)
            except Exception as error:
                decision, reason = "raised {0}".format(type(error).__name__), ""
            ok = decision == expected
            if not ok:
                failures += 1
            print("{0} {1} -> {2} (expected {3})".format(
                "PASS" if ok else "FAIL", label, decision, expected))
            if decision == DENY and not reason:
                failures += 1
                print("FAIL {0} -> denied with an empty reason".format(label))

    failures += workspace_resolver_failures()
    print("\n{0} case(s) failed".format(failures) if failures else "\nall cases passed")
    return 1 if failures else 0


def workspace_resolver_failures():
    """session_home.workspace_root(), which this gate and every other Tools site reads instead of a hardcoded
    workspace path: CRGOLDEN_WORKSPACE wins, else the folder the MAIN Tools checkout sits in, found from a worktree's
    .git file; a clone anywhere else resolves to its own parent; a .git file that is not a worktree's (a submodule)
    leaves the checkout as it is."""
    with tempfile.TemporaryDirectory(dir=claude_temp.scratch_dir()) as root:
        root = os.path.realpath(root)
        main_tools = os.path.join(root, "apps", "Tools")
        os.makedirs(os.path.join(main_tools, ".git", "worktrees", "wt"))
        worktree = os.path.join(root, "worktrees", "wt", "Tools")
        os.makedirs(worktree)
        with open(os.path.join(worktree, ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: " + os.path.join(main_tools, ".git", "worktrees", "wt") + "\n")
        relative = os.path.join(root, "relative", "Tools")
        os.makedirs(relative)
        with open(os.path.join(relative, ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: " + os.path.relpath(os.path.join(main_tools, ".git", "worktrees", "wt"), relative))
        submodule = os.path.join(root, "host", "Tools")
        os.makedirs(submodule)
        with open(os.path.join(submodule, ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: ../.git/modules/Tools\n")
        elsewhere = os.path.join(root, "elsewhere", "Tools")
        os.makedirs(os.path.join(elsewhere, ".git"))
        apps = os.path.join(root, "apps")
        identity = os.path.join(apps, "Identity", "Program.cs")
        guide = os.path.join(apps, "AGENTS", "REPOS", "Identity.md")
        os.makedirs(os.path.dirname(guide))
        with open(guide, "w", encoding="utf-8") as handle:
            handle.write("# Identity\n")

        def refused(relative):
            try:
                session_home.workspace_doc(relative, apps)
            except ValueError:
                return "refused"
            return "resolved"

        rows = [
            ("must-resolve: a workspace doc that exists resolves under the workspace",
             session_home.workspace_doc("AGENTS/REPOS/Identity.md", apps), guide),
            ("control: a workspace doc that does not exist resolves to nothing",
             session_home.workspace_doc("AGENTS/REPOS/Curator.md", apps), None),
            ("must-resolve: a writer creating a doc gets its path though it does not exist",
             session_home.workspace_doc("AGENTS.md", apps, must_exist=False), os.path.join(apps, "AGENTS.md")),
            ("must-refuse: a parent step out of the docs folder", refused("AGENTS/../Identity/Program.cs"), "refused"),
            ("must-refuse: a path that is no fleet doc", refused("Identity/Program.cs"), "refused"),
            ("must-refuse: a file below AGENTS.md, which is a file", refused("AGENTS.md/x.md"), "refused"),
            ("must-resolve: CRGOLDEN_WORKSPACE wins over the checkout's location",
             session_home.workspace_root(main_tools, {"CRGOLDEN_WORKSPACE": os.path.join(root, "set")}),
             os.path.join(root, "set")),
            ("must-resolve: a main checkout resolves to the folder it sits in",
             session_home.workspace_root(main_tools, {}), apps),
            ("must-resolve: a worktree resolves to its main checkout's folder, not its own",
             session_home.workspace_root(worktree, {}), apps),
            ("must-resolve: a relative gitdir in a worktree's .git file resolves the same way",
             session_home.workspace_root(relative, {}), apps),
            ("control: a submodule's .git file is no worktree, so the checkout's own folder stands",
             session_home.workspace_root(submodule, {}), os.path.join(root, "host")),
            ("must-resolve: a Tools clone elsewhere resolves to its own parent, needing no workspace",
             session_home.workspace_root(elsewhere, {}), os.path.join(root, "elsewhere")),
            ("must-resolve: the live default is the folder holding the main Tools checkout",
             os.path.isdir(os.path.join(session_home.workspace_root(environ={}), "Tools", "Hooks")), True),
            ("must-find: a path in the resolved workspace finds it",
             find_workspace_root(identity, None, apps), apps),
            ("control: a path outside the resolved workspace finds nothing",
             find_workspace_root(os.path.join(root, "elsewhere", "x.cs"), None, apps), None),
        ]
    failures = 0
    for label, got, expected in rows:
        ok = os.path.normcase(got) == os.path.normcase(expected) if isinstance(expected, str) else got == expected
        print("{0} {1} -> {2!r}".format("PASS" if ok else "FAIL", label, got))
        failures += 0 if ok else 1
    return failures


if __name__ == "__main__":
    sys.exit(main())
