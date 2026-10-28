# GATE-STEP: Main-checkout gate harness
# GATE-LISTING: os.walk(root) | *.py *.ps1
"""Regression harness for invoke_main_checkout_gate.py. Exits non-zero on any regression.

    python Harness/Hooks/test_main_checkout_gate.py

In-process against evaluate() and gate_change(), against a fixture checkout and a fixture settings.json, so no row
touches the real Tools checkout or the operator's settings.
"""

import _winapi
import ctypes
import json
import os
import re
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import invoke_main_checkout_gate as gate  # noqa: E402

TOOLS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESOLVERS = {os.path.normcase(os.path.join("Hooks", name)) for name in ("claude_temp.py", "ClaudeTemp.ps1")}
HARNESS_FIXTURES = {os.path.normcase(os.path.join("Gates", name)) for name in ("gate-common-probe.ps1", "Test-GateSlot.ps1")}
SKIPPED_FOLDERS = {".git", ".claude", "node_modules", "__pycache__"}
RE_TMPDIR_READ = re.compile(
    r"environ(?:\.get)?\s*[\(\[]\s*['\"]CLAUDE_CODE_TMPDIR['\"]|getenv\(\s*['\"]CLAUDE_CODE_TMPDIR['\"]"
    r"|\$env:CLAUDE_CODE_TMPDIR\b(?!\s*=(?!=))", re.IGNORECASE)
RE_TEMP_CLAUDE = re.compile(
    r"(?:\$env:(?:TEMP|TMP)\b|environ(?:\.get)?\s*[\(\[]\s*['\"](?:TEMP|TMP)['\"]|getenv\(\s*['\"](?:TEMP|TMP)['\"]"
    r"|gettempdir\(\)|GetTempPath\(\))[^\n]*?[\\/'\"]claude(?=[\\/'\"]|$)", re.IGNORECASE)


def code_lines(text, powershell):
    """(line number, line) for every line that is code: comment lines and PowerShell <# #> blocks are dropped."""
    inside_block = False
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if powershell and inside_block:
            inside_block = "#>" not in stripped
            continue
        if powershell and stripped.startswith("<#"):
            inside_block = "#>" not in stripped[2:]
            continue
        if stripped.startswith("#"):
            continue
        yield number, line


def own_temp_reads(root):
    """Every file:line outside the two resolvers that reads CLAUDE_CODE_TMPDIR, or builds Claude Code's tree from the
    temp folder. Harnesses and their fixture scripts set and clear the variable to isolate themselves, so they are not
    judged."""
    found = []
    for folder, folders, files in os.walk(root):
        folders[:] = sorted(name for name in folders if name not in SKIPPED_FOLDERS)
        for name in sorted(files):
            if not name.lower().endswith((".py", ".ps1", ".psm1")) or name.startswith(("test_", "Test-")):
                continue
            path = os.path.join(folder, name)
            relative = os.path.relpath(path, root)
            if os.path.normcase(relative) in RESOLVERS | HARNESS_FIXTURES:
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            for number, line in code_lines(text, not name.lower().endswith(".py")):
                if RE_TMPDIR_READ.search(line) or RE_TEMP_CLAUDE.search(line):
                    found.append("{0}:{1}".format(relative, number))
    return found


BARE_TEMP_BY_DESIGN = {os.path.normcase(os.path.join("AlertTriage", name)) for name in (
    "Invoke-AlertTriageProbe.ps1", "Invoke-TriagePermissionMatrix.ps1", "Invoke-AlertTriageRun.ps1",
    "Test-AlertTriage.ps1")}
TRIAGE_FENCE_STATE = {os.path.normcase(os.path.join("AlertTriage", name)) for name in (
    "Invoke-AlertTriageGate.ps1", "Invoke-AlertTriageProbe.ps1", "Invoke-TriagePermissionMatrix.ps1",
    "Install-AlertTriage.ps1", "test_triage_gate.py")} | {os.path.normcase(os.path.join("Gates", "GateCommon.ps1"))}
THIS_DETECTOR = {os.path.normcase(os.path.join("Hooks", "test_main_checkout_gate.py"))}
RE_PY_CREATOR = re.compile(r"\btempfile\.(?:mkdtemp|mkstemp|TemporaryDirectory|NamedTemporaryFile|TemporaryFile"
                           r"|SpooledTemporaryFile)\(")
RE_PS_CREATOR = re.compile(r"GetTempFileName\(\)|\bNew-TemporaryFile\b|GetTempPath\(\)|Join-Path\s+\$env:(?:TEMP|TMP)\b"
                           r"|[\"']\$env:(?:TEMP|TMP)\\", re.IGNORECASE)
RE_LEGACY_ROOT = re.compile(r"claude-tmp|crgolden-oneshot|crgolden-fleet-setup|\.claude-master"
                            r"|\.claude[\\/]{1,2}(?:audits|gates|handoffs|master|parking-lot|scratch|work-products)\b"
                            r"|['\"]\.claude['\"]\s*[,/]\s*['\"](?:audits|gates|handoffs|master|parking-lot|scratch"
                            r"|work-products)['\"]"
                            r"|\\\.claude\[[^\]\n]*\](?:audits|gates|handoffs|master|parking-lot|scratch|work-products)\b"
                            r"|LOCALAPPDATA[^\n]{0,40}['\"\\/]crgolden(?:-gates)?['\"]", re.IGNORECASE)


