# GATE-STEP: Session-start harness
# GATE-LISTING: os.walk(FIXTURES) | none
"""Regression harness for Invoke-SessionStart.ps1. Exits non-zero on any regression.

    python Harness/Hooks/test_session_start.py

Runs the real script as the SessionStart hook runs it, once per source the event fires for, and asserts the
Stop-gate rules card reaches the context whole. A Stop block names only the check that failed, so this card is
where the rules live; a source that dropped it would leave that session judged by rules it never saw. The
must-warn control points the script at a card that does not exist.

The Claude temp root rows run the script against fixture roots, never the live one: a missing root is created
with its inherited ACL cut, a root carrying inherited entries is re-secured, a secured root is left alone, no
root set (the workspace's default) secures nothing and names the account's own temp folder, and a root that
cannot be created warns the operator. The ACL is read back with
icacls, so the check sees what Windows enforces, not what the script reports.

The held-handle rows open one copy's file with delete sharing, the handle a scanner or OneDrive holds, while the
hook runs: that copy must be reported deferred with its files whole, the other copies must still migrate and the
BLOCKED notice must still reach the operator, and once the handle is closed the next start must link it.

The answered-question rows hand the script a fixture transcript: after a compaction the operator's page answer must
come back verbatim, an answer the session wrote itself or read from a page it did not publish must not, and neither a
startup nor a compaction with nothing answered adds anything.
"""

import _winapi
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import claude_temp  # noqa: E402
import session_home  # noqa: E402

SCRIPT = os.path.join(HERE, "Invoke-SessionStart.ps1")
CARD = os.path.join(HERE, "stop-gate-rules.txt")
SOURCES = ("startup", "resume", "clear", "compact")
GENERIC_READ = 0x80000000
FILE_SHARE_READ_WRITE_DELETE = 0x7
OPEN_EXISTING = 3


FIXTURES = os.path.realpath(tempfile.mkdtemp(prefix="session-start-", dir=claude_temp.scratch_dir()))
EMPTY_SKILLS = os.path.join(FIXTURES, "empty-source")
SCRATCH_LINKS = os.path.join(FIXTURES, "scratch-links")
LEGACY_LINKS = os.path.join(FIXTURES, "legacy-links")
LEGACY_REPORT = "~\\.claude\\skills still holds "
OUTSIDE_CONTEXT_LIMIT = 1000




def run(source, extra=(), skills=(EMPTY_SKILLS, SCRATCH_LINKS), temp_root="", transcript=None, env=None, cwd=None):
    """Every row but the sync rows passes an empty -ToolsCheckout, so no run touches the live Tools checkout."""
    hook_input = {"hook_event_name": "SessionStart", "source": source}
    if transcript:
        hook_input["transcript_path"] = transcript
    if cwd:
        hook_input["cwd"] = cwd
    payload = json.dumps(hook_input).encode("utf-8")
    argv = ["pwsh", "-File", SCRIPT, "-SkillSource", skills[0], "-SkillDestination", skills[1],
            "-ClaudeTempRoot", temp_root] + list(extra)
    if "-LegacySkillRoot" not in extra:
        argv += ["-LegacySkillRoot", LEGACY_LINKS]
    if "-ToolsCheckout" not in extra:
        argv += ["-ToolsCheckout", ""]
    result = subprocess.run(argv, input=payload, capture_output=True, env=env)
    text = result.stdout.decode("utf-8", "replace").strip()
    return result.returncode, json.loads(text) if text else None, result.stderr.decode("utf-8", "replace")


def acl_lines(path):
    """icacls' entries for the directory itself, each as `<account>:(flags)`: what Windows enforces, read back after
    the script ran. icacls prints the path before the first entry, and an account name can hold a space (NT
    AUTHORITY\\SYSTEM), so the path is cut off by its own text, never by splitting on spaces."""
    out = subprocess.run(["icacls", path], capture_output=True).stdout.decode("utf-8", "replace")
    lines = []
    for line in out.splitlines():
        if path.lower() in line.lower():
            line = line[line.lower().index(path.lower()) + len(path):]
        line = line.strip()
        if ":(" in line:
            lines.append(line)
    return lines


