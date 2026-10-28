"""Branches this session made that the operator has merged and that are still present.

The session that makes a branch removes its worktree and deletes the branch once the change is on origin/main
(Tools/AGENTS.md). The Stop hook holds it to that: this module reads the session's own `git -C <repo> worktree add -b`,
`checkout -b` and `switch -c` calls from its transcript, and for each branch still present whose work origin/main
contains, names the commands that clean it up. A branch with no commit since it was made, one whose worktree holds
uncommitted work, and one the operator has not merged never count. The cleanup ends by pulling the repo's main checkout
and recording the landing in its global gate record, which reads CURRENT-IMPLICIT and stops any gate still running on
the old tree. The session's own worktree, on the branch named
by its session id at worktrees\\<session-id>\\<Repo>, is kept across its gate, commit and merge loop, so it counts only
when the session is finishing. No git call runs unless the transcript shows the session made a branch, and the
transcript is folded once, with the byte offset latched like the goal.
"""

import json
import os
import re
import subprocess
import tempfile

SHELL_TOOLS = ("PowerShell", "Bash")
LINE_NEEDLES = (b"worktree add", b"checkout -b", b"checkout -B", b"switch -c", b"switch -C")
RE_GIT_IN_REPO = re.compile(r"""\bgit(?:\.exe)?\s+-C\s+(?:"([^"]+)"|'([^']+)'|(\S+))\s+([^\r\n;&|]*)""", re.IGNORECASE)
RE_ARGUMENT = re.compile(r""""([^"]*)"|'([^']*)'|(\S+)""")
BRANCH_FLAGS = ("-b", "-B", "-c", "-C")
VALUE_FLAGS = ("--reason", "--orphan")
MERGED_BRANCH_REASON = (
    "STOP GATE: a branch this session made is merged into origin/main and still present (Tools/AGENTS.md: the "
    "session that made the branch removes its worktree and deletes the branch once the change is on main):\n"
    "{problems}\nRun each command as its own call, in the order given, then stop. When branch -d answers that the "
    "branch is not fully merged, its change landed on origin/main as a squash or rebase (git cherry shows every "
    "commit there), so delete it with branch -D instead.")


def arguments(text):
    return [next(group for group in match.groups() if group is not None) for match in RE_ARGUMENT.finditer(text)]


def branch_made(command):
    """(repo, branch, worktree or None) for a git command that makes a branch, else None."""
    match = RE_GIT_IN_REPO.search(command or "")
    if not match:
        return None
    repo = next(group for group in match.groups()[:3] if group is not None)
    words = arguments(match.group(4))
    if len(words) >= 2 and words[0] == "worktree" and words[1] == "add":
        branch, worktree, rest = None, None, words[2:]
        index = 0
        while index < len(rest):
            word = rest[index]
            if word in BRANCH_FLAGS[:2] and index + 1 < len(rest):
                branch, index = rest[index + 1], index + 2
            elif word in VALUE_FLAGS and index + 1 < len(rest):
                index += 2
            elif word.startswith("-"):
                index += 1
            else:
                worktree = worktree or word
                index += 1
        return (repo, branch, worktree) if branch and worktree else None
    if len(words) >= 3 and ((words[0] == "checkout" and words[1] in BRANCH_FLAGS[:2])
                            or (words[0] == "switch" and words[1] in BRANCH_FLAGS[2:])):
        return repo, words[2], None
    return None


def latch_path(state_root, session_id):
    return os.path.join(state_root, re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or "unknown")) + ".branches.json")


