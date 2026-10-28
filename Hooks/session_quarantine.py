"""The record of a session ejected for trying to change the gate that judges it, read by every hook that must
refuse it.

The main-checkout gate (invoke_main_checkout_gate.py, through the dispatcher) writes the record when a session
tries to change the hooks block of settings.json or write a quarantine record. From then on the dispatcher
refuses that session's every tool call with `continue: false`, and the Stop gate halts its stop. Only the operator
lifts a quarantine, by deleting its record. The record sits in a folder the session's own account can write, so
like every same-account control it holds against a careless session, not a determined one.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402

QUARANTINE_ROOT = claude_temp.quarantine_root()

STOP_REASON = (
    "EJECTED: this session tried to change the gate that judges it ({why}) and is quarantined. It may not run\n"
    "another tool or turn. Only the operator lifts this, by deleting {record}."
)
FILLER_STOP_REASON = (
    "EJECTED: this session {why} and is quarantined. It may not run\n"
    "another tool or turn. Only the operator lifts this, by deleting {record}."
)


def safe_session_id(session_id):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or "unknown"))


def record_path(session_id, root=None):
    return os.path.join(root or QUARANTINE_ROOT, safe_session_id(session_id) + ".json")


def quarantine(session_id, why, evidence, root=None, template=STOP_REASON):
    """Write the record and return the stop reason. A failed write still ejects: the halt never depends on it. The
    template words the reason for what the session did: a gate edit (STOP_REASON) or a second filler-streak eject
    (FILLER_STOP_REASON), and the record keeps it so the halt reads the same at every later stop."""
    path = record_path(session_id, root)
    entry = {"session_id": str(session_id), "why": why, "at": datetime.now(timezone.utc).isoformat(),
             "evidence": re.sub(r"[\r\n\t]+", " ", evidence or "")[:400]}
    if template is not STOP_REASON:
        entry["template"] = template
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        staged = path + ".tmp"
        with open(staged, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, separators=(",", ":"))
        os.replace(staged, path)
    except Exception:
        pass
    return template.format(why=why, record=path)


def quarantined(session_id, root=None):
    """The record, or None. An unreadable record still quarantines: deleting it is the only release."""
    path = record_path(session_id, root)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            entry = json.load(fh)
        return entry if isinstance(entry, dict) else {"why": "unreadable record"}
    except Exception:
        return {"why": "unreadable record"}


def halt_envelope(session_id, root=None):
    """The hook output that stops a quarantined session, or None when it is not quarantined."""
    entry = quarantined(session_id, root)
    if entry is None:
        return None
    template = entry.get("template") if isinstance(entry.get("template"), str) and "{record}" in entry.get("template") \
        else STOP_REASON
    reason = template.format(why=entry.get("why", "?"), record=record_path(session_id, root))
    return {"continue": False, "stopReason": reason}
