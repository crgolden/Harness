"""Check the master's audit findings an attended session is about to apply, one row at a time:

    python Harness/Hooks/apply_instruction_proposals.py <export folder>

The master publishes an audit edit it cannot make as a coordination-board `handoffs` row of kind `audit-edit`
(MasterSession/RUNBOOK.md, "Audit hand-offs"). The attended session exports those rows into a folder under
%TEMP%\\claude (ArtifactData query with out_dir), runs this over it, and applies each READY row with its own Edit,
where the operator approves the write; this script writes nothing. For each row it prints the target, a verdict and,
when the edit is a structured {old, new} pair, the unified diff:

    READY       the target's sha256 matches the row and `old` occurs exactly once
    STALE       the target changed since the audit read it
    NOT-FOUND   `old` does not occur in the target
    AMBIGUOUS   `old` occurs more than once
    MISSING     the target does not exist on this machine
    BY-HAND     the edit is free text, so only the target and its sha256 were checked
    SKIPPED     not an audit-edit row
"""

import difflib
import hashlib
import json
import os
import sys

KIND = "audit-edit"


def row_of(path):
    """An exported row: the document itself, or the {id, data, version} envelope ArtifactData writes."""
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    row = document.get("data") if isinstance(document.get("data"), dict) else document
    return os.path.splitext(os.path.basename(path))[0], row


def text_of(data):
    return data.decode("utf-8", "replace").replace("\r\n", "\n")


def verdict(row):
    """(verdict, detail, diff lines) for one exported row."""
    if row.get("kind") != KIND:
        return "SKIPPED", "kind is {0!r}".format(row.get("kind")), []
    target = os.path.expandvars(str(row.get("target") or ""))
    if not target or "%" in target or not os.path.isfile(target):
        return "MISSING", target or "(no target)", []
    with open(target, "rb") as handle:
        data = handle.read()
    if hashlib.sha256(data).hexdigest().lower() != str(row.get("sha256") or "").lower():
        return "STALE", "{0}: sha256 differs from the row's, so the file changed after the audit".format(target), []
    edit = row.get("edit")
    if not (isinstance(edit, dict) and isinstance(edit.get("old"), str) and isinstance(edit.get("new"), str)):
        return "BY-HAND", "{0}: free-text edit: {1}".format(target, edit), []
    live = text_of(data)
    old, new = edit["old"].replace("\r\n", "\n"), edit["new"].replace("\r\n", "\n")
    count = live.count(old) if old else 1
    if count == 0:
        return "NOT-FOUND", "{0}: the old text does not occur".format(target), []
    if count > 1:
        return "AMBIGUOUS", "{0}: the old text occurs {1} times".format(target, count), []
    proposed = live.replace(old, new, 1) if old else live + new
    diff = list(difflib.unified_diff(live.splitlines(True), proposed.splitlines(True), fromfile="live", tofile="proposed"))
    return "READY", "{0}: {1}".format(target, row.get("why") or ""), diff


def main(argv):
    if len(argv) != 2 or not os.path.isdir(argv[1]):
        sys.stderr.write("usage: apply_instruction_proposals.py <folder of exported handoffs rows>\n")
        return 2
    paths = sorted(os.path.join(root, name) for root, _, names in os.walk(argv[1]) for name in names if name.endswith(".json"))
    if not paths:
        print("No exported rows under {0}.".format(argv[1]))
        return 0
    for path in paths:
        doc_id, row = row_of(path)
        result, detail, diff = verdict(row)
        print("{0:<10} {1}  {2}".format(result, doc_id, detail))
        sys.stdout.writelines(diff)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
