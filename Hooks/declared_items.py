r"""Whether an open session-audit verdict names the declared work that holds it, judged one way for the audit and the
Stop gate alike.

The session-audit skill records a verdict per operator message in %TEMP%\claude\audits\session-audit\<session>\
verdicts.jsonl (append-only, the latest row per message id wins). An open one (outstanding, needs-operator,
needs-gate) is work the session owes; the Stop gate sees only the work-product declaration, so a row whose evidence
names no open declared item is work the gate cannot see, and a hand-off stop over it is a stop over undeclared work.
A row names an item when its evidence quotes DECLARED_ITEM_WORDS consecutive words of an open repos item or an open
standing item, at least one of them not a stopword.

Once the audit's page is published (page --record-url), a verdict newer than the last successful Artifact publish of
that page in the transcript is one the operator cannot see; stale_page reports it, reading only the transcript's tail
after that verdict.

Verified by Harness/Hooks/test_work_product_closure.py and the session-audit harness.
"""

import json
import os

import claude_temp
import session_home

DECLARED_OPEN = frozenset({"outstanding", "needs-operator", "needs-gate"})
DECLARED_ITEM_WORDS = 4
ITEM_WORD_PUNCTUATION = "\"'`.,;:!?()[]{}<>"
ITEM_STOPWORDS = frozenset({"a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it", "its",
                            "of", "on", "or", "so", "that", "the", "their", "this", "to", "with"})


def open_items(data):
    """The text of every open item of a declaration: each repos item and each standing item not done."""
    if not isinstance(data, dict):
        return []
    repos = data.get("repos") if isinstance(data.get("repos"), dict) else {}
    entries = [entry for items in repos.values() if isinstance(items, list) for entry in items]
    entries += data.get("standing") if isinstance(data.get("standing"), list) else []
    return [entry["item"] for entry in entries
            if isinstance(entry, dict) and entry.get("done") is not True and isinstance(entry.get("item"), str)]


def item_words(text):
    """The lowercased words of a text with punctuation stripped from each, so a trailing comma never splits a match."""
    return [word for word in (token.strip(ITEM_WORD_PUNCTUATION) for token in (text or "").lower().split()) if word]


def names_declared_item(evidence, items):
    """True when the evidence quotes DECLARED_ITEM_WORDS consecutive words of one of the items, and the quoted window
    holds a word that is not a stopword, so generic prose ("in the main checkout") names no item."""
    spoken = " {0} ".format(" ".join(item_words(evidence)))
    for item in items:
        tokens = item_words(item)
        size = min(DECLARED_ITEM_WORDS, len(tokens))
        for i in range(len(tokens) - size + 1 if size else 0):
            window = tokens[i:i + size]
            if any(word not in ITEM_STOPWORDS for word in window) and " {0} ".format(" ".join(window)) in spoken:
                return True
    return False


def audit_verdicts_path(session_id, root=None):
    return os.path.join(root or os.path.join(claude_temp.audits_root(), "session-audit"), str(session_id),
                        "verdicts.jsonl")


def latest_verdicts(path):
    """{message id: latest verdict row} from an audit's verdicts.jsonl; {} when the session was never audited here."""
    latest = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("id"), str):
                    latest[row["id"]] = row
    except OSError:
        return {}
    return latest


SESSION_AUDIT_SCRIPT = os.path.join(session_home.workspace_root(), "Tools", "Claude", "skills", "session-audit",
                                    "scripts", "session_audit.py")


def session_audit_module():
    """The session-audit script itself, so an operator message here is exactly what its catalog calls one. The
    skills stay in the workspace's Tools checkout, so the script is read there, beside this checkout."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("session_audit_for_hooks", SESSION_AUDIT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_jsonl(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return None
    return rows


def messages_after_sync(folder, audit):
    """[(timestamp, id)] for every operator message written to the chain's transcripts after the audit's synced
    offset, judged by the audit's own classify(), so none can hide in bytes the catalog has not reached."""
    try:
        with open(os.path.join(folder, "state.json"), "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return []
    found = []
    for name, entry in (state.get("files") or {}).items():
        path = os.path.expandvars(name)
        offset = entry.get("offset") if isinstance(entry, dict) else None
        if not isinstance(offset, int):
            continue
        try:
            with open(path, "rb") as handle:
                handle.seek(offset)
                for raw in handle:
                    if not any(marker in raw for marker in audit.LINE_PREFILTER):
                        continue
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    for kind, _channel, _text, _image, suffix in audit.classify(record):
                        if kind == "message":
                            found.append((str(record.get("timestamp") or ""), str(record.get("uuid") or "") + suffix))
        except OSError:
            continue
    return found


def unrecorded_messages(session_id, root=None):
    """[message id] for every operator message but the newest that the session's audit has no verdict for: one
    cataloged and never recorded, or one written after the last sync. [] when the session was never audited here.
    The newest is exempt: it is the ask the turn in hand answers."""
    folder = os.path.dirname(audit_verdicts_path(session_id, root))
    cataloged = read_jsonl(os.path.join(folder, "messages.jsonl"))
    if cataloged is None:
        return []
    verdicts = latest_verdicts(audit_verdicts_path(session_id, root))
    known = [(str(row.get("ts") or ""), row["id"]) for row in cataloged if isinstance(row.get("id"), str)]
    known_ids = {message_id for _, message_id in known}
    later = [(ts, message_id) for ts, message_id in messages_after_sync(folder, session_audit_module())
             if message_id not in known_ids]
    everything = sorted(known + later)
    newest = everything[-1][1] if everything else None
    missing = [message_id for _, message_id in known if message_id not in verdicts and message_id != newest]
    missing += [message_id for _, message_id in later if message_id != newest]
    return missing


AUDIT_PAGE_NAME = "session-audit.html"


def parse_time(text):
    """A timestamp as an aware datetime, from a verdict's "+00:00" or a transcript's "Z" spelling; None if unreadable."""
    import datetime
    try:
        stamp = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=datetime.timezone.utc)


