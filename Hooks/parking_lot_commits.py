r"""The parking lot's commit-message rule: a checkout a session worked in (wrote a file of, as the Stop gate records
in its <session>.worked.json and <session>.repos.json state) that is still dirty owes that session's parking lot its
current commit message, gated or not, since the operator commits a tree without a gate to reach CURRENT-IMPLICIT.
So does a repo whose latest gate record is PASSED by the session, and a repo the session's work-product declaration
names once its main checkout, or the session's own worktree, is GREEN-CURRENT and dirty with a file the session
itself wrote there (the Stop gate's record), whichever session's gate passed on it: a peer's own uncommitted files
in the same checkout owe this session nothing, even a file one of this session's landed items once named. One message per checkout, keyed Repo for the main checkout and Repo@<worktree> for a worktree. A
committed checkout's message leaves the parking lot.

Imported by invoke_stop_gate.py (the turn-end veto) and by Claude/skills/parking-lot/scripts/parking_lot.py (the CLI
that writes the messages). It lives in Hooks/ beside the Stop hook, its main consumer, so a change to the rule goes
through the same pull request as the hook that enforces it.
"""

import json
import os
import re
import subprocess
import sys

HOOKS = os.path.dirname(os.path.abspath(__file__))
if HOOKS not in sys.path:
    sys.path.insert(0, HOOKS)
import session_home  # noqa: E402
import work_product_items  # noqa: E402

REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


STATUS_COMMAND = ("git -C \"<checkout>\" -c core.quotePath=false status --porcelain --no-renames "
                  "--untracked-files=all (one file line per path after its two status letters, without the double "
                  "quotes git puts around a path holding a space)")
UNREADABLE_PROBLEM = ("{0}: its uncommitted files could not be read, so its message cannot be judged: {1}; fix what "
                      "that error names if this session caused it, otherwise end on a REFUSING: line quoting the error "
                      "and four or more of the operator's words")


def dirty_files(path):
    """Every uncommitted path, one per file (untracked folders expanded, renames as their two paths)."""
    status = subprocess.run(["git", "-C", path, "-c", "core.fsmonitor=false", "-c", "core.quotePath=false", "status",
                             "-z", "--porcelain=v1", "--no-renames", "--untracked-files=all"],
                            capture_output=True, text=True, encoding="utf-8")
    if status.returncode != 0:
        raise RuntimeError("git status failed in {0}: {1}".format(path, status.stderr.strip()))
    return sorted({entry[3:] for entry in status.stdout.split("\0") if len(entry) > 3})


def gate_status_module():
    """Harness/Gates/gate_status.py owns what a repo's most recent gate record says and whether it still holds."""
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Gates")
    if here not in sys.path:
        sys.path.insert(0, here)
    import gate_status
    return gate_status


def session_checkout(repo, workspace, session_id):
    """(checkout, record) for the newest gate record this session wrote among the repo's checkouts, else (None, None).
    A peer's newer record on another worktree of the same repo never stands for this session's work."""
    try:
        gate_status = gate_status_module()
        best = (None, None)
        for checkout in gate_status.checkouts(repo, workspace):
            try:
                record = gate_status.read_record(repo, gate_status.checkout_state_dir(repo, checkout))
            except (OSError, ValueError):
                continue
            if record and record.get("session") == session_id and (
                    best[1] is None or gate_status.record_moment(record) > gate_status.record_moment(best[1])):
                best = (checkout, record)
        return best
    except (OSError, ValueError, RuntimeError):
        return None, None


def session_gate_state(repo, workspace, session_id):
    """gate_status.py's state for the newest record this session wrote for the repo, else UNKNOWN."""
    checkout, record = session_checkout(repo, workspace, session_id)
    if record is None:
        return "UNKNOWN"
    try:
        gate_status = gate_status_module()
        return gate_status.status(repo, workspace, gate_status.checkout_state_dir(repo, checkout)).get(
            gate_status.STATUS_STATE)
    except Exception:
        return "UNKNOWN"


def declared_items(workspace, session_id):
    """{repo: [item text]} from this session's work-product declaration (Harness/AGENTS/GATES.md, "Declaring the work
    product"), read from the session's home or the workspace (session_home.declaration_path)."""
    path = session_home.declaration_path(workspace, session_id)
    try:
        with open(path, encoding="utf-8") as handle:
            repos = json.load(handle).get("repos")
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(repos, dict):
        return {}
    return {repo: [entry["item"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("item"), str)]
            for repo, entries in repos.items() if isinstance(entries, list)}


