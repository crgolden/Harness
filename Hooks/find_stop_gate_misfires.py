"""Find stop-gate misfires from facts.log, with the evidence attached.

A misfire is a turn the gate blocked when the agent had in fact earned a stop. Until facts.log existed
this could only be argued by replaying the transcript, which is archaeology: the file has moved on, and
the replay shows what the gate WOULD see now rather than what it did see. facts.log records the size
the gate read and the first 80 characters it judged, so both halves are available and the check is
mechanical.

Two independent findings per blocked decision:

  REPLAY   -- cut the transcript at the size the gate recorded and re-run the real decide(). If it now
              allows, the block was a misfire. This is the same exact-offset method that found 45 of 99
              in session 94edf861; it agreed with the registered command on all 99.

  DRIFT    -- compare what the gate judged (`ahead=`) against the newest assistant text record that now
              ends at or before that same size. These must be the same record. If they differ, the file
              changed under the gate: at decision time the newer message either did not exist or did not
              read the same, which is the one-message-lag signature.

Usage:
    python find_stop_gate_misfires.py
    python find_stop_gate_misfires.py --facts <path> --projects <dir>   # for testing the detector
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile

TAIL = 8600000
HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import claude_temp  # noqa: E402
import session_home  # noqa: E402


def projects_dir(workspace, profile):
    """The folder Claude Code keeps a workspace's transcripts in: the launch path with every character that is
    not a letter or digit turned into '-'."""
    return os.path.join(profile, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", workspace))


def default_projects(profile, workspace=None):
    """The workspace's transcripts under `profile`. The workspace is session_home.workspace_root(), which resolves a
    worktree copy of this file to its main checkout's folder, never to the worktree's own."""
    return projects_dir(workspace or session_home.workspace_root(), profile)


def clear_readonly_and_retry(func, path, exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def refuse_acceptance_checks(command, **kwargs):
    raise OSError("a replay runs no acceptance check: it would judge today's live state, not the state at the stop")


def load_gate():
    spec = importlib.util.spec_from_file_location(
        "stop_gate", os.path.join(HERE, "invoke_stop_gate.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_rows(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            fields = {}
            for f in parts[3:]:
                if "=" in f:
                    key, value = f.split("=", 1)
                    fields[key] = value
            rows.append({"ts": parts[0], "session": parts[1], "signal": parts[2], "f": fields})
    return rows


def newest_assistant_text_before(path, limit):
    """The record read_facts would latch: newest assistant text ending at or before `limit`."""
    best = None
    pos = 0
    with open(path, "rb") as fh:
        for raw in fh:
            start = pos
            pos += len(raw)
            if pos > limit:
                break
            try:
                record = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            if not isinstance(record, dict) or record.get("type") != "assistant":
                continue
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text").strip()
            if text:
                best = {"end": pos, "ts": record.get("timestamp"), "text": text}
    return best


def one_line(text, width=80):
    return " ".join(str(text).split())[:width]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--facts", default=os.path.join(claude_temp.stop_gate_state_root(), "facts.log"))
    parser.add_argument("--projects", default=default_projects(os.environ.get("USERPROFILE", os.path.expanduser("~"))))
    parser.add_argument("--session", default=None, help="only this session id")
    parser.add_argument("--workspace", default=None,
                        help="the workspace whose work-product declarations a replayed DONE: is judged against")
    args = parser.parse_args()

    gate = load_gate()
    rows = read_rows(args.facts)
    if not rows:
        print("no facts.log rows at", args.facts)
        return 0

    blocks = [r for r in rows if r["signal"].startswith("block")]
    if args.session:
        blocks = [r for r in blocks if r["session"] == args.session]
    print("facts.log rows: {0}   blocked decisions: {1}".format(len(rows), len(blocks)))
    print("transcripts    :", args.projects)
    print()

    work = tempfile.mkdtemp(prefix="misfire-", dir=claude_temp.scratch_dir())
    replay_temp = os.path.join(work, "temp")
    misfires = 0
    drifted = 0
    checked = 0
    skipped = 0

    try:
        for row in blocks:
            name = row["f"].get("file", "")
            size = row["f"].get("size", "")
            path = os.path.join(args.projects, name)
            if not name or not size.isdigit() or not os.path.exists(path):
                skipped += 1
                continue
            size = int(size)
            if os.path.getsize(path) < size:
                skipped += 1
                continue
            checked += 1

            begin = max(0, size - TAIL)
            cut = os.path.join(work, os.path.basename(name))
            with open(path, "rb") as src, open(cut, "wb") as out:
                src.seek(begin)
                out.write(src.read(size - begin))

            state = os.path.join(work, "state")
            if os.path.exists(state):
                shutil.rmtree(state, onexc=clear_readonly_and_retry)
            # No payload text on a replay, so this exercises the transcript fallback deliberately:
            # the point is to re-judge what the gate saw at the time. Record ages are ancient by
            # construction here and no longer mean anything to the gate.
            signal, output = gate.decide({"session_id": "misfire-check"}, cut, state, 100,
                                         gate_workspace=args.workspace, temp_root=replay_temp,
                                         acceptance_runner=refuse_acceptance_checks)

            judged = row["f"].get("ahead", "")
            latched = newest_assistant_text_before(path, size)
            latched_head = one_line(latched["text"]) if latched else ""
            drift = bool(judged) and bool(latched_head) and not latched_head.startswith(judged[:60])

            if not output or drift:
                if not output:
                    misfires += 1
                if drift:
                    drifted += 1
                print("{0}  {1}  {2}".format(row["ts"][:23], row["session"][:36], row["signal"]))
                print("    replay now says : {0}{1}".format(
                    signal, "   <<< MISFIRE" if not output else ""))
                print("    gate judged     : {0!r}".format(judged))
                print("    file now holds  : {0!r}".format(latched_head))
                if drift:
                    print("    <<< DRIFT: the record at that offset is not what the gate read")
                print("    transcript      : {0} at {1} bytes".format(name, size))
                print("    age at decision : {0}".format(row["f"].get("age", "?")))
                print()
    finally:
        shutil.rmtree(work, onexc=clear_readonly_and_retry)

    print("checked {0} blocked decision(s), skipped {1}".format(checked, skipped))
    print("  replay as allow (misfires): {0}".format(misfires))
    print("  judged text drifted       : {0}".format(drifted))
    if blocks and not checked:
        print("  NOTHING CHECKED -- every blocked decision was skipped; is --projects the right folder?")
    elif not misfires and not drifted:
        print("  nothing to report -- every block still blocks, on the bytes the gate actually read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
