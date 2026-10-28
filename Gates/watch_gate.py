"""Follow a gate through its record and print one line per event, for Claude Code's Monitor tool.

GateCommon.ps1 rewrites <Repo>.<COMPUTERNAME>.last.json when each step starts and after each row, so this reads the
record instead of a process id or a log. It prints a line when a step starts and one per finished row, and it ends on
every terminal state: the final verdict (exit 0 for PASSED, 1 otherwise), or a RUNNING record whose gate process is
gone (exit 2), so a killed gate is reported rather than waited on. A record that finished before the watcher started
belongs to an earlier run and is ignored until a new run writes it. Works for any gate on this machine, an interactive
one or the triage runner's, since both write the same record.

    python watch_gate.py <Repo>                       the repo's record in its own state directory
    python watch_gate.py --record <path to .last.json>
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate_status  # noqa: E402

POLL_SECONDS = 2.0
process_alive = gate_status.process_alive


def read(path: str):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def parse_time(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def events(record: dict, seen_rows: int, seen_step):
    """The lines a record adds since `seen_rows` rows and step `seen_step`, and the new counters."""
    lines = []
    rows = [row for row in record.get("steps") or [] if isinstance(row, dict)]
    for row in rows[seen_rows:]:
        detail = f"  {row.get('detail')}" if row.get("detail") else ""
        lines.append(f"{row.get('name')}  {row.get('verdict')}{detail}")
    current = record.get("currentStep")
    if current and current != seen_step:
        lines.append(f"running {current}")
    return lines, len(rows), current


def terminal(record: dict, started: datetime, alive=process_alive):
    """(line, exit code) when the record ends the watch, else None."""
    verdict = record.get(gate_status.FIELD_VERDICT)
    if verdict != gate_status.VERDICT_RUNNING:
        completed = parse_time(record.get("completedAt"))
        if completed is None or completed < started:
            return None
        return f"GATE: {verdict}", 0 if verdict == gate_status.VERDICT_PASSED else 1
    if not alive(record.get("pid")):
        return f"GATE: STOPPED (process {record.get('pid')} is gone and its record never completed)", 2
    return None


def watch(path: str, interval: float = POLL_SECONDS, alive=process_alive, out=print, sleep=time.sleep) -> int:
    started = datetime.now(timezone.utc)
    seen_rows, seen_step, waiting = 0, None, False
    while True:
        record = read(path)
        if record is None or (record.get(gate_status.FIELD_VERDICT) != gate_status.VERDICT_RUNNING
                              and terminal(record, started, alive) is None):
            if not waiting:
                out(f"waiting for a gate run to write {path}")
                waiting = True
            sleep(interval)
            continue
        lines, seen_rows, seen_step = events(record, seen_rows, seen_step)
        for line in lines:
            out(line)
        ended = terminal(record, started, alive)
        if ended:
            out(ended[0])
            return ended[1]
        sleep(interval)


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--record":
        path = argv[1]
    elif len(argv) == 1 and not argv[0].startswith("--"):
        path = gate_status.record_path(argv[0], gate_status.repo_state_dir(argv[0]))
    else:
        print(__doc__)
        return 64
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    return watch(path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