def call_text(text, start):
    """The source of the call opening at start, through its closing parenthesis (at most 600 characters)."""
    depth = 0
    for index in range(start, min(len(text), start + 600)):
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        if depth == 0 and text[index] == ")":
            return text[start:index + 1]
    return text[start:start + 600]


def bare_temp_creators(root):
    """Every file:line, harnesses included, that creates a temp file or folder outside the one root: a Python tempfile
    call with no dir=, or a PowerShell GetTempFileName, New-TemporaryFile, GetTempPath or $env:TEMP path. The two
    resolvers derive the root itself, and the triage probes write the bare temp folder on purpose to test the fence."""
    found = []
    for folder, folders, files in os.walk(root):
        folders[:] = sorted(name for name in folders if name not in SKIPPED_FOLDERS)
        for name in sorted(files):
            if not name.lower().endswith((".py", ".ps1", ".psm1")):
                continue
            path = os.path.join(folder, name)
            relative = os.path.relpath(path, root)
            if os.path.normcase(relative) in RESOLVERS | HARNESS_FIXTURES | BARE_TEMP_BY_DESIGN | THIS_DETECTOR:
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            python = name.lower().endswith(".py")
            offsets = [0]
            for line in text.splitlines(keepends=True):
                offsets.append(offsets[-1] + len(line))
            for number, line in code_lines(text, not python):
                if python:
                    for match in RE_PY_CREATOR.finditer(line):
                        if "dir=" not in call_text(text, offsets[number - 1] + match.end() - 1):
                            found.append("{0}:{1}".format(relative, number))
                elif RE_PS_CREATOR.search(line) and not re.match(r"\s*\$env:(?:TEMP|TMP)\s*=", line, re.IGNORECASE):
                    found.append("{0}:{1}".format(relative, number))
    return found


def legacy_root_names(root):
    """Every file:line, harnesses included, that names a folder an earlier layout kept temp state in. The resolver
    lists them (claude_temp.legacy_roots) for the fleet audit's absence check, the triage fence keeps its own
    .claude\\gates beside its worktrees under its own account, and this detector plants them; nothing else may name
    one, since code that names one writes there again and a harness that names one is a migration test that outlived
    its migration."""
    found = []
    for folder, folders, files in os.walk(root):
        folders[:] = sorted(name for name in folders if name not in SKIPPED_FOLDERS)
        for name in sorted(files):
            if not name.lower().endswith((".py", ".ps1", ".psm1")):
                continue
            path = os.path.join(folder, name)
            relative = os.path.relpath(path, root)
            if os.path.normcase(relative) in RESOLVERS | HARNESS_FIXTURES | TRIAGE_FENCE_STATE | THIS_DETECTOR:
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            for number, line in code_lines(text, not name.lower().endswith(".py")):
                if RE_LEGACY_ROOT.search(line):
                    found.append("{0}:{1}".format(relative, number))
    return found


DOC_RESOLVER = {os.path.normcase(os.path.join("Hooks", "session_home.py"))}
SELFTEST_FIXTURE_WORKSPACE = {os.path.normcase(os.path.join("Claude", "skills", "memory-audit", "scripts", "doc_refs.py"))}
RE_WORKSPACE_DOC_DOC = r"(?:AGENTS(?:[\\/][^'\"]*)?|AGENTS\.md|CLAUDE\.md)"
RE_WORKSPACE_DOC_PATH = re.compile(
    r"Join-Path\s+\$\w*(?:workspace|fleet|ws)\w*\s+['\"]" + RE_WORKSPACE_DOC_DOC + r"['\"]"
    r"|os\.path\.join\(\s*\w*(?:workspace|ws)\w*\s*,\s*['\"]" + RE_WORKSPACE_DOC_DOC + r"['\"]", re.IGNORECASE)


def workspace_doc_paths(root):
    """Every file:line, harnesses included, that builds a path to the caller's fleet docs (the workspace's AGENTS
    folder, AGENTS.md or CLAUDE.md) by joining a workspace variable with the doc's name. Tools takes the workspace
    from its caller (session_home.workspace_root) and its doc layout only from session_home.workspace_doc, so a
    path built anywhere else couples Tools to one caller's layout. doc_refs.py's selftest builds a fixture workspace
    in the scratch folder, which is no caller's."""
    found = []
    for folder, folders, files in os.walk(root):
        folders[:] = sorted(name for name in folders if name not in SKIPPED_FOLDERS)
        for name in sorted(files):
            if not name.lower().endswith((".py", ".ps1", ".psm1")):
                continue
            path = os.path.join(folder, name)
            relative = os.path.relpath(path, root)
            if os.path.normcase(relative) in DOC_RESOLVER | SELFTEST_FIXTURE_WORKSPACE | THIS_DETECTOR:
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            for number, line in code_lines(text, not name.lower().endswith(".py")):
                if RE_WORKSPACE_DOC_PATH.search(line):
                    found.append("{0}:{1}".format(relative, number))
    return found


