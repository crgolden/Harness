"""PreToolUse gate: hold every shell call that runs a quality check to the protocol in Harness/AGENTS/GATES.md.

One concern, registered beside invoke_pretooluse_gate.py in the same PowerShell matcher entry. It decides
only whether a command that runs a check may run now; every other command defers.

  deny  a gate step issued on its own (dotnet test, npm run lint, pytest, jb inspectcode ...). A gate is one script.
        A plain build of a path (dotnet build <path>, npm run build --prefix <path>) is no gate step: it is the
        proof a gate needs.
  deny  a repo's own checked-in gate.ps1, in a checkout with a build (a .sln/.slnx, or a package.json build
        script), unless this session ran a foreground plain build of a path inside that checkout that came back
        without an error, and no non-markdown file in the checkout changed after it. The gate is not the compiler.
        A script whose own file name is a gate step (Get-CodeStyleCatalog.ps1, Get-CommentCatalog.ps1) is that
        step issued on its own, whatever its body names.
  deny  a gate script narrowed to a test name or file, unless its code declares the delta it is narrowed to
        (GATE_DELTA = [...] / $GateDelta = @(...)) and every entry is a file this session edited after its
        previous gate, or a plant: file the script itself writes and restores.
  deny  a gate script naming a workspace repo that has its own checked-in gate.ps1, unless it is that file or a
        narrowed run whose declared delta the rule above verifies: a targeted check stands in for no full gate.
  deny  a gate script that leaves out a gate step its repo's workflow runs on the ordinary trigger (a verified
        narrowed run excepted, for the same reason).
  deny  a repo's own checked-in gate.ps1 whose Register-GateSteps names a step with no Register-StepInputs entry,
        or whose Register-StepInputs names a step it does not register; the reason shows the form to add.
  deny  a repo's own checked-in gate.ps1 that calls Resolve-OwedSteps, launched with any -Steps: it works out its
        own steps. Any other checked-in gate.ps1 launched with no -Steps, an empty one, or one naming a step the
        gate does not have; the reason lists the gate's steps. GateCommon decides whether each named step qualifies.
  deny  a probe (a checked-in gate.ps1 given -Probe '<step>,...') naming a step its gate.ps1 does not register, or
        one with no declared input this session edited, or while the machine is busy. A probe is not a gate: no
        goal, work-product, build or unchanged-tree check applies, it records no verdict, and a later gate never
        counts it as a previous launch.
  deny  a repo's own checked-in gate.ps1 whose checkout holds no uncommitted change: a gate never runs on a clean
        repo (GateCommon refuses it too).
  deny  a repo's own checked-in gate.ps1 while its most recent gate record PASSED on exactly the current inputs
        (Harness/Gates/gate_status.py reports GREEN-CURRENT).
  deny  a gate script with no GOAL: line restating the goal the operator pinned, when one is pinned.
  deny  a gate while this session already has one in flight.
  deny  a gate while another session on this machine has one in flight. An ask would put that judgment on
        the operator, who cannot see another session's processes.
  deny  a gate while a process holds this account's gate slot's file lock (Harness/Gates/GateSlot.ps1, under
        %TEMP%\\claude): the slot is how a gate launched outside any Claude Code session is seen at all.
  ask   a gate when no non-markdown file has been edited in this session since its previous gate.
  ask   a gate that passes every check above while a goal is pinned: restating the goal is the session's own
        claim that the work product is finished, which only the operator can confirm.

State comes from transcripts, the same record walk as invoke_agents_doc_gate.py. A background task is in
flight from the tool_result carrying `backgroundTaskId` until a `<task-notification>` names its task id.
The session's subagent transcripts are read with it, because a subagent's edits live in their own files.
A `jb cleanupcode`, a `ruff format` without --check/--diff, or a `ruff check --fix` whose only --select is
import sorting (I) and unused imports (F401) rewrites files, so it counts as an edit, not a gate step. The
pinned goal is read with
invoke_stop_gate.py's own patterns, so the two hooks cannot disagree about what a pin is.

An unreadable payload, transcript or script defers. A GATE command is never let through by the hook
failing to judge it: an exception denies, and a hang is bounded by Claude Code's hook timeout under the
dispatcher entry's onFailure "block", so the hook keeps no deadline of its own. Each transcript's parsed state
is cached with its byte offset under the temp root, so a call reads only what was appended since the last one;
transcripts only ever grow.
"""

import fnmatch
import glob
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime

DENY = "deny"
ASK = "ask"
DEFER = "defer"

INLINE = "inline"
SCRIPT = "script"

PENDING = "pending"
BACKGROUND = "background"
FINISHED = "finished"

CI = re.IGNORECASE

GATE_STEP_PATTERNS = [
    re.compile(r"\bdotnet\s+(build|test|format|coverlet|dotnet-coverage|stryker|sonarscanner)\b(?!-)", CI),
    re.compile(r"\bdotnet-coverage\s+collect\b", CI),
    re.compile(r"\bnpm\s+(test|t)(\s|$)", CI),
    re.compile(r"\bnpm\s+run(-script)?\s+(?!e2e:synthetic\b)(lint|typecheck|test|e2e|build|check)[\w:.-]*", CI),
    re.compile(r"\bnpx\s+(vitest|tsc|eslint|stylelint|playwright\s+test|ng\s+(lint|test|build)|astro\s+check)\b", CI),
    re.compile(r"\bvitest(\.cmd)?\s+run\b", CI),
    re.compile(r"(^|\s)ng\s+(lint|test|build)\b", CI),
    re.compile(r"\bpytest(?![\w-])", CI),
    re.compile(r"\bmypy\s", CI),
    re.compile(r"\bruff\s+(check|format)\b", CI),
    re.compile(r"\bpoetry\s+check\b", CI),
    re.compile(r"(?<![\w\\/.-])sonar-scanner(\.bat)?(?![\w.-])|[\\/]bin[\\/]sonar-scanner(\.bat)?(?![\w.-])", CI),
    re.compile(r"\bjb(\.exe)?\s+inspectcode\b", CI),
    re.compile(r"Get-(CodeStyle|Comment)Catalog\.ps1", CI),
    re.compile(r"\.Tests(\.\w+)*\.exe\b", CI),
]
RE_GATE_STEP = re.compile("|".join("(?:{0})".format(pattern.pattern) for pattern in GATE_STEP_PATTERNS), CI)

EXEMPT_MARKER = "GATE-CADENCE: gate-step-strings-are-data"

RE_PACKAGE_INSTALL = re.compile(
    r"\b(?:py(?:thon)?3?(?:\.exe)?\s+-m\s+)?pip3?(?:\.exe)?\s+install\b[^\r\n|;&)]*", CI)

RE_READ_ONLY_HEAD = re.compile(
    r"^\s*(git(\s+-C\s+(\"[^\"]+\"|\S+))?\s+(diff|show|log|blame|grep|status|ls-files)\b"
    r"|Get-Content\b|Select-String\b|Get-Item\b|Get-ChildItem\b|Get-FileHash\b|rg\b|findstr\b|type\b|cat\b)", CI)
RE_INVOKES_WHAT_IT_READS = re.compile(r"[|;&]|\bInvoke-Expression\b|\biex\b|\bpwsh\b|\bpython\b|\bnode\b", CI)
RE_HARNESS_NAME = re.compile(r"(^|[\\/])(test_\w+\.py|Test-[\w-]+\.ps1)$", CI)
RE_SCRIPT_CALL = re.compile(r"^\s*(?:pwsh\s+-File|python(?:\s+-[A-Za-z]\S*)*)\s+(?P<path>\"[^\"]+\"|[^\s\"]+)", CI)
RE_GATE_TARGET = re.compile(r"(?:^|[\\/\"'\s=(,])(?:gate\.ps1|test_\w+\.py|Test-[\w-]+\.ps1)\b", CI)
RE_PS_RUNNER = re.compile(
    r"(?:^|[\s;=(|{])&\s*\S|\bpwsh(?:\.exe)?\b|\bpowershell(?:\.exe)?\b|\bpython[0-9.]*(?:\.exe)?\b"
    r"|\bStart-Process\b|\bInvoke-Expression\b|\biex\b", CI)
RE_PY_ARGV_RUNNER = re.compile(r"\bsys\.executable\b|[\"'](?:pwsh|powershell|python)(?:\.exe)?[\"']", CI)
RE_PY_LAUNCH = re.compile(r"\bsubprocess\.\w+\s*\(|\bPopen\s*\(|\bos\.(?:system|popen|exec\w*|spawn\w*)\s*\(|\brunpy\.", CI)
RE_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
RE_COMPILE_ONLY = re.compile(r"\bpy_compile\b|\bcompileall\b", CI)
RE_ASSIGNED_NAME = re.compile(r"^\s*\$?(?P<name>[A-Za-z_]\w*)\s*=(?!=)", CI)
RE_PROCESS_WAIT = re.compile(
    r"\bWait-Process\b|\bWaitFor(?:SingleObject|MultipleObjects)(?:Ex)?\b|\bMsgWaitForMultipleObjects(?:Ex)?\b"
    r"|\bos\.waitpid\s*\(|\b(?:GetProcessById|Get-Process)\b[^\n]*\.WaitForExit(?:Async)?\s*\(", CI)
RE_PROCESS_LOOKUP = re.compile(r"^\s*\$?(?P<name>[A-Za-z_]\w*)\s*=(?!=)[^\n]*\b(?:GetProcessById|Get-Process)\b", CI | re.M)
CLI_ARGUMENT_GAP = r"(?:[\"']\s*,\s*[\"']|[\"']?\s+[\"']?)"
RE_CLI_WAIT = re.compile(
    r"\bgh(?:\.exe)?" + CLI_ARGUMENT_GAP + r"run" + CLI_ARGUMENT_GAP + r"watch\b"
    r"|\bgh(?:\.exe)?" + CLI_ARGUMENT_GAP + r"pr" + CLI_ARGUMENT_GAP + r"checks\b[^\n]*?[\s\"']--watch\b"
    r"|\baz(?:\.cmd)?(?:" + CLI_ARGUMENT_GAP + r"[a-z][\w-]*)*?" + CLI_ARGUMENT_GAP + r"wait(?![\w-])", CI)
RE_CLI_WAIT_COMMAND = re.compile(
    r"^\s*(?:&\s*)?(?:cmd(?:\.exe)?\s+/c\s+)?(?:\"[^\"]*[\\/]|'[^']*[\\/]|[^\s\"']*[\\/])?"
    r"(?:gh(?:\.exe)?[\"']?\s+(?:run\s+watch\b|pr\s+checks\b[^\n]*?\s--watch\b)"
    r"|az(?:\.cmd)?[\"']?(?:\s+[a-z][\w-]*)*?\s+wait(?![\w-]))", CI)
RE_STATUS_POLL = re.compile(
    r"^\s*(?:python(?:\.exe)?\s+(?:-[A-Za-z]\s+)*[\"']?[^\"'\s]*(?:gate_status|hook_status)\.py\b"
    r"|pwsh(?:\.exe)?\s+-File\s+[\"']?[^\"'\s]*GateSlot\.ps1[\"']?\s+-Status\b"
    r"|Get-ScheduledTaskInfo\b)", CI)
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")
INPUT_ATTACHMENTS = ("queued_command",)
NO_INPUT_ORIGINS = (None, "auto-continuation")
POLL_TAIL_BYTES = 4 * 1024 * 1024
RE_SLEEP_CALL = re.compile(
    r"\btime\.sleep\s*\(|\basyncio\.sleep\s*\(|\bStart-Sleep\b|\bWait-Job\b|\bWait-Event\b|\bThread\]?::Sleep\b"
    r"|\bThread\.Sleep\b|\bTask\]?::Delay\b|(?<![\w.-])sleep\s+(?=\d)|(?<![\w.])sleep\s*\(|\bEvent\(\)\.wait\s*\("
    r"|\btimeout(?:\.exe)?\s+/t\b|\bping(?:\.exe)?\s+(?:-n|/n)\s+\d", CI)
RE_LITERAL_PAUSE = re.compile(
    r"^\s*\(?\s*(?:-(?P<unit>Milliseconds|ms|Seconds|s)\s+)?(?P<amount>\d+(?:\.\d+)?)\s*\)?\s*$", CI)
RE_IMPORT_LINE = re.compile(r"^\s*(?:import\s|from\s+\S+\s+import\s)", CI)
SETTLE_PAUSE_SECONDS = 5
RE_WATCHED_STATE = re.compile(
    r"GATE:|gate_status|gate\.ps1|\.last\.json|crgolden-gates|GateSlot|claude-stopgate|\.output\b|[\\/]tasks[\\/]"
    r"|gateresults|gatejobs|\.gate\.log|test_\w+\.py|Test-[\w-]+\.ps1", CI)
RE_LOOP = re.compile(r"^\s*(?:while\b|for\b|do\s*\{|until\b)|\b(?:while|foreach|for)\s*\(|\bdo\s*\{", CI | re.MULTILINE)
RE_GIT_LAUNCH = re.compile(r"[\[(]\s*[\"']git(?:\.exe)?[\"']", CI)
RE_GATE_STEPS = re.compile(r"Register-GateSteps\s+@\((?P<body>.*?)\)[ \t]*\r?$", re.IGNORECASE | re.DOTALL | re.MULTILINE)
RE_STEP_INPUTS = re.compile(r"Register-StepInputs\s+@\{(?P<body>.*?)^\s*\}", re.IGNORECASE | re.DOTALL | re.MULTILINE)
RE_STEP_INPUT_KEY = re.compile(r"^\s*'(?P<name>[^']+)'\s*=", re.MULTILINE)
RE_STEP_NAME = re.compile(r"'([^']+)'")
RE_GOAL_ARGUMENT = re.compile(r"\s-Goal\s+(?:'(?P<single>(?:[^']|'')*)'|\"(?P<double>(?:[^\"`]|\"\"|`.)*)\")", CI)
RE_TESTS_ARGUMENT = re.compile(r"\s-Tests\s+(?:'(?P<single>[^']*)'|\"(?P<double>[^\"]*)\"|(?P<bare>[^\s'\"]+))", CI)
RE_STEPS_ARGUMENT = re.compile(r"\s-Steps\s+(?:'(?P<single>[^']*)'|\"(?P<double>[^\"]*)\"|(?P<bare>[^\s'\"]+))", CI)
RE_PROBE_ARGUMENT = re.compile(r"\s-Probe\s+(?:'(?P<single>[^']*)'|\"(?P<double>[^\"]*)\"|(?P<bare>[^\s'\"]+))", CI)
RE_STEP_INPUT_ROW = re.compile(r"^\s*'(?P<name>[^']+)'\s*=\s*@\((?P<list>[^)]*)\)", re.MULTILINE)
RE_SELECTED_RUNNER = re.compile(r"\bInvoke-SelectedTests\b", CI)
RE_TEST_METHOD_NAME = re.compile(r"^[A-Za-z_][\w.+]*$")
RE_NARROWED_RUN = re.compile(
    r"--grep\b|--testNamePattern\b|--filter-(class|method|namespace|uid|query)\b|FullyQualifiedName"
    r"|\bvitest(\.cmd)?['\"]?\s+run\b[^\r\n]*?(\s-t\s|[\w./\\-]+\.(spec|test)\.[jt]sx?\b)"
    r"|\bplaywright\s+test\b[^\r\n]*?[\w./\\-]+\.(spec|test)\.[jt]sx?\b"
    r"|\bpytest\b[^\r\n]*?(\s-k\s|[\w./\\-]+\.py\b)"
    r"|\.Tests(\.\w+)*\.exe\b[^\r\n]*?\s-(method|class)\s"
    r"|\binspectcode\b[^\r\n]*?\s--(include|project)\b",
    CI)
RE_HARNESS_NARROWED = re.compile(r"\s(--only|-Only|-Rule)\b", CI)
RE_HARNESS_SELECTION = re.compile(r"\s(?:--only|-Only|-Rule)\s+(?P<rest>.*)$", CI | re.DOTALL)
# A test step that runs a whole tier: a trait/category sweep, or a runner invoked with no test named.
# Used only on a RE-RUN, where a baseline already exists and the reachable set is knowable.
RE_FULL_TEST_RUN = re.compile(
    r"--filter-trait\b"
    r"|\bdotnet\s+test\b(?![^\r\n]*--filter-(class|method|namespace|uid|query))"
    r"|\bnpm\s+run\s+test(:\w+)?\b(?![^\r\n]*[\w./\\-]+\.(spec|test)\.[jt]sx?)"
    r"|\bnpm\s+run\s+e2e(:\w+)?\b(?![^\r\n]*(--grep|[\w./\\-]+\.(spec|test)\.[jt]sx?))"
    r"|\bvitest(\.cmd)?['\"]?\s+run\b(?![^\r\n]*(\s-t\s|[\w./\\-]+\.(spec|test)\.[jt]sx?))"
    r"|\bplaywright\s+test\b(?![^\r\n]*(--grep|[\w./\\-]+\.(spec|test)\.[jt]sx?))",
    CI)
RE_TEST_FILE = re.compile(
    r"(^|[\\/])(test_\w+\.py|[\w.-]*Tests?\.cs|[\w./\\-]+\.(spec|test)\.[jt]sx?)$", CI)
RE_GATE_DELTA = re.compile(r"(?:\bGATE_DELTA\s*=\s*\[|\$GateDelta\s*=\s*@\()(?P<body>.*?)[\]\)]", re.DOTALL)
RE_QUOTED = re.compile(r"\"([^\"]+)\"|'([^']+)'")
RE_WRITE_CALL = re.compile(r"\.write_text\(|\.write_bytes\(|\bopen\([^\n]*[\"'][wa][\"']|Set-Content\b|Out-File\b|\[IO\.File\]::Write", CI)
PLANT_PREFIX = "plant:"
RE_VARIABLE_STEP = re.compile(r"\b(npm\s+run|dotnet)\s+\$", CI)
RE_GATE_LIBRARY = re.compile(r"GateCommon\.ps1|GATE_LIBRARY", CI)
RE_GATE_LIBRARY_VARIABLE = re.compile(r"\$([\w:]+)\s*=[^=\n][^\n]*(?:GateCommon\.ps1|GATE_LIBRARY)", CI)
RE_SCRIPT_LOAD = re.compile(r"^\s*(?:\.|Import-Module)\s+(?P<target>[^\n]+)", CI | re.M)
RUN_TARGET_SEPARATOR = r"[\"']?(?:\s*,\s*|\s+)"
RUN_TARGET_FLAGS = r"(?:[\"']?-[^\s,\"']+[\"']?(?:\s*,\s*|\s+))*"
RUN_TARGET_TEMPLATE = (
    r"(?:&\s*(?:[\"']?[^\s\"']*(?:python|pwsh|powershell|py)[\w.]*" + RUN_TARGET_SEPARATOR + RUN_TARGET_FLAGS + r")?"
    r"|(?<![\w$-])(?:python|pwsh|powershell|py)[\w.]*" + RUN_TARGET_SEPARATOR + RUN_TARGET_FLAGS
    + r"|sys\.executable\s*,\s*" + RUN_TARGET_FLAGS + r"|-File(?:Path)?\s+)[\"'(]*\$?{name}\b")