def state_file(session_id, suffix, state_root=None):
    """<state root>\\<session><suffix>: the Stop gate's per-session state folder (claude_temp.stop_gate_state_root)."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import claude_temp
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or "unknown"))
    return os.path.join(state_root or claude_temp.stop_gate_state_root(), safe + suffix)


def canonical_path(path):
    """A path as one spelling: realpath expands an 8.3 short name (C:\\Users\\ADMINI~1) that abspath keeps, so a path
    written through TEMP and the long form git reports for the same folder compare equal."""
    return os.path.normcase(os.path.realpath(path))

def checkout_of(path, workspace):
    """(repo, checkout) for a file under a workspace repo's main checkout or one of its registered worktrees, the
    deepest match, else None. A worktree sits outside the workspace, so the main-checkout test alone misses it."""
    try:
        full = canonical_path(path)
        gate_status = gate_status_module()
        names = sorted(name for name in os.listdir(workspace)
                       if REPO_NAME.fullmatch(name) and os.path.isdir(os.path.join(workspace, name, ".git")))
    except (OSError, ValueError, RuntimeError):
        return None
    best = None
    for repo in names:
        for checkout in gate_status.checkouts(repo, workspace):
            root = canonical_path(checkout) + os.sep
            if full.startswith(root) and (best is None or len(root) > len(best[2])):
                best = (repo, checkout, root)
    return (best[0], best[1]) if best else None


def worked_checkouts(session_id, state_root=None):
    """{repo: [checkouts]} this session has written files in, as the Stop gate remembers it: the checkouts in its
    <session>.worked.json, and the main checkout for each repo in its <session>.repos.json (remember_edited_paths in
    invoke_stop_gate.py keeps that one)."""
    found = {}
    try:
        with open(state_file(session_id, ".worked.json", state_root), encoding="utf-8") as handle:
            stored = json.load(handle)
        for repo, checkouts in (stored.get("checkouts") or {}).items():
            if REPO_NAME.fullmatch(repo) and isinstance(checkouts, list):
                found[repo] = [c for c in checkouts if isinstance(c, str)]
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with open(state_file(session_id, ".repos.json", state_root), encoding="utf-8") as handle:
            repos = json.load(handle).get("repos", [])
    except (OSError, ValueError, AttributeError):
        repos = []
    for repo in repos:
        if isinstance(repo, str) and REPO_NAME.fullmatch(repo):
            found.setdefault(repo, [])
    return found


def remember_worked(session_id, worked, state_root=None):
    """Union {repo: [checkouts]} into <session>.worked.json and return the whole remembered set, so a checkout written
    in an earlier turn still owes its message after an allow."""
    remembered = worked_checkouts(session_id, state_root)
    merged = {repo: list(checkouts) for repo, checkouts in remembered.items()}
    for repo, checkouts in worked.items():
        if not REPO_NAME.fullmatch(repo):
            continue
        known = merged.setdefault(repo, [])
        for checkout in checkouts:
            if checkout and not any(os.path.normcase(checkout) == os.path.normcase(k) for k in known):
                known.append(checkout)
    if merged != remembered:
        try:
            path = state_file(session_id, ".worked.json", state_root)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"checkouts": merged}, handle, separators=(",", ":"))
        except OSError:
            pass
    return merged


def worked_repos(session_id, state_root=None):
    """The repos this session has written files in, by name."""
    return set(worked_checkouts(session_id, state_root))


def resolve_checkout(repo, workspace, session_id, explicit=None, state_root=None):
    """The checkout a session's message for `repo` covers: an explicit path (which must be the main checkout or a
    registered worktree), else the checkout of the newest gate record this session wrote, else a checkout the
    Stop gate remembers the session writing in (its own worktree first), else a worktree named for the session,
    else the main checkout."""
    gate_status = gate_status_module()
    registered = gate_status.checkouts(repo, workspace)
    same = same_path
    if explicit:
        match = next((c for c in registered if same(c, explicit)), None)
        if match is None:
            raise ValueError("{0} is not the main checkout or a registered worktree of {1}".format(explicit, repo))
        return match
    gated = session_checkout(repo, workspace, session_id)[0]
    if gated:
        return gated
    worked = [c for c in worked_checkouts(session_id, state_root).get(repo, []) if os.path.isdir(c)]
    for candidate in sorted(worked, key=lambda c: (str(session_id) not in c, c)):
        return candidate
    own = next((c for c in registered if str(session_id) in c), None)
    return own or os.path.join(workspace, repo)


def green_owned_checkout(repo, workspace, session_id):
    """The main checkout or this session's own worktree whose record is GREEN-CURRENT, whichever session ran that
    gate, else None: a session that worked in a repo owes its message once the tree is green, even when a peer's
    gate is the one that passed on it."""
    try:
        gate_status = gate_status_module()
        main = canonical_path(os.path.join(workspace, repo))
        for checkout in gate_status.checkouts(repo, workspace):
            owned = canonical_path(checkout) == main or session_id in checkout
            if owned and gate_status.status(repo, workspace, gate_status.checkout_state_dir(repo, checkout)).get(
                    gate_status.STATUS_STATE) == "GREEN-CURRENT":
                return checkout
    except (OSError, ValueError, RuntimeError):
        return None
    return None


def message_checkout(entry, repo, workspace, session_id, green_checkout=None):
    """The checkout a held message covers: the one it was written for, else this session's gated checkout, else the
    green checkout this session worked in, else the main checkout."""
    recorded = (entry or {}).get("checkout")
    if isinstance(recorded, str) and recorded:
        return recorded
    return session_checkout(repo, workspace, session_id)[0] or green_checkout or os.path.join(workspace, repo)


def landed_files(path):
    """The checkout's uncommitted files; none for a checkout that no longer exists, because a branch worktree is
    removed only once its change is on origin/main (Tools/AGENTS.md), so its message has landed."""
    return dirty_files(path) if os.path.isdir(path) else []


def this_machine():
    """The machine a held message is judged on, named as the gate records name it."""
    return os.environ.get("COMPUTERNAME", "")


def judged_elsewhere(entry):
    """Whether a held message covers a checkout on another machine, which only that machine can judge: crgolden/.claude
    syncs, so every machine holds the message. A message names its machine; one written before it did names a
    checkout that is absent here and outside this profile, which is another machine's path, not a removed worktree."""
    machine = (entry or {}).get("machine")
    if isinstance(machine, str) and machine:
        return machine.upper() != this_machine().upper()
    checkout = (entry or {}).get("checkout")
    if not isinstance(checkout, str) or not checkout or os.path.isdir(checkout):
        return False
    profile = os.path.normcase(os.path.abspath(os.path.expanduser("~")))
    return not os.path.normcase(os.path.abspath(checkout)).startswith(profile + os.sep)


