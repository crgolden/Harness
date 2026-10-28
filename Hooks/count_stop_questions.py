"""Measure how often the user has had to tell the agent to stop stopping.

This is the metric behind Harness/Hooks/STOP-HOOK.md. The gate's whole justification is that instructions,
memories and promises failed against this behavior, so the gate itself must be judged the same way --
by whether the count falls, not by whether it sounds like it should work.

Reads every session transcript for a project, keeps only genuine user-authored prose (tool results,
system reminders and injected continuation summaries are not the user speaking), and prints each match
verbatim so the count can be checked rather than trusted.

Usage:
    python count_stop_questions.py [project-transcript-directory] [--summary]

``--summary`` prints only the totals, for the daily digest in Send-MemoryAuditReport.ps1. Without it every
match is printed verbatim so the count can be audited rather than trusted.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_home  # noqa: E402

DEFAULT_TRANSCRIPTS = Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", session_home.workspace_root())

PATTERNS = {
    "asked why I stopped": re.compile(
        r"why (did|are) you (stop|stopped|paused)|you (just )?stopped|stopped again|why you stopped"
        r"|you seem to have stopped|why'd you stop",
        re.I,
    ),
    "told me not to stop / keep going": re.compile(
        r"\b(don'?t|do not) (stop|pause|wait|idle)|stop stopping|keep (going|working)"
        r"|without (stopping|pausing)|don'?t bug me|continue with the remaining",
        re.I,
    ),
    "'was that a question?'": re.compile(r"was that a question|is there a question for me|any question", re.I),
}

INJECTED_MARKERS = ("<system-reminder>", "<local-command", "This session is being continued from")


def user_texts(path: Path) -> list[str]:
    texts: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "user":
            continue
        content = record.get("message", {}).get("content")
        if isinstance(content, str):
            chunks = [content]
        elif isinstance(content, list):
            chunks = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        else:
            continue
        for chunk in chunks:
            if not chunk.strip() or any(marker in chunk for marker in INJECTED_MARKERS):
                continue
            texts.append(chunk.strip())
    return texts


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    summary_only = "--summary" in sys.argv[1:]
    root = Path(args[0]) if args else DEFAULT_TRANSCRIPTS
    if not root.is_dir():
        print(f"no transcript directory at {root}")
        return 1

    files = sorted(root.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    totals = {label: 0 for label in PATTERNS}
    seen: set[str] = set()
    for path in files:
        for position, text in enumerate(user_texts(path), start=1):
            labels = [label for label, pattern in PATTERNS.items() if pattern.search(text)]
            if not labels:
                continue
            key = text[:200]
            if key in seen:
                continue
            seen.add(key)
            for label in labels:
                totals[label] += 1
            if summary_only:
                continue
            excerpt = " ".join(text.split())
            if len(excerpt) > 160:
                excerpt = excerpt[:157] + "..."
            print(f"[{path.stem[:8]} msg {position:>3}] ({', '.join(labels)})\n    {excerpt}\n")

    print("=" * 70)
    for label, count in totals.items():
        print(f"{label:<34} {count}")
    print(f"{'distinct messages matching any':<34} {len(seen)}")
    print(f"transcripts scanned: {len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