def secured(path):
    """Every entry explicit (none inherited), full control with inheritance to children, and nobody but the
    running account, Administrators and SYSTEM."""
    lines = acl_lines(path)
    account = os.environ.get("USERNAME", "").lower()
    names = [line.split(":(", 1)[0].lower() for line in lines]
    return (bool(lines) and all("(I)" not in line and "(OI)(CI)(F)" in line for line in lines)
            and "nt authority\\system" in names and "builtin\\administrators" in names
            and all(name in ("nt authority\\system", "builtin\\administrators") or name.endswith("\\" + account)
                    for name in names))


def temp_root_rows():
    """The fixture roots sit under a parent carrying an inheritable BUILTIN\\Users grant, the shape ProgramData
    hands a new folder, so a child shows inherited entries whatever ACL the temp folder itself has (a Remote
    Desktop session's Temp\\<n> hands its children no inherited entries at all)."""
    parent = os.path.join(FIXTURES, "temp-root-parent")
    os.makedirs(parent)
    subprocess.run(["icacls", parent, "/grant", "*S-1-5-32-545:(OI)(CI)(RX)"], capture_output=True, check=True)
    fresh = os.path.join(parent, "fresh")
    inherited = os.path.join(parent, "inherited")
    os.makedirs(inherited)
    blocked_parent = os.path.join(FIXTURES, "a-file")
    with open(blocked_parent, "w", encoding="utf-8") as handle:
        handle.write("not a directory")
    before = acl_lines(inherited)
    first = run("startup", temp_root=fresh)
    second = run("startup", temp_root=fresh)
    fixed = run("startup", temp_root=inherited)
    return [
        ("must-create: a missing temp root is created with its inherited ACL cut",
         lambda: first, lambda r: r[0] == 0 and "Created the Claude temp root" in context(r)
         and "systemMessage" not in r[1] and secured(fresh)),
        ("control: the inherited fixture carries BUILTIN\\Users by inheritance before the script runs",
         lambda: before, lambda lines: any("(I)" in line and line.lower().startswith("builtin\\users:") for line in lines)),
        ("must-fix: a root carrying inherited entries is re-secured",
         lambda: fixed, lambda r: r[0] == 0 and "Re-secured the Claude temp root" in context(r) and secured(inherited)),
        ("control: a secured root is left alone and reported secured",
         lambda: second, lambda r: r[0] == 0 and "is secured to this account" in context(r)
         and "Created" not in context(r) and "Re-secured" not in context(r) and secured(fresh)),
        ("control: no root set secures nothing and names the account's own temp folder",
         lambda: run("startup", temp_root=""), lambda r: r[0] == 0 and "CLAUDE_CODE_TMPDIR is not set" in context(r)
         and "the account's own temp folder" in context(r) and "Claude temp root" not in context(r)),
        ("must-warn: a root that cannot be created tells the operator and the session still starts",
         lambda: run("startup", temp_root=os.path.join(blocked_parent, "root")),
         lambda r: r[0] == 0 and "was not secured" in r[1]["systemMessage"] and "WARNING: the Claude temp root" in context(r)),
    ]


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "core.fsmonitor=false", "-c", "user.name=harness",
                    "-c", "user.email=harness@example.invalid"] + list(args), check=True, capture_output=True)


