r"""PreToolUse gate: a file tool names a path in its long spelling, never its 8.3 short one.

Claude Code matches a permission rule against the path as the tool names it, and an allow rule written as
~/AppData/Local/Temp/claude/** matches C:\Users\Administrator\... but not C:\Users\ADMINI~1\..., the spelling TEMP
carries on a profile whose name is longer than eight characters. A session that builds a path from $env:TEMP gets the
short spelling, and every Read, Edit or Write there becomes an approval prompt that freezes the session until the
operator answers. This gate makes the short spelling impossible instead: a deny returns at once, naming the same
path in its long spelling, and the retry matches the rule.

  deny  Read, Edit, Write, NotebookEdit, Grep or Glob whose path, or a Glob pattern's literal folder before its
        first wildcard, holds an 8.3 segment (NAME~N) that claude_temp.long_path expands to a different spelling.

Everything else defers to the normal permission flow. Wiring: PRETOOLUSE-HOOK.md, "The short-path gate". Any change
must be verified with Harness/Hooks/test_short_path_gate.py.
"""

import json
import os
import re
import sys

HOOKS = os.path.dirname(os.path.abspath(__file__))
if HOOKS not in sys.path:
    sys.path.insert(0, HOOKS)
from claude_temp import long_path  # noqa: E402

DENY = "deny"
DEFER = "defer"

FILE_TOOLS = ("Read", "Edit", "Write", "NotebookEdit", "Grep", "Glob")
SHORT_SEGMENT = re.compile(r"(^|[\\/])[^\\/~]{1,8}~\d+([.\\/]|$)")

REASON = (
    "Blocked: {path} uses an 8.3 short spelling, and allow rules match only a path's long spelling, so this call "
    "would ask the operator and freeze this session. Make the same call with {long} instead."
)


def target(tool_name, tool_input):
    if tool_name in ("Read", "Edit", "Write"):
        return tool_input.get("file_path")
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path")
    return tool_input.get("path")


def pattern_base(pattern):
    """The literal folder a Glob pattern names before its first wildcard, or None: an absolute pattern reaches its
    own root whatever path says."""
    if not isinstance(pattern, str):
        return None
    wild = min([pattern.find(mark) for mark in "*?[{" if mark in pattern] or [len(pattern)])
    base = os.path.dirname(pattern[:wild]) if wild < len(pattern) else pattern
    return base or None


def evaluate(tool_name, tool_input):
    """(decision, reason). Split out so the harness and the dispatcher call it in-process."""
    if tool_name not in FILE_TOOLS or not isinstance(tool_input, dict):
        return DEFER, ""
    paths = [target(tool_name, tool_input)]
    if tool_name == "Glob":
        paths.append(pattern_base(tool_input.get("pattern")))
    for path in paths:
        if not isinstance(path, str) or not SHORT_SEGMENT.search(path):
            continue
        try:
            expanded = long_path(os.path.abspath(path))
        except (OSError, ValueError):
            continue
        if os.path.normcase(expanded) != os.path.normcase(os.path.abspath(path)):
            return DENY, REASON.format(path=path, long=expanded)
    return DEFER, ""


def main():
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    decision, reason = evaluate(payload.get("tool_name"), payload.get("tool_input"))
    if decision == DENY:
        sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": DENY,
                                                            "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
