# GATE-STEP: Work-product closure harness
"""Regression harness for work_product_closure.py. Exits non-zero on any regression.

    python Harness/Hooks/test_work_product_closure.py

A declaration whose items all read done is still open while a dirty file the session edited is named by no item, while
a done item's repo holds gate-covered dirty files its checkout's record does not pass (excused only for a wait on a
gate with nothing else open), while a standing item is open and the line does not name its peer, or while the
session's own audit holds an open verdict no open declared item names (QUEUED: and DONE: alike). Real git repos, a
main checkout and a worktree, decide what is dirty; the cadence hook's transcript readers are stubbed.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import claude_temp  # noqa: E402
import work_product_closure as closure  # noqa: E402
import work_product_items  # noqa: E402

failures = []


class SkipAuditRows(Exception):
    """Raised when the workspace's session-audit script predates the eject catalog: the audit rows past the skill
    check are listed as skipped, never counted as a pass, and run once Tools carries it."""


EXCLUDED = [re.compile(r"\.md$")]


def record(name, ok, detail=""):
    print("{0}  {1}{2}".format("PASS" if ok else "FAIL", name, "" if ok else "  got: " + str(detail)))
    if not ok:
        failures.append(name)


def clear_readonly_and_retry(func, path, _):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd, "-c", "core.fsmonitor=false"] + list(args), check=True, capture_output=True)


root = tempfile.mkdtemp(prefix="closure-", dir=claude_temp.scratch_dir())
try:
    workspace = os.path.join(root, "workspace")
    main = os.path.join(workspace, "Tools")
    worktree = os.path.join(root, "worktrees", "s1", "Tools")
    os.makedirs(os.path.join(main, "Hooks"))
    git(main, "init", "-q")
    git(main, "config", "core.fsmonitor", "false")
    git(main, "config", "user.email", "fixture@example.invalid")
    git(main, "config", "user.name", "fixture")
    for name in ("Hooks/test_hook_dispatcher.py", "Hooks/invoke_stop_gate.py", "Hooks/STOP-HOOK.md",
                 "Hooks/landed.py"):
        with open(os.path.join(main, name), "w", encoding="utf-8") as handle:
            handle.write("original\n")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "fixture")
    git(main, "worktree", "add", "-q", worktree)
    git(worktree, "config", "core.fsmonitor", "false")

    def touch(checkout, name):
        with open(os.path.join(checkout, name), "a", encoding="utf-8") as handle:
            handle.write("edited\n")

    declaration = os.path.join(root, "work-products", "s1.json")
    os.makedirs(os.path.dirname(declaration))
    ledger = os.path.join(root, "ledger")
    transcript = os.path.join(root, "s1.jsonl")
    state = {"edits": []}
    cadence = types.SimpleNamespace(
        workspace_checkouts=lambda _: [(os.path.normcase(os.path.abspath(main)), "Tools"),
                                       (os.path.normcase(os.path.abspath(worktree)), "Tools")],
        session_repo_edits=lambda *_: list(state["edits"]),
        work_product_path=lambda *_: declaration,
        names_changed_path=work_product_items.names_path,
        TEMP_CLAUDE_ROOT=root, SCAN_CACHE_DIRECTORY="cache")

    def declare(items, standing=None):
        data = {"repos": {"Tools": [{"item": text, "done": True, "approval": "fixture"} for text in items]}}
        if standing is not None:
            data["standing"] = standing
        with open(declaration, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def judge(text="QUEUED: behind the slot.", waits_on_gate=True, gate_state="GREEN-CURRENT", approved=None,
              ledger_root=None, open_questions=()):
        return closure.closure_problem(cadence, transcript, workspace, "s1", text, waits_on_gate,
                                       lambda *_: (gate_state, "", []), EXCLUDED, approved=approved,
                                       ledger_root=ledger_root or ledger,
                                       waits_on_open=lambda entry: str(entry.get("pending") or "") in open_questions)

    # The incident: every item done, a later edit no item names, QUEUED: behind a held slot.
    touch(worktree, "Hooks/invoke_stop_gate.py")
    touch(worktree, "Hooks/test_hook_dispatcher.py")
    state["edits"] = [("Tools", os.path.join(worktree, "Hooks", "invoke_stop_gate.py")),
                      ("Tools", os.path.join(worktree, "Hooks", "test_hook_dispatcher.py"))]
    declare(["Hooks/invoke_stop_gate.py refuses the wait"])
    reason = judge(gate_state="RED")
    record("must-block: a worktree edit no item names keeps QUEUED: behind a held slot refused",
           reason is not None and "test_hook_dispatcher.py" in reason and "no declared item names" in reason, reason)
    declare(["Hooks/invoke_stop_gate.py refuses the wait", "Hooks/test_hook_dispatcher.py names the gate"])
    record("control: the same wait earns the stop once an item names the file", judge(gate_state="RED") is None,
           judge(gate_state="RED"))
    state["edits"] = [("Tools", os.path.join(worktree, "HOOKS", "invoke_stop_gate.py")),
                      ("Tools", os.path.join(worktree, "Hooks", "test_hook_dispatcher.py"))]
    declare(["Hooks/invoke_stop_gate.py refuses the wait", "Hooks/test_hook_dispatcher.py names the gate"])
    found = closure.dirty_session_edits(cadence, transcript, workspace, {})
    record("precondition: the case-spelled edit is matched to its checkout and found dirty, so the next row judges it",
           any(relative.lower() == "hooks/invoke_stop_gate.py" for _, _, relative in found), found)
    record("must-block: an edit path spelled in another case is still that file, named by the item",
           judge(gate_state="RED") is None, judge(gate_state="RED"))
    state["edits"] = [("Tools", os.path.join(worktree, "Hooks", "invoke_stop_gate.py")),
                      ("Tools", os.path.join(worktree, "Hooks", "test_hook_dispatcher.py"))]
    declare(["Hooks/invoke_stop_gate.py refuses the wait", "Hooks/dispatcher.py names the gate"])
    record("must-block: a name that only ends the file's name (dispatcher.py) does not cover test_hook_dispatcher.py",
           judge() is not None, judge())

    # Only a file still dirty in the checkout the edit targeted counts.
    state["edits"] = [("Tools", os.path.join(main, "Hooks", "landed.py"))]
    declare(["Hooks/invoke_stop_gate.py refuses the wait"])
    record("control: an edit that has landed (clean in its checkout) reopens nothing", judge() is None, judge())
    touch(main, "Hooks/landed.py")
    record("must-block: the same edit still uncommitted in the main checkout reopens the declaration",
           judge() is not None, judge())
    git(main, "checkout", "--", "Hooks/landed.py")

    # Ungated done work.
    state["edits"] = [("Tools", os.path.join(worktree, "Hooks", "invoke_stop_gate.py"))]
    declare(["Hooks/invoke_stop_gate.py refuses the wait"])
    reason = judge(text="QUEUED: on crgolden-37's review.", waits_on_gate=False, gate_state="RED")
    record("must-block: QUEUED: on a peer's non-gate work while a done item's repo is RED is refused",
           reason is not None and "RED" in reason and "written, not proven" in reason, reason)
    record("control: the same RED repo is excused for a wait on a gate with nothing else open",
           judge(waits_on_gate=True, gate_state="RED") is None, judge(waits_on_gate=True, gate_state="RED"))
    record("control: a GREEN-CURRENT checkout is proven", judge(waits_on_gate=False) is None, judge(waits_on_gate=False))

    # One slot serializes every gate: while a stop waits on one, every other ungated done repo is blocked too, except
    # one whose last gate failed on exactly its current tree, whose next step is a fix.
    touch(main, "Hooks/landed.py")
    state["edits"] = [("Tools", os.path.join(worktree, "Hooks", "invoke_stop_gate.py")),
                      ("Tools", os.path.join(main, "Hooks", "landed.py"))]
    declare(["Hooks/invoke_stop_gate.py refuses the wait", "Hooks/landed.py lands"])
    failed = {"checkout": None}

    def failed_here(repo, workspace_root, session_id, checkout):
        return failed["checkout"] is not None and os.path.normcase(checkout) == os.path.normcase(failed["checkout"])

    def judge_failed(waits):
        return closure.closure_problem(cadence, transcript, workspace, "s1", "QUEUED: behind the slot.", waits,
                                       lambda *_: ("RED", "", []), EXCLUDED, ledger_root=ledger, failed_here=failed_here)

    record("control: two ungated checkouts are both excused while the stop waits on a gate",
           judge_failed(True) is None, judge_failed(True))
    failed["checkout"] = os.path.abspath(main)
    reason = judge_failed(True)
    record("must-block: a checkout whose last gate failed on its current tree is refused even while a gate runs",
           reason is not None and "failed on exactly its current tree" in reason, reason)
    failed["checkout"] = None
    reason = judge_failed(False)
    record("control: with no gate wait the same ungated checkouts are refused as ungated",
           reason is not None and "written, not proven" in reason, reason)
    git(main, "checkout", "--", "Hooks/landed.py")
    touch(worktree, "Hooks/STOP-HOOK.md")
    state["edits"] = [("Tools", os.path.join(worktree, "Hooks", "STOP-HOOK.md"))]
    declare(["Hooks/STOP-HOOK.md states the wait"])
    record("control: a dirty file no gate covers (markdown) needs no gate",
           judge(waits_on_gate=False, gate_state="RED") is None, judge(waits_on_gate=False, gate_state="RED"))

    # Standing work.
    state["edits"] = [("Tools", os.path.join(worktree, "Hooks", "invoke_stop_gate.py"))]
    standing = [{"item": "Review crgolden-37's cadence-hook cache fix", "peer": "crgolden-37 / crgolden-fa",
                 "done": False, "approval": "work with crgolden-37 and review each other's changes"}]
    declare(["Hooks/invoke_stop_gate.py refuses the wait"], standing)
    reason = judge(text="QUEUED: on master's gate.")
    record("must-block: an open standing item refuses a QUEUED: line that does not name its peer",
           reason is not None and "standing item" in reason, reason)
    record("control: the line naming the peer by its ListAgents label earns the stop",
           judge(text="QUEUED: on crgolden-fa's diff.") is None, judge(text="QUEUED: on crgolden-fa's diff."))
    record("control: the line naming the peer by its parking-lot name earns the stop",
           judge(text="QUEUED: on crgolden-37's diff.") is None, judge(text="QUEUED: on crgolden-37's diff."))
    reason = judge(text="QUEUED: on crgolden-fa's diff.", gate_state="RED")
    record("must-block: an open standing item cancels the gate-wait excuse for a RED repo",
           reason is not None and "RED" in reason, reason)
    declare(["Hooks/invoke_stop_gate.py refuses the wait"], [dict(standing[0], pending="q78-run-state-cutovers")])
    reason = judge(text="WAITING: my gate, and crgolden-fa's diff.", gate_state="RED",
                   open_questions=("q78-run-state-cutovers",))
    record("control: a standing item pending on a parked question still open keeps the gate-wait excuse",
           reason is None, reason)
    reason = judge(text="WAITING: my gate, and crgolden-fa's diff.", gate_state="RED")
    record("must-block: a standing item pending on a question already answered waits on nothing, so no excuse",
           reason is not None and "RED" in reason, reason)
    reason = judge(text="WAITING: my gate, and crgolden-fa's diff.", waits_on_gate=False, gate_state="RED",
                   open_questions=("q78-run-state-cutovers",))
    record("must-block: a pending standing item excuses nothing when the stop waits on no gate",
           reason is not None and "RED" in reason, reason)

    # A standing item whose next step is a declared item's gate and landing links to it with "after".
    waited = "Hooks/invoke_stop_gate.py refuses the wait"
    linked = dict(standing[0], after="invoke_stop_gate.py refuses the wait")
    declare([waited], [linked])
    reason = judge(text="WAITING: my gate, and crgolden-fa's diff.", gate_state="RUNNING")
    record("must-pass: a standing item linked to a done item still uncommitted keeps the gate-wait excuse",
           reason is None, reason)
    reason = judge(text="WAITING: my gate, and crgolden-fa's diff.", waits_on_gate=False, gate_state="RUNNING")
    record("must-block: the same link excuses nothing when the stop waits on no gate",
           reason is not None and "RUNNING" in reason, reason)
    declare([waited], [standing[0]])
    reason = judge(text="WAITING: my gate, and crgolden-fa's diff.", gate_state="RUNNING")
    record("control: the same standing item with no link still cancels the gate-wait excuse",
           reason is not None and "RUNNING" in reason, reason)
    declare([waited], [dict(standing[0], after="a declared item nobody ever wrote")])
    reason = judge(text="WAITING: my gate, and crgolden-fa's diff.", gate_state="RUNNING")
    record("must-block: a link quoting no declared item holds nothing", reason is not None and "RUNNING" in reason,
           reason)
    declare([waited], [dict(standing[0], after="refuses the wait")])
    reason = judge(text="WAITING: my gate, and crgolden-fa's diff.", gate_state="RUNNING")
    record("must-block: a link of fewer than four words holds nothing", reason is not None and "RUNNING" in reason,
           reason)
    git(worktree, "checkout", "--", "Hooks/invoke_stop_gate.py")
    landed_link = {"repos": {"Tools": [{"item": waited, "done": True}]}, "standing": [linked]}
    held = closure.after_held_standing(landed_link, closure.dirty_session_edits(cadence, transcript, workspace, {}),
                                       work_product_items.names_path)
    record("must-block: a link whose item's files are all committed holds nothing, so the standing work is due now",
           held == set(), held)
    touch(worktree, "Hooks/invoke_stop_gate.py")
    held = closure.after_held_standing(landed_link, closure.dirty_session_edits(cadence, transcript, workspace, {}),
                                       work_product_items.names_path)
    record("control: the same link holds while the item's file is uncommitted again", held == {linked["item"]}, held)
    declare(["Hooks/invoke_stop_gate.py refuses the wait"], [])
    record("must-block: a standing item deleted after it was seen is still open",
           judge(text="QUEUED: on master's gate.") is not None, judge(text="QUEUED: on master's gate."))
    closed = [dict(standing[0], done=True)]
    declare(["Hooks/invoke_stop_gate.py refuses the wait"], closed)
    record("control: marking the standing item done closes it", judge(text="QUEUED: on master's gate.") is None,
           judge(text="QUEUED: on master's gate."))
    fresh = os.path.join(root, "ledger-approval")
    declare(["Hooks/invoke_stop_gate.py refuses the wait"], standing)
    reason = judge(text="QUEUED: on crgolden-fa.", approved=lambda words: False, ledger_root=fresh)
    record("must-block: a standing item whose approval is not the operator's words is refused",
           reason is not None and "approval" in reason, reason)
    record("control: the same item with an approval the operator wrote passes",
           judge(text="QUEUED: on crgolden-fa.", approved=lambda words: True, ledger_root=fresh) is None,
           judge(text="QUEUED: on crgolden-fa.", approved=lambda words: True, ledger_root=fresh))

    # DONE:
    done_ledger = os.path.join(root, "ledger-done")
    state["edits"] = [("Tools", os.path.join(worktree, "Hooks", "test_hook_dispatcher.py"))]
    declare(["Hooks/invoke_stop_gate.py refuses the wait"])
    reason = closure.done_problem(cadence, transcript, workspace, ledger_root=done_ledger)
    record("must-block: DONE: is refused while an edit no item names is dirty",
           reason is not None and "test_hook_dispatcher.py" in reason, reason)
    declare(["Hooks/invoke_stop_gate.py refuses the wait", "Hooks/test_hook_dispatcher.py names the gate"], standing)
    reason = closure.done_problem(cadence, transcript, workspace, ledger_root=done_ledger)
    record("must-block: DONE: is refused while a standing item is open, whatever the line names",
           reason is not None and "standing item" in reason, reason)
    declare(["Hooks/invoke_stop_gate.py refuses the wait", "Hooks/test_hook_dispatcher.py names the gate"], closed)
    record("control: DONE: passes with every edit named and the standing item done",
           closure.done_problem(cadence, transcript, workspace, ledger_root=done_ledger) is None,
           closure.done_problem(cadence, transcript, workspace, ledger_root=done_ledger))

    # The session's own audit: an open verdict no open declared item names is work the declaration hides.
    audits = os.path.join(root, "audits")
    verdicts = os.path.join(audits, "s1", "verdicts.jsonl")
    os.makedirs(os.path.dirname(verdicts))

    def audit(*rows):
        with open(verdicts, "w", encoding="utf-8") as handle:
            for message_id, status, evidence in rows:
                handle.write(json.dumps({"id": message_id, "status": status, "evidence": evidence}) + "\n")

    def judge_audit(text="QUEUED: behind the slot.", ledger_name="ledger-audit"):
        return closure.closure_problem(cadence, transcript, workspace, "s1", text, True,
                                       lambda *_: ("GREEN-CURRENT", "", []), EXCLUDED,
                                       ledger_root=os.path.join(root, ledger_name), audit_root=audits)

    def done_audit(ledger_name="ledger-audit-done"):
        return closure.done_problem(cadence, transcript, workspace, ledger_root=os.path.join(root, ledger_name),
                                    audit_root=audits)

    state["edits"] = [("Tools", os.path.join(worktree, "Hooks", "invoke_stop_gate.py"))]
    declare(["Hooks/invoke_stop_gate.py refuses the wait"])
    audit(("aaaaaaaa-0000-4000-8000-000000000001", "outstanding",
           "the triage gate proofs for Identity, Inventory and Infrastructure are unverified"))
    reason = judge_audit()
    record("must-block: an open audit verdict no declared item names refuses a QUEUED: line with every item done",
           reason is not None and "aaaaaaaa [outstanding]" in reason, reason)
    reason = done_audit()
    record("must-block: the same undeclared audit verdict refuses DONE:", reason is not None and "aaaaaaaa" in reason,
           reason)
    for status in ("needs-operator", "needs-gate"):
        audit(("aaaaaaaa-0000-4000-8000-000000000001", status, "unverified, waiting"))
        reason = judge_audit()
        record("must-block: an undeclared {0} verdict is open work too".format(status),
               reason is not None and status in reason, reason)
    audit(("aaaaaaaa-0000-4000-8000-000000000001", "outstanding", "first, unverified"),
          ("aaaaaaaa-0000-4000-8000-000000000001", "addressed", "Hooks/invoke_stop_gate.py:1 holds the fix"))
    record("control: a later closed verdict for the same message closes it", judge_audit() is None, judge_audit())
    audit(("aaaaaaaa-0000-4000-8000-000000000001", "stale-evidence", "cited line moved"))
    record("control: a stale-evidence verdict is the audit's to re-record, not undeclared work",
           judge_audit() is None, judge_audit())
    standing_audit = [{"item": "triage gate proofs for Identity, Inventory and Infrastructure on each next dirty "
                       "change", "peer": "operator", "done": False, "approval": "fixture"}]
    declare(["Hooks/invoke_stop_gate.py refuses the wait"], standing_audit)
    audit(("aaaaaaaa-0000-4000-8000-000000000001", "outstanding",
           "held by the standing item: triage gate proofs for Identity, Inventory and Infrastructure"))
    record("control: the verdict quoting an open standing item is declared (the standing check still holds the stop)",
           "audit holds" not in (judge_audit(text="QUEUED: on the operator.", ledger_name="ledger-audit-2") or ""),
           judge_audit(text="QUEUED: on the operator.", ledger_name="ledger-audit-2"))
    declare(["Hooks/invoke_stop_gate.py refuses the wait"], [dict(standing_audit[0], done=True)])
    reason = judge_audit(ledger_name="ledger-audit-3")
    record("must-block: once the item it names is done, the still-open verdict is undeclared again",
           reason is not None and "audit holds" in reason, reason)
    audit(("aaaaaaaa-0000-4000-8000-000000000001", "outstanding", "and in the of the to work"))
    declare(["Hooks/invoke_stop_gate.py refuses the wait", "work in the of the to repo"])
    reason = judge_audit(ledger_name="ledger-audit-4")
    record("must-block: evidence sharing only stopwords with an item names no item",
           reason is not None and "audit holds" in reason, reason)
    os.remove(verdicts)
    declare(["Hooks/invoke_stop_gate.py refuses the wait"])
    record("control: a session never audited here has no audit problem", judge_audit() is None, judge_audit())

    # An operator message with no verdict at all: cataloged and never recorded, or written after the last sync.
    messages = os.path.join(audits, "s1", "messages.jsonl")
    synced = os.path.join(root, "synced.jsonl")

    def catalog(*rows):
        with open(messages, "w", encoding="utf-8") as handle:
            for seq, message_id, ts in rows:
                handle.write(json.dumps({"id": message_id, "seq": seq, "ts": ts, "text": "ask"}) + "\n")

    def synced_transcript(*typed):
        with open(synced, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "assistant", "uuid": "a0", "message": {"content": "done"}}) + "\n")
        offset = os.path.getsize(synced)
        with open(synced, "a", encoding="utf-8") as handle:
            for message_id, ts in typed:
                handle.write(json.dumps({"type": "user", "uuid": message_id, "timestamp": ts, "origin": {"kind": "human"},
                                         "promptSource": "typed", "message": {"role": "user", "content": "later ask"}},
                                        separators=(",", ":")) + "\n")
        with open(os.path.join(audits, "s1", "state.json"), "w", encoding="utf-8") as handle:
            json.dump({"files": {synced: {"offset": offset}}}, handle)

    synced_transcript()
    catalog((1, "bbbbbbbb-0000-4000-8000-000000000001", "2026-01-01T00:00:00Z"),
            (2, "cccccccc-0000-4000-8000-000000000002", "2026-01-01T00:05:00Z"))
    audit()
    reason = judge_audit(ledger_name="ledger-unrecorded-1")
    record("must-block: a cataloged message with no verdict, not the newest, refuses QUEUED:",
           reason is not None and "bbbbbbbb" in reason and "cccccccc" not in reason, reason)
    reason = done_audit(ledger_name="ledger-unrecorded-done")
    record("must-block: the same unrecorded message refuses DONE:", reason is not None and "bbbbbbbb" in reason, reason)
    audit(("bbbbbbbb-0000-4000-8000-000000000001", "answered", "a0"))
    record("control: once it has a verdict, only the newest is left and it is exempt",
           judge_audit(ledger_name="ledger-unrecorded-2") is None, judge_audit(ledger_name="ledger-unrecorded-2"))
    synced_transcript(("dddddddd-0000-4000-8000-000000000003", "2026-01-01T00:10:00Z"),
                      ("eeeeeeee-0000-4000-8000-000000000004", "2026-01-01T00:15:00Z"))
    reason = judge_audit(ledger_name="ledger-unrecorded-3")
    record("must-block: a message written after the last sync, not the newest, refuses QUEUED:",
           reason is not None and "dddddddd" in reason and "eeeeeeee" not in reason, reason)
    record("must-block: the cataloged message it superseded as newest needs its verdict too",
           reason is not None and "cccccccc" in reason, reason)
    os.remove(messages)
    record("control: no catalog means never audited here, so nothing is unrecorded",
           judge_audit(ledger_name="ledger-unrecorded-4") is None, judge_audit(ledger_name="ledger-unrecorded-4"))

    # A published audit page older than the audit's newest verdict: the operator reads verdicts the audit replaced.
    page_url = "https://claude.ai/artifact/AuditPageFixture"
    page_file = os.path.join(audits, "s1", "session-audit.html")
    published = os.path.join(root, "published.jsonl")

    def verdict_at(at):
        with open(verdicts, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": "ffffffff-0000-4000-8000-000000000005", "status": "addressed",
                                     "evidence": "Hooks/invoke_stop_gate.py:1 holds the fix", "at": at}) + "\n")

    def publishes(*calls, offset_after=False, url=page_url):
        with open(published, "w", encoding="utf-8") as handle:
            for index, (ts, args, failed) in enumerate(calls):
                call = "toolu_page_{0}".format(index)
                handle.write(json.dumps({"type": "assistant", "timestamp": ts, "message": {"content": [
                    {"type": "tool_use", "id": call, "name": "Artifact", "input": args}]}}) + "\n")
                handle.write(json.dumps({"type": "user", "timestamp": ts, "message": {"content": [
                    {"type": "tool_result", "tool_use_id": call, "is_error": failed, "content": "result"}]}}) + "\n")
        state_row = {"files": {published: {"offset": os.path.getsize(published) if offset_after else 0}}}
        if url:
            state_row["page_url"] = url
        with open(os.path.join(audits, "s1", "state.json"), "w", encoding="utf-8") as handle:
            json.dump(state_row, handle)

    verdict_at("2026-01-02T00:10:00+00:00")
    publishes(("2026-01-02T00:05:00.000Z", {"file_path": page_file, "url": page_url}, False))
    reason = judge_audit(ledger_name="ledger-page-1")
    record("must-block: a verdict recorded after the audit page's last publish refuses QUEUED:",
           reason is not None and "audit page was published before" in reason, reason)
    reason = done_audit(ledger_name="ledger-page-done")
    record("must-block: the same stale page refuses DONE:",
           reason is not None and "audit page was published before" in reason, reason)
    publishes(("2026-01-02T00:05:00.000Z", {"file_path": page_file, "url": page_url}, False),
              ("2026-01-02T00:15:00.000Z", {"file_path": page_file, "url": page_url}, False))
    record("control: a publish after the newest verdict leaves the page current",
           judge_audit(ledger_name="ledger-page-2") is None, judge_audit(ledger_name="ledger-page-2"))
    publishes(("2026-01-02T00:15:00.000Z", {"file_path": page_file, "url": page_url}, False), offset_after=True)
    record("control: a sync whose offset passed the publish still counts that publish",
           judge_audit(ledger_name="ledger-page-3") is None, judge_audit(ledger_name="ledger-page-3"))
    publishes(("2026-01-02T00:15:00.000Z", {"file_path": page_file, "url": page_url}, True))
    reason = judge_audit(ledger_name="ledger-page-4")
    record("must-block: a publish that errored is no publish",
           reason is not None and "audit page was published before" in reason, reason)
    publishes(("2026-01-02T00:15:00.000Z", {"file_path": os.path.join(root, "other.html")}, False))
    reason = judge_audit(ledger_name="ledger-page-5")
    record("must-block: publishing another file is no publish of the audit page",
           reason is not None and "audit page was published before" in reason, reason)
    publishes(("2026-01-02T00:05:00.000Z", {"file_path": page_file}, False), url=None)
    record("control: an audit whose page was never published has no page to be stale",
           judge_audit(ledger_name="ledger-page-6") is None, judge_audit(ledger_name="ledger-page-6"))
    os.remove(verdicts)

    # A landed edit: a commit changed the file after this session's last edit of it, so its dirty bytes now are
    # another session's, never the session's undeclared work.
    landed_file = os.path.join(main, "Hooks", "landed.py")
    landed_key = os.path.normcase(os.path.abspath(landed_file))
    edited_at = time.time() - 120
    touch(main, "Hooks/landed.py")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "a peer lands the session's edit")
    touch(main, "Hooks/landed.py")
    state["edits"] = [("Tools", landed_file)]
    declare(["Hooks/invoke_stop_gate.py refuses the wait"])
    cadence.session_file_edit_times = lambda *_: {landed_key: edited_at}
    reason = judge(ledger_root=os.path.join(root, "ledger-landed"))
    record("control: a file a commit changed after the session's last edit of it is a peer's now, not undeclared work",
           reason is None, reason)
    cadence.session_file_edit_times = lambda *_: {landed_key: time.time() + 60}
    reason = judge(ledger_root=os.path.join(root, "ledger-landed-2"))
    record("must-block: the session's own edit made after that commit is still undeclared work",
           reason is not None and "landed.py" in reason, reason)
    cadence.session_file_edit_times = lambda *_: {}
    reason = judge(ledger_root=os.path.join(root, "ledger-landed-3"))
    record("must-block: an edit with no recorded time is never excused", reason is not None and "landed.py" in reason,
           reason)

    # The session's own worktree: a commit it fast-forwarded onto changed the file later, yet the dirty bytes are the
    # session's, since no other session writes there.
    own_file = os.path.join(worktree, "Hooks", "landed.py")
    own_key = os.path.normcase(os.path.abspath(own_file))
    touch(worktree, "Hooks/landed.py")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "origin changes the file after the session's edit")
    touch(worktree, "Hooks/landed.py")
    state["edits"] = [("Tools", own_file)]
    cadence.session_file_edit_times = lambda *_: {own_key: time.time() - 120}
    found = closure.dirty_session_edits(cadence, transcript, workspace, {})
    record("must-block: a dirty file in the session's own worktree stays the session's past a later commit to it",
           any(relative == "Hooks/landed.py" for _, _, relative in found), found)
    found = closure.dirty_session_edits(cadence, os.path.join(root, "s2.jsonl"), workspace, {})
    record("control: the same worktree judged for another session leaves the landed file out", found == [], found)

    # Bytes a command wrote (git apply, npm ci): no edit tool records them, so the checkout a repo-changing command ran
    # in is what makes its dirty files the session's, in the main checkout only where an item of the session names one.
    git(worktree, "checkout", "--", "Hooks/landed.py")
    git(main, "checkout", "--", "Hooks/landed.py")
    touch(main, "Hooks/invoke_stop_gate.py")
    touch(main, "Hooks/test_hook_dispatcher.py")
    state["edits"] = []
    cadence.session_file_edit_times = lambda *_: {}
    main_key = os.path.normcase(os.path.abspath(main))
    applied = "Hooks/invoke_stop_gate.py, applied from the kept draft"
    declare([applied])
    cadence.session_command_checkouts = lambda *_: set()
    found = closure.dirty_session_edits(cadence, transcript, workspace, {})
    record("control: with no repo-changing command, a dirty file no edit tool wrote is not the session's", found == [], found)
    cadence.session_command_checkouts = lambda *_: {main_key}
    found = closure.dirty_session_edits(cadence, transcript, workspace, {})
    relatives = sorted(relative for _, _, relative in found)
    record("must-block: a file a command left dirty in the main checkout is the session's when an item names it",
           "Hooks/invoke_stop_gate.py" in relatives, relatives)
    record("must-block: a dirty file there that no item of the session names stays a peer's",
           "Hooks/test_hook_dispatcher.py" not in relatives, relatives)
    touch(worktree, "Hooks/landed.py")
    state["edits"] = [("Tools", os.path.join(worktree, "Hooks", "landed.py"))]
    linked_to_applied = dict(standing[0], after="applied from the kept draft")
    declare([applied, "Hooks/landed.py waits for the slot"], [linked_to_applied])
    reason = judge(text="WAITING: my gate, and crgolden-fa's diff.", gate_state="RUNNING")
    record("must-pass: a standing item linked to an item whose files a command wrote keeps the gate-wait excuse",
           reason is None, reason)
    cadence.session_command_checkouts = lambda *_: set()
    reason = judge(text="WAITING: my gate, and crgolden-fa's diff.", gate_state="RUNNING")
    record("control: the same link with the command unseen holds nothing, so the standing item cancels the excuse",
           reason is not None and "RUNNING" in reason, reason)
    del cadence.session_command_checkouts
    git(main, "checkout", "--", "Hooks/invoke_stop_gate.py", "Hooks/test_hook_dispatcher.py")
    git(worktree, "checkout", "--", "Hooks/landed.py")

    # A pull the session cannot finish: mid-merge in its own worktree, a file whose bytes equal the merged commit's
    # came from that commit, while a file the session resolved stays its edit.
    git(main, "reset", "-q", "--hard")
    git(worktree, "reset", "-q", "--hard")
    incoming = subprocess.run(["git", "-C", main, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    for name, text in (("Hooks/incoming.py", "origin's new file\n"), ("Hooks/invoke_stop_gate.py", "origin's side\n")):
        with open(os.path.join(main, name), "w", encoding="utf-8") as handle:
            handle.write(text)
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "origin moves on")
    with open(os.path.join(worktree, "Hooks", "invoke_stop_gate.py"), "w", encoding="utf-8") as handle:
        handle.write("the session's side\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", "the session's commit")
    subprocess.run(["git", "-C", worktree, "-c", "core.fsmonitor=false", "merge", "--no-ff", "--no-commit", incoming],
                   capture_output=True)
    with open(os.path.join(worktree, "Hooks", "invoke_stop_gate.py"), "w", encoding="utf-8") as handle:
        handle.write("origin's side\nthe session's side\n")
    git(worktree, "add", "-A")
    record("precondition: the fixture's worktree is mid-merge", closure.merge_in_progress(worktree),
           closure.merge_in_progress(worktree))
    state["edits"] = [("Tools", os.path.join(worktree, "Hooks", "incoming.py")),
                      ("Tools", os.path.join(worktree, "Hooks", "invoke_stop_gate.py"))]
    cadence.session_file_edit_times = lambda *_: {}
    relatives = sorted(relative for _, _, relative in closure.dirty_session_edits(cadence, transcript, workspace, {}))
    record("must-block: mid-merge, a file whose bytes equal the merged commit's is not the session's edit",
           "Hooks/incoming.py" not in relatives, relatives)
    record("control: mid-merge, the file the session resolved stays its edit",
           "Hooks/invoke_stop_gate.py" in relatives, relatives)
    git(worktree, "merge", "--abort")
    record("control: with the merge aborted the checkout holds no merge in progress",
           not closure.merge_in_progress(worktree), closure.merge_in_progress(worktree))

    # Leftovers: the session's worktree folder and branch once its work has landed.
    worktrees_root = os.path.join(root, "worktrees")
    folder = os.path.join(worktrees_root, "s1")
    git(worktree, "reset", "-q", "--hard")
    state["edits"] = [("Tools", os.path.join(worktree, "Hooks", "landed.py"))]
    declare(["Hooks/landed.py waits for the slot"])

    def leftover():
        return closure.leftover_problem(cadence, transcript, workspace, "s1", worktrees_root)
    reason = leftover()
    record("control: a worktree the session wrote in that holds a commit origin lacks is live", reason is None, reason)
    git(worktree, "update-ref", "refs/remotes/origin/main", "HEAD")
    reason = leftover()
    record("must-block: the same worktree, clean with every commit on origin, is a leftover",
           reason is not None and "the landed worktree" in reason, reason)
    reason = closure.closure_problem(cadence, transcript, workspace, "s1", "QUEUED: behind the slot.", True,
                                     lambda *_: ("GREEN-CURRENT", "", []), EXCLUDED, ledger_root=ledger,
                                     worktrees_root=worktrees_root)
    record("must-block: a hand-off is refused while the landed worktree remains",
           reason is not None and "leftovers remain" in reason, reason)
    reason = closure.done_problem(cadence, transcript, workspace, ledger_root=os.path.join(root, "leftover-ledger"),
                                  worktrees_root=worktrees_root)
    record("must-block: DONE: is refused while the landed worktree remains",
           reason is not None and "leftovers remain" in reason, reason)
    touch(worktree, "Hooks/landed.py")
    reason = leftover()
    record("control: the same worktree holding an uncommitted file is live", reason is None, reason)
    git(worktree, "checkout", "--", "Hooks/landed.py")
    state["edits"] = []
    reason = leftover()
    record("control: a landed-looking worktree the session never wrote in is live, not a leftover", reason is None, reason)
    scratch = os.path.join(folder, "probe.ps1")
    with open(scratch, "w", encoding="utf-8") as handle:
        handle.write("scratch\n")
    reason = leftover()
    record("must-block: a scratch file in the session's worktree folder is a leftover",
           reason is not None and "probe.ps1" in reason, reason)
    os.remove(scratch)
    git(main, "worktree", "remove", "--force", worktree)
    reason = leftover()
    record("must-block: the empty folder a removed worktree leaves is a leftover",
           reason is not None and "the empty folder" in reason, reason)
    os.rmdir(folder)
    record("control: with the folder gone and no branch, nothing is left", leftover() is None, leftover())
    git(main, "branch", "s1")
    reason = leftover()
    record("must-block: a branch named for the session with no worktree of it is a leftover",
           reason is not None and "the branch s1 in Tools" in reason, reason)
    git(main, "pack-refs", "--all")
    reason = leftover()
    record("precondition: pack-refs moved the branch out of its loose ref file",
           not os.path.isfile(os.path.join(main, ".git", "refs", "heads", "s1")),
           os.listdir(os.path.join(main, ".git", "refs", "heads")))
    record("must-block: the same branch packed is still a leftover",
           reason is not None and "the branch s1 in Tools" in reason, reason)
    git(main, "branch", "-D", "s1")
    record("control: with the branch deleted, nothing is left", leftover() is None, leftover())

    # A stop that claims an honest stop is audited first (declared_items.audit_required_problem), in a fixed order:
    # declaration, audit run, reconcile matched, nothing after the sync, ejects cataloged, verdicts, declared rows.
    import declared_items
    token_audits = os.path.join(root, "token-audits")
    token_folder = os.path.join(token_audits, "s2")
    os.makedirs(token_folder)
    token_transcript = os.path.join(root, "token.jsonl")
    decisions = os.path.join(root, "decisions.log")
    declaration_file = os.path.join(root, "s2.json")

    def token_transcript_with(*typed):
        with open(token_transcript, "w", encoding="utf-8") as handle:
            for message_id, ts in typed:
                handle.write(json.dumps({"type": "user", "uuid": message_id, "timestamp": ts, "origin": {"kind": "human"},
                                         "promptSource": "typed", "message": {"role": "user", "content": "ask"}},
                                        separators=(",", ":")) + "\n")
        return os.path.getsize(token_transcript)

    def token_state(offset, reconciled=True):
        state = {"transcript": token_transcript, "chain": [{"path": token_transcript, "limit": None}],
                 "files": {token_transcript: {"offset": offset}}}
        if reconciled:
            state["reconciled"] = {token_transcript: offset}
        with open(os.path.join(token_folder, "state.json"), "w", encoding="utf-8") as handle:
            json.dump(state, handle)

    def token_catalog(*rows):
        with open(os.path.join(token_folder, "messages.jsonl"), "w", encoding="utf-8") as handle:
            for seq, (message_id, ts) in enumerate(rows, 1):
                handle.write(json.dumps({"id": message_id, "seq": seq, "ts": ts, "text": "ask", "channel": "typed",
                                         "file": token_transcript, "start": 0, "end": 0}) + "\n")

    def token_verdicts(*rows):
        with open(os.path.join(token_folder, "verdicts.jsonl"), "w", encoding="utf-8") as handle:
            for message_id, status, evidence in rows:
                handle.write(json.dumps({"id": message_id, "status": status, "evidence": evidence,
                                         "at": "2026-01-01T01:00:00+00:00"}) + "\n")

    def token_ejects(*stamps):
        with open(decisions, "w", encoding="utf-8") as handle:
            for stamp in stamps:
                handle.write("{0}\ts2\teject:filler-streak:4\n".format(stamp))
            handle.write("2026-01-01T00:20:00+00:00\tother-session\teject:filler-streak:4\n")

    def judge_token(data=None):
        return declared_items.audit_required_problem("s2", {"repos": {}} if data is None else data, declaration_file,
                                                     root=token_audits, decisions_log=decisions)

    first_id, second_id = "11111111-0000-4000-8000-000000000001", "22222222-0000-4000-8000-000000000002"
    reason = declared_items.audit_required_problem("s2", None, declaration_file, root=token_audits,
                                                   decisions_log=decisions)
    record("must-block: a token with no work-product declaration is refused first",
           reason is not None and "no work-product declaration" in reason and declaration_file in reason, reason)
    reason = judge_token()
    record("must-block: a session never audited is refused, and the reason says one message needs sync alone",
           reason is not None and "never run its session audit" in reason and "one message" in reason, reason)
    size = token_transcript_with((first_id, "2026-01-01T00:00:00Z"))
    token_state(size)
    token_catalog((first_id, "2026-01-01T00:00:00Z"))
    stub = os.path.join(root, "outdated_session_audit.py")
    with open(stub, "w", encoding="utf-8") as handle:
        handle.write("def reconciled_now(state):\n    return True\n")
    kept_script = declared_items.SESSION_AUDIT_SCRIPT
    declared_items.SESSION_AUDIT_SCRIPT = stub
    try:
        reason = judge_token()
    finally:
        declared_items.SESSION_AUDIT_SCRIPT = kept_script
    record("must-block: a session-audit script without the eject catalog is named, never passed",
           reason is not None and stub in reason, reason)
    try:
        audit = declared_items.session_audit_module()
        skill_problem = None if hasattr(audit, "session_ejects") and hasattr(audit, "eject_id") else (
            "session_audit.py at {0} predates the eject catalog (session_ejects, eject_id)".format(
                declared_items.SESSION_AUDIT_SCRIPT))
    except Exception as error:
        skill_problem = "session_audit.py at {0} did not load ({1})".format(declared_items.SESSION_AUDIT_SCRIPT, error)
    if skill_problem:
        print("SKIP  the nine audit rows past the skill check: {0}; they run once the workspace's Tools carries it"
              .format(skill_problem))
        raise SkipAuditRows()
    size = token_transcript_with((first_id, "2026-01-01T00:00:00Z"))
    token_state(size, reconciled=False)
    token_catalog((first_id, "2026-01-01T00:00:00Z"))
    reason = judge_token()
    record("must-block: a reconcile not matched at the current sync is refused",
           reason is not None and "reconcile has not matched" in reason, reason)
    token_state(size)
    record("control: one cataloged message, synced and reconciled, passes with no verdict (the newest is exempt)",
           judge_token() is None, judge_token())
    size = token_transcript_with((first_id, "2026-01-01T00:00:00Z"), (second_id, "2026-01-01T00:10:00Z"))
    reason = judge_token()
    record("must-block: an operator message written after the sync is refused before the verdict check",
           reason is not None and "written after the session audit's last sync" in reason, reason)
    token_state(size)
    token_catalog((first_id, "2026-01-01T00:00:00Z"), (second_id, "2026-01-01T00:10:00Z"))
    reason = judge_token()
    record("must-block: a cataloged message other than the newest with no verdict is refused",
           reason is not None and "no audit verdict" in reason and first_id[:8] in reason and second_id[:8] not in reason,
           reason)
    token_verdicts((first_id, "addressed", "Hooks/invoke_stop_gate.py:1 holds the fix"))
    record("control: with every message but the newest recorded, the token stands", judge_token() is None, judge_token())
    token_ejects("2026-01-01T00:05:00+00:00")
    reason = judge_token()
    record("must-block: an eject the catalog lacks is refused, and another session's eject is not counted",
           reason is not None and "ended 1 of this session's turn(s)" in reason, reason)
    token_catalog((first_id, "2026-01-01T00:00:00Z"), ("eject:2026-01-01T00:05:00+00:00", "2026-01-01T00:05:00+00:00"),
                  (second_id, "2026-01-01T00:10:00Z"))
    reason = judge_token()
    record("must-block: a cataloged eject with no verdict is refused like any message",
           reason is not None and "no audit verdict" in reason and "eject:20" in reason, reason)
    token_verdicts((first_id, "addressed", "Hooks/invoke_stop_gate.py:1 holds the fix"),
                   ("eject:2026-01-01T00:05:00+00:00", "answered", "a-explained"))
    record("control: an eject with a verdict stands", judge_token() is None, judge_token())
    token_verdicts((first_id, "outstanding", "the port fix in port.ts is unverified"),
                   ("eject:2026-01-01T00:05:00+00:00", "answered", "a-explained"))
    reason = judge_token()
    record("must-block: an open verdict naming no open declared item is refused",
           reason is not None and "names no open declared item" in reason, reason)
    reason = judge_token({"repos": {"App": [{"item": "the port fix in port.ts", "done": False}]}})
    record("control: the same verdict naming an open item stands", reason is None, reason)
except SkipAuditRows:
    pass
finally:
    shutil.rmtree(root, onerror=clear_readonly_and_retry)

if failures:
    print("\n{0} closure case(s) failed.".format(len(failures)))
    sys.exit(1)
print("\nAll closure cases passed.")