TAIL_BLOCK = 4 * 1024 * 1024


def records_since(path, since):
    """Every JSON record of a transcript whose timestamp is at or after `since`, read backward from its end one block
    at a time until a record older than `since` shows the window is covered, so a long transcript is never read
    whole."""
    records = []
    try:
        with open(path, "rb") as handle:
            end, carry = os.path.getsize(path), b""
            while end > 0:
                start = max(0, end - TAIL_BLOCK)
                handle.seek(start)
                lines = (handle.read(end - start) + carry).split(b"\n")
                carry, lines = (lines[0], lines[1:]) if start else (b"", lines)
                end = start
                covered = False
                block = []
                for raw in lines:
                    try:
                        record = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    stamp = parse_time(record.get("timestamp")) if record.get("timestamp") else None
                    if stamp is not None and stamp < since:
                        covered = True
                        continue
                    block.append(record)
                records = block + records
                if covered:
                    break
    except OSError:
        return []
    return records


def page_publishes(folder, state, since):
    """[datetime] of every Artifact publish of the audit page at or after `since` that did not error, in the chain's
    transcripts: a publish to the recorded url or of the page file the `page` command writes."""
    url = state.get("page_url")
    page = os.path.normcase(os.path.normpath(os.path.join(folder, AUDIT_PAGE_NAME)))
    calls, failed = {}, set()
    for name in state.get("files") or {}:
        for record in records_since(os.path.expandvars(name), since):
            content = (record.get("message") or {}).get("content")
            for part in content if isinstance(content, list) else []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool_result" and part.get("is_error"):
                    failed.add(part.get("tool_use_id"))
                if part.get("type") != "tool_use" or part.get("name") != "Artifact":
                    continue
                args = part.get("input") if isinstance(part.get("input"), dict) else {}
                if args.get("action", "publish") != "publish" or args.get("asset"):
                    continue
                target = args.get("file_path")
                if (url and args.get("url") == url) or (
                        isinstance(target, str) and os.path.normcase(os.path.normpath(target)) == page):
                    calls[part.get("id")] = parse_time(record.get("timestamp"))
    return [stamp for call, stamp in calls.items() if call not in failed and stamp is not None]


