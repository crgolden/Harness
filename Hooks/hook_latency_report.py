"""Report what the Claude Code hooks actually cost in production, from session transcripts.

Measure-HookLatency.ps1 is the synthetic counterpart: it benches the registered hooks of one tool call, idle
and with every core busy. Neither of its numbers is the one that matters. Hooks time
out under real load -- a build, a test run, AV, OneDrive/Syncthing -- and a timed-out hook emits nothing,
so the action proceeds and the failure is silent. The only record is the transcript.

This reads the hook attachment records Claude Code writes per invocation:

    {"type":"attachment","attachment":{"type":"hook_success"|"hook_cancelled"|...,
      "hookName":..., "hookEvent":..., "durationMs":..., "timedOut":true, "timeoutMs":...}}

`timedOut` separates a hook that hit its execution budget from a user Esc cancellation, which lacks the
field and says nothing about speed.

**`durationMs` on a timed-out record is not execution time.** It is wall-clock until the cancellation was
recorded, and it overruns the enforced budget badly -- every record over 60s in this workspace's history
was a `timedOut` hook against a 30000ms budget, one of them at 320859ms, which is a 30s timeout whose
record landed five minutes later (a suspend, most likely). Summing those durations as "time blocked"
overstates it by design. So timings are reported over SUCCESSFUL runs only, and a timeout contributes its
budget, not its recorded duration.

**A window's total hides when its cost fell.** One bad stretch (a stalled machine, a hook that has since been
fixed) dominates a multi-day sum and reads as the present. `--by-day` prints each day's recorded runs, timeouts
and blocked minutes per hook, so the current state is read from the newest days, not from the total.

Usage:
    python Harness/Hooks/hook_latency_report.py [--days N] [--project SLUG] [--by-day]
"""

import argparse
import json
import os
import statistics
import time
from collections import defaultdict


def transcript_files(root, project, days):
    cutoff = time.time() - days * 86400
    pattern_root = os.path.join(root, project) if project else root
    for dirpath, _dirnames, filenames in os.walk(pattern_root):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(path) >= cutoff:
                    yield path
            except OSError:
                continue