RE_BLOCK_COMMENT = re.compile(r"<#.*?#>", re.DOTALL)
RE_LINE_COMMENT = re.compile(r"^\s*(#|//|REM\s)", CI)
RE_WORKSPACE_REPO = re.compile(r"crgolden[\\/]+([A-Za-z0-9_.-]+)", CI)
RE_PLAIN_BUILD = re.compile(
    r"^\s*(?:dotnet\s+build\s+(?P<dotnet>\"[^\"]+\"|[^\s\"-][^\s\"]*)"
    r"(?:\s+(?!-t\b|-target\b|--target\b)-{1,2}[\w:.-]+(?:[=\s](?!-)(?:\"[^\"]*\"|[^\s\"-][^\s\"]*))?)*"
    r"|npm\s+(?:--prefix\s+(?P<before>\"[^\"]+\"|[^\s\"]+)\s+)?run(?:-script)?\s+build"
    r"(?:\s+--prefix\s+(?P<after>\"[^\"]+\"|[^\s\"]+))?)\s*$", CI)
RE_EDITING_COMMAND = re.compile(
    r"\bjb(\.exe)?\s+cleanupcode\b"
    r"|\bruff\s+format\b(?![^\r\n]*\s--(check|diff)\b)[^\r\n]*"
    r"|\bruff\s+check\b(?=[^\r\n]*\s--fix\b)(?=[^\r\n]*\s--select[=\s]+(I\d*|F401)(,(I\d*|F401))*(\s|$))"
    r"(?![^\r\n]*\s--(diff|no-fix|unsafe-fixes|extend-select|statistics|exit-non-zero-on-fix)\b)"
    r"(?![^\r\n]*\s--select\b[^\r\n]*\s--select\b)[^\r\n]*",
    CI)

TOOLS_ROOT =os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPERATIONAL_PROBES_CHECKOUT = "Tools"
OPERATIONAL_PROBES = (
    ("Claude", "skills", "version-audit", "scripts", "Test-VmService.ps1"),
    ("Seeding", "Test-Irs990Coverage.ps1"),
)
RE_PULL_REQUEST_TRIGGER = re.compile(r"^\s{2}pull_request:", re.MULTILINE)
RE_PUSH_TRIGGER = re.compile(r"^\s{2}push:", re.MULTILINE)
RE_OFF_PULL_REQUEST = re.compile(r"workflow_dispatch|schedule|!=\s*'pull_request'|==\s*'push'|github\.ref\s*==", CI)
RE_OFF_PUSH = re.compile(r"workflow_dispatch|schedule|!=\s*'push'|==\s*'pull_request'", CI)
RE_TEST_PROJECT = re.compile(r"(?<![\w.])([A-Za-z]\w*(?:\.\w+)*?\.Tests\.\w+)")
RE_TRAIT = re.compile(r"Category=(\w+)")
RE_NPM_SCRIPT = re.compile(r"\bnpm\s+run(?:-script)?\s+([\w:.-]+)", CI)
RE_NPM_TEST = re.compile(r"\bnpm\s+(?:test|t)(?:\s|$)", CI)
NPM_TEST_IDENTITY = r"\bnpm\s+(?:test|t)\b(?![\w:.-])"
TOOL_IDENTITIES = [
    (re.compile(r"\bruff\s+check\b", CI), r"\bruff\s+check\b"),
    (re.compile(r"\bruff\s+format\b", CI), r"\bruff\s+format\b"),
    (re.compile(r"\bpoetry\s+check\b", CI), r"\bpoetry\s+check\b"),
    (re.compile(r"\bmypy\b", CI), r"\bmypy\b"),
    (re.compile(r"\bpytest\b", CI), r"\bpytest\b"),
    (re.compile(r"\bvitest\b", CI), r"\bvitest\b"),
    (re.compile(r"\bplaywright\s+test\b", CI), r"\bplaywright\b"),
    (re.compile(r"\bstryker\b", CI), r"\bstryker\b"),
    (re.compile(r"\bdotnet\s+build\b", CI), r"\bdotnet\s+build\b"),
    (re.compile(r"\bdotnet\s+format\b", CI), r"\bdotnet\s+format\b"),
    (re.compile(r"\bjb(\.exe)?\s+inspectcode\b", CI), r"\bjb(\.exe)?\s+inspectcode\b"),
]
RE_TOKEN_PUNCTUATION = re.compile(r"[\"',\[\]]")
RE_TASK_ID = re.compile(r"<task-id>([^<]+)</task-id>")
RE_OUTPUT_FILE = re.compile(r"Output is being written to: (.+?\.output)")
RE_HOOK_REFUSAL = re.compile(r"\s*PreToolUse:\w+ hook error:")

EDIT_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
SHELL_TOOLS = {"PowerShell", "Bash"}
LINE_NEEDLES = ('"tool_use"', '"tool_result"', "task-notification")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import session_home  # noqa: E402
import work_product_items  # noqa: E402

TEMP_CLAUDE_ROOT = claude_temp.claude_root()
WORKSPACE = session_home.workspace_root()
SCRIPT_CODE_CACHE = {}

FOREGROUND_LIMIT_SECONDS = 660
BACKGROUND_STALE_SECONDS = 3600
PEER_WINDOW_SECONDS = 6 * 3600

SCAN_CACHE_DIRECTORY = "gate-cadence-cache"
SCAN_VERSION = 5
REQUEST_COPY_TYPE = "api-request-blob"
SCAN_HEAD_BYTES = 4096

OPT_OUT = (
    "A script that runs no gate step and only quotes runner strings as data opts out of this gate with the "
    "line " + EXEMPT_MARKER + " anywhere in its body."
)

INLINE_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md): this command is a gate step run on its own. "
    "Before the work product is finished the number of checks is zero, except one plain foreground build of the "
    "checkout (dotnet build \"<path>\" or npm run build --prefix \"<path>\"), which a repo with a build requires "
    "before its gate. A targeted, cheap or single-spec run is still a gate step, and so is a run to exercise test "
    "code you just wrote. When every edit in the work product is made, run the repo's checked-in gate as one bare "
    "command: pwsh -File \"<workspace>\\<Repo>\\gate.ps1\" -Goal \"<the pinned goal, verbatim>\". Do not write a "
    "script of gate steps instead: one that names a repo with its own gate.ps1 is refused too. If you ran this to "
    "find what your change broke, that is a search done with a compiler: find references with Grep across every "
    "project, test projects included, and the gate reports whatever the search missed. If this command runs no "
    "check and only names a step as data (a step name quoted in a probe, or a harness path it reads), put it in a "
    "script whose body carries the line " + EXEMPT_MARKER + " and run that script with pwsh -File or python: the "
    "marker takes it out of this gate."
)

MISSING_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md): this gate script leaves out steps its repo's CI runs. "
    "Missing: {missing}. A gate is the whole CI step list in one script; a script running part of it is a "
    "check before the work product is finished, whatever an earlier run reported. A step the machine cannot "
    "fit stays in the script and is reported NOT RUN, so the gate reads INCOMPLETE: it is never finished "
    "later by a script of its own, and its skip threshold is never lowered so that it runs. Name every step "
    "in the script's code, not only in a comment. " + OPT_OUT
)

STEPS_ARGUMENT_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Re-running after a red or incomplete gate\"): a gate "
    "runs the steps its caller names, and {problem}. Pass them as one comma-separated -Steps argument: "
    "pwsh -File \"{path}\" -Steps '<step>,<step>' (every step to run all of them). Its steps: {steps}. "
    "Name exactly the steps whose inputs changed since their last pass, or that last failed or never passed: "
    "GateCommon fails the gate before anything runs when a named step passed on exactly these inputs (it would "
    "return the same green) or an unnamed one did not, and its error lists which."
)

CLEAN_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md): {checkout} has no uncommitted changes, and a gate never runs "
    "on a repo with no uncommitted changes. A change is gated while it is uncommitted; once it is checked in and "
    "pushed, gate_status.py reads it CURRENT-IMPLICIT, which stands in for a gate run. For its verdict, run "
    "python \"<workspace>\\Harness\\Gates\\gate_status.py\" <Repo> and cite the line it prints instead of gating."
)

BUILD_MISSING_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Build before the gate\"): {checkout} has no successful build "
    "in this session. A gate is never the first compile of a change: build the repo on its own first, as one "
    "foreground command naming its path ({example}), fix every error and warning-as-error it reports, then gate. "
    "A background build is no proof, because its call returns before the build ends."
)

BUILD_STALE_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Build before the gate\"): {checkout} changed after this "
    "session's last successful build of it ({at}), so that build is no proof the change compiles. Build it again "
    "({example}), fix what it reports, then gate."
)

OWED_STEPS_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Re-running after a red or incomplete gate\"): this gate "
    "works out its own steps from its per-step records (GateCommon's Resolve-OwedSteps) and takes no -Steps "
    "argument. Run it without one: pwsh -File \"{path}\" -Goal \"<the pinned goal, verbatim>\"."
)

STEP_INPUTS_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Re-running after a red or incomplete gate\"): {repo}'s "
    "gate.ps1 does not declare the inputs of every step it registers, and GateCommon fails such a gate at its "
    "first line. {problems}. Every name in Register-GateSteps needs an entry, spelled exactly as registered, "
    "in a Register-StepInputs block right after it: Register-StepInputs @{{ '<step>' = @('src/*', "
    "'package.json') ... }}. A pattern is -like against repo-relative forward-slash paths and must match a "
    "file the gate fingerprints (markdown and .github/ never do); use @('*') for a step that reads the whole "
    "repo or whose result another step writes (a build, coverage feeding Sonar, anything reading dist/)."
)

CHECKED_IN_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Every repo carries its own checked-in gate\"): this "
    "script gates a repo that has its own gate.ps1, which is the single definition of that repo's step list. "
    "Run the checked-in gate instead: {commands}. Leave out -Goal when no goal is pinned. Absence from git "
    "status is not absence of the file: a committed gate.ps1 never shows there. " + OPT_OUT + " That marker "
    "is the only way past this denial: making the same change through another tool is working around the "
    "gate, and a denial believed to be a misfire is reported to the operator."
)

GOAL_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md): the operator pinned this session's goal, and this "
    "command does not carry it as a -Goal argument: \"{goal}\". A gate runs once the work product is "
    "finished, which this hook judges by your work-product declaration: every item it lists for the repo "
    "done. A goal that repeats every cycle never finishes, so it is the declared items that decide. If an "
    "edit the work needs is not made, make it and do not gate: a gate for part of it is a check before the "
    "work product is finished. A test harness (test_*.py, Test-*.ps1, or a script that runs one) is a gate "
    "too and runs only as a step of the repo's gate, or alone through that gate's -Probe '<step>' when the step "
    "reads a file this session edited (GATES.md, \"Probing one step\"). Then run the repo's checked-in gate as one bare command "
    "carrying the goal: pwsh -File \"<workspace>\\<Repo>\\gate.ps1\" -Goal \"<that goal, verbatim>\". "
    "The match ignores case and every character that is not a letter or digit, so leave out any ; | & or "
    "backtick in the goal (the command line may not carry one) and double each double quote inside it; an "
    "apostrophe is fine inside the double quotes. A GOAL: line inside a script satisfies nothing, and a script "
    "that runs a gate for you is refused."
)

WRAPPER_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Every repo carries its own checked-in gate\"): {script} "
    "runs a gate or a harness ({target}) from inside a script. A wrapper hides the gate from the peers and "
    "hooks that coordinate gates by its command line, and from the transcript. Run the repo's checked-in gate "
    "as one bare command: pwsh -File \"<workspace>\\<Repo>\\gate.ps1\" -Goal \"<the "
    "pinned goal>\", announced to peers. No marker exempts a script that invokes a gate."
)

WAIT_REASON = (
    "Blocked: {script} waits ({call}). A script that sleeps or blocks on a process, a file, a CI run or a peer's gate "
    "exists to pass time, and waiting is never work: it outlasts the Stop gate and the coordination protocol. "
    "Delete the wait from the script and do the next piece of real work instead. A background task you started "
    "notifies this session when it ends, and a peer's gate is reported to you by its FINISHED message. The one "
    "sleep a script may keep is a settle pause for a restarted service: a single literal Start-Sleep -Seconds N "
    "(or time.sleep(N)) with N at most " + str(SETTLE_PAUSE_SECONDS) + ", on a line of its own, outside any loop, "
    "in a script that does other work and reads no gate state."
)

COMMAND_WAIT_REASON = (
    "Blocked: {call} blocks until a CI run or a deployment finishes (gh run watch, gh pr checks --watch, "
    "az ... wait), so the command exists to pass time, and waiting is never work: it outlasts the Stop gate and "
    "the coordination protocol, in the foreground or the background alike. Read the state once (gh run view <id> "
    "--json status,conclusion, gh run list, gh pr checks without --watch, or the az show command) and do the next "
    "piece of real work; read it again only when you have something to act on."
)

REPEATED_POLL_REASON = (
    "Blocked: this status command already ran, and nothing it reports could have changed since: no edit, no other "
    "command, no operator message, peer message or task notification came in between. Polling a status script "
    "passes time the same way a sleep loop does, and every poll burns context. A background task you started "
    "notifies this session when it ends, and a peer's gate is reported by its FINISHED message, so end the turn on a "
    "WAITING: line naming what runs, or do the next piece of real work, and read the status again only once "
    "something has happened."
)

GREEN_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"The gate record\"): {repo}'s most recent gate PASSED at "
    "{at} on {machine}, on exactly the current inputs: `python Harness/Gates/gate_status.py {repo}` reports "
    "GREEN-CURRENT. Nothing a gate reads has changed since, so a re-run can only repeat that result. Cite the "
    "record instead of re-running it."
)

PEER_GREEN_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md): {repo}'s gate PASSED at {at} in the worktree {peer}, after "
    "origin's newest commit ({head}, {head_at}), and that worktree's change is not on origin yet. A peer's green "
    "gate means its change lands next, and a gate run before it lands proves a tree that is about to be stale. "
    "Wait for that session to say its change is pushed, then fetch, move this checkout onto origin's new head "
    "with your changes on top, and gate once there."
)

IN_FLIGHT_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md): this session already has a gate in flight "
    "({command}). While a gate runs the tree is frozen: no second gate, and no edit "
    "to what it is checking. Wait for its completion notification; if it is wedged, stop it with TaskStop "
    "first."
)

NARROWED_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md): this gate narrows a test run to a name or a file "
    "({match}) and its code declares no delta. A narrowed run is a gate step, not a gate, unless the "
    "script's code names the delta it is narrowed to: GATE_DELTA = [...] (or $GateDelta = @(...)) listing "
    "every file this session edited since its previous gate, or plant:<file> for a file the script itself "
    "plants and restores. Every test the delta can reach still runs in full. Before the work product is "
    "finished the number of checks is zero, narrowed or not. " + OPT_OUT
)

HARNESS_SELECTION_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Running only the tests you wrote\"): this harness run "
    "is narrowed ({match}) but {problem}. A harness narrows to the cases this session wrote or changed: the "
    "session must have edited the harness, and every selection term must name a case in it. Otherwise run "
    "the harness whole as a step of its repo's gate: pwsh -File \"<checkout or worktree>\\gate.ps1\" -Goal "
    "\"<the pinned goal, verbatim>\"."
)

DELTA_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md): this gate narrows a test run ({match}) to a declared "
    "delta, but {problem}. A delta entry is a file this session's transcript shows edited after its "
    "previous gate launch, or plant:<file> for a file the script's own code writes and restores; "
    "anything else is a narrowed run with no delta behind it."
)

SELECTED_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Running only the tests you wrote\"): this tests-only run "
    "cannot be verified: {problem}. -Tests names the test methods this session created or modified, comma "
    "separated, bare or fully qualified, with no wildcard, and each one must appear in a test file this "
    "session's transcript shows it editing. A pattern or a name from an untouched file runs tests nobody "
    "changed, which is the full run this mode exists to avoid."
)

PROBE_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Probing one step\"): this probe cannot be verified: "
    "{problem}. -Probe names steps the checkout's gate.ps1 registers, comma separated, and each one must declare "
    "an input (Register-StepInputs) that this session's transcript shows it editing. A probe checks a fix to the "
    "session's own work before the full gate, records no verdict and carries nothing, so a step the session's "
    "edits cannot reach has nothing for it to check."
)

SELECTED_ELSEWHERE_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Running only the tests you wrote\"): this script calls "
    "Invoke-SelectedTests, and the tests-only runner is reached only through a repo's checked-in gate.ps1 with "
    "-Tests, where this hook verifies every name against the session's edits. Run "
    "pwsh -File \"<workspace>\\<Repo>\\gate.ps1\" -Tests '<names>' -TestProject <test project> instead."
)

BLAST_RADIUS_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"A re-run after a baseline narrows by blast "
    "radius\"): this is a RE-RUN, every file in its declared delta is a test file, and it still runs a "
    "whole test tier ({match}). When the edit was in a failing test file, run ONLY the tests that "
    "failed: a test's own fixture or assertions cannot change what a different test observes, so every "
    "other test already has its verdict from the baseline run. \"It only takes a few seconds\" is the "
    "excuse this rule removes, not a justification - a step that runs tests the edit cannot reach "
    "reports a verdict it did not earn, and a cheap step is the easiest place to hide a full re-run. "
    "Narrow the step and assert a floor on the executed count so a filter matching nothing cannot read "
    "as green. " + OPT_OUT
)

# NO peer-notification check lives in THIS hook, and the reason is worth keeping so nobody adds one
# again. evaluate() returns DEFER the moment classify() yields None, which is every command that is not
# a gate step or a gate script. So a check here could only ever fire when the session launches ANOTHER
# gate - never while it is editing, reading or running ordinary commands, which is exactly the busy
# session whose peers stay blocked. Six harness cases proved it: each expected defer for a re-gate after
# an edit and got ask instead, because a finished-but-unreported gate short-circuited a decision about
# something else. The obligation is enforced by invoke_stop_gate.py's fourth veto; catching the busy
# session needs a hook on a matcher that sees ordinary tool calls, which this one is not.

STOPPED_REASON = (
    "Gate protocol (Harness/AGENTS/GATES.md): this session stopped its previous gate before it finished "
    "({command}). Stopping a gate to fold in another fix means the work product was not finished when that "
    "gate started, and while findings are still arriving it stays open. Approve only if that gate was the "
    "wrong script or had hung."
)

PEER_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md): another session on this machine has a gate in flight "
    "({command}, transcript {transcript}). Two gates at once starve this machine and collide in obj/ and "
    "bin/. Run ListAgents and find the peer whose session id is that transcript's file name, SendMessage it "
    "that you are queued behind its gate, then do work that runs no gate; this clears on its own once that gate "
    "exits, and its FINISHED message tells you when. If its process is gone and this still blocks, tell the "
    "operator: the operator, not this session, decides whether the record is stale."
)

SLOT_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md): this account's gate slot is held by {owner} (pid {pid}, "
    "since {since}). One gate at a time per account, and the slot is the only record of a gate launched outside "
    "a Claude Code session. This clears on its own once that process exits; "
    "Harness/Gates/GateSlot.ps1 owns the slot. Do work that runs no gate and launch yours after it is free; while "
    "every declared item is done, a QUEUED: line naming this slot earns the stop."
)

GATE_SLOT_RECORD = "gate-slot.json"
GATE_SLOT_LOCK = "gate-slot.lock"

