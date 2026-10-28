"""PreToolUse gate: an agent uses a parking-lot page only the way the page is built to be used, and never answers it.

An agent writes a page's store as the operator does (both are the owner), so the store alone cannot tell the
operator's answer from one a session typed. What can tell them apart is the path: the operator answers with the
page's own controls, in the browser, which no hook sees; an agent writes through ArtifactData or Artifact tool
calls, which this gate sees before they run. So every agent write to a recorded parking-lot page (any session's,
from any parking-lot/<session>/state.json) is held to the page's shape, and none may set, change or remove an
answer. Every answer on a page is then the operator's, and page_answers() hands the ones this session read back
from its own page to the hooks that check the operator's words.

  deny  any write to a recorded page other than this session's own (the hook payload's session_id names the
        parking-lot/<session>/state.json holding it; a peer's page is only read),
        a write setting or removing answer or answeredAt, a status other than open on a new question or withdrawn
        (with withdrawnReason) on an existing one, a question id not shaped qN-slug, a question missing its
        context, recommendation, options, askedBy or a measured askedAt, a question whose question, recommendation
        or an option names committing or pushing (the operator's acts; the message goes in commits), the deletion of a question or a ledger
        entry, an ask write touching anything but its reply, repliedAt (measured) and status replied, a new ask that
        is not a cN-slug chat ask (source chat, the operator's question and its message's askedAt), the deletion of an
        ask, a ledger entry missing its id shape, measured time, summary or evidence, any write sending its data
        as file_path (Claude Code asks the operator to approve every such write), a commit message other than the
        object parking_lot.py commit wrote (or the landed copy parking_lot.py committed wrote), a collection the page
        does not read, a page publish that is not the rendered template, an asset upload or delete of a page, and a
        file-tool edit of a page's state.json.

Everything else defers. The rules are described for agents in the artifact-usage skill. Any change must be verified
with Harness/Hooks/test_artifact_gate.py.
"""

import json
import os
import re
import sys

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_home  # noqa: E402

DENY = "deny"
DEFER = "defer"

WORKSPACE = session_home.workspace_root()
TEMPLATE = os.path.join(WORKSPACE, "Tools", "Claude", "skills", "parking-lot", "assets", "parking-lot.html")
QUESTION_STATUSES = ("open", "answered", "withdrawn")
ANSWER_FIELDS = ("answer", "answeredAt")
QUESTION_TEXT_FIELDS = ("question", "context", "recommendation", "askedBy")
QUESTION_ASK_FIELDS = ("question", "recommendation", "options")
RE_COMMIT_OR_PUSH_WORD = re.compile(r"commit|\bpush(?:es|ed|ing)?\b(?![\s-]*notification)", re.IGNORECASE)
COMMIT_QUESTION_PROBLEM = "a parking-lot question never asks about committing or pushing: committing, pushing and " \
    "merging are the operator's, and a repo gated green owes its message in commits (parking_lot.py commit), never a " \
    "question"
LOG_TEXT_FIELDS = ("summary", "evidence")
SESSION_TEXT_FIELDS = ("goal", "session", "status")
PAGE_COLLECTIONS = ("questions", "asks", "log", "commits", "session")
ASK_REPLY_FIELDS = ("reply", "repliedAt", "status")
RE_QUESTION_ID = re.compile(r"^q\d+-[a-z0-9]+(?:-[a-z0-9]+)*$")
RE_CHAT_ASK_ID = re.compile(r"^c\d+-[a-z0-9]+(?:-[a-z0-9]+)*$")
RE_LOG_ID = re.compile(r"^\d{2,}-[a-z0-9]+(?:-[a-z0-9]+)*$")
RE_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$")
RE_ARTIFACT_URL = re.compile(r"https://claude\.ai/(?:code/)?artifact/[A-Za-z0-9-]+")
RE_STORE_ROW = re.compile(r'^\{"id":.*\}$', re.MULTILINE)
WRITE_ACTIONS = ("set", "update", "str_replace", "delete", "batch")
READ_ACTIONS = ("get", "list", "query")
ARTIFACT_NEEDLES = (b'"Artifact"', b'"ArtifactData"')
REASON = "Blocked: {problem}. The artifact-usage skill holds the rules for a parking-lot page."
RENDER_ACTION = ("run python \"<workspace>\\Tools\\Claude\\skills\\parking-lot\\scripts\\parking_lot.py\" render "
                 "<session id> and publish the page path it prints, unchanged")