def collect(paths, daily=None):
    stats = defaultdict(lambda: {"durations": [], "timeouts": 0, "budget": None, "runs": 0})
    tool_calls = defaultdict(int)
    seen = set()
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"tool_use"' in line and '"assistant"' in line:
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        for block in (record.get("message") or {}).get("content") or []:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_calls[block.get("name") or "?"] += 1
                        continue
                    if '"attachment"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("type") != "attachment":
                        continue
                    attachment = record.get("attachment") or {}
                    kind = attachment.get("type") or ""
                    if not kind.startswith("hook_"):
                        continue
                    # A resumed session copies the records it resumed, so the same record can appear twice.
                    if record.get("uuid") in seen:
                        continue
                    seen.add(record.get("uuid"))
                    key = (attachment.get("hookEvent") or "?", attachment.get("hookName") or "?")
                    entries = [stats[key]]
                    if daily is not None:
                        entries.append(daily[((record.get("timestamp") or "?")[:10],) + key])
                    timed_out = kind == "hook_cancelled" and bool(attachment.get("timedOut"))
                    duration = attachment.get("durationMs")
                    for entry in entries:
                        entry["runs"] += 1
                        if timed_out:
                            entry["timeouts"] += 1
                            budget = attachment.get("timeoutMs")
                            if isinstance(budget, (int, float)):
                                entry["budget"] = budget
                        elif isinstance(duration, (int, float)) and duration > 0:
                            # Successful runs only: a timed-out record's duration is wall-clock until the
                            # cancellation was logged, not what the hook actually cost.
                            entry["durations"].append(duration)
        except OSError:
            continue
    # A PreToolUse hook on Edit or Write leaves a transcript record only when it is cancelled or
    # errors; a successful run leaves none. Its run count is therefore the tool-call count, and the
    # timeout rate is timeouts over calls, not over records.
    for key, entry in stats.items():
        event, name = key
        if event != "PreToolUse" or not name.startswith("PreToolUse:"):
            continue
        tool = name.split(":", 1)[1]
        calls = tool_calls.get(tool, 0)
        if calls > entry["runs"]:
            entry["runs"] = calls
            entry["unrecorded_successes"] = calls - entry["timeouts"] - len(entry["durations"])
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--project", default=None,
                        help="project slug under ~/.claude/projects (default: all)")
    parser.add_argument("--by-day", action="store_true",
                        help="also print each day's recorded runs, timeouts and blocked minutes per hook")
    args = parser.parse_args()

    root = os.path.expanduser("~/.claude/projects")
    paths = list(transcript_files(root, args.project, args.days))
    daily = defaultdict(lambda: {"durations": [], "timeouts": 0, "budget": None, "runs": 0}) if args.by_day else None
    stats = collect(paths, daily)

    print(f"transcripts scanned: {len(paths)}  window: {args.days}d")
    if not stats:
        print("no hook records found")
        return

    print()
    header = "{:<50} {:>7} {:>9} {:>9} {:>9} {:>8} {:>11}"
    print("timings are over SUCCESSFUL runs; a timeout contributes its budget, not its logged duration")
    print()
    print(header.format("event / hook", "runs", "median", "p90", "max", "timeout", "blocked"))
    print("-" * 108)

    total_blocked = 0.0
    for key in sorted(stats, key=lambda k: -stats[k]["runs"]):
        entry = stats[key]
        durations = sorted(entry["durations"])
        runs = entry["runs"]
        timeouts = entry["timeouts"]
        if durations:
            median = statistics.median(durations)
            p90 = durations[min(len(durations) - 1, int(len(durations) * 0.9))]
            worst = durations[-1]
        else:
            median = p90 = worst = 0
        # A timeout blocks for its budget, not for its inflated recorded duration.
        budget = entry["budget"] if isinstance(entry["budget"], (int, float)) else 30000
        blocked = (sum(durations) + timeouts * budget) / 1000.0
        total_blocked += blocked
        rate = (timeouts / runs * 100.0) if runs else 0.0
        label = f"{key[0]} / {key[1]}"
        unrecorded = entry.get("unrecorded_successes")
        if unrecorded:
            label += f" ({unrecorded} unrecorded)"
        print(header.format(
            label[:50], runs, f"{median:.0f}ms" if durations else "-", f"{p90:.0f}ms" if durations else "-",
            f"{worst:.0f}ms" if durations else "-", f"{rate:.1f}%", f"{blocked/60:.1f}min"))

    print("-" * 108)
    print(f"total time blocked in hooks across the window: {total_blocked/3600:.2f} hours")
    if daily is not None:
        print()
        print("by day: recorded runs only (see the note below); blocked counts a timeout at its budget")
        print("{:<11} {:<46} {:>6} {:>9} {:>6} {:>9}".format("day", "event / hook", "runs", "median", "t/o", "blocked"))
        for key in sorted(daily):
            entry = daily[key]
            durations = sorted(entry["durations"])
            budget = entry["budget"] if isinstance(entry["budget"], (int, float)) else 30000
            blocked = (sum(durations) + entry["timeouts"] * budget) / 60000.0
            median = "{0:.0f}ms".format(statistics.median(durations)) if durations else "-"
            print("{:<11} {:<46} {:>6} {:>9} {:>6} {:>8.1f}m".format(
                key[0], "{0} / {1}".format(key[1], key[2])[:46], entry["runs"], median, entry["timeouts"], blocked))
    print()
    print("A timed-out hook emits nothing, so the action proceeds -- protections silently do not apply")
    print("on those calls. Compare a nonzero timeout rate against the hook's configured timeout in")
    print("~/.claude/settings.json before assuming any hook is enforcing anything under load.")
    print("'(N unrecorded)' means the platform logs that hook only when it is cancelled or errors: its")
    print("runs are the tool calls, N successes left no record, and its latency cannot be read here.")


if __name__ == "__main__":
    main()
