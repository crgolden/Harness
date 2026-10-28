# GATE-LISTING: os.listdir(HERE) | Hooks/*
# GATE-LISTING: importlib.import_module(name) | Hooks/*
"""Regression harness for invoke_hook_dispatcher.py. Exits non-zero on any regression.

    python Harness/Hooks/test_hook_dispatcher.py
    python Harness/Hooks/test_hook_dispatcher.py --only launcher

Three layers, each pinned in both directions:

  * merge and failure policy, in-process against stub gates: precedence, every failure kind denying, the reply
    tools' carve-out, PostToolUse surfacing a failure instead of dropping it;
  * the real gates through the dispatcher: one must-deny control per gate beside a must-not-deny control;
  * LAUNCHER, the exact text settings.json registers, run in a child against a fixture USERPROFILE: dispatcher
    present, missing and broken, the one repair command, and the reply tools.

The git commands below are fixture data the gates must judge, so this file declares:
    PRETOOLUSE-GATE: git-write-strings-are-test-data
"""

import argparse
import importlib
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import harness_scope  # noqa: E402
import invoke_hook_dispatcher as dispatcher  # noqa: E402
import invoke_peer_message_gate  # noqa: E402
import invoke_read_scope_gate  # noqa: E402
import session_home  # noqa: E402

HERE =os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(os.environ.get("USERPROFILE", ""), ".claude", "settings.json")


def owned_workspace(directory):
    """A workspace this harness owns (an Identity repo with its guide, a Tools repo with a status doc), handed to every
    gate that resolves the workspace's docs, so the real cases judge the live gate code against docs the harness
    wrote, never the caller's. Returns the paths the cases use."""
    import invoke_agents_doc_gate
    import invoke_comment_reminder
    workspace = os.path.join(directory, "workspace")
    for repo in ("Identity", "Tools"):
        os.makedirs(os.path.join(workspace, repo, ".git"))
    guide = session_home.workspace_doc("AGENTS/REPOS/Identity.md", workspace, must_exist=False)
    status = os.path.join(workspace, "Tools", "SearchConsole", "AGENTS.md")
    for path, text in ((guide, "# Identity\n\nRead me first.\n"), (status, "# Search Console\n")):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    invoke_agents_doc_gate.WORKSPACE = workspace
    invoke_comment_reminder.WORKSPACE = workspace
    return {"workspace": workspace, "status": status, "guide": guide,
            "repo_file": os.path.join(workspace, "Identity", "Pages", "Index.cshtml")}
SHORT_DEADLINE = 0.5
PEER = "uds:\\\\.\\pipe\\LOCAL\\cc-msg-fixture"


def shell_stub(decision=None, reason="", error=None):
    """The shell gate's evaluate() alone returns (decision, reason, exit code)."""
    module = stub(decision, reason, error)
    two = module.evaluate
    module.evaluate = lambda *args, **kwargs: two(*args, **kwargs) + (0,)
    return module


def stub(decision=None, reason="", error=None, reminder=None, block=None):
    """A stand-in gate module: decide()/evaluate()/reminder() return what the case needs, raise, or block."""
    def answer(*_, **__):
        if block is not None:
            block.wait()
        if error is not None:
            raise error
        return (decision, reason)

    module = types.SimpleNamespace(decide=answer, DENY=dispatcher.DENY)
    module.evaluate = answer
    module.reminder = lambda *_: (_ for _ in ()).throw(error) if error is not None else reminder
    module.in_scope = lambda *_: True
    module.load_lint = lambda: None
    module.gate_change = lambda *_: None
    return module


def importer_for(modules):
    def load(name):
        if name not in modules:
            raise ImportError("no module named " + name)
        return modules[name]
    return load


def every_gate(**overrides):
    """Stub modules for every registered gate, deferring unless overridden."""
    modules = {name: stub(dispatcher.DEFER) for name, _, _ in dispatcher.PRE_GATES}
    modules["invoke_pretooluse_gate"] = shell_stub(dispatcher.DEFER)
    modules.update({name: stub(reminder=None) for name, _, _ in dispatcher.POST_REMINDERS})
    modules["session_quarantine"] = types.SimpleNamespace(halt_envelope=lambda *_: None, quarantine=lambda *_: "")
    modules["claude_temp"] = types.SimpleNamespace(share_problem=lambda: None)
    modules.update(overrides)
    return modules