def sync_rows():
    """The hook brings its Tools checkout to origin before reading it: a clone one commit behind is fast-forwarded and
    says so, a current one says it is current, and a folder git cannot read is reported to the operator while the
    session still starts. Each run reads a fixture TEMP, so the slot of the gate running this harness is not seen."""
    root = os.path.join(FIXTURES, "sync")
    origin = os.path.join(root, "origin.git")
    seed = os.path.join(root, "seed")
    clone = os.path.join(root, "Tools")
    os.makedirs(seed)
    subprocess.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", origin], check=True, capture_output=True)
    git(seed, "init", "--quiet", "--initial-branch=main")
    write_bytes(os.path.join(seed, "a.txt"), b"one\n")
    git(seed, "add", "--all")
    git(seed, "commit", "--quiet", "--message", "first")
    git(seed, "remote", "add", "origin", origin)
    git(seed, "push", "--quiet", "origin", "main")
    subprocess.run(["git", "clone", "--quiet", origin, clone], check=True, capture_output=True)
    git(clone, "config", "core.fsmonitor", "false")
    write_bytes(os.path.join(seed, "a.txt"), b"two\n")
    git(seed, "commit", "--quiet", "--all", "--message", "second")
    git(seed, "push", "--quiet", "origin", "main")
    temp = os.path.join(root, "temp")
    os.makedirs(temp)
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CODE_TMPDIR"}
    env.update({"TEMP": temp, "TMP": temp})
    head = lambda: subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    origin_head = subprocess.run(["git", "-C", origin, "rev-parse", "main"], capture_output=True, text=True).stdout.strip()
    behind = run("startup", extra=("-ToolsCheckout", clone), env=env)
    current = run("startup", extra=("-ToolsCheckout", clone), env=env)
    unreadable = run("startup", extra=("-ToolsCheckout", os.path.join(root, "not-a-repo")), env=env)
    return [
        ("must-advance: a Tools checkout one commit behind origin is fast-forwarded before it is read, and says so",
         lambda: behind, lambda r: r[0] == 0 and "Fast-forwarded Tools at " + clone in context(r) and head() == origin_head),
        ("control: a current checkout is reported current and left alone",
         lambda: current, lambda r: r[0] == 0 and "is current with origin/main" in context(r)
         and "Fast-forwarded" not in context(r)),
        ("must-warn: a checkout git cannot read is reported to the operator and the session still starts",
         lambda: unreadable, lambda r: r[0] == 0 and "was not brought to origin" in r[1].get("systemMessage", "")
         and any(line.startswith("OPERATOR ACTION: Tools at ") for line in context(r).splitlines())
         and "never proof that what it names does not exist" in context(r)),
    ]


def board_rows():
    """The board's URL is put in context from coordination.json; a missing file is a stale checkout, never no board."""
    root = os.path.join(FIXTURES, "board")
    os.makedirs(root)
    named = os.path.join(root, "named.json")
    write_bytes(named, b'{"boardUrl": "https://claude.ai/artifact/FixtureBoard1"}')
    unnamed = os.path.join(root, "unnamed.json")
    write_bytes(unnamed, b'{"sessionsCollection": "sessions"}')
    return [
        ("must-carry: the board URL in coordination.json reaches the context",
         lambda: run("startup", extra=("-CoordinationPath", named)),
         lambda r: r[0] == 0 and "Coordination board: https://claude.ai/artifact/FixtureBoard1" in context(r)),
        ("must-warn: a missing coordination.json reads as a stale checkout, never as no board",
         lambda: run("startup", extra=("-CoordinationPath", os.path.join(root, "absent.json"))),
         lambda r: r[0] == 0 and "is missing, so this Tools checkout is older than the coordination board" in context(r)
         and "Coordination board: " not in context(r)),
        ("must-warn: a coordination.json naming no board says the URL is unknown, not that no board exists",
         lambda: run("startup", extra=("-CoordinationPath", unnamed)),
         lambda r: r[0] == 0 and "it is not evidence that no board exists" in context(r)),
    ]


def context(result):
    return result[1]["hookSpecificOutput"]["additionalContext"] if result[1] else ""


def is_junction(path):
    spelled = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
    return os.path.isdir(path) and os.path.realpath(path) != spelled


