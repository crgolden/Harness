"""PreToolUse gate for the Claude Code PowerShell tool, registered in ~/.claude/settings.json.

Rules unchanged: this is a HOST change. Measured on this machine the PowerShell version costs 2491ms on
a benign allowed command, of which ~1652ms is bare pwsh startup. It fires on EVERY shell call, which
makes it the largest single latency cost of any hook here -- larger in total than the Stop gate, which
is dearer per invocation but runs only once a turn.

CASE SENSITIVITY IS LOAD-BEARING AND MIXED IN THIS FILE, which makes it the likeliest thing for an
editor to get wrong. The PowerShell semantics below are the reason the rules read as they do; this
began as a port of a PowerShell gate, and the asymmetry was deliberate there and is deliberate here:

  * PowerShell `-match`, `-notmatch`, `-like`, `-notlike` and `-replace` are CASE-INSENSITIVE. That is
    why `PWSH -File x` is caught by the pwsh-flags rule and why `PYTHON foo.py` hits the allow tail.
  * The git read-only allow deliberately uses `-cmatch` / `-cnotmatch`, which are CASE-SENSITIVE. So
    `GIT STATUS` does NOT take the read-only fast path. That asymmetry is intentional and is pinned by
    test_pretooluse_gate.py; do not "tidy" it into a single case mode.

THERE IS NO SECOND IMPLEMENTATION. The PowerShell gate and the parity harness that kept the two in step
were deleted once it was clear only this file is ever executed: settings.json points the PreToolUse hook
here, so the twin's sole reader was the harness comparing it against this file. Every case those
harnesses covered was checked against test_pretooluse_gate.py first; all three that looked
PowerShell-only turned out to be byte-identical command fixtures under different names.
"""

import json
import os
import re
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_scope  # noqa: E402

CI = re.IGNORECASE

GIT_WRITE_PATTERN = re.compile(
    r"\bgit\b(\s+(-C\s+(\"[^\"]*\"|\S+)|-c\s+\S+|--[\w-]+(=(\"[^\"]*\"|\S+))?))*\s+(commit|push)\b",
    CI,
)
COMMIT_REASON = (
    "Blocked: git writes are the operator's to run, always. This is UNCONDITIONAL - being told to "
    "commit, push or merge does NOT lift it, and asking again will not change the answer. It covers "
    "every spelling - git commit, git push, git -C <path> commit, git -c k=v push - and a git commit "
    "or git push found INSIDE a .ps1 or .py you are about to run; moving the write into a script is "
    "not a way around this gate. Do this instead: stage the work, run the gate, then give the "
    "operator the finished command AND the full commit message inline in your reply - not a file "
    "path they have to open, and not a reference to something earlier in the conversation."
)

# INTENT rules describe WHAT is being done, so they hold wherever the payload lives -- inline, or in
# the body of a script about to be run. Deliberately separate from the HYGIENE rules further down,
# which are about the SHAPE of a command line and whose whole purpose is to push work into a script.
# A `dotnet test` intent rule used to sit here; it was removed once every repo's global.json opted
# into the MTP `dotnet test` runner (`{"test":{"runner":"Microsoft.Testing.Platform"}}`), which makes
# `dotnet test` work from a repo root. It still fails from the workspace root, which has no
# global.json -- that is a working-directory condition, not a reason to deny the command.
STOP_GATE_STATE_PATTERN = re.compile(r"claude-stopgate", CI)
STOP_GATE_STATE_REASON = (
    "Blocked: this command names the Stop gate's state directory (claude-stopgate), which holds the block count, "
    "the delivered asks and the decision log the gate judges every stop from. No session's shell touches it, "
    "because text cannot tell a read from a write. Read its logs with the Read tool, or run "
    "Tools/Claude/skills/hook-status/scripts/hook_status.py."
)
TRANSCRIPT_INDEX_PATTERN = re.compile(r"transcript-index", CI)
TRANSCRIPT_INDEX_REASON = (
    "Blocked: this command names the hooks' transcript index (transcript-index), the offsets of the transcript lines "
    "the hooks read the operator's messages, refused writes and /goal from; an index edited to drop a line hides it "
    "from them. No session's shell touches it, because text cannot tell a read from a write. Read it with the Read "
    "tool."
)

