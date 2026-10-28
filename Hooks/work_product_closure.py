"""Whether a session's work product is closed in fact, not only marked closed: the Stop gate's QUEUED:, WAITING: and
DONE: paths read this beside the cadence hook's work_product_problem, which only sees each item's done flag.

Three ways a declaration can read closed while work is left:

  uncovered  a file the session edited is still uncommitted in the checkout the edit targeted, and no item names it:
             the edit came after the items were written. Only a dirty file counts, so an edit that has since landed
             never reopens anything.
  ungated    a done item's repo holds an uncommitted file the session wrote that a gate covers (gate_status.EXCLUDED
             leaves markdown and the like out), while that checkout's gate record is not GREEN-CURRENT. A line that
             waits on a gate (the slot, a peer's live gate, its own gate) is excused for every such repo, since one
             slot serializes every gate and none of them can start before it frees, but only when nothing else is
             open, and never for a repo whose last gate failed on exactly its current tree: that repo's next step
             is a fix, which no gate blocks.
  standing   the declaration's top-level "standing" list holds work that edits no file (a review, watching a peer):
             {"item": ..., "peer": "<the peer's session id, or operator>", "done": false, "approval": "<operator's
             words>"}. An open one closes nothing until it is done; a QUEUED: line is honest about it only when the
             line names its peer, by session id, since a session name is neither unique nor stable. Every standing item ever read is kept in a ledger, so deleting one leaves it open.
             An open one cancels the gate-wait excuse unless it is "pending" on a parked question still open, or
             carries "after" quoting four or more words of one declared repo item that is done and still holds an
             uncommitted file of this session's that it names: its next step is then that item's gate and landing.
  audited    the session's own session-audit holds an open verdict (outstanding, needs-operator, needs-gate) whose
             evidence names no open declared item (declared_items.py): the audit found work the declaration does not
             admit to, so no hand-off and no DONE: stands until it is declared or the verdict is closed. Once the
             audit's page has been published, a verdict newer than its last successful publish in the transcript
             holds the same way: the operator reads the page, not the cache.
  leftover   the session's worktree folder (<USERPROFILE>\\source\\worktrees\\<session id>) or its branch outlived the
             work: a worktree the session wrote in that is clean with nothing origin lacks, anything else in the folder
             (a scratch file, an empty folder), or a branch named for the session in a workspace repo with no
             worktree of it left. A worktree still holding unlanded work, or one the session has not written in yet,
             is live and leaves nothing behind.

Verified by Harness/Hooks/test_work_product_closure.py.
"""

import json
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SESSION_WORKTREES = os.path.join(os.environ.get("USERPROFILE", ""), "source", "worktrees")

STANDING_KEY = "standing"
AFTER_KEY = "after"
AFTER_WORDS = 4
RE_PEER_NAME = re.compile(r"[A-Za-z0-9][\w.-]*")

UNCOVERED_REASON = (
    "your work product is not closed: {count} file(s) this session edited are still uncommitted and no declared item "
    "names them: {files}. A file edited after the items were written is work the declaration does not admit to. Add "
    "an item naming it, with the operator's words that asked for it, in {path}.")
UNGATED_REASON = (
    "your work product is not closed: {repo} holds uncommitted file(s) a gate covers ({files}) and its gate record "
    "in {checkout} is {state}, so a done item there is written, not proven. Run its gate, or keep working.")
FAILED_HERE_REASON = (
    "your work product is not closed: {repo}'s last gate in {checkout} failed on exactly its current tree, so its "
    "next step is a fix, not a gate, and no wait on a gate excuses it. Read its failed step logs and fix them.")
STANDING_REASON = (
    "your work product is not closed: standing item(s) are still open: {items}. A QUEUED: line is honest about one "
    "only when it names its peer ({peers}); otherwise do that work now.")
AUDITED_REASON = (
    "your work product is not closed: this session's audit holds {count} open verdict(s) whose evidence names no open "
    "declared item: {rows}. The audit found work the declaration does not admit to. Declare it in {path} (a repos "
    "item, or a standing item for work that edits no file) with the operator's words as its approval, then re-record "
    "each verdict quoting four words of that item.")