def goal_cases(directory):
    """The real goal gate: no tool but the reply tools and AskUserQuestion runs until the operator pins a goal."""
    import invoke_goal_gate
    state = os.path.join(directory, "goal-state")
    none = write_transcript(os.path.join(directory, "goal-none"), [], goal=False)
    pinned = write_transcript(os.path.join(directory, "goal-pinned"), [])
    retired = write_transcript(os.path.join(directory, "goal-retired"),
                               [operator_message("GOAL: none")], goal=False)
    quoted = write_transcript(os.path.join(directory, "goal-peer"), [peer_question()], goal=False)
    second = write_transcript(os.path.join(directory, "goal-second"), [
        operator_message("audit the hooks"), operator_message("GOAL: audit the hooks")], goal=False)
    third = write_transcript(os.path.join(directory, "goal-third"), [
        operator_message("audit the hooks"), operator_message("start with the dispatcher"),
        operator_message("GOAL: audit the hooks")], goal=False)
    repinned = write_transcript(os.path.join(directory, "goal-repinned"), [
        operator_message("GOAL: audit the hooks"), operator_message("next"), operator_message("more"),
        operator_message("GOAL: ship the dispatcher")], goal=False)
    injected = write_transcript(os.path.join(directory, "goal-injected"), [
        dict(operator_message("the operator's pinned GOAL: whatever the skill says"), isMeta=True)], goal=False)
    queued = write_transcript(os.path.join(directory, "goal-queued"), [{
        "type": "attachment", "attachment": {"type": "queued_command", "origin": {"kind": "human"},
                                             "prompt": "GOAL: audit the hooks"}}], goal=False)
    goal_set = {"type": "attachment", "attachment": {"type": "goal_status", "met": False, "sentinel": True,
                                                     "condition": "audit the hooks"}}
    goal_met = {"type": "attachment", "attachment": {"type": "goal_status", "met": True,
                                                     "condition": "audit the hooks"}}
    command = write_transcript(os.path.join(directory, "goal-command"), [
        goal_set, dict(operator_message("Goal set: audit the hooks"), origin={"kind": "auto-continuation"})],
        goal=False)
    met = write_transcript(os.path.join(directory, "goal-met"), [goal_set, goal_met], goal=False)
    late_command = write_transcript(os.path.join(directory, "goal-late-command"), [
        operator_message("audit the hooks"), operator_message("start with the dispatcher"), goal_set], goal=False)
    mid_sentence = write_transcript(os.path.join(directory, "goal-mid-sentence"), [
        operator_message("your GOAL: audit the hooks")], goal=False)

    saved = os.path.join(directory, "tool-results", "hook-fixture-1-additionalContext.txt")

    def started(stamp="2026-10-07T13:00:00Z", text=None):
        body = text if text is not None else (
            "<persisted-output>\nOutput too large (14KB). Full output saved to: " + saved + "\n\nPreview (first 2KB):\n")
        return {"type": "attachment", "timestamp": stamp, "attachment": {
            "type": "hook_additional_context", "hookEvent": "SessionStart", "hookName": "SessionStart:startup",
            "content": [body]}}

    def read_of(path, error=False, call_id="toolu_start"):
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": call_id, "name": "Read", "input": {"file_path": path}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": call_id, "is_error": error, "content": "rules"}]}}]

    unread = write_transcript(os.path.join(directory, "start-unread"), [started()])
    read = write_transcript(os.path.join(directory, "start-read"), [started()] + read_of(saved))
    read_upper = write_transcript(os.path.join(directory, "start-read-upper"), [started()] + read_of(saved.upper()))
    failed_read = write_transcript(os.path.join(directory, "start-failed-read"), [started()] + read_of(saved, True))
    read_before = write_transcript(os.path.join(directory, "start-read-before"), read_of(saved) + [started()])
    reread = write_transcript(os.path.join(directory, "start-compacted"),
                              [started()] + read_of(saved) + [started("2026-10-07T15:00:00Z")])
    inline = write_transcript(os.path.join(directory, "start-inline"), [started(text="AGENT_MACHINE_TYPE=x\nrules")])
    legacy_start = write_transcript(os.path.join(directory, "start-legacy"), [started("2026-10-07T09:00:00Z")])
    pages = os.path.join(directory, "start-workspace")
    os.makedirs(pages, exist_ok=True)
    os.makedirs(session_home.parking_dir(None, "s-paged"))
    with open(os.path.join(session_home.parking_dir(None, "s-paged"), "state.json"), "w", encoding="utf-8") as handle:
        json.dump({"url": "https://claude.ai/artifact/fixture", "name": "crgolden-99"}, handle)

    def judge(tool, transcript, session):
        return invoke_goal_gate.evaluate(tool, transcript, session, state)[0]

    board_url = invoke_goal_gate.coordination.config()[0]
    profile_skill = os.path.join(os.environ.get("USERPROFILE", ""), ".claude", "skills", "coordination")
    prompt_rule_at = datetime(2026, 10, 23, 11, 0, tzinfo=timezone.utc)

    def registration(tool, tool_input):
        return invoke_goal_gate.evaluate(tool, none, "g-none", state, tool_input=tool_input)[0]

    def stamped(record):
        return dict(record, timestamp="2026-10-23T12:00:00Z")

    def row_write(doc_id):
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "tu-reg", "name": "ArtifactData", "input": {
                        "action": "set", "url": board_url, "collection": "sessions", "doc_id": doc_id,
                        "data": {"sessionId": doc_id}}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "tu-reg", "content": "committed"}]}}]

    stop_feedback = {"type": "user", "isMeta": True, "message": {"content": "Stop hook feedback:\nSTOP GATE: x"}}
    unregistered_goal = write_transcript(os.path.join(directory, "goal-unregistered"),
                                         [stamped(operator_message("GOAL: exercise the real gates"))], goal=False)
    registered_goal = write_transcript(os.path.join(directory, "goal-registered"),
                                       [stamped(operator_message("register")), stop_feedback]
                                       + row_write("g-registered")
                                       + [operator_message("GOAL: exercise the real gates")], goal=False)
    unconsented_goal = write_transcript(os.path.join(directory, "goal-unconsented"),
                                        [stamped(operator_message("register"))] + row_write("g-unconsented")
                                        + [operator_message("GOAL: exercise the real gates")], goal=False)
    slash_record = {"type": "user", "message": {"content": (
        "<command-name>/goal</command-name>\n<command-message>goal</command-message>\n"
        "<command-args>exercise the real gates</command-args>")}}
    slash_goal = write_transcript(os.path.join(directory, "goal-slash-consented"),
                                  [stamped(operator_message("register")), slash_record] + row_write("g-slash")
                                  + [goal_set], goal=False)
    feedback_unconsented = write_transcript(os.path.join(directory, "goal-feedback-unconsented"),
                                            [stamped(operator_message("register"))] + row_write("g-feedback")
                                            + [operator_message("GOAL: exercise the real gates"), stop_feedback],
                                            goal=False)

    def start_judge(tool, transcript, session="s-unpaged", tool_input=None):
        return invoke_goal_gate.evaluate(tool, transcript, session, state, tool_input=tool_input or {},
                                         workspace=pages, effective_at=effective)[0]

    render = 'python "C:\\skills\\parking-lot\\scripts\\parking_lot.py" render crgolden-99'

    def resumed_scan():
        """A scan cached while the file was unread, then the Read appended: the next call sees it."""
        path = write_transcript(os.path.join(directory, "start-resumed"), [started()])
        first = start_judge("Edit", path, "s-resumed-paged")
        with open(path, "a", encoding="utf-8") as handle:
            for record in read_of(saved):
                handle.write(json.dumps(record) + "\n")
        return first, start_judge("Edit", path, "s-resumed-paged")

    def shrunk_scan():
        """A scan cached past a clean Read, then the transcript rewritten shorter with no Read: it is scanned again."""
        path = write_transcript(os.path.join(directory, "start-shrunk"), [started()] + read_of(saved))
        first = start_judge("Edit", path, "s-shrunk-paged")
        write_transcript(os.path.join(directory, "start-shrunk"), [started()])
        return first, start_judge("Edit", path, "s-shrunk-paged")

    for name in ("s-resumed-paged", "s-shrunk-paged"):
        os.makedirs(session_home.parking_dir(None, name))
        with open(os.path.join(session_home.parking_dir(None, name), "state.json"), "w", encoding="utf-8") as handle:
            json.dump({"url": "https://claude.ai/artifact/fixture"}, handle)

    def first_record_at(stamp):
        path = os.path.join(directory, "goal-legacy-" + stamp[:10] + ".jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "user", "timestamp": stamp, "origin": {"kind": "human"},
                                     "message": {"content": "audit the hooks"}}) + "\n")
        return path

    effective = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
    landed = os.path.join(directory, "goal-gate-landed.py")
    with open(landed, "w", encoding="utf-8") as handle:
        handle.write("\n")
    os.utime(landed, (effective.timestamp(), effective.timestamp()))

    return [
        ("goal: a session with no GOAL: pin is denied a tool call", lambda: judge("Read", none, "g-none"),
         lambda r: r == dispatcher.DENY),
        ("goal: a session with no pin can still ask for one", lambda: judge("AskUserQuestion", none, "g-none"),
         lambda r: r == dispatcher.DEFER),
        ("goal: a session with no pin can still answer a peer", lambda: judge("SendMessage", none, "g-none"),
         lambda r: r == dispatcher.DEFER),
        ("goal: a session with no pin may write its own row on the coordination board",
         lambda: registration("ArtifactData", {"action": "set", "url": board_url, "collection": "sessions",
                                               "doc_id": "g-none"}),
         lambda r: r == dispatcher.DEFER),
        ("goal: a session with no pin may query the coordination board",
         lambda: registration("ArtifactData", {"action": "query", "url": board_url, "collection": "sessions"}),
         lambda r: r == dispatcher.DEFER),
        ("goal control: a set of another session's row is still denied with no pin",
         lambda: registration("ArtifactData", {"action": "set", "url": board_url, "collection": "sessions",
                                               "doc_id": "g-other"}),
         lambda r: r == dispatcher.DENY),
        ("goal control: a delete on the board is still denied with no pin",
         lambda: registration("ArtifactData", {"action": "delete", "url": board_url, "collection": "sessions",
                                               "doc_id": "g-none"}),
         lambda r: r == dispatcher.DENY),
        ("goal control: a batch with one write beside the session's own row is still denied with no pin",
         lambda: registration("ArtifactData", {"action": "batch", "url": board_url, "writes": [
             {"op": "set", "collection": "sessions", "doc_id": "g-none"},
             {"op": "set", "collection": "gates", "doc_id": "g-none"}]}),
         lambda r: r == dispatcher.DENY),
        ("goal control: an ArtifactData call on any other artifact is still denied with no pin",
         lambda: registration("ArtifactData", {"action": "query", "url": board_url + "-other"}),
         lambda r: r == dispatcher.DENY),
        ("goal: a session with no pin may load the coordination skill",
         lambda: registration("Skill", {"skill": "coordination"}), lambda r: r == dispatcher.DEFER),
        ("goal control: any other skill is still denied with no pin",
         lambda: registration("Skill", {"skill": "parking-lot"}), lambda r: r == dispatcher.DENY),
        ("goal control: a plugin skill that is also named coordination is still denied with no pin",
         lambda: registration("Skill", {"skill": "someplugin:coordination"}), lambda r: r == dispatcher.DENY),
        ("goal: a session with no pin may read the coordination skill through its ~/.claude/skills junction",
         lambda: registration("Read", {"file_path": os.path.join(profile_skill, "SKILL.md")}),
         lambda r: r == dispatcher.DEFER),
        ("goal: a session with no pin may read the coordination skill through its workspace .claude/skills junction",
         lambda: registration("Read", {"file_path": os.path.join(invoke_goal_gate.session_home.workspace_root(), ".claude",
                                                                 "skills", "coordination", "SKILL.md")}),
         lambda r: r == dispatcher.DEFER),
        ("goal control: a Read of another skill through the workspace .claude/skills is still denied with no pin",
         lambda: registration("Read", {"file_path": os.path.join(invoke_goal_gate.session_home.workspace_root(), ".claude",
                                                                 "skills", "parking-lot", "SKILL.md")}),
         lambda r: r == dispatcher.DENY),
        ("goal: a session with no pin may read coordination.json",
         lambda: registration("Read", {"file_path": invoke_goal_gate.coordination.CONFIG_PATH}),
         lambda r: r == dispatcher.DEFER),
        ("goal control: a Read of another skill is still denied with no pin",
         lambda: registration("Read", {"file_path": os.path.join(os.path.dirname(profile_skill), "parking-lot",
                                                                 "SKILL.md")}),
         lambda r: r == dispatcher.DENY),
        ("goal control: a Read of any other folder named skills\\coordination is still denied with no pin",
         lambda: registration("Read", {"file_path": os.path.join(directory, "skills", "coordination", "SKILL.md")}),
         lambda r: r == dispatcher.DENY),
        ("goal: a session with no pin may run a bare Get-Date for its row's time",
         lambda: registration("PowerShell", {"command": "Get-Date -AsUTC -Format o"}), lambda r: r == dispatcher.DEFER),
        ("goal control: Get-Date with a second command chained on is still denied with no pin",
         lambda: registration("PowerShell", {"command": "Get-Date; Remove-Item x"}), lambda r: r == dispatcher.DENY),
        ("goal control: Get-Date running a subexpression is still denied with no pin",
         lambda: registration("PowerShell", {"command": "Get-Date -Date (Remove-Item x)"}),
         lambda r: r == dispatcher.DENY),
        ("goal control: Get-Date redirected into a file is still denied with no pin",
         lambda: registration("PowerShell", {"command": "Get-Date > x"}), lambda r: r == dispatcher.DENY),
        ("goal: every call the coordination skill prescribes runs before a goal, in order",
         lambda: [registration(tool, tool_input) for tool, tool_input in (
             ("Skill", {"skill": "coordination"}),
             ("Read", {"file_path": os.path.join(session_home.workspace_root(), "Tools", "Claude", "skills",
                                                 "coordination", "SKILL.md")}),
             ("Read", {"file_path": invoke_goal_gate.coordination.CONFIG_PATH}),
             ("PowerShell", {"command": "Get-Date -AsUTC -Format o"}),
             ("ArtifactData", {"action": "set", "url": board_url, "collection": "sessions", "doc_id": "g-none",
                               "data": {"sessionId": "g-none"}}))],
         lambda r: r == [dispatcher.DEFER] * 5),
        ("goal: a session under the prompt rule with a goal pinned but no board row is denied work",
         lambda: invoke_goal_gate.evaluate("Read", unregistered_goal, "g-unregistered", state, tool_input={},
                                           prompt_rule_at=prompt_rule_at),
         lambda r: r[0] == dispatcher.DENY and board_url in r[1] and "g-unregistered" in r[1]),
        ("goal must-block: a session whose row was written only in a typed turn is denied work until it has consent",
         lambda: invoke_goal_gate.evaluate("Read", unconsented_goal, "g-unconsented", state, tool_input={},
                                           prompt_rule_at=prompt_rule_at),
         lambda r: r[0] == dispatcher.DENY and "allow this session's artifact writes" in r[1] and board_url in r[1]),
        ("goal control: that session may still write its own row, the write that asks for consent",
         lambda: invoke_goal_gate.evaluate("ArtifactData", unconsented_goal, "g-unconsented", state,
                                           tool_input={"action": "update", "url": board_url, "collection": "sessions",
                                                       "doc_id": "g-unconsented", "if_version": 1,
                                                       "data": {"lastSeen": "2026-10-26T00:00:00Z"}},
                                           prompt_rule_at=prompt_rule_at)[0],
         lambda r: r == dispatcher.DEFER),
        ("goal control: the same session with its row written after Stop hook feedback runs its tools",
         lambda: invoke_goal_gate.evaluate("Read", registered_goal, "g-registered", state, tool_input={},
                                           prompt_rule_at=prompt_rule_at)[0],
         lambda r: r == dispatcher.DEFER),
        ("goal: a session whose row was written in a slash-command turn (/goal) has consent and runs its tools",
         lambda: invoke_goal_gate.evaluate("Read", slash_goal, "g-slash", state, tool_input={},
                                           prompt_rule_at=prompt_rule_at)[0],
         lambda r: r == dispatcher.DEFER),
        ("goal must-block: the consent reason in a typed turn says to end the turn, not to write again",
         lambda: invoke_goal_gate.evaluate("Read", unconsented_goal, "g-unconsented", state, tool_input={},
                                           prompt_rule_at=prompt_rule_at)[1],
         lambda r: "end the turn with a NEEDED:" in r and "This turn is one:" not in r),
        ("goal must-block: the consent reason in a Stop-feedback turn says to update the row now",
         lambda: invoke_goal_gate.evaluate("Read", feedback_unconsented, "g-feedback", state, tool_input={},
                                           prompt_rule_at=prompt_rule_at),
         lambda r: r[0] == dispatcher.DENY and "This turn is one:" in r[1] and "end the turn" not in r[1]),
        ("goal control: a session whose operator pinned a goal runs its tools", lambda: judge("Read", pinned, "g-pinned"),
         lambda r: r == dispatcher.DEFER),
        ("goal: a goal retired with GOAL: none is no goal", lambda: judge("Read", retired, "g-retired"),
         lambda r: r == dispatcher.DENY),
        ("goal: a peer message never pins a goal", lambda: judge("Read", quoted, "g-peer"),
         lambda r: r == dispatcher.DENY),
        ("goal: the pin is latched, so a later read with no transcript still sees it",
         lambda: invoke_goal_gate.goal_in_force(None, "g-pinned", state), lambda r: r == "exercise the real gates"),
        ("goal: a call with no transcript cannot show an opening goal, so it is denied",
         lambda: judge("Read", None, "g-pinned"), lambda r: r == dispatcher.DENY),
        ("goal: a GOAL: in the message right after the original prompt opens the session",
         lambda: judge("Read", second, "g-second"), lambda r: r == dispatcher.DEFER),
        ("goal: a GOAL: first written in the third operator message does not open the session",
         lambda: judge("Read", third, "g-third"), lambda r: r == dispatcher.DENY),
        ("goal: a later GOAL: re-pins the goal",
         lambda: invoke_goal_gate.goal_in_force(repinned, "g-repinned", state), lambda r: r == "ship the dispatcher"),
        ("goal: a lost latch never locks out a goal pinned turns ago",
         lambda: (os.path.exists(os.path.join(state, "g-repinned.goal.json"))
                  and os.remove(os.path.join(state, "g-repinned.goal.json")),
                  judge("Read", repinned, "g-repinned"))[1], lambda r: r == dispatcher.DEFER),
        ("goal: a skill injection carrying GOAL: pins nothing",
         lambda: judge("Read", injected, "g-injected"), lambda r: r == dispatcher.DENY),
        ("goal: a GOAL: the operator sent mid-turn (queued) opens the session",
         lambda: judge("Read", queued, "g-queued"), lambda r: r == dispatcher.DEFER),
        ("goal: a session that opens with /goal runs its tools",
         lambda: judge("Read", command, "g-command"), lambda r: r == dispatcher.DEFER),
        ("goal: a /goal is the goal in force", lambda: invoke_goal_gate.goal_in_force(command, "g-command", state),
         lambda r: r == "audit the hooks"),
        ("goal: a /goal that is met, with no GOAL: line, leaves no goal",
         lambda: judge("Read", met, "g-met"), lambda r: r == dispatcher.DENY),
        ("goal: a /goal set after the second operator message does not open the session",
         lambda: judge("Read", late_command, "g-late-command"), lambda r: r == dispatcher.DENY),
        ("goal: the opening reason tells a session past its second message that only a new session opens it",
         lambda: invoke_goal_gate.evaluate("Read", late_command, "g-late-command", state)[1],
         lambda r: "no later /goal opens this session" in r and "new session" in r),
        ("goal: a GOAL: mid-sentence opens nothing", lambda: judge("Read", mid_sentence, "g-mid-sentence"),
         lambda r: r == dispatcher.DENY),
        ("goal: a session older than the rule's arrival is not held to an opening goal",
         lambda: invoke_goal_gate.opened_under_the_rule(first_record_at("2026-10-07T09:00:00Z"), effective),
         lambda r: r is False),
        ("goal control: a session newer than the rule's arrival is held to it",
         lambda: invoke_goal_gate.opened_under_the_rule(first_record_at("2026-10-07T13:00:00Z"), effective),
         lambda r: r is True),
        ("goal: the rule takes effect when the checkout received the gate file (its modification time)",
         lambda: invoke_goal_gate.rule_effective_at(landed), lambda r: r == effective),
        ("goal must-block: an unreadable gate file leaves no effective time, so the rule applies",
         lambda: invoke_goal_gate.rule_effective_at(os.path.join(directory, "absent.py")), lambda r: r is None),
        ("start must-block: an Edit before the saved SessionStart output is read is denied",
         lambda: start_judge("Edit", unread, "s-paged"), lambda r: r == dispatcher.DENY),
        ("start must-block: a PowerShell call before it is read is denied, even with a page",
         lambda: start_judge("PowerShell", unread, "s-paged", {"command": "git status"}),
         lambda r: r == dispatcher.DENY),
        ("start must-block: a Read of another file before it is read is denied",
         lambda: start_judge("Read", unread, "s-paged", {"file_path": os.path.join(directory, "other.txt")}),
         lambda r: r == dispatcher.DENY),
        ("start: the deny names the saved file to read",
         lambda: invoke_goal_gate.evaluate("Edit", unread, "s-paged", state, tool_input={}, workspace=pages,
                                           effective_at=effective)[1], lambda r: saved in r),
        ("start must-allow: the Read of the saved file itself runs",
         lambda: start_judge("Read", unread, "s-paged", {"file_path": saved}), lambda r: r == dispatcher.DEFER),
        ("start must-allow: the reply tools still run before it is read",
         lambda: start_judge("SendMessage", unread, "s-paged"), lambda r: r == dispatcher.DEFER),
        ("start control: once a Read of it came back clean, every tool runs",
         lambda: start_judge("Edit", read, "s-paged"), lambda r: r == dispatcher.DEFER),
        ("start control: the Read matches the path without regard to case",
         lambda: start_judge("Edit", read_upper, "s-paged"), lambda r: r == dispatcher.DEFER),
        ("start must-block: a Read that came back as an error reads nothing",
         lambda: start_judge("Edit", failed_read, "s-paged"), lambda r: r == dispatcher.DENY),
        ("start must-block: a Read made before the SessionStart record does not count",
         lambda: start_judge("Edit", read_before, "s-paged"), lambda r: r == dispatcher.DENY),
        ("start must-block: a compaction's new SessionStart output is read again",
         lambda: start_judge("Edit", reread, "s-paged"), lambda r: r == dispatcher.DENY),
        ("start control: a SessionStart context shown inline has no file to read",
         lambda: start_judge("Edit", inline, "s-paged"), lambda r: r == dispatcher.DEFER),
        ("start control: a transcript with no SessionStart context is held to neither step",
         lambda: start_judge("Edit", pinned, "s-unpaged"), lambda r: r == dispatcher.DEFER),
        ("start control: a SessionStart older than the rule's arrival is held to neither step",
         lambda: start_judge("Edit", legacy_start, "s-unpaged"), lambda r: r == dispatcher.DEFER),
        ("page must-block: with the start read but no page recorded, an Edit is denied",
         lambda: start_judge("Edit", read, "s-unpaged"), lambda r: r == dispatcher.DENY),
        ("page must-block: a work command is denied before the page exists",
         lambda: start_judge("PowerShell", read, "s-unpaged", {"command": "git status"}),
         lambda r: r == dispatcher.DENY),
        ("page must-block: a command that only names parking_lot.py later is not setup",
         lambda: start_judge("PowerShell", read, "s-unpaged", {"command": "git log parking_lot.py render"}),
         lambda r: r == dispatcher.DENY),
        ("page: the deny says how to set the page up",
         lambda: invoke_goal_gate.evaluate("Edit", read, "s-unpaged", state, tool_input={}, workspace=pages,
                                           effective_at=effective)[1], lambda r: "record-url" in r),
        ("page must-allow: parking_lot.py render runs before the page exists",
         lambda: start_judge("PowerShell", read, "s-unpaged", {"command": render}), lambda r: r == dispatcher.DEFER),
        ("page must-allow: parking_lot.py record-url runs before the page exists",
         lambda: start_judge("PowerShell", read, "s-unpaged",
                             {"command": render.replace("render crgolden-99", "record-url https://x")}),
         lambda r: r == dispatcher.DEFER),
        ("page must-allow: Get-Date runs before the page exists",
         lambda: start_judge("PowerShell", read, "s-unpaged", {"command": "Get-Date -AsUTC -Format o"}),
         lambda r: r == dispatcher.DEFER),
        ("page must-allow: the Artifact publish, ArtifactData, Skill and Read run before the page exists",
         lambda: [start_judge(tool, read, "s-unpaged") for tool in ("Artifact", "ArtifactData", "Skill", "Read")],
         lambda r: r == [dispatcher.DEFER] * 4),
        ("page control: once the page URL is recorded, every tool runs",
         lambda: start_judge("Edit", read, "s-paged"), lambda r: r == dispatcher.DEFER),
        ("start: a scan resumed from its cache sees a Read appended after it",
         resumed_scan, lambda r: r == (dispatcher.DENY, dispatcher.DEFER)),
        ("start must-block: a transcript rewritten shorter than the cached scan is scanned again",
         shrunk_scan, lambda r: r == (dispatcher.DEFER, dispatcher.DENY)),
        ("start control: the scan's cache sits in the Stop gate's write-protected state",
         lambda: invoke_goal_gate.start_cache_path(None, "s-x"),
         lambda r: os.path.dirname(r) == invoke_goal_gate.STATE_ROOT and r.endswith("s-x.start.json")),
        ("start control: the dispatcher passes tool_input, so the saved file's Read runs through it",
         lambda: dispatcher.goal_gate(types.SimpleNamespace(evaluate=lambda *a, **k: k.get("tool_input")),
                                      {"tool_name": "Read", "tool_input": {"file_path": saved}}),
         lambda r: r == {"file_path": saved}),
    ]