INTENT_RULES = [
    (GIT_WRITE_PATTERN, COMMIT_REASON),
    (STOP_GATE_STATE_PATTERN, STOP_GATE_STATE_REASON),
    (TRANSCRIPT_INDEX_PATTERN, TRANSCRIPT_INDEX_REASON),
]

EXEMPT_MARKER = "PRETOOLUSE-GATE: git-write-strings-are-test-data"

RE_SCRIPT_CALL = re.compile(
    r"^\s*(?:pwsh\s+-File|python(?:\s+(?:-[XW]\s*(?:\"[^\"]*\"|[^\s\"]+)|--check-hash-based-pycs\s+\S+|-[A-Za-z]+))*)"
    r"\s+(?P<path>\"[^\"]+\"|(?!-)[^\s\"]+)", CI)
RE_CD_CHAIN = re.compile(r"^\s*(cd|Set-Location)\s+\S.*(&&|;)", CI)
RE_PUSHD = re.compile(r"\b(Push-Location|Pop-Location|pushd|popd)\b", CI)
RE_SHELL_OPERATOR = re.compile(r"[;|`]|&&|\$\(|[\r\n]|(?<!^)(?<=\s)&(\s|$)", CI)
RE_DOUBLED_BACKSLASH = re.compile(r"\\\\")
RE_POWERSHELL_CALL = re.compile(r"^(pwsh|powershell)(\.exe)?\s", CI)
RE_REDIRECT = re.compile(r">(?!\s*&)")
RE_PIP_INSTALL = re.compile(r"\bpip\s+install\b", CI)

# Case-SENSITIVE on purpose -- see the module docstring.
RE_GIT_READONLY = re.compile(
    r"^git\s+(-C\s+(\"[^\"]+\"|[^\s\"]+)\s+)?"
    r"(status|log|diff|show|rev-parse|describe|blame|shortlog|ls-files|ls-tree|cat-file|for-each-ref)(\s|$)"
)
RE_GIT_DANGEROUS_FLAG = re.compile(r"(^|\s)(-c\s|--exec-path|--upload-pack|--output)")

SHELL_OPERATOR_REASON = (
    "Blocked: this command contains a shell operator or command separator - semicolon, pipe, "
    "&&, ||, a bare &, a backtick, $( ), or a newline. Two separate problems, both serious. "
    "(1) APPROVAL SPAM: Claude Code splits a command on these and requires EVERY resulting "
    "segment to match a permissions.allow rule. Quoting does NOT protect you - an operator "
    "inside a quoted argument still breaks the match and forces an approval prompt even for "
    "whitelisted commands like playwright-cli, python, dotnet, gh and az. (2) ALLOW-LIST "
    "BYPASS: newline, $( ) and bare & chaining let a SECOND, non-whitelisted command execute "
    "with no approval at all - this is a security hole, not a nuisance. Fix: put the payload in "
    "a file with the Write tool (use the session scratchpad), then run ONE bare command - "
    "python <abs path>.py, or pwsh -File <abs path>.ps1, or playwright-cli --raw run-code "
    "--filename=<abs path>.js. Commands that legitimately need semicolons or pipes - sqlpackage "
    "connection strings, dotnet -p: property lists, gh --jq, az --query JMESPath - must also go "
    "in a .ps1 and run via pwsh -File. Never inline code with -c, -e, -Command or run-code "
    "<snippet>. If you need two commands, issue two separate tool calls; the working directory "
    "persists between them, but shell variables and functions do not, so pass values as arguments. "
    "The script's body is judged too: one that runs a gate step or a test harness, sleeps or waits, "
    "or runs git commit or push is refused, so it may only do the work itself. Any change to this "
    "hook must be verified with Harness/Hooks/test_pretooluse_gate.py."
)

