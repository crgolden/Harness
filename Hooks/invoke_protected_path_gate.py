"""PreToolUse gate: the master session never makes a file-tool write Claude Code will not let an allow rule
pre-approve.

Claude Code never auto-approves a write to a protected path (permission-modes docs, "Protected paths"): Write, Edit
and NotebookEdit there ask for approval whatever settings.json allows, and nobody is there to answer the master's
prompts, so the call waits on a prompt for good. Reads there pass the allow rules. The master is told apart by
master_session.runs_as, so every other session, whose operator can answer a prompt, is left to the normal permission
flow.

  deny  a master session's Write, Edit or NotebookEdit whose path, in any spelling (as given, long, resolved through
        a junction), runs through a protected folder (PROTECTED_FOLDERS, and .config\\git), or ends in a protected
        file (PROTECTED_FILES, and a worktree's .git file). For .claude the reason names a script run through
        PowerShell; for the others, whose files can run as code later, it says to leave the write out and report it
        for a session the operator attends, since no route may stand in for that approval.
  defer the .claude exceptions a call can be checked against: Claude's own worktrees under .claude\\worktrees, and a
        markdown file in the calling session's own project's memory folder (the folder its transcript sits in). The
        other documented exceptions belong to "the current session's own" plan file or background-job scratch, which
        a call does not show, so they stay denied: a deny never hangs the master, a wrong allow would.

Wiring: PRETOOLUSE-HOOK.md, "The protected-path gate". Verified by Harness/Hooks/test_protected_path_gate.py.
"""

import os
import sys

HOOKS = os.path.dirname(os.path.abspath(__file__))
if HOOKS not in sys.path:
    sys.path.insert(0, HOOKS)
import claude_temp  # noqa: E402
import master_session  # noqa: E402

DENY = "deny"
DEFER = "defer"
WRITE_TOOLS = ("Write", "Edit", "NotebookEdit")
CLAUDE_FOLDER = ".claude"
PROTECTED_FOLDERS = (CLAUDE_FOLDER, ".git", ".vscode", ".idea", ".husky", ".cargo", ".devcontainer", ".yarn", ".mvn")
PROTECTED_FOLDER_PAIRS = ((".config", "git"),)
PROTECTED_FILES = (
    ".git", ".gitconfig", ".gitmodules",
    ".bashrc", ".bash_profile", ".bash_login", ".bash_aliases", ".bash_logout", ".zshrc", ".zprofile", ".zshenv",
    ".zlogin", ".zlogout", ".profile", ".envrc",
    ".npmrc", ".yarnrc", ".yarnrc.yml", ".pnp.cjs", ".pnp.loader.mjs", ".pnpmfile.cjs", "bunfig.toml", ".bunfig.toml",
    ".bazelrc", ".bazelversion", ".bazeliskrc",
    ".pre-commit-config.yaml", "lefthook.yml", "lefthook.yaml", ".lefthook.yml", ".lefthook.yaml",
    "gradle-wrapper.properties", "maven-wrapper.properties",
    ".devcontainer.json", ".ripgreprc", "pyrightconfig.json", ".mcp.json", ".claude.json",
)
WORKTREES_FOLDER = "worktrees"
MEMORY_FOLDER = "memory"

SCRIPT_ROUTE_FOLDERS = (CLAUDE_FOLDER,)

REASON = (
    "Blocked: {path} is inside a {folder} folder, where Claude Code asks for approval on every file-tool write "
    "whatever settings.json allows, and nobody answers the master session's prompts, so this call would wait for "
    "good. Write the file from a script run through PowerShell (python \"<script>\" with the script in your "
    "scratchpad), which the allow rules approve, or leave the write out."
)

CODE_FOLDER_REASON = (
    "Blocked: {path} is a protected path ({folder}), where Claude Code asks for approval on every file-tool write "
    "whatever settings.json allows, because a file there can run as code or change how a tool runs later (git and "
    "husky hooks, shell and package-manager startup files, editor tasks). Nobody answers the master session's "
    "prompts, and no other route may stand in for that approval: leave the write out, and name it in your report as "
    "work for a session the operator attends."
)


def spellings(path):
    absolute = os.path.abspath(path)
    forms = {absolute, claude_temp.long_path(absolute)}
    try:
        forms.add(os.path.realpath(absolute))
    except (OSError, ValueError):
        pass
    return forms


def parts_of(form):
    return [part for part in os.path.normcase(form).replace("/", "\\").split("\\") if part]


def own_memory_folders(transcript_path):
    """Every spelling of the calling session's own project memory folder: <project>\\memory beside its transcript."""
    if not transcript_path:
        return []
    return [parts_of(os.path.join(os.path.dirname(form), MEMORY_FOLDER)) for form in spellings(transcript_path)]


def excepted(parts, index, memory_folders):
    """True when the .claude folder at parts[index] holds one of the exceptions a call can be checked against."""
    if index + 1 < len(parts) - 1 and parts[index + 1] == WORKTREES_FOLDER:
        return True
    if parts[-1].endswith(".md"):
        folder = parts[:-1]
        return any(folder == memory for memory in memory_folders)
    return False


def protected_name(form, memory_folders):
    """The protected folder or file a spelling of a path names, else None."""
    parts = parts_of(form)
    if not parts:
        return None
    for index, part in enumerate(parts[:-1]):
        if part in PROTECTED_FOLDERS:
            if part == CLAUDE_FOLDER and excepted(parts, index, memory_folders):
                continue
            return part
        for pair in PROTECTED_FOLDER_PAIRS:
            if index + len(pair) < len(parts) and tuple(parts[index:index + len(pair)]) == pair:
                return "\\".join(pair)
    if parts[-1] in PROTECTED_FILES:
        return parts[-1]
    return None


def protected_folder(path, transcript_path=None):
    """The protected folder or file a path names, in any spelling, else None. Only a whole name counts, so x.claude,
    .github or a file named .claude never does."""
    memory_folders = own_memory_folders(transcript_path)
    for form in spellings(path):
        name = protected_name(form, memory_folders)
        if name:
            return name
    return None


def evaluate(tool_name, tool_input, environ=None, transcript_path=None):
    """(decision, reason). Split out so the harness and the dispatcher call it in-process."""
    if tool_name not in WRITE_TOOLS or not isinstance(tool_input, dict):
        return DEFER, ""
    if not master_session.runs_as(master_session.MASTER_SESSION_VARIABLE, environ):
        return DEFER, ""
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(target, str) or not target.strip():
        return DEFER, ""
    try:
        folder = protected_folder(target, transcript_path)
    except (TypeError, ValueError):
        return DEFER, ""
    if not folder:
        return DEFER, ""
    reason = REASON if folder in SCRIPT_ROUTE_FOLDERS else CODE_FOLDER_REASON
    return DENY, reason.format(path=target, folder=folder)