FAILED_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md): the gate-cadence check failed ({error}) before it judged "
    "this gate, so nothing about a peer gate in flight, a stopped gate or an unchanged tree was checked, and a gate "
    "nothing judged does not launch. Do not retry: report it, since the failure is a defect in "
    "Harness/Hooks/invoke_gate_cadence_gate.py."
)

DECLARATION_NEEDLES = ((b'"Write"', b"work-products"),)
DECLARATION_CHANGE_NEEDLES = ((b'"Write"', b"work-products"), (b'"Edit"', b"work-products"))
OPERATOR_NEEDLES = (b'"human"', b"<command-args>")
TRANSCRIPT_INDEX_ROOT = None

PLANT_MARKER = "PLANTED"

WORK_PRODUCT_FORMAT = (
    "{\"repos\": {\"<Repo>\": [{\"item\": \"<one deliverable>\", \"done\": false}, "
    "{\"item\": \"<a planted proof>\", \"done\": false, \"plant\": \"<path in the repo of the planted file>\"}, "
    "{\"item\": \"<an item added after the first declaration>\", \"done\": false, \"approval\": \"<the "
    "operator's words asking for it, verbatim>\"}, "
    "{\"item\": \"<temporary work serving an investigation>\", \"done\": false, \"until\": \"<four or more words "
    "of the investigation's own item>\"}]}, "
    "\"dropped\": {\"<Repo>\": [{\"item\": \"<a removed item, verbatim>\", \"approval\": \"<the operator's words, "
    "verbatim>\"}]}, "
    "\"standing\": [{\"item\": \"<work that edits no file, such as a review>\", \"peer\": \"<the peer's session id, "
    "or operator>\", \"done\": false, \"approval\": \"<the operator's words asking for it, verbatim>\"}]}"
)

APPROVAL_ROUTE = (
    "To ask: park a question on your parking-lot page (ArtifactData set on questions, doc_id qN-slug), read the "
    "page once the operator answers (a query on questions where status == answered), and copy at least three "
    "consecutive words of their answer or chat message into \"approval\"."
)

WORK_PRODUCT_MISSING_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Declaring the work product\"): this session has no "
    "readable work-product declaration for {repo}, so nothing records what this gate is the gate OF. Write "
    "{path} as {format}, listing every deliverable the operator's ask puts in {repo}, not the part you have "
    "finished. A gate runs once every item is done: true. A session whose ask changes no file in any repo and "
    "gates nothing (a review, an audit, a report) writes {{\"repos\": {{}}}} instead, and the hook checks that it "
    "edited none. When {path} already exists, each item added for {repo} carries the operator's approval. "
    + APPROVAL_ROUTE
)

WORK_PRODUCT_OPEN_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Declaring the work product\"): {repo}'s declared work "
    "product still has {count} open item(s): {items}. A gate before the last edit is a check mid-batch. Finish "
    "them, then mark each done: true in {path}."
)

UNTIL_MIN_WORDS = 4

UNTIL_UNRESOLVED_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Declaring the work product\"): {repo}'s item(s) {items} "
    "carry an \"until\" that names no other item declared for {repo}. An \"until\" quotes at least "
    + str(UNTIL_MIN_WORDS) + " consecutive words of the item it waits on, and holds this item open while that one "
    "is open. Work that exists only to serve an investigation is linked to the investigation's own item; an "
    "investigation that is not declared is invisible to every hook, so declare it as its own item first."
)

EMPTY_DECLARATION_EDITED_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Declaring the work product\"): {path} declares no work in any "
    "repo, yet this session edited {repo} ({file}){more}. A declaration with no items belongs to a read-only session, "
    "whose ask changes no file in any repo and gates nothing (a review, an audit, a report). A session that changed "
    "a repo lists every deliverable there as an item naming the file it changes, then gates that repo. Adding an "
    "item to a declaration already written needs the operator's approval. " + APPROVAL_ROUTE
)

REPO_WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

ITEMS_SHOWN = 2
ITEM_CLIP = 120

PLANT_CLAIMED_DONE_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Declaring the work product\"): {repo}'s item(s) {items} "
    "are marked done: true while the planted file still holds the " + PLANT_MARKER + " marker. A planted proof "
    "is finished only once its plant is removed: remove it, then gate the clean tree."
)

WORK_PRODUCT_REMOVED_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Declaring the work product\"): items this session "
    "declared are gone from {path} with no approval the operator gave: {items}. Narrowing the work product "
    "is the operator's decision. Put each item back as it was, or, if they agree to drop it, list it under "
    "\"dropped\" with their words verbatim as \"approval\", which this hook checks against their messages. "
    + APPROVAL_ROUTE
)

NOT_A_REPO_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Declaring the work product\"): {path} would declare work under "
    "{keys}, which name no git repo directly under the workspace. A key under \"repos\" is the repo the item's edit "
    "lands in, never a topic, a worktree folder or a file outside every repo: work in a worktree belongs to its "
    "repo's key. The one key that is no repo is \"AGENTS\", for edits to the workspace's AGENTS.md and the markdown "
    "in its AGENTS folder."
)

WORKSPACE_DOCS_KEY = "AGENTS"

WORKSPACE_DOCS_ITEM_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Declaring the work product\"): {path} declares item(s) "
    "under \"{key}\" that name a file other than the workspace's AGENTS.md or a markdown file in its AGENTS folder: "
    "{items}. That key holds only edits to the fleet docs, each naming the doc it changes (AGENTS.md, "
    "AGENTS/GATES.md, AGENTS/REPOS/Identity.md); an edit to a repo file belongs under that repo's key."
)

NOT_AN_EDIT_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Declaring the work product\"): {repo}'s declared item(s) "
    "name no file that changed in {repo} during this session: {items}. An item is an edit and names the file it "
    "changes. An outcome (a gate passing, a check or a probe passing, a task enabled) is not an item, however it is "
    "worded: it can never be marked done, because it needs a gate to pass and no gate launches while an item is "
    "open, so writing one only blocks this session until it asks the operator to drop it. If the item names a file "
    "you have not changed yet, make that edit. Otherwise rewording it is the operator's decision: move the old "
    "wording under \"dropped\" and add the new one, naming the file the edit changes, each with their approval. "
    + APPROVAL_ROUTE + " The goal's live state belongs in an operator-approved acceptance check."
)
ITEM_NAMES_NO_FILE_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Declaring the work product\"): {path} would declare item(s) "
    "naming no file: {items}. An item is an edit and names the file or files it changes (App.tsx, "
    "Hooks/parking_lot_commits.py, .gitignore), checked here when it is written so it never surfaces at the gate "
    "as a change only the operator can approve. An outcome belongs in the declaration's acceptance list.")
DECLARATION_COMMAND_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Declaring the work product\"): this command writes a "
    "work-product declaration, which only Edit or Write may change: that is where an item added after the first "
    "declaration is checked for the operator's approval. Make the change with Edit or Write.")

WORK_PRODUCT_ADDED_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Declaring the work product\"): {path} holds item(s) "
    "that were not in this session's first declaration and carry no approval the operator gave: {items}. The "
    "work product is fixed when it is first declared from the operator's GOAL:, and widening or rewording it "
    "is the operator's decision. If they agree, give the item an \"approval\" holding their words "
    "verbatim (at least three, or a shorter message naming the question and approving it, such as \"approve "
    "q44\"), and this hook checks those words against their messages sent after the declaration's change just "
    "before the item first appeared: words they gave earlier, for other work, approve nothing new. Otherwise remove "
    "it and revert whatever it changed. " + APPROVAL_ROUTE
)

UNCHANGED_REASON = (
    "Gate protocol (Harness/AGENTS/GATES.md): no file other than markdown has been edited in this session since "
    "its previous gate ({timestamp}), so a re-run can only repeat that result. Wanting a stronger claim "
    "(\"against the final tree\") is not a change; narrow the claim instead. Approve only if the tree "
    "changed outside this session (an operator edit, a pull, a merge) or that run never finished."
)


def is_gate_step(text):
    if not isinstance(text, str) or not text:
        return False
    text = RE_PACKAGE_INSTALL.sub(" ", text)
    return bool(RE_GATE_STEP.search(text) or RE_GATE_STEP.search(RE_TOKEN_PUNCTUATION.sub(" ", text)))


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except Exception:
        return None


def code_text(body):
    """A script body without its comment lines and PowerShell comment blocks, so prose that names a spec
    file or a step is never read as a command that runs it."""
    uncommented = RE_BLOCK_COMMENT.sub("", body or "")
    return "\n".join(line for line in uncommented.splitlines() if not RE_LINE_COMMENT.match(line))


def loads_gate_library(code):
    """True when the code dot-sources or imports Gates/GateCommon.ps1, the one writer of a gate record, by its name,
    through GATE_LIBRARY, or through a variable assigned from either, as every repo's gate.ps1 does. A script that
    only names the library (a path it checks, an environment value it sets) loads nothing."""
    variables = {name.lower() for name in RE_GATE_LIBRARY_VARIABLE.findall(code or "")}
    for load in RE_SCRIPT_LOAD.finditer(code or ""):
        target = load.group("target")
        if RE_GATE_LIBRARY.search(target):
            return True
        if any(re.search(r"\$" + re.escape(name) + r"(?![\w:])", target, CI) for name in variables):
            return True
    return False


def script_entry(path):
    """(exempt, code, is a gate) for a script file, judged once per CONTENT of the file within this process. Many
    commands run the same script with different arguments, and a transcript's history names each of them; every
    one after the first reuses the judgment instead of scanning the body again. The key is a hash of the text
    actually read, never the file's metadata, which a session can restore after an edit. Nothing is written to
    disk: a stored verdict is one a session could rewrite."""
    body = read_text(path)
    if body is None:
        return False, "", False
    key = hashlib.sha1(body.encode("utf-8", errors="replace")).hexdigest()
    if key not in SCRIPT_CODE_CACHE:
        exempt = bool(body) and EXEMPT_MARKER.lower() in body.lower()
        code = code_text(body)
        gate = not exempt and bool(os.path.basename(path).lower() == "gate.ps1" or is_gate_step(code)
                                   or invoked_gate_target(code, python=path.lower().endswith(".py"))
                                   or loads_gate_library(code) or RE_VARIABLE_STEP.search(code))
        SCRIPT_CODE_CACHE[key] = (exempt, code, gate)
    return SCRIPT_CODE_CACHE[key]


def operational_probe(path):
    """True when path resolves to one of Tools' checked-in operational probes: a script named like a harness
    that reads a live system rather than the tree, so it is no gate step (Harness/AGENTS/GATES.md). The probes live in
    the workspace's Tools checkout, whichever checkout this hook runs from. The match is the resolved file, never the
    name: a skills junction resolves to it, and a copy anywhere else does not."""
    tools = os.path.join(session_home.workspace_root(), OPERATIONAL_PROBES_CHECKOUT)
    resolved = os.path.normcase(os.path.realpath(path))
    return any(resolved == os.path.normcase(os.path.realpath(os.path.join(tools, *parts)))
               for parts in OPERATIONAL_PROBES)


def is_harness(path):
    """True for a regression harness path: named test_*.py or Test-*.ps1 and not an operational probe."""
    return bool(RE_HARNESS_NAME.search(path)) and not operational_probe(path)


def classify(command):
    """INLINE for a gate step issued on its own, SCRIPT for a script that is a gate, None otherwise. A script is a
    gate by name (gate.ps1) or by what it runs: a gate step, a harness, Gates/GateCommon.ps1 (the one record writer)
    or steps through a variable (& npm run $step). Text it holds, a GATE: verdict it compares or a harness it only
    names, makes no gate. A read-only reader (git diff, Get-Content ...) whose
    argument names a gate-step script runs nothing."""
    if not isinstance(command, str) or not command.strip():
        return None
    if RE_READ_ONLY_HEAD.match(command) and not RE_INVOKES_WHAT_IT_READS.search(command):
        return None
    match = RE_SCRIPT_CALL.match(command)
    if match:
        path = match.group("path").strip('"')
        if is_harness(path):
            return SCRIPT
        if is_gate_step(os.path.basename(path)):
            return INLINE
        if os.path.isfile(path):
            return SCRIPT if script_entry(path)[2] else None
    if RE_EDITING_COMMAND.search(command) and not is_gate_step(RE_EDITING_COMMAND.sub(" ", command)):
        return None
    if plain_build_target(command):
        return None
    return INLINE if is_gate_step(command) else None


def plain_build_target(command):
    """The absolute path a plain build names, else None: one dotnet build of a path (no -t target, which can run
    anything) or one npm run build with --prefix. A build is the proof a gate needs before it runs, so it is no
    gate step; a script holding one is still judged by its body."""
    match = RE_PLAIN_BUILD.match(command or "") if isinstance(command, str) else None
    if match is None:
        return None
    path = match.group("dotnet") or match.group("before") or match.group("after")
    return os.path.abspath(path.strip('"')) if path else None


def narrowing(command):
    """The text that narrows a gate script to a test name or file, else None. A harness is judged by its
    command line only: its body quotes narrowing flags as fixture data."""
    match = RE_SCRIPT_CALL.match(command or "")
    if not match:
        return None
    path = match.group("path").strip('"')
    if is_harness(path):
        found = RE_HARNESS_NARROWED.search(command)
    else:
        found = RE_NARROWED_RUN.search(code_text(read_text(path))) if os.path.isfile(path) else None
    return found.group(0).strip() if found else None


def harness_selection(command):
    """The --only / -Only / -Rule terms of a harness run, else None."""
    match = RE_SCRIPT_CALL.match(command or "")
    if not match or not is_harness(match.group("path").strip('"')):
        return None
    found = RE_HARNESS_SELECTION.search(command)
    if not found:
        return None
    try:
        tokens = shlex.split(found.group("rest"))
    except ValueError:
        return []
    terms = []
    for token in tokens:
        if token.startswith("-"):
            break
        terms.append(token)
    return terms


def unverified_harness_selection(command, terms, edits):
    """The reason a narrowed harness run fails, or None when this session edited the harness and every
    term names a case in its body."""
    path = RE_SCRIPT_CALL.match(command).group("path").strip('"')
    if not terms:
        return "it selects nothing"
    if not any(normalised_path(edited) == normalised_path(os.path.abspath(path)) for _, edited in edits):
        return "this session's transcript shows no edit to the harness"
    body = (read_text(path) or "").lower()
    for term in terms:
        if term.lower() not in body:
            return "no case in the harness names {0!r}".format(term)
    return None


def declared_delta(command):
    """The entries of the GATE_DELTA / $GateDelta declaration in a gate script's code, or None when the
    code declares none. A declaration in a comment is prose and declares nothing, and so is one in a
    harness body, which quotes declarations as fixture data."""
    match = RE_SCRIPT_CALL.match(command or "")
    path = match.group("path").strip('"') if match else None
    if not path or is_harness(path) or not os.path.isfile(path):
        return None
    code = code_text(read_text(path))
    declaration = RE_GATE_DELTA.search(code)
    if not declaration:
        return None
    return [first or second for first, second in RE_QUOTED.findall(declaration.group("body"))]


def normalised_path(value):
    """Forward slashes and lower case, done by hand: os.path.normcase would turn the slashes back into
    backslashes on Windows and the directory-boundary test below would never match."""
    return value.replace("\\", "/").strip("/").lower()


def edited_path_matches(edited, entry):
    """True when a transcript edit path is the delta entry: the same absolute path, or a path ending in
    the entry at a directory boundary, so a repo-relative entry matches the absolute path the edit tool
    recorded."""
    edited = normalised_path(edited)
    entry = normalised_path(entry)
    return edited == entry or edited.endswith("/" + entry)


def planted_by_script(command, entry):
    """True when the script's own code names the planted file and carries a write call, which is what a
    plant-and-restore looks like from the outside."""
    code = code_text(script_body(command))
    name = os.path.basename(entry.replace("\\", "/"))
    return bool(name) and name.lower() in code.lower() and bool(RE_WRITE_CALL.search(code))


def unverified_delta(command, entries, edits, since):
    """The reason a declared delta fails, or None when every entry is a session edit after `since` (any
    time, when since is None) or a plant: file the script writes."""
    if not entries:
        return "the declaration is empty"
    for entry in entries:
        if entry.lower().startswith(PLANT_PREFIX):
            planted = entry[len(PLANT_PREFIX):].strip()
            if not planted_by_script(command, planted):
                return "the script's code never writes the planted file {0!r}".format(planted)
            continue
        if not any(edited_path_matches(path, entry) and (since is None or stamp > since)
                   for stamp, path in edits):
            return "this session's transcript shows no edit to {0!r} after its previous gate".format(entry)
    return None


def test_files_only(entries):
    """True when every declared delta entry is a test file (a plant: entry is not one).

    This is the checkable half of the blast-radius rule. The other half - a production edit runs only
    the green tests that could now go red - needs a reachability judgement no hook can make, so it stays
    a documented rule rather than an enforced one. Saying so here keeps a reader from assuming a clean
    pass means the whole rule was checked."""
    if not entries:
        return False
    return all(not entry.startswith(PLANT_PREFIX) and RE_TEST_FILE.search(normalised_path(entry))
               for entry in entries)


def full_test_run(command):
    """The first whole-tier test step in a gate script's code, or None."""
    match = RE_SCRIPT_CALL.match(command or "")
    path = match.group("path").strip('"') if match else None
    if not path or not os.path.isfile(path):
        return None
    found = RE_FULL_TEST_RUN.search(code_text(read_text(path)))
    return found.group(0).strip() if found else None


def indent_of(line):
    return len(line) - len(line.lstrip())


def scalar(lines, index, key_indent, value):
    """A YAML key's value as lines, and the index after it: the inline value, or a block scalar's more
    indented lines."""
    value = value.strip()
    if value and value[0] not in "|>":
        return [value], index
    body = []
    while index < len(lines) and (not lines[index].strip() or indent_of(lines[index]) > key_indent):
        if lines[index].strip():
            body.append(lines[index].strip())
        index += 1
    return body, index


def joined_commands(lines):
    """Shell lines with PowerShell backtick and POSIX backslash continuations joined into one command."""
    commands = []
    pending = ""
    for line in lines:
        if line.endswith("`") or line.endswith(" \\"):
            pending += line[:-1] + " "
            continue
        commands.append(pending + line)
        pending = ""
    if pending.strip():
        commands.append(pending.strip())
    return commands


def workflow_steps(text):
    """Every run: step of a workflow as a dict of its joined commands, its own if: and its job's if:."""
    lines = text.splitlines()
    steps = []
    job_if = ""
    step = None
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        indent = indent_of(line)
        index += 1
        if not stripped or stripped.startswith("#"):
            continue
        if indent <= 2 and stripped.endswith(":"):
            job_if, step = "", None
            continue
        if step is not None and indent <= step["indent"]:
            step = None
        key_indent = indent
        if stripped.startswith("- "):
            step = {"indent": indent, "if": "", "job_if": job_if, "commands": []}
            steps.append(step)
            stripped = stripped[2:].strip()
            key_indent = indent + 2
        key, _, value = stripped.partition(":")
        if key == "if":
            condition, index = scalar(lines, index, key_indent, value)
            if step is None:
                job_if = " ".join(condition)
            else:
                step["if"] = " ".join(condition)
        elif key == "run" and step is not None:
            body, index = scalar(lines, index, key_indent, value)
            step["commands"] = joined_commands(body)
    return steps


def token_pattern(name, trailing=r"[\w:.-]"):
    return r"(?<![\w:.-])" + re.escape(name) + r"(?!" + trailing + r")"