def parking_pages(workspace=WORKSPACE):
    """Every page URL a session on this machine recorded with parking_lot.py record-url
    (session_home.all_parking_dirs)."""
    urls = set()
    for path in (os.path.join(directory, "state.json") for directory in session_home.all_parking_dirs(workspace)):
        try:
            with open(path, encoding="utf-8") as handle:
                url = json.load(handle).get("url")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(url, str) and url:
            urls.add(url.rstrip("/"))
    return urls


def own_page(session_id):
    """The page URL this session recorded with parking_lot.py record-url, or None before it recorded one."""
    if not session_id:
        return None
    state = read_json_file(os.path.join(session_home.parking_dir(None, session_id), "state.json"))
    url = state.get("url") if isinstance(state, dict) else None
    return url.rstrip("/") if isinstance(url, str) and url else None


def foreign_page_problem(url, session_id):
    """Why a write to another session's parking-lot page is refused, or None for this session's own page. A session
    reads a peer's page to monitor it, but its questions, ledger, commit messages and goal belong on its own page,
    and a write to a peer's page leaves its own page stale while putting a card the peer never wrote on the peer's."""
    mine = own_page(session_id)
    if mine == url:
        return None
    return "{0} is another session's parking-lot page; this session's own page is {1}. A session writes only its " \
           "own page (parking_lot.py show prints it) and only reads a peer's".format(
               url, mine or "not recorded yet (parking_lot.py render, publish, then record-url)")


def text(value):
    return isinstance(value, str) and bool(value.strip())


def document(entry, read_json):
    """The data an entry writes: its inline data, else the JSON object in its file_path, else None."""
    if isinstance(entry.get("data"), dict):
        return entry["data"]
    path = entry.get("file_path")
    if isinstance(path, str):
        loaded = read_json(path)
        return loaded if isinstance(loaded, dict) else None
    return None


def asks_about_committing(data):
    """True when the question's own ask (its question, recommendation or an option) names committing or pushing."""
    for name in QUESTION_ASK_FIELDS:
        value = data.get(name)
        for part in value if isinstance(value, list) else [value]:
            if isinstance(part, str) and RE_COMMIT_OR_PUSH_WORD.search(part):
                return True
    return False


def question_problem(op, doc_id, data, field, new_str=None):
    if op == "delete":
        return "a question is the operator's record and is never deleted; withdraw it instead (update status " \
               "\"withdrawn\" with a withdrawnReason)"
    if op == "str_replace":
        if field in ANSWER_FIELDS + ("status",):
            return "a question's {0} is never edited in place: an answer is the operator's alone, and a question " \
                   "is withdrawn with an update whose data is {{\"status\": \"withdrawn\", \"withdrawnReason\": " \
                   "\"<why>\"}}".format(field)
        if field in QUESTION_ASK_FIELDS and isinstance(new_str, str) and RE_COMMIT_OR_PUSH_WORD.search(new_str):
            return COMMIT_QUESTION_PROBLEM
        return None
    if data is None:
        return "the question's data could not be read, so it cannot be checked; send data as an inline JSON object"
    touched = [name for name in ANSWER_FIELDS if name in data]
    if touched:
        return "only the operator answers a question, on the page; an agent never writes {0}".format(
            " or ".join(touched))
    if asks_about_committing(data):
        return COMMIT_QUESTION_PROBLEM
    if op == "set":
        if not RE_QUESTION_ID.match(doc_id or ""):
            return "a question's id is qN-slug (such as q12-dispatcher-wiring), not {0}".format(doc_id)
        missing = [name for name in QUESTION_TEXT_FIELDS if not text(data.get(name))]
        options = data.get("options")
        if not isinstance(options, list) or not options or not all(text(option) for option in options):
            missing.append("options")
        if missing:
            return "a parked question carries its {0}".format(", ".join(missing))
        if not RE_INSTANT.match(str(data.get("askedAt", ""))):
            return "askedAt is a measured UTC time (Get-Date -AsUTC -Format o), not {0!r}".format(data.get("askedAt"))
        if data.get("status") != "open":
            return "a new question's status is \"open\", not {0!r}".format(data.get("status"))
        return None
    status = data.get("status")
    if status is not None and status != "withdrawn":
        return "an agent sets a question's status only to \"withdrawn\" (the page's statuses are {0}), not " \
               "{1!r}".format(", ".join(QUESTION_STATUSES), status)
    if status == "withdrawn" and not text(data.get("withdrawnReason")):
        return "a withdrawn question carries its withdrawnReason"
    return None