def workspace_doc_rows(base):
    """The workspace-doc rule on planted files (each must-fail shape caught, each control left alone), then on the
    real Tools tree, which must hold none."""
    planted = os.path.join(base, "planted-workspace-docs")
    files = {
        "joins_testing.ps1": "$doc = Join-Path $workspace 'AGENTS\\TESTING.md'\n",
        "joins_fleet.ps1": "$docs = Join-Path $fleetRoot 'AGENTS'\n",
        "joins_guide.py": "import os\nguide = os.path.join(workspace_root, \"AGENTS\", \"REPOS\", repo + \".md\")\n",
        "joins_claude.py": "import os\nrules = os.path.join(WORKSPACE, 'CLAUDE.md')\n",
        "own_docs.ps1": "$doc = Join-Path (Split-Path -Parent $PSScriptRoot) 'AGENTS\\TOOLING.md'\n",
        "own_docs.py": "import os\ndoc = os.path.join(TOOLS_ROOT, \"AGENTS\", \"GATES.md\")\n",
        "resolved.py": "import session_home\nguide = session_home.workspace_doc('AGENTS/REPOS/Identity.md', workspace)\n",
        "repo_path.py": "import os\nrepo = os.path.join(workspace, 'Identity')\n",
    }
    os.makedirs(planted)
    for name, text in files.items():
        with open(os.path.join(planted, name), "w", encoding="utf-8") as handle:
            handle.write(text)
    found = sorted(workspace_doc_paths(planted))
    wanted = ["joins_claude.py:2", "joins_fleet.ps1:1", "joins_guide.py:2", "joins_testing.ps1:1"]
    real = workspace_doc_paths(TOOLS_ROOT)
    return [
        ("must-fail: each planted path built from a workspace to its fleet docs is caught, and no Tools-own doc, "
         "resolver call or repo path is", found == wanted, found),
        ("no Tools file, harnesses included, builds a path to the caller's fleet docs outside session_home",
         not real, real),
    ]


def legacy_temp_rows(base):
    """Both one-root rules on planted files (each must-fail shape caught, each control left alone), then on the real
    Tools tree, which must hold neither."""
    planted = os.path.join(base, "planted-legacy")
    files = {
        "bare_mkdtemp.py": "import tempfile\nroot = tempfile.mkdtemp(prefix='x-')\n",
        "bare_multiline.py": "import tempfile\nwith tempfile.NamedTemporaryFile(\n        'w', delete=False) as h:\n    pass\n",
        "scoped_mkdtemp.py": "import tempfile, claude_temp\nroot = tempfile.mkdtemp(prefix='x-', dir=claude_temp.scratch_dir())\n",
        "scoped_multiline.py": "import tempfile\nwith tempfile.TemporaryDirectory(\n        dir=base) as h:\n    pass\n",
        "bare_temp_file.ps1": "$tmp = [IO.Path]::GetTempFileName()\n",
        "bare_temp_join.ps1": "$zip = Join-Path $env:TEMP 'installer.zip'\n",
        "bare_temp_string.ps1": "$log = \"$env:TEMP\\run.log\"\n",
        "sets_temp.ps1": "$env:TEMP = 'C:\\fixture'\n",
        "scoped_temp.ps1": "$zip = New-ClaudeTempPath 'installer.zip'\n",
        "legacy_audits.py": "import os\nrun = os.path.join(workspace, '.claude', 'audits')\nother = 'x\\\\.claude\\\\gates'\n"
                            "cache = base / \".claude\" / \"audits\" / \"session-audit\"\n",
        "legacy_localappdata.ps1": "$state = Join-Path $env:LOCALAPPDATA 'crgolden'\n",
        "legacy_programdata.ps1": "$tmp = 'C:\\ProgramData\\claude-tmp'\n",
        "not_legacy.ps1": "$cache = Join-Path $env:LOCALAPPDATA 'crgolden-notes-unrelated'\n$doc = '.claude\\settings.json'\n",
        "test_migration.py": "import os\nold = os.path.join(ws, '.claude', 'parking-lot', 's')\n",
        "legacy_regex.py": "import re\nRE = re.compile(r\"[\\\\/]\\.claude[\\\\/]work-products[\\\\/][^\\\\/]+\\.json$\")\n",
    }
    os.makedirs(planted)
    for name, text in files.items():
        with open(os.path.join(planted, name), "w", encoding="utf-8") as handle:
            handle.write(text)
    creators = sorted(bare_temp_creators(planted))
    wanted_creators = ["bare_mkdtemp.py:2", "bare_multiline.py:2", "bare_temp_file.ps1:1", "bare_temp_join.ps1:1",
                       "bare_temp_string.ps1:1"]
    names = sorted(legacy_root_names(planted))
    wanted_names = ["legacy_audits.py:2", "legacy_audits.py:3", "legacy_audits.py:4", "legacy_localappdata.ps1:1",
                    "legacy_programdata.ps1:1", "legacy_regex.py:2", "test_migration.py:2"]
    real_creators = bare_temp_creators(TOOLS_ROOT)
    real_names = legacy_root_names(TOOLS_ROOT)
    return [
        ("must-fail: each planted bare-temp creator is caught, and no scoped call or TEMP setter is",
         creators == wanted_creators, creators),
        ("must-fail: each planted legacy root is caught, a harness's included, and no lookalike is",
         names == wanted_names, names),
        ("no Tools file, harnesses included, creates a temp file outside %TEMP%\\claude", not real_creators,
         real_creators),
        ("no Tools file, harnesses included, names a legacy temp root but the resolver and the triage fence",
         not real_names, real_names),
    ]