DOUBLED_BACKSLASH_REASON = (
    "Blocked: this command contains a doubled backslash (\\\\). Claude Code's permission matcher "
    "splits a command there even inside a quoted argument, so a part matches no permissions.allow "
    "rule and the call waits on an approval prompt; in an unattended session nobody answers it. "
    "Fix: put the payload (a regex, a pattern, a UNC path) in a file with the Write tool (use the "
    "session scratchpad), then run ONE bare command - python <abs path>.py or pwsh -File <abs "
    "path>.ps1 - or use the Grep tool for a content search. A path with single backslashes is fine."
)

CD_CHAIN_REASON = (
    "Blocked: do not chain cd with && or ; before a command. Pass the full path directly to "
    "the command instead (e.g. dotnet build <full path to project>)."
)

PUSHD_REASON = (
    "Blocked: Push-Location/Pop-Location (or pushd/popd) is the same cd-chaining workaround "
    "under a different name. Pass the full path directly to the command instead. If a tool "
    "genuinely requires a specific cwd (e.g. dotnet test with a global.json test runner), issue "
    "a standalone Set-Location call by itself first (not chained with ; or &&), then run plain "
    "commands in subsequent calls - the working directory persists across calls."
)

PWSH_FLAGS_REASON = (
    "Blocked: the only auto-approved way to run a PowerShell script here is exactly - "
    "pwsh -File <abs path>. The allow-rule PowerShell(pwsh -File *) is a PREFIX match over the "
    "WHOLE command, so any flag placed before -File (-NoProfile, -NonInteractive, -Command) "
    "misses it and forces a manual approval prompt on EVERY call. That cost is invisible to the "
    "agent - the tool result looks exactly like success - and is paid by the operator instead, "
    "which is why it went unnoticed for about ten calls in a single session. Drop the extra "
    "flags. Never use powershell or powershell.exe either: the PowerShell tool is ALREADY pwsh "
    "7, so those spawn a nested Windows PowerShell 5.1. Any change to this hook must be "
    "verified with Harness/Hooks/test_pretooluse_gate.py."
)

INLINE_CODE_REASON = (
    "Blocked: inline code (python -c, node -e or -p) runs with no file for this hook to read, so the "
    "allow tail would approve it while the script-body intent check never sees what it does. Write the "
    "code to a file in your session scratchpad (the path the harness names) with the Write tool and run ONE bare command: "
    "python <abs path>.py. Any change to this hook must be verified with Harness/Hooks/test_pretooluse_gate.py."
)

RE_INTERPRETER = re.compile(r"^(?:py|pythonw?|node)(?:\.exe)?$", CI)
PYTHON_OPTIONS_WITH_VALUE = ("-W", "-X", "--check-hash-based-pycs")
RE_PYTHON_INLINE_FLAG = re.compile(r"^-[A-VYZa-z]*c")
RE_NODE_INLINE_FLAG = re.compile(r"^(?:-[A-Za-z]*[ep][A-Za-z]*|--(?:eval|print))(?:$|=)")


def runs_inline_code(cmd):
    """True when an interpreter is handed inline code.

    Python's options are a small fixed set, so they are read up to the first positional argument or -m, skipping
    the value of each option that takes one; its flags are case-sensitive and combine (-Ic, or -cCODE glued), and
    a -W or -X value glued to its letter is never read as one. Node's value-taking options are many, so every
    token up to a bare -- is read and any short cluster holding e or p (-pe) or --eval/--print denies. A command
    that cannot be tokenized is read as inline code."""
    try:
        tokens = [token.strip("\"'") for token in shlex.split(cmd, posix=False)]
    except ValueError:
        words = cmd.split()
        return bool(words) and bool(RE_INTERPRETER.match(re.split(r"[\\/]", words[0].strip("\"'"))[-1]))
    interpreter = re.split(r"[\\/]", tokens[0])[-1] if tokens else ""
    if not RE_INTERPRETER.match(interpreter):
        return False
    tokens = tokens[1:]
    if interpreter.lower().startswith("node"):
        for token in tokens:
            if token == "--":
                return False
            if RE_NODE_INLINE_FLAG.match(token):
                return True
        return False
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if RE_PYTHON_INLINE_FLAG.match(token):
            return True
        if token in PYTHON_OPTIONS_WITH_VALUE:
            skip = True
            continue
        if token in ("-m", "--") or not token.startswith("-"):
            return False
    return False