def remove_fixtures():
    subprocess.run(["icacls", FIXTURES, "/reset", "/T", "/Q"], capture_output=True)
    for directory, subdirectories, _ in os.walk(FIXTURES):
        for name in list(subdirectories):
            path = os.path.join(directory, name)
            if is_junction(path):
                os.rmdir(path)
                subdirectories.remove(name)
    shutil.rmtree(FIXTURES, onerror=lambda function, path, _: (os.chmod(path, stat.S_IWRITE), function(path)))


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def skill_link_rows():
    """A copy is what OneDrive delivers when another machine's junction synced its files first: delta matches the
    source exactly, epsilon differs only in line endings (an LF copy of a CRLF checkout), and both must become
    junctions with the source left whole. beta holds a file the source lacks and zeta a file whose content differs;
    both must stay real directories with their own bytes and reach the operator, since removing them would lose data."""
    source = os.path.join(FIXTURES, "skills")
    links = os.path.join(FIXTURES, "links")
    for name in ("alpha", "beta", "gamma"):
        write_bytes(os.path.join(source, name, "SKILL.md"), name.encode("utf-8"))
    write_bytes(os.path.join(source, "delta", "SKILL.md"), b"delta body\r\n")
    write_bytes(os.path.join(links, "delta", "SKILL.md"), b"delta body\r\n")
    write_bytes(os.path.join(source, "epsilon", "SKILL.md"), b"first\r\nsecond\r\n")
    write_bytes(os.path.join(links, "epsilon", "SKILL.md"), b"first\nsecond\n")
    write_bytes(os.path.join(source, "eta", "scripts", "tool.py"), b"print('eta')\r\n")
    write_bytes(os.path.join(links, "eta", "scripts", "tool.py"), b"print('eta')\r\n")
    write_bytes(os.path.join(links, "eta", "scripts", "__pycache__", "tool.cpython-314.pyc"), b"\x00bytecode")
    write_bytes(os.path.join(source, "zeta", "SKILL.md"), b"repo text")
    write_bytes(os.path.join(links, "zeta", "SKILL.md"), b"edited only in the copy")
    real = os.path.join(links, "beta")
    write_bytes(os.path.join(real, "only-here.md"), b"a file the repo does not have")
    write_bytes(os.path.join(links, "omega", "SKILL.md"), b"a deployed skill with no source")
    elsewhere = os.path.join(FIXTURES, "elsewhere")
    os.makedirs(elsewhere)
    subprocess.run(["cmd", "/c", "mklink", "/J", os.path.join(links, "gamma"), elsewhere], capture_output=True, check=True)
    first = run("startup", skills=(source, links))
    second = run("startup", skills=(source, links))
    alpha = os.path.join(links, "alpha")
    delta = os.path.join(links, "delta")
    epsilon = os.path.join(links, "epsilon")
    zeta = os.path.join(links, "zeta")
    return [
        ("must-link: a skill with no entry gets a junction to its source",
         lambda: first, lambda r: r[0] == 0 and is_junction(alpha)
         and os.path.realpath(alpha) == os.path.realpath(os.path.join(source, "alpha"))
         and "Linked the new skill alpha" in context(r)),
        ("must-migrate: a copy holding only the source's files becomes a junction and the source keeps them",
         lambda: first, lambda r: r[0] == 0 and is_junction(delta)
         and os.path.realpath(delta) == os.path.realpath(os.path.join(source, "delta"))
         and read_bytes(os.path.join(source, "delta", "SKILL.md")) == b"delta body\r\n"
         and "migrate delta" in context(r)),
        ("must-migrate: an LF copy of a CRLF source becomes a junction",
         lambda: first, lambda r: r[0] == 0 and is_junction(epsilon)
         and read_bytes(os.path.join(source, "epsilon", "SKILL.md")) == b"first\r\nsecond\r\n"
         and "migrate epsilon" in context(r)),
        ("must-migrate: Python bytecode under __pycache__ alone does not keep a copy from becoming a junction",
         lambda: first, lambda r: r[0] == 0 and is_junction(os.path.join(links, "eta"))
         and read_bytes(os.path.join(source, "eta", "scripts", "tool.py")) == b"print('eta')\r\n"
         and "migrate eta" in context(r)),
        ("must-not-touch: a copy holding a file the source lacks keeps it and the operator is told",
         lambda: first, lambda r: r[0] == 0 and "beta" in r[1].get("systemMessage", "")
         and "only-here.md" in r[1].get("systemMessage", "")
         and not is_junction(real) and read_bytes(os.path.join(real, "only-here.md")) == b"a file the repo does not have"),
        ("must-not-touch: a copy whose file differs in content keeps its bytes and the operator is told",
         lambda: first, lambda r: r[0] == 0 and "zeta" in r[1].get("systemMessage", "")
         and not is_junction(zeta) and read_bytes(os.path.join(zeta, "SKILL.md")) == b"edited only in the copy"
         and read_bytes(os.path.join(source, "zeta", "SKILL.md")) == b"repo text"),
        ("control: the session-start report omits the stray-directory listing a full deploy prints for omega",
         lambda: first, lambda r: "Present in the deployment" not in context(r) and "omega" not in context(r)
         and os.path.isfile(os.path.join(links, "omega", "SKILL.md"))),
        ("must-not-touch: a junction to another target is reported and left pointing there",
         lambda: first, lambda r: "gamma" in context(r) and "Left untouched" in context(r)
         and os.path.realpath(os.path.join(links, "gamma")) == os.path.realpath(elsewhere)),
        ("control: a second start links nothing new",
         lambda: second, lambda r: r[0] == 0 and "Linked the new skill" not in context(r) and is_junction(alpha)),
        ("a missing skill source is reported and the session still starts",
         lambda: run("startup", skills=(os.path.join(FIXTURES, "no-such-source"), links)),
         lambda r: r[0] == 0 and "Skill links were not checked" in context(r)),
        ("must-not-touch: a legacy ~/.claude/skills junction is reported and left in place, its target whole",
         lambda: legacy_start(source), lambda r: r[0] == 0 and LEGACY_REPORT + "alpha" in context(r)
         and "Retire-UserSkillLinks.ps1" in context(r) and is_junction(os.path.join(LEGACY_LINKS, "alpha"))
         and os.path.isfile(os.path.join(source, "alpha", "SKILL.md"))
         and "OPERATOR ACTION" not in context(r)),
        ("control: an empty legacy folder holding only synced reports nothing",
         lambda: legacy_start(source, only_synced=True), lambda r: r[0] == 0 and LEGACY_REPORT not in context(r)),
        ("must-warn: a session started in the Tools checkout is told to start in the crgolden workspace",
         lambda: run("startup", cwd=os.path.dirname(HERE)),
         lambda r: r[0] == 0 and "started in the Tools checkout" in r[1].get("systemMessage", "")
         and "OPERATOR ACTION" not in context(r)),
        ("control: a session started in the workspace is told nothing about its directory",
         lambda: run("startup", cwd=os.path.dirname(os.path.dirname(HERE))),
         lambda r: r[0] == 0 and "started in the Tools checkout" not in r[1].get("systemMessage", "")),
        ("must-shrink: a session started outside the workspace gets a short context without the rules card, so Claude "
         "Code saves nothing into ~/.claude/projects for that directory",
         lambda: run("startup", cwd=FIXTURES),
         lambda r: r[0] == 0 and "started outside the crgolden workspace" in context(r)
         and len(context(r)) < OUTSIDE_CONTEXT_LIMIT and "# Stop gate rules" not in context(r)),
        ("control: a session started in a repo under the workspace still gets the rules card",
         lambda: run("startup", cwd=os.path.dirname(HERE)),
         lambda r: r[0] == 0 and "# Stop gate rules" in context(r)
         and "started outside the crgolden workspace" not in context(r)),
    ]