def session_paths(session_id, repo, state_root=None):
    """Repo-relative paths the Stop gate recorded this session writing in the repo (the `paths` of its
    <session>.repos.json, kept by remember_edited_paths in invoke_stop_gate.py)."""
    try:
        with open(state_file(session_id, ".repos.json", state_root), encoding="utf-8") as handle:
            paths = (json.load(handle).get("paths") or {}).get(repo)
    except (OSError, ValueError, AttributeError):
        return set()
    return {p.replace("\\", "/") for p in paths if isinstance(p, str)} if isinstance(paths, list) else set()


def owed_files(checkout, repo, workspace, session_id, state_root=None, items=None):
    """The checkout's uncommitted files this session answers for: in its own worktree (a path naming its session
    id) every one; in a checkout it shares, those the Stop gate recorded it writing or its declaration items name,
    so a peer's files in the same checkout owe it nothing and never appear in its message."""
    files = landed_files(checkout)
    if not files or str(session_id) in os.path.normcase(os.path.abspath(checkout)):
        return files
    if items is None:
        items = declared_items(workspace, session_id).get(repo, [])
    own = session_paths(session_id, repo, state_root)
    return [f for f in files if f in own or any(work_product_items.names_path(item, [f]) for item in items)]


def peer_green_is_ours(checkout, repo, session_id, state_root=None):
    """Whether a checkout another session's gate passed on holds a dirty file this session itself wrote, as the
    Stop gate records it. A declared item names files but cannot tell a landed edit of this session's from a peer's
    later edit of the same file, so only a recorded write makes a peer's green tree this session's to message."""
    try:
        return bool(session_paths(session_id, repo, state_root) & set(landed_files(checkout)))
    except RuntimeError:
        return True