RE_GIT_BRANCH_CREATING = re.compile(r"\bgit(?:\.exe)?\b.*\b(?:worktree\s+add|checkout|switch)\b", CI)
GIT_GLOBAL_OPTIONS_WITH_VALUE = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path")
WORKTREE_ADD_OPTIONS_WITH_VALUE = ("--reason",)
CHECKOUT_CREATE_FLAGS = ("-b", "-B", "--orphan")
SWITCH_CREATE_FLAGS = ("-c", "-C", "--create", "--force-create", "--orphan")

SESSION_BRANCH_REASON = (
    "Blocked: a branch or worktree this session creates is named by the session id and lives at one path "
    "(Tools/AGENTS/WORKFLOWS.md, the worktree rule). Expected branch: {branch}. Expected worktree path: {path}. "
    "Found: {found}. Use exactly: git -C <repo> worktree add -b {branch} \"{path}\" origin/main, the path ending in "
    "the repository's folder name. A worktree is made only for a change to the repository whose hooks judge the "
    "session (Tools/Gates/ and Tools/Hooks/ until the launchers switch to Harness, then Harness by the master alone) "
    "or when the operator asks for one; every other change goes in the main checkout. Any change to this hook must "
    "be verified with Harness/Hooks/test_pretooluse_gate.py."
)

DEFAULT_WORKTREE_REPO = "Tools"


def session_worktree_path(profile, session_id, repo=DEFAULT_WORKTREE_REPO):
    return os.path.join(profile, "source", "worktrees", session_id, repo)


def worktree_repo(cmd):
    """The folder name of the repository a git command runs in: the basename of its -C directory, else the Tools
    default every session's worktree was named for before Harness existed."""
    try:
        tokens = [token.strip("\"'") for token in shlex.split(cmd or "", posix=False)]
    except ValueError:
        tokens = [token.strip("\"'") for token in (cmd or "").split()]
    directory, _ = git_subcommand(tokens)
    return os.path.basename(os.path.normpath(directory)) if directory else DEFAULT_WORKTREE_REPO


def same_path(left, right):
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def git_subcommand(tokens):
    """(working directory from -C or None, index of the subcommand) after a leading git token, or (None, -1)."""
    if not tokens or not re.match(r"^git(?:\.exe)?$", re.split(r"[\\/]", tokens[0])[-1], CI):
        return None, -1
    directory = None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in GIT_GLOBAL_OPTIONS_WITH_VALUE:
            if token == "-C" and index + 1 < len(tokens):
                directory = tokens[index + 1]
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return directory, index
    return directory, -1


def flag_value(tokens, flags):
    """The value given to the first of flags, spelled `-b x` or `--create=x`; None when no flag is present."""
    for index, token in enumerate(tokens):
        if token in flags:
            return tokens[index + 1] if index + 1 < len(tokens) else ""
        for flag in flags:
            if flag.startswith("--") and token.startswith(flag + "="):
                return token[len(flag) + 1:]
    return None