def legacy_start(source, only_synced=False):
    root = os.path.join(FIXTURES, "legacy-synced" if only_synced else "legacy-links")
    os.makedirs(os.path.join(root, "synced"), exist_ok=True)
    alpha = os.path.join(root, "alpha")
    if not only_synced and not os.path.lexists(alpha):
        subprocess.run(["cmd", "/c", "mklink", "/J", alpha, os.path.join(source, "alpha")], capture_output=True, check=True)
    return run("startup", extra=("-LegacySkillRoot", root), skills=(source, os.path.join(FIXTURES, "legacy-dest")))


def held_handle_rows():
    source = os.path.join(FIXTURES, "held-skills")
    links = os.path.join(FIXTURES, "held-links")
    for name in ("kappa", "held"):
        write_bytes(os.path.join(source, name, "SKILL.md"), name.encode("utf-8"))
        write_bytes(os.path.join(links, name, "SKILL.md"), name.encode("utf-8"))
    write_bytes(os.path.join(source, "held", "scripts", "tool.py"), b"print('held')\r\n")
    write_bytes(os.path.join(links, "held", "scripts", "tool.py"), b"print('held')\r\n")
    pyc = os.path.join(links, "held", "scripts", "__pycache__", "tool.cpython-314.pyc")
    write_bytes(pyc, b"\x00bytecode")
    write_bytes(os.path.join(source, "lambda", "SKILL.md"), b"lambda")
    write_bytes(os.path.join(links, "lambda", "only-here.md"), b"a file the repo does not have")
    handle = _winapi.CreateFile(pyc, GENERIC_READ, FILE_SHARE_READ_WRITE_DELETE, 0, OPEN_EXISTING, 0, 0)
    try:
        while_held = run("startup", skills=(source, links))
    finally:
        _winapi.CloseHandle(handle)
    held = os.path.join(links, "held")
    held_kept_whole = (not is_junction(held) and os.path.isfile(pyc)
                       and read_bytes(os.path.join(held, "scripts", "tool.py")) == b"print('held')\r\n")
    kappa_linked = is_junction(os.path.join(links, "kappa"))
    after_release = run("startup", skills=(source, links))
    return [
        ("must-defer: a copy with a file held open is reported deferred and keeps every file",
         lambda: while_held, lambda r: r[0] == 0 and "DEFERRED held" in context(r) and held_kept_whole),
        ("must-continue: a held copy does not stop the other copies from migrating",
         lambda: while_held, lambda r: r[0] == 0 and "migrate kappa" in context(r) and kappa_linked
         and "Skill links were not checked" not in context(r)),
        ("must-continue: a held copy does not stop the BLOCKED notice reaching the operator",
         lambda: while_held, lambda r: r[0] == 0 and "only-here.md" in r[1].get("systemMessage", "")),
        ("control: once the handle is closed the next start links the deferred copy",
         lambda: after_release, lambda r: r[0] == 0 and is_junction(held) and "migrate held" in context(r)
         and os.path.realpath(held) == os.path.realpath(os.path.join(source, "held"))),
    ]


