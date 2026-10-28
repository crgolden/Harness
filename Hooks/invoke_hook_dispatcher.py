"""One process per hook call: every PreToolUse gate and every PostToolUse reminder, run in-process.

Wiring, measurements and the failure policy live in PRETOOLUSE-HOOK.md, "The dispatcher". In short:

  * One interpreter per tool call instead of one per gate. Three gate processes started together compete for
    the cores, and the gap is widest exactly when the machine is busy.
  * FAILS CLOSED. A gate that cannot be imported, raises or returns an unknown decision turns a PreToolUse call
    into a deny naming the gate and the failure. A gate's own "not in scope" verdict still defers: that is a
    verdict, not a failure. The reply tools are the one carve-out, so a broken gate cannot cut a session off from
    its peers. The dispatcher keeps no clock of its own: it waits for every gate, and a gate that hangs is
    bounded by Claude Code's hook timeout, which with the settings entry's onFailure "block" blocks the call
    instead of letting it through.
  * Precedence is the host's: deny over ask over allow, every reason of the winning decision kept.
  * PostToolUse runs after the edit is on disk, so a failing reminder cannot undo anything; it is surfaced to
    the operator as a systemMessage and to the agent as context instead of vanishing.
  * LAUNCHER is the exact program settings.json registers: a short bootstrap that runs Hooks/launch_dispatcher.py
    from the Tools checkout, which stays on main and changes only through a merged pull request (Tools/AGENTS.md).
    That file reads stdin first and runs this one, so a missing or broken dispatcher, or a missing launcher file,
    still denies every call but the reply tools. Repairing it is the operator's: no
    command a session could run is exempt, since none of them would pass through the gates that failed to load.

Each gate module keeps its own main() and harness; this file calls the same functions those main()s call.
Any change here must be verified with Harness/Hooks/test_hook_dispatcher.py.
"""

import importlib
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DENY = "deny"
ASK = "ask"
ALLOW = "allow"
DEFER = "defer"
PRECEDENCE = (DENY, ASK, ALLOW)
DECISIONS = PRECEDENCE + (DEFER,)

PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"

FAILURE_DETAIL_LIMIT = 1200

ALL_TOOLS = None
SHELL_TOOLS = ("PowerShell", "Monitor")
EDIT_TOOLS = ("Edit", "Write")
REPLY_TOOLS = ("SendMessage", "ListAgents", "ToolSearch")

FAILURE_REASON = (
    "Blocked: a hook gate could not judge this call, and a gate that cannot judge never lets a call through.\n\n"
    "{failures}\n\n"
    "The live gates run from {here}, the Tools checkout on main. A fix is made on a branch in a separate worktree, "
    "verified with Harness/Hooks/test_hook_dispatcher.py and the gate's own test_*.py, and goes live only when the "
    "operator merges its pull request and the checkout ({tools}) is pulled. Do not work around it. When every "
    "failure above is a timeout (\"was not imported within\" or \"did not finish within\"), the machine may only "
    "have been busy: re-issue the identical call once. Otherwise, or when it fails again, do not retry: end the "
    "turn with a NEEDED: line naming the failing gate, because only the operator can repair it."
)

POST_FAILURE_MESSAGE = (
    "A PostToolUse reminder could not run, so this edit was not checked by it:\n{failures}"
)

UNKNOWN_EVENT_MESSAGE = (
    "invoke_hook_dispatcher.py was invoked for {event!r}, which it does not handle. It is registered for "
    "PreToolUse and PostToolUse only; check the hooks block in ~/.claude/settings.json."
)

import session_home  # noqa: E402

TOOLS_CHECKOUT = session_home.main_checkout()

TOOLS_ROOT_VARIABLE = "CLAUDE_TOOLS_ROOT"

LAUNCHER = r"""import os,runpy,sys
p=os.path.join(os.environ.get('USERPROFILE',''),os.environ.get('CLAUDE_TOOLS_ROOT') or '?','Hooks','launch_dispatcher.py')
try:runpy.run_path(p,run_name='__main__')
except OSError:
 import json
 try:j=json.loads(sys.stdin.read())
 except ValueError:j={}
 j=j if isinstance(j,dict) else {}
 if j.get('hook_event_name')=='PreToolUse' and j.get('tool_name') not in('SendMessage','ListAgents','ToolSearch'):sys.stdout.write('{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Blocked: no Tools hook launcher under CLAUDE_TOOLS_ROOT. End the turn with a NEEDED: line asking the operator to set it or pull Tools on main."}}')
"""