def reply_problem(data):
    """What is wrong with a reply's fields, else None: a reply carries its text, a measured repliedAt and status
    replied."""
    if not text(data.get("reply")):
        return "a reply carries its reply text"
    if not RE_INSTANT.match(str(data.get("repliedAt", ""))):
        return "repliedAt is a measured UTC time (Get-Date -AsUTC -Format o), not {0!r}".format(data.get("repliedAt"))
    if data.get("status") != "replied":
        return "a reply sets status \"replied\", not {0!r}".format(data.get("status"))
    return None


def ask_problem(op, doc_id, data, field):
    """The operator asks the session questions on the page, so an ask's question, askedAt, source and readAt are
    theirs. The session writes only a reply to one (update of reply, repliedAt and status replied), or records a
    question the operator asked in chat as a new cN-slug ask with source chat, so its answer stays on the page."""
    if op == "delete":
        return "an ask is the operator's record and is never deleted"
    if op == "str_replace":
        if field != "reply":
            return "an agent edits only an ask's reply in place, never its {0}".format(field)
        return None
    if data is None:
        return "the ask's data could not be read, so it cannot be checked; send data as an inline JSON object"
    if op == "set":
        if not RE_CHAT_ASK_ID.match(doc_id or ""):
            return "an agent creates only a chat ask, id cN-slug (such as c3-why-worktree), not {0}: an ask written " \
                   "on the page is the operator's, and the reply to it is an update".format(doc_id)
        if data.get("source") != "chat" or not text(data.get("question")):
            return "a chat ask carries source \"chat\" and the operator's question, quoted verbatim"
        if not RE_INSTANT.match(str(data.get("askedAt", ""))):
            return "a chat ask's askedAt is the operator message's own UTC timestamp, not {0!r}".format(
                data.get("askedAt"))
        if "readAt" in data:
            return "only the operator marks a reply read; an agent never writes readAt"
        return reply_problem(data)
    touched = [name for name in data if name not in ASK_REPLY_FIELDS]
    if touched:
        return "an agent updates only an ask's reply, repliedAt and status, never its {0}".format(", ".join(touched))
    return reply_problem(data)


def log_problem(op, doc_id, data):
    if op == "delete":
        return "the ledger is the session's audit trail; an entry is never deleted. Correct it with a new entry " \
               "whose summary names the entry it corrects and what was wrong"
    if op != "set":
        return None
    if data is None:
        return "the ledger entry's data could not be read, so it cannot be checked; send data as an inline JSON object"
    if not RE_LOG_ID.match(doc_id or ""):
        return "a ledger entry's id is NN-slug (such as 40-goal-command-pin), not {0}".format(doc_id)
    missing = [name for name in LOG_TEXT_FIELDS if not text(data.get(name))]
    if missing:
        return "a ledger entry carries its {0}".format(", ".join(missing))
    if not RE_INSTANT.match(str(data.get("at", ""))):
        return "a ledger entry's at is a measured UTC time, not {0!r}: run Get-Date -AsUTC -Format o and send what " \
               "it prints".format(data.get("at"))
    return None


def commit_problem(op, doc_id, entry, workspace):
    """A commit message reaches the page only as the object parking_lot.py commit wrote for that repo, sent inline."""
    if op == "delete":
        return None
    data = entry.get("data")
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import parking_lot_commits
    written = [read_json_file(path) for path in parking_lot_commits.written_messages(workspace, str(doc_id))]
    if not isinstance(data, dict) or data not in written:
        return "a commit message is the object parking_lot.py commit wrote for {0}, sent inline as data, " \
               "unchanged".format(doc_id)
    return None


def session_problem(op, doc_id, data):
    if op != "set" or doc_id != "goal":
        return None
    missing = [name for name in SESSION_TEXT_FIELDS if not text((data or {}).get(name))]
    if missing:
        return "session/goal carries its {0}".format(", ".join(missing))
    if not RE_INSTANT.match(str((data or {}).get("setAt", ""))):
        return "session/goal's setAt is the goal record's own UTC timestamp, not {0!r}: copy the timestamp field of " \
               "the transcript record that set the goal (its goal_status attachment, or the operator message " \
               "opening GOAL:)".format((data or {}).get("setAt"))
    return None