def answered_question_rows():
    """After a compaction the operator's page answers come back verbatim. The fixture transcript publishes its own page,
    reads back q1 (answered, with a non-ASCII answer) and q3 (answered, but this session wrote q3's answer itself),
    and reads q2 (answered) from a page it never published: only q1 may come back, and only on source compact."""
    own, other = "https://claude.ai/artifact/FixturePageOne", "https://claude.ai/artifact/SomeoneElsesPage"

    def row(doc_id, question, answer):
        return json.dumps({"id": doc_id, "data": {"question": question, "status": "answered", "answer": answer,
                                                  "answeredAt": "2026-10-10T15:00:00.000Z"}, "version": 2})

    def call(tool_id, name, tool_input, result):
        return [{"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": tool_id, "content": result}]}}]

    records = (call("t1", "Artifact", {"file_path": "C:\\fixture\\parking-lot.html"}, "Published at " + own)
               + call("t2", "ArtifactData", {"action": "set", "url": own, "collection": "questions", "doc_id": "q3-self",
                                             "data": {"answer": "an answer the session typed"}}, "ok")
               + call("t3", "ArtifactData", {"action": "query", "url": own, "collection": "questions"},
                      "\n".join([row("q1-color", "Which color?", "Blue, caf\u00e9 au lait"),
                                 row("q3-self", "Which size?", "an answer the session typed")]))
               + call("t4", "ArtifactData", {"action": "query", "url": other, "collection": "questions"},
                      row("q2-elsewhere", "Which shape?", "Round")))
    transcript = os.path.join(FIXTURES, "answered-transcript.jsonl")
    write_bytes(transcript, "\n".join(json.dumps(record) for record in records).encode("utf-8"))
    empty = os.path.join(FIXTURES, "unanswered-transcript.jsonl")
    write_bytes(empty, "\n".join(json.dumps(record) for record in records[:2]).encode("utf-8"))

    compacted = run("compact", transcript=transcript)
    return [
        ("must-restore: after a compaction the operator's page answer comes back verbatim with its question",
         lambda: compacted, lambda r: r[0] == 0 and "q1-color: Which color?" in context(r)
         and "Blue, caf\u00e9 au lait" in context(r) and "never ask any of these again" in context(r)),
        ("must-omit: an answer this session wrote itself is not restored",
         lambda: compacted, lambda r: r[0] == 0 and "q3-self" not in context(r)),
        ("must-omit: an answer read from a page this session did not publish is not restored",
         lambda: compacted, lambda r: r[0] == 0 and "q2-elsewhere" not in context(r) and "Round" not in context(r)),
        ("control: a startup with the same transcript restores nothing",
         lambda: run("startup", transcript=transcript), lambda r: r[0] == 0 and "q1-color" not in context(r)),
        ("control: a compaction with no answered question read back adds nothing and warns nothing",
         lambda: run("compact", transcript=empty), lambda r: r[0] == 0 and "never ask any of these again" not in context(r)
         and "were not restored" not in context(r)),
    ]