def eject_cases(directory):
    """The real write-protection and quarantine modules against fixture roots: only an attempt to change a gate
    that judges the session ejects it, and an ejected session is halted on every later call."""
    import invoke_main_checkout_gate as guard
    import session_quarantine
    root = os.path.join(directory, "quarantine")
    settings = os.path.join(directory, "settings.json")
    with open(settings, "w", encoding="utf-8") as handle:
        json.dump({"env": {"A": "1"}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python"}]}]}},
                  handle, indent=2)
    with open(settings, "r", encoding="utf-8") as handle:
        settings_text = handle.read()
    quarantine = types.SimpleNamespace(
        halt_envelope=lambda session_id: session_quarantine.halt_envelope(session_id, root),
        quarantine=lambda session_id, why, evidence: session_quarantine.quarantine(session_id, why, evidence, root))
    checkout = os.path.join(directory, "Tools")
    protection = types.SimpleNamespace(
        gate_change=lambda tool, tool_input: guard.gate_change(tool, tool_input, settings),
        evaluate=lambda tool, tool_input: guard.evaluate(tool, tool_input, checkout=checkout, settings=settings))
    modules = every_gate(session_quarantine=quarantine, invoke_main_checkout_gate=protection)

    def call(session_id, tool, tool_input):
        return dispatcher.respond({"hook_event_name": "PreToolUse", "session_id": session_id, "tool_name": tool,
                                   "tool_input": tool_input}, importer=importer_for(modules))

    def recorded(session_id):
        return os.path.exists(session_quarantine.record_path(session_id, root))

    record_file = os.path.join(guard.QUARANTINE_ROOT, "another-session.json")
    hook_file = os.path.join(checkout, "Hooks", "invoke_stop_gate.py")
    hooks_edit = {"file_path": settings, "old_string": '"command": "python"', "new_string": '"command": "exit 0"'}
    env_edit = {"file_path": settings, "old_string": '"A": "1"', "new_string": '"A": "2"'}

    def ejected(session_id, tool, tool_input):
        first = call(session_id, tool, tool_input)
        later = call(session_id, "Read", {"file_path": "x"})
        return first, later, recorded(session_id)

    def halted(result):
        first, later, was_recorded = result
        return (first["continue"] is False and first["hookSpecificOutput"]["permissionDecision"] == dispatcher.DENY
                and "EJECTED" in first["stopReason"] and later["continue"] is False and was_recorded)

    return [
        ("eject: a Write to a quarantine record quarantines the session and halts every later call",
         lambda: ejected("eject-record", "Write", {"file_path": record_file, "content": "{}"}), halted),
        ("eject: an Edit that changes settings.json's hooks block quarantines the session",
         lambda: ejected("eject-hooks", "Edit", hooks_edit), halted),
        ("eject: a Write that leaves settings.json unparsable quarantines the session",
         lambda: ejected("eject-unparsable", "Write", {"file_path": settings, "content": settings_text[:-3]}), halted),
        ("eject control: an Edit of settings.json that leaves the hooks block alone is not a gate change",
         lambda: (call("eject-env", "Edit", env_edit), recorded("eject-env")),
         lambda r: r[0] is None and r[1] is False),
        ("eject control: an Edit of a main-checkout hook is denied by the gate but never ejects",
         lambda: (call("eject-hook", "Edit", {"file_path": hook_file, "old_string": "a", "new_string": "b"}),
                  recorded("eject-hook")),
         lambda r: r[1] is False and r[0]["hookSpecificOutput"]["permissionDecision"] == dispatcher.DENY
         and r[0].get("continue") is not False),
        ("quarantine: an unreadable quarantine module fails closed",
         lambda: dispatcher.respond({"hook_event_name": "PreToolUse", "session_id": "any", "tool_name": "Read",
                                     "tool_input": {}}, importer=importer_for(
             {k: v for k, v in every_gate().items() if k != "session_quarantine"})),
         lambda r: r["continue"] is False and r["hookSpecificOutput"]["permissionDecision"] == dispatcher.DENY),
        ("share: must-block: an enabled machine whose state share is unreachable denies every call, naming the fix",
         lambda: dispatcher.respond({"hook_event_name": "PreToolUse", "session_id": "any", "tool_name": "Read",
                                     "tool_input": {"file_path": "x"}}, importer=importer_for(every_gate(
             claude_temp=types.SimpleNamespace(share_problem=lambda: "the state share at Z:\\ does not exist: map Z:")))),
         lambda r: r["hookSpecificOutput"]["permissionDecision"] == dispatcher.DENY
         and "map Z:" in r["hookSpecificOutput"]["permissionDecisionReason"]),
        ("share: control: a reachable or disabled share leaves the call to the gates",
         lambda: dispatcher.respond({"hook_event_name": "PreToolUse", "session_id": "any", "tool_name": "Read",
                                     "tool_input": {"file_path": "x"}}, importer=importer_for(every_gate())),
         lambda r: r is None),
        ("share: control: a PostToolUse call is never denied for the share",
         lambda: dispatcher.respond({"hook_event_name": "PostToolUse", "session_id": "any", "tool_name": "Read",
                                     "tool_input": {"file_path": "x"}}, importer=importer_for(every_gate(
             claude_temp=types.SimpleNamespace(share_problem=lambda: "unreachable")))),
         lambda r: r is None or "permissionDecision" not in r.get("hookSpecificOutput", {})),
        ("share: a share check that cannot run fails closed",
         lambda: dispatcher.respond({"hook_event_name": "PreToolUse", "session_id": "any", "tool_name": "Read",
                                     "tool_input": {}}, importer=importer_for(
             {k: v for k, v in every_gate().items() if k != "claude_temp"})),
         lambda r: r["hookSpecificOutput"]["permissionDecision"] == dispatcher.DENY),
    ]


def pre(tool, modules, deadline=None, tool_input=None):
    full_input = {"command": "stub command", "file_path": "stub.md", "new_string": "stub text"}
    payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input or full_input}
    return dispatcher.pre_tool_use(payload, deadline, importer_for(modules))