def entry_problem(entry, read_json, workspace):
    op, collection, doc_id = entry.get("op"), entry.get("collection"), entry.get("doc_id")
    if entry.get("file_path"):
        return "a write to a parking-lot page sends its data inline, never as file_path: Claude Code asks the " \
               "operator to approve every write that reads a local file, while inline data needs no approval"
    if collection not in PAGE_COLLECTIONS:
        return "a parking-lot page reads only {0}; {1!r} is not one of them".format(", ".join(PAGE_COLLECTIONS),
                                                                                  collection)
    if collection == "questions":
        return question_problem(op, doc_id, None if op in ("delete", "str_replace") else document(entry, read_json),
                                entry.get("field"), entry.get("new_str"))
    if collection == "asks":
        return ask_problem(op, doc_id, None if op in ("delete", "str_replace") else document(entry, read_json),
                           entry.get("field"))
    if collection == "log":
        return log_problem(op, doc_id, None if op == "delete" else document(entry, read_json))
    if collection == "commits":
        return commit_problem(op, doc_id, entry, workspace)
    return session_problem(op, doc_id, None if op == "delete" else document(entry, read_json))


def write_entries(tool_input):
    action = tool_input.get("action")
    if action == "batch":
        writes = tool_input.get("writes")
        return [write for write in writes if isinstance(write, dict)] if isinstance(writes, list) else []
    entry = {"op": action, "collection": tool_input.get("collection"), "doc_id": tool_input.get("doc_id"),
             "data": tool_input.get("data"), "file_path": tool_input.get("file_path"),
             "field": tool_input.get("field"), "new_str": tool_input.get("new_str")}
    return [entry]