def temp_resolver_rows(base):
    """The single-resolver rule on planted files (each must-fail shape caught, each control left alone), then on the
    real Tools tree, which must hold no read but the resolvers' own."""
    planted = os.path.join(base, "planted")
    files = {
        "reads_override.ps1": "$root = $env:CLAUDE_CODE_TMPDIR\n",
        "reads_override.py": "import os\nroot = os.environ.get('CLAUDE_CODE_TMPDIR')\n",
        "builds_tree.ps1": "$out = \"$env:TEMP\\claude\\catalog.csv\"\n",
        "joins_tree.ps1": "$tree = Join-Path $env:TEMP 'claude'\n",
        "joins_tree.py": "import os\ntree = os.path.join(os.environ['TEMP'], 'claude')\n",
        "scratch.ps1": "$zip = Join-Path $env:TEMP 'installer.zip'\n$fixture = Join-Path $env:TEMP \"claude-tmp-x\"\n",
        "commented.ps1": "# $env:CLAUDE_CODE_TMPDIR names the root\n<#\n$env:TEMP\\claude\\x\n#>\nGet-Service Redis\n",
        "sets_override.ps1": "$env:CLAUDE_CODE_TMPDIR = 'C:\\fixture'\n",
        "test_fixture.py": "import os\nroot = os.environ.get('CLAUDE_CODE_TMPDIR')\n",
    }
    os.makedirs(planted)
    for name, text in files.items():
        with open(os.path.join(planted, name), "w", encoding="utf-8") as handle:
            handle.write(text)
    found = sorted(own_temp_reads(planted))
    wanted = ["builds_tree.ps1:1", "joins_tree.ps1:1", "joins_tree.py:2", "reads_override.ps1:1", "reads_override.py:2"]
    real = own_temp_reads(TOOLS_ROOT)
    return [
        ("must-fail: each planted read of the variable or of TEMP plus claude is caught, and no control is",
         found == wanted, found),
        ("no file but the two resolvers reads CLAUDE_CODE_TMPDIR or builds Claude Code's tree from TEMP",
         not real, real),
    ]


def short_path(path):
    """The 8.3 spelling of an existing path (C:\\Users\\ADMINI~1\\...), or the path unchanged."""
    if os.name != "nt":
        return path
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer))
    return buffer.value if 0 < length < len(buffer) else path


def checkout_cases(checkout, worktree, skill_link):
    return [
        ("deny", "an Edit of a hook in the main checkout",
         ("Edit", {"file_path": os.path.join(checkout, "Hooks", "invoke_stop_gate.py")})),
        ("deny", "a Write of a gate helper in the main checkout",
         ("Write", {"file_path": os.path.join(checkout, "Gates", "GateCommon.ps1"), "content": "x"})),
        ("deny", "a NotebookEdit under the main checkout's Hooks",
         ("NotebookEdit", {"notebook_path": os.path.join(checkout, "Hooks", "n.ipynb")})),
        ("deny", "a forward-slash spelling of a main-checkout hook",
         ("Edit", {"file_path": checkout.replace("\\", "/") + "/Hooks/invoke_pretooluse_gate.py"})),
        ("deny", "a main-checkout script outside Gates and Hooks",
         ("Edit", {"file_path": os.path.join(checkout, "Health", "Watch-DiskHealth.ps1")})),
        ("deny", "a new file at the main checkout's root",
         ("Write", {"file_path": os.path.join(checkout, "AGENTS.md"), "content": "x"})),
        ("deny", "a main-checkout skill reached through a skills junction",
         ("Edit", {"file_path": os.path.join(skill_link, "SKILL.md")})),
        ("defer", "control: the same script edited on a branch worktree",
         ("Edit", {"file_path": os.path.join(worktree, "Health", "Watch-DiskHealth.ps1")})),
        ("defer", "control: a sibling folder whose name only starts the same as the checkout",
         ("Write", {"file_path": checkout + "Archive" + os.sep + "a.py", "content": "x"})),
        ("defer", "control: reading a main-checkout hook",
         ("Read", {"file_path": os.path.join(checkout, "Hooks", "invoke_stop_gate.py")})),
        ("defer", "control: running a main-checkout hook's harness",
         ("PowerShell", {"command": "python \"{0}\"".format(os.path.join(checkout, "Hooks", "test_stop_gate.py"))})),
    ]