def session_branch_violation(cmd, session_id, profile):
    """A description of how cmd creates a branch or worktree off the session's one name and path, or None.

    Only git worktree add, checkout with a create flag and switch with a create flag are judged; every other
    git command, and every command when the session id or profile is unknown, passes untouched. A command that
    cannot be tokenized is judged on the raw words."""
    if not session_id or not profile or not RE_GIT_BRANCH_CREATING.search(cmd):
        return None
    try:
        tokens = [token.strip("\"'") for token in shlex.split(cmd, posix=False)]
    except ValueError:
        tokens = [token.strip("\"'") for token in cmd.split()]
    directory, index = git_subcommand(tokens)
    if index < 0:
        return None
    subcommand = tokens[index]
    rest = tokens[index + 1:]
    expected_path = session_worktree_path(profile, session_id, worktree_repo(cmd))
    if subcommand == "checkout" or subcommand == "switch":
        branch = flag_value(rest, CHECKOUT_CREATE_FLAGS if subcommand == "checkout" else SWITCH_CREATE_FLAGS)
        if branch is None or branch == session_id:
            return None
        return "git {0} creating branch '{1}'".format(subcommand, branch)
    if subcommand != "worktree" or not rest or rest[0] != "add":
        return None
    arguments = rest[1:]
    branch = flag_value(arguments, ("-b", "-B", "--orphan"))
    positionals = []
    skip = False
    for position, token in enumerate(arguments):
        if skip:
            skip = False
            continue
        if token in ("-b", "-B", "--orphan") or token in WORKTREE_ADD_OPTIONS_WITH_VALUE:
            skip = True
            continue
        if token.startswith("-"):
            continue
        positionals.append(token)
    problems = []
    if "--detach" in arguments or "-d" in arguments:
        problems.append("a detached worktree")
    path = positionals[0] if positionals else ""
    if path and not os.path.isabs(path) and directory and os.path.isabs(directory):
        path = os.path.join(directory, path)
    if not path or not os.path.isabs(path) or not same_path(path, expected_path):
        problems.append("worktree path '{0}'".format(path or "(none)"))
    if branch is None and len(positionals) > 1:
        branch = positionals[1]
    if branch != session_id:
        problems.append("branch '{0}'".format(branch if branch is not None else "(git would name it after the folder)"))
    return ", ".join(problems) if problems else None


RE_WORKTREE_REMOVAL = re.compile(r"\bworktree\s+remove\b|\b(?:Remove-Item|rm|rmdir|rd|del|ri)\b.*[\\/]worktrees[\\/]",
                                 CI)
RE_REMOVE_COMMAND = re.compile(r"^(?:Remove-Item|rm|rmdir|rd|del|ri)$", CI)
RE_CITED_PATH = re.compile(r"[A-Za-z]:\\[^\s:,;\"'()]+")

WORKTREE_REMOVAL_REASON = (
    "Blocked: this removes the worktree {path}, and this session's audit has not been run against it: {problem}. "
    "Run the session-audit skill first (session_audit.py sync, record every message, re-record each verdict that "
    "cites a file under the worktree with the same line where the work landed, such as the main checkout), then "
    "remove it. A verdict citing a removed worktree stops resolving and reopens as stale-evidence. Any change to "
    "this hook must be verified with Harness/Hooks/test_pretooluse_gate.py."
)


def removal_target(cmd, profile):
    """The worktree path a git worktree remove or a delete under %USERPROFILE%\\source\\worktrees names, or None."""
    if not RE_WORKTREE_REMOVAL.search(cmd):
        return None
    try:
        tokens = [token.strip("\"'") for token in shlex.split(cmd, posix=False)]
    except ValueError:
        tokens = [token.strip("\"'") for token in cmd.split()]
    directory, index = git_subcommand(tokens)
    if index >= 0:
        rest = tokens[index + 1:]
        if tokens[index] != "worktree" or not rest or rest[0] != "remove":
            return None
        paths = [token for token in rest[1:] if not token.startswith("-")]
        if not paths:
            return None
        path = paths[0]
        return os.path.join(directory, path) if directory and not os.path.isabs(path) else path
    if not tokens or not RE_REMOVE_COMMAND.match(tokens[0]):
        return None
    root = os.path.join(profile or "", "source", "worktrees")
    for token in tokens[1:]:
        if not token.startswith("-") and profile and os.path.normcase(os.path.normpath(token)).startswith(
                os.path.normcase(os.path.normpath(root)) + os.sep):
            return token
    return None