def read_json_file(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def rendered_template(content):
    """True when content is the parking-lot template with only its session name and session id filled in."""
    try:
        with open(TEMPLATE, encoding="utf-8", newline="") as handle:
            template = handle.read().replace("\r\n", "\n")
    except OSError:
        return False
    fills = {"{{SESSION}}": r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}", "{{SESSION_ID}}": r"[0-9a-f-]{36}"}
    pattern = "".join(re.escape(part) if index % 2 == 0 else fills[part]
                      for index, part in enumerate(re.split(r"(\{\{SESSION(?:_ID)?\}\})", template)))
    return re.fullmatch(pattern, content.replace("\r\n", "\n")) is not None


def publish_problem(tool_input, pages):
    action = tool_input.get("action") or "publish"
    url = str(tool_input.get("url") or "").rstrip("/")
    path = tool_input.get("file_path")
    is_page_file = isinstance(path, str) and os.path.basename(path) == "parking-lot.html"
    if url not in pages and not is_page_file:
        return None
    if action == "delete":
        return "a parking-lot page is the operator's record of the session and is never deleted by an agent"
    if action != "publish":
        return None
    if tool_input.get("asset") or tool_input.get("file_paths") or tool_input.get("files"):
        return "a parking-lot page carries no assets or extra files"
    if not isinstance(path, str):
        return "a parking-lot page is published only from the file parking_lot.py render wrote; " + RENDER_ACTION
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            content = handle.read()
    except OSError:
        return "the page file {0} could not be read, so it cannot be checked; {1}".format(path, RENDER_ACTION)
    if not rendered_template(content):
        return "a parking-lot page is the rendered template, unchanged (parking_lot.py render); this file differs " \
               "from Claude/skills/parking-lot/assets/parking-lot.html; " + RENDER_ACTION
    return None


def state_file_problem(tool_input):
    path = os.path.normcase(str(tool_input.get("file_path") or ""))
    lots = os.path.normcase(session_home.parking_root())
    if os.path.basename(path) == "state.json" and path.startswith(lots + os.sep):
        return "a page's state.json is written only by parking_lot.py: record-url <url> records the page's URL, " \
               "and render <session id> sets its label"
    return None


def evaluate(tool_name, tool_input, workspace=None, read_json=None, session_id=None):
    """(decision, reason). Split out so the harness and the dispatcher call it in-process. With the hook payload's
    session_id, a write to a recorded page other than that session's own is refused before its shape is judged."""
    if not isinstance(tool_input, dict):
        return DEFER, ""
    workspace = WORKSPACE if workspace is None else workspace
    problem = None
    if tool_name in ("Edit", "Write"):
        problem = state_file_problem(tool_input)
    elif tool_name == "Artifact":
        problem = publish_problem(tool_input, parking_pages(workspace))
    elif tool_name == "ArtifactData" and tool_input.get("action") in WRITE_ACTIONS:
        url = str(tool_input.get("url") or "").rstrip("/")
        if url in parking_pages(workspace):
            problem = foreign_page_problem(url, session_id) if session_id else None
            for entry in [] if problem else write_entries(tool_input):
                problem = entry_problem(entry, read_json or read_json_file, workspace)
                if problem:
                    break
    return (DENY, REASON.format(problem=problem)) if problem else (DEFER, "")


def tool_blocks(record):
    message = record.get("message") if isinstance(record.get("message"), dict) else {}
    content = message.get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def result_text(block):
    content = block.get("content")
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def page_answers(transcript_path):
    """[(answeredAt, answer)] for every answered question this session read back from a parking-lot page it
    published itself, leaving out any question this session ever wrote an answer to (before this gate existed, a
    session could). The page URL comes from Claude Code's own publish result, never from a file a session writes."""
    answers = []
    for _, data in answered_rows(transcript_path):
        entry = (str(data.get("answeredAt") or ""), data["answer"])
        if entry not in answers:
            answers.append(entry)
    return answers


def page_questions(transcript_path):
    """Every answered question page_answers() trusts, as {id, question, answeredAt, answer}, once per id, in the
    order the session first read it back, carrying the latest read of it."""
    questions = {}
    for doc_id, data in answered_rows(transcript_path):
        questions[doc_id] = {"id": doc_id, "question": str(data.get("question") or ""),
                             "answeredAt": str(data.get("answeredAt") or ""), "answer": data["answer"]}
    return list(questions.values())


def answered_rows(transcript_path):
    """(id, data) for every answered row of every read this session made of a parking-lot page it published itself,
    in read order, leaving out any question this session ever wrote an answer to."""
    calls, own_pages, written, reads = {}, set(), set(), []
    if not transcript_path:
        return []
    import transcript_index
    root = transcript_index.default_cache_root()
    for record in transcript_index.candidate_records(transcript_path, ARTIFACT_NEEDLES, root):
        for block in tool_blocks(record):
            if block.get("type") == "tool_use" and block.get("name") in ("Artifact", "ArtifactData"):
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                calls[block.get("id")] = (block.get("name"), tool_input)
                if block.get("name") == "ArtifactData" and tool_input.get("action") in WRITE_ACTIONS:
                    for entry in write_entries(tool_input):
                        data = entry.get("data")
                        if entry.get("collection") == "questions" and (
                                entry.get("file_path") or not isinstance(data, dict)
                                or any(name in data for name in ANSWER_FIELDS)):
                            written.add(entry.get("doc_id"))
    for record in transcript_index.result_records(transcript_path, calls, root):
        for block in tool_blocks(record):
            if block.get("type") == "tool_result" and block.get("tool_use_id") in calls:
                name, tool_input = calls[block["tool_use_id"]]
                body = result_text(block)
                if block.get("is_error"):
                    continue
                if name == "Artifact" and os.path.basename(str(tool_input.get("file_path") or "")) == \
                        "parking-lot.html":
                    own_pages.update(url.rstrip("/") for url in RE_ARTIFACT_URL.findall(body))
                elif name == "ArtifactData" and tool_input.get("action") in READ_ACTIONS \
                        and tool_input.get("collection") == "questions":
                    reads.append((str(tool_input.get("url") or "").rstrip("/"), body))
    rows = []
    for url, body in reads:
        if url not in own_pages:
            continue
        for row in RE_STORE_ROW.findall(body):
            try:
                parsed = json.loads(row)
            except ValueError:
                continue
            data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
            if parsed.get("id") in written or data.get("status") != "answered" or not text(data.get("answer")):
                continue
            rows.append((str(parsed.get("id")), data))
    return rows


def main():
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    decision, reason = evaluate(payload.get("tool_name"), payload.get("tool_input"),
                                session_id=payload.get("session_id"))
    if decision == DENY:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": DENY,
                                                            "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