SESSION_START_COMMAND = (
    "try { $script = Join-Path $env:USERPROFILE $env:CLAUDE_TOOLS_ROOT 'Hooks\\Invoke-SessionStart.ps1'; "
    "& $script } catch { "
    "@{ systemMessage = \"The SessionStart hook could not run ($($_.Exception.Message)), so this session started "
    "without its context. Put the Tools checkout back on main and pull it.\"; "
    "hookSpecificOutput = @{ hookEventName = 'SessionStart'; additionalContext = 'WARNING: the SessionStart hook "
    "did not run, so the Stop-gate rules are not in context. Read Harness/Hooks/stop-gate-rules.txt in full before "
    "ending any turn, and tell the operator the Tools checkout needs to be on main and pulled.' } } | ConvertTo-Json "
    "-Compress -Depth 4 }"
)


def failure_detail():
    """The exception being handled, as the tail of its traceback. traceback is imported only here, on a failure."""
    import traceback
    return traceback.format_exc().strip()[-FAILURE_DETAIL_LIMIT:]


def tool_input(payload):
    value = payload.get("tool_input")
    return value if isinstance(value, dict) else {}


def text_or_none(value):
    return value if isinstance(value, str) else None


def peer_gate(module, payload):
    return module.decide(payload.get("transcript_path"), payload.get("tool_name"))


def shell_gate(module, payload):
    decision, reason, _ = module.evaluate(text_or_none(tool_input(payload).get("command")),
                                          text_or_none(payload.get("session_id")), os.environ.get("USERPROFILE"),
                                          cwd=text_or_none(payload.get("cwd")))
    return decision, reason


def cadence_gate(module, payload):
    return module.decide(text_or_none(tool_input(payload).get("command")), payload.get("transcript_path"),
                         payload.get("tool_use_id"))


def agents_doc_gate(module, payload):
    file_path = text_or_none(tool_input(payload).get("file_path"))
    if file_path is None:
        return DEFER, ""
    return module.evaluate(file_path, payload.get("cwd"), payload.get("transcript_path"))


def doc_state_gate(module, payload):
    inputs = tool_input(payload)
    file_path = text_or_none(inputs.get("file_path"))
    new_text = text_or_none(inputs.get("content"))
    old_string = None
    if new_text is None:
        new_text = text_or_none(inputs.get("new_string"))
        old_string = inputs.get("old_string")
    if file_path is None or new_text is None:
        return DEFER, ""
    return module.evaluate(file_path, new_text, old_string)


def comment_reminder(module, payload):
    return module.reminder(payload)


def doc_prose_reminder(module, payload):
    return module.reminder(payload, module.load_lint()) if module.in_scope(payload) else None


def main_checkout_gate(module, payload):
    return module.evaluate(payload.get("tool_name"), tool_input(payload))


def goal_gate(module, payload):
    return module.evaluate(payload.get("tool_name"), payload.get("transcript_path"), payload.get("session_id"),
                           tool_input=tool_input(payload))


def read_scope_gate(module, payload):
    return module.evaluate(payload.get("tool_name"), tool_input(payload), payload.get("cwd"), payload.get("session_id"))


def artifact_gate(module, payload):
    return module.evaluate(payload.get("tool_name"), tool_input(payload), session_id=payload.get("session_id"))


def short_path_gate(module, payload):
    return module.evaluate(payload.get("tool_name"), tool_input(payload))


def protected_path_gate(module, payload):
    return module.evaluate(payload.get("tool_name"), tool_input(payload),
                           transcript_path=text_or_none(payload.get("transcript_path")))


def push_gate(module, payload):
    return module.evaluate(payload.get("tool_name"), tool_input(payload), text_or_none(payload.get("transcript_path")),
                           text_or_none(payload.get("tool_use_id")))


def session_name_gate(module, payload):
    return module.evaluate(payload.get("tool_name"), tool_input(payload))


def overlap_gate(module, payload):
    return module.evaluate(payload.get("tool_name"), tool_input(payload), payload.get("transcript_path"),
                           text_or_none(payload.get("session_id")))


PRE_GATES = (
    ("invoke_peer_message_gate", ALL_TOOLS, peer_gate),
    ("invoke_goal_gate", ALL_TOOLS, goal_gate),
    ("invoke_main_checkout_gate", ("Edit", "Write", "NotebookEdit"), main_checkout_gate),
    ("invoke_pretooluse_gate", SHELL_TOOLS, shell_gate),
    ("invoke_gate_cadence_gate", SHELL_TOOLS, cadence_gate),
    ("invoke_agents_doc_gate", EDIT_TOOLS, agents_doc_gate),
    ("invoke_doc_state_gate", EDIT_TOOLS, doc_state_gate),
    ("invoke_read_scope_gate", ("Read", "Grep", "Glob", "EnterWorktree"), read_scope_gate),
    ("invoke_artifact_gate", ("ArtifactData", "Artifact", "Edit", "Write"), artifact_gate),
    ("invoke_short_path_gate", ("Read", "Edit", "Write", "NotebookEdit", "Grep", "Glob"), short_path_gate),
    ("invoke_overlap_gate", ("Edit", "Write", "NotebookEdit", "PowerShell"), overlap_gate),
    ("invoke_protected_path_gate", ("Edit", "Write", "NotebookEdit"), protected_path_gate),
    ("invoke_push_gate", ("PushNotification",), push_gate),
    ("invoke_session_name_gate", ("SendMessage", "ArtifactData", "PushNotification", "PowerShell"), session_name_gate),
)