def audit_rows(path):
    """Every JSON object row of an audit cache file, or None when the file is missing."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return None
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            rows.append(row)
    return rows


def worktree_removal_problem(target, session_id, audit_root=None):
    """Why removing `target` would leave this session's audit behind, or None: no audit catalog, a cataloged
    operator message other than the newest with no verdict, or a verdict citing a file under the worktree. Messages
    written after the last sync are the Stop gate's to judge (declared_items.unrecorded_messages)."""
    import claude_temp
    folder = os.path.join(audit_root or os.path.join(claude_temp.audits_root(), "session-audit"), session_id)
    cataloged = audit_rows(os.path.join(folder, "messages.jsonl"))
    if cataloged is None:
        return "no session audit has been run in this session"
    verdicts = {row["id"]: row for row in audit_rows(os.path.join(folder, "verdicts.jsonl")) or []}
    ordered = [row["id"] for row in sorted(cataloged, key=lambda row: str(row.get("ts") or ""))]
    unrecorded = [message_id for message_id in ordered[:-1] if message_id not in verdicts]
    if unrecorded:
        return "{0} operator message(s) have no verdict ({1})".format(
            len(unrecorded), ", ".join(message_id[:8] for message_id in unrecorded[:5]))
    prefix = os.path.normcase(os.path.normpath(target)) + os.sep
    citing = sorted(message_id[:8] for message_id, row in verdicts.items()
                    if any(os.path.normcase(os.path.normpath(path)).startswith(prefix)
                           for path in RE_CITED_PATH.findall(str(row.get("evidence") or ""))))
    if citing:
        return "{0} verdict(s) cite files under it ({1})".format(len(citing), ", ".join(citing[:8]))
    return None


GIT_READONLY_REASON = (
    "Auto-approved: git read-only subcommand, single bare invocation, no shell operators, "
    "no config-override, exec-path, upload-pack or output flag."
)

ALLOW_PREFIXES = [
    "az ", "dotnet ", "Expand-Archive ", "gcloud ", "gcx ", "gh ", "Get-ChildItem ",
    "Get-Content ", "Get-Date ", "jb ", "mypy ", "New-Item ", "npm ", "npx ", "playwright-cli ", "pwsh -File ", "python ",
    "Resolve-Path ", "ruff ", "Set-Location ", "sonar ", "sqlpackage ", "syncthing ", "Test-Path ",
]


DENY = "deny"
ALLOW = "allow"
DEFER = "defer"


def test_intent(text, pattern):
    """Shell-shaped patterns expect whitespace between a command and its subcommand. A tokenized call
    separates them with quotes and commas instead, so subprocess.run(["git","commit"]) slipped through.
    The raw text is tested too, so nothing that matched before stops matching."""
    if not text:
        return False
    if pattern.search(text):
        return True
    normalized = re.sub(r"[\"',\[\]]", " ", text)
    return bool(pattern.search(normalized))