def stub_cases():
    hang = threading.Event()
    cases = [
        ("merge: deny beats allow", lambda: pre("PowerShell", every_gate(
            invoke_pretooluse_gate=shell_stub(dispatcher.ALLOW, "allowed"),
            invoke_gate_cadence_gate=stub(dispatcher.DENY, "cadence says no"))),
         lambda r: r == (dispatcher.DENY, "cadence says no")),
        ("merge: ask beats allow", lambda: pre("PowerShell", every_gate(
            invoke_pretooluse_gate=shell_stub(dispatcher.ALLOW, "allowed"),
            invoke_gate_cadence_gate=stub(dispatcher.ASK, "confirm"))),
         lambda r: r == (dispatcher.ASK, "confirm")),
        ("merge: allow alone is allow", lambda: pre("PowerShell", every_gate(
            invoke_pretooluse_gate=shell_stub(dispatcher.ALLOW, "allowed"))),
         lambda r: r == (dispatcher.ALLOW, "allowed")),
        ("merge: every reason of the winning decision", lambda: pre("Edit", every_gate(
            invoke_agents_doc_gate=stub(dispatcher.DENY, "read the guide"),
            invoke_doc_state_gate=stub(dispatcher.DENY, "no status"))),
         lambda r: r[0] == dispatcher.DENY and "read the guide" in r[1] and "no status" in r[1]),
        ("merge: all defer prints nothing", lambda: pre("PowerShell", every_gate()),
         lambda r: r == (dispatcher.DEFER, "")),
        ("routing: a shell gate never judges an Edit", lambda: pre("Edit", every_gate(
            invoke_pretooluse_gate=shell_stub(dispatcher.DENY, "shell"))),
         lambda r: r == (dispatcher.DEFER, "")),
        ("routing: the read-scope gate judges a Read", lambda: pre("Grep", every_gate(
            invoke_read_scope_gate=stub(dispatcher.DENY, "temp folder"))),
         lambda r: r == (dispatcher.DENY, "temp folder")),
        ("routing control: the read-scope gate never judges a shell command", lambda: pre("PowerShell", every_gate(
            invoke_read_scope_gate=stub(dispatcher.DENY, "temp folder"))),
         lambda r: r == (dispatcher.DEFER, "")),
        # The Monitor tool runs a shell command too; a sleeping watcher it ran reached the operator as a prompt.
        ("routing: the cadence gate judges a Monitor command", lambda: pre("Monitor", every_gate(
            invoke_gate_cadence_gate=stub(dispatcher.DENY, "cadence says no")),
            tool_input={"command": "python watch.py", "description": "matrix verdict", "timeout_ms": 1000}),
         lambda r: r == (dispatcher.DENY, "cadence says no")),
        ("routing: the shell gate judges a Monitor command", lambda: pre("Monitor", every_gate(
            invoke_pretooluse_gate=shell_stub(dispatcher.DENY, "shell")),
            tool_input={"command": "python watch.py", "description": "matrix verdict", "timeout_ms": 1000}),
         lambda r: r == (dispatcher.DENY, "shell")),
        ("routing control: the read-scope gate never judges a Monitor command", lambda: pre("Monitor", every_gate(
            invoke_read_scope_gate=stub(dispatcher.DENY, "temp folder")),
            tool_input={"command": "python watch.py", "description": "matrix verdict", "timeout_ms": 1000}),
         lambda r: r == (dispatcher.DEFER, "")),
        ("routing: the real read-scope gate denies a Read of the temp folder through the dispatcher",
         lambda: pre("Read", every_gate(invoke_read_scope_gate=invoke_read_scope_gate),
                     tool_input={"file_path": os.path.join(tempfile.gettempdir(), "some-other-program", "task.output")}),
         lambda r: r[0] == dispatcher.DENY and "where no allow rule reaches" in r[1]),
        ("routing control: the real read-scope gate lets a Read of a workspace repo through",
         lambda: pre("Read", every_gate(invoke_read_scope_gate=invoke_read_scope_gate),
                     tool_input={"file_path": os.path.join(session_home.workspace_root(), "Identity", "README.md")}),
         lambda r: r == (dispatcher.DEFER, "")),
        ("routing: the real read-scope gate denies a Read of a Harness hook through the dispatcher",
         lambda: pre("Read", every_gate(invoke_read_scope_gate=invoke_read_scope_gate),
                     tool_input={"file_path": os.path.join(harness_scope.main_checkout(), "Hooks", "invoke_read_scope_gate.py")}),
         lambda r: r[0] == dispatcher.DENY and "Harness repository" in r[1]),
        ("routing: the artifact gate judges an ArtifactData call", lambda: pre("ArtifactData", every_gate(
            invoke_artifact_gate=stub(dispatcher.DENY, "only the operator answers"))),
         lambda r: r == (dispatcher.DENY, "only the operator answers")),
        ("routing control: the artifact gate never judges a Read", lambda: pre("Read", every_gate(
            invoke_artifact_gate=stub(dispatcher.DENY, "only the operator answers"))),
         lambda r: r == (dispatcher.DEFER, "")),
        ("routing: the peer gate judges every tool", lambda: pre("Read", every_gate(
            invoke_peer_message_gate=stub(dispatcher.DENY, "answer the peer"))),
         lambda r: r == (dispatcher.DENY, "answer the peer")),
        ("fail closed: a gate that cannot be imported", lambda: pre("PowerShell", {
            name: stub(dispatcher.DEFER) for name in ("invoke_peer_message_gate", "invoke_pretooluse_gate")}),
         lambda r: r[0] == dispatcher.DENY and "invoke_gate_cadence_gate" in r[1] and "ImportError" in r[1]),
        ("fail closed: a gate that raises", lambda: pre("Edit", every_gate(
            invoke_doc_state_gate=stub(error=ValueError("boom")))),
         lambda r: r[0] == dispatcher.DENY and "invoke_doc_state_gate" in r[1] and "ValueError: boom" in r[1]),
        ("fail closed: a gate that returns no verdict", lambda: pre("PowerShell", every_gate(
            invoke_pretooluse_gate=shell_stub("allowed", "typo"))),
         lambda r: r[0] == dispatcher.DENY and "not a verdict" in r[1]),
        ("fail closed: a gate whose return has the wrong shape", lambda: pre("Edit", every_gate(
            invoke_doc_state_gate=shell_stub(dispatcher.DEFER))),
         lambda r: r[0] == dispatcher.DENY and "invoke_doc_state_gate" in r[1] and "not a verdict" in r[1]),
        ("fail closed: a gate still running at the deadline", lambda: pre("PowerShell", every_gate(
            invoke_gate_cadence_gate=stub(dispatcher.DEFER, block=hang)), SHORT_DEADLINE),
         lambda r: r[0] == dispatcher.DENY and "did not finish within" in r[1]),
        ("fail closed: a timeout's reason allows one identical retry and keeps the stop gate's NEEDED: phrase",
         lambda: pre("PowerShell", every_gate(
             invoke_gate_cadence_gate=stub(dispatcher.DEFER, block=hang)), SHORT_DEADLINE),
         lambda r: r[0] == dispatcher.DENY and "re-issue the identical call once" in r[1]
         and "end the turn with a NEEDED:" in r[1]),
        ("concurrency: a gate that never finishes does not keep the next gate from being judged",
         lambda: pre("PowerShell", every_gate(
             invoke_peer_message_gate=stub(dispatcher.DEFER, block=hang),
             invoke_pretooluse_gate=shell_stub(dispatcher.DENY, "no chaining")), SHORT_DEADLINE),
         lambda r: r[0] == dispatcher.DENY and "no chaining" in r[1]
         and "invoke_peer_message_gate: did not finish within" in r[1]),
        ("concurrency: an import that never finishes denies, named as not imported",
         lambda: dispatcher.pre_tool_use({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {}},
                                         SHORT_DEADLINE, lambda name: hang.wait() or None),
         lambda r: r[0] == dispatcher.DENY and "invoke_peer_message_gate: was not imported within" in r[1]),
        ("fail closed: an allow elsewhere cannot rescue a failure", lambda: pre("PowerShell", every_gate(
            invoke_pretooluse_gate=shell_stub(dispatcher.ALLOW, "allowed"),
            invoke_gate_cadence_gate=stub(error=RuntimeError("x")))),
         lambda r: r[0] == dispatcher.DENY),
        ("fail closed: the failure keeps the other gates' deny reasons", lambda: pre("PowerShell", every_gate(
            invoke_pretooluse_gate=shell_stub(dispatcher.DENY, "no chaining"),
            invoke_gate_cadence_gate=stub(error=RuntimeError("x")))),
         lambda r: r[0] == dispatcher.DENY and "no chaining" in r[1] and "RuntimeError" in r[1]),
        ("carve-out: a reply tool still runs when its gate fails", lambda: pre("SendMessage", every_gate(
            invoke_peer_message_gate=stub(error=RuntimeError("x")))),
         lambda r: r == (dispatcher.DEFER, "")),
        ("carve-out control: a non-reply tool with the same failure denies", lambda: pre("Read", every_gate(
            invoke_peer_message_gate=stub(error=RuntimeError("x")))),
         lambda r: r[0] == dispatcher.DENY),
        ("post: reminders are joined into one context", lambda: dispatcher.post_tool_use(
            {"tool_name": "Edit"}, importer=importer_for(every_gate(
                invoke_comment_reminder=stub(reminder="comment note"),
                invoke_doc_prose_reminder=stub(reminder="prose note")))),
         lambda r: r["hookSpecificOutput"]["additionalContext"] == "comment note\n\nprose note"
         and "systemMessage" not in r),
        ("post: a failing reminder reaches the operator", lambda: dispatcher.post_tool_use(
            {"tool_name": "Write"}, importer=importer_for(every_gate(
                invoke_doc_prose_reminder=stub(error=OSError("gone"))))),
         lambda r: "invoke_doc_prose_reminder" in r["systemMessage"]
         and "OSError" in r["hookSpecificOutput"]["additionalContext"]),
        ("post: nothing to say prints nothing", lambda: dispatcher.respond(
            {"hook_event_name": "PostToolUse", "tool_name": "Edit"}, importer=importer_for(every_gate())),
         lambda r: r is None),
        ("event: an unhandled event is reported, never silently passed", lambda: dispatcher.respond(
            {"hook_event_name": "Stop"}, importer=importer_for(every_gate())),
         lambda r: "Stop" in r["systemMessage"]),
    ]
    return cases, hang