def answered_files(checkout, repo, workspace, session_id, state_root=None, items=None):
    """The files a session's message for one checkout must cover: every uncommitted file of a worktree (one change
    per worktree) and of a checkout this session gated, whatever the verdict, since its gate judged the whole tree;
    in the shared main checkout otherwise, owed_files."""
    gated = session_checkout(repo, workspace, session_id)[0]
    if not same_path(checkout, os.path.join(workspace, repo)) or (gated and same_path(gated, checkout)):
        return landed_files(checkout)
    return owed_files(checkout, repo, workspace, session_id, state_root, items)


def named_paths(message):
    """The paths a message's lines name: a bare path, a bulleted one or a backticked one."""
    return {re.sub(r"^[-*]\s+", "", line.strip()).strip("`") for line in message.splitlines()}


def commits_dir(workspace, session_id, repo=None):
    """Every repo's message sits in the session's parking lot under %TEMP%\\claude (session_home.state_root())."""
    return os.path.join(session_home.parking_dir(session_home.state_root(), session_id), "commits")


def commit_path(workspace, session_id, repo, key=None):
    """Where the repo's message for one checkout lives: its repo's store, named by the checkout's key (the repo's
    name for the main checkout, Repo@<worktree> for a worktree)."""
    return os.path.join(commits_dir(workspace, session_id, repo), (key or repo) + ".json")


def key_repo(key, entry):
    repo = entry.get("repo") if isinstance(entry, dict) else None
    return repo if isinstance(repo, str) and REPO_NAME.fullmatch(repo) else key.split("@", 1)[0]


def landed_path(workspace, session_id, repo, key=None):
    """Where committed keeps a landed message's page copy (the message plus landedAt): beside the session's messages,
    under a name held_messages never reads, so the page's update of that card still matches a file the CLI wrote."""
    return os.path.join(commits_dir(workspace, session_id, repo), (key or repo) + ".landed")


def written_messages(workspace, key):
    """The path of every session's message, and of every landed copy, under one checkout's key (the repo's name, or
    Repo@<worktree>)."""
    import glob
    repo = key_repo(key, {})
    return glob.glob(commit_path(workspace, "*", repo, key)) + glob.glob(landed_path(workspace, "*", repo, key))


def held_messages(workspace, session_id):
    """[(key, entry)] for every message the session holds in its parking lot; an unreadable message is {}."""
    held = []
    commits = commits_dir(workspace, session_id)
    if not os.path.isdir(commits):
        return held
    for name in sorted(os.listdir(commits)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(commits, name), encoding="utf-8") as handle:
                entry = json.load(handle)
            entry = entry if isinstance(entry, dict) else {}
        except (OSError, ValueError):
            entry = {}
        held.append((name[:-5], entry))
    return held


UNGATED_PREFIX = "UNGATED: "
UNPROVEN_VERDICTS = ("FAIL", "NOT REACHED", "NOT RUN")
STATE_DETAILS = {"GREEN-STALE": "inputs changed since the pass", "RUNNING": "the gate is still running",
                 "STOPPED": "the gate was stopped before its verdict",
                 "DRIFTED": "inputs drifted during the run", "UNKNOWN": "no gate record from this session"}


def checkout_gate(repo, workspace, session_id, checkout):
    """(state, record) for one checkout's own gate record: this session's record there and its state; GREEN-CURRENT
    with no record when another session's green record stands on a checkout this session owns (the main checkout or
    a worktree named for the session); else UNKNOWN with no record. A message for one worktree is never judged by
    another checkout's gate."""
    try:
        gate_status = gate_status_module()
        state_dir = gate_status.checkout_state_dir(repo, checkout)
        record = gate_status.read_record(repo, state_dir)
        state = gate_status.status(repo, workspace, state_dir).get(gate_status.STATUS_STATE) if record else None
    except (OSError, ValueError, RuntimeError):
        return "UNKNOWN", None
    if record and record.get("session") == session_id:
        return state or "UNKNOWN", record
    owned = same_path(checkout, os.path.join(workspace, repo)) or str(session_id) in checkout
    if record and owned and state == "GREEN-CURRENT":
        return "GREEN-CURRENT", None
    return "UNKNOWN", None