UNRECORDED_REASON = (
    "your work product is not closed: this session's audit has no verdict for {count} operator message(s) other than "
    "the newest: {ids}. An unrecorded message is an ask nobody has checked was met. Run session_audit.py sync, then "
    "record each one, before handing off.")
STALE_PAGE_REASON = (
    "your work product is not closed: this session's audit page was published before its newest verdict ({at}), so "
    "the operator reads verdicts older than the audit's. Run session_audit.py page, then publish that file to the url "
    "it names, before handing off.")
LEFTOVER_REASON = (
    "your work product is not closed: this session's worktree work has landed but its leftovers remain: {items}. "
    "Remove them before handing off: git worktree remove <worktree> and git worktree prune from the repo's main "
    "checkout, git branch -d {session} there, then delete {folder}; git worktree list and git branch --list {session} "
    "confirm it. A branch git branch -d refuses holds commits origin lacks: report that to the operator instead.")
STANDING_UNAPPROVED_REASON = (
    "a standing item carries no approval quoting the operator's own words: {items}. Standing work is part of the work "
    "product only when the operator asked for it.")


def read_declaration(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def declared_items_by_repo(data):
    """{repo: [item text]} for every declared item, done or not."""
    repos = data.get("repos") if isinstance(data.get("repos"), dict) else {}
    return {repo: [entry.get("item") for entry in entries if isinstance(entry, dict) and isinstance(entry.get("item"), str)]
            for repo, entries in repos.items() if isinstance(entries, list)}


def done_repos(data):
    repos = data.get("repos") if isinstance(data.get("repos"), dict) else {}
    return {repo for repo, entries in repos.items() if isinstance(entries, list)
            and any(isinstance(entry, dict) and entry.get("done") is True for entry in entries)}


def standing_entries(data):
    entries = data.get(STANDING_KEY)
    return [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("item"), str)
            and entry["item"].strip()] if isinstance(entries, list) else []


def parked_standing(data, waits_on_open):
    """The open standing items that wait on the operator: each carries "pending" naming a parked question that
    `waits_on_open(entry)` finds still open. One pending on a question already answered or withdrawn waits on nothing,
    and with no judge none counts."""
    if not callable(waits_on_open):
        return set()
    return {entry["item"].strip() for entry in standing_entries(data)
            if entry.get("done") is not True and waits_on_open(entry)}


def after_target(quote, data):
    """(repo, entry) for the one declared repo item whose text holds an "after" quote of AFTER_WORDS or more words,
    compared case-insensitively with whitespace collapsed; None for a shorter quote, or one naming no item or several."""
    words = str(quote or "").lower().split()
    if len(words) < AFTER_WORDS:
        return None
    needle = " ".join(words)
    repos = data.get("repos") if isinstance(data.get("repos"), dict) else {}
    found = [(repo, entry) for repo, entries in repos.items() if isinstance(entries, list)
             for entry in entries if isinstance(entry, dict) and isinstance(entry.get("item"), str)
             and needle in " ".join(entry["item"].lower().split())]
    return found[0] if len(found) == 1 else None


def after_held_standing(data, edits, names_path):
    """The open standing items whose "after" link names a declared repo item that is done and not landed: its repo
    still holds a file this session edited, uncommitted, that the item names. Such a standing item's next step is that
    item's gate and landing, so it leaves the gate-wait excuse standing. A link naming no item, an item still open, or
    one whose files are all committed holds nothing."""
    held = set()
    for entry in standing_entries(data):
        if entry.get("done") is True or AFTER_KEY not in entry:
            continue
        target = after_target(entry.get(AFTER_KEY), data)
        if target is None or target[1].get("done") is not True:
            continue
        repo, item = target
        if any(edit_repo == repo and names_path(path_case(item["item"]), [path_case(relative)])
               for edit_repo, _, relative in edits):
            held.add(entry["item"].strip())
    return held