def operator_message(text):
    return {"type": "user", "origin": {"kind": "human"}, "promptSource": "typed", "message": {"content": text}}


GOAL_RECORD = operator_message("GOAL: exercise the real gates")


def write_transcript(directory, records, goal=True):
    """A fixture transcript, opening with the operator's GOAL: pin unless goal is False, so the goal gate judges
    every real call the way it judges a session that has one."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "fixture-session.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for record in ([GOAL_RECORD] if goal else []) + list(records):
            handle.write(json.dumps(record) + "\n")
    return path


def full_read(file_path):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_read", "name": "Read", "input": {"file_path": file_path}}]}}


def peer_question():
    body = '<cross-session-message from="{0}" from-name="fixture-peer">is the machine free?</cross-session-message>'
    return {"type": "user", "origin": {"kind": "peer"}, "message": {"content": body.format(PEER)}}


def respond_as_session(tool, tool_input, transcript, cwd, event="PreToolUse"):
    payload = {"hook_event_name": event, "tool_name": tool, "tool_input": tool_input, "cwd": cwd,
               "transcript_path": transcript, "tool_use_id": "toolu_fixture",
               "session_id": "fixture-" + os.path.basename(os.path.dirname(transcript))}
    return dispatcher.respond(payload)


def decision_of(output):
    return dispatcher.DEFER if output is None else output["hookSpecificOutput"]["permissionDecision"]


def real_cases(directory, paths):
    workspace, status_doc, repo_file, repo_guide = paths["workspace"], paths["status"], paths["repo_file"], paths["guide"]

    def real(tool, tool_input, transcript, event="PreToolUse"):
        return respond_as_session(tool, tool_input, transcript, workspace, event)

    quiet = write_transcript(os.path.join(directory, "quiet"), [])
    read = write_transcript(os.path.join(directory, "read"), [full_read(repo_guide)])
    owed = write_transcript(os.path.join(directory, "owed"), [peer_question()])
    status_edit = {"file_path": status_doc, "old_string": "zz-no-anchor",
                   "new_string": "- **DONE, live 2026-09-10.** Shipped."}
    return [
        ("real shell gate: git commit denies", lambda: real("PowerShell", {"command": "git commit -m x"}, quiet),
         lambda r: decision_of(r) == dispatcher.DENY),
        ("real shell gate: a chained command denies",
         lambda: real("PowerShell", {"command": "dotnet --version; Write-Output x"}, quiet),
         lambda r: decision_of(r) == dispatcher.DENY),
        ("real shell gate: an allow-listed bare command allows",
         lambda: real("PowerShell", {"command": "dotnet --version"}, quiet),
         lambda r: decision_of(r) == dispatcher.ALLOW),
        ("real shell gate: an unlisted command defers",
         lambda: real("PowerShell", {"command": "curl https://example.com"}, quiet),
         lambda r: decision_of(r) == dispatcher.DEFER),
        ("real cadence gate: a gate step issued on its own denies",
         lambda: real("PowerShell", {"command": "dotnet test --project C:\\x\\x.csproj"}, quiet),
         lambda r: decision_of(r) == dispatcher.DENY and "gate" in r["hookSpecificOutput"]["permissionDecisionReason"].lower()),
        ("real cadence gate: the same gate step through the Monitor tool denies",
         lambda: real("Monitor", {"command": "dotnet test --project C:\\x\\x.csproj", "description": "tests", "timeout_ms": 1000}, quiet),
         lambda r: decision_of(r) == dispatcher.DENY and "gate" in r["hookSpecificOutput"]["permissionDecisionReason"].lower()),
        ("real shell gate: a chained command through the Monitor tool denies",
         lambda: real("Monitor", {"command": "dotnet --version; Write-Output x", "description": "x", "timeout_ms": 1000}, quiet),
         lambda r: decision_of(r) == dispatcher.DENY),
        ("real shell gate control: a Monitor with a socket source and no command defers",
         lambda: real("Monitor", {"ws": {"url": "wss://example.test/stream"}, "description": "x", "timeout_ms": 1000}, quiet),
         lambda r: decision_of(r) == dispatcher.DEFER),
        ("real agents-doc gate: an edit before the guide is read denies",
         lambda: real("Edit", {"file_path": repo_file, "old_string": "a", "new_string": "b"}, quiet),
         lambda r: decision_of(r) == dispatcher.DENY and "Identity.md" in r["hookSpecificOutput"]["permissionDecisionReason"]),
        ("real agents-doc gate: the same edit after a full read defers",
         lambda: real("Edit", {"file_path": repo_file, "old_string": "a", "new_string": "b"}, read),
         lambda r: decision_of(r) == dispatcher.DEFER),
        ("real doc-state gate: status beside a date denies",
         lambda: real("Edit", status_edit, quiet),
         lambda r: decision_of(r) == dispatcher.DENY),
        ("real doc-state gate: durable prose defers",
         lambda: real("Edit", dict(status_edit, new_string="Run the gate from the repo root."), quiet),
         lambda r: decision_of(r) == dispatcher.DEFER),
        ("real peer gate: an unanswered peer question denies a Read",
         lambda: real("Read", {"file_path": status_doc}, owed),
         lambda r: decision_of(r) == dispatcher.DENY),
        ("real peer gate: the reply itself is never denied",
         lambda: real("SendMessage", {"to": PEER, "message": "yes"}, owed),
         lambda r: decision_of(r) == dispatcher.DEFER),
        ("real comment reminder: an added comment line reminds",
         lambda: real("Edit", {"file_path": os.path.join(workspace, "Identity", "src", "Program.cs"),
                               "old_string": "", "new_string": "// explains the loop"}, quiet, "PostToolUse"),
         lambda r: r is not None and "COMMENTS.md" in r["hookSpecificOutput"]["additionalContext"]),
        ("real comment reminder: code only says nothing",
         lambda: real("Edit", {"file_path": os.path.join(workspace, "Identity", "src", "Program.cs"),
                               "old_string": "var a = 1;", "new_string": "var a = 2;"}, quiet, "PostToolUse"),
         lambda r: r is None),
    ]


CONCURRENT_RUNS = 25


def concurrency_cases(directory, paths):
    """Every real gate, started at once on its own thread, must reach the verdicts a strictly sequential call of the
    same gates reaches, on every one of CONCURRENT_RUNS runs."""
    status_doc, repo_file = paths["status"], paths["repo_file"]
    quiet = write_transcript(os.path.join(directory, "concurrent-quiet"), [])
    owed = write_transcript(os.path.join(directory, "concurrent-owed"), [peer_question()])
    payloads = [
        {"tool_name": "PowerShell", "tool_input": {"command": "git commit -m x"}, "transcript_path": quiet},
        {"tool_name": "PowerShell", "tool_input": {"command": "dotnet --version"}, "transcript_path": quiet},
        {"tool_name": "PowerShell", "tool_input": {"command": "dotnet test --project C:\\x\\x.csproj"}, "transcript_path": quiet},
        {"tool_name": "Edit", "tool_input": {"file_path": repo_file, "old_string": "a", "new_string": "b"},
         "transcript_path": quiet},
        {"tool_name": "Edit", "tool_input": {"file_path": status_doc, "old_string": "zz",
                                             "new_string": "- **DONE, live 2026-09-10.** Shipped."}, "transcript_path": quiet},
        {"tool_name": "Read", "tool_input": {"file_path": status_doc}, "transcript_path": owed},
    ]
    for payload in payloads:
        payload.update(hook_event_name="PreToolUse", cwd=paths["workspace"], tool_use_id="toolu_fixture",
                       session_id="fixture-" + os.path.basename(os.path.dirname(payload["transcript_path"])))

    def sequential(payload):
        verdicts = []
        for name, adapter in dispatcher.applicable(dispatcher.PRE_GATES, payload["tool_name"]):
            verdicts.append(adapter(importlib.import_module(name), payload))
        return dispatcher.merge(verdicts)[0]

    def all_runs_agree():
        expected = [sequential(payload) for payload in payloads]
        observed = [[dispatcher.pre_tool_use(payload)[0] for payload in payloads] for _ in range(CONCURRENT_RUNS)]
        return expected, [run for run in observed if run != expected]

    return [
        ("concurrency: {0} runs of every real gate at once agree with the sequential verdicts".format(CONCURRENT_RUNS),
         all_runs_agree, lambda r: r[1] == [] and dispatcher.DENY in r[0] and dispatcher.ALLOW in r[0]),
    ]


def child(args, payload, env_overrides=None, raw=None):
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTHONUTF8", "PYTHONIOENCODING", "CRGOLDEN_WORKSPACE", dispatcher.TOOLS_ROOT_VARIABLE)}
    env["PYTHONUTF8"] = "0"
    env.update(env_overrides or {})
    data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    result = subprocess.run([sys.executable] + args, input=data, capture_output=True, env=env)
    text = result.stdout.decode("utf-8", "replace").strip()
    return result.returncode, (json.loads(text) if text else None), result.stderr.decode("utf-8", "replace")


def fixture_machine(directory, name, dispatcher_source=None, launcher_file=None):
    """(environment, Tools checkout) for a machine whose Tools checkout's Hooks/ holds the given dispatcher source,
    or none at all, beside the real launch_dispatcher.py whenever a dispatcher is given (or launcher_file says so).
    The checkout lives under USERPROFILE, where LAUNCHER loads it from."""
    profile = os.path.join(directory, name, "profile")
    checkout = os.path.join(profile, "source", "repos", "crgolden", "Tools")
    os.makedirs(os.path.join(checkout, "Hooks"), exist_ok=True)
    if dispatcher_source is not None:
        with open(os.path.join(checkout, "Hooks", "invoke_hook_dispatcher.py"), "w", encoding="utf-8") as handle:
            handle.write(dispatcher_source)
    if launcher_file if launcher_file is not None else dispatcher_source is not None:
        shutil.copy2(os.path.join(HERE, "launch_dispatcher.py"), os.path.join(checkout, "Hooks", "launch_dispatcher.py"))
    return {"USERPROFILE": profile, dispatcher.TOOLS_ROOT_VARIABLE: os.path.relpath(checkout, profile)}, checkout


def checkout_machine(directory):
    """(environment, workspace) for a machine whose Tools checkout holds this branch's Hooks/ and Gates/, with a
    marker prose_lint in its Claude/skills, so a row proves the reminder loads the checkout's own lint."""
    profile = os.path.join(directory, "checkout", "profile")
    workspace = os.path.join(profile, "source", "repos", "crgolden")
    root = os.path.join(workspace, "Tools")
    os.makedirs(os.path.join(root, "Hooks"))
    for name in os.listdir(HERE):
        if name.endswith((".py", ".txt")):
            shutil.copy2(os.path.join(HERE, name), os.path.join(root, "Hooks", name))
    shutil.copytree(os.path.join(os.path.dirname(HERE), "Gates"), os.path.join(root, "Gates"))
    lint = os.path.join(root, "Claude", "skills", "docs-audit", "scripts")
    os.makedirs(lint)
    with open(os.path.join(lint, "prose_lint.py"), "w", encoding="utf-8") as handle:
        handle.write("def scan_line(line):\n    return [('checkout-copy', 'marker')]\n")
    os.makedirs(session_home.workspace_doc("AGENTS", workspace, must_exist=False))
    temp = os.path.join(directory, "checkout", "temp")
    os.makedirs(temp)
    return {"USERPROFILE": profile, dispatcher.TOOLS_ROOT_VARIABLE: os.path.relpath(root, profile), "TEMP": temp,
            "TMP": temp}, workspace