def other_checkout(repo, workspace, session_id, checkout):
    """Whether a message's checkout is one other than the repo's default (the checkout this session last gated, else
    the main checkout): such a message is judged by that checkout's own gate record."""
    if not checkout:
        return False
    default = session_checkout(repo, workspace, session_id)[0] or os.path.join(workspace, repo)
    return not same_path(checkout, default)


def gate_state(repo, workspace, session_id, checkout=None):
    """(state, record) a message records: its own checkout's when that is not the repo's default, else this
    session's newest record for the repo and its state."""
    if other_checkout(repo, workspace, session_id, checkout):
        return checkout_gate(repo, workspace, session_id, checkout)
    return session_gate_state(repo, workspace, session_id), session_checkout(repo, workspace, session_id)[1]


def landing_state(repo, workspace, session_id, checkout=None):
    """(state, detail, unproven steps) for the tree a message would land: its own checkout when that is not the
    repo's default, else the repo as this session last gated it. GREEN-CURRENT with no detail when this session's
    record holds on the current inputs, or a green record stands on a checkout this session owns; otherwise the
    state and what the record leaves unproven, the words the UNGATED line must carry: every step that failed or was
    never reached, else why the record is not a pass on these inputs."""
    if other_checkout(repo, workspace, session_id, checkout):
        state, record = checkout_gate(repo, workspace, session_id, checkout)
        if state == "GREEN-CURRENT":
            return "GREEN-CURRENT", "", []
    else:
        state = session_gate_state(repo, workspace, session_id)
        record = session_checkout(repo, workspace, session_id)[1]
        if state == "GREEN-CURRENT" or (record is None and green_owned_checkout(repo, workspace, session_id)):
            return "GREEN-CURRENT", "", []
    if record is None:
        return "UNKNOWN", STATE_DETAILS["UNKNOWN"], []
    steps = [step for step in record.get("steps") or [] if isinstance(step, dict)]
    failed = [step.get("name") for step in steps if step.get("verdict") == "FAIL" and step.get("name")]
    unreached = [step.get("name") for step in steps
                 if step.get("verdict") in UNPROVEN_VERDICTS[1:] and step.get("name")]
    parts = []
    if failed:
        parts.append("failed: " + ", ".join(failed))
    if unreached:
        parts.append("not reached: " + ", ".join(unreached))
    if not parts:
        parts.append(STATE_DETAILS.get(state, "the record is not a pass on these inputs"))
    return state, "; ".join(parts), failed + unreached


def ungated_line(repo, state, detail):
    """The line a message for an ungated tree opens with: UNGATED: <repo> <state>: <what is unproven>."""
    return "{0}{1} {2}: {3}".format(UNGATED_PREFIX, repo, state, detail)


def ungated_problem(text, repo, workspace, session_id, first_line_only=False, checkout=None):
    """The UNGATED line a message for repo must carry, when it does not: a line opening UNGATED: <repo> <state> that
    names every failed and unreached step. None when the tree is GREEN-CURRENT or the line is there. The parking-lot
    script holds the message's first line to it; the stop gate accepts it anywhere in the parked message."""
    state, detail, names = landing_state(repo, workspace, session_id, checkout)
    if state == "GREEN-CURRENT":
        return None
    head = "{0}{1} {2}".format(UNGATED_PREFIX, repo, state)
    lines = (text or "").splitlines()
    candidates = lines[:1] if first_line_only else lines
    for line in candidates:
        stripped = line.strip().lstrip("-* ").strip("`")
        if stripped.startswith(head) and all(name in stripped for name in names):
            return None
    return ungated_line(repo, state, detail)


def same_path(first, second):
    return canonical_path(first) == canonical_path(second)


def commit_key(repo, checkout, workspace):
    """The parking lot's id for a checkout's message: the repo's name for its main checkout, else the repo's name,
    an @ and the worktree's own name (its folder, or the folder above it when the worktree is named for the repo),
    so a session holding two checkouts of one repo parks one message per checkout."""
    if not checkout or same_path(checkout, os.path.join(workspace, repo)):
        return repo
    name = os.path.basename(os.path.normpath(checkout))
    if name.lower() == repo.lower():
        name = os.path.basename(os.path.dirname(os.path.normpath(checkout)))
    return repo + "@" + re.sub(r"[^A-Za-z0-9._-]", "-", name)