def step_identity(command):
    """The patterns a gate script's code must carry to show it runs this workflow step: a test tier by its
    project and trait, an npm script by its name, anything else by its tool. Empty when the step cannot be
    identified, which leaves it unchecked rather than guessed at."""
    projects = sorted(set(RE_TEST_PROJECT.findall(command)))
    if projects:
        traits = sorted(set(RE_TRAIT.findall(command)))
        return tuple([token_pattern(project, r"\w") for project in projects]
                     + [re.escape("Category=" + trait) for trait in traits])
    script = RE_NPM_SCRIPT.search(command)
    if script:
        return (token_pattern(script.group(1)),)
    if RE_NPM_TEST.search(command):
        return (NPM_TEST_IDENTITY,)
    for probe, wanted in TOOL_IDENTITIES:
        if probe.search(command):
            return (wanted,)
    return ()


def required_steps(workspace, repo):
    """(command, identity, workflow file) for each gate step the repo's workflows run on their ordinary
    trigger: pull_request where a workflow has one, else push. A step whose own or whose job's if: keeps it
    off that trigger (a dispatch-only load tier, a push-only integration tier, a deploy job) is not required."""
    found = []
    for path in sorted(glob.glob(os.path.join(workspace, repo, ".github", "workflows", "*.y*ml"))):
        text = read_text(path) or ""
        if RE_PULL_REQUEST_TRIGGER.search(text):
            off_trigger = RE_OFF_PULL_REQUEST
        elif RE_PUSH_TRIGGER.search(text):
            off_trigger = RE_OFF_PUSH
        else:
            continue
        for step in workflow_steps(text):
            if off_trigger.search(step["job_if"] + " " + step["if"]):
                continue
            for command in step["commands"]:
                identity = step_identity(command) if is_gate_step(command) else ()
                if identity and all(identity != known for _, known, _ in found):
                    found.append((command.strip(), identity, os.path.basename(path)))
    return found


def missing_steps(command, workspace):
    """Each workflow gate step of every workspace repo the script's code names that the code never runs.
    A harness is exempt: its body names repos as fixture data."""
    match = RE_SCRIPT_CALL.match(command or "")
    path = match.group("path").strip('"') if match else None
    if not path or is_harness(path) or not os.path.isfile(path):
        return []
    code = code_text(read_text(path))
    repos = []
    for name in RE_WORKSPACE_REPO.findall(code):
        if name.lower() not in (repo.lower() for repo in repos) \
                and os.path.isdir(os.path.join(workspace, name, ".github", "workflows")):
            repos.append(name)
    missing = []
    for repo in repos:
        for step_command, identity, workflow in required_steps(workspace, repo):
            if not all(re.search(pattern, code, CI) for pattern in identity):
                missing.append("{0} `{1}` ({2})".format(repo, step_command[:90], workflow))
    return missing


def bypassed_gates(command, workspace):
    """The checked-in <Repo>/gate.ps1 of each workspace repo the script's code names, when the script is not
    that file. A harness is exempt: its body names repos as fixture data."""
    match = RE_SCRIPT_CALL.match(command or "")
    path = match.group("path").strip('"') if match else None
    if not path or is_harness(path) or not os.path.isfile(path):
        return []
    own =os.path.normcase(os.path.abspath(path))
    gates = []
    for name in RE_WORKSPACE_REPO.findall(code_text(read_text(path))):
        checked_in = os.path.join(workspace, name, "gate.ps1")
        if os.path.isfile(checked_in) and os.path.normcase(os.path.abspath(checked_in)) != own \
                and checked_in not in gates:
            gates.append(checked_in)
    return gates


def inside_workspace_repo(path, workspace):
    """True when path sits inside a git repository directly under the workspace: reviewed, checked-in code,
    as opposed to a session's own throwaway script in the workspace's .claude folder or a temp directory."""
    relative = os.path.relpath(os.path.abspath(path), os.path.abspath(workspace))
    parts = relative.split(os.sep)
    if relative.startswith("..") or len(parts) < 2:
        return False
    return os.path.isdir(os.path.join(workspace, parts[0], ".git"))


def uncommitted_in_repo(path, workspace):
    """True when a workspace-repo file is modified or untracked in its checkout, so it is the session's own code
    rather than reviewed, checked-in code. A git status that cannot be read counts as uncommitted: a gate that
    cannot judge never lets a call through."""
    relative = os.path.relpath(os.path.abspath(path), os.path.abspath(workspace))
    repo = os.path.join(os.path.abspath(workspace), relative.split(os.sep)[0])
    listed = subprocess.run(["git", "-C", repo, "status", "--porcelain", "--untracked-files=all", "--",
                             os.path.abspath(path)], capture_output=True, text=True)
    return listed.returncode != 0 or bool(listed.stdout.strip())


def launches(line, python):
    """True when a line starts a process: a subprocess/os/runpy call in Python, or in PowerShell a call
    operator, pwsh, python or Start-Process outside any quoted string (a command built as a string is data)."""
    if python:
        return bool(RE_PY_LAUNCH.search(line)) and not RE_GIT_LAUNCH.search(line)
    return bool(RE_PS_RUNNER.search(RE_QUOTED.sub(" ", line)))


def invoked_gate_target(code, python=False):
    """The gate.ps1 or harness a script's code runs, else None: a line that both launches a process and names
    the target, or a launching line using a variable that an earlier line assigned the target. Naming a gate
    as data (git show, Select-String, a command string a fixture builds, a path compiled) runs nothing."""
    holders = {}
    file_launches = python and bool(RE_PY_LAUNCH.search(code))
    for line in code.splitlines():
        if RE_COMPILE_ONLY.search(line):
            continue
        target = RE_GATE_TARGET.search(line)
        runs = launches(line, python)
        if target and (runs or (file_launches and RE_PY_ARGV_RUNNER.search(line))):
            return target.group(0).strip(" \"'\\/=(,")
        assigned = RE_ASSIGNED_NAME.match(line)
        if assigned and target:
            holders[assigned.group("name").lower()] = target.group(0).strip(" \"'\\/=(,")
            continue
        if holders and runs:
            for name, held in holders.items():
                if re.search(RUN_TARGET_TEMPLATE.format(name=re.escape(name)), line, CI):
                    return held
    return None


def script_abuse(command, workspace):
    """The denial for a session's own script that runs a gate or a harness for it (a wrapper), or that waits,
    else None. The script's code is read, comments stripped, never its name: a wrapper is any script that both
    names a gate.ps1 or a harness and invokes something, and a wait script is any that sleeps or blocks. A
    committed script inside a workspace repo is reviewed code, and the checked-in gate.ps1 and a harness are the
    gates themselves, so none of them is judged here; a workspace-repo script its checkout reports modified or
    untracked is the session's own and is judged like any other. A bare command that writes a declaration is
    denied the same way a script that does is, and a bare command that blocks on a CI run the same way a script
    that does is."""
    if RE_DECLARATION_NAME.search(command or "") and RE_FILE_WRITE.search(command or ""):
        return DECLARATION_COMMAND_REASON
    blocking = RE_CLI_WAIT_COMMAND.match(command or "")
    if blocking:
        return COMMAND_WAIT_REASON.format(call=blocking.group(0).strip())
    match = RE_SCRIPT_CALL.match(command or "")
    path = match.group("path").strip('"') if match else None
    if not path or not os.path.isfile(path) or is_harness(path):
        return None
    if checked_in_gate_repo(command, workspace) is not None:
        return None
    if inside_workspace_repo(path, workspace) and not uncommitted_in_repo(path, workspace):
        return None
    code = code_text(read_text(path))
    target = invoked_gate_target(code, python=path.lower().endswith(".py"))
    if target:
        return WRAPPER_REASON.format(script=os.path.basename(path), target=target)
    wait = wait_call(code)
    if wait:
        return WAIT_REASON.format(script=os.path.basename(path), call=wait.group(0).strip())
    if RE_DECLARATION_NAME.search(code) and RE_FILE_WRITE.search(code):
        return DECLARATION_SCRIPT_REASON.format(script=os.path.basename(path))
    return None


def transcript_tail(transcript_path, limit=POLL_TAIL_BYTES):
    """The transcript's last records, newest first, read from at most its last `limit` bytes."""
    try:
        with open(transcript_path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            data = handle.read()
    except (OSError, TypeError):
        return []
    records = []
    for raw in data.splitlines()[1 if size > limit else 0:]:
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records[::-1]


def poll_key(command):
    return re.sub(r"\s+", " ", command or "").strip().lower()


def repeated_poll(command, transcript_path, tool_use_id=None):
    """True when `command` is a status poll that already ran since the last thing that could change what it reports:
    an edit, any other command that is not itself a status poll, or any input the session took in (an operator
    message, a peer message, a task notification, a queued command), read from the transcript's tail."""
    if not RE_STATUS_POLL.search(command or "") or not isinstance(transcript_path, str):
        return False
    wanted = poll_key(command)
    for record in transcript_tail(transcript_path):
        kind = record.get("type")
        if kind not in ("user", "assistant"):
            if kind == "queue-operation":
                return False
            if kind == "attachment" and (record.get("attachment") or {}).get("type") in INPUT_ATTACHMENTS:
                return False
            continue
        content = (record.get("message") or {}).get("content")
        if kind == "user":
            origin = record.get("origin") if isinstance(record.get("origin"), dict) else {}
            if record.get("isMeta") is True and origin.get("kind") in NO_INPUT_ORIGINS:
                continue
            blocks = content if isinstance(content, list) else [content]
            if any(not (isinstance(block, dict) and block.get("type") == "tool_result") for block in blocks):
                return False
            continue
        for block in reversed(content if isinstance(content, list) else []):
            if not isinstance(block, dict) or block.get("type") != "tool_use" or block.get("id") == tool_use_id:
                continue
            name = block.get("name")
            if name in EDIT_TOOLS:
                return False
            if name != "PowerShell":
                continue
            earlier = str((block.get("input") or {}).get("command") or "")
            if not RE_STATUS_POLL.search(earlier):
                return False
            if poll_key(earlier) == wanted:
                return True
    return False


def looked_up_process_wait(code):
    """The WaitForExit call on a variable an earlier line assigned from GetProcessById or Get-Process, a process the
    script found rather than started, else None."""
    for name in {match.group("name") for match in RE_PROCESS_LOOKUP.finditer(code or "")}:
        wait = re.search(r"(?<![\w$])\$?" + re.escape(name) + r"\.WaitForExit(?:Async)?\s*\(", code, CI)
        if wait:
            return wait
    return None


def wait_call(code):
    """The call that makes a script a wait script, else None: a block on a process or a CI run, or any sleep but one
    settle pause. A script exists to pass time when it sleeps in a loop, beside gate or session state, more than once,
    for a length it reads at run time, for longer than SETTLE_PAUSE_SECONDS, or with nothing else to do."""
    wait = RE_PROCESS_WAIT.search(code) or RE_CLI_WAIT.search(code) or looked_up_process_wait(code)
    if wait:
        return wait
    sleeps = list(RE_SLEEP_CALL.finditer(code))
    if not sleeps:
        return None
    if len(sleeps) > 1 or RE_WATCHED_STATE.search(code) or RE_LOOP.search(code):
        return sleeps[0]
    return None if is_settle_pause(code, sleeps[0]) else sleeps[0]


def is_settle_pause(code, sleep):
    """True when the sleep is a literal pause of at most SETTLE_PAUSE_SECONDS on a line of its own, in a script that
    does something besides import and sleep, such as letting a restarted service come up before it is read."""
    start = code.rfind("\n", 0, sleep.start()) + 1
    end = code.find("\n", sleep.end())
    line_end = len(code) if end < 0 else end
    pause = RE_LITERAL_PAUSE.match(code[sleep.end():line_end])
    if not pause or code[start:sleep.start()].strip():
        return False
    seconds = float(pause.group("amount"))
    if (pause.group("unit") or "").lower() in ("milliseconds", "ms"):
        seconds /= 1000
    work = [line for line in (code[:start] + code[line_end:]).splitlines()
            if line.strip() and not RE_IMPORT_LINE.match(line)]
    return seconds <= SETTLE_PAUSE_SECONDS and bool(work)


RE_DECLARATION_NAME = re.compile(r"(?:^|[\\/'\"])work-products(?=[\\/'\"]|$)", re.IGNORECASE | re.MULTILINE)
RE_FILE_WRITE = re.compile(
    r"open\([^)]*['\"][wax]b?\+?['\"]|\.write_text\(|\.write_bytes\(|json\.dump\(|shutil\.(?:copy|move)|"
    r"\b(?:Set-Content|Add-Content|Out-File|Copy-Item|Move-Item|New-Item)\b|WriteAllText|WriteAllBytes",
    re.IGNORECASE)
DECLARATION_SCRIPT_REASON = (
    "Blocked by the gate protocol (Harness/AGENTS/GATES.md, \"Declaring the work product\"): {script} names the "
    "declarations folder as a path segment and writes a file, so it writes a work-product declaration, which only Edit "
    "or Write may change: that is where an item added after the first declaration is checked for the operator's "
    "approval. Make the change with Edit or Write. A script that only mentions the folder in prose or a search pattern "
    "is not judged by it.")


RE_WORKTREE_GITDIR = re.compile(r"^gitdir:\s*(?P<main>.+?)[\\/]\.git[\\/]worktrees[\\/][^\\/]+\s*$", re.IGNORECASE)


def same_path(first, second):
    """Whether two paths name one folder, read through the file system: realpath expands an 8.3 short name
    (C:\\Users\\ADMINI~1) that git never writes, which abspath leaves as it is."""
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def gate_checkout(command, workspace):
    """(repo, checkout) when the command runs a workspace repo's own gate.ps1: <workspace>/<Repo>/gate.ps1, or the
    gate.ps1 of a worktree of <workspace>/<Repo>, which its .git file names in one line. Else None."""
    match = RE_SCRIPT_CALL.match(command or "")
    path = match.group("path").strip('"') if match else None
    if not path or os.path.basename(path).lower() != "gate.ps1":
        return None
    checkout = os.path.dirname(os.path.abspath(path))
    if same_path(os.path.dirname(checkout), workspace):
        return os.path.basename(checkout), checkout
    gitfile = os.path.join(checkout, ".git")
    if not os.path.isfile(gitfile):
        return None
    worktree = RE_WORKTREE_GITDIR.match(read_text(gitfile) or "")
    main = os.path.abspath(worktree.group("main")) if worktree else None
    if main is None or not same_path(os.path.dirname(main), workspace):
        return None
    return os.path.basename(main), checkout


def checked_in_gate_repo(command, workspace):
    """The repo name when the command runs that repo's own gate.ps1, in the workspace or a worktree of it, else None."""
    found = gate_checkout(command, workspace)
    return found[0] if found else None


def undeclared_step_inputs(command, workspace):
    """What a checked-in gate.ps1 leaves undeclared, as a sentence, else None: each step Register-GateSteps
    names with no Register-StepInputs entry, and each entry naming no registered step. A gate that registers
    no steps has nothing to declare."""
    found = gate_checkout(command, workspace)
    if found is None:
        return None
    code = code_text(read_text(os.path.join(found[1], "gate.ps1")) or "")
    registered = RE_GATE_STEPS.search(code)
    if not registered:
        return None
    steps = RE_STEP_NAME.findall(registered.group("body"))
    block = RE_STEP_INPUTS.search(code)
    declared = RE_STEP_INPUT_KEY.findall(block.group("body")) if block else []
    problems = []
    undeclared = [step for step in steps if step not in declared]
    if undeclared:
        problems.append("No inputs declared for: " + ", ".join("'{0}'".format(step) for step in undeclared))
    unregistered = [name for name in declared if name not in steps]
    if unregistered:
        problems.append("Inputs declared for steps it does not register: "
                        + ", ".join("'{0}'".format(name) for name in unregistered))
    return "; ".join(problems) if problems else None


def planned_step_inputs(repo_path):
    """{step: inputs} from Gates/step_inputs.py's plan when the checkout's gate.ps1 takes its steps from that plan
    rather than listing them, else None. The plan is this Tools checkout's own code run over the target checkout, so
    a session's edit to the target's step_inputs.py never decides what its probe may run. A plan that cannot be made
    names no step."""
    code = code_text(read_text(os.path.join(repo_path, "gate.ps1")) or "")
    if "step_inputs.py" not in code or "--plan" not in code:
        return None
    gates = os.path.join(TOOLS_ROOT, "Gates")
    if gates not in sys.path:
        sys.path.insert(0, gates)
    import step_inputs
    try:
        planned = step_inputs.plan(repo_path, step_inputs.default_cache_path(repo_path))
    except Exception:
        return {}
    return {step["name"]: list(step["inputs"]) for step in planned["steps"]}


def gate_step_names(repo_path):
    """The steps a caller may name in -Steps: GateCommon's two catalogs, then the gate's own registered steps."""
    planned = planned_step_inputs(repo_path)
    if planned is not None:
        return ["Code-style catalog", "Comment catalog"] + list(planned)
    code = code_text(read_text(os.path.join(repo_path, "gate.ps1")) or "")
    registered = RE_GATE_STEPS.search(code)
    return ["Code-style catalog", "Comment catalog"] + (RE_STEP_NAME.findall(registered.group("body")) if registered else [])


RE_OWED_CALL = re.compile(r"^\s*Resolve-OwedSteps\s*$", re.IGNORECASE | re.MULTILINE)


def steps_argument_problem(command, workspace):
    """Why a checked-in gate.ps1 launch's -Steps cannot run, else None. A gate.ps1 that calls GateCommon's
    Resolve-OwedSteps works out its own steps, so any -Steps on it is refused. Any other gate.ps1 still takes
    -Steps: absent, empty, or naming a step the gate does not have is refused, and whether each named step
    qualifies is GateCommon's to decide from the verdicts."""
    found = gate_checkout(command, workspace)
    if found is None:
        return None
    match = RE_STEPS_ARGUMENT.search(command or "")
    if RE_OWED_CALL.search(code_text(read_text(os.path.join(found[1], "gate.ps1")) or "")):
        return OWED_STEPS_REASON.format(path=os.path.join(found[1], "gate.ps1")) if match else None
    value = (match.group("single") or match.group("double") or match.group("bare") or "") if match else ""
    requested = [name.strip() for name in value.split(",") if name.strip()]
    steps = gate_step_names(found[1])
    if not match:
        problem = "this launch has no -Steps argument"
    elif not requested:
        problem = "its -Steps argument names no step"
    else:
        unknown = [name for name in requested if name not in steps]
        if not unknown:
            return None
        problem = "-Steps names steps this gate does not have: " + ", ".join("'{0}'".format(name) for name in unknown)
    return STEPS_ARGUMENT_REASON.format(problem=problem, path=os.path.join(found[1], "gate.ps1"),
                                        steps=", ".join(steps))


def session_stem(transcript_path):
    """The session a transcript belongs to: its own file stem, or its parent's for a subagent transcript."""
    absolute = os.path.abspath(transcript_path)
    parent = os.path.dirname(absolute)
    if os.path.basename(parent).lower() == "subagents":
        return os.path.basename(os.path.dirname(parent))
    return os.path.splitext(os.path.basename(absolute))[0]


def work_products_directories(workspace, transcript_path=None):
    """Every declaration directory a session's file may sit in, one per root session_home.roots names: today only
    %TEMP%\\claude\\work-products."""
    return [session_home.work_products_dir(root) for root in session_home.roots(workspace, transcript_path)]


def work_product_path(transcript_path, workspace):
    """<session>.json under work-products (session_home.declaration_path): the first root holding the session's
    declaration, else %TEMP%\\claude\\work-products."""
    return session_home.declaration_path(workspace, session_stem(transcript_path), transcript_path)


def read_work_product(path):
    """({repo: [(item, done, plant)]}, {repo: [(item, approval)]}) from a declaration, else None when it is
    absent or malformed. Every item must be non-blank text, every done flag a real boolean, and a plant, when
    given, the non-blank path in the repo of the file a planted proof edits."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    repos = data.get("repos") if isinstance(data, dict) else None
    dropped = data.get("dropped", {}) if isinstance(data, dict) else None
    if not isinstance(repos, dict) or not isinstance(dropped, dict):
        return None
    declared = {}
    for repo, entries in repos.items():
        if not isinstance(entries, list):
            return None
        triples = [(entry.get("item"), entry.get("done"), entry.get("plant"))
                   for entry in entries if isinstance(entry, dict)]
        if len(triples) != len(entries) or any(
                not isinstance(item, str) or not item.strip() or not isinstance(done, bool)
                or (plant is not None and (not isinstance(plant, str) or not plant.strip()))
                for item, done, plant in triples):
            return None
        declared[repo] = [(item.strip(), done, plant) for item, done, plant in triples]
    removals = {}
    for repo, entries in dropped.items():
        if not isinstance(entries, list):
            return None
        pairs = [(entry.get("item"), entry.get("approval")) for entry in entries if isinstance(entry, dict)]
        if len(pairs) != len(entries) or any(not isinstance(item, str) or not isinstance(approval, str)
                                             for item, approval in pairs):
            return None
        removals[repo] = [(item.strip(), approval) for item, approval in pairs]
    return declared, removals


def normalised_words(text):
    """Lowercase words with apostrophes, a curly one folded to straight, so "that’s" typed and "that's" quoted match."""
    folded = (text or "").lower().replace("’", "'").replace("‘", "'")
    return " ".join(re.findall(r"[a-z0-9']+", folded))


def item_approvals(path):
    """{repo: {item: approval}} for every declared item carrying an "approval" string, else {}."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        return {}
    return {repo: {entry["item"].strip(): entry["approval"] for entry in entries
                   if isinstance(entry, dict) and isinstance(entry.get("item"), str)
                   and isinstance(entry.get("approval"), str)}
            for repo, entries in repos.items() if isinstance(entries, list)}


def item_links(path):
    """{repo: {item: until}} for every declared item carrying an "until" key, whatever its value, else {}."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        return {}
    return {repo: {entry["item"].strip(): entry["until"] for entry in entries
                   if isinstance(entry, dict) and isinstance(entry.get("item"), str) and "until" in entry}
            for repo, entries in repos.items() if isinstance(entries, list)}


def until_targets(until, item, items):
    """The other items whose words hold `until`'s words consecutively, or [] when `until` is not text of at least
    UNTIL_MIN_WORDS words."""
    quoted = normalised_words(until) if isinstance(until, str) else ""
    if len(quoted.split()) < UNTIL_MIN_WORDS:
        return []
    return [other for other in items if other != item and " {0} ".format(quoted) in " {0} ".format(normalised_words(other))]


def until_held(entries, links):
    """Every item done: true that an "until" chain holds open because an item it waits on is still open."""
    items = [item for item, _, _ in entries]
    open_items = {item for item, done, _ in entries if not done}
    changed = True
    while changed:
        changed = False
        for item, until in links.items():
            if item in items and item not in open_items and open_items & set(until_targets(until, item, items)):
                open_items.add(item)
                changed = True
    return {item for item, done, _ in entries if done and item in open_items}


def declaration_key(path):
    """The declaration's tail, work-products/<session>.json, normalized. The transcript records each Write under the
    profile of the machine that made it, and the session continues on another machine after ~/.claude syncs, so the
    same declaration can carry another absolute path, under %TEMP%\\claude or a legacy .claude home."""
    return os.sep.join(os.path.normcase(os.path.normpath(path)).split(os.sep)[-2:])


def declaration_baseline(transcript_path, path):
    """{repo: set(items)} the operator's goals set, else None: the first declaration, joined by the first Write
    after each newer goal was pinned, since that Write answers the newer goal as the first answered the first.
    Every later addition still needs the operator's own words (addition_approved)."""
    baseline = first_declaration(transcript_path, path)
    if baseline is None:
        return None
    since = goal_pin_module().goal_set_time(transcript_path)
    first_at = first_declaration_time(transcript_path, path)
    if since is not None and first_at is not None and since > first_at:
        for repo, items in (first_declaration(transcript_path, path, after=since) or {}).items():
            baseline.setdefault(repo, set()).update(items)
    return baseline


def first_write_under_newer_goal(transcript_path, path):
    """True when the operator pinned a goal after this session first declared, and no Write of the declaration has
    completed since: the Write now being judged is that goal's first declaration, set by the goal itself."""
    since = goal_pin_module().goal_set_time(transcript_path)
    first_at = first_declaration_time(transcript_path, path)
    if since is None or first_at is None or since <= first_at:
        return False
    wanted = declaration_key(path)
    writes, completed = set(), set()
    for transcript in session_files(transcript_path):
        for record in transcript_records(transcript, DECLARATION_NEEDLES):
            for block in message_blocks(record):
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                if block.get("type") == "tool_use" and block.get("name") == "Write" \
                        and isinstance(tool_input.get("file_path"), str) \
                        and declaration_key(tool_input["file_path"]) == wanted \
                        and (record_time(record) or 0) > since:
                    writes.add(block.get("id"))
    for transcript in session_files(transcript_path):
        for record in result_records(transcript, writes):
            for block in message_blocks(record):
                if block.get("type") == "tool_result" and block.get("tool_use_id") in writes \
                        and not block.get("is_error") \
                        and "doesn't want to proceed with this tool use" not in result_text(block):
                    completed.add(block["tool_use_id"])
    return not writes & completed


def declaration_changes(transcript_path, path):
    """[(POSIX seconds, {(repo, item)} after the change)] for every Write or Edit of the declaration that completed, in
    transcript order. The file is rebuilt by replaying each call: a Write sets its content, an Edit replaces its
    old_string as Claude Code did. A refused or failed call wrote nothing and is skipped, and a call the transcript
    records more than once (one tool_use id) is replayed once, where it first appears. An Edit whose old_string
    the rebuilt text lacks means something outside the transcript changed the file, so the item set is unknown
    from there on and the entry carries None. Consecutive calls from one assistant message (one message id) are one
    change, dated by its first call: items written together answer the same operator words, so no sibling's edit
    in the same response becomes another's floor."""
    wanted = declaration_key(path)
    calls, replayed = [], set()
    for transcript in session_files(transcript_path):
        ids, found = set(), []
        for record in transcript_records(transcript, DECLARATION_CHANGE_NEEDLES):
            for block in message_blocks(record):
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                if block.get("type") != "tool_use" or block.get("name") not in ("Write", "Edit") \
                        or not isinstance(tool_input.get("file_path"), str) \
                        or declaration_key(tool_input["file_path"]) != wanted or block.get("id") in replayed:
                    continue
                replayed.add(block.get("id"))
                message = record.get("message") if isinstance(record.get("message"), dict) else {}
                found.append((block.get("id"), record_time(record), block.get("name"), tool_input, message.get("id")))
                ids.add(block.get("id"))
        failed = set()
        for record in result_records(transcript, ids):
            for block in message_blocks(record):
                if block.get("type") == "tool_result" and block.get("tool_use_id") in ids and (
                        block.get("is_error") or "doesn't want to proceed with this tool use" in result_text(block)):
                    failed.add(block["tool_use_id"])
        calls += [call for call in found if call[0] not in failed]
    states, text, items = [], None, set()
    batch_id, batch_at = None, None
    for _, at, name, tool_input, message_id in calls:
        same_batch = message_id is not None and message_id == batch_id
        batch_id = message_id
        if not same_batch:
            batch_at = at
        elif states and states[-1][0] == batch_at:
            states.pop()
        at = batch_at
        if name == "Write":
            text = tool_input.get("content") if isinstance(tool_input.get("content"), str) else None
        elif text is not None:
            old, new = tool_input.get("old_string"), tool_input.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str) or old not in text:
                text = None
            else:
                text = text.replace(old, new) if tool_input.get("replace_all") else text.replace(old, new, 1)
        parsed = declared_items(text) if text is not None else None
        if text is None:
            items = None
        elif parsed is not None:
            items = {(repo, item) for repo, entries in parsed.items() for item in entries}
        if at is not None:
            states.append((at, None if items is None else set(items)))
    return states