def launcher_cases(directory, paths):
    missing, missing_tools = fixture_machine(directory, "missing")
    launch_only, _ = fixture_machine(directory, "launch-only", launcher_file=True)
    broken, _ = fixture_machine(directory, "broken", "raise RuntimeError('half-written gate')\n")
    exits_one, _ = fixture_machine(directory, "exits-one", "import sys\nsys.exit(1)\n")
    exits_clean, _ = fixture_machine(directory, "exits-clean", "import sys\nsys.exit(0)\n")
    present, _ = fixture_machine(directory, "present",
                                 "import json,sys\npayload=json.loads(sys.stdin.read())\n"
                                 "sys.stdout.write(json.dumps({'echo':payload['tool_name']}))\n")
    deployed, fixture_workspace = checkout_machine(directory)
    unset = {key: value for key, value in present.items() if key != dispatcher.TOOLS_ROOT_VARIABLE}
    goal_transcript = write_transcript(os.path.join(directory, "launcher-goal"), [])
    launch = ["-c", dispatcher.LAUNCHER]

    def run(environment, tool, command=None, event="PreToolUse"):
        tool_input = {"command": command} if command is not None else {}
        return child(launch, {"hook_event_name": event, "tool_name": tool, "tool_input": tool_input,
                              "transcript_path": goal_transcript, "session_id": "launcher-fixture"}, environment)

    def denied(result):
        code, output, _ = result
        return code == 0 and output is not None and \
            output["hookSpecificOutput"]["permissionDecision"] == dispatcher.DENY and \
            "NEEDED:" in output["hookSpecificOutput"]["permissionDecisionReason"]

    repair = 'git -C "{0}" pull --ff-only'.format(missing_tools)
    return [
        ("launcher: a present dispatcher runs, reading stdin the launcher already consumed",
         lambda: run(present, "Read"), lambda r: r[0] == 0 and r[1] == {"echo": "Read"}),
        ("launcher: a missing launcher file denies, exit 0", lambda: run(missing, "Read"), denied),
        ("launcher: no CLAUDE_TOOLS_ROOT denies, even beside a present dispatcher", lambda: run(unset, "Read"), denied),
        ("launcher: a launcher file beside no dispatcher denies, exit 0", lambda: run(launch_only, "Read"), denied),
        ("launcher: a launcher file beside no dispatcher still lets a reply tool through",
         lambda: run(launch_only, "SendMessage"), lambda r: r[0] == 0 and r[1] is None),
        ("coupling: LAUNCHER stays a short bootstrap, since a long -c program is content-scanned on every start",
         lambda: len(dispatcher.LAUNCHER), lambda r: r < 800),
        ("launcher: a missing launcher file denies a call whose input merely names a reply tool",
         lambda: run(missing, "Grep", "SendMessage"), denied),
        ("launcher: a dispatcher that fails to load denies", lambda: run(broken, "Edit"), denied),
        ("launcher: a dispatcher that exits with code 1 denies, never a non-blocking error",
         lambda: run(exits_one, "Read"), denied),
        ("launcher control: a dispatcher that exits cleanly with no output denies nothing",
         lambda: run(exits_clean, "Read"), lambda r: r[0] == 0 and r[1] is None),
        ("launcher: even the repair pull denies; repair is the operator's",
         lambda: run(missing, "PowerShell", repair), denied),
        ("launcher: the repair pull with a second command denies",
         lambda: run(missing, "PowerShell", repair + "; Remove-Item x"), denied),
        ("launcher: a pull of a different repo denies",
         lambda: run(missing, "PowerShell", repair.replace("Tools", "Identity")), denied),
        ("launcher: a reply tool is let through", lambda: run(missing, "SendMessage"),
         lambda r: r[0] == 0 and r[1] is None),
        ("launcher: PostToolUse warns the operator", lambda: run(launch_only, "Edit", event="PostToolUse"),
         lambda r: r[0] == 0 and "systemMessage" in r[1]),
        ("launcher: a missing launcher file prints nothing for PostToolUse, whose tool PreToolUse already denied",
         lambda: run(missing, "Edit", event="PostToolUse"), lambda r: r[0] == 0 and r[1] is None),
        ("launcher: the checkout's real gates decide a real call",
         lambda: run(deployed, "PowerShell", "git commit -m x"),
         lambda r: r[0] == 0 and r[1]["hookSpecificOutput"]["permissionDecision"] == dispatcher.DENY
         and "git writes are the operator's" in r[1]["hookSpecificOutput"]["permissionDecisionReason"]),
        ("launcher: the checkout's prose reminder loads the checkout's own prose_lint",
         lambda: child(launch, {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_response": {},
                                "tool_input": {"file_path": session_home.workspace_doc("AGENTS/GATES.md", fixture_workspace, must_exist=False),
                                               "old_string": "", "new_string": "The gate used to pass."}}, deployed),
         lambda r: r[0] == 0 and "[checkout-copy] marker" in r[1]["hookSpecificOutput"]["additionalContext"]),
        ("launcher: the checkout's gates allow what the shell gate allows",
         lambda: run(deployed, "PowerShell", "dotnet --version"),
         lambda r: r[0] == 0 and r[1]["hookSpecificOutput"]["permissionDecision"] == dispatcher.ALLOW),
        ("process: UTF-8 stdin under the ANSI code page reaches the reminder intact",
         lambda: child([dispatcher.__file__], {
             "hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_response": {},
             "tool_input": {"file_path": os.path.join(paths["workspace"], "Identity", "src", "Program.cs"),
                            "old_string": "", "new_string": "// explains \u2014 the loop"}},
                       {"CRGOLDEN_WORKSPACE": paths["workspace"]}),
         lambda r: r[1] is not None and "\u2014" in r[1]["hookSpecificOutput"]["additionalContext"]),
        ("process: stdin that is not JSON denies", lambda: child([dispatcher.__file__], None, raw=b"not json"),
         lambda r: r[0] == 0 and r[1]["hookSpecificOutput"]["permissionDecision"] == dispatcher.DENY),
    ]