def evaluate(cmd, session_id=None, profile=None, audit_root=None, environ=None, cwd=None, harness=None):
    """The whole decision as data: (decision, reason, exit_code).

    Split out from main() so the test suite can call it IN-PROCESS. Note that DEFER -- no output at
    all, exit 0, handing the call back to Claude Code's normal permission flow -- is a real decision
    and the easiest one to lose in a port, because it looks like nothing happened. `environ` is the hook's
    environment (the master's MASTER_SESSION marker), `cwd` the session's working directory relative paths resolve
    against, and `harness` names a fixture Harness main checkout for the test suite.
    """
    for pattern, reason in INTENT_RULES:
        if test_intent(cmd, pattern):
            return DENY, reason, 1

    if cmd and not harness_scope.is_master(environ):
        reached = harness_scope.denied_command_path(cmd, cwd, harness, environ)
        if reached and RE_GIT_BRANCH_CREATING.search(cmd):
            return DENY, harness_scope.WORKTREE_REASON.format(path=reached), 1
        if reached:
            return DENY, harness_scope.COMMAND_REASON.format(path=reached), 1

    if cmd:
        found = session_branch_violation(cmd, session_id, profile)
        if found:
            return DENY, SESSION_BRANCH_REASON.format(
                branch=session_id, path=session_worktree_path(profile, session_id, worktree_repo(cmd)), found=found), 1

    target = removal_target(cmd, profile) if cmd and session_id else None
    if target:
        problem = worktree_removal_problem(target, session_id, audit_root)
        if problem:
            return DENY, WORKTREE_REMOVAL_REASON.format(path=target, problem=problem), 1

    # `pwsh -File <path>` and `python <path>` sit on the allow tail, so without this the gate approves
    # a script whose body does the forbidden thing. Static text matching: it stops an agent that writes
    # the forbidden call plainly, which is the actual threat here. Not a sandbox.
    if cmd:
        m = RE_SCRIPT_CALL.match(cmd)
        if m:
            script_path = m.group("path").strip('"')
            if script_path and os.path.isfile(script_path):
                body = None
                try:
                    with open(script_path, "r", encoding="utf-8", errors="replace") as fh:
                        body = fh.read()
                except Exception:
                    body = None
                # A file can legitimately contain these strings as DATA. The exemption is explicit and
                # greppable instead of inferred.
                for line in (body or "").splitlines() if session_id else []:
                    target = removal_target(line.strip(), profile)
                    problem = worktree_removal_problem(target, session_id, audit_root) if target else None
                    if problem:
                        return DENY, WORKTREE_REMOVAL_REASON.format(path=target, problem=problem) + (
                            " Found in the script body: {0}.".format(script_path)), 1
                if body and EXEMPT_MARKER.lower() not in body.lower():
                    for pattern, reason in INTENT_RULES:
                        if test_intent(body, pattern):
                            return DENY, (
                                reason
                                + " Found in the script body: {0}. If this file holds ".format(script_path)
                                + "the string as test data rather than running it, declare that by putting the "
                                + "exact line '{0}' in the file.".format(EXEMPT_MARKER)
                            ), 1

    if cmd and RE_CD_CHAIN.search(cmd):
        return DENY, CD_CHAIN_REASON, 1

    if cmd and RE_PUSHD.search(cmd):
        return DENY, PUSHD_REASON, 1

    if cmd and RE_SHELL_OPERATOR.search(cmd):
        return DENY, SHELL_OPERATOR_REASON, 1

    if cmd and RE_DOUBLED_BACKSLASH.search(cmd):
        return DENY, DOUBLED_BACKSLASH_REASON, 1

    if not cmd:
        return DEFER, "", 0
    t = cmd.strip()

    if RE_POWERSHELL_CALL.search(t) and not t.lower().startswith("pwsh -file "):
        return DENY, PWSH_FLAGS_REASON, 1

    if runs_inline_code(t):
        return DENY, INLINE_CODE_REASON, 1

    if RE_REDIRECT.search(t):
        return DEFER, "", 0
    if RE_PIP_INSTALL.search(t):
        return DEFER, "", 0

    if RE_GIT_READONLY.search(t) and not RE_GIT_DANGEROUS_FLAG.search(t):
        return ALLOW, GIT_READONLY_REASON, 0

    for prefix in ALLOW_PREFIXES:
        if t.lower().startswith(prefix.lower()):
            return ALLOW, (
                "Auto-approved by PreToolUse allow tail (prefix: "
                + prefix.strip()
                + ") - single bare invocation, no shell operators, no file redirect."
            ), 0

    return DEFER, "", 0


def main():
    raw = sys.stdin.read()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    tool_input = payload.get("tool_input")
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    if cmd is not None and not isinstance(cmd, str):
        cmd = None

    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        session_id = None
    decision, reason, code = evaluate(cmd, session_id, os.environ.get("USERPROFILE"))
    if decision != DEFER:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }, separators=(",", ":")))
    return code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