def item_approval_floor(repo, item, changes):
    """POSIX seconds an added item's approval must quote an operator message newer than: the last change before the
    item first appeared that altered the declaration's item set, so the operator's words answer the state that asked
    for them, never an older message given for other work. Flipping done, setting pending or rewording an approval
    alters no item and is not counted. None when no replayed change shows the item, or the replay lost the file (a
    script, a tool outside the transcript, or records another machine's copy of the transcript holds changed it):
    the item's introduction is then unprovable, and its approval is judged against every operator message."""
    floor, previous = None, set()
    for at, items in changes:
        if items is None:
            return None
        if (repo, item) in items:
            return floor if floor is not None else at
        if items != previous:
            floor = at
        previous = items
    return None


def item_landed(repo, item, changes, workspace):
    """True when a commit made after the item first appeared in the declaration changed a file it names: its work
    landed, so a later item dirtying the same file never re-judges its approval. False when the replay never shows
    the item."""
    appeared = next((at for at, items in changes if items is not None and (repo, item) in items), None)
    return appeared is not None and names_changed_path(item, repo_committed_paths(repo, workspace, appeared))


def transcript_index_module():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import transcript_index
    return transcript_index


def transcript_records(transcript, needles):
    """The transcript's records on lines holding any of `needles`, through the shared resumable index
    (transcript_index.py), so a call parses only what the transcript gained since the last one."""
    index = transcript_index_module()
    return index.candidate_records(transcript, needles, TRANSCRIPT_INDEX_ROOT or index.default_cache_root())


def result_records(transcript, ids):
    """The transcript's tool-result records answering any of the tool calls `ids`, through the same index."""
    index = transcript_index_module()
    return index.result_records(transcript, ids, TRANSCRIPT_INDEX_ROOT or index.default_cache_root())


def first_declaration(transcript_path, path, after=None):
    """{repo: set(items)} of the declaration as this session first wrote it, read from its own transcript's
    first Write of that file that was not refused, else None when no such Write is recorded (a script or an Edit
    created it). A Write whose tool result is an error (the operator rejected it, or it failed) wrote nothing, so
    it is no declaration: the next Write is the first. With `after` (POSIX seconds), only a Write recorded after
    it counts."""
    wanted = declaration_key(path)
    writes = []
    failed = set()
    for transcript in session_files(transcript_path):
        for record in transcript_records(transcript, DECLARATION_NEEDLES):
            for block in message_blocks(record):
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                if block.get("type") != "tool_use" or block.get("name") != "Write" \
                        or not isinstance(tool_input.get("file_path"), str) \
                        or declaration_key(tool_input["file_path"]) != wanted:
                    continue
                writes.append((block.get("id"), tool_input.get("content") or "", record_time(record)))
    write_ids = {tool_id for tool_id, _, _ in writes}
    for transcript in session_files(transcript_path):
        for record in result_records(transcript, write_ids):
            for block in message_blocks(record):
                if block.get("type") == "tool_result" and (
                        block.get("is_error") or "doesn't want to proceed with this tool use" in result_text(block)):
                    failed.add(block.get("tool_use_id"))
    for tool_id, content, written_at in writes:
        if tool_id in failed or (after is not None and (written_at is None or written_at <= after)):
            continue
        try:
            data = json.loads(content)
        except ValueError:
            return None
        repos = data.get("repos") if isinstance(data, dict) else None
        if not isinstance(repos, dict):
            return None
        return {repo: {entry["item"].strip() for entry in entries
                       if isinstance(entry, dict) and isinstance(entry.get("item"), str)}
                for repo, entries in repos.items() if isinstance(entries, list)}
    return None


def declared_items(text):
    """{repo: set(items)} from a declaration's JSON text, else None when it does not parse as one."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        return None
    return {repo: {entry["item"].strip() for entry in entries
                   if isinstance(entry, dict) and isinstance(entry.get("item"), str)}
            for repo, entries in repos.items() if isinstance(entries, list)}


def first_declaration_time(transcript_path, path):
    """POSIX seconds of this session's first Write of its declaration, else None."""
    wanted = declaration_key(path)
    for transcript in session_files(transcript_path):
        for record in transcript_records(transcript, DECLARATION_NEEDLES):
            for block in message_blocks(record):
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                if block.get("type") == "tool_use" and block.get("name") == "Write" \
                        and isinstance(tool_input.get("file_path"), str) \
                        and declaration_key(tool_input["file_path"]) == wanted:
                    return record_time(record)
    return None