def session_start(environment):
    """The registered SessionStart command in a child, as settings.json runs it."""
    env = dict(os.environ)
    env.pop("CRGOLDEN_WORKSPACE", None)
    env.pop(dispatcher.TOOLS_ROOT_VARIABLE, None)
    env.update(environment)
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", dispatcher.SESSION_START_COMMAND],
                            input=json.dumps({"hook_event_name": "SessionStart", "source": "startup"}).encode("utf-8"),
                            capture_output=True, env=env)
    text = result.stdout.decode("utf-8", "replace").strip()
    try:
        return result.returncode, json.loads(text) if text else None
    except ValueError:
        return result.returncode, text


def session_start_cases(directory):
    def machine(name, script):
        profile = os.path.join(directory, "session-" + name, "profile")
        hooks = os.path.join(profile, "source", "repos", "crgolden", "Tools", "Hooks")
        os.makedirs(hooks)
        if script is not None:
            with open(os.path.join(hooks, "Invoke-SessionStart.ps1"), "w", encoding="utf-8") as handle:
                handle.write(script)
        return {"USERPROFILE": profile, dispatcher.TOOLS_ROOT_VARIABLE: os.path.relpath(os.path.dirname(hooks), profile)}

    present = machine("present", "@{ hookSpecificOutput = @{ hookEventName = 'SessionStart'; "
                                 "additionalContext = 'from the checkout' } } | ConvertTo-Json -Compress\n")
    unset = {key: value for key, value in present.items() if key != dispatcher.TOOLS_ROOT_VARIABLE}
    missing = machine("missing", None)
    broken = machine("broken", "throw 'half-written hook'\n")

    def names_a_real_card(context):
        named = re.search(r"Read Harness/Hooks/([\w.-]+) in full", context)
        return named is not None and os.path.isfile(os.path.join(HERE, named.group(1))) \
            and not named.group(1).lower().endswith(".md")

    def warned(result):
        code, output = result
        return code == 0 and isinstance(output, dict) and "systemMessage" in output \
            and names_a_real_card(output["hookSpecificOutput"]["additionalContext"])

    return [
        ("session start: the checkout's script output passes through",
         lambda: session_start(present),
         lambda r: r[0] == 0 and r[1]["hookSpecificOutput"]["additionalContext"] == "from the checkout"),
        ("session start: a missing script warns the operator and the agent", lambda: session_start(missing),
         warned),
        ("session start: no CLAUDE_TOOLS_ROOT warns, even beside a present script", lambda: session_start(unset),
         warned),
        ("session start: a script that throws warns the operator and the agent", lambda: session_start(broken),
         warned),
    ]