def own_worktree(checkout, session_id):
    """True when the checkout is this session's own worktree (<...>\\worktrees\\<session id>\\<Repo>): no other session
    writes there, so every uncommitted file in it is this session's."""
    parts = os.path.normcase(os.path.normpath(checkout)).split(os.sep)
    return bool(session_id) and len(parts) >= 3 and parts[-3] == "worktrees" and parts[-2] == os.path.normcase(session_id)


def dirty_paths(checkout, cache):
    """Repo-relative paths, forward slashes, that git reports changed or untracked in `checkout`; None when git cannot
    read it, which callers treat as nothing to judge rather than as clean."""
    key = os.path.normcase(os.path.abspath(checkout))
    if key not in cache:
        done = subprocess.run(["git", "-C", checkout, "-c", "core.fsmonitor=false", "-c", "core.quotePath=false",
                               "status", "-z", "--porcelain=v1", "--no-renames", "--untracked-files=all"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        cache[key] = ({entry[3:].replace("\\", "/") for entry in done.stdout.split("\0") if len(entry) > 3}
                      if done.returncode == 0 else None)
    return cache[key]


def landed_after(checkout, relative, since):
    """True when a commit on the checkout's HEAD made after `since` (POSIX seconds) changed the file: the session's
    edit landed, so the file's uncommitted bytes now are another session's."""
    if since is None:
        return False
    done = subprocess.run(["git", "-C", checkout, "-c", "core.fsmonitor=false", "log", "--since=@{0}".format(int(since)),
                           "--format=%H", "-1", "HEAD", "--", relative],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    return done.returncode == 0 and bool(done.stdout.strip())


def merge_in_progress(checkout):
    """True while the checkout holds an unconcluded merge (MERGE_HEAD): its staged bytes came partly from the merged
    commit, and concluding it is a commit, which only the operator makes."""
    done = subprocess.run(["git", "-C", checkout, "-c", "core.fsmonitor=false", "rev-parse", "-q", "--verify",
                           "MERGE_HEAD"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return done.returncode == 0


def merged_in(checkout, relative):
    """True when a file's working bytes equal the merged commit's (MERGE_HEAD): the merge brought them, not the
    session, so they are not its edit. A file the session resolved or changed differs from MERGE_HEAD and still
    counts."""
    done = subprocess.run(["git", "-C", checkout, "-c", "core.fsmonitor=false", "diff", "--quiet", "MERGE_HEAD", "--",
                           relative], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return done.returncode == 0


def dirty_session_edits(cadence, transcript_path, workspace, cache):
    """[(repo, checkout, relative path)] for every file this session edited that is still uncommitted in the checkout
    the edit targeted (a worktree's edits are judged in that worktree, never the main checkout). A file a commit
    changed after this session's last edit of it is left out: that edit landed, and the bytes there now belong to
    whoever changed the file since. In the session's own worktree no one else writes, so a dirty file there is always
    the session's, even when a commit it fast-forwarded onto changed the same file later; but mid-merge, a file whose
    bytes equal the merged commit's came from that commit and is nobody's edit here."""
    session_id = os.path.splitext(os.path.basename(transcript_path))[0]
    checkouts = cadence.workspace_checkouts(workspace)
    edit_times = cadence.session_file_edit_times(transcript_path) if hasattr(cadence, "session_file_edit_times") else {}
    found = []
    seen = set()
    merging = {}

    def from_the_merge(checkout, relative):
        if checkout not in merging:
            merging[checkout] = merge_in_progress(checkout)
        return merging[checkout] and merged_in(checkout, relative)
    for repo, file_path in cadence.session_repo_edits(transcript_path, workspace):
        absolute = os.path.normcase(os.path.abspath(file_path))
        matched = [(checkout, name) for checkout, name in checkouts
                   if name == repo and absolute.startswith(checkout + os.sep)]
        if not matched:
            continue
        checkout = max(matched, key=lambda pair: len(pair[0]))[0]
        relative = os.path.abspath(file_path)[len(checkout) + 1:].replace("\\", "/")
        if (checkout, relative) in seen:
            continue
        seen.add((checkout, relative))
        dirty = dirty_paths(checkout, cache)
        if dirty is not None and any(os.path.normcase(path) == os.path.normcase(relative) for path in dirty) \
                and (own_worktree(checkout, session_id) or not landed_after(checkout, relative, edit_times.get(absolute))) \
                and not from_the_merge(checkout, relative):
            found.append((repo, checkout, relative))
    found += [entry for entry in command_changed_files(cadence, transcript_path, workspace, cache, session_id, checkouts, seen)
              if not from_the_merge(entry[1], entry[2])]
    return found


def command_changed_files(cadence, transcript_path, workspace, cache, session_id, checkouts, seen):
    """[(repo, checkout, relative path)] for the dirty files a repo-changing command of this session (git apply, git
    checkout, npm ci...; cadence.session_command_checkouts) left in a checkout: every dirty file in the session's own
    worktree, and elsewhere each dirty file a declared item of this session names, since a peer's file there is named
    by no item of this session's. No edit tool records these bytes, so without this they would escape every check."""
    if not hasattr(cadence, "session_command_checkouts"):
        return []
    targets = cadence.session_command_checkouts(transcript_path, workspace)
    if not targets:
        return []
    data = read_declaration(cadence.work_product_path(transcript_path, workspace)) or {}
    items = declared_items_by_repo(data)
    found = []
    for checkout, repo in checkouts:
        if checkout not in targets:
            continue
        own = own_worktree(checkout, session_id)
        for relative in sorted(dirty_paths(checkout, cache) or ()):
            if (checkout, relative) in seen:
                continue
            if own or any(cadence.names_changed_path(path_case(item), [path_case(relative)]) for item in items.get(repo, [])):
                seen.add((checkout, relative))
                found.append((repo, checkout, relative))
    return found


def path_case(text):
    """Windows paths ignore case, so an edit to HOOKS/x.py is the file an item names as Hooks/x.py."""
    return text.lower() if os.name == "nt" else text


def uncovered_problem(data, edits, path, names_path):
    items = declared_items_by_repo(data)
    uncovered = sorted("{0}/{1}".format(repo, relative) for repo, _, relative in edits
                       if not any(names_path(path_case(item), [path_case(relative)]) for item in items.get(repo, [])))
    if not uncovered:
        return None
    shown = uncovered[:5] + (["and {0} more".format(len(uncovered) - 5)] if len(uncovered) > 5 else [])
    return UNCOVERED_REASON.format(count=len(uncovered), files=", ".join(shown), path=path)


def ungated_problem(data, edits, workspace, session_id, landing_state, excluded, gate_wait=False, failed_here=None,
                    blocked=None):
    """The first done item's repo whose session-written, gate-covered dirty files sit in a checkout that is not
    GREEN-CURRENT, as a reason, else None. With gate_wait, only a repo failed_here(repo, workspace, session_id,
    checkout) says last failed on its current tree counts; a checkout blocked(repo, checkout) says no gate may run
    in yet is excused too, unless it failed there."""
    repos = done_repos(data)
    by_checkout = {}
    for repo, checkout, relative in edits:
        if repo in repos and not any(pattern.search(relative) for pattern in excluded):
            by_checkout.setdefault((repo, checkout), []).append(relative)
    for (repo, checkout), files in sorted(by_checkout.items()):
        state = landing_state(repo, workspace, session_id, checkout)[0]
        if state == "GREEN-CURRENT":
            continue
        if failed_here is not None and failed_here(repo, workspace, session_id, checkout):
            return FAILED_HERE_REASON.format(repo=repo, checkout=checkout)
        if not gate_wait and not (blocked is not None and blocked(repo, checkout)):
            return UNGATED_REASON.format(repo=repo, files=", ".join(sorted(files)[:5]), checkout=checkout, state=state)
    return None


def standing_ledger_path(root, stem):
    return os.path.join(root, "work-products", stem + ".standing.json")


def open_standing(data, ledger_path):
    """[(item, peer)] for every standing item not marked done, counting one the ledger saw that the file no longer
    holds as open: only `"done": true` closes standing work."""
    current = {entry["item"].strip(): entry for entry in standing_entries(data)}
    try:
        with open(ledger_path, "r", encoding="utf-8") as handle:
            seen = json.load(handle)
    except (OSError, ValueError):
        seen = {}
    if not isinstance(seen, dict):
        seen = {}
    for item, entry in current.items():
        seen[item] = str(entry.get("peer") or "")
    try:
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "w", encoding="utf-8") as handle:
            json.dump(seen, handle)
    except OSError:
        pass
    return [(item, peer) for item, peer in sorted(seen.items())
            if item not in current or current[item].get("done") is not True]


def peer_names(peer):
    return {name.lower() for name in RE_PEER_NAME.findall(peer or "") if any(ch.isdigit() for ch in name) or "-" in name}


def line_names_peer(text, peer):
    words = {word.lower().rstrip(".-") for word in RE_PEER_NAME.findall(text or "")}
    return bool(peer_names(peer) & words)


def audited_problem(data, session_id, path, audit_root=None):
    """The reason this session's audit holds an operator message with no verdict, or an open verdict no open declared
    item names, else None."""
    import declared_items
    unrecorded = declared_items.unrecorded_messages(session_id, audit_root)
    if unrecorded:
        return UNRECORDED_REASON.format(count=len(unrecorded), ids=", ".join(i[:8] for i in unrecorded[:5]))
    rows = declared_items.undeclared_open_rows(data, declared_items.audit_verdicts_path(session_id, audit_root))
    if not rows:
        stale = declared_items.stale_page(session_id, audit_root)
        return STALE_PAGE_REASON.format(at=stale.isoformat()) if stale else None
    shown = ", ".join("{0} [{1}]".format(message_id[:8], status) for message_id, status in rows[:5])
    return AUDITED_REASON.format(count=len(rows), rows=shown, path=path)


def checkout_unlanded(checkout):
    """True when git reports the checkout dirty, or holding a commit no origin ref contains."""
    status = subprocess.run(["git", "-C", checkout, "-c", "core.fsmonitor=false", "status", "--porcelain",
                             "--untracked-files=all"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if status.returncode != 0:
        return False
    if status.stdout.strip():
        return True
    ahead = subprocess.run(["git", "-C", checkout, "-c", "core.fsmonitor=false", "rev-list", "--count", "HEAD", "--not",
                            "--remotes=origin"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0")


def branch_exists(main, branch):
    """True when the main checkout holds a local branch of that name, loose or packed; read from the ref files, so a
    Stop gate pays no git process per workspace repo."""
    git_dir = os.path.join(main, ".git")
    if os.path.isfile(os.path.join(git_dir, "refs", "heads", branch)):
        return True
    try:
        with open(os.path.join(git_dir, "packed-refs"), "r", encoding="utf-8") as handle:
            return any(line.rstrip("\n").endswith(" refs/heads/" + branch) for line in handle)
    except OSError:
        return False


def leftover_problem(cadence, transcript_path, workspace, session_id, worktrees_root=None):
    """The reason the session's worktree folder or branch outlived its work (the module's "leftover"), else None."""
    if not session_id:
        return None
    folder = os.path.join(SESSION_WORKTREES if worktrees_root is None else worktrees_root, session_id)
    left = []
    if os.path.isdir(folder):
        edited = [os.path.normcase(os.path.abspath(path)) for _, path in cadence.session_repo_edits(transcript_path, workspace)]
        commanded = set(cadence.session_command_checkouts(transcript_path, workspace) or ()) \
            if hasattr(cadence, "session_command_checkouts") else set()
        children = sorted(os.listdir(folder))
        for name in children:
            child = os.path.join(folder, name)
            if not os.path.exists(os.path.join(child, ".git")):
                left.append(child)
                continue
            key = os.path.normcase(os.path.abspath(child))
            worked = key in commanded or any(path.startswith(key + os.sep) for path in edited)
            if worked and not checkout_unlanded(child):
                left.append("the landed worktree " + child)
        if not children:
            left.append("the empty folder " + folder)
    repos = {name for _, name in cadence.workspace_checkouts(workspace)}
    for name in sorted(repos):
        if branch_exists(os.path.join(workspace, name), session_id) \
                and not os.path.exists(os.path.join(folder, name, ".git")):
            left.append("the branch {0} in {1}".format(session_id, name))
    if not left:
        return None
    return LEFTOVER_REASON.format(items="; ".join(left[:5]), session=session_id, folder=folder)


def done_problem(cadence, transcript_path, workspace, ledger_root=None, audit_root=None, worktrees_root=None):
    """A reason a DONE: claims a work product that is not closed in fact (an edit no item names, an open standing
    item, an open audit verdict no item names), else None. Whether DONE:'s repos are gated is the commit-message
    checks' to judge, not this one."""
    path = cadence.work_product_path(transcript_path, workspace)
    data = read_declaration(path)
    if data is None:
        return None
    root = ledger_root if ledger_root is not None else os.path.join(cadence.TEMP_CLAUDE_ROOT, cadence.SCAN_CACHE_DIRECTORY)
    stem = os.path.splitext(os.path.basename(transcript_path))[0]
    standing = open_standing(data, standing_ledger_path(root, stem))
    if standing:
        return STANDING_REASON.format(items="; ".join(item for item, _ in standing[:3]),
                                      peers=", ".join(peer for _, peer in standing[:3] if peer) or "none named")
    problem = audited_problem(data, stem, path, audit_root) or \
        leftover_problem(cadence, transcript_path, workspace, stem, worktrees_root)
    if problem:
        return problem
    edits = dirty_session_edits(cadence, transcript_path, workspace, {})
    return uncovered_problem(data, edits, path, cadence.names_changed_path)


def closure_problem(cadence, transcript_path, workspace, session_id, text, waits_on_gate, landing_state, excluded,
                    approved=None, ledger_root=None, failed_here=None, blocked=None, audit_root=None,
                    waits_on_open=None, worktrees_root=None):
    """A reason the work product is not closed in fact, else None. `text` is the stop's message, read only for the
    peers a QUEUED: line names; `waits_on_gate` is True when the stop waits on a gate (the slot or a peer's live gate);
    `approved(words)` judges a standing item's approval against the operator's messages; `failed_here` names a repo
    whose last gate failed on its current tree, and `blocked` a checkout no gate may run in yet (ungated_problem)."""
    path = cadence.work_product_path(transcript_path, workspace)
    data = read_declaration(path)
    if data is None:
        return None
    root = ledger_root if ledger_root is not None else os.path.join(cadence.TEMP_CLAUDE_ROOT, cadence.SCAN_CACHE_DIRECTORY)
    stem = os.path.splitext(os.path.basename(transcript_path))[0]
    standing = open_standing(data, standing_ledger_path(root, stem))
    unapproved = [entry["item"] for entry in standing_entries(data)
                  if approved is not None and not approved(str(entry.get("approval") or ""))]
    if unapproved:
        return STANDING_UNAPPROVED_REASON.format(items="; ".join(unapproved[:3]))
    cache = {}
    edits = dirty_session_edits(cadence, transcript_path, workspace, cache)
    problem = uncovered_problem(data, edits, path, cadence.names_changed_path) or \
        audited_problem(data, session_id, path, audit_root) or \
        leftover_problem(cadence, transcript_path, workspace, session_id, worktrees_root)
    if problem:
        return problem
    unnamed = [(item, peer) for item, peer in standing if not line_names_peer(text, peer)]
    if unnamed:
        return STANDING_REASON.format(items="; ".join(item for item, _ in unnamed[:3]),
                                      peers=", ".join(peer for _, peer in unnamed[:3] if peer) or "none named")
    parked = parked_standing(data, waits_on_open) | after_held_standing(data, edits, cadence.names_changed_path)
    unparked = [item for item, _ in standing if item not in parked]
    return ungated_problem(data, edits, workspace, session_id, landing_state, excluded,
                           gate_wait=waits_on_gate and not unparked, failed_here=failed_here, blocked=blocked)
