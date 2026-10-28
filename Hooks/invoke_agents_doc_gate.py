"""PreToolUse gate: refuse the first edit in a repo until AGENTS/REPOS/<Repo>.md has been read.

WHY THIS EXISTS. AGENTS.md already says "open the one file for the repo you are working in", and it
loads into every session. On 2026-09-01 an agent edited Identity/Pages/Index.cshtml having only
GREPPED AGENTS/REPOS/Identity.md for unrelated keywords, got "No matches found", and treated that as
licence to skip it -- then spent several turns guessing a SQL server name and reconstructing a launch
URL that was printed on line 6 of the file it skipped. The operator's words: "when you open x app,
look for AGENTS/x.md - this is junior level 101 - why am I even having to explain this to you?"

An instruction in context did not work, and a memory entry asks the agent to remember. This asks
nothing. It is the same reasoning as the pwsh-flags branch in invoke_pretooluse_gate.py: convert a
cost the agent cannot see into an error it must react to.

A GREP DOES NOT COUNT, AND NEITHER DOES A PARTIAL READ. The whole failure mode was substituting a
keyword probe for reading the file. Chunked reading is legitimate, so the gate unions the line ranges
of every Read of the doc and requires them to cover it end to end; a Read with no limit and no offset
covers it outright. Rejecting `limit` outright was the first design and it was wrong in a way worth
recording: on the very session that produced this gate, the agent read AGENTS/REPOS/Identity.md as
lines 1-36, 36-95 and 95-169 of 231 and then told the operator it had read the file "end to end".
Coverage is what makes that claim checkable.

A COMPACTION RESETS THE CREDIT. Reading proves nothing once the reading has been summarised away, and
the transcript keeps the tool_use record either way. On 2026-09-08 this gate deferred every edit in
Identity on the strength of a full read at transcript line 9549 that two later compactions (lines
12580 and 17967) had discarded; the agent then reasoned from fragments and invented a PAR theory that
AGENTS/REPOS/Identity.md:81 exists specifically to debunk. A compacted session is a fresh agent wearing the
old transcript, so ranges recorded before the last `isCompactSummary` record are dropped. Re-reading
after a compaction restores the credit, which is what stops a long session locking itself out.

FAIL-OPEN, DELIBERATELY. If the transcript cannot be read or parsed the gate defers. Hooks here
already fail open on crash (see PRETOOLUSE-HOOK.md, "Failure behavior"), and a gate that hard-blocks
every Edit in the workspace when a transcript hiccups is worse than one that occasionally misses.
This is a real hole, it is pinned by the harness, and it should not be "fixed" by failing closed
without measuring how often the read fails first.

SCOPE. Only paths under <workspace>/<Repo>/ where AGENTS/REPOS/<Repo>.md actually exists are gated. Editing
AGENTS/ itself, Tools/ (which uses Tools/CLAUDE.md), the scratchpad, or anything outside the
workspace defers -- otherwise the gate would block the very file it is telling you to read.
"""

import json
import os
import sys

DENY = "deny"
DEFER = "defer"

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_home  # noqa: E402

WORKSPACE = session_home.workspace_root()

COMPACTION_KEY = "isCompactSummary"

REASON_TEMPLATE = (
    "Blocked: you are about to edit {path} but have not read AGENTS/REPOS/{repo}.md in this session. "
    "Read it in full first - Read the whole file, no limit and no offset. A grep is NOT a read: "
    "grepping for the wrong keyword returns 'No matches found', which says nothing about whether "
    "the file is useful, and treating that as permission to skip it is the exact failure this gate "
    "exists to stop. That file carries the repo's run command, its User Secrets id, its settled "
    "decisions and its do-not-re-derive list. Read {agents_doc} now, then retry this edit. Any "
    "change to this hook must be verified with Harness/Hooks/test_agents_doc_gate.py."
)


def split_repo(file_path, workspace_root):
    """Return the repo directory name for a path inside the workspace, else None.

    Returns None for a path outside the workspace and for a path that IS the workspace root, so a
    caller can treat None as "nothing to gate" without a second check.
    """
    if not file_path or not workspace_root:
        return None
    try:
        relative = os.path.relpath(os.path.abspath(file_path), workspace_root)
    except ValueError:
        # Different drive on Windows; relpath raises rather than returning something useless.
        return None
    if relative.startswith(os.pardir):
        return None
    head = relative.split(os.sep)[0]
    if not head or head == os.curdir:
        return None
    return head