def coupling_cases():
    return [
        ("coupling: the reply tools match the peer gate's own list",
         lambda: tuple(invoke_peer_message_gate.REPLY_TOOLS), lambda r: r == dispatcher.REPLY_TOOLS),
        ("coupling: every registered gate module exists beside the dispatcher",
         lambda: [name for name, _, _ in dispatcher.PRE_GATES + dispatcher.POST_REMINDERS
                  if not os.path.isfile(os.path.join(HERE, name + ".py"))],
         lambda r: r == []),
        ("coupling: the dispatcher runs exactly the registered hooks and the two approved gates, nothing more",
         lambda: ([name for name, _, _ in dispatcher.PRE_GATES], [name for name, _, _ in dispatcher.POST_REMINDERS]),
         lambda r: r == (["invoke_peer_message_gate", "invoke_goal_gate", "invoke_main_checkout_gate",
                          "invoke_pretooluse_gate", "invoke_gate_cadence_gate", "invoke_agents_doc_gate",
                          "invoke_doc_state_gate", "invoke_read_scope_gate", "invoke_artifact_gate",
                          "invoke_short_path_gate", "invoke_overlap_gate", "invoke_protected_path_gate",
                          "invoke_push_gate", "invoke_session_name_gate"],
                         ["invoke_comment_reminder", "invoke_doc_prose_reminder"])),
        ("coupling: EnterWorktree reaches the read-scope gate beside the every-tool gates",
         lambda: [name for name, _ in dispatcher.applicable(dispatcher.PRE_GATES, "EnterWorktree")],
         lambda r: r == ["invoke_peer_message_gate", "invoke_goal_gate", "invoke_read_scope_gate"]),
        ("a live call keeps no clock: every entry point waits for its gates by default",
         lambda: [inspect.signature(entry).parameters["deadline"].default
                  for entry in (dispatcher.respond, dispatcher.pre_tool_use, dispatcher.post_tool_use)],
         lambda r: r == [None, None, None]),
    ]


def registration_cases(directory):
    """The registration check against fixture settings, so it is proven to fire before the real switch."""
    path_launcher = dispatcher.LAUNCHER.replace("os.environ.get('CLAUDE_TOOLS_ROOT') or '?'",
                                                "'source','repos','workspace','Tools'")

    def fixture(name, events, extra=None, launchers=None):
        def hook(event):
            launcher = (launchers or {}).get(event, dispatcher.LAUNCHER)
            entry = {"type": "command", "command": "python", "args": ["-c", launcher]}
            if event == dispatcher.PRE_TOOL_USE:
                entry["onFailure"] = "block"
            entry.update((extra or {}).get(event, {}))
            return entry

        path = os.path.join(directory, name + "-settings.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"hooks": {event: [{"matcher": "*", "hooks": [hook(event)]}] for event in events}}, handle)
        return path

    other = fixture("other-event", ("PreToolUse", "PostToolUse", "UserPromptSubmit"))
    open_on_failure = fixture("open-on-failure", ("PreToolUse", "PostToolUse"),
                              {dispatcher.PRE_TOOL_USE: {"onFailure": "continue"}})
    short_timeout = fixture("short-timeout", ("PreToolUse", "PostToolUse"), {"PostToolUse": {"timeout": 60}})
    previous = fixture("previous", ("PreToolUse", "PostToolUse"), launchers={"PreToolUse": path_launcher,
                                                                              "PostToolUse": path_launcher})
    half_switched = fixture("half-switched", ("PreToolUse", "PostToolUse"), launchers={"PostToolUse": path_launcher})
    return [
        ("registration: a launcher naming the Tools path is the previous one, SKIPPED until the switch",
         lambda: registration(previous), lambda r: r[0] == "SKIPPED" and dispatcher.TOOLS_ROOT_VARIABLE in r[1]),
        ("must-block registration: one event switched and one still naming the Tools path fails",
         lambda: registration(half_switched), lambda r: r[0] == "FAIL" and "PostToolUse args differ" in r[1]),
        ("registration: LAUNCHER registered for an event its load failure does not decide fails",
         lambda: registration(other), lambda r: r[0] == "FAIL" and "UserPromptSubmit registers LAUNCHER" in r[1]),
        ("must-block registration: a PreToolUse entry a failure or timeout would let through fails",
         lambda: registration(open_on_failure), lambda r: r[0] == "FAIL" and "onFailure" in r[1]),
        ("must-block registration: an entry lowering Claude Code's hook timeout fails",
         lambda: registration(short_timeout), lambda r: r[0] == "FAIL" and "timeout" in r[1]),
    ]


def registration(settings=SETTINGS):
    """('PASS'|'FAIL'|'SKIPPED', detail) for the hooks block in the real settings.json. Until the operator
    switches it, the dispatcher is not registered and this reports SKIPPED, never PASS."""
    try:
        with open(settings, encoding="utf-8") as handle:
            hooks = json.load(handle).get("hooks", {})
    except (OSError, ValueError) as error:
        return "FAIL", "settings.json unreadable: {0}".format(error)
    found = []
    for event in hooks:
        for entry in hooks.get(event, []):
            for hook in entry.get("hooks", []):
                if dispatcher.LAUNCHER.strip() in " ".join(hook.get("args") or []) or \
                        "invoke_hook_dispatcher" in json.dumps(hook) or "launch_dispatcher.py" in json.dumps(hook):
                    found.append((event, entry.get("matcher"), hook))
    if not found:
        return "SKIPPED", "settings.json does not register the dispatcher yet"
    reads_variable = "os.environ.get('{0}')".format(dispatcher.TOOLS_ROOT_VARIABLE)
    previous = [hook for _, _, hook in found if reads_variable not in " ".join(hook.get("args") or [])]
    if previous and len(previous) == len(found):
        return "SKIPPED", ("settings.json registers a previous LAUNCHER, which names the Tools checkout's path instead "
                           "of reading " + dispatcher.TOOLS_ROOT_VARIABLE + "; the operator switches it, with the "
                           "variable in the env block, only after every machine's Tools checkout holds this one")
    problems = []
    for event, matcher, hook in found:
        if event not in (dispatcher.PRE_TOOL_USE, "PostToolUse"):
            problems.append("{0} registers LAUNCHER, whose load failure decides only PreToolUse and PostToolUse, so "
                            "a broken deploy would pass that event unjudged".format(event))
        if hook.get("args") != ["-c", dispatcher.LAUNCHER]:
            problems.append("{0} args differ from LAUNCHER".format(event))
        if "timeout" in hook:
            problems.append("{0} sets timeout {1!r}, lowering Claude Code's own hook timeout".format(
                event, hook.get("timeout")))
        if event == dispatcher.PRE_TOOL_USE and hook.get("onFailure") != "block":
            problems.append("PreToolUse onFailure is {0!r}, so a gate that crashes or hangs lets the call through; "
                            "it must be \"block\"".format(hook.get("onFailure")))
    session = [hook for entry in hooks.get("SessionStart", []) for hook in entry.get("hooks", [])]
    if [hook.get("args") for hook in session] != [["-NoProfile", "-NonInteractive", "-Command",
                                                    dispatcher.SESSION_START_COMMAND]]:
        problems.append("SessionStart is not exactly one hook running SESSION_START_COMMAND from the Tools checkout")
    return ("FAIL", "; ".join(problems)) if problems else ("PASS", "registered as LAUNCHER and SESSION_START_COMMAND")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args()
    started = time.time()
    failures = 0
    ran = 0
    directory = tempfile.mkdtemp(prefix="hook-dispatcher-", dir=claude_temp.scratch_dir())
    os.environ["CLAUDE_CODE_TMPDIR"] = os.path.join(directory, "temp")
    import invoke_goal_gate
    invoke_goal_gate.STATE_ROOT = os.path.join(directory, "stopgate-state")
    stubbed, hang = stub_cases()
    paths = owned_workspace(directory)
    try:
        for name, act, check in (stubbed + real_cases(directory, paths) + concurrency_cases(directory, paths)
                                 + launcher_cases(directory, paths) + session_start_cases(directory) + coupling_cases()
                                 + registration_cases(directory) + eject_cases(directory) + goal_cases(directory)):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            try:
                result = act()
                ok = bool(check(result))
            except Exception as error:
                result, ok = "{0}: {1}".format(type(error).__name__, error), False
            print("{0}  {1}".format("PASS" if ok else "FAIL", name))
            if not ok:
                print("        got: {0!r}".format(result)[:600])
                failures += 1
    finally:
        hang.set()
        shutil.rmtree(directory, ignore_errors=False)
    if not args.only or any(o.lower() in "registration" for o in args.only):
        state, detail = registration()
        print("{0}  registration in settings.json: {1}".format(state, detail))
        failures += 1 if state == "FAIL" else 0
    print("")
    print("{0} case(s) in {1:.2f}s".format(ran, time.time() - started))
    if ran == 0:
        print("No cases ran; refusing to report success on an empty selection.")
        return 1
    if failures == 0:
        print("All hook-dispatcher cases passed.")
        return 0
    print("{0} case(s) failed.".format(failures))
    return 1


if __name__ == "__main__":
    sys.exit(main())