def branches_made(transcript_path, state_root, session_id):
    """Every branch this session's own shell calls made, folded from the latched offset."""
    path = latch_path(state_root, session_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        if not isinstance(state.get("offset"), int) or not isinstance(state.get("made"), list):
            state = None
    except (OSError, ValueError, AttributeError):
        state = None
    try:
        handle = open(transcript_path, "rb")
    except (OSError, TypeError):
        return [tuple(made) for made in (state or {}).get("made", [])]
    with handle:
        size = os.fstat(handle.fileno()).st_size
        if state is None or state["offset"] > size:
            state = {"offset": 0, "made": []}
        handle.seek(state["offset"])
        data = handle.read()
    end = data.rfind(b"\n") + 1
    if end:
        for raw in data[:end].split(b"\n"):
            if not any(needle in raw for needle in LINE_NEEDLES):
                continue
            try:
                record = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            for made in commands_making_branches(record):
                if list(made) not in state["made"]:
                    state["made"].append(list(made))
        state["offset"] += end
        try:
            os.makedirs(state_root, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=state_root, suffix=".tmp")
            with os.fdopen(descriptor, "w", encoding="utf-8") as out:
                json.dump(state, out, separators=(",", ":"))
            os.replace(temporary, path)
        except OSError:
            pass
    return [tuple(made) for made in state["made"]]


def commands_making_branches(record):
    if not isinstance(record, dict) or record.get("type") != "assistant":
        return []
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content") if isinstance(message.get("content"), list) else []
    made = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in SHELL_TOOLS:
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            found = branch_made(tool_input.get("command"))
            if found:
                made.append(found)
    return made


def git(repo, arguments_list, timeout):
    """(exit code, stdout) of one git call in repo, or (None, "") when it cannot run."""
    try:
        done = subprocess.run(["git", "-C", repo] + arguments_list, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return done.returncode, done.stdout.strip()


def merged_and_present(repo, branch, worktree, timeout):
    """The cleanup commands owed for one branch, else None."""
    code, tip = git(repo, ["rev-parse", "--verify", "--quiet", "refs/heads/" + branch], timeout)
    if code != 0 or not tip:
        return None
    code, history = git(repo, ["reflog", "--format=%H", "refs/heads/" + branch], timeout)
    made_at = history.splitlines()[-1] if code == 0 and history else None
    if not made_at or made_at == tip:
        return None
    present = bool(worktree) and os.path.isdir(worktree)
    if present:
        code, dirty = git(worktree, ["status", "--porcelain"], timeout)
        if code != 0 or dirty:
            return None
    code, _ = git(repo, ["merge-base", "--is-ancestor", tip, "origin/main"], timeout)
    if code != 0:
        code, cherry = git(repo, ["cherry", "origin/main", tip, made_at], timeout)
        if code != 0 or not cherry or any(not line.startswith("-") for line in cherry.splitlines()):
            return None
    commands = []
    if present:
        commands.append('git -C "{0}" worktree remove "{1}"'.format(repo, worktree))
    commands.append('git -C "{0}" branch -d {1}'.format(repo, branch))
    commands.append(landing_command(repo))
    return "- {0} in {1}: {2}".format(branch, repo, "; then ".join(commands))


def landing_command(repo):
    """The command that writes the landing into the repo's global gate record once its main checkout is clean at
    origin's HEAD, so a gate still running on the old tree stops (Harness/Gates/gate_status.py --record-landing)."""
    main = os.path.normpath(repo)
    tools = os.path.join(os.path.dirname(main), "Tools", "Gates", "gate_status.py")
    return 'git -C "{0}" pull --ff-only; then python "{1}" --record-landing {2}'.format(
        main, tools, os.path.basename(main))


def own_session_worktree(branch, worktree, session_id):
    if not session_id or not worktree or branch != session_id:
        return False
    folder = os.path.basename(os.path.dirname(os.path.normpath(worktree)))
    return os.path.normcase(folder) == os.path.normcase(session_id)


def problems(transcript_path, state_root, session_id, timeout=5, finishing=True):
    """One line per merged branch this session made that is still present, naming its cleanup commands."""
    found = []
    for repo, branch, worktree in branches_made(transcript_path, state_root, session_id):
        if not finishing and own_session_worktree(branch, worktree, session_id):
            continue
        owed = merged_and_present(repo, branch, worktree, timeout)
        if owed and owed not in found:
            found.append(owed)
    return found