def harness_cases(harness, harness_worktree, sibling):
    """Rows against a fixture Harness main checkout: a file-tool write there is denied for every session, the master
    included, while a worktree of Harness, a sibling folder and a read are not this gate's."""
    return [
        ("deny", "must-block: an Edit of a hook in the Harness main checkout",
         ("Edit", {"file_path": os.path.join(harness, "Hooks", "invoke_stop_gate.py")})),
        ("deny", "must-block: a Write at the Harness main checkout's root",
         ("Write", {"file_path": os.path.join(harness, "AGENTS.md"), "content": "x"})),
        ("deny", "must-block: a forward-slash spelling of a Harness hook",
         ("Edit", {"file_path": harness.replace("\\", "/") + "/Hooks/invoke_read_scope_gate.py"})),
        ("deny", "must-block: a NotebookEdit under the Harness main checkout",
         ("NotebookEdit", {"notebook_path": os.path.join(harness, "Hooks", "n.ipynb")})),
        ("defer", "control: the same hook in a worktree of Harness",
         ("Edit", {"file_path": os.path.join(harness_worktree, "Hooks", "invoke_stop_gate.py")})),
        ("defer", "control: a sibling whose name only starts like Harness",
         ("Write", {"file_path": os.path.join(sibling, "a.py"), "content": "x"})),
        ("defer", "control: reading a Harness hook is not this gate's",
         ("Read", {"file_path": os.path.join(harness, "Hooks", "invoke_stop_gate.py")})),
    ]


def settings_cases(settings):
    """(expected decision, expected gate change, name, tool, tool_input) against a fixture settings.json."""
    with open(settings, "w", encoding="utf-8") as handle:
        json.dump({"env": {"A": "1"}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python"}]}]}},
                  handle, indent=2)
    with open(settings, "r", encoding="utf-8") as handle:
        text = handle.read()
    return [
        ("deny", True, "must-block: an Edit that changes the hooks block of settings.json",
         "Edit", {"file_path": settings, "old_string": '"command": "python"', "new_string": '"command": "exit 0"'}),
        ("deny", True, "must-block: a Write that removes the hooks block",
         "Write", {"file_path": settings, "content": json.dumps({"env": {"A": "1"}})}),
        ("deny", True, "must-block: a Write that leaves settings.json unparsable",
         "Write", {"file_path": settings, "content": text[:-3]}),
        ("defer", False, "control: an Edit of settings.json that leaves the hooks block alone",
         "Edit", {"file_path": settings, "old_string": '"A": "1"', "new_string": '"A": "2"'}),
        ("defer", False, "control: the same hooks-shaped Edit to another JSON file",
         "Edit", {"file_path": settings + ".other.json", "old_string": '"command": "python"', "new_string": "x"}),
    ]


SHARE_SERVER = "SHARE-HOST"
SHARE_DESKTOP = "SHARE-DESK"
SHARE_DISK = "D:\\crgolden-state"


def share_fixture(base, enabled, users=("Administrator", "crgol")):
    """A fixture state_share.json, never the live one, enabling only the machines named."""
    path = os.path.join(base, "state_share-{0}.json".format("-".join(enabled) or "none"))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"server": SHARE_SERVER, "share": "crgolden-state", "serverPath": SHARE_DISK, "drive": "Z",
                   "enabled": list(enabled), "users": list(users)}, handle)
    return path


def share_spellings(relative):
    """The three spellings of one path under the share, as a caller would type them: the server's disk, the mapped
    drive and the UNC path."""
    return [("D:", SHARE_DISK + "\\" + relative), ("Z:", "Z:\\" + relative),
            ("UNC", "\\\\" + SHARE_SERVER + "\\crgolden-state\\" + relative)]


def load_as(config_path, machine, user, cut_over=True):
    """claude_temp, the quarantine module and the gate reloaded as machine and user under config_path, so the gate's
    module-level roots are computed from the share the way a hook process started there computes them; cut_over says
    whether that account's state has been moved onto the share, and None keeps the real marker check."""
    import importlib
    import session_quarantine
    os.environ["COMPUTERNAME"] = machine
    os.environ["USERNAME"] = user
    importlib.reload(claude_temp)
    claude_temp.SHARE_CONFIG = config_path
    if cut_over is not None:
        claude_temp.cut_over = lambda folder: cut_over and bool(folder)
        claude_temp.legacy_cut_over = lambda folder: False
    importlib.reload(session_quarantine)
    importlib.reload(gate)