def stale_page(session_id, root=None):
    """The newest verdict time when the audit's page was published once (page --record-url) and no successful
    publish is as new as that verdict, else None: the operator reads the page, so a verdict it does not show is a
    verdict the operator cannot see."""
    folder = os.path.dirname(audit_verdicts_path(session_id, root))
    try:
        with open(os.path.join(folder, "state.json"), "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or not state.get("page_url"):
        return None
    stamps = [parse_time(row.get("at")) for row in latest_verdicts(audit_verdicts_path(session_id, root)).values()]
    stamps = [stamp for stamp in stamps if stamp is not None]
    if not stamps:
        return None
    newest = max(stamps)
    if any(published >= newest for published in page_publishes(folder, state, newest)):
        return None
    return newest


def undeclared_open_rows(data, verdicts_path):
    """[(message id, status)] for every open audit verdict whose evidence names no open declared item."""
    items = open_items(data)
    return [(message_id, row.get("status")) for message_id, row in sorted(latest_verdicts(verdicts_path).items())
            if row.get("status") in DECLARED_OPEN and not names_declared_item(str(row.get("evidence") or ""), items)]


AUDIT_SYNC_COMMAND = ('python "<workspace>\\Tools\\Claude\\skills\\session-audit\\scripts\\session_audit.py" sync, then '
                      'reconcile, then record a verdict per message (the session-audit skill)')
NO_DECLARATION_PROBLEM = ("this session has no work-product declaration, so no audit can judge its asks against "
                          "declared work. Write {path} first (a read-only ask declares {{\"repos\": {{}}}}), then "
                          + AUDIT_SYNC_COMMAND + ".")
NEVER_AUDITED_PROBLEM = ("this session has never run its session audit, so nothing has checked that every operator "
                         "message was met. Run " + AUDIT_SYNC_COMMAND + ". A session whose catalog holds one message "
                         "owes no verdict yet: the newest message is the ask this turn answers, so sync and "
                         "reconcile alone satisfy this.")
UNRECONCILED_PROBLEM = ("the session audit's reconcile has not matched at its current sync, so its catalog is not "
                        "known to hold every operator message. Run session_audit.py reconcile (after sync).")
STALE_SYNC_PROBLEM = ("{count} operator message(s) were written after the session audit's last sync, so the audit "
                      "has not seen the ask this stop answers. Run session_audit.py sync, then reconcile, then "
                      "record a verdict for every message but the newest.")
EJECTS_PROBLEM = ("the Stop gate ended {count} of this session's turn(s) on a run of refused stops (eject:filler-"
                  "streak in decisions.log) that the session audit has not cataloged. Run session_audit.py sync: each "
                  "eject becomes a message that owes a verdict naming the explanation and the remedy.")
UNRECORDED_TOKEN_PROBLEM = ("{count} cataloged message(s) other than the newest have no audit verdict: {ids}. Record "
                            "one for each (session_audit.py record <id> --status ... --evidence ...); an ask "
                            "nobody checked was met is not one an honest stop can stand on.")
UNDECLARED_TOKEN_PROBLEM = ("the session audit holds {count} open verdict(s) whose evidence names no open declared "
                            "item: {rows}. Declare that work in {path}, or close the verdict with evidence.")
STALE_PAGE_TOKEN_PROBLEM = ("the session audit's page was published before its newest verdict ({at}): run "
                            "session_audit.py page and publish the file it names, so the operator reads the audit "
                            "this stop rests on.")
AUDIT_SKILL_OUTDATED_PROBLEM = ("the session-audit script at {path} has no eject catalog (session_ejects), so the "
                                "Stop gate's ejects cannot be audited: pull the Tools checkout that holds it.")


def read_audit_state(session_id, root=None):
    """The session audit's state.json as a dict, else None when the session was never audited here."""
    folder = os.path.dirname(audit_verdicts_path(session_id, root))
    try:
        with open(os.path.join(folder, "state.json"), "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def uncataloged_ejects(session_id, audit, root=None, decisions_log=None):
    """[timestamp] of every filler-streak eject decisions.log holds for this session that the audit catalog lacks:
    each is a turn the gate ended that no verdict has explained, and one the catalog lacks means sync has not run
    since it."""
    folder = os.path.dirname(audit_verdicts_path(session_id, root))
    cataloged = read_jsonl(os.path.join(folder, "messages.jsonl")) or []
    known = {row.get("id") for row in cataloged}
    return [stamp for stamp, _ in audit.session_ejects(session_id, decisions_log) if audit.eject_id(stamp) not in known]


def audit_required_problem(session_id, data, path, root=None, decisions_log=None):
    """Why a stop token that claims an honest stop (DONE:, QUEUED:, WAITING:, NEEDED:, a closing question, IDLE:)
    earns nothing yet, else None: the session's own audit, read from its cache and never from its narration, must
    show every operator message met. In order: a declaration exists; the audit has run; its reconcile matched at the
    current sync; no operator message was written after that sync; every eject the Stop gate logged is cataloged;
    every cataloged message but the newest has a verdict; no open verdict names undeclared work; the published page
    is as new as the newest verdict."""
    if data is None:
        return NO_DECLARATION_PROBLEM.format(path=path)
    state = read_audit_state(session_id, root)
    if state is None:
        return NEVER_AUDITED_PROBLEM
    audit = session_audit_module()
    if not hasattr(audit, "session_ejects") or not hasattr(audit, "eject_id"):
        return AUDIT_SKILL_OUTDATED_PROBLEM.format(path=SESSION_AUDIT_SCRIPT)
    if not audit.reconciled_now(state):
        return UNRECONCILED_PROBLEM
    folder = os.path.dirname(audit_verdicts_path(session_id, root))
    later = messages_after_sync(folder, audit)
    if later:
        return STALE_SYNC_PROBLEM.format(count=len(later))
    ejects = uncataloged_ejects(session_id, audit, root, decisions_log)
    if ejects:
        return EJECTS_PROBLEM.format(count=len(ejects))
    unrecorded = unrecorded_messages(session_id, root)
    if unrecorded:
        return UNRECORDED_TOKEN_PROBLEM.format(count=len(unrecorded), ids=", ".join(i[:8] for i in unrecorded[:5]))
    rows = undeclared_open_rows(data, audit_verdicts_path(session_id, root))
    if rows:
        shown = ", ".join("{0} [{1}]".format(message_id[:8], status) for message_id, status in rows[:5])
        return UNDECLARED_TOKEN_PROBLEM.format(count=len(rows), rows=shown, path=path)
    stale = stale_page(session_id, root)
    if stale:
        return STALE_PAGE_TOKEN_PROBLEM.format(at=stale.isoformat())
    return None
