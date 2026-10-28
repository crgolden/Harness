# GATE-STEP: Protected-path gate harness
"""Regression harness for invoke_protected_path_gate.py. Exits non-zero on any regression.

    python Harness/Hooks/test_protected_path_gate.py

The master session's Write, Edit and NotebookEdit to any path Claude Code protects (its protected folders and files)
are denied, in any case or spelling, except Claude's own worktrees and the session's own project memory markdown;
every other session, the master on another machine, a read, a .github folder and a name that merely ends in a
protected one all defer.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import invoke_protected_path_gate as gate  # noqa: E402
import master_session  # noqa: E402

failures = []
PROFILE = os.environ.get("USERPROFILE", "C:\\Users\\fixture")
MASTER = {master_session.MASTER_SESSION_VARIABLE: "1", "COMPUTERNAME": master_session.MASTER_HOST}
OTHER_SESSION = {"COMPUTERNAME": master_session.MASTER_HOST}
MASTER_ELSEWHERE = {master_session.MASTER_SESSION_VARIABLE: "1", "COMPUTERNAME": "DESKTOP-FIXTURE"}
MEMORY = os.path.join(PROFILE, ".claude", "projects", "C--Users-Administrator-source-repos-crgolden", "memory",
                      "fixture-note.md")
REPOS = os.path.join(PROFILE, "source", "repos", "crgolden")


def record(name, ok, detail=""):
    print("{0}  {1}{2}".format("PASS" if ok else "FAIL", name, "" if ok else "  got: " + str(detail)))
    if not ok:
        failures.append(name)


def decide(tool, path, environ, key="file_path"):
    return gate.evaluate(tool, {key: path, "content": "x"}, environ)


cases = (
    ("must-block: the master's Write to its own memory folder", "Write", MEMORY, MASTER, gate.DENY),
    ("must-block: the master's Edit under ~/.claude", "Edit", os.path.join(PROFILE, ".claude", "settings.json"),
     MASTER, gate.DENY),
    ("must-block: the master's Write inside a repo's .git", "Write",
     os.path.join(REPOS, "Tools", ".git", "fixture"), MASTER, gate.DENY),
    ("must-block: the master's Write inside a .vscode folder", "Write",
     os.path.join(REPOS, "Tools", ".vscode", "settings.json"), MASTER, gate.DENY),
    ("must-block: the master's Write inside an .idea folder", "Write",
     os.path.join(REPOS, "Tools", ".idea", "workspace.xml"), MASTER, gate.DENY),
    ("must-block: the master's Write inside a .husky folder", "Write",
     os.path.join(REPOS, "Inventory", ".husky", "pre-commit"), MASTER, gate.DENY),
    ("must-block: the folder name matches in any case", "Write",
     os.path.join(PROFILE, ".Claude", "projects", "fixture.md"), MASTER, gate.DENY),
    ("must-block: the workspace's own .claude folder", "Write", os.path.join(REPOS, ".claude", "settings.local.json"),
     MASTER, gate.DENY),
    ("control: another session's Write to the same memory folder defers", "Write", MEMORY, OTHER_SESSION,
     gate.DEFER),
    ("control: MASTER_SESSION on another machine is not the master", "Write", MEMORY, MASTER_ELSEWHERE, gate.DEFER),
    ("control: the master's Write to an ordinary repo file defers", "Write", os.path.join(REPOS, "Tools", "README.md"),
     MASTER, gate.DEFER),
    ("control: the master's Write inside .github defers (an allow rule covers it)", "Write",
     os.path.join(REPOS, "Tools", ".github", "workflows", "ci.yml"), MASTER, gate.DEFER),
    ("control: a folder that only ends in .claude is not one", "Write",
     os.path.join(REPOS, "notes.claude", "x.md"), MASTER, gate.DEFER),
    ("control: a file named .claude is not a folder inside one", "Write", os.path.join(REPOS, "Tools", ".claude"),
     MASTER, gate.DEFER),
    ("control: the master's Read under ~/.claude defers (reads pass the allow rules)", "Read", MEMORY, MASTER,
     gate.DEFER),
)
for name, tool, path, environ, expected in cases:
    decision, reason = decide(tool, path, environ)
    record(name, decision == expected, (decision, reason[:120]))

WORKTREE = os.path.join(PROFILE, "source", "worktrees", "fixture-session", "Tools")
documented = (
    ("must-block: a write under .cargo", os.path.join(REPOS, "Tools", ".cargo", "config.toml")),
    ("must-block: a write under .devcontainer", os.path.join(REPOS, "Tools", ".devcontainer", "devcontainer.json")),
    ("must-block: a write under .yarn", os.path.join(REPOS, "Inventory", ".yarn", "releases", "yarn.cjs")),
    ("must-block: a write under .mvn", os.path.join(REPOS, "Tools", ".mvn", "wrapper", "x.jar")),
    ("must-block: a write under .config\\git", os.path.join(PROFILE, ".config", "git", "config")),
    ("must-block: a worktree's .git file, the pointer to its repository", os.path.join(WORKTREE, ".git")),
    ("must-block: .gitconfig", os.path.join(PROFILE, ".gitconfig")),
    ("must-block: .npmrc", os.path.join(REPOS, "Inventory", ".npmrc")),
    ("must-block: .mcp.json", os.path.join(REPOS, "Tools", ".mcp.json")),
    ("must-block: .claude.json, a file beside no .claude folder", os.path.join(PROFILE, ".claude.json")),
    ("must-block: a shell startup file", os.path.join(PROFILE, ".bashrc")),
    ("must-block: a .pre-commit-config.yaml", os.path.join(REPOS, "Tools", ".pre-commit-config.yaml")),
)
for name, path in documented:
    decision, reason = decide("Write", path, MASTER)
    record(name, decision == gate.DENY and "leave the write out" in reason, (decision, reason[:120]))
for name, path in (("control: a .config folder that is not .config\\git", os.path.join(PROFILE, ".config", "x", "y")),
                   ("control: a file that only ends in .npmrc", os.path.join(REPOS, "Tools", "x.npmrc")),
                   ("control: npmrc with no leading dot", os.path.join(REPOS, "Tools", "npmrc")),
                   ("control: a git folder with no dot", os.path.join(REPOS, "Tools", "git", "x"))):
    decision, reason = decide("Write", path, MASTER)
    record(name, decision == gate.DEFER, (decision, reason[:120]))

OWN_TRANSCRIPT = os.path.join(PROFILE, ".claude", "projects", "C--Users-Administrator-source-repos-crgolden",
                              "fixture-session.jsonl")
OTHER_TRANSCRIPT = os.path.join(PROFILE, ".claude", "projects", "C--Users-Administrator--claude-master",
                                "fixture-session.jsonl")
exceptions = (
    ("control: a markdown file in the session's own project memory folder defers", MEMORY, OWN_TRANSCRIPT,
     gate.DEFER),
    ("must-block: the same markdown file from a session in another project, which Claude Code prompts for", MEMORY,
     OTHER_TRANSCRIPT, gate.DENY),
    ("must-block: a non-markdown file in the own memory folder", os.path.join(os.path.dirname(MEMORY), "index.json"),
     OWN_TRANSCRIPT, gate.DENY),
    ("must-block: a markdown file one folder below the own memory folder", os.path.join(
        os.path.dirname(MEMORY), "nested", "x.md"), OWN_TRANSCRIPT, gate.DENY),
    ("control: a file in Claude's own worktree under .claude\\worktrees defers", os.path.join(
        REPOS, "Tools", ".claude", "worktrees", "w1", "README.md"), None, gate.DEFER),
    ("must-block: a file directly named .claude\\worktrees is no worktree", os.path.join(
        REPOS, "Tools", ".claude", "worktrees"), None, gate.DENY),
    ("must-block: a .git folder inside Claude's own worktree", os.path.join(
        REPOS, "Tools", ".claude", "worktrees", "w1", ".git", "config"), None, gate.DENY),
)
for name, path, transcript, expected in exceptions:
    decision, reason = gate.evaluate("Write", {"file_path": path, "content": "x"}, MASTER, transcript_path=transcript)
    record(name, decision == expected, (decision, reason[:120]))

decision, reason = decide("NotebookEdit", os.path.join(REPOS, ".claude", "x.ipynb"), MASTER, key="notebook_path")
record("must-block: the master's NotebookEdit inside .claude, by notebook_path", decision == gate.DENY, decision)
decision, reason = decide("Write", MEMORY, MASTER)
record("the .claude reason names the folder and the script route", ".claude" in reason and "python" in reason, reason)
for folder, path in ((".git", os.path.join(REPOS, "Tools", ".git", "hooks", "pre-commit")),
                     (".husky", os.path.join(REPOS, "Inventory", ".husky", "pre-commit")),
                     (".vscode", os.path.join(REPOS, "Tools", ".vscode", "tasks.json")),
                     (".idea", os.path.join(REPOS, "Tools", ".idea", "workspace.xml"))):
    decision, reason = decide("Write", path, MASTER)
    record("must-block: the {0} reason offers no route around the approval".format(folder),
           decision == gate.DENY and folder in reason and "python" not in reason and "script" not in reason
           and "leave the write out" in reason, reason)

if failures:
    print("\n{0} protected-path case(s) failed.".format(len(failures)))
    sys.exit(1)
print("\nAll protected-path cases passed.")