def changed_paths(repo_root, since):
    """Every repo-relative path, forward slashes, that is uncommitted now or that a commit since `since` (POSIX
    seconds) changed, else None when `repo_root` is no git work tree."""
    if not os.path.exists(os.path.join(repo_root, ".git")):
        return None
    git = ["git", "-C", repo_root, "-c", "core.fsmonitor=false", "-c", "core.quotePath=false"]
    paths = set()
    status = subprocess.run(git + ["status", "-z", "--porcelain=v1", "--no-renames", "--untracked-files=all"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if status.returncode == 0:
        paths |= {entry[3:] for entry in status.stdout.split("\0") if len(entry) > 3}
    if since is not None:
        paths |= committed_paths(repo_root, since) or set()
    return {path.replace("\\", "/") for path in paths}


def committed_paths(repo_root, since):
    """Every repo-relative path, forward slashes, that a commit on HEAD since `since` (POSIX seconds) changed, else
    None when `repo_root` is no git work tree."""
    if not os.path.exists(os.path.join(repo_root, ".git")):
        return None
    log = subprocess.run(["git", "-C", repo_root, "-c", "core.fsmonitor=false", "-c", "core.quotePath=false", "log",
                          "--since=@{0}".format(int(since)), "--name-only", "--format=", "HEAD"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    if log.returncode != 0:
        return set()
    return {line.strip().replace("\\", "/") for line in log.stdout.splitlines() if line.strip()}


def repo_committed_paths(repo, workspace, since):
    """committed_paths across every checkout of the repo, as repo_changed_paths gathers them."""
    try:
        checkouts = gate_status_module().checkouts(repo, workspace)
    except Exception:
        checkouts = [os.path.join(workspace, repo)]
    found = set()
    for checkout in checkouts or [os.path.join(workspace, repo)]:
        found |= committed_paths(checkout, since) or set()
    return found


def repo_changed_paths(repo, workspace, since):
    """changed_paths across every checkout of the repo (its main checkout and each live worktree, as
    gate_status.checkouts lists them), so an edit made on a branch worktree counts; None when no checkout is a git
    work tree."""
    try:
        checkouts = gate_status_module().checkouts(repo, workspace)
    except Exception:
        checkouts = [os.path.join(workspace, repo)]
    found = None
    for checkout in checkouts or [os.path.join(workspace, repo)]:
        paths = changed_paths(checkout, since)
        if paths is not None:
            found = (found or set()) | paths
    return found


def uncommitted_repo_paths(repo, workspace):
    """The repo's uncommitted paths across every checkout (repo_changed_paths with no commit window), else None. An
    added item naming none of them has landed, and approval matters only before the gate that lands the work."""
    return repo_changed_paths(repo, workspace, None)


def names_changed_path(item, changed):
    """True when the item names, by full path or by file name, a path in `changed`."""
    return work_product_items.names_path(item, changed)


def workspace_repo(name):
    """True when `name` is one plain folder name directly under the workspace holding a .git directory (a clone,
    never a worktree, whose .git is a file), or names this Tools checkout's own clone, which needs no workspace."""
    if not isinstance(name, str) or not name or name in (".", "..") or os.path.basename(name) != name:
        return False
    own = session_home.CHECKOUT_ROOT
    return os.path.isdir(os.path.join(os.path.abspath(WORKSPACE), name, ".git")) or (
        os.path.normcase(name) == os.path.normcase(os.path.basename(own)) and os.path.isdir(os.path.join(own, ".git")))


def work_product_edit_problem(file_path, tool_input, transcript_path):
    """The denial for an Edit or Write that widens a work-product declaration after its first version, else
    None. The declaration's owner is the session its file is named for; its first Write, read from that
    session's transcript, is the work product the operator's GOAL: set. Any item the new text adds or rewords
    must carry an "approval" quoting the operator verbatim, checked against the owner's operator messages."""
    if not isinstance(file_path, str) or not isinstance(transcript_path, str):
        return None
    products = {os.path.normcase(os.path.abspath(directory))
                for directory in work_products_directories(WORKSPACE, transcript_path)}
    target = os.path.normcase(os.path.abspath(file_path))
    if os.path.dirname(target) not in products or not target.endswith(".json"):
        return None
    exists = os.path.isfile(file_path)
    current = (read_text(file_path) or "") if exists else ""
    if isinstance(tool_input.get("content"), str):
        new_text = tool_input["content"]
    elif isinstance(tool_input.get("old_string"), str) and isinstance(tool_input.get("new_string"), str):
        count = -1 if tool_input.get("replace_all") else 1
        new_text = current.replace(tool_input["old_string"], tool_input["new_string"], count)
    else:
        return None
    proposed = declared_items(new_text)
    if proposed is None:
        return None
    strays = sorted(repo for repo in proposed if not workspace_repo(repo) and repo != WORKSPACE_DOCS_KEY)
    if strays:
        return NOT_A_REPO_REASON.format(path=file_path, keys=", ".join(strays))
    outside_docs = sorted(item for item in proposed.get(WORKSPACE_DOCS_KEY, set())
                          if not work_product_items.item_paths(item)
                          or not all(workspace_doc_path(named) for named in work_product_items.item_paths(item)))
    if outside_docs:
        return WORKSPACE_DOCS_ITEM_REASON.format(path=file_path, key=WORKSPACE_DOCS_KEY,
                                                 items=item_summary(outside_docs, file_path))
    if proposed.get(WORKSPACE_DOCS_KEY):
        record_existing_docs(proposed[WORKSPACE_DOCS_KEY], WORKSPACE, docs_existed_path(TEMP_CLAUDE_ROOT, transcript_path))
    present = declared_items(current) or {}
    unnamed = sorted(item for repo, items in proposed.items() for item in items
                     if item not in present.get(repo, set()) and not work_product_items.item_paths(item))
    if unnamed:
        return ITEM_NAMES_NO_FILE_REASON.format(path=file_path, items=item_summary(unnamed, file_path))
    if not exists:
        return None
    owner = os.path.join(os.path.dirname(os.path.abspath(transcript_path)),
                         os.path.splitext(os.path.basename(file_path))[0] + ".jsonl")
    baseline = declaration_baseline(owner, file_path) if os.path.isfile(owner) else None
    if isinstance(tool_input.get("content"), str) and os.path.isfile(owner) \
            and first_write_under_newer_goal(owner, file_path):
        return None
    if baseline is None:
        baseline = declared_items(current) or {}
    approvals = {}
    try:
        data = json.loads(new_text)
        for repo, entries in (data.get("repos") or {}).items():
            approvals[repo] = {entry["item"].strip(): entry["approval"] for entry in entries
                               if isinstance(entry, dict) and isinstance(entry.get("item"), str)
                               and isinstance(entry.get("approval"), str)}
    except (ValueError, AttributeError, TypeError):
        pass
    said = None
    goal = None
    source = owner if os.path.isfile(owner) else transcript_path
    widened = []
    for repo, items in proposed.items():
        for item in sorted(items - baseline.get(repo, set()) - present.get(repo, set())):
            if said is None:
                said, goal = operator_texts(source), pinned_goal(source) or ""
            if not addition_approved(approvals.get(repo, {}).get(item, ""), said, goal):
                widened.append(item)
    if widened:
        return WORK_PRODUCT_ADDED_REASON.format(path=file_path, items=item_summary(widened, file_path))
    return None


def record_time(record):
    """The record's timestamp as POSIX seconds, else None."""
    stamp = record.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def operator_texts(transcript_path, since=None):
    """Everything the operator said: a turn (type user, origin human, string content), a message sent mid-turn
    (a queued_command attachment, origin human), the arguments of a slash command they ran (/goal <text>, a user
    record Claude Code writes without an origin: goal_pin.command_args) and an answer they gave on this session's
    parking-lot page (invoke_artifact_gate.page_answers). Tool results, peer messages and system text carry no human
    origin, and no agent can write a page answer or a command record, so nothing a session writes through a tool can
    put words in the operator's mouth. With `since` (POSIX seconds), only messages timestamped after it count, and
    one with no timestamp never does."""
    texts = []
    for record in transcript_records(transcript_path, OPERATOR_NEEDLES):
        args = goal_pin_module().command_args(record)
        if args:
            if since is None or (record_time(record) or 0) > since:
                texts.append(args)
            continue
        attachment = record.get("attachment") if isinstance(record.get("attachment"), dict) else {}
        origin = record.get("origin") or attachment.get("origin")
        if not isinstance(origin, dict) or origin.get("kind") != "human":
            continue
        if since is not None and (record_time(record) or 0) <= since:
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        if record.get("type") == "user" and isinstance(message.get("content"), str):
            texts.append(message["content"])
        elif attachment.get("type") == "queued_command" and isinstance(attachment.get("prompt"), str):
            texts.append(attachment["prompt"])
    for answered_at, answer in artifact_gate_module().page_answers(transcript_path):
        if since is None or (parse_timestamp(answered_at) or 0) > since:
            texts.append(answer)
    return texts


def artifact_gate_module():
    """invoke_artifact_gate.py owns which page answers are the operator's: no agent can write one."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import invoke_artifact_gate
    return invoke_artifact_gate


RE_QUESTION_ID_WORD = re.compile(r"^q\d+$")
APPROVE_WORDS = frozenset({"approve", "approved"})
NEGATION_WORDS = frozenset({"not", "no", "never", "don't", "dont", "didn't", "didnt"})


def question_approval(words):
    """True when these words name a parked question (qN) and approve it, in any order, with no negation."""
    tokens = set(words)
    return (any(RE_QUESTION_ID_WORD.match(token) for token in tokens) and bool(tokens & APPROVE_WORDS)
            and not tokens & NEGATION_WORDS)


def operator_approved(approval, said):
    """True when the approval quotes the operator verbatim, up to case and punctuation: at least three words, or a
    shorter quote naming the question and approving it ("approve q44") taken from a message that does the same."""
    wanted = normalised_words(approval)
    if len(wanted.split()) >= 3 and any(wanted in normalised_words(text) for text in said):
        return True
    if not question_approval(wanted.split()):
        return False
    return any(question_approval(normalised_words(text).split())
               and " {0} ".format(wanted) in " {0} ".format(normalised_words(text)) for text in said)


def addition_approved(approval, said, goal):
    """operator_approved for an item added after the first declaration, which the pinned GOAL: cannot
    approve: the goal is what the first declaration was written from, so citing it would let any widening
    through."""
    if goal and normalised_words(approval.replace("`", "")) in normalised_words(goal.replace("`", "")):
        return False
    return operator_approved(approval, said)


def plant_in_place(workspace, repo, plant):
    """True while a planted proof's file still holds the marker, read from the file rather than the claim."""
    if not plant:
        return False
    text = read_text(os.path.join(workspace, repo, plant))
    return bool(text) and PLANT_MARKER in text


def workspace_checkouts(workspace):
    """[(checkout, repo)] for every git repo directly under the workspace (a directory holding .git) and each
    worktree of it that gate_status.checkouts lists, checkout paths normalized for a prefix match."""
    found = []
    try:
        names = sorted(os.listdir(workspace))
    except OSError:
        return found
    for name in names:
        main = os.path.join(workspace, name)
        if not os.path.isdir(main) or not os.path.exists(os.path.join(main, ".git")):
            continue
        try:
            checkouts = gate_status_module().checkouts(name, workspace)
        except Exception:
            checkouts = [main]
        for checkout in checkouts or [main]:
            found.append((os.path.normcase(os.path.abspath(checkout)), name))
    return found


def session_repo_edits(transcript_path, workspace):
    """[(repo, file)] for every Edit, Write, MultiEdit or NotebookEdit this session ran, its subagents included,
    whose file lies in a workspace repo's checkout or one of its worktrees, and whose tool result was no error: a
    rejected or failed edit changed nothing, so it counts for nothing. This is what an empty declaration is held
    to: a session that declares no work in any repo must have edited none."""
    checkouts = workspace_checkouts(workspace)
    if not checkouts:
        return []
    edits = []
    for file_path in session_file_edits(transcript_path):
        absolute = os.path.normcase(os.path.abspath(file_path))
        for checkout, repo in checkouts:
            if absolute.startswith(checkout + os.sep):
                edits.append((repo, file_path))
                break
    return edits


def workspace_doc_path(path):
    """True when a path an item names is the workspace's AGENTS.md index or a markdown file in its AGENTS folder,
    matched without regard to case, as Windows matches paths."""
    parts = (path or "").replace("\\", "/").strip("/").lower().split("/")
    if ".." in parts or "." in parts:
        return False
    return parts == ["agents.md"] or (len(parts) > 1 and parts[0] == WORKSPACE_DOCS_KEY.lower() and parts[-1].endswith(".md"))


def workspace_doc_edits(transcript_path, workspace, items, existed_path):
    """The workspace-doc paths (AGENTS.md, AGENTS/...) this session changed: each one a landed Edit or Write of this
    session wrote, and each one an item names that is gone now though `existed_path` (a ledger this keeps) recorded
    it present when it was first judged, a deletion. A named doc that never existed is no deletion. The workspace has
    no git history, so the session's own edits are the proof an AGENTS item is an edit. Paths are lowercased, as
    Windows matches them."""
    named, present, existed = record_existing_docs(items, workspace, existed_path)
    root = os.path.normcase(os.path.abspath(workspace))
    changed = {path for path in named - present if path in existed}
    for file_path in session_file_edits(transcript_path):
        absolute = os.path.normcase(os.path.abspath(file_path))
        if absolute.startswith(root + os.sep):
            relative = os.path.relpath(absolute, root).replace("\\", "/")
            if workspace_doc_path(relative):
                changed.add(relative.lower())
    return changed


def docs_existed_path(temp_root, transcript_path):
    """The ledger of workspace docs this session's AGENTS items named while the docs existed."""
    return os.path.join(temp_root, SCAN_CACHE_DIRECTORY, "work-products",
                        session_stem(transcript_path) + ".docs-existed.json")


def record_existing_docs(items, workspace, existed_path):
    """(named, present, existed): the workspace docs the items name, the ones present now, and every one recorded
    present so far, this read's present ones added to the ledger at existed_path. Paths are lowercased."""
    try:
        with open(existed_path, "r", encoding="utf-8") as handle:
            existed = set(json.load(handle))
    except (OSError, ValueError, TypeError):
        existed = set()
    named = {path.lower() for item in items for path in work_product_items.item_paths(item) if workspace_doc_path(path)}
    present = {path for path in named if os.path.exists(os.path.join(workspace, *path.split("/")))}
    if present - existed:
        existed |= present
        try:
            os.makedirs(os.path.dirname(existed_path), exist_ok=True)
            with open(existed_path, "w", encoding="utf-8") as handle:
                json.dump(sorted(existed), handle)
        except OSError:
            pass
    return named, present, existed


def session_file_edits(transcript_path):
    """Every file an Edit, Write, MultiEdit or NotebookEdit this session ran, its subagents included, wrote with a
    tool result that was no error."""
    calls = {}
    failed = set()
    for transcript in session_files(transcript_path):
        try:
            with open(transcript, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"tool_use"' not in line and '"tool_result"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    for block in message_blocks(record):
                        if block.get("type") == "tool_use" and block.get("name") in REPO_WRITE_TOOLS:
                            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                            file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
                            if isinstance(file_path, str) and file_path.strip():
                                calls[block.get("id")] = file_path
                        elif block.get("type") == "tool_result" and (
                                block.get("is_error") or "doesn't want to proceed with this tool use" in result_text(block)):
                            failed.add(block.get("tool_use_id"))
        except OSError:
            continue
    return [file_path for tool_id, file_path in calls.items() if tool_id not in failed]


def session_file_edit_times(transcript_path):
    """{file: POSIX seconds of this session's last landed Edit, Write, MultiEdit or NotebookEdit of it}, the same edits
    session_file_edits finds, so a later change by another session can be told from this session's own."""
    calls = {}
    failed = set()
    for transcript in session_files(transcript_path):
        try:
            with open(transcript, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"tool_use"' not in line and '"tool_result"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    for block in message_blocks(record):
                        if block.get("type") == "tool_use" and block.get("name") in REPO_WRITE_TOOLS:
                            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                            file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
                            at = record_time(record) if isinstance(record, dict) else None
                            if isinstance(file_path, str) and file_path.strip() and at is not None:
                                calls[block.get("id")] = (file_path, at)
                        elif block.get("type") == "tool_result" and (
                                block.get("is_error") or "doesn't want to proceed with this tool use" in result_text(block)):
                            failed.add(block.get("tool_use_id"))
        except OSError:
            continue
    latest = {}
    for tool_id, (file_path, at) in calls.items():
        if tool_id not in failed:
            key = os.path.normcase(os.path.abspath(file_path))
            latest[key] = max(at, latest.get(key, at))
    return latest


COMMAND_TOOLS = ("PowerShell", "Bash")
RE_GIT_CHANGING = re.compile(
    r"\bgit(?:\.exe)?\s+-C\s+(?:\"(?P<quoted>[^\"]+)\"|(?P<bare>\S+))\s+"
    r"(?P<verb>apply|checkout|restore|merge|pull|reset|am|cherry-pick|revert|stash)(?![\w-])(?P<rest>[^\r\n]*)",
    re.IGNORECASE)
RE_NPM_CHANGING = re.compile(
    r"\bnpm(?:\.cmd)?\s+(?:ci|install|i|update|uninstall)\b[^\r\n]*?--prefix\s+(?:\"(?P<quoted>[^\"]+)\"|(?P<bare>\S+))",
    re.IGNORECASE)
RE_DRY_RUN = re.compile(r"(?<![\w-])--(?:check|dry-run|stat|numstat|summary)\b|(?<![\w-])-n\b", re.IGNORECASE)


def command_changed_checkout(command):
    """The checkout a repo-changing git or npm command names, else None: git -C <path> apply, checkout, restore, merge,
    pull, reset, am, cherry-pick, revert or stash (never a --check or other dry run), or npm ci, install, update or
    uninstall with --prefix <path>. Such a command writes tracked files that no Edit or Write records."""
    for match in RE_GIT_CHANGING.finditer(command or ""):
        if RE_DRY_RUN.search(match.group("rest") or ""):
            continue
        return match.group("quoted") or match.group("bare")
    match = RE_NPM_CHANGING.search(command or "")
    if match:
        return match.group("quoted") or match.group("bare")
    return None


def session_command_checkouts(transcript_path, workspace):
    """{normcased checkout path} for every workspace checkout (a repo's main checkout or a worktree of it) that a
    PowerShell or Bash call of this session ran a repo-changing command in (command_changed_checkout) with a result
    that was no error: the files such a command left dirty are this session's, though no edit tool wrote them."""
    checkouts = workspace_checkouts(workspace)
    if not checkouts:
        return set()
    calls = {}
    failed = set()
    for transcript in session_files(transcript_path):
        try:
            with open(transcript, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"tool_use"' not in line and '"tool_result"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    for block in message_blocks(record):
                        if block.get("type") == "tool_use" and block.get("name") in COMMAND_TOOLS:
                            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                            target = command_changed_checkout(str(tool_input.get("command") or ""))
                            if target:
                                calls[block.get("id")] = target
                        elif block.get("type") == "tool_result" and (
                                block.get("is_error") or "doesn't want to proceed with this tool use" in result_text(block)):
                            failed.add(block.get("tool_use_id"))
        except OSError:
            continue
    found = set()
    for tool_id, target in calls.items():
        if tool_id in failed:
            continue
        absolute = os.path.normcase(os.path.abspath(target))
        for checkout, _ in checkouts:
            if absolute == checkout or absolute.startswith(checkout + os.sep):
                found.add(checkout)
    return found


def work_product_problem(transcript_path, workspace, temp_root, repos=None, allow_plants=False):
    """(repo, reason) for the first way this session's declared work product is not finished, else None.

    `repos` limits the judgment to those repos; None judges every repo declared. Every item ever read is
    kept in a per-session ledger under the temp root, so an item that later vanishes from the file is seen
    as removed rather than never declared; it passes only when the operator's own words approve it.

    A declaration with no items, {"repos": {}}, is a read-only session's: its ask changes no file in any repo
    and gates nothing, so it has no item to name. It is closed only while session_repo_edits() finds no edit of
    a workspace repo; a session that edited one is refused naming the repo and the file.

    `allow_plants` is the planted run's exemption, for a gate launch only: an open item whose plant file
    still holds the marker does not block it, because running with the plant in place is that item's work.
    No caller judging whether the work product is FINISHED passes it. A plant item marked done while its
    marker is still in the file is refused in every mode."""
    path = work_product_path(transcript_path, workspace)
    parsed = read_work_product(path)
    if parsed is None:
        return (repos or ["this session"])[0], WORK_PRODUCT_MISSING_REASON.format(
            repo=(repos or ["each repo"])[0], path=path, format=WORK_PRODUCT_FORMAT)
    declared, removals = parsed
    ledger_path = os.path.join(temp_root, SCAN_CACHE_DIRECTORY, "work-products", session_stem(transcript_path) + ".json")
    try:
        with open(ledger_path, "r", encoding="utf-8") as handle:
            seen = json.load(handle)
    except (OSError, ValueError):
        seen = {}
    merged = {repo: sorted(set(seen.get(repo, [])) | {entry[0] for entry in declared.get(repo, [])})
              for repo in set(seen) | set(declared)}
    try:
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "w", encoding="utf-8") as handle:
            json.dump(merged, handle)
    except OSError:
        pass
    baseline = declaration_baseline(transcript_path, path)
    if baseline is None:
        baseline_path =ledger_path[:-len(".json")] + ".baseline.json"
        try:
            with open(baseline_path, "r", encoding="utf-8") as handle:
                baseline = {repo: set(items) for repo, items in json.load(handle).items()}
        except (OSError, ValueError, AttributeError):
            baseline = {repo: {entry[0] for entry in entries} for repo, entries in declared.items()}
            try:
                with open(baseline_path, "w", encoding="utf-8") as handle:
                    json.dump({repo: sorted(items) for repo, items in baseline.items()}, handle)
            except OSError:
                pass
    added_approvals = item_approvals(path)
    judged = repos if repos is not None else sorted(set(declared) | set(seen))
    if not judged:
        edited = session_repo_edits(transcript_path, workspace)
        names = {checkout: repo for checkout, repo in workspace_checkouts(workspace)}
        edited += [(names[checkout], checkout) for checkout in sorted(session_command_checkouts(transcript_path, workspace))]
        if edited:
            repo, file_path = edited[0]
            more = len(edited) - 1
            return repo, EMPTY_DECLARATION_EDITED_REASON.format(
                path=path, repo=repo, file=file_path, more=" and {0} more edit(s)".format(more) if more else "")
        return None
    said = None
    goal_for_additions = None
    changes = []
    fresh = {}

    def said_since(floor):
        if floor is None:
            return []
        if floor not in fresh:
            fresh[floor] = operator_texts(transcript_path, since=floor)
        return fresh[floor]

    for repo in judged:
        current = {entry[0] for entry in declared.get(repo, [])}
        missing = [item for item in seen.get(repo, []) if item not in current]
        approvals = dict(removals.get(repo, []))
        added = sorted(current - baseline.get(repo, set()))
        if missing or approvals or added:
            said = operator_texts(transcript_path) if said is None else said
        unapproved = [item for item in missing if not operator_approved(approvals.get(item, ""), said)]
        unapproved += [item for item, approval in approvals.items()
                       if item not in missing and not operator_approved(approval, said)]
        if unapproved:
            return repo, WORK_PRODUCT_REMOVED_REASON.format(path=path, items=item_summary(unapproved, path))
        if added and goal_for_additions is None:
            goal_for_additions = pinned_goal(transcript_path) or ""
            changes = declaration_changes(transcript_path, path)
        dirty = uncommitted_repo_paths(repo, workspace) if added else None
        floors = {item: item_approval_floor(repo, item, changes) for item in added
                  if names_changed_path(item, dirty or set()) and not item_landed(repo, item, changes, workspace)}
        widened = [item for item in added
                   if not addition_approved(
                       added_approvals.get(repo, {}).get(item, ""),
                       said_since(floors[item]) if floors.get(item) is not None else said, goal_for_additions)]
        if widened:
            return repo, WORK_PRODUCT_ADDED_REASON.format(path=path, items=item_summary(widened, path))
        if repo == WORKSPACE_DOCS_KEY:
            changed = workspace_doc_edits(transcript_path, workspace, [entry[0] for entry in declared.get(repo, [])],
                                          docs_existed_path(temp_root, transcript_path))
            not_edits = [item for item, done, _ in declared.get(repo, [])
                         if done and not names_changed_path(item.lower(), changed)]
            if not_edits:
                return repo, NOT_AN_EDIT_REASON.format(repo=repo, items=item_summary(not_edits, path))
            changed = None
        else:
            changed = repo_changed_paths(repo, workspace, first_declaration_time(transcript_path, path)) \
                if declared.get(repo) else None
        if changed is not None:
            not_edits = [entry[0] for entry in declared[repo] if not names_changed_path(entry[0], changed)]
            if not_edits:
                return repo, NOT_AN_EDIT_REASON.format(repo=repo, items=item_summary(not_edits, path))
        if repo not in declared or not declared[repo]:
            return repo, WORK_PRODUCT_MISSING_REASON.format(repo=repo, path=path, format=WORK_PRODUCT_FORMAT)
        links = item_links(path).get(repo, {})
        items = [entry[0] for entry in declared[repo]]
        unresolved = [item for item, until in links.items() if item in items and not until_targets(until, item, items)]
        if unresolved:
            return repo, UNTIL_UNRESOLVED_REASON.format(repo=repo, items=item_summary(unresolved, path))
        held = until_held(declared[repo], links)
        planted = {item for item, _, plant in declared[repo] if plant_in_place(workspace, repo, plant)}
        claimed_done = [item for item, done, _ in declared[repo] if done and item in planted]
        if claimed_done:
            return repo, PLANT_CLAIMED_DONE_REASON.format(repo=repo, items=item_summary(claimed_done, path))
        still_open = [item for item, done, _ in declared[repo]
                      if (not done or item in held) and not (allow_plants and item in planted)]
        if still_open:
            return repo, WORK_PRODUCT_OPEN_REASON.format(repo=repo, count=len(still_open),
                                                         items=item_summary(still_open, path), path=path)
    return None


def item_summary(items, path):
    """The first ITEMS_SHOWN items, each clipped to ITEM_CLIP characters, and how many more `path` holds. A
    denial is read by the agent and shown to the operator, so it names the work rather than reprinting the
    declaration the agent can open."""
    shown = [item if len(item) <= ITEM_CLIP else item[:ITEM_CLIP - 3] + "..." for item in items[:ITEMS_SHOWN]]
    more = len(items) - len(shown)
    return "; ".join(shown) + (" (and {0} more in {1})".format(more, path) if more else "")


def live_peer_gate(transcript_path, now=None, temp_root=None, classifier=None):
    """(command, transcript) of a gate another session on this machine has in flight, else None. A resumed
    session starts a new transcript that copies the old one's records, and its gates' completion notices land
    only in the new one; a launch whose tool_use id this session's own transcript also holds is this session's
    gate, judged by its own transcript, never a peer's."""
    now = time.time() if now is None else now
    temp_root = TEMP_CLAUDE_ROOT if temp_root is None else temp_root
    classifier = classify if classifier is None else classifier
    cache_root = os.path.join(temp_root, SCAN_CACHE_DIRECTORY)
    own_ids = set()
    if isinstance(transcript_path, str) and os.path.isfile(transcript_path):
        for path in session_files(transcript_path):
            own_ids.update(scan_transcript(path, cache_root)["gates"])
    for path in peer_files(transcript_path, now):
        peer_gates, _ = collect([path], classifier, unfinished_only=True, cache_root=cache_root)
        live = [gate for gate in peer_gates
                if gate["id"] not in own_ids and is_live(gate, now, session_temp_dir(path, temp_root))]
        if live:
            return live[-1]["command"], path
    return None


def selected_tests(command, workspace):
    """The test names of a tests-only run (a checked-in gate.ps1 given -Tests), else None. pwsh -File passes
    -Tests A,B as the one string "A,B", so the names are split on commas here exactly as the script splits
    them."""
    if checked_in_gate_repo(command, workspace) is None:
        return None
    match = RE_TESTS_ARGUMENT.search(command or "")
    if not match:
        return None
    value = match.group("single") or match.group("double") or match.group("bare") or ""
    return [name.strip() for name in value.split(",") if name.strip()]


def probe_steps(command, workspace):
    """The step names of a probe (a checked-in gate.ps1 given -Probe), else None. pwsh -File passes -Probe A,B as
    the one string "A,B", so the names are split on commas exactly as the script splits them."""
    if checked_in_gate_repo(command, workspace) is None:
        return None
    match = RE_PROBE_ARGUMENT.search(command or "")
    if not match:
        return None
    value = match.group("single") or match.group("double") or match.group("bare") or ""
    return [name.strip() for name in value.split(",") if name.strip()]


def step_input_patterns(repo_path):
    """Each registered step's declared input patterns, from the checkout's gate.ps1 Register-StepInputs block, or
    from the plan of a gate.ps1 that derives them."""
    planned = planned_step_inputs(repo_path)
    if planned is not None:
        return planned
    code = code_text(read_text(os.path.join(repo_path, "gate.ps1")) or "")
    block = RE_STEP_INPUTS.search(code)
    rows = RE_STEP_INPUT_ROW.finditer(block.group("body")) if block else ()
    return {row.group("name"): RE_STEP_NAME.findall(row.group("list")) for row in rows}


def unverified_probe(names, checkout, edits):
    """The reason a probe's names fail, or None when each is a step the checkout's gate.ps1 registers and declares
    an input that one of this session's edits matches, as GateCommon's -like does: repo-relative, forward slashes,
    case-insensitive."""
    if not names:
        return "its -Probe argument names no step"
    registered = gate_step_names(checkout)
    unknown = [name for name in names if name not in registered]
    if unknown:
        return "-Probe names steps this gate does not have: " + ", ".join("'{0}'".format(name) for name in unknown)
    patterns = step_input_patterns(checkout)
    edited = []
    for _, target in edits:
        if isinstance(target, str) and os.path.isabs(target) and is_under(target, checkout):
            edited.append(os.path.relpath(target, checkout).replace("\\", "/").lower())
    unreached = [name for name in names
                 if not any(fnmatch.fnmatchcase(path, pattern.lower()) for pattern in patterns.get(name, ())
                            for path in edited)]
    if unreached:
        return "no file this session edited in {0} is an input of {1}".format(
            checkout, ", ".join("'{0}'".format(name) for name in unreached))
    return None


def evaluate_probe(command, names, workspace, transcript_path, tool_use_id, now, temp_root, classifier,
                   slot_directory=None):
    """The decision for a probe: every name a registered step reached by this session's edits, and the machine
    free. No goal, work-product, build or unchanged-tree check applies, because a probe is not a gate."""
    if not isinstance(transcript_path, str) or not os.path.isfile(transcript_path):
        return ASK, PROBE_REASON.format(problem="this session's transcript is unreadable")
    cache_root = os.path.join(temp_root, SCAN_CACHE_DIRECTORY)
    own_gates, own_edits = collect(session_files(transcript_path), classifier, cache_root=cache_root)
    problem = unverified_probe(names, gate_checkout(command, workspace)[1], own_edits)
    if problem:
        return DENY, PROBE_REASON.format(problem=problem)
    current = current_call_id(own_gates, command, tool_use_id)
    busy = machine_busy([gate for gate in own_gates if gate["id"] != current], transcript_path, now, temp_root,
                        cache_root, classifier, slot_directory)
    return busy or (DEFER, "")


def calls_selected_runner_elsewhere(command, workspace):
    """True when a script other than a checked-in gate.ps1 calls the tests-only runner. A harness is exempt:
    its body names the runner as fixture data."""
    match = RE_SCRIPT_CALL.match(command or "")
    path = match.group("path").strip('"') if match else None
    if not path or is_harness(path) or checked_in_gate_repo(command, workspace) is not None:
        return False
    return bool(RE_SELECTED_RUNNER.search(code_text(script_body(command) or "")))


def unverified_test_names(names, edits):
    """The reason a tests-only run's names fail, or None when each is a method name, with no wildcard, found
    whole-word in a test file this session edited that still exists. A qualified name must have its class
    and method in the same file."""
    if not names:
        return "-Tests names no test"
    texts = []
    for path in dict.fromkeys(path for _, path in edits):
        if RE_TEST_FILE.search(normalised_path(path)) and os.path.isfile(path):
            texts.append(read_text(path) or "")
    for name in names:
        if not RE_TEST_METHOD_NAME.match(name):
            return "{0!r} is not a test method name".format(name)
        needles = re.split(r"[.+]", name)[-2:]
        if not any(all(re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", text) for needle in needles)
                   for text in texts):
            return "no test file this session edited names {0!r}".format(name)
    return None


def gate_status_module():
    """Harness/Gates/gate_status.py owns what the most recent gate record says and whether it still holds."""
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Gates")
    if here not in sys.path:
        sys.path.insert(0, here)
    import gate_status
    return gate_status


def clean_checkout(command, workspace):
    """The checkout of a checked-in gate launch when it holds no uncommitted change (a gate's own build leftovers
    aside, as GateCommon's Get-CleanRepoRefusal counts them), else None. A gate never runs on a clean repo."""
    found = gate_checkout(command, workspace)
    if found is None:
        return None
    statuses = gate_status_module()
    listed = subprocess.run(["git", "-C", found[1], "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, text=True)
    if listed.returncode != 0:
        return None
    pending = [line for line in listed.stdout.splitlines() if line and not statuses.LEFTOVER.search(line[3:].strip('"'))]
    return None if pending else found[1]


def green_record(command, workspace):
    """The gate record of a checked-in gate whose last run PASSED on exactly the current inputs of this checkout,
    else None. A record another checkout wrote into a shared state directory never stands for this one."""
    found = gate_checkout(command, workspace)
    if found is None:
        return None
    repo, checkout = found
    statuses = gate_status_module()
    state_dir = statuses.checkout_state_dir(repo, checkout)
    recorded = (statuses.read_record(repo, state_dir) or {}).get("repoPath")
    if recorded and not same_path(recorded, checkout):
        return None
    entry = statuses.status(repo, workspace=workspace, state_dir=state_dir)
    return entry if entry.get("state") == statuses.GREEN_CURRENT else None


def git_output(checkout, *args):
    """A git command's stripped stdout in the checkout, else None when it fails."""
    done = subprocess.run(["git", "-C", checkout] + list(args), capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else None


def origin_head(checkout):
    """(sha, committed-at in epoch seconds, committed-at as written) of origin's default branch as last fetched
    (origin/HEAD, else origin/main), else None."""
    for ref in ("origin/HEAD", "origin/main"):
        line = git_output(checkout, "log", "-1", "--format=%H %cI", ref)
        if line:
            sha, committed = line.split(" ", 1)
            return sha, parse_timestamp(committed), committed
    return None


def landed(checkout, head):
    """Whether everything in the checkout is on origin: nothing uncommitted (a gate's own leftovers aside) and its
    HEAD an ancestor of origin's head."""
    listed = git_output(checkout, "status", "--porcelain", "--untracked-files=all")
    if listed is None:
        return True
    leftover = gate_status_module().LEFTOVER
    if any(line and not leftover.search(line[3:].strip('"')) for line in listed.splitlines()):
        return False
    return subprocess.run(["git", "-C", checkout, "merge-base", "--is-ancestor", "HEAD", head],
                          capture_output=True).returncode == 0


def peer_green_unlanded(command, workspace):
    """For a Tools gate launch, (peer checkout, its record, origin head) when another checkout of Tools holds a
    PASSED record completed after origin's newest commit that still describes that checkout's tree (GREEN-CURRENT),
    and that checkout's change is not on origin yet, else None. A stale green is a change its session abandoned or
    went on editing, which lands next as nothing, so it holds nobody."""
    found = gate_checkout(command, workspace)
    statuses = gate_status_module()
    if found is None or found[0] != statuses.TOOLS_REPO:
        return None
    repo, checkout = found
    head = origin_head(checkout)
    if head is None or head[1] is None:
        return None
    for peer in statuses.checkouts(repo, workspace):
        if same_path(peer, checkout):
            continue
        try:
            record = statuses.read_record(repo, statuses.checkout_state_dir(repo, peer))
        except (OSError, ValueError, RuntimeError):
            continue
        if not record or record.get(statuses.FIELD_VERDICT) != "PASSED":
            continue
        recorded = record.get("repoPath")
        completed = parse_timestamp(record.get("completedAt"))
        if (recorded and not same_path(recorded, peer)) or completed is None or completed <= head[1]:
            continue
        try:
            state = statuses.status(repo, workspace, state_dir=statuses.checkout_state_dir(repo, peer))
        except (OSError, ValueError, RuntimeError):
            continue
        if state.get(statuses.STATUS_STATE) != statuses.GREEN_CURRENT:
            continue
        if not landed(peer, head[0]):
            return peer, record, head
    return None


def script_body(command):
    match = RE_SCRIPT_CALL.match(command or "")
    path = match.group("path").strip('"') if match else None
    return read_text(path) if path and os.path.isfile(path) else None


def goal_pin_module():
    """goal_pin.py owns what the session's goal is. The hook runs through runpy.run_path, which does not put this
    directory on sys.path, so it is added before the import."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import goal_pin
    return goal_pin


def pinned_goal(transcript_path, state_root=None):
    """The goal in force for the session (an active /goal, else the operator's newest unretired GOAL: line), else
    None, folded incrementally under state_root when one is given."""
    return goal_pin_module().goal_in_force(transcript_path, state_root)


def restates_goal(body, goal):
    """True when a GOAL: line carries the goal's words verbatim, after the Stop hook's own normalisation of case,
    punctuation and whitespace. Markdown backticks are not words, and the PowerShell gate refuses any command line
    holding one, so they are dropped from both sides."""
    goal_pin = goal_pin_module()
    wanted = goal_pin.normalised_words(goal.replace("`", ""))
    return any(wanted in goal_pin.normalised_words(payload.replace("`", ""))
               for payload in goal_pin.RE_GOAL_PIN.findall(body or ""))


def goal_argument(command):
    """A checked-in gate cannot carry a session's goal in its code, so the command restates it instead:
    pwsh -File "<repo>\\gate.ps1" -Goal '<the goal>'. Returned as a GOAL: line, else None."""
    match = RE_GOAL_ARGUMENT.search(command or "")
    if match is None:
        return None
    if match.group("single") is not None:
        return "GOAL: " + match.group("single").replace("''", "'")
    return "GOAL: " + re.sub(r"`(.)", r"\1", match.group("double") or "").replace('""', '"')


def parse_timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def file_age(path, now):
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return None


def message_blocks(record):
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def result_text(block):
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def empty_scan(head=""):
    return {"version": SCAN_VERSION, "head": head, "offset": 0, "gates": {}, "results": {}, "finished": [],
            "stopped": [], "edits": [], "builds": {}}


def absorb(scan, line):
    """Fold one complete transcript line into a scan: the record walk collect() reads. A shell call a PreToolUse
    hook refused never ran, so its refusal drops the launch rather than finishing it: a refused gate is no gate a
    peer must be told about."""
    if not any(needle in line for needle in LINE_NEEDLES):
        return
    if "task-notification" in line:
        scan["finished"].extend(task.strip() for task in RE_TASK_ID.findall(line))
    try:
        record = json.loads(line)
    except Exception:
        return
    if not isinstance(record, dict) or record.get("type") == REQUEST_COPY_TYPE:
        return
    timestamp = record.get("timestamp")
    for block in message_blocks(record):
        kind = block.get("type")
        if kind == "tool_use":
            name = block.get("name")
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            command = tool_input.get("command")
            if name == "TaskStop" and isinstance(tool_input.get("task_id"), str):
                scan["stopped"].append(tool_input["task_id"])
            if name in EDIT_TOOLS:
                target = tool_input.get("file_path") or tool_input.get("notebook_path")
                if isinstance(target, str) and not target.lower().endswith(".md"):
                    scan["edits"].append([timestamp, target])
            elif name in SHELL_TOOLS and isinstance(command, str) and RE_EDITING_COMMAND.search(command):
                scan["edits"].append([timestamp, ""])
            elif name in SHELL_TOOLS and isinstance(command, str) and isinstance(block.get("id"), str):
                scan["gates"][block["id"]] = {"id": block["id"], "timestamp": timestamp, "command": command}
                target = plain_build_target(command)
                if target and not tool_input.get("run_in_background"):
                    scan["builds"][block["id"]] = [None, target]
        elif kind == "tool_result" and isinstance(block.get("tool_use_id"), str):
            build = scan["builds"].get(block["tool_use_id"])
            if build is not None:
                if block.get("is_error"):
                    del scan["builds"][block["tool_use_id"]]
                else:
                    build[0] = timestamp
            if block.get("is_error") and RE_HOOK_REFUSAL.match(result_text(block)):
                scan["gates"].pop(block["tool_use_id"], None)
                continue
            tool_use_result = record.get("toolUseResult")
            task_id = tool_use_result.get("backgroundTaskId") if isinstance(tool_use_result, dict) else None
            output = RE_OUTPUT_FILE.search(result_text(block))
            scan["results"][block.get("tool_use_id")] = [task_id, output.group(1) if output else None]


def scan_cache_file(cache_root, path):
    digest = hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()
    return os.path.join(cache_root, digest + ".json")


def load_scan(cache_root, path):
    try:
        with open(scan_cache_file(cache_root, path), "r", encoding="utf-8") as handle:
            scan = json.load(handle)
        return scan if isinstance(scan, dict) and scan.get("version") == SCAN_VERSION else None
    except Exception:
        return None


def save_scan(cache_root, path, scan):
    """Written to a temporary file and renamed over the cache, so a concurrent reader sees either the old
    scan or the new one, each consistent with its own offset."""
    try:
        os.makedirs(cache_root, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=cache_root, suffix=".tmp")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(scan, handle, separators=(",", ":"))
        os.replace(temporary, scan_cache_file(cache_root, path))
    except Exception:
        return


def scan_transcript(path, cache_root=None):
    """A transcript's record walk, resumed from the cached byte offset when cache_root is given. A file whose
    first bytes differ from the cached ones, or that is shorter than the cached offset, is a different file
    and is walked from the start. Only complete lines are consumed, so a line still being written is read
    whole on the next call."""
    try:
        handle = open(path, "rb")
    except Exception:
        return empty_scan()
    with handle:
        head = hashlib.sha1(handle.read(SCAN_HEAD_BYTES)).hexdigest()
        size = os.fstat(handle.fileno()).st_size
        scan = load_scan(cache_root, path) if cache_root else None
        if not scan or scan.get("head") != head or scan.get("offset", 0) > size:
            scan = empty_scan(head)
        handle.seek(scan["offset"])
        data = handle.read()
    end = data.rfind(b"\n") + 1
    if end:
        for raw in data[:end].split(b"\n"):
            if raw:
                absorb(scan, raw.decode("utf-8", errors="replace"))
        scan["offset"] += end
        if cache_root:
            save_scan(cache_root, path, scan)
    return scan


def collect(paths, classifier, unfinished_only=False, cache_root=None):
    """Walk transcripts into gate launches, non-markdown edits and finished background task ids. Shell
    calls are classified after their state is known, so unfinished_only skips classifying the finished
    history a peer check cannot use."""
    gates = {}
    edits = []
    results = {}
    finished_tasks = set()
    stopped_tasks = set()
    for path in paths:
        scan = scan_transcript(path, cache_root)
        for gate_id, gate in scan["gates"].items():
            gates[gate_id] = dict(gate, transcript=path)
        results.update(scan["results"])
        finished_tasks.update(scan["finished"])
        stopped_tasks.update(scan["stopped"])
        edits.extend((timestamp, target) for timestamp, target in scan["edits"])

    for gate_id, gate in gates.items():
        if gate_id not in results:
            gate["state"] = PENDING
            continue
        task_id, output_file = results[gate_id]
        gate["output_file"] = output_file
        gate["stopped"] = bool(task_id) and task_id in stopped_tasks
        running = task_id and task_id not in finished_tasks and not gate["stopped"]
        gate["state"] = BACKGROUND if running else FINISHED

    gates = {gate_id: gate for gate_id, gate in gates.items()
             if not (unfinished_only and gate["state"] == FINISHED) and classifier(gate["command"])}
    stamped_edits = [(stamp, path) for stamp, path in ((parse_timestamp(value), path) for value, path in edits)
                     if stamp is not None]
    return list(gates.values()), stamped_edits


def message_stamps(paths):
    """When this session last sent a peer a SendMessage. A separate pass rather than a third return
    value from collect(), so collect()'s contract - which the harness depends on - is untouched."""
    stamps = []
    for path in paths:
        try:
            handle = open(path, "r", encoding="utf-8", errors="replace")
        except Exception:
            continue
        with handle:
            for line in handle:
                if "SendMessage" not in line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if not isinstance(record, dict):
                    continue
                for block in message_blocks(record):
                    if block.get("type") == "tool_use" and block.get("name") == "SendMessage":
                        stamp = parse_timestamp(record.get("timestamp"))
                        if stamp is not None:
                            stamps.append(stamp)
    return stamps


def owes_a_finished_line(gates, message_stamps, peers):
    """The completion-triggered half of the peer-notification rule, or None when nothing is owed.

    A gate that has EXITED creates an obligation to tell the sessions holding for it, and the obligation
    starts then rather than at the turn boundary: a session that keeps working is exactly the one whose
    peers stay blocked, and a stop-hook check never fires for it.

    SOLO SESSIONS OWE NOTHING. With no peer transcript there is nobody to notify, and a rule that blocked
    a lone agent would be a rule that cannot be satisfied. `peers` empty is therefore an early return, not
    an edge case to discover later."""
    if not peers:
        return None
    latest_message = max(message_stamps) if message_stamps else None
    for gate in gates:
        if gate.get("state") != FINISHED or gate.get("stopped"):
            continue
        launched = parse_timestamp(gate.get("timestamp"))
        if launched is None:
            continue
        # A message sent after the gate was launched is the report; the exit time is not recorded, so
        # the launch is the conservative lower bound and a session that messaged mid-run is not nagged.
        if latest_message is not None and latest_message >= launched:
            continue
        return gate
    return None


def session_temp_dir(transcript_path, temp_root):
    """<temp root>\\claude\\<slug>\\<session> for a session or subagent transcript. It exists only on the machine
    that ran the session, which transcripts under the synced projects directory cannot show."""
    absolute = os.path.abspath(transcript_path)
    parent = os.path.dirname(absolute)
    if os.path.basename(parent).lower() == "subagents":
        session_dir = os.path.dirname(parent)
        return os.path.join(temp_root, os.path.basename(os.path.dirname(session_dir)), os.path.basename(session_dir))
    return os.path.join(temp_root, os.path.basename(parent), os.path.splitext(os.path.basename(absolute))[0])


def is_live(gate, now, local_session_dir=None):
    """A gate still running on THIS machine. A background task counts only while its output file exists
    here, and a peer's pending foreground call only while that session's temp directory exists here,
    because transcripts sync across machines and neither of those does."""
    if gate["state"] == PENDING:
        started = parse_timestamp(gate["timestamp"])
        if started is None or now - started > FOREGROUND_LIMIT_SECONDS:
            return False
        return local_session_dir is None or os.path.isdir(local_session_dir)
    if gate["state"] == BACKGROUND:
        output_file = gate.get("output_file")
        age = file_age(output_file, now) if output_file else None
        return age is not None and age <= BACKGROUND_STALE_SECONDS
    return False


def session_files(transcript_path):
    stem = os.path.splitext(transcript_path)[0]
    return [transcript_path] + sorted(glob.glob(os.path.join(stem, "subagents", "*.jsonl")))


def peer_files(transcript_path, now):
    projects_root = os.path.dirname(os.path.dirname(os.path.abspath(transcript_path)))
    own_stem = os.path.normcase(os.path.splitext(os.path.abspath(transcript_path))[0])
    candidates = glob.glob(os.path.join(projects_root, "*", "*.jsonl"))
    candidates += glob.glob(os.path.join(projects_root, "*", "*", "subagents", "*.jsonl"))
    for path in candidates:
        if os.path.normcase(os.path.abspath(path)).startswith(own_stem):
            continue
        age = file_age(path, now)
        if age is not None and age <= PEER_WINDOW_SECONDS:
            yield path


def current_call_id(gates, command, tool_use_id):
    """The launch record of the call being judged, which the transcript may already hold."""
    ids = {gate["id"] for gate in gates}
    if tool_use_id in ids:
        return tool_use_id
    matching = [gate for gate in gates if gate["state"] == PENDING and gate["command"] == command]
    return max(matching, key=lambda gate: gate["timestamp"] or "")["id"] if matching else None


def evaluate(command, transcript_path, tool_use_id=None, now=None, temp_root=None, workspace=None, slot_directory=None):
    """The whole decision as data: (decision, reason). Split out so the harness can call it in-process."""
    now = time.time() if now is None else now
    temp_root = TEMP_CLAUDE_ROOT if temp_root is None else temp_root
    workspace = WORKSPACE if workspace is None else workspace
    cache = {}

    def classifier(text):
        if not isinstance(text, str):
            return None
        if text not in cache:
            cache[text] = classify(text)
        return cache[text]

    abuse = script_abuse(command, workspace)
    if abuse:
        return DENY, abuse
    if repeated_poll(command, transcript_path, tool_use_id):
        return DENY, REPEATED_POLL_REASON
    kind = classifier(command)
    if kind is None:
        return DEFER, ""
    if kind == INLINE:
        return DENY, INLINE_REASON
    selected = selected_tests(command, workspace)
    if selected is not None:
        return evaluate_selected(command, selected, transcript_path, tool_use_id, now, temp_root, classifier,
                                 slot_directory)
    probed = probe_steps(command, workspace)
    if probed is not None:
        return evaluate_probe(command, probed, workspace, transcript_path, tool_use_id, now, temp_root, classifier,
                              slot_directory)
    narrowed = narrowing(command)
    # Computed unconditionally: the blast-radius check below applies to a script that is NOT narrowed,
    # so deriving the delta only for an already-narrowed script would leave that check unable to fire.
    delta = declared_delta(command)
    selection = harness_selection(command)
    if narrowed and delta is None and selection is None:
        return DENY, NARROWED_REASON.format(match=narrowed)
    targeted = narrowed and delta is not None
    bypassed = [] if targeted else bypassed_gates(command, workspace)
    if bypassed:
        return DENY, CHECKED_IN_REASON.format(
            commands="; ".join("pwsh -File \"{0}\" -Goal '<the pinned goal, verbatim>'".format(gate)
                               for gate in bypassed))
    if calls_selected_runner_elsewhere(command, workspace):
        return DENY, SELECTED_ELSEWHERE_REASON
    missing = [] if targeted else missing_steps(command, workspace)
    if missing:
        return DENY, MISSING_REASON.format(missing="; ".join(missing))
    undeclared = undeclared_step_inputs(command, workspace)
    if undeclared:
        return DENY, STEP_INPUTS_REASON.format(repo=checked_in_gate_repo(command, workspace), problems=undeclared)
    steps_problem = steps_argument_problem(command, workspace)
    if steps_problem:
        return DENY, steps_problem
    clean = clean_checkout(command, workspace)
    if clean:
        return DENY, CLEAN_REASON.format(checkout=clean)
    green = green_record(command, workspace)
    if green:
        return DENY, GREEN_REASON.format(repo=green["repo"], at=green.get("completed_at"), machine=green.get("machine"))
    pending = peer_green_unlanded(command, workspace)
    if pending:
        peer, record, head = pending
        return DENY, PEER_GREEN_REASON.format(repo=record.get("repo") or "Tools", at=record.get("completedAt"),
                                              peer=peer, head=head[0][:7], head_at=head[2])
    if not isinstance(transcript_path, str) or not os.path.isfile(transcript_path):
        return DEFER, ""

    cache_root = os.path.join(temp_root, SCAN_CACHE_DIRECTORY)
    goal = pinned_goal(transcript_path, cache_root)
    if goal and not restates_goal(goal_argument(command), goal):
        return DENY, GOAL_REASON.format(goal=re.sub(r"\s+", " ", goal).strip())

    gated_repo = checked_in_gate_repo(command, workspace)
    if gated_repo:
        unfinished = work_product_problem(transcript_path, workspace, temp_root, repos=[gated_repo], allow_plants=True)
        if unfinished:
            return DENY, unfinished[1]

    own_gates, own_edits = collect(session_files(transcript_path), classifier, cache_root=cache_root)
    unbuilt = build_problem(command, workspace, transcript_path, cache_root, own_edits)
    if unbuilt:
        return DENY, unbuilt
    current = current_call_id(own_gates, command, tool_use_id)
    running_anything = [gate for gate in own_gates if gate["id"] != current]
    prior = [gate for gate in running_anything if selected_tests(gate["command"], workspace) is None
             and probe_steps(gate["command"], workspace) is None]
    launched = [stamp for stamp in (parse_timestamp(gate["timestamp"]) for gate in prior) if stamp is not None]

    if narrowed and selection is not None:
        problem = unverified_harness_selection(command, selection, own_edits)
        if problem:
            return DENY, HARNESS_SELECTION_REASON.format(match=narrowed, problem=problem)
    elif narrowed:
        problem = unverified_delta(command, delta, own_edits, max(launched) if launched else None)
        if problem:
            return DENY, DELTA_REASON.format(match=narrowed, problem=problem)

    # Blast radius, and only on a RE-RUN: the first full run is the baseline that earns the verdicts a
    # later run is allowed to keep, so this never fires on it.
    if launched and test_files_only(delta):
        whole_tier = full_test_run(command)
        if whole_tier:
            return DENY, BLAST_RADIUS_REASON.format(match=whole_tier)

    busy = machine_busy(running_anything, transcript_path, now, temp_root, cache_root, classifier, slot_directory)
    if busy:
        return busy

    ordered = sorted((gate for gate in prior if parse_timestamp(gate["timestamp"]) is not None),
                     key=lambda gate: parse_timestamp(gate["timestamp"]))
    if ordered and ordered[-1].get("stopped"):
        return ASK, STOPPED_REASON.format(command=ordered[-1]["command"])

    checkout_launched, checkout_edits = same_checkout_history(command, workspace, prior, own_edits)
    if checkout_launched:
        last_gate = max(checkout_launched)
        if not any(stamp > last_gate for stamp, _ in checkout_edits) and not checkout_changed_since(command, last_gate):
            stamp = datetime.fromtimestamp(last_gate).isoformat(timespec="seconds")
            return ASK, UNCHANGED_REASON.format(timestamp=stamp)

    return DEFER, ""


def same_checkout_history(command, workspace, prior, own_edits):
    """(launch stamps, edits) that bear on whether this gate repeats an earlier one. For a checked-in gate.ps1 that
    is the earlier gates of the same checkout and the edits inside it, plus every edit whose target is not a path
    (a shell command that rewrites files); a first gate on one checkout after a gate on another is not a re-run.
    Any other gate script keeps every earlier gate and every edit."""
    stamps = lambda gates: [s for s in (parse_timestamp(gate["timestamp"]) for gate in gates) if s is not None]
    gated = gate_checkout(command, workspace)
    if not gated:
        return stamps(prior), own_edits
    checkout = gated[1]
    earlier = [(gate, gate_checkout(gate.get("command"), workspace)) for gate in prior]
    same = [gate for gate, found in earlier if found and same_path(found[1], checkout)]
    inside = [(stamp, target) for stamp, target in own_edits
              if not (isinstance(target, str) and os.path.isabs(target)) or is_under(target, checkout)]
    return stamps(same), inside


def is_under(path, folder):
    """Whether path is folder or lies inside it, compared as same_path does."""
    child = os.path.normcase(os.path.realpath(path))
    parent = os.path.normcase(os.path.realpath(folder))
    return child == parent or child.startswith(parent.rstrip("\\/") + os.sep)


def needs_build(checkout):
    """Whether a checkout has a build to prove: a .sln or .slnx at its root, or a package.json whose scripts
    include build. A repo with neither (Tools, Curator) has nothing a build would compile."""
    if glob.glob(os.path.join(checkout, "*.sln")) or glob.glob(os.path.join(checkout, "*.slnx")):
        return True
    try:
        with open(os.path.join(checkout, "package.json"), "r", encoding="utf-8") as handle:
            scripts = json.load(handle).get("scripts")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(scripts, dict) and "build" in scripts


def collect_builds(paths, cache_root=None):
    """(finished at, target) for every foreground plain build in these transcripts whose call came back without
    an error, read from the same cached scan collect() uses."""
    builds = []
    for path in paths:
        for finished, target in scan_transcript(path, cache_root).get("builds", {}).values():
            stamp = parse_timestamp(finished)
            if stamp is not None and isinstance(target, str):
                builds.append((stamp, target))
    return builds


def build_problem(command, workspace, transcript_path, cache_root, own_edits):
    """The refusal for a checked-in gate launched without proof its checkout builds, else None: no successful
    foreground build of a path inside the checkout in this session, or a non-markdown change to the checkout
    after the newest one (an edit of this session, or any file git sees changed later)."""
    found = gate_checkout(command, workspace)
    if found is None or not needs_build(found[1]):
        return None
    checkout = found[1]
    solutions = sorted(glob.glob(os.path.join(checkout, "*.slnx")) + glob.glob(os.path.join(checkout, "*.sln")))
    example = ('dotnet build "{0}"'.format(solutions[0]) if solutions
               else 'npm run build --prefix "{0}"'.format(checkout))
    stamps = [stamp for stamp, target in collect_builds(session_files(transcript_path), cache_root)
              if is_under(target, checkout)]
    if not stamps:
        return BUILD_MISSING_REASON.format(checkout=checkout, example=example)
    latest = max(stamps)
    edited = any(stamp > latest and (not (isinstance(target, str) and os.path.isabs(target)) or is_under(target, checkout))
                 for stamp, target in own_edits)
    if edited or checkout_changed_since(command, latest, build_inputs_only=True):
        return BUILD_STALE_REASON.format(checkout=checkout, example=example,
                                         at=datetime.fromtimestamp(latest).isoformat(timespec="seconds"))
    return None


def checkout_changed_since(command, since, build_inputs_only=False):
    """True when git changed the gated script's checkout after `since` (POSIX seconds): a HEAD committed later (a
    pull, merge or rebase), or a changed or untracked file written later. Such a change reaches the tree without an
    Edit, so a re-run after it is not a repeat. build_inputs_only leaves out .md files, which no build reads, and a
    gate's own leftovers (gate_status.LEFTOVER), which a gate writes after the build it follows."""
    match = RE_SCRIPT_CALL.match(command or "")
    path = match.group("path").strip('"') if match else None
    if not path:
        return False
    checkout = os.path.dirname(os.path.abspath(path))
    if not os.path.exists(os.path.join(checkout, ".git")):
        return False

    def git(*arguments):
        try:
            done = subprocess.run(["git", "-C", checkout] + list(arguments), capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return ""
        return done.stdout if done.returncode == 0 else ""

    head = git("log", "-1", "--format=%ct").strip()
    if head.isdigit() and int(head) > since:
        return True
    for line in git("status", "--porcelain", "--untracked-files=all").splitlines():
        name = line[3:].split(" -> ")[-1].strip().strip('"')
        if build_inputs_only and (name.lower().endswith(".md") or gate_status_module().LEFTOVER.search(name)):
            continue
        try:
            if os.path.getmtime(os.path.join(checkout, name)) > since:
                return True
        except OSError:
            continue
    return False


def gate_slot_directory():
    """Where Harness/Gates/GateSlot.ps1 keeps the slot: under %TEMP%\\claude (CLAUDE_CODE_TMPDIR when set), and nowhere
    else."""
    return claude_temp.gate_slot_root()


def gate_slot_locked(directory):
    """True while a process holds the slot's file lock. GateSlot.ps1 opens the lock with FileShare.None for
    the life of its holder, so an ordinary open fails with a sharing violation exactly while it is held; a
    crash closes the handle and frees the slot. Access denied cannot tell held from free and reads as held.
    open() goes through the C runtime, which raises both as a PermissionError with no winerror."""
    try:
        with open(os.path.join(directory, GATE_SLOT_LOCK), "rb"):
            return False
    except FileNotFoundError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def gate_slot_holder(directory=None):
    """A description of the slot's holder while its file lock is held, else None. Only the lock decides:
    the record beside it is written by whoever holds the slot, so a record alone, forged or left behind by a
    crash, never blocks a gate. It only names the holder in the denial."""
    directory = gate_slot_directory() if directory is None else directory
    if not gate_slot_locked(directory):
        return None
    try:
        with open(os.path.join(directory, GATE_SLOT_RECORD), encoding="utf-8-sig") as handle:
            holder = json.load(handle)
    except (OSError, ValueError):
        holder = None
    return holder if isinstance(holder, dict) else {"owner": "a holder that left no record", "pid": "unknown",
                                                    "acquiredAt": "unknown"}


def machine_busy(own_prior, transcript_path, now, temp_root, cache_root, classifier, slot_directory=None):
    """(decision, reason) when this session or a peer on this machine has a gate or a tests-only run in
    flight, or a live process holds the machine's gate slot, else None."""
    in_flight = [gate for gate in own_prior if is_live(gate, now)]
    if in_flight:
        return DENY, IN_FLIGHT_REASON.format(command=in_flight[-1]["command"])
    peer = live_peer_gate(transcript_path, now, temp_root, classifier)
    if peer:
        return DENY, PEER_REASON.format(command=peer[0], transcript=peer[1])
    holder = gate_slot_holder(slot_directory)
    if holder:
        return DENY, SLOT_REASON.format(owner=holder.get("owner"), pid=holder.get("pid"), since=holder.get("acquiredAt"))
    return None


def evaluate_selected(command, names, transcript_path, tool_use_id, now, temp_root, classifier, slot_directory=None):
    """The decision for a tests-only run: every name verified against this session's edited test files, and
    the machine free. No goal, baseline, delta or unchanged-tree check applies, because it is not a gate."""
    if not names:
        return DENY, SELECTED_REASON.format(problem=unverified_test_names(names, []))
    if not isinstance(transcript_path, str) or not os.path.isfile(transcript_path):
        return ASK, SELECTED_REASON.format(problem="this session's transcript is unreadable")
    cache_root = os.path.join(temp_root, SCAN_CACHE_DIRECTORY)
    own_gates, own_edits = collect(session_files(transcript_path), classifier, cache_root=cache_root)
    problem = unverified_test_names(names, own_edits)
    if problem:
        return DENY, SELECTED_REASON.format(problem=problem)
    current = current_call_id(own_gates, command, tool_use_id)
    busy = machine_busy([gate for gate in own_gates if gate["id"] != current], transcript_path, now, temp_root,
                        cache_root, classifier, slot_directory)
    return busy or (DEFER, "")


def decide(command, transcript_path, tool_use_id=None, evaluator=None):
    """evaluate() for a gate command, run to completion: the hook keeps no clock of its own. Claude Code's hook
    timeout bounds it, and the dispatcher's settings entry carries onFailure "block", so a hook that hangs or
    crashes blocks the call rather than letting it through. An evaluation that raises denies with the error's
    name, never asks, since a session the operator does not watch (the master) cannot answer a prompt. A command
    classify() does not call a gate never reaches the evaluator."""
    evaluator = evaluate if evaluator is None else evaluator
    try:
        abuse = script_abuse(command, WORKSPACE)
        if abuse:
            return DENY, abuse
        if classify(command) is None:
            return DEFER, ""
        return evaluator(command, transcript_path, tool_use_id)
    except Exception as error:
        return DENY, FAILED_REASON.format(error=type(error).__name__)


def main():
    raw = sys.stdin.read()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    decision, reason = decide(command, payload.get("transcript_path"), payload.get("tool_use_id"))
    if decision == DEFER:
        return 0
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }, separators=(",", ":")))
    return 1 if decision == DENY else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
