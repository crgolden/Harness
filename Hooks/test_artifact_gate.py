"""Regression harness for invoke_artifact_gate.py. Exits non-zero on any regression.

Every rule has a must-block row and a must-allow control, against a fixture workspace whose one recorded page is
PAGE, so a rule that matched everything or nothing fails here.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import invoke_artifact_gate as gate  # noqa: E402

PAGE = "https://claude.ai/artifact/FixturePage1"
OTHER = "https://claude.ai/artifact/SomeOtherApp2"
OWNER = "00000000-0000-0000-0000-000000000000"
PEER = "11111111-1111-1111-1111-111111111111"
NEWCOMER = "22222222-2222-2222-2222-222222222222"
PEER_PAGE = "https://claude.ai/artifact/PeerPage5"
QUESTION = {"question": "Which repo first?", "context": "Two repos wait on a gate.", "recommendation": "Tools.",
            "options": ["Tools", "Curator"], "askedBy": "crgolden-99", "askedAt": "2026-10-10T15:00:00.1234567Z",
            "status": "open"}
LEDGER = {"at": "2026-10-10T15:00:00Z", "summary": "Fixed the reader.", "evidence": "goal_pin.py:12"}
GOAL = {"goal": "ship it", "session": "crgolden-99", "setAt": "2026-10-10T15:00:00Z", "status": "set"}
REPLY = {"reply": "Because the gate reads it.", "repliedAt": "2026-10-10T15:05:00Z", "status": "replied"}
CHAT_ASK = dict(REPLY, question="why does the gate only count it then?", askedAt="2026-10-10T15:04:00Z",
                source="chat")
failures = []


def record(name, ok, detail=""):
    print("{0}  {1:<78} {2}".format("PASS" if ok else "FAIL", name, detail))
    if not ok:
        failures.append(name)


def write(action="set", collection="questions", doc_id="q1-which-repo", data=None, url=PAGE, **extra):
    tool_input = {"action": action, "url": url, "collection": collection, "doc_id": doc_id}
    if data is not None:
        tool_input["data"] = data
    tool_input.update(extra)
    return tool_input


def main():
    root = tempfile.mkdtemp(prefix="artifact-gate-", dir=claude_temp.scratch_dir())
    os.environ["CLAUDE_CODE_TMPDIR"] = os.path.join(root, "temp")
    try:
        state = gate.session_home.parking_dir(None, "00000000-0000-0000-0000-000000000000")
        os.makedirs(state)
        with open(os.path.join(state, "state.json"), "w", encoding="utf-8") as handle:
            json.dump({"url": PAGE, "name": "crgolden-99"}, handle)
        commit_file = os.path.join(state, "commits", "Tools.json")
        workspace_file = os.path.join(state, "commits", "Directory.json")
        for path, message in ((commit_file, "x"), (workspace_file, "y")):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"repo": os.path.basename(path)[:-5], "message": message}, handle)
        landed_copy = {"repo": "Inventory", "message": "z", "landedAt": "2026-10-23T18:00:00Z"}
        with open(os.path.join(state, "commits", "Inventory.landed"), "w", encoding="utf-8") as handle:
            json.dump(landed_copy, handle)
        answered_file = os.path.join(root, "answered.json")
        with open(answered_file, "w", encoding="utf-8") as handle:
            json.dump(dict(QUESTION, answer="yes"), handle)

        def judge(tool_input, tool="ArtifactData"):
            return gate.evaluate(tool, tool_input, workspace=root)

        blocked = [
            ("an agent setting a question's answer", write("update", data={"answer": "yes"}, if_version=1)),
            ("an agent setting answeredAt", write("update", data={"answeredAt": "2026-10-10T15:00:00Z"}, if_version=1)),
            ("an agent removing an answer", write("update", data={"answer": {"__delete__": True}}, if_version=1)),
            ("an agent setting status answered", write("update", data={"status": "answered"}, if_version=1)),
            ("an agent setting the invalid status addressed", write("update", data={"status": "addressed"},
                                                                    if_version=1)),
            ("an agent reopening a question", write("update", data={"status": "open"}, if_version=1)),
            ("a withdrawal with no reason", write("update", data={"status": "withdrawn"}, if_version=1)),
            ("a new question carrying an answer", write(data=dict(QUESTION, answer="yes"))),
            ("a new question whose status is answered", write(data=dict(QUESTION, status="answered"))),
            ("a new question with no context", write(data=dict(QUESTION, context=""))),
            ("a new question with no options", write(data=dict(QUESTION, options=[]))),
            ("a new question with a typed time", write(data=dict(QUESTION, askedAt="around 3pm"))),
            ("a question id not shaped qN-slug", write(doc_id="question-1", data=QUESTION)),
            ("an answer arriving through file_path", write(file_path=answered_file)),
            ("an answer arriving in a batch", {"action": "batch", "url": PAGE, "writes": [
                {"op": "update", "collection": "questions", "doc_id": "q1-which-repo", "data": {"answer": "yes"},
                 "if_version": 1}]}),
            ("str_replace on an answer", write("str_replace", field="answer", old_str="no", new_str="yes",
                                               if_version=1)),
            ("str_replace on a status", write("str_replace", field="status", old_str="open", new_str="answered",
                                              if_version=1)),
            ("deleting a question", write("delete", if_version=1)),
            ("deleting a ledger entry", write("delete", collection="log", doc_id="40-reader", if_version=1)),
            ("a ledger entry with no evidence", write(collection="log", doc_id="40-reader",
                                                      data=dict(LEDGER, evidence=""))),
            ("a ledger entry id not shaped NN-slug", write(collection="log", doc_id="reader", data=LEDGER)),
            ("an inline commit message that is not the one parking_lot.py wrote", write(
                collection="commits", doc_id="Tools", data={"repo": "Tools", "message": "something else"})),
            ("a landed-shaped update no landed copy holds, its landedAt typed rather than the one committed wrote",
             write("update", collection="commits", doc_id="Inventory", if_version=1,
                   data=dict(landed_copy, landedAt="2026-10-23T19:00:00Z"))),
            ("a commit message sent as file_path, which prompts the operator every time", write(
                collection="commits", doc_id="Tools", file_path=commit_file)),
            ("a ledger entry sent as file_path", write(collection="log", doc_id="40-reader",
                                                       file_path=answered_file)),
            ("session/goal with no setAt", write(collection="session", doc_id="goal", data=dict(GOAL, setAt=""))),
            ("a collection the page does not read", write(collection="votes", doc_id="v1", data={"x": 1})),
            ("a new question asking leave to commit", write(data=dict(
                QUESTION, question="May I commit the two Syncthing files (not push)?"))),
            ("a new question whose recommendation is to commit", write(data=dict(
                QUESTION, recommendation="Commit those two files after the gate passes."))),
            ("a new question whose option names a commit", write(data=dict(
                QUESTION, options=["Don't commit; I'll handle it", "Tools"]))),
            ("a reworded question naming Committing, any case", write("update", data={
                "question": "Committing now, or after the gate?"}, if_version=1)),
            ("a commit question in a batch", {"action": "batch", "url": PAGE, "writes": [
                {"op": "set", "collection": "questions", "doc_id": "q2-ship", "data": dict(
                    QUESTION, question="Should I commit and push?")}]}),
            ("str_replace putting commit into a question", write("str_replace", field="question", old_str="Which",
                                                                new_str="Which commit", if_version=1)),
            ("a new question asking leave to push, the other half of the pair", write(data=dict(
                QUESTION, question="May I push the Tools branch now?"))),
            ("a new question whose option names a push, any case", write(data=dict(
                QUESTION, options=["PUSH it after the gate", "Wait for the review"]))),
            ("str_replace putting pushed into a recommendation", write("str_replace", field="recommendation",
                                                                      old_str="Wait", new_str="Get it pushed",
                                                                      if_version=1)),
            ("a bare 'shall I push?'", write(data=dict(QUESTION, question="Shall I push?"))),
            ("an option naming git push", write(data=dict(QUESTION, options=["Run git push", "Wait"]))),
            ("a recommendation to push to origin", write(data=dict(QUESTION, recommendation="Push to origin now"))),
            ("a noun push, kept refused beside commit", write(data=dict(
                QUESTION, question="Is the once-a-minute metrics push the stall?"))),
            ("an agent rewriting the operator's ask", write("update", collection="asks", doc_id="p-1",
                                                            data=dict(REPLY, question="something easier"),
                                                            if_version=1)),
            ("an agent marking its own reply read", write("update", collection="asks", doc_id="p-1",
                                                          data=dict(REPLY, readAt="2026-10-10T15:06:00Z"),
                                                          if_version=1)),
            ("a reply with no text", write("update", collection="asks", doc_id="p-1", data=dict(REPLY, reply=" "),
                                           if_version=1)),
            ("a reply with a typed time", write("update", collection="asks", doc_id="p-1",
                                                data=dict(REPLY, repliedAt="just now"), if_version=1)),
            ("a reply setting a status other than replied", write("update", collection="asks", doc_id="p-1",
                                                                  data=dict(REPLY, status="open"), if_version=1)),
            ("an agent overwriting a page ask with set", write(collection="asks", doc_id="p-1", data=CHAT_ASK)),
            ("an agent creating a page-shaped ask", write(collection="asks", doc_id="c1-why",
                                                          data=dict(CHAT_ASK, source="page"))),
            ("a chat ask with no question", write(collection="asks", doc_id="c1-why", data=dict(CHAT_ASK,
                                                                                                 question=""))),
            ("a chat ask with a typed askedAt", write(collection="asks", doc_id="c1-why",
                                                      data=dict(CHAT_ASK, askedAt="earlier"))),
            ("a chat ask the agent marks read", write(collection="asks", doc_id="c1-why",
                                                      data=dict(CHAT_ASK, readAt="2026-10-10T15:06:00Z"))),
            ("deleting an ask", write("delete", collection="asks", doc_id="p-1", if_version=1)),
            ("str_replace on an ask's question", write("str_replace", collection="asks", doc_id="p-1",
                                                       field="question", old_str="why", new_str="how",
                                                       if_version=1)),
            ("an ask reply sent as file_path", write("update", collection="asks", doc_id="p-1",
                                                     file_path=answered_file, if_version=1)),
        ]
        for name, tool_input in blocked:
            decision, reason = judge(tool_input)
            record("must-block: " + name, decision == gate.DENY, reason[:90])

        allowed = [
            ("a well-formed new question", write(data=QUESTION)),
            ("an agent withdrawing a question with a reason", write("update", data={
                "status": "withdrawn", "withdrawnReason": "superseded"}, if_version=1)),
            ("an agent correcting a question's measured askedAt", write("update", data={
                "askedAt": "2026-10-10T15:01:00Z"}, if_version=1)),
            ("str_replace on a question's context", write("str_replace", field="context", old_str="Two",
                                                          new_str="Three", if_version=1)),
            ("a question whose context cites a commit as evidence", write(data=dict(
                QUESTION, context="Commit 53df4e1 holds both files."))),
            ("control: a question about the master's PushNotification names no push", write(data=dict(
                QUESTION, question="Should the master's PushNotification name every open question id?"))),
            ("control: a push notification, spelled as two words, names no push", write(data=dict(
                QUESTION, options=["Send a push notification per question", "One per cycle"]))),
            ("control: a push-notification, hyphenated, names no push", write(data=dict(
                QUESTION, recommendation="Keep the push-notification per cycle"))),
            ("control: a Prometheus pushgateway names no push", write(data=dict(
                QUESTION, question="Should Alloy scrape the Prometheus pushgateway?"))),
            ("withdrawing a commit question, its reason naming the commit", write("update", data={
                "status": "withdrawn", "withdrawnReason": "the commit message belongs in commits"}, if_version=1)),
            ("a well-formed ledger entry", write(collection="log", doc_id="40-reader", data=LEDGER)),
            ("the commit message parking_lot.py wrote, sent inline", write(
                collection="commits", doc_id="Tools", data={"repo": "Tools", "message": "x"})),
            ("control: another repo's message in the workspace's parking lot", write(
                collection="commits", doc_id="Directory", data={"repo": "Directory", "message": "y"})),
            ("the landed copy parking_lot.py committed wrote, sent as an update to fold the card", write(
                "update", collection="commits", doc_id="Inventory", data=landed_copy, if_version=1)),
            ("re-parking the message parking_lot.py wrote as an update of the existing card", write(
                "update", collection="commits", doc_id="Tools", data={"repo": "Tools", "message": "x"}, if_version=1)),
            ("a well-formed session/goal", write(collection="session", doc_id="goal", data=GOAL)),
            ("deleting a committed repo's message", write("delete", collection="commits", doc_id="Tools",
                                                          if_version=1)),
            ("reading the questions", {"action": "query", "url": PAGE, "collection": "questions"}),
            ("a reply to the operator's ask", write("update", collection="asks", doc_id="p-1", data=REPLY,
                                                    if_version=1)),
            ("a reply to an ask in a batch", {"action": "batch", "url": PAGE, "writes": [
                {"op": "update", "collection": "asks", "doc_id": "p-1", "data": REPLY, "if_version": 1}]}),
            ("a chat ask recording the operator's chat question and its reply", write(collection="asks",
                                                                                      doc_id="c1-why",
                                                                                      data=CHAT_ASK)),
            ("str_replace on an ask's reply", write("str_replace", collection="asks", doc_id="p-1", field="reply",
                                                    old_str="Because", new_str="Since", if_version=1)),
            ("reading the asks", {"action": "query", "url": PAGE, "collection": "asks",
                                  "query": {"where": [["status", "==", "open"]]}}),
            ("an answer written to an artifact that is not a parking-lot page", write(
                "update", data={"answer": "yes"}, url=OTHER, if_version=1)),
        ]
        for name, tool_input in allowed:
            decision, reason = judge(tool_input)
            record("must-allow: " + name, decision == gate.DEFER, reason[:90])

        with open(gate.TEMPLATE, encoding="utf-8", newline="") as handle:
            template = handle.read()
        filled = template.replace("{{SESSION_ID}}", "00000000-0000-4000-8000-000000000000").replace(
            "{{SESSION}}", "crgolden-99")
        rendered = os.path.join(state, "parking-lot.html")
        with open(rendered, "w", encoding="utf-8", newline="") as handle:
            handle.write(filled)
        tampered = os.path.join(root, "parking-lot.html")
        with open(tampered, "w", encoding="utf-8", newline="") as handle:
            handle.write(filled + "<script>answerEverything()</script>\n")
        unfilled_id = os.path.join(root, "unfilled-id.html")
        with open(unfilled_id, "w", encoding="utf-8", newline="") as handle:
            handle.write(template.replace("{{SESSION_ID}}", "<script>x()</script>").replace("{{SESSION}}", "crgolden-99"))
        for name, tool_input, expected in (
                ("must-allow: publishing the rendered template to the page", {"url": PAGE, "file_path": rendered},
                 gate.DEFER),
                ("must-allow: a first publish of the rendered template", {"file_path": rendered, "icon": "inbox"},
                 gate.DEFER),
                ("must-block: publishing a changed template to the page", {"url": PAGE, "file_path": tampered},
                 gate.DENY),
                ("must-block: a first publish of a changed template", {"file_path": tampered}, gate.DENY),
                ("must-block: a page whose session id slot holds anything but a session id",
                 {"url": PAGE, "file_path": unfilled_id}, gate.DENY),
                ("must-block: deleting the page", {"action": "delete", "url": PAGE}, gate.DENY),
                ("must-block: uploading an asset to the page", {"url": PAGE, "file_path": rendered, "asset": True},
                 gate.DENY),
                ("must-allow: publishing some other artifact", {"file_path": os.path.join(root, "report.html")},
                 gate.DEFER)):
            decision, reason = judge(tool_input, tool="Artifact")
            record(name, decision == expected, reason[:90])

        # Each refusal names the write that clears it, not only the rule it broke.
        for name, tool_input, tool, action in (
                ("a status str_replace reason shows the withdrawal write",
                 write("str_replace", field="status", old_str="open", new_str="answered", if_version=1), "ArtifactData",
                 "\"withdrawnReason\": \"<why>\""),
                ("a ledger delete reason shows the correcting entry",
                 write("delete", collection="log", doc_id="40-reader", if_version=1), "ArtifactData",
                 "a new entry whose summary names the entry it corrects"),
                ("a typed ledger time reason shows the command that measures it",
                 write(collection="log", doc_id="40-reader", data=dict(LEDGER, at="around 3pm")), "ArtifactData",
                 "Get-Date -AsUTC -Format o"),
                ("a changed page reason shows the render command",
                 {"url": PAGE, "file_path": tampered}, "Artifact", 'parking_lot.py" render')):
            decision, reason = judge(tool_input, tool=tool)
            record("reason names its action: " + name, decision == gate.DENY and action in reason, reason[-120:])

        state_file = os.path.join(state, "state.json")
        decision, reason = gate.evaluate("Write", {"file_path": state_file, "content": "{}"}, workspace=root)
        record("must-block: a file-tool write to a page's state.json", decision == gate.DENY and "record-url" in reason)
        decision, _ = gate.evaluate("Edit", {"file_path": os.path.join(root, "notes", "state.json")}, workspace=root)
        record("must-allow: a state.json outside the parking lot", decision == gate.DEFER)

        recorded_page_cases(root)
        page_ownership_cases(root)
        page_answer_cases(root)
    finally:
        shutil.rmtree(root, ignore_errors=False)
    print("{0} failure(s)".format(len(failures)))
    return 1 if failures else 0


def recorded_page_cases(root):
    """A page recorded in a parking lot under %TEMP%\\claude is a parking-lot page, and an unrecorded one is not."""
    decision, reason = gate.evaluate("ArtifactData", write("update", data={"answer": "yes"},
                                                           url="https://claude.ai/artifact/UnrecordedPage3",
                                                           if_version=1), workspace=root)
    record("control: a page no parking lot records is not read as one", decision == gate.DEFER, reason[:90])
    decision, reason = gate.evaluate("ArtifactData", write("update", data={"answer": "yes"}, url=PAGE, if_version=1),
                                     workspace=root)
    record("must-block: an answer written to a page recorded under %TEMP%\\claude", decision == gate.DENY,
           reason[:90])


def page_ownership_cases(root):
    """A session writes only its own page: PAGE belongs to OWNER, PEER_PAGE to PEER, and NEWCOMER has none yet."""
    peer_state = gate.session_home.parking_dir(None, PEER)
    os.makedirs(peer_state)
    with open(os.path.join(peer_state, "state.json"), "w", encoding="utf-8") as handle:
        json.dump({"url": PEER_PAGE, "name": "master"}, handle)

    def judge(tool_input, session_id, tool="ArtifactData"):
        return gate.evaluate(tool, tool_input, workspace=root, session_id=session_id)

    ledger = write(collection="log", doc_id="61-regate-precheck", data=LEDGER, url=PEER_PAGE)
    decision, reason = judge(ledger, OWNER)
    record("must-block: a well-formed ledger entry written to a peer's page", decision == gate.DENY
           and "another session's parking-lot page" in reason and PAGE in reason, reason[:90])
    decision, reason = judge(write(collection="commits", doc_id="Tools", data={"repo": "Tools", "message": "x"},
                                   url=PEER_PAGE), OWNER)
    record("must-block: the commit message parking_lot.py wrote, sent to a peer's page", decision == gate.DENY,
           reason[:90])
    decision, reason = judge(write(data=QUESTION, url=PEER_PAGE), OWNER)
    record("must-block: a well-formed question parked on a peer's page", decision == gate.DENY, reason[:90])
    decision, reason = judge(write("batch", url=PEER_PAGE, collection=None, doc_id=None,
                                   writes=[{"op": "set", "collection": "log", "doc_id": "62-x", "data": LEDGER}]), OWNER)
    record("must-block: a batch to a peer's page", decision == gate.DENY, reason[:90])
    decision, reason = judge(write(collection="log", doc_id="61-x", data=LEDGER), NEWCOMER)
    record("must-block: a session with no recorded page writing a recorded one", decision == gate.DENY
           and "not recorded yet" in reason, reason[:90])
    decision, reason = judge(write(collection="log", doc_id="61-regate-precheck", data=LEDGER), OWNER)
    record("control: the same ledger entry written to the session's own page", decision == gate.DEFER, reason[:90])
    decision, reason = judge({"action": "query", "url": PEER_PAGE, "collection": "questions"}, OWNER)
    record("control: a read of a peer's page", decision == gate.DEFER, reason[:90])
    decision, reason = judge(write(collection="claims", doc_id="c1", data={"x": 1},
                                   url="https://claude.ai/artifact/CoordinationBoard4"), OWNER)
    record("control: a write to a page no parking lot records (the coordination board) is not judged",
           decision == gate.DEFER, reason[:90])
    decision, reason = judge(write(data=dict(QUESTION, answer="yes")), OWNER)
    record("must-block: the session's own page still holds every shape rule", decision == gate.DENY, reason[:90])
    import invoke_hook_dispatcher as dispatcher
    decision, reason = dispatcher.artifact_gate(gate, {"tool_name": "ArtifactData", "session_id": OWNER,
                                                       "tool_input": ledger})
    record("must-block: the dispatcher hands the gate the payload's session_id", decision == gate.DENY, reason[:90])


def tool_use(use_id, name, tool_input):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": use_id, "name": name, "input": tool_input}]}}


def tool_result(use_id, body, is_error=False):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": use_id, "content": body, "is_error": is_error}]}}


def store_rows(*rows):
    return "=== BEGIN ARTIFACT DB x ===\n" + "\n".join(json.dumps(row, separators=(",", ":")) for row in rows) + \
        "\n=== END ARTIFACT DB x ==="


def page_answer_cases(root):
    """page_answers: only answers this session read back from a page it published itself, and never one it wrote."""
    answered = {"id": "q1-which-repo", "data": dict(QUESTION, status="answered", answer="yes ship the Tools fix",
                                                   answeredAt="2026-10-10T15:05:00Z"), "version": 3}
    other = {"id": "q2-other", "data": dict(QUESTION, status="answered", answer="an answer this session wrote",
                                           answeredAt="2026-10-10T15:06:00Z"), "version": 2}
    still_open = {"id": "q3-open", "data": QUESTION, "version": 1}
    records = [
        tool_use("p1", "Artifact", {"file_path": os.path.join(root, "parking-lot.html"), "icon": "inbox"}),
        tool_result("p1", "Published: " + PAGE),
        tool_use("w1", "ArtifactData", {"action": "update", "url": PAGE, "collection": "questions", "doc_id": "q2-other",
                                        "data": {"answer": "an answer this session wrote"}, "if_version": 1}),
        tool_result("w1", "committed"),
        tool_use("r1", "ArtifactData", {"action": "query", "url": PAGE, "collection": "questions"}),
        tool_result("r1", store_rows(answered, other, still_open)),
        tool_use("r2", "ArtifactData", {"action": "query", "url": OTHER, "collection": "questions"}),
        tool_result("r2", store_rows({"id": "q9-elsewhere", "data": dict(
            QUESTION, status="answered", answer="an answer on another artifact"), "version": 1})),
    ]
    path = os.path.join(root, "session.jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for item in records:
            handle.write(json.dumps(item) + "\n")
    texts = [answer for _, answer in gate.page_answers(path)]
    record("page answers: an answer read back from this session's own page counts", "yes ship the Tools fix" in texts,
           repr(texts))
    record("page answers: an answer this session wrote never counts", "an answer this session wrote" not in texts)
    record("page answers: an answer on another artifact never counts", "an answer on another artifact" not in texts)
    record("page answers: an open question carries no answer", len(texts) == 1, repr(texts))
    record("page answers: no transcript, no answers", gate.page_answers(os.path.join(root, "absent.jsonl")) == [])


if __name__ == "__main__":
    sys.exit(main())