def problems(workspace, session_id, worked=None, state_root=None):
    """Every way the session's parking lot is missing or carrying a stale commit message. git runs only for a
    checkout this session worked in, green in this session or holding a message; a checkout whose status cannot be
    read is itself a problem. `worked` is {repo: [checkouts]} the session wrote files in (an empty list means the
    main checkout); None reads it from the Stop gate's state. A held message for a tree that is not GREEN-CURRENT
    must carry its UNGATED line, so an ungated landing never reads as ready, and a message written on another
    machine is judged only there."""
    worked = dict(worked) if worked is not None else worked_checkouts(session_id, state_root)
    found = []
    held = {}
    for key, entry in held_messages(workspace, session_id):
        held.setdefault(key_repo(key, entry), []).append((key, entry))
    declared = declared_items(workspace, session_id)
    for repo in sorted(os.listdir(workspace)):
        if not REPO_NAME.fullmatch(repo) or not os.path.isdir(os.path.join(workspace, repo, ".git")):
            continue
        main = os.path.join(workspace, repo)
        entries = held.get(repo, [])
        gated_checkout, record = session_checkout(repo, workspace, session_id)
        record = record or {}
        green_here = record.get("verdict") == "PASSED" \
            and session_gate_state(repo, workspace, session_id) in ("GREEN-CURRENT", "UNKNOWN")
        green_checkout = None
        if not green_here and repo in declared:
            green_checkout = green_owned_checkout(repo, workspace, session_id)
            if green_checkout is not None and not peer_green_is_ours(green_checkout, repo, session_id, state_root):
                green_checkout = None
            green_here = green_checkout is not None
        owed = {}
        if green_here:
            checkout = gated_checkout or green_checkout or main
            owed[canonical_path(checkout)] = (checkout, "its latest gate passed {0} and it is dirty".format(
                "in this session" if green_checkout is None else "(a peer's gate, on a checkout this session declared work in)"))
        if repo in worked:
            recorded = worked.get(repo, [])
            for checkout in [c for c in recorded if os.path.isdir(c)] if recorded else [main]:
                owed.setdefault(canonical_path(checkout), (
                    checkout, "this session wrote files in {0} and it is dirty (gated or not, the operator commits it)".format(checkout)))
        if not entries and not owed:
            continue
        covered = set()
        items = declared.get(repo, [])

        def answered_for(checkout):
            return answered_files(checkout, repo, workspace, session_id, state_root, items)

        for key, entry in entries:
            if judged_elsewhere(entry):
                continue
            judged = message_checkout(entry, repo, workspace, session_id, green_checkout)
            covered.add(canonical_path(judged))
            try:
                files = answered_for(judged)
            except RuntimeError as error:
                found.append(UNREADABLE_PROBLEM.format(key, error))
                continue
            wanted = ungated_problem(entry.get("message") or "", repo, workspace, session_id, checkout=judged) if files else None
            if not files:
                found.append("{0}: committed, but its message is still in the parking lot; run committed {1}".format(key, repo))
            elif wanted:
                found.append("{0}: its parked message reads as ready to land, but the tree is not GREEN-CURRENT; "
                             "rewrite it opening with this line and run commit {1} <message file>: {2}".format(key, repo, wanted))
            elif sorted(entry.get("files") or []) != files:
                found.append("{0}: the parking lot's message covers a different file list than git status now; "
                             "rewrite its file lines from " + STATUS_COMMAND + ", run commit {1} <message file>{2}, "
                             "and ArtifactData set the object it prints on commits/{0} again".format(
                                 key, repo, "" if same_path(judged, main) else ' --checkout "{0}"'.format(judged)))
        for normalised, (checkout, why) in sorted(owed.items()):
            if normalised in covered:
                continue
            try:
                files = answered_for(checkout)
            except RuntimeError as error:
                found.append(UNREADABLE_PROBLEM.format(repo, error))
                continue
            if not files:
                continue
            found.append("{0}: {1}, but the parking lot holds no commit message for it; run commit {0} <message file>{2}"
                         .format(repo, why, "" if same_path(checkout, main) else ' --checkout "{0}"'.format(checkout)))
    return found