def share_rows(base, settings):
    """The state share: on an enabled machine a write to the Stop gate's state, a quarantine record, a gate record or
    the transcript index is a gate change in each spelling of the share (server disk, mapped drive, UNC), on the
    server and on a desktop; a folder beside the share is not; and a disabled machine, or an enabled machine's user
    outside the config's users, keeps every root under %TEMP%\\claude."""
    rows = []
    kept = {key: os.environ.get(key) for key in ("COMPUTERNAME", "USERNAME")}
    kept_config = getattr(claude_temp, "SHARE_CONFIG", None)
    raw = os.path.join(os.environ.get("CLAUDE_CODE_TMPDIR") or os.environ["TEMP"], "claude")
    enabled = share_fixture(base, [SHARE_SERVER, SHARE_DESKTOP])
    try:
        relatives = {}
        for machine, root in ((SHARE_SERVER, SHARE_DISK), (SHARE_DESKTOP, "Z:\\")):
            load_as(enabled, machine, "Administrator")
            share = claude_temp.share_root()
            rows.append(("share: {0}'s share root is {1}".format(machine, root),
                         share is not None and os.path.normcase(share) == os.path.normcase(root), share))
            state_roots = (("Stop gate state", gate.STOP_GATE_STATE_ROOT), ("quarantine", gate.QUARANTINE_ROOT),
                           ("gate records", gate.GATE_RECORDS_ROOT), ("transcript index", gate.TRANSCRIPT_INDEX_ROOT))
            for label, state_root in state_roots:
                placed = gate.under(state_root, root)
                rows.append(("share: on {0} the {1} sits under the share".format(machine, label), placed, state_root))
                if not placed:
                    continue
                relative = os.path.relpath(state_root, root) + "\\a-session.json"
                relatives.setdefault(label, relative)
                for spelling, target in share_spellings(relative):
                    why = gate.gate_change("Write", {"file_path": target, "content": "{}"}, settings)
                    rows.append(("must-block: on {0} a write to the {1} through {2} is a gate change".format(
                        machine, label, spelling), why is not None, target))
            load_as(enabled, machine, "Administrator", cut_over=False)
            rows.append(("must-block: on {0} an enabled account not yet cut over keeps the Stop gate state and gate "
                         "records under %TEMP%\\claude, so the enable strands nothing".format(machine),
                         gate.under(gate.STOP_GATE_STATE_ROOT, raw) and gate.under(gate.GATE_RECORDS_ROOT, raw),
                         gate.GATE_RECORDS_ROOT))
            load_as(enabled, machine, "Administrator")
            sibling = "D:\\crgolden-state-other\\" + machine + "\\claude\\claude-stopgate\\a-session.json"
            rows.append(("control: on {0} a folder beside the share is no gate change".format(machine),
                         gate.gate_change("Write", {"file_path": sibling, "content": "{}"}, settings) is None, sibling))
        for name, config_path, user in (
                ("a disabled machine", share_fixture(base, []), "Administrator"),
                ("an enabled machine's user outside the config's users", enabled, "crgolden-triage")):
            load_as(config_path, SHARE_SERVER, user)
            share = claude_temp.share_root()
            rows.append(("control: for {0} there is no share root".format(name), share is None, share))
            rows.append(("control: for {0} the Stop gate state and quarantine stay under %TEMP%\\claude".format(name),
                         gate.under(gate.STOP_GATE_STATE_ROOT, raw) and gate.under(gate.QUARANTINE_ROOT, raw),
                         gate.STOP_GATE_STATE_ROOT))
            for label, relative in sorted(relatives.items()):
                target = share_spellings(relative)[0][1]
                rows.append(("control: for {0} a write to the share's {1} path is no gate change".format(name, label),
                             gate.gate_change("Write", {"file_path": target, "content": "{}"}, settings) is None,
                             target))
    finally:
        for key, value in kept.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        load_as(kept_config, kept["COMPUTERNAME"] or "", kept["USERNAME"] or "", cut_over=None)
    return rows