def find_workspace_root(file_path, cwd, workspace=None):
    """The workspace (session_home.workspace_root()) when either of the two paths sits inside it, else None."""
    workspace = workspace or WORKSPACE
    for candidate in (file_path, cwd):
        if candidate and split_repo(candidate, workspace) is not None:
            return workspace
    return None


def read_entries(transcript_path, needles):
    """Yield parsed JSONL records whose raw line mentions any of `needles`.

    The substring test runs before json.loads on purpose: transcripts reach tens of megabytes and
    parsing every record per Edit would put this hook back in pwsh latency territory.
    """
    if isinstance(needles, str):
        needles = (needles,)
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not any(needle in line for needle in needles):
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except Exception:
        return


def is_compaction_boundary(record):
    """True for the record a context compaction writes, which is a top-level boolean, not a substring.

    Measured on a real transcript: four of these, each paired with a preceding `compactMetadata`
    system record carrying `trigger`/`preTokens`/`postTokens`.
    """
    return record.get(COMPACTION_KEY) is True


def iter_tool_uses(record):
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block


def count_lines(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return sum(1 for _ in handle)
    except Exception:
        return 0


def as_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def read_ranges(transcript_path, agents_doc):
    """Every (start, end) line range the agent's own Read calls covered SINCE THE LAST COMPACTION.

    Only assistant tool_use blocks are considered. A Read the OPERATOR issues on the agent's behalf
    is deliberately not counted -- the point is whether the agent read the file, and crediting it for
    someone else's read would let exactly the failure this gate exists to stop pass.

    Ranges before the last compaction are discarded; see the module docstring for why.
    """
    ranges = []
    basename = os.path.basename(agents_doc)
    for record in read_entries(transcript_path, (basename, COMPACTION_KEY)):
        if is_compaction_boundary(record):
            ranges = []
            continue
        for block in iter_tool_uses(record):
            if block.get("name") != "Read":
                continue
            tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                continue
            read_path = tool_input.get("file_path")
            if not isinstance(read_path, str):
                continue
            if os.path.normcase(os.path.abspath(read_path)) != os.path.normcase(agents_doc):
                continue
            limit = as_int(tool_input.get("limit"))
            offset = as_int(tool_input.get("offset"))
            if limit is None and offset is None:
                ranges = [(1, None)]
                continue
            start = offset if offset else 1
            end = None if limit is None else start + limit - 1
            ranges.append((start, end))
    return ranges


def has_full_read(transcript_path, agents_doc):
    """True when the agent's own Reads of `agents_doc` cover it from line 1 to the last line."""
    ranges = read_ranges(transcript_path, agents_doc)
    if not ranges:
        return False
    if any(end is None and start <= 1 for start, end in ranges):
        return True

    total = count_lines(agents_doc)
    if total <= 0:
        return False

    reached = 0
    # Sorting by start lets one pass close the gaps: a range that begins past the high-water mark
    # leaves a hole nothing later can fill, because every later range starts later still.
    for start, end in sorted(ranges, key=lambda span: (span[0], float("inf") if span[1] is None else span[1])):
        if start > reached + 1:
            return False
        if end is None:
            return True
        reached = max(reached, end)
        if reached >= total:
            return True
    return reached >= total


def evaluate(file_path, cwd, transcript_path, workspace=None):
    """The whole decision as data: (decision, reason). Split out so the harness can call it in-process."""
    workspace_root = find_workspace_root(file_path, cwd, workspace)
    if not workspace_root:
        return DEFER, ""

    repo = split_repo(file_path, workspace_root)
    if not repo:
        return DEFER, ""

    agents_doc = session_home.workspace_doc("AGENTS/REPOS/" + repo + ".md", workspace_root)
    if not agents_doc or not os.path.isfile(agents_doc):
        return DEFER, ""

    if not transcript_path or not os.path.isfile(transcript_path):
        return DEFER, ""

    if has_full_read(transcript_path, agents_doc):
        return DEFER, ""

    return DENY, REASON_TEMPLATE.format(path=file_path, repo=repo, agents_doc=agents_doc)


def main():
    raw = sys.stdin.read()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    tool_input = payload.get("tool_input")
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(file_path, str):
        return 0

    decision, reason = evaluate(
        file_path,
        payload.get("cwd"),
        payload.get("transcript_path"),
    )
    if decision != DEFER:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }, separators=(",", ":")))
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