POST_REMINDERS = (
    ("invoke_comment_reminder", EDIT_TOOLS, comment_reminder),
    ("invoke_doc_prose_reminder", EDIT_TOOLS, doc_prose_reminder),
)


def applicable(gates, tool_name):
    return [(name, adapter) for name, tools, adapter in gates if tools is ALL_TOOLS or tool_name in tools]


def is_verdict(outcome):
    return (isinstance(outcome, tuple) and len(outcome) == 2 and outcome[0] in DECISIONS
            and isinstance(outcome[1], str))


def is_reminder(outcome):
    return outcome is None or isinstance(outcome, str)


def run(gates, payload, deadline, valid, importer=importlib.import_module):
    """[(name, outcome, failure)] for every gate that applies to this tool, in order. Exactly one of outcome
    and failure is set. Every gate module is imported first, one after another on one worker thread, so no two
    gates race a first import; then every gate starts at once on its own thread, so a slow gate never keeps
    another from being judged. A live call passes no deadline and waits for every gate. Only a harness passes
    one, to plant a gate that never returns without hanging itself; each gate without a result when it passes is
    a failure that says which stage it was in."""
    selected = applicable(gates, payload.get("tool_name"))
    results = {}
    modules = {}
    finish = None if deadline is None else time.monotonic() + deadline

    def remaining():
        return None if finish is None else max(0.0, finish - time.monotonic())

    def load():
        for name, _ in selected:
            try:
                modules[name] = importer(name)
            except BaseException:
                results[name] = (None, failure_detail())

    def judge(name, adapter):
        try:
            outcome = adapter(modules[name], payload)
        except BaseException:
            results[name] = (None, failure_detail())
            return
        if valid(outcome):
            results[name] = (outcome, None)
        else:
            results[name] = (None, "returned {0!r}, which is not a verdict".format(outcome)[:FAILURE_DETAIL_LIMIT])

    loader = threading.Thread(target=load, daemon=True)
    loader.start()
    loader.join(remaining())
    loaded = [(name, adapter) for name, adapter in selected if name in modules] if not loader.is_alive() else []
    judges = [threading.Thread(target=judge, args=(name, adapter), daemon=True) for name, adapter in loaded]
    for thread in judges:
        thread.start()
    for thread in judges:
        thread.join(remaining())
    stage = {name: "did not finish within {0} seconds".format(deadline) for name, _ in loaded}
    unloaded = "was not imported within {0} seconds".format(deadline)
    return [(name,) + results.get(name, (None, stage.get(name, unloaded))) for name, _ in selected]


def describe(failures):
    return "\n\n".join("{0}: {1}".format(name, failure) for name, failure in failures)


def merge(verdicts):
    """(decision, reason) the host reaches from these verdicts as separate hooks: the most restrictive wins."""
    for decision in PRECEDENCE:
        reasons = [reason for current, reason in verdicts if current == decision]
        if reasons:
            return decision, "\n\n".join(reason for reason in reasons if reason)
    return DEFER, ""


def pre_tool_use(payload, deadline=None, importer=importlib.import_module):
    results = run(PRE_GATES, payload, deadline, is_verdict, importer)
    failures = [(name, failure) for name, _, failure in results if failure]
    verdicts = [outcome for _, outcome, failure in results if not failure]
    if failures and payload.get("tool_name") not in REPLY_TOOLS:
        failed = FAILURE_REASON.format(failures=describe(failures), here=HERE, tools=TOOLS_CHECKOUT)
        return DENY, "\n\n".join([failed] + [reason for decision, reason in verdicts if decision == DENY and reason])
    return merge(verdicts)


def post_tool_use(payload, deadline=None, importer=importlib.import_module):
    """The PostToolUse output object, empty when there is nothing to say."""
    results = run(POST_REMINDERS, payload, deadline, is_reminder, importer)
    messages = [outcome for _, outcome, failure in results if not failure and outcome]
    failures = [(name, failure) for name, _, failure in results if failure]
    output = {}
    if failures:
        warning = POST_FAILURE_MESSAGE.format(failures=describe(failures))
        output["systemMessage"] = warning
        messages.append(warning)
    if messages:
        output["hookSpecificOutput"] = {"hookEventName": POST_TOOL_USE, "additionalContext": "\n\n".join(messages)}
    return output


