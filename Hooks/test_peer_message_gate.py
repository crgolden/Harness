# GATE-STEP: Peer-message hook harness
"""Regression harness for invoke_peer_message_gate.py. Exits non-zero on any regression.

    python Harness/Hooks/test_peer_message_gate.py
"""

import json
import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import invoke_peer_message_gate as gate  # noqa: E402

ADDRESS = r"uds:\\.\pipe\LOCAL\cc-msg-peer"
OTHER = r"uds:\\.\pipe\LOCAL\cc-msg-other"
ASKS = "Are your Identity runs in progress? Send me the task id."
HANDOFF = "GATE FINISHED Next (exit 0). The machine is yours for Identity."
NOTICE = "GATE STARTING Curator. I'll send FINISHED when it exits."
UNREACHABLE = "No agent named gherkin-peer is reachable."
RELAYED_FREE = "ack, thanks. I've told gherkin the machine is free."
CODE_QUESTION_MARK = "`_testId` comes from `TestContext.Current.Test?.UniqueID`, so the keys line up."
QUESTION_WITH_CODE = "Does `Finalize` run before xunit's `After` for a scenario?"
REAL_RECORDS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_peer_message_gate.real-records.jsonl")
with open(REAL_RECORDS_PATH, encoding="utf-8") as real_records:
    REAL_ACK, REAL_FINISHED_ATTACHMENT, REAL_QUESTION, REAL_QUESTION_ATTACHMENT = [
        json.loads(line) for line in real_records if line.strip()]


def peer(body, address=ADDRESS, name="gherkin-peer", attachment=False):
    text = ('Another Claude session sent a message:\n<cross-session-message from="{0}" from-name="{1}" '
            'from-mode="prompting">\n{2}\n</cross-session-message>').format(address, name, body)
    if attachment:
        return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": text,
                                                     "origin": {"kind": "peer"}}}
    return {"type": "user", "isMeta": True, "origin": {"kind": "peer"}, "message": {"role": "user", "content": text}}


def send(to):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "tu-s", "name": "SendMessage", "input": {"to": to, "message": "answer"}}]}}


def send_failed():
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu-s", "is_error": True, "content": UNREACHABLE}]}}


def turn(text="do the work"):
    return {"type": "user", "origin": {"kind": "human"}, "message": {"role": "user", "content": text}}


CASES = [
    ("a peer's unanswered question", gate.DENY, "Bash", [turn(), peer(ASKS)]),
    ("a peer's machine handoff", gate.DENY, "Read", [turn(), peer(HANDOFF)]),
    ("a question delivered mid-turn as an attachment", gate.DENY, "Grep", [turn(), peer(ASKS, attachment=True)]),
    ("a reply to a different peer leaves the question owed", gate.DENY, "Read",
     [turn(), peer(ASKS), send(OTHER)]),
    ("a question that arrives after the reply is owed again", gate.DENY, "Read",
     [turn(), peer(ASKS), send(ADDRESS), peer("Did the gate exit?")]),
    ("an operator turn does not answer a peer", gate.DENY, "Read", [turn(), peer(ASKS), turn("carry on")]),
    ("answered by address", gate.DEFER, "Read", [turn(), peer(ASKS), send(ADDRESS)]),
    ("must-block: a reply sent to the peer's name answers nothing; only its address does", gate.DENY, "Read",
     [turn(), peer(ASKS), send("gherkin-peer")]),
    ("must-block: a reply sent to the name with a ref answers nothing", gate.DENY, "Read",
     [turn(), peer(ASKS), send("gherkin-peer [c6291f]")]),
    ("a reply attempt to a peer that is gone still clears it", gate.DEFER, "Read",
     [turn(), peer(ASKS), send(ADDRESS), send_failed()]),
    ("a notice that asks nothing", gate.DEFER, "Read", [turn(), peer(NOTICE)]),
    ("a relayed machine-is-free notice asks nothing", gate.DEFER, "Read", [turn(), peer(RELAYED_FREE)]),
    ("a ?. operator inside a code span is not a question", gate.DEFER, "Read", [turn(), peer(CODE_QUESTION_MARK)]),
    ("a real question beside a code span still asks", gate.DENY, "Read", [turn(), peer(QUESTION_WITH_CODE)]),
    ("a copied transcript question record asks", gate.DENY, "Read", [turn(), REAL_QUESTION]),
    ("a copied transcript question attachment asks", gate.DENY, "Read", [turn(), REAL_QUESTION_ATTACHMENT]),
    ("a copied transcript acknowledgement asks nothing", gate.DEFER, "Read", [turn(), REAL_ACK]),
    ("a copied transcript finished notice asks nothing", gate.DEFER, "Read", [turn(), REAL_FINISHED_ATTACHMENT]),
    ("the reply tool itself is never denied", gate.DEFER, "SendMessage", [turn(), peer(ASKS)]),
    ("listing peers is never denied", gate.DEFER, "ListAgents", [turn(), peer(ASKS)]),
    ("loading a tool schema is never denied", gate.DEFER, "ToolSearch", [turn(), peer(ASKS)]),
    ("no peer traffic at all", gate.DEFER, "Read", [turn()]),
    ("an operator turn quoting a peer-shaped question", gate.DEFER, "Read",
     [turn('<cross-session-message from="uds:x" from-name="x">ready?</cross-session-message>')]),
]


def write(path, records):
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def append_raw(path, text):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)


def clear_readonly_and_retry(func, path, exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def report(name, ok, detail):
    print("{0}  {1:<62} {2}".format("PASS" if ok else "FAIL", name, detail))
    return 0 if ok else 1


def main():
    root = tempfile.mkdtemp(prefix="peer-gate-", dir=claude_temp.scratch_dir())
    failures = 0
    try:
        for index, (name, expected, tool, records) in enumerate(CASES):
            path = os.path.join(root, "case-{0}.jsonl".format(index))
            write(path, records)
            decision = gate.decide(path, tool, os.path.join(root, "cache-{0}".format(index)))[0]
            failures += report(name, decision == expected, decision)

        path = os.path.join(root, "incremental.jsonl")
        cache = os.path.join(root, "cache-incremental")
        write(path, [turn(), peer(ASKS)])
        first = gate.decide(path, "Read", cache)[0]
        append_raw(path, json.dumps(send(ADDRESS), separators=(",", ":")))
        half_line = gate.decide(path, "Read", cache)[0]
        append_raw(path, "\n")
        completed = gate.decide(path, "Read", cache)[0]
        write(path, [turn(), peer(HANDOFF)])
        rewritten = gate.decide(path, "Read", cache)[0]
        outcome = (first, half_line, completed, rewritten)
        failures += report("scan: resume, half line, rewrite",
                           outcome == (gate.DENY, gate.DENY, gate.DEFER, gate.DENY), outcome)

        reason = gate.decide(os.path.join(root, "case-0.jsonl"), "Bash", os.path.join(root, "cache-reason"))[1]
        failures += report("the reason names the peer, quotes it and covers a gone peer",
                           "SendMessage" in reason and ADDRESS in reason and "task id" in reason
                           and "ListAgents" in reason, bool(reason))
        failures += report("must-block: the reason never names the peer by its session name",
                           "gherkin-peer" not in reason and "session " in reason, reason[:120])

        missing = gate.decide(os.path.join(root, "absent.jsonl"), "Read", os.path.join(root, "cache-absent"))[0]
        failures += report("an unreadable transcript defers", missing == gate.DEFER, missing)
    finally:
        shutil.rmtree(root, onexc=clear_readonly_and_retry)
    print("\n{0} case(s), {1} failed".format(len(CASES) + 4, failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