def main():
    started = time.time()
    failures = 0
    count = 0
    base = tempfile.mkdtemp(prefix="main-checkout-gate-", dir=claude_temp.scratch_dir())
    checkout = os.path.join(base, "Tools")
    worktree = os.path.join(base, "Tools-branch")
    settings = os.path.join(base, "settings.json")
    skill = os.path.join(checkout, "Claude", "skills", "parking-lot")
    skill_link = os.path.join(base, "profile-skills", "parking-lot")
    os.makedirs(skill)
    os.makedirs(os.path.dirname(skill_link))
    _winapi.CreateJunction(skill, skill_link)
    try:
        for expected, name, (tool, tool_input) in checkout_cases(checkout, worktree, skill_link):
            decision, reason = gate.evaluate(tool, tool_input, checkout=checkout, settings=settings)
            ok = decision == expected and (decision == gate.DEFER or "worktree" in reason)
            print("{0}  {1:<64} {2}".format("PASS" if ok else "FAIL", name, decision))
            failures += 0 if ok else 1
            count += 1
        harness = os.path.join(base, "Harness")
        harness_worktree = os.path.join(base, "source", "worktrees", "a-session", "Harness")
        for expected, name, (tool, tool_input) in harness_cases(harness, harness_worktree, harness + "Archive"):
            decision, reason = gate.evaluate(tool, tool_input, checkout=checkout, settings=settings, harness=harness)
            ok = decision == expected and (decision == gate.DEFER or ("worktree" in reason and "Harness" in reason))
            print("{0}  {1:<64} {2}".format("PASS" if ok else "FAIL", name, decision))
            failures += 0 if ok else 1
            count += 1
        for expected, change, name, tool, tool_input in settings_cases(settings):
            decision, reason = gate.evaluate(tool, tool_input, checkout=checkout, settings=settings)
            why = gate.gate_change(tool, tool_input, settings)
            ok = decision == expected and bool(why) == change and (decision == gate.DEFER or bool(reason))
            print("{0}  {1:<64} {2}, gate change: {3}".format("PASS" if ok else "FAIL", name, decision, bool(why)))
            failures += 0 if ok else 1
            count += 1
        record = ("Write", {"file_path": os.path.join(gate.QUARANTINE_ROOT, "a-session.json"), "content": "{}"})
        decision, _ = gate.evaluate(record[0], record[1], checkout=checkout, settings=settings)
        why = gate.gate_change(record[0], record[1], settings)
        ok = decision == gate.DENY and bool(why)
        print("{0}  {1:<64} {2}, gate change: {3}".format(
            "PASS" if ok else "FAIL", "must-block: a write to a quarantine record denies and ejects", decision, bool(why)))
        failures += 0 if ok else 1
        count += 1
        for name, target in (
                ("must-block: a write to a Stop gate state file denies and ejects",
                 os.path.join(gate.STOP_GATE_STATE_ROOT, "a-session.json")),
                ("must-block: a write to the Stop gate decision log denies and ejects",
                 os.path.join(gate.STOP_GATE_STATE_ROOT, "decisions.log")),
                ("must-block: a forged PASSED gate record denies and ejects",
                 os.path.join(gate.GATE_RECORDS_ROOT, claude_temp.checkout_key(checkout), "Tools.HOST.last.json")),
                ("must-block: a forged per-step carry denies and ejects",
                 os.path.join(gate.GATE_RECORDS_ROOT, claude_temp.checkout_key(checkout), "Tools.HOST.json")),
                ("must-block: a write to the hooks' transcript index denies and ejects",
                 os.path.join(gate.TRANSCRIPT_INDEX_ROOT, "0123abcd.json"))):
            forged = ("Write", {"file_path": target, "content": "{\"blocks\":100}"})
            decision, _ = gate.evaluate(forged[0], forged[1], checkout=checkout, settings=settings)
            why = gate.gate_change(forged[0], forged[1], settings)
            ok = decision == gate.DENY and bool(why)
            print("{0}  {1:<64} {2}, gate change: {3}".format("PASS" if ok else "FAIL", name, decision, bool(why)))
            failures += 0 if ok else 1
            count += 1
        sibling = ("Write", {"file_path": os.path.join(gate.STOP_GATE_STATE_ROOT + "-other", "a.json"), "content": "{}"})
        ok = gate.gate_change(sibling[0], sibling[1], settings) is None
        print("{0}  {1:<64} gate change: {2}".format(
            "PASS" if ok else "FAIL", "control: a sibling of the Stop gate state is no gate change", not ok))
        failures += 0 if ok else 1
        count += 1
        slot = ("Write", {"file_path": os.path.join(claude_temp.gate_slot_root(), "notes.txt"), "content": "x"})
        ok = gate.gate_change(slot[0], slot[1], settings) is None
        print("{0}  {1:<64} gate change: {2}".format(
            "PASS" if ok else "FAIL", "control: a file beside the gate records, not in them, is no gate change", not ok))
        failures += 0 if ok else 1
        count += 1
        sibling = ("Write", {"file_path": os.path.join(gate.TRANSCRIPT_INDEX_ROOT + "-other", "a.json"), "content": "{}"})
        ok = gate.gate_change(sibling[0], sibling[1], settings) is None
        print("{0}  {1:<64} gate change: {2}".format(
            "PASS" if ok else "FAIL", "control: a sibling of the transcript index is no gate change", not ok))
        failures += 0 if ok else 1
        count += 1
        share = {"server": "WIN-VL9HOKP0RAM", "share": "crgolden-state", "serverPath": "D:\\crgolden-state",
                 "drive": "Z", "users": ["Administrator", "crgol"]}
        state = "D:\\crgolden-state\\WIN-VL9HOKP0RAM\\claude\\claude-hooks.quarantine"
        resolver_rows = [
            ("must-block: a share path in its local spelling is under the state root",
             gate.under(state + "\\a.json", state), True),
            ("must-block: the same path through the mapped drive Z: is under it",
             gate.under("Z:\\WIN-VL9HOKP0RAM\\claude\\claude-hooks.quarantine\\a.json", state), True),
            ("must-block: the same path in its UNC spelling is under it",
             gate.under("\\\\WIN-VL9HOKP0RAM\\crgolden-state\\WIN-VL9HOKP0RAM\\claude\\claude-hooks.quarantine\\a.json",
                        state), True),
            ("control: a sibling of the share is not under it",
             gate.under("D:\\crgolden-state-other\\WIN-VL9HOKP0RAM\\claude\\claude-hooks.quarantine\\a.json", state),
             False),
            ("control: a disabled machine keeps its state in the raw temp root",
             claude_temp.share_root(dict(share, enabled=[]), "WIN-VL9HOKP0RAM", "Administrator"), None),
            ("an enabled server reaches the share's one shared folder through its local path",
             claude_temp.share_root(dict(share, enabled=["WIN-VL9HOKP0RAM"]), "WIN-VL9HOKP0RAM", "Administrator"),
             "D:\\crgolden-state"),
            ("an enabled desktop reaches the same shared folder through the mapped drive",
             claude_temp.share_root(dict(share, enabled=["DESKTOP-HO4IRUI"]), "DESKTOP-HO4IRUI", "crgol"), "Z:\\"),
            ("must-block: each account keeps its own cutover switch, so one account's cutover never moves another",
             claude_temp.cutover_folder(dict(share, enabled=["WIN-VL9HOKP0RAM"]), "WIN-VL9HOKP0RAM", "Administrator")
             != claude_temp.cutover_folder(dict(share, enabled=["WIN-VL9HOKP0RAM"]), "WIN-VL9HOKP0RAM", "crgol"), True),
            ("must-block: the gate slot is never on the share, whatever the share enables",
             gate.under(claude_temp.gate_slot_root(), "D:\\crgolden-state")
             or gate.under(claude_temp.gate_slot_root(), "Z:\\"), False),
            ("must-block: in the shared folder a gate record folder carries its machine, so two desktops' same-path "
             "checkouts never collide",
             claude_temp.checkout_key("C:\\Users\\crgol\\source\\repos\\crgolden\\Tools", "DESKTOP-HO4IRUI", flat=True)
             != claude_temp.checkout_key("C:\\Users\\crgol\\source\\repos\\crgolden\\Tools", "DESKTOP-STFVL2F", flat=True),
             True),
            ("control: an account not yet on the shared folder keeps its unprefixed record key, so landing re-gates "
             "nothing",
             claude_temp.checkout_key("C:\\Users\\crgol\\source\\repos\\crgolden\\Tools", "DESKTOP-HO4IRUI", flat=False),
             "c--users-crgol-source-repos-crgolden-tools"),
            ("control: an account outside the share's users keeps its own temp root",
             claude_temp.share_root(dict(share, enabled=["WIN-VL9HOKP0RAM"]), "WIN-VL9HOKP0RAM", "crgolden-triage"),
             None),
            ("must-block: an enabled machine with no share root gets a verdict naming the fix",
             "remove this machine" in (claude_temp.share_problem(dict(share, drive="Q", enabled=["DESKTOP-X"]),
                                                                  "DESKTOP-X") or ""), True),
        ]
        for name, got, expected in resolver_rows:
            ok = got == expected
            print("{0}  {1:<64} got={2}".format("PASS" if ok else "FAIL", name, got))
            failures += 0 if ok else 1
            count += 1
        raw = os.path.join(os.environ.get("CLAUDE_CODE_TMPDIR") or os.environ["TEMP"], "claude")
        share = claude_temp.share_root()
        on_share = share and claude_temp.account_temp(claude_temp.root())
        legacy = claude_temp.legacy_folder()
        claude_root = (os.path.join(share, "claude") if on_share and claude_temp.cut_over(claude_temp.cutover_folder())
                       else os.path.join(legacy, "claude") if on_share and claude_temp.legacy_cut_over(legacy)
                       else raw)
        ok = gate.under(gate.STOP_GATE_STATE_ROOT, claude_root) and gate.under(gate.QUARANTINE_ROOT, claude_root)
        print("{0}  {1:<64} {2}".format(
            "PASS" if ok else "FAIL", "control: the Stop gate state and quarantine sit under this account's state "
            "root (%TEMP%\\claude, its per-machine share folder, or the shared folder once cut over)",
            gate.STOP_GATE_STATE_ROOT))
        failures += 0 if ok else 1
        count += 1
        os.makedirs(gate.QUARANTINE_ROOT, exist_ok=True)
        short_root = short_path(gate.QUARANTINE_ROOT)
        if short_root == gate.QUARANTINE_ROOT:
            print("NOTE  the quarantine folder has no distinct 8.3 spelling here; the 8.3 row repeats the long one")
        short_record = ("Write", {"file_path": os.path.join(short_root, "a-session.json"), "content": "{}"})
        ok = gate.gate_change(short_record[0], short_record[1], settings) is not None
        print("{0}  {1:<64} gate change: {2}".format(
            "PASS" if ok else "FAIL", "must-block: a quarantine write in 8.3 spelling still ejects", ok))
        failures += 0 if ok else 1
        count += 1
        hook_edit = ("Edit", {"file_path": os.path.join(checkout, "Hooks", "invoke_stop_gate.py")})
        ok = gate.gate_change(hook_edit[0], hook_edit[1], settings) is None
        print("{0}  {1:<64} gate change: {2}".format(
            "PASS" if ok else "FAIL", "control: a main-checkout hook edit is denied but never ejects", not ok))
        failures += 0 if ok else 1
        count += 1
        # The reason's worktree command, with the session id filled in as it says, must pass the PreToolUse naming
        # rule that judges git worktree add: a path left as %USERPROFILE% never would.
        import invoke_pretooluse_gate as pretool  # noqa: E402
        _, reason = gate.evaluate(*hook_edit, checkout=checkout, settings=settings)
        session_id = "11111111-2222-3333-4444-555555555555"
        start = reason.index("git -C ")
        command = reason[start:reason.index(" origin/main", start) + len(" origin/main")].replace("<session-id>", session_id)
        found = pretool.session_branch_violation(command, session_id, os.environ.get("USERPROFILE", ""))
        ok = found is None and "%USERPROFILE%" not in reason
        print("{0}  {1:<64} {2}".format(
            "PASS" if ok else "FAIL", "the reason's worktree command, id filled in, passes the naming rule", found))
        failures += 0 if ok else 1
        count += 1
        for name, ok, detail in (temp_resolver_rows(base) + legacy_temp_rows(base) + workspace_doc_rows(base)
                                  + share_rows(base, settings)):
            print("{0}  {1:<64} {2}".format("PASS" if ok else "FAIL", name, detail))
            failures += 0 if ok else 1
            count += 1
    finally:
        os.rmdir(skill_link)
        shutil.rmtree(base)
    print("")
    print("{0} case(s) in {1:.2f}s".format(count, time.time() - started))
    if failures == 0:
        print("All main-checkout-gate cases passed.")
        return 0
    print("{0} case(s) failed.".format(failures))
    return 1


if __name__ == "__main__":
    sys.exit(main())