def halt_output(payload, reason):
    """continue: false with the reason, and a deny besides on PreToolUse, so the call itself never runs."""
    halt = {"continue": False, "stopReason": reason}
    if payload.get("hook_event_name") == PRE_TOOL_USE:
        halt["hookSpecificOutput"] = {"hookEventName": PRE_TOOL_USE, "permissionDecision": DENY,
                                      "permissionDecisionReason": reason}
    return halt


def quarantine_halt(payload, importer=importlib.import_module):
    """The output that halts a quarantined session, or None. A session ejected for trying to change the gate that
    judges it never acts again: every hook call it makes is refused, and a PreToolUse call is denied besides, so it
    cannot run the command that would delete its own record. When the record cannot be judged at all, the call
    fails closed like any gate that cannot judge."""
    try:
        halt = importer("session_quarantine").halt_envelope(payload.get("session_id"))
    except BaseException:
        return halt_output(payload, FAILURE_REASON.format(
            failures="session_quarantine: " + failure_detail(),
            here=HERE, tools=TOOLS_CHECKOUT))
    return halt_output(payload, halt["stopReason"]) if halt else None


def eject_on_gate_change(payload, importer=importlib.import_module):
    """Quarantines and halts a session whose PreToolUse call tries to change a gate that judges it (see
    invoke_main_checkout_gate.gate_change), or None. A failure here leaves the call to the gates, which deny it."""
    if payload.get("hook_event_name") != PRE_TOOL_USE:
        return None
    try:
        why = importer("invoke_main_checkout_gate").gate_change(payload.get("tool_name"), tool_input(payload))
        if not why:
            return None
        evidence = json.dumps({"tool_name": payload.get("tool_name"), "tool_input": tool_input(payload)})
        reason = importer("session_quarantine").quarantine(payload.get("session_id"), why, evidence)
    except BaseException:
        return None
    return halt_output(payload, reason)


def share_deny(payload, importer=importlib.import_module):
    """A deny for a PreToolUse call on a machine the state share enables whose share root cannot be reached, or None.
    The hooks' state lives there, so a gate that wrote it locally instead would judge from a split copy; the reason
    names the fix. A failure to judge fails closed like any gate."""
    if payload.get("hook_event_name") != PRE_TOOL_USE:
        return None
    try:
        problem = importer("claude_temp").share_problem()
    except BaseException:
        problem = "the state share could not be checked: " + failure_detail()
    if not problem:
        return None
    return {"hookSpecificOutput": {"hookEventName": PRE_TOOL_USE, "permissionDecision": DENY,
                                   "permissionDecisionReason": "Blocked: " + problem + "."}}


def respond(payload, deadline=None, importer=importlib.import_module):
    """The JSON object to print for this payload, or None to print nothing."""
    unreachable = share_deny(payload, importer)
    if unreachable is not None:
        return unreachable
    halted = quarantine_halt(payload, importer) or eject_on_gate_change(payload, importer)
    if halted is not None:
        return halted
    event = payload.get("hook_event_name")
    if event == PRE_TOOL_USE:
        decision, reason = pre_tool_use(payload, deadline, importer)
        if decision == DEFER:
            return None
        return {"hookSpecificOutput": {"hookEventName": PRE_TOOL_USE, "permissionDecision": decision,
                                       "permissionDecisionReason": reason}}
    if event == POST_TOOL_USE:
        return post_tool_use(payload, deadline, importer) or None
    return {"systemMessage": UNKNOWN_EVENT_MESSAGE.format(event=event)}


def read_payload(stream):
    """The hook payload, or None when stdin is not a JSON object. Read as UTF-8 bytes: the default text stdin
    on Windows is the ANSI code page, which garbles every non-ASCII character a gate must match."""
    raw = stream.buffer.read() if hasattr(stream, "buffer") else stream.read().encode("utf-8")
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def main():
    payload = read_payload(sys.stdin)
    if payload is None:
        output = {"hookSpecificOutput": {"hookEventName": PRE_TOOL_USE, "permissionDecision": DENY,
                                         "permissionDecisionReason": FAILURE_REASON.format(
                                             failures="invoke_hook_dispatcher: stdin was not a JSON object",
                                             here=HERE, tools=TOOLS_CHECKOUT)}}
    else:
        output = respond(payload)
    if output is not None:
        sys.stdout.write(json.dumps(output, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