def main():
    try:
        return run_rows()
    finally:
        remove_fixtures()


def run_rows():
    started = time.time()
    failures = 0
    os.makedirs(EMPTY_SKILLS)
    with open(CARD, encoding="utf-8") as handle:
        card = handle.read().strip()
    rows = []
    for source in SOURCES:
        rows.append(("the card reaches the context whole on " + source, lambda s=source: run(s),
                     lambda r: r[0] == 0 and card in r[1]["hookSpecificOutput"]["additionalContext"]
                     and "systemMessage" not in r[1]))
    missing = os.path.join(tempfile.gettempdir(), "no-such-dir-session-start", "stop-gate-rules.txt")
    rows.append(("must-warn: a missing card tells the operator and the agent",
                 lambda: run("startup", ["-RulesCardPath", missing]),
                 lambda r: r[0] == 0 and "card" in r[1]["systemMessage"]
                 and "stop-gate-rules.txt" in r[1]["hookSpecificOutput"]["additionalContext"]
                 and card not in r[1]["hookSpecificOutput"]["additionalContext"]))
    skill_path = os.path.join(session_home.tools_checkout(), "Claude", "skills", "artifact-usage", "SKILL.md")
    with open(skill_path, encoding="utf-8") as handle:
        skill_body = handle.read().split("\n---\n", 1)[1].strip()
    for source in SOURCES:
        rows.append(("the artifact-usage skill reaches the context whole on " + source, lambda s=source: run(s),
                     lambda r: r[0] == 0 and skill_body in r[1]["hookSpecificOutput"]["additionalContext"].replace(
                         "\r\n", "\n") and "name: artifact-usage" not in r[1]["hookSpecificOutput"]["additionalContext"]))
    missing_skill = os.path.join(tempfile.gettempdir(), "no-such-dir-session-start", "SKILL.md")
    rows.append(("must-warn: a missing artifact-usage skill tells the operator and the agent",
                 lambda: run("startup", ["-ArtifactSkillPath", missing_skill]),
                 lambda r: r[0] == 0 and "artifact-usage" in r[1]["systemMessage"]
                 and skill_body not in r[1]["hookSpecificOutput"]["additionalContext"]))
    rows.append(("the machine type still reaches the context", lambda: run("startup"),
                 lambda r: r[0] == 0 and "AGENT_MACHINE_TYPE=" in r[1]["hookSpecificOutput"]["additionalContext"]))
    rows.extend(skill_link_rows())
    rows.extend(held_handle_rows())
    rows.extend(temp_root_rows())
    rows.extend(answered_question_rows())
    rows.extend(sync_rows())
    rows.extend(board_rows())
    for name, act, check in rows:
        try:
            result = act()
            ok = bool(check(result))
        except Exception as error:
            result, ok = "{0}: {1}".format(type(error).__name__, error), False
        print("{0}  {1}".format("PASS" if ok else "FAIL", name))
        if not ok:
            print("        got: {0!r}".format(result)[:600])
            failures += 1
    print("")
    print("{0} case(s) in {1:.2f}s".format(len(rows), time.time() - started))
    if failures == 0:
        print("All session-start cases passed.")
        return 0
    print("{0} case(s) failed.".format(failures))
    return 1


if __name__ == "__main__":
    sys.exit(main())
