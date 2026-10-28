"""PostToolUse reminder: an Edit or Write that adds a comment line puts Tools/AGENTS/COMMENTS.md in front of the agent.

Context only, never a block, by operator decision. The comment is already on disk when this runs; the point is
that the agent judges it now instead of in a later sweep. PreToolUse would not deliver it sooner: the hooks
reference adds its additionalContext alongside the tool result.

Scope is COMMENTS.md's own and is read, never copied: the extension map from Tools/Shared/ScanScopeHelper.ps1,
the exempt repos and the allowed shapes from Tools/CommentAudit/Get-CommentCatalog.ps1. Detection is by line
and deliberately loose, because a false reminder costs a sentence and nothing is blocked.

Added lines come from the tool result's structuredPatch ("+" lines), which both tools write. A Write that
creates a file has an empty patch, so every line of its content is added. Without a patch, an Edit's
new_string lines that are not in its old_string are the added ones.

FAIL-OPEN: anything unreadable emits nothing.
"""

import json
import os
import re
import sys

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_home  # noqa: E402

WORKSPACE = session_home.workspace_root()
TOOLS_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SCOPE_HELPER = ("Shared", "ScanScopeHelper.ps1")
COMMENT_CATALOG = ("CommentAudit", "Get-CommentCatalog.ps1")
ALLOWED_PATTERN_NAMES = ("suppressionPattern", "aaaBarePattern", "pinnedActionVersionPattern",
                         "toolingDirectivePattern")

RE_EXTENSION_ENTRY = re.compile(r"'(\.\w+)'\s*=\s*'(\w+)'")
RE_EXEMPT_REPOS = re.compile(r"\$exemptRepos\s*=\s*@\(([^)]*)\)")
RE_QUOTED = re.compile(r"'([^']*)'")
RE_NUMBERED_MIGRATION = re.compile(r"[\\/]migrations[\\/]\d{4}_[^\\/]*\.sql$", re.IGNORECASE)

SLASH_LANGUAGES = {"CSharp", "TypeScript", "JavaScript", "Astro"}
HASH_LANGUAGES = {"Python", "PowerShell", "Shell", "Yaml"}

RE_SLASH_COMMENT = re.compile(r"^\s*(//|/\*)|[;{}),]\s*//")
RE_BLOCK_COMMENT = re.compile(r"^\s*/\*")
RE_HASH_COMMENT = re.compile(r"^\s*#(?![!>])|\s#\s")
RE_POWERSHELL_BLOCK = re.compile(r"^\s*<#\s*\S")
RE_SQL_COMMENT = re.compile(r"^\s*(--|/\*)")

FIRST_LINE_LIMIT = 120

REMINDER = (
    "Are you adding a comment? Ensure it is compliant with Tools/AGENTS/COMMENTS.md, otherwise you'll just have to "
    "remove it later. This edit added {count} comment line(s) to {file}; the first is: {first}\n"
    "A comment is a running translation of the code into English, for a reader who cannot follow the code, "
    "because they are not fluent in its language or because it is written too poorly to follow. Fix that "
    "symptom instead: a better name or structure, a test, or a doc. A translation kept beside the code is a "
    "second copy that is guaranteed to drift.\n"
    "Allowed: a suppression carrying its rule and reason, a bare Arrange/Act/Assert marker, PowerShell "
    "comment-based help, a numbered SQL migration's header, a SHA-pinned action's version annotation, a bare "
    "tooling directive. Anything else gets exactly one disposition: a test, a doc line (AGENTS/REPOS/<Repo>.md "
    "or the repo's ARCHITECTURE.md), a rename or named constant, or deletion."
)


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except Exception:
        return None


def load_scope(tools_root):
    """(extension map, exempt repos, allowed patterns) read from the catalog's own files, or None."""
    helper = read_text(os.path.join(tools_root, *SCOPE_HELPER))
    catalog = read_text(os.path.join(tools_root, *COMMENT_CATALOG))
    if not helper or not catalog:
        return None
    extensions = {extension.lower(): language for extension, language in RE_EXTENSION_ENTRY.findall(helper)}
    exempt_match = RE_EXEMPT_REPOS.search(catalog)
    if not extensions or not exempt_match:
        return None
    exempt = {name.lower() for name in RE_QUOTED.findall(exempt_match.group(1))}
    allowed = []
    for name in ALLOWED_PATTERN_NAMES:
        found = re.search(r"\$" + name + r"\s*=\s*'((?:[^']|'')*)'", catalog)
        if not found:
            return None
        allowed.append(re.compile(found.group(1).replace("''", "'"), re.IGNORECASE))
    return extensions, exempt, allowed


def repo_of(file_path, workspace):
    try:
        relative = os.path.relpath(os.path.abspath(file_path), os.path.abspath(workspace))
    except ValueError:
        return None
    parts = relative.replace("\\", "/").split("/")
    if len(parts) < 2 or parts[0] in ("..", "", "."):
        return None
    return parts[0]


def added_lines(payload):
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    response = payload.get("tool_response") if isinstance(payload.get("tool_response"), dict) else {}
    patch = response.get("structuredPatch")
    if isinstance(patch, list) and patch:
        return [line[1:] for hunk in patch if isinstance(hunk, dict)
                for line in (hunk.get("lines") or []) if isinstance(line, str) and line.startswith("+")]
    if payload.get("tool_name") == "Write":
        content = tool_input.get("content")
        if response.get("type") == "update" or not isinstance(content, str):
            return []
        return content.splitlines()
    new = tool_input.get("new_string")
    if not isinstance(new, str):
        return []
    existing = set((tool_input.get("old_string") or "").splitlines())
    return [line for line in new.splitlines() if line not in existing]


def is_comment(line, language):
    if language in SLASH_LANGUAGES:
        return bool(RE_SLASH_COMMENT.search(line))
    if language == "Css":
        return bool(RE_BLOCK_COMMENT.search(line))
    if language == "Sql":
        return bool(RE_SQL_COMMENT.search(line))
    if language == "PowerShell" and RE_POWERSHELL_BLOCK.search(line):
        return True
    if language in HASH_LANGUAGES:
        return bool(RE_HASH_COMMENT.search(line))
    return False


def reminder(payload, workspace=None, tools_root=TOOLS_ROOT):
    """The reminder text for this edit, or None. The workspace is WORKSPACE unless one is given."""
    workspace = workspace or WORKSPACE
    if payload.get("tool_name") not in ("Edit", "Write"):
        return None
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    repo = repo_of(file_path, workspace)
    if repo is None:
        return None
    scope = load_scope(tools_root)
    if scope is None:
        return None
    extensions, exempt, allowed = scope
    if repo.lower() in exempt:
        return None
    language = extensions.get(os.path.splitext(file_path)[1].lower())
    if language is None or (language == "Sql" and RE_NUMBERED_MIGRATION.search(file_path)):
        return None
    found = [line.strip() for line in added_lines(payload)
             if is_comment(line, language) and not any(pattern.search(line) for pattern in allowed)]
    if not found:
        return None
    first = found[0] if len(found[0]) <= FIRST_LINE_LIMIT else found[0][:FIRST_LINE_LIMIT - 3] + "..."
    return REMINDER.format(count=len(found), file=file_path, first=first)


def main():
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        message = reminder(payload)
    except Exception:
        return 0
    if message:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": message}
        }, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
