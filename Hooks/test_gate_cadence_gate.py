# GATE-STEP: Gate-cadence hook harness
"""Regression harness for invoke_gate_cadence_gate.py. Exits non-zero on any regression.

Runs IN-PROCESS against evaluate(), over fixture transcripts built in the record shape Claude Code writes:
assistant tool_use blocks, user tool_result blocks carrying toolUseResult, and queue-operation records
carrying <task-notification>. Every decision is pinned in both directions.

Each case gets its own fixture root. The peer scan reads every transcript under a projects directory, so
fixtures sharing one would leak each other's live gates into every later case.

    python Harness/Hooks/test_gate_cadence_gate.py
    python Harness/Hooks/test_gate_cadence_gate.py --only peer
"""

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import invoke_gate_cadence_gate as gate  # noqa: E402

CODE_FILE = "C:\\repo\\src\\app.ts"

# Required on a pull request: lint, the unit tier (project and trait, through a backtick continuation) and e2e.
# Not required: npm ci (setup), a dispatch-only load tier, a push-only integration tier whose if: follows its
# run:, and a push-only deploy job.
APP_WORKFLOW = "\n".join([
    "on:",
    "  push:",
    "    branches: [main]",
    "  pull_request:",
    "jobs:",
    "  build:",
    "    runs-on: windows-latest",
    "    steps:",
    "      - run: npm ci",
    "      - name: Lint",
    "        run: npm run lint",
    "      - name: Unit tests",
    "        run: |",
    "          dotnet coverlet App.Tests.Unit\\bin\\Release\\net10.0 `",
    "            --targetargs \"test --project App.Tests.Unit -- --filter-trait Category=Unit\"",
    "      - name: Load tests",
    "        if: github.event_name == 'workflow_dispatch'",
    "        run: dotnet test --project App.Tests.E2E -- --filter-trait Category=Load",
    "      - name: Integration tests",
    "        run: dotnet test --project App.Tests.Unit -- --filter-trait Category=Integration",
    "        if: github.event_name != 'pull_request'",
    "      - run: npm run e2e",
    "  deploy:",
    "    if: github.event_name == 'push'",
    "    steps:",
    "      - run: npm run build",
    ""])

NPM_TEST_WORKFLOW = "on:\n  pull_request:\njobs:\n  build:\n    steps:\n      - run: npm run build\n      - run: npm test\n"

JB_WORKFLOW = ("on:\n  pull_request:\njobs:\n  build:\n    steps:\n      - run: dotnet build --configuration Release\n"
               "      - name: ReSharper inspection\n        run: |\n"
               "          dotnet jb inspectcode App.slnx --no-build -e=WARNING -o=inspectcode.sarif\n")

SCHEDULED_WORKFLOW ="on:\n  schedule:\n    - cron: '0 6 * * *'\njobs:\n  walk:\n    steps:\n      - run: npm run lint\n"


HELD_LOCKS = []


def hold_exclusively(path):
    """A handle on `path` opened with no sharing, the way GateSlot.ps1 holds the slot (FileShare.None)."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(path, 0xC0000000, 0, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "could not hold {0} exclusively".format(path))
    return handle


def release_held_locks():
    import ctypes
    while HELD_LOCKS:
        ctypes.WinDLL("kernel32").CloseHandle(HELD_LOCKS.pop())


def clear_readonly_and_retry(func, path, exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


def long_path_form(path):
    """The \\\\?\\ form of an absolute path on Windows, which every file call accepts past MAX_PATH even with long
    paths off: a fixture checkout's gate records nest its full path inside the fixture root, so rmtree's unlink of a
    record past 260 characters fails as not found, the folder is left and its rmdir fails."""
    full = os.path.abspath(path)
    return "\\\\?\\" + full if os.name == "nt" and not full.startswith("\\\\") else full


def iso(seconds_ago):
    return datetime.fromtimestamp(time.time() - seconds_ago, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def new_tool_id():
    return "toolu_" + uuid.uuid4().hex


def tool_use(seconds_ago, tool_id, name, tool_input):
    return {"type": "assistant", "timestamp": iso(seconds_ago), "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]}}


def tool_result(seconds_ago, tool_id, text, tool_use_result):
    return {"type": "user", "timestamp": iso(seconds_ago), "toolUseResult": tool_use_result,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": text}]}}


def notification(seconds_ago, task_id):
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(seconds_ago),
            "content": "<task-notification>\n<task-id>{0}</task-id>\n<status>completed</status>\n"
                       "</task-notification>".format(task_id)}


def temp_root_for(transcript_path):
    """The fixture's stand-in for %TEMP%\\claude: <fixture root>\\temp\\claude, beside projects\\."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(transcript_path))), "temp", "claude")


def workspace_for(transcript_path):
    """The fixture's stand-in for the crgolden workspace: <fixture root>\\workspace, beside projects\\."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(transcript_path))), "workspace")


def edit(seconds_ago, file_path):
    return tool_use(seconds_ago, new_tool_id(), "Edit", {"file_path": file_path, "old_string": "a", "new_string": "b"})


def shell(seconds_ago, command):
    return tool_use(seconds_ago, new_tool_id(), "PowerShell", {"command": command})


def operator(seconds_ago, text):
    return {"type": "user", "timestamp": iso(seconds_ago), "origin": {"kind": "human"},
            "message": {"role": "user", "content": text}}


def goal_status(seconds_ago, condition, met=False):
    """The attachment Claude Code writes for /goal: on setting it (sentinel), at each stop it judges, and once met."""
    attachment = {"type": "goal_status", "met": met, "condition": condition}
    if not met:
        attachment["sentinel"] = True
    return {"type": "attachment", "timestamp": iso(seconds_ago), "attachment": attachment}


def session_words(seconds_ago, text):
    """A user-type record with no human origin: text a session or a tool put in the transcript, never the operator's."""
    return {"type": "user", "timestamp": iso(seconds_ago), "message": {"role": "user", "content": text}}


def human_turn(seconds_ago, text):
    """An operator turn as Claude Code records one: origin human, string content."""
    return dict(operator(seconds_ago, text), origin={"kind": "human"})


def human_mid_turn(seconds_ago, text):
    """An operator message sent mid-turn: a queued_command attachment with origin human."""
    return {"type": "attachment", "timestamp": iso(seconds_ago), "attachment": {
        "type": "queued_command", "prompt": text, "origin": {"kind": "human"}, "humanTurn": True}}


def declare(transcript_path, repos, dropped=None):
    """Write the session's work-product declaration where the hook reads it."""
    directory = gate.session_home.work_products_dir()
    os.makedirs(directory, exist_ok=True)
    body = {"repos": {repo: [dict({"item": entry[0], "done": entry[1]}, **({"plant": entry[2]} if len(entry) > 2 else {}))
                             for entry in items] for repo, items in repos.items()}}
    if dropped is not None:
        body["dropped"] = {repo: [{"item": item, "approval": approval} for item, approval in entries]
                           for repo, entries in dropped.items()}
    with open(os.path.join(directory, gate.session_stem(transcript_path) + ".json"), "w", encoding="utf-8") as handle:
        json.dump(body, handle)


def seed_ledger(transcript_path, repos):
    """Items the hook already read from an earlier version of the declaration."""
    directory = os.path.join(temp_root_for(transcript_path), gate.SCAN_CACHE_DIRECTORY, "work-products")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, gate.session_stem(transcript_path) + ".json"), "w", encoding="utf-8") as handle:
        json.dump(repos, handle)


CLOSED = {"App": [("the whole deliverable in code.cs and gate.ps1", True)]}


class Fixture:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="gate-cadence-", dir=claude_temp.scratch_dir())
        self.slug = os.path.join(self.root, "projects", "C--workspace")
        self.tasks = os.path.join(self.root, "tasks")
        os.makedirs(self.slug)
        os.makedirs(self.tasks)
        self.gate_script = self.write_file("librarian-gate.ps1", ["npm ci", "npm run lint", "npm run e2e"])
        self.probe_script = self.write_file("probe.ps1", ["Get-ChildItem C:\\repo"])
        self.harness_runner = self.write_file("tools-gate.py", [
            "import subprocess, sys",
            "subprocess.run([sys.executable, r'C:\\repo\\Tools\\Claude\\test_widget_gate.py'])"])
        self.gate_command = 'pwsh -File "{0}"'.format(self.gate_script)

    def write_file(self, name, lines):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def transcript(self, records, subagent_of=None, age_seconds=None):
        session = str(uuid.uuid4())
        if subagent_of:
            directory = os.path.join(os.path.splitext(subagent_of)[0], "subagents")
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, "agent-{0}.jsonl".format(session))
        else:
            path = os.path.join(self.slug, session + ".jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        if age_seconds is not None:
            stamp = time.time() - age_seconds
            os.utime(path, (stamp, stamp))
        return path

    def background_launch(self, seconds_ago, command, output_exists=True):
        tool_id = new_tool_id()
        task_id = "b" + uuid.uuid4().hex[:8]
        output_file = os.path.join(self.tasks, task_id + ".output")
        if output_exists:
            with open(output_file, "w", encoding="utf-8") as handle:
                handle.write("running\n")
        text = ("Command running in background with ID: {0}. Output is being written to: {1}. "
                "You will be notified when it completes.").format(task_id, output_file)
        records = [
            tool_use(seconds_ago, tool_id, "PowerShell", {"command": command, "run_in_background": True}),
            tool_result(seconds_ago - 1, tool_id, text, {"stdout": "", "backgroundTaskId": task_id}),
        ]
        return records, task_id

    @staticmethod
    def foreground_gate(seconds_ago, command):
        tool_id = new_tool_id()
        return [
            tool_use(seconds_ago, tool_id, "PowerShell", {"command": command}),
            tool_result(seconds_ago - 5, tool_id, "GATE: PASSED", {"stdout": "GATE: PASSED"}),
        ]


def build_cases(fixtures):
    """Each builder returns (command, transcript_path, tool_use_id) inside its own fresh fixture."""

    def fresh():
        fixture = Fixture()
        fixtures.append(fixture)
        return fixture

    def plain(command):
        fixture = fresh()
        return command, fixture.transcript([]), None

    def gate_script_first():
        fixture = fresh()
        return fixture.gate_command, fixture.transcript([]), None

    def probe_script():
        fixture = fresh()
        return 'pwsh -File "{0}"'.format(fixture.probe_script), fixture.transcript([]), None

    def narrowed_script(lines):
        def build():
            fixture = fresh()
            path = fixture.write_file("narrowed-gate.ps1", lines)
            return 'pwsh -File "{0}"'.format(path), fixture.transcript([]), None
        return build

    def narrowed_delta(lines, records_for=lambda fixture: []):
        def build():
            fixture = fresh()
            path = fixture.write_file("narrowed-gate.ps1", lines)
            return 'pwsh -File "{0}"'.format(path), fixture.transcript(records_for(fixture)), None
        return build

    def after_gate(*records):
        return lambda fixture: fixture.foreground_gate(300, fixture.gate_command) + list(records)

    def before_gate(*records):
        return lambda fixture: list(records) + fixture.foreground_gate(300, fixture.gate_command)

    def harness_quoting_narrowing():
        fixture = fresh()
        path = fixture.write_file("test_fixture_holder.py", ["CASES = ['npm run e2e:run -- --grep palette']"])
        return 'python "{0}"'.format(path), fixture.transcript([]), None

    def relaunch_after_stop(finished_gate_after):
        def build():
            fixture = fresh()
            records, task_id = fixture.background_launch(900, fixture.gate_command)
            records.append(tool_use(800, new_tool_id(), "TaskStop", {"task_id": task_id}))
            if finished_gate_after:
                records += fixture.foreground_gate(600, fixture.gate_command)
            records.append(edit(300, CODE_FILE))
            return fixture.gate_command, fixture.transcript(records), None
        return build

    def harness_runner(prior_gate):
        def build():
            fixture = fresh()
            command = 'python "{0}"'.format(fixture.harness_runner)
            records = fixture.foreground_gate(300, command) if prior_gate else []
            return command, fixture.transcript(records), None
        return build

    def own_background_running():
        fixture = fresh()
        records, _ = fixture.background_launch(120, fixture.gate_command)
        return fixture.gate_command, fixture.transcript(records + [edit(60, CODE_FILE)]), None

    def own_background_running_with_request_copy():
        """Claude Code writes an api-request-blob after the launch: the same tool_use and tool_result blocks with no
        timestamp and no toolUseResult. Read as a record, it overwrote the launch and its task id."""
        fixture = fresh()
        records, _ = fixture.background_launch(120, fixture.gate_command)
        blocks = records[0]["message"]["content"] + records[1]["message"]["content"]
        copy = {"type": gate.REQUEST_COPY_TYPE, "hash": "h", "sessionId": "s",
                "message": {"role": "user", "content": blocks}}
        return fixture.gate_command, fixture.transcript(records + [copy, edit(60, CODE_FILE)]), None

    def gate_record_read_while_own_gate_runs():
        fixture = fresh()
        records, _ = fixture.background_launch(120, fixture.gate_command)
        reader = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Gates", "gate_status.py")
        return 'python "{0}"'.format(reader), fixture.transcript(records + [edit(60, CODE_FILE)]), None

    def own_background_finished_then_edit():
        fixture = fresh()
        records, task_id = fixture.background_launch(600, fixture.gate_command)
        records += [notification(300, task_id), edit(200, CODE_FILE)]
        return fixture.gate_command, fixture.transcript(records), None

    def own_background_finished_no_edit():
        fixture = fresh()
        records, task_id = fixture.background_launch(600, fixture.gate_command)
        return fixture.gate_command, fixture.transcript(records + [notification(300, task_id)]), None

    def own_background_output_absent():
        fixture = fresh()
        records, _ = fixture.background_launch(120, fixture.gate_command, output_exists=False)
        return fixture.gate_command, fixture.transcript(records + [edit(60, CODE_FILE)]), None

    def parallel_pending(seconds_ago):
        def build():
            fixture = fresh()
            other = 'python "C:\\absent\\test_other.py"'
            records = [tool_use(seconds_ago, new_tool_id(), "PowerShell", {"command": other})]
            if seconds_ago > gate.FOREGROUND_LIMIT_SECONDS:
                records.append(edit(seconds_ago - 60, CODE_FILE))
            return fixture.gate_command, fixture.transcript(records), None
        return build

    def current_call(pass_id):
        def build():
            fixture = fresh()
            tool_id = new_tool_id()
            transcript = fixture.transcript([tool_use(1, tool_id, "PowerShell", {"command": fixture.gate_command})])
            return fixture.gate_command, transcript, tool_id if pass_id else None
        return build

    def regate(extra_before, extra_after):
        def build():
            fixture = fresh()
            records = extra_before + fixture.foreground_gate(300, fixture.gate_command) + extra_after
            return fixture.gate_command, fixture.transcript(records), None
        return build

    def git_changed_regate(committed_seconds_ago):
        """A re-gate whose gated script's checkout is a real repo, last committed `committed_seconds_ago`, after a
        gate 300 s ago; the session made no edit in between."""
        def build():
            fixture = fresh()
            quiet = {"cwd": fixture.root, "capture_output": True, "text": True}
            subprocess.run(["git", "init"], **quiet)
            subprocess.run(["git", "config", "core.fsmonitor", "false"], **quiet)
            with open(os.path.join(fixture.root, ".gitignore"), "w", encoding="utf-8") as handle:
                handle.write("/*\n!/*.ps1\n!/.gitignore\n")
            subprocess.run(["git", "add", "-A"], **quiet)
            stamp = "@{0} +0000".format(int(time.time() - committed_seconds_ago))
            committed = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "pulled"],
                                       env=dict(os.environ, GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp), **quiet)
            if committed.returncode != 0:
                raise RuntimeError("fixture commit failed: " + committed.stderr.strip())
            records = fixture.foreground_gate(300, fixture.gate_command)
            return fixture.gate_command, fixture.transcript(records), None
        return build

    def goal_gate(pins, script_lines, steps=("npm ci", "npm run lint")):
        def build():
            fixture = fresh()
            path = fixture.write_file("goal-gate.ps1", script_lines + list(steps))
            records = [operator(900 - index * 10, pin) for index, pin in enumerate(pins)]
            return 'pwsh -File "{0}"'.format(path), fixture.transcript(records), None
        return build

    def goal_records_gate(records, steps=("npm ci", "npm run lint")):
        def build():
            fixture = fresh()
            path = fixture.write_file("goal-gate.ps1", list(steps))
            return 'pwsh -File "{0}"'.format(path), fixture.transcript(records), None
        return build

    def goal_argument_gate(pins, goal_argument, quote="'"):
        def build():
            fixture = fresh()
            path = fixture.write_file("gate.ps1", ["npm ci", "npm run lint"])
            records = [operator(900 - index * 10, pin) for index, pin in enumerate(pins)]
            escaped = goal_argument.replace(quote, quote * 2)
            return "pwsh -File \"{0}\" -Goal {1}{2}{1}".format(path, quote, escaped), fixture.transcript(records), None
        return build

    def goal_command_gate(records, goal_argument):
        def build():
            fixture = fresh()
            path = fixture.write_file("gate.ps1", ["npm ci", "npm run lint"])
            return "pwsh -File \"{0}\" -Goal '{1}'".format(path, goal_argument), fixture.transcript(records), None
        return build

    def goal_argument_double(pins, goal_argument):
        def build():
            fixture = fresh()
            path = fixture.write_file("gate.ps1", ["npm ci", "npm run lint"])
            records = [operator(900 - index * 10, pin) for index, pin in enumerate(pins)]
            return "pwsh -File \"{0}\" -Goal \"{1}\"".format(path, goal_argument), fixture.transcript(records), None
        return build

    def polled(command, between, earlier=None):
        """`command` run again after an earlier run of `earlier` (the same command by default) and `between`."""
        def build():
            fixture = fresh()
            first = new_tool_id()
            records = [tool_use(300, first, "PowerShell", {"command": earlier or command}),
                       tool_result(299, first, "RUNNING", {"stdout": "RUNNING"})]
            records += [record() if callable(record) else record for record in between]
            current = new_tool_id()
            records.append(tool_use(10, current, "PowerShell", {"command": command}))
            return command, fixture.transcript(records), current
        return build

    def own_script(name, lines, pins=(), runner="pwsh -File"):
        def build():
            fixture = fresh()
            path = fixture.write_file(name, list(lines))
            records = [operator(900 - index * 10, pin) for index, pin in enumerate(pins)]
            return '{0} "{1}"'.format(runner, path), fixture.transcript(records), None
        return build

    def existing_catalog(name):
        def build():
            fixture = fresh()
            path = fixture.write_file(name, ["param([string]$OutputPath)", "Get-ChildItem C:\\repo"])
            return 'pwsh -File "{0}" -OutputPath "C:\\repo\\catalog.csv"'.format(path), fixture.transcript([]), None
        return build

    def goal_pinned_harness():
        fixture = fresh()
        path = fixture.write_file("test_widget_gate.py", ["# GOAL: ship the widget export", "import sys", "sys.exit(0)"])
        return 'python "{0}"'.format(path), fixture.transcript([operator(900, "GOAL: ship the widget export")]), None

    def summary_pin():
        fixture = fresh()
        path = fixture.write_file("goal-gate.ps1", ["npm ci", "npm run lint"])
        record = dict(operator(900, "Summary:\nGOAL: ship the widget export"), isCompactSummary=True)
        return 'pwsh -File "{0}"'.format(path), fixture.transcript([record]), None

    def custom_regate(lines):
        def build():
            fixture = fresh()
            path = fixture.write_file("custom-gate.ps1", lines)
            command = 'pwsh -File "{0}"'.format(path)
            return command, fixture.transcript(fixture.foreground_gate(300, command)), None
        return build

    def script_in_repo(workflow, script_lines, name="repo-gate.ps1", runner="pwsh -File", checked_in_gate=False):
        def build():
            fixture = fresh()
            transcript = fixture.transcript([])
            repo = os.path.join(workspace_for(transcript), "App")
            directory = os.path.join(repo, ".github", "workflows")
            os.makedirs(directory)
            with open(os.path.join(directory, "main_app.yml"), "w", encoding="utf-8") as handle:
                handle.write(workflow)
            if checked_in_gate:
                with open(os.path.join(repo, "gate.ps1"), "w", encoding="utf-8") as handle:
                    handle.write("\n".join(script_lines) + "\n")
            path = fixture.write_file(name, ["Set-Location 'C:\\Users\\someone\\source\\repos\\crgolden\\App'"]
                                      + script_lines)
            return '{0} "{1}"'.format(runner, path), transcript, None
        return build

    def repo_script(name, lines, state):
        """A script inside workspace/App, a real git repo, in one of three states: committed as given, committed
        then rewritten so the checkout reports it modified, or never added so it is untracked."""
        def build():
            fixture = fresh()
            transcript = fixture.transcript([])
            repo = os.path.join(workspace_for(transcript), "App")
            os.makedirs(repo)
            for args in (["init", "-q"], ["config", "core.fsmonitor", "false"], ["config", "user.email", "f@x"],
                         ["config", "user.name", "f"]):
                subprocess.run(["git", "-C", repo] + args, check=True)
            path = os.path.join(repo, name)
            committed = {"committed": list(lines), "modified": ["Get-Service Redis"], "untracked": None}[state]
            if committed is not None:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(committed) + "\n")
            else:
                with open(os.path.join(repo, "README.md"), "w", encoding="utf-8") as handle:
                    handle.write("# App\n")
            subprocess.run(["git", "-C", repo, "add", "--all"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "fixture"], check=True)
            if state != "committed":
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
            return 'pwsh -File "{0}"'.format(path), transcript, None
        return build

    def checked_in_gate_itself(workflow, script_lines, declaration=CLOSED, records=(), dropped=None, ledger=None,
                               repo_files=None, steps_argument=" -Steps 'Code-style catalog'"):
        def build():
            fixture = fresh()
            transcript = fixture.transcript(list(records))
            if declaration is not None:
                declare(transcript, declaration, dropped)
            if ledger is not None:
                seed_ledger(transcript, ledger)
            repo = os.path.join(workspace_for(transcript), "App")
            os.makedirs(os.path.join(repo, ".github", "workflows"))
            for relative, content in (repo_files or {}).items():
                with open(os.path.join(repo, relative), "w", encoding="utf-8") as handle:
                    handle.write(content)
            with open(os.path.join(repo, ".github", "workflows", "main_app.yml"), "w", encoding="utf-8") as handle:
                handle.write(workflow)
            path = os.path.join(repo, "gate.ps1")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(script_lines) + "\n")
            return 'pwsh -File "{0}"{1}'.format(path, steps_argument), transcript, None
        return build

    def recorded_gate(verdict="PASSED", edit_after_record=False):
        """A git repo with a checked-in gate.ps1 and a gate record written the way GateCommon writes one."""
        def build():
            fixture = fresh()
            transcript = fixture.transcript([])
            declare(transcript, CLOSED)
            workspace = workspace_for(transcript)
            repo = os.path.join(workspace, "App")
            os.makedirs(repo)
            subprocess.run(["git", "-C", repo, "init", "-q"], check=True)
            subprocess.run(["git", "-C", repo, "config", "core.fsmonitor", "false"], check=True)
            with open(os.path.join(repo, "code.cs"), "w", encoding="utf-8") as handle:
                handle.write("class A {}\n")
            path = os.path.join(repo, "gate.ps1")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("npm test\n")
            statuses = gate.gate_status_module()
            files = statuses.manifest(repo)
            tools = statuses.tool_hashes()
            record = {"repo": "App", "verdict": verdict, "startedAt": "2026-01-01T00:00:00Z",
                      "completedAt": "2026-01-01T00:05:00Z", "machine": "fixture", "steps": [],
                      "manifest": files, "tools": tools, "fingerprint": statuses.fingerprint(files, tools)}
            os.makedirs(statuses.checkout_state_dir("App", repo))
            with open(statuses.record_path("App", statuses.checkout_state_dir("App", repo)), "w",
                      encoding="utf-8") as handle:
                json.dump(record, handle)
            if edit_after_record:
                with open(os.path.join(repo, "code.cs"), "a", encoding="utf-8") as handle:
                    handle.write("class B {}\n")
            return 'pwsh -File "{0}" -Steps \'Code-style catalog\''.format(path), transcript, None
        return build

    def clean_gate(leftover=False, pending=False):
        """A git repo whose gate.ps1 and every file are committed, optionally with only a gate's StrykerOutput/
        leftover beside them, or with one uncommitted source file."""
        def build():
            fixture = fresh()
            transcript = fixture.transcript([])
            declare(transcript, CLOSED)
            repo = os.path.join(workspace_for(transcript), "App")
            os.makedirs(repo)
            for args in (["init", "-q"], ["config", "core.fsmonitor", "false"], ["config", "user.email", "f@x"],
                         ["config", "user.name", "f"]):
                subprocess.run(["git", "-C", repo] + args, check=True)
            with open(os.path.join(repo, "code.cs"), "w", encoding="utf-8") as handle:
                handle.write("class A {}\n")
            path = os.path.join(repo, "gate.ps1")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("Register-GateSteps @('Run tests')\nRegister-StepInputs @{\n    'Run tests' = @('*')\n}\n"
                             "Resolve-OwedSteps\nnpm test\n")
            subprocess.run(["git", "-C", repo, "add", "--all"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "fixture"], check=True)
            if leftover:
                os.makedirs(os.path.join(repo, "StrykerOutput"))
                with open(os.path.join(repo, "StrykerOutput", "report.json"), "w", encoding="utf-8") as handle:
                    handle.write("{}\n")
            if pending:
                with open(os.path.join(repo, "code.cs"), "a", encoding="utf-8") as handle:
                    handle.write("class B {}\n")
            return 'pwsh -File "{0}"'.format(path), transcript, None
        return build

    def built_gate(build_ago=None, change_ago=600, background=False, failed=False, changed_after="",
                   build_inside=True):
        """A committed git repo with an App.slnx and one uncommitted source change written change_ago seconds ago,
        and optionally a plain dotnet build of it build_ago seconds ago (in the background, or failing). A file
        named changed_after is written after the build."""
        def build():
            fixture = fresh()
            repo = os.path.join(fixture.root, "workspace", "App")
            os.makedirs(repo)
            for args in (["init", "-q"], ["config", "core.fsmonitor", "false"], ["config", "user.email", "f@x"],
                         ["config", "user.name", "f"]):
                subprocess.run(["git", "-C", repo] + args, check=True)
            for name, text in (("code.cs", "class A {}\n"), ("App.slnx", "<Solution />\n"), ("README.md", "# App\n")):
                with open(os.path.join(repo, name), "w", encoding="utf-8") as handle:
                    handle.write(text)
            path = os.path.join(repo, "gate.ps1")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("Register-GateSteps @('Run tests')\nRegister-StepInputs @{\n    'Run tests' = @('*')\n}\n"
                             "Resolve-OwedSteps\ndotnet test\n")
            subprocess.run(["git", "-C", repo, "add", "--all"], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "fixture", "--date", "2020-01-01T00:00:00"],
                           check=True, env=dict(os.environ, GIT_COMMITTER_DATE="2020-01-01T00:00:00"))
            with open(os.path.join(repo, "code.cs"), "a", encoding="utf-8") as handle:
                handle.write("class B {}\n")
            stamp = time.time() - change_ago
            os.utime(os.path.join(repo, "code.cs"), (stamp, stamp))
            records = []
            if build_ago is not None:
                tool_id = new_tool_id()
                target = os.path.join(repo, "App.slnx") if build_inside else os.path.join(fixture.root, "Other.slnx")
                tool_input = {"command": 'dotnet build "{0}" --configuration Release'.format(target)}
                if background:
                    tool_input["run_in_background"] = True
                records.append(tool_use(build_ago, tool_id, "PowerShell", tool_input))
                result = tool_result(build_ago - 30, tool_id, "Exit code 1\nBuild FAILED." if failed else "Build succeeded.",
                                     {"stdout": ""})
                if failed:
                    result["message"]["content"][0]["is_error"] = True
                records.append(result)
            if changed_after:
                target = os.path.join(repo, changed_after)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "a", encoding="utf-8") as handle:
                    handle.write("changed\n")
            transcript = fixture.transcript(records)
            declare(transcript, CLOSED)
            return 'pwsh -File "{0}"'.format(path), transcript, None
        return build

    def worktree_gate(steps_argument=" -Steps 'Code-style catalog'", record_for=None, outside_workspace=False):
        """The gate.ps1 of a git worktree of workspace/App made outside the workspace, optionally with a passing
        record in that worktree's state directory whose repoPath names `record_for` ("self" or "sibling")."""
        def build():
            fixture = fresh()
            transcript = fixture.transcript([])
            declare(transcript, CLOSED)
            workspace = workspace_for(transcript)
            main = os.path.join(os.path.join(fixture.root, "other-workspace") if outside_workspace else workspace, "App")
            os.makedirs(main)
            for args in (["init", "-q"], ["config", "core.fsmonitor", "false"], ["config", "user.email", "f@x"],
                         ["config", "user.name", "f"]):
                subprocess.run(["git", "-C", main] + args, check=True)
            with open(os.path.join(main, "code.cs"), "w", encoding="utf-8") as handle:
                handle.write("class A {}\n")
            subprocess.run(["git", "-C", main, "add", "--all"], check=True)
            subprocess.run(["git", "-C", main, "commit", "-q", "-m", "fixture"], check=True)
            checkout = os.path.join(fixture.root, "worktrees", "app-branch")
            subprocess.run(["git", "-C", main, "worktree", "add", "-q", "-b", "branch", checkout], check=True)
            subprocess.run(["git", "-C", checkout, "config", "core.fsmonitor", "false"], check=True)
            path = os.path.join(checkout, "gate.ps1")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("Register-GateSteps @('Run tests')\nRegister-StepInputs @{\n    'Run tests' = @('*')\n}\nnpm test\n")
            if record_for:
                statuses = gate.gate_status_module()
                files = statuses.manifest(checkout)
                tools = statuses.tool_hashes()
                named = checkout if record_for == "self" else os.path.join(fixture.root, "worktrees", "other-branch")
                os.makedirs(named, exist_ok=True)
                record = {"repo": "App", "verdict": "PASSED", "startedAt": "2026-01-01T00:00:00Z",
                          "completedAt": "2026-01-01T00:05:00Z", "machine": "fixture", "steps": [],
                          "manifest": files, "tools": tools, "fingerprint": statuses.fingerprint(files, tools),
                          "repoPath": named}
                state = statuses.checkout_state_dir("App", checkout)
                os.makedirs(state, exist_ok=True)
                with open(statuses.record_path("App", state), "w", encoding="utf-8") as handle:
                    json.dump(record, handle)
            return 'pwsh -File "{0}"{1}'.format(path, steps_argument), transcript, None
        return build

    def peer_green(repo_name="Tools", verdict="PASSED", completed="2026-01-01T00:05:00Z", peer_state="dirty",
                   stale=False):
        """Two worktrees of workspace/<repo_name>, whose origin/main was committed in 2020: this session's, launching
        its gate, and a sibling holding a gate record (`verdict`, completed at `completed`) whose change is
        uncommitted ("dirty"), committed but not on origin ("unpushed") or already on origin ("clean"). The record
        carries the sibling's real manifest, so it reads GREEN-CURRENT, or the manifest from before its change when
        `stale`, so the sibling has edited past the pass."""
        def build():
            fixture = fresh()
            transcript = fixture.transcript([])
            declare(transcript, {repo_name: CLOSED["App"]})
            main = os.path.join(workspace_for(transcript), repo_name)
            os.makedirs(main)
            for args in (["init", "-q"], ["config", "core.fsmonitor", "false"], ["config", "user.email", "f@x"],
                         ["config", "user.name", "f"]):
                subprocess.run(["git", "-C", main] + args, check=True)
            for name, text in (("code.cs", "class A {}\n"), (".gitignore", ".claude/\n")):
                with open(os.path.join(main, name), "w", encoding="utf-8") as handle:
                    handle.write(text)
            subprocess.run(["git", "-C", main, "add", "--all"], check=True)
            subprocess.run(["git", "-C", main, "commit", "-q", "-m", "fixture", "--date", "2020-01-01T00:00:00"],
                           check=True, env=dict(os.environ, GIT_COMMITTER_DATE="2020-01-01T00:00:00"))
            subprocess.run(["git", "-C", main, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
            checkouts = {}
            for name in ("mine", "peer"):
                checkouts[name] = os.path.join(fixture.root, "worktrees", name)
                subprocess.run(["git", "-C", main, "worktree", "add", "-q", "-b", name, checkouts[name]], check=True)
                subprocess.run(["git", "-C", checkouts[name], "config", "core.fsmonitor", "false"], check=True)
            path = os.path.join(checkouts["mine"], "gate.ps1")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("Register-GateSteps @('Run tests')\nRegister-StepInputs @{\n    'Run tests' = @('*')\n}\nnpm test\n")
            peer = checkouts["peer"]
            statuses = gate.gate_status_module()
            before = statuses.manifest(peer)
            if peer_state != "clean":
                with open(os.path.join(peer, "change.cs"), "w", encoding="utf-8") as handle:
                    handle.write("class B {}\n")
            if peer_state == "unpushed":
                subprocess.run(["git", "-C", peer, "add", "--all"], check=True)
                subprocess.run(["git", "-C", peer, "commit", "-q", "-m", "peer change"], check=True)
            state = statuses.checkout_state_dir(repo_name, peer)
            os.makedirs(state, exist_ok=True)
            record = {"repo": repo_name, "verdict": verdict, "startedAt": completed, "completedAt": completed,
                      "machine": "fixture", "steps": [], "repoPath": peer,
                      statuses.FIELD_MANIFEST: before if stale else statuses.manifest(peer),
                      statuses.FIELD_TOOLS: statuses.tool_hashes()}
            with open(statuses.record_path(repo_name, state), "w", encoding="utf-8") as handle:
                json.dump(record, handle)
            return 'pwsh -File "{0}" -Steps \'Run tests\''.format(path), transcript, None
        return build

    def tests_only(tests_argument, test_file="WidgetTests.cs", edited=True,
                   body=("class WidgetTests { public void Widget_Renders() {} public void Widget_Hides() {} }",),
                   history=lambda fixture, selected, full: [], run_full=False):
        """A checked-in App/gate.ps1 run with -Tests, beside a test file the session edited (or not)."""
        def build():
            fixture = fresh()
            repo = os.path.join(fixture.root, "workspace", "App")
            os.makedirs(os.path.join(repo, "App.Tests"))
            path = os.path.join(repo, "gate.ps1")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("if ($Tests) { Invoke-SelectedTests $repo $TestProject $Tests {} }\n"
                             "dotnet test --project App.Tests\nWrite-Host 'GATE: PASSED'\n")
            test_path = os.path.join(repo, "App.Tests", test_file)
            with open(test_path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(body) + "\n")
            full = 'pwsh -File "{0}" -Steps \'Code-style catalog\''.format(path)
            selected = 'pwsh -File "{0}" -Tests {1} -TestProject App.Tests'.format(path, tests_argument)
            records = ([edit(400, test_path)] if edited else []) + history(fixture, selected, full)
            command = full if run_full else selected
            transcript = fixture.transcript(records)
            declare(transcript, CLOSED)
            return command, transcript, None
        return build

    def probe_run(probe_argument, edited_file="src/widget.py",
                  history=lambda fixture, probe, full: [], run_full=False, planned=False):
        """A checked-in App/gate.ps1 run with -Probe: its 'Widget harness' reads src/*, its 'Docs harness' docs/*,
        beside a file the session edited (or none, when edited_file is None). Planned, the gate takes its steps from
        Gates/step_inputs.py's plan: src/test_widget.py imports src/widget.py, docs/test_docs.py reads nothing else."""
        def build():
            fixture = fresh()
            repo = os.path.join(fixture.root, "workspace", "App")
            os.makedirs(os.path.join(repo, "src"))
            os.makedirs(os.path.join(repo, "docs"))
            path = os.path.join(repo, "gate.ps1")
            with open(path, "w", encoding="utf-8") as handle:
                if planned:
                    handle.write("$planText = & python (Join-Path $repo 'Gates\\step_inputs.py') $repo --plan\n"
                                 "Register-GateSteps @($harnesses.Keys)\nRegister-StepInputs $stepInputs\n"
                                 "Write-Host 'GATE: PASSED'\n")
                else:
                    handle.write("Register-GateSteps @('Widget harness', 'Docs harness')\nRegister-StepInputs @{\n"
                                 "    'Widget harness' = @('src/*')\n    'Docs harness' = @('docs/*')\n}\n"
                                 "python test_widget.py\nWrite-Host 'GATE: PASSED'\n")
            if planned:
                with open(os.path.join(repo, "src", "test_widget.py"), "w", encoding="utf-8") as handle:
                    handle.write("# GATE-STEP: Widget harness\nimport widget\n")
                with open(os.path.join(repo, "docs", "test_docs.py"), "w", encoding="utf-8") as handle:
                    handle.write("# GATE-STEP: Docs harness\nX = 1\n")
                subprocess.run(["git", "-C", repo, "-c", "core.fsmonitor=false", "init", "--quiet"], check=True,
                               capture_output=True)
                subprocess.run(["git", "-C", repo, "config", "core.fsmonitor", "false"], check=True, capture_output=True)
            records = []
            if edited_file:
                edited_path = os.path.join(repo, *edited_file.split("/"))
                with open(edited_path, "w", encoding="utf-8") as handle:
                    handle.write("WIDGET = 1\n")
                records.append(edit(400, edited_path))
            full = 'pwsh -File "{0}" -Steps \'Widget harness\''.format(path)
            probe = 'pwsh -File "{0}" -Probe {1}'.format(path, probe_argument)
            records += history(fixture, probe, full)
            transcript = fixture.transcript(records)
            declare(transcript, CLOSED)
            return (full if run_full else probe), transcript, None
        return build

    def harness_selected(selection, edited=True,
                         body=("CASES = [('deny', 'widget renders'), ('defer', 'widget hides')]",
                               "FIXTURE = \"GATE_DELTA = ['src/app.ts']\"")):
        """A harness run with --only, the harness edited by this session (or not), its body quoting a delta
        declaration as fixture data."""
        def build():
            fixture = fresh()
            path = fixture.write_file("test_widget_gate.py", list(body))
            records = [edit(100, path)] if edited else []
            return 'python "{0}" --only {1}'.format(path, selection), fixture.transcript(records), None
        return build

    def scratch_calling_selected_runner():
        fixture = fresh()
        path = fixture.write_file("scratch-tests.ps1", ["Invoke-SelectedTests $repo 'App.Tests' 'Widget_Renders' {}",
                                                        "dotnet test --project App.Tests"])
        return 'pwsh -File "{0}"'.format(path), fixture.transcript([]), None

    def subagent_edit():
        fixture = fresh()
        parent = fixture.transcript(fixture.foreground_gate(300, fixture.gate_command))
        fixture.transcript([edit(100, CODE_FILE)], subagent_of=parent)
        return fixture.gate_command, parent, None

    def peer(output_exists=True, age_seconds=None, finished=False, foreground=False, local_session=True):
        def build():
            fixture = fresh()
            if foreground:
                records = [tool_use(30, new_tool_id(), "PowerShell", {"command": "npm run e2e"})]
            else:
                records, task_id = fixture.background_launch(120, "npm run e2e", output_exists=output_exists)
                if finished:
                    records.append(notification(60, task_id))
            peer_path = fixture.transcript(records, age_seconds=age_seconds)
            if local_session:
                session = os.path.splitext(os.path.basename(peer_path))[0]
                os.makedirs(os.path.join(temp_root_for(peer_path), os.path.basename(fixture.slug), session))
            return fixture.gate_command, fixture.transcript([]), None
        return build

    def resumed(finished_in_new):
        """A session resumed into a new transcript: the old one still holds the gate launch, the new one
        copies it, and the completion notice, when the gate has exited, lands only in the new one."""
        def build():
            fixture = fresh()
            records, task_id = fixture.background_launch(120, "npm run e2e")
            old_path = fixture.transcript(records)
            session = os.path.splitext(os.path.basename(old_path))[0]
            os.makedirs(os.path.join(temp_root_for(old_path), os.path.basename(fixture.slug), session))
            copied = records + ([notification(60, task_id), edit(30, CODE_FILE)] if finished_in_new else [])
            return fixture.gate_command, fixture.transcript(copied), None
        return build

    def slot_held(locked, record, base=gate_script_first, planted=False):
        """`base`'s case with a gate slot of its own: its lock file held exclusively (locked) or present and
        free, and its holder record a real one naming this live process, a forged one naming the same live
        process, absent (None), or not JSON. A planted slot sits where the retired CRGOLDEN_GATE_SLOT_DIRECTORY
        names, outside the account's %TEMP%\\claude."""
        def build():
            command, transcript, tool_use_id = base()
            temp = os.path.join(os.path.dirname(transcript), "temp")
            directory = os.path.join(temp, "claude", "crgolden-gates")
            os.makedirs(directory, exist_ok=True)
            os.environ["CLAUDE_CODE_TMPDIR"] = temp
            if planted:
                directory = os.path.join(os.path.dirname(transcript), "planted-slot")
                os.makedirs(directory, exist_ok=True)
                os.environ["CRGOLDEN_GATE_SLOT_DIRECTORY"] = directory
            lock = os.path.join(directory, gate.GATE_SLOT_LOCK)
            with open(lock, "wb"):
                pass
            if locked:
                HELD_LOCKS.append(hold_exclusively(lock))
            if record is not None:
                body = "not json" if record == "unreadable" else json.dumps(
                    {"owner": "harness holder", "token": "harness", "pid": os.getpid(),
                     "acquiredAt": "2026-01-01T00:00:00.0000000Z"})
                with open(os.path.join(directory, gate.GATE_SLOT_RECORD), "w", encoding="utf-8") as handle:
                    handle.write(body)
            return command, transcript, tool_use_id
        return build

    return [
        # --- the machine's gate slot (Harness/Gates/GateSlot.ps1) ----------------------------------------
        ("deny", "gate slot lock held, with its holder record", slot_held(True, "real")),
        ("deny", "gate slot lock held, no holder record", slot_held(True, None)),
        ("deny", "gate slot lock held, holder record not JSON", slot_held(True, "unreadable")),
        ("defer", "gate slot record forged beside a free lock", slot_held(False, "forged")),
        ("defer", "gate slot lock free and no record", slot_held(False, None)),
        ("defer", "a held slot where CRGOLDEN_GATE_SLOT_DIRECTORY points is not this account's slot",
         slot_held(True, "real", planted=True)),
        ("deny", "tests-only run while the gate slot lock is held",
         slot_held(True, "real", base=tests_only("Widget_Renders"))),

        # --- not a gate: always defer -------------------------------------------------------------
        ("defer", "git status", lambda: plain("git status --porcelain")),
        ("defer", "dotnet --version", lambda: plain("dotnet --version")),
        ("defer", "npm ci is setup", lambda: plain("npm ci")),
        ("defer", "dotnet restore is setup", lambda: plain("dotnet restore Identity.slnx")),
        ("defer", "dotnet build-server shutdown is maintenance", lambda: plain("dotnet build-server shutdown")),
        ("defer", "synthetic walker is not a gate", lambda: plain("npm run e2e:synthetic")),
        ("defer", "playwright browser install is setup", lambda: plain("npm run playwright:install")),
        ("defer", "pip install pytest-cov is setup", lambda: plain('python -m pip install -e ".[dev]" pytest-cov')),
        ("defer", "pip install naming pytest itself is setup", lambda: plain("python -m pip install pytest openpyxl pyyaml")),
        ("defer", "bare pip install pytest is setup", lambda: plain("pip install pytest")),
        ("deny", "control: a pytest run chained after a pip install is still a step",
         lambda: plain("python -m pip install pytest; python -m pytest tests -q")),
        ("deny", "control: a pytest run piped after a pip install is still a step",
         lambda: plain("python -m pip install pytest | python -m pytest tests")),
        ("defer", "non-gate script body", probe_script),
        ("defer", "jb cleanupcode is an edit, not a gate step", lambda: plain('jb cleanupcode "C:\\repo\\App.slnx" --no-build')),
        ("defer", "ruff format rewrites files, so it is an edit",
         lambda: plain('python -m ruff format src tests app.py')),
        ("defer", "ruff check --fix limited to import sorting and unused imports is an edit",
         lambda: plain('python -m ruff check --fix --select I,F401 "C:\\repo\\src" "C:\\repo\\tests"')),
        ("defer", "ruff check --fix --select=I is an edit", lambda: plain("ruff check --fix --select=I src")),
        ("defer", "jb --version", lambda: plain("jb --version")),
        ("defer", "empty command", lambda: plain("")),

        # --- inline gate steps: deny --------------------------------------------------------------
        ("defer", "a plain dotnet build of a path is the proof a gate needs, not a gate step",
         lambda: plain('dotnet build "C:\\repo\\Identity.slnx" --configuration Release')),
        ("defer", "a plain npm run build with --prefix is the proof a gate needs",
         lambda: plain('npm run build --prefix "C:\\repo\\Churches"')),
        ("deny", "must-block: a dotnet build naming a target can run tests, so it is a gate step",
         lambda: plain('dotnet build "C:\\repo\\Identity.slnx" -t:VSTest')),
        ("deny", "must-block: a dotnet build chained to a test run is a gate step",
         lambda: plain('dotnet build "C:\\repo\\Identity.slnx"; dotnet test --project Identity.Tests.Unit')),
        ("deny", "must-block: a dotnet build naming no path is a gate step", lambda: plain("dotnet build")),
        ("deny", "inline dotnet test", lambda: plain("dotnet test --project Identity.Tests.Unit")),
        ("deny", "inline npm run lint", lambda: plain("npm run lint")),
        ("deny", "inline npm run typecheck:e2e", lambda: plain("npm run typecheck:e2e")),
        ("deny", "inline npm test", lambda: plain("npm test")),
        ("deny", "inline npx vitest", lambda: plain("npx vitest run src/app.spec.ts")),
        ("deny", "inline npx playwright test", lambda: plain("npx playwright test --grep login")),
        ("deny", "inline python -m pytest", lambda: plain("python -m pytest tests -q")),
        ("deny", "inline ruff check", lambda: plain("ruff check src")),
        ("deny", "inline ruff format --check", lambda: plain("python -m ruff format --check src tests")),
        ("deny", "inline ruff format --diff", lambda: plain("ruff format --diff src")),
        ("deny", "ruff check --fix with no selection still lints everything", lambda: plain("ruff check --fix src")),
        ("deny", "ruff check --fix selecting a rule it only reports",
         lambda: plain("ruff check --fix --select I,E501 src")),
        ("deny", "ruff check --fix widened by --extend-select",
         lambda: plain("ruff check --fix --select I --extend-select E src")),
        ("deny", "ruff check --fix with a second --select", lambda: plain("ruff check --fix --select I --select E src")),
        ("deny", "inline mypy", lambda: plain("mypy src tests")),
        ("deny", "inline test exe", lambda: plain('Identity.Tests.Unit.exe -trait "Category=Unit"')),
        ("deny", "inline jb inspectcode",
         lambda: plain('jb inspectcode "C:\\repo\\App.slnx" --no-build -e=WARNING -o="C:\\out\\App.sarif"')),
        ("deny", "inline sonar-scanner", lambda: plain('sonar-scanner -D"sonar.projectKey=crgolden_Curator"')),
        ("deny", "inline sonar-scanner.bat", lambda: plain('sonar-scanner.bat -D"sonar.projectKey=crgolden_Curator"')),
        ("deny", "sonar-scanner by its full path",
         lambda: plain('& "C:\\sonar-scanner-9.9.9.9-windows-x64\\bin\\sonar-scanner.bat" -D"sonar.projectKey=x"')),
        ("defer", "a path through the sonar-scanner install is not the scanner",
         lambda: plain('& "C:\\scratch\\sonar-scanner-9.9.9.9-windows-x64\\jre\\bin\\java.exe" -version')),

        # --- a gate narrowed to a test name or file: deny ------------------------------------------
        ("deny", "script narrowed to one spec file", narrowed_script(["npx vitest run src/app/app.component.spec.ts"])),
        ("deny", "script narrowed by --grep", narrowed_script(["npm run e2e:run -- --grep palette"])),
        ("defer", "same body, marked as data",
         narrowed_script(["# GATE-CADENCE: gate-step-strings-are-data", "npm run e2e:run -- --grep palette"])),
        ("deny", "script narrowed by --filter-method",
         narrowed_script(['dotnet test --project App.Tests -- --filter-method "*Queue*"'])),
        ("deny", "script narrowed to one pytest file", narrowed_script(["python -m pytest tests/test_catalog_routes.py -q"])),
        ("defer", "full script with CI's --filter-trait tier filter",
         narrowed_script(['dotnet test --project App.Tests -- --filter-trait "Category=Unit"'])),
        ("deny", "script narrowed by inspectcode --include",
         narrowed_script(['jb inspectcode "C:\\repo\\App.slnx" --no-build --include="src/**/*.cs"'])),
        ("deny", "script narrowed by inspectcode --project",
         narrowed_script(['jb inspectcode "C:\\repo\\App.slnx" --no-build --project=App.Api'])),
        ("defer", "full script inspecting the whole solution",
         narrowed_script(['dotnet build "C:\\repo\\App.slnx"',
                          'jb inspectcode "C:\\repo\\App.slnx" --no-build -e=WARNING -o="C:\\out\\App.sarif"'])),
        ("deny", "harness narrowed by --only", lambda: plain('python "C:\\absent\\test_widget.py" --only git')),
        ("defer", "harness body quoting --grep as fixture data", harness_quoting_narrowing),

        # --- a narrowed gate whose code declares the delta it is narrowed to ------------------------
        ("deny", "delta names a file the session never edited",
         narrowed_delta(["GATE_DELTA = ['src/app.ts']", "npx vitest run src/app.spec.ts"])),
        ("defer", "delta edited after the last gate",
         narrowed_delta(["GATE_DELTA = ['src/app.ts']", "npx vitest run src/app.spec.ts"], after_gate(edit(100, CODE_FILE)))),
        ("deny", "delta edited only BEFORE the last gate",
         narrowed_delta(["GATE_DELTA = ['src/app.ts']", "npx vitest run src/app.spec.ts"], before_gate(edit(400, CODE_FILE)))),
        ("defer", "delta edited with no gate before it",
         narrowed_delta(["GATE_DELTA = ['src/app.ts']", "npx vitest run src/app.spec.ts"], lambda _: [edit(100, CODE_FILE)])),
        ("defer", "delta declared in the PowerShell form",
         narrowed_delta(["$GateDelta = @('src/app.ts')", "npm run e2e:run -- --grep palette"], lambda _: [edit(100, CODE_FILE)])),
        ("deny", "delta declared only in a comment",
         narrowed_delta(["# GATE_DELTA = ['src/app.ts']", "npx vitest run src/app.spec.ts"], lambda _: [edit(100, CODE_FILE)])),
        ("deny", "an empty delta",
         narrowed_delta(["GATE_DELTA = []", "npx vitest run src/app.spec.ts"], lambda _: [edit(100, CODE_FILE)])),
        ("deny", "a delta entry matching an edited path off a directory boundary",
         narrowed_delta(["GATE_DELTA = ['pp.ts']", "npx vitest run src/app.spec.ts"], lambda _: [edit(100, CODE_FILE)])),
        ("deny", "a plant: entry the script never writes",
         narrowed_delta(["GATE_DELTA = ['plant:src/app/app.config.ts']", "npx playwright test e2e/scroll.spec.ts"])),
        ("defer", "a plant: entry the script writes and restores",
         narrowed_delta(["APP_CONFIG = REPO / 'src' / 'app' / 'app.config.ts'",
                         "APP_CONFIG.write_text(original.replace(PLANT, ''))",
                         "GATE_DELTA = ['plant:src/app/app.config.ts']",
                         "npx playwright test e2e/scroll.spec.ts"])),
        ("ask", "a plant-only delta with nothing edited since the last gate asks like any re-run",
         narrowed_delta(["APP_CONFIG = REPO / 'src' / 'app' / 'app.config.ts'",
                         "APP_CONFIG.write_text(original.replace(PLANT, ''))",
                         "GATE_DELTA = ['plant:src/app/app.config.ts']",
                         "npx playwright test e2e/scroll.spec.ts"], after_gate())),

        # --- a gate script with no history: defer -------------------------------------------------
        ("defer", "first gate script", gate_script_first),
        ("defer", "harness name, file absent", lambda: plain('python "C:\\absent\\test_widget.py"')),
        ("defer", "reading a harness file is not running it", lambda: plain('Get-Content "C:\\repo\\test_widget.py"')),
        ("defer", "git diff naming a catalog script reads it",
         lambda: plain('git -C "C:\\repo\\Tools" diff -- Claude/Get-CodeStyleCatalog.ps1')),
        ("defer", "git show naming a catalog script reads it",
         lambda: plain('git show HEAD:Claude/Get-CommentCatalog.ps1')),
        ("defer", "Get-Content naming a catalog script reads it",
         lambda: plain('Get-Content "C:\\repo\\Tools\\Claude\\Get-CodeStyleCatalog.ps1"')),
        ("deny", "running a catalog script by pwsh -File is a gate step",
         lambda: plain('pwsh -File "C:\\absent\\Get-CodeStyleCatalog.ps1"')),
        ("deny", "running a catalog script that exists on disk is a gate step, whatever its body names",
         existing_catalog("Get-CodeStyleCatalog.ps1")),
        ("deny", "running the comment catalog that exists on disk is a gate step",
         existing_catalog("Get-CommentCatalog.ps1")),
        ("defer", "a script whose body and name name no gate step stays ordinary", probe_script),
        ("deny", "running a catalog script by the call operator is a gate step",
         lambda: plain('& "C:\\absent\\Get-CommentCatalog.ps1" -Repo Identity')),
        ("deny", "a reader piped into an interpreter invokes what it reads",
         lambda: plain('Get-Content "C:\\absent\\Get-CodeStyleCatalog.ps1" | Invoke-Expression')),
        ("deny", "a reader chained into a gate step",
         lambda: plain('git -C "C:\\repo" status; npm run lint')),
        ("deny", "a session's own script that runs a harness is a wrapper, first run", harness_runner(False)),
        ("deny", "a session's own script that runs a harness is a wrapper, re-run unchanged", harness_runner(True)),

        # --- own in-flight gate: deny -------------------------------------------------------------
        ("deny", "own background gate running", own_background_running),
        ("deny", "own background gate running, its launch copied into an api-request-blob",
         own_background_running_with_request_copy),
        ("defer", "the gate-record reader while that gate runs", gate_record_read_while_own_gate_runs),
        ("defer", "own background gate finished, then edit", own_background_finished_then_edit),
        ("ask", "own background gate finished, no edit", own_background_finished_no_edit),
        ("defer", "own background gate with no local output file", own_background_output_absent),
        ("deny", "parallel foreground gate pending", parallel_pending(30)),
        ("defer", "pending foreground gate past the timeout", parallel_pending(1200)),
        ("defer", "the call being judged, matched by id", current_call(True)),
        ("defer", "the call being judged, matched without an id", current_call(False)),

        # --- relaunch after stopping this session's own gate: ask ----------------------------------
        ("ask", "relaunch after stopping the previous gate", relaunch_after_stop(False)),
        ("defer", "a stopped gate followed by a finished one, then an edit", relaunch_after_stop(True)),

        # --- nothing changed since the last gate: ask ---------------------------------------------
        ("ask", "re-gate with no edit since", regate([], [])),
        ("defer", "re-gate after git committed to the gated checkout since (a pull, merge or rebase)",
         git_changed_regate(100)),
        ("ask", "control: re-gate when the checkout's newest commit predates the last gate", git_changed_regate(600)),
        ("ask", "re-gate after markdown-only edits", regate([], [edit(100, "C:\\repo\\README.md")])),
        ("defer", "re-gate after a code edit", regate([], [edit(100, CODE_FILE)])),
        ("ask", "an edit BEFORE the last gate does not count", regate([edit(400, CODE_FILE)], [])),
        ("defer", "a subagent's edit counts for the session", subagent_edit),
        ("defer", "re-gate after jb cleanupcode, which is an edit",
         regate([], [shell(100, 'jb cleanupcode "C:\\repo\\App.slnx" --no-build')])),
        ("ask", "re-gate after a jb call that edits nothing", regate([], [shell(100, "jb --version")])),
        ("defer", "re-gate after ruff format, which is an edit",
         regate([], [shell(100, "python -m ruff format src tests")])),
        ("ask", "re-gate after a ruff call that edits nothing", regate([], [shell(100, "python -m ruff --version")])),

        # --- the operator's pinned GOAL: the gate script must restate it ---------------------------
        ("deny", "goal pinned, gate script does not restate it", goal_gate(["GOAL: ship the widget export"], [])),
        # The operator: "we need to disallow running scripts that exists just to wrap and run a gate". A GOAL:
        # comment is how a wrapper satisfied this check, so only the checked-in gate's -Goal argument does now.
        ("deny", "goal pinned, a GOAL: comment in a gate script satisfies nothing",
         goal_gate(["GOAL: ship the widget export"], ["# GOAL: Ship the widget-export."])),
        ("deny", "goal pinned, a harness stamped with a GOAL: comment satisfies nothing", goal_pinned_harness),
        ("defer", "a -Goal in double quotes matches a pinned goal holding backticks and an apostrophe",
         goal_argument_double(["GOAL: audit `crgolden-36`'s session. assume laziness"],
                              "audit crgolden-36's session. assume laziness")),
        ("deny", "control: the same double-quoted -Goal with one word changed",
         goal_argument_double(["GOAL: audit `crgolden-36`'s session. assume laziness"],
                              "audit crgolden-36's session. assume diligence")),
        ("defer", "a single-quoted -Goal reads a doubled apostrophe as one",
         goal_argument_gate(["GOAL: audit crgolden-36's session"], "audit crgolden-36''s session")),
        ("defer", "no goal pinned, a restating gate script defers", goal_gate([], ["# GOAL: Ship the widget-export."])),
        ("deny", "gate script restates only part of the goal",
         goal_gate(["GOAL: ship the widget export and its docs"], ["# GOAL: ship the widget export"])),
        ("defer", "goal retired by GOAL: none", goal_gate(["GOAL: ship the widget export", "GOAL: none"], [])),
        ("defer", "a checked-in gate restating the goal through its -Goal argument runs without asking",
         goal_argument_gate(["GOAL: ship the widget export"], "Ship the widget-export.")),
        ("deny", "a -Goal argument carrying only part of the goal",
         goal_argument_gate(["GOAL: ship the widget export and its docs"], "ship the widget export")),
        ("defer", "the master's standing goal carried as -Goal runs its gate",
         goal_argument_gate(["GOAL: read and follow Tools/MasterSession/RUNBOOK.md from origin/main, every cycle"],
                            "read and follow Tools/MasterSession/RUNBOOK.md from origin/main, every cycle", quote='"')),
        ("deny", "control: the master's gate without -Goal is refused",
         goal_gate(["GOAL: read and follow Tools/MasterSession/RUNBOOK.md from origin/main, every cycle"], [])),
        ("defer", "a -Goal argument single-quoted with doubled apostrophes carries a goal holding both quote kinds",
         goal_argument_gate(["GOAL: pair with \"crgolden-2d\" and I'd review it"],
                            "pair with \"crgolden-2d\" and I'd review it")),
        ("defer", "the same goal double-quoted with doubled double quotes",
         goal_argument_gate(["GOAL: pair with \"crgolden-2d\" and I'd review it"],
                            "pair with \"crgolden-2d\" and I'd review it", quote='"')),
        ("defer", "a -Goal leaving out the goal's shell operators, which the command line may not carry",
         goal_argument_gate(["GOAL: fix the build; then gate | report it & stop"], "fix the build then gate report it stop")),
        ("deny", "control: the same operator-free -Goal with one word changed",
         goal_argument_gate(["GOAL: fix the build; then gate | report it & stop"], "fix the build then gate report it go")),
        ("defer", "a goal holding backticks and an apostrophe, the backticks left out, double-quoted",
         goal_argument_gate(["GOAL: review `crgolden-37`'s fix a | b & c"], "review crgolden-37's fix a b c",
                            quote='"')),
        ("deny", "an escaped -Goal argument with one word changed",
         goal_argument_gate(["GOAL: pair with \"crgolden-2d\" and I'd review it"],
                            "pair with \"crgolden-2d\" and I'd skip it")),
        ("deny", "an escaped -Goal argument carrying only part of the goal",
         goal_argument_gate(["GOAL: pair with \"crgolden-2d\" and I'd review it"], "pair with \"crgolden-2d\"",
                            quote='"')),
        ("deny", "the newest pin wins",
         goal_gate(["GOAL: ship the widget export", "GOAL: rename the importer"], ["# GOAL: ship the widget export"])),
        ("defer", "a GOAL: inside a system reminder pins nothing",
         goal_gate(["<system-reminder>\nGOAL: ship the widget export\n</system-reminder>"], [])),
        ("defer", "a GOAL: mid-sentence pins nothing: only a line opening GOAL: does",
         goal_gate(["we need to link it. your GOAL: ship the widget export"], [])),
        ("defer", "a lowercase goal: opening a line pins nothing", goal_gate(["goal: ship the widget export"], [])),
        ("deny", "an active /goal is the goal",
         goal_records_gate([goal_status(900, "ship the widget export")])),
        ("defer", "a -Goal restating the active /goal",
         goal_command_gate([goal_status(900, "ship the widget export")], "ship the widget export")),
        ("deny", "an active /goal outranks a later GOAL: line",
         goal_command_gate([goal_status(900, "ship the widget export"), operator(800, "GOAL: rename the importer")],
                           "rename the importer")),
        ("defer", "a /goal that is met hands back to the GOAL: line",
         goal_command_gate([operator(950, "GOAL: rename the importer"), goal_status(900, "ship the widget export"),
                            goal_status(800, "ship the widget export", met=True)], "rename the importer")),
        ("deny", "control: the GOAL: line is not the goal while that /goal is still active",
         goal_command_gate([operator(950, "GOAL: rename the importer"), goal_status(900, "ship the widget export")],
                           "rename the importer")),
        ("deny", "a goal_status record of a shape the reader does not know leaves the /goal in force",
         goal_command_gate([goal_status(900, "ship the widget export"),
                            {"type": "attachment", "timestamp": iso(800), "attachment": {"type": "goal_status"}}],
                           "rename the importer")),
        ("defer", "a GOAL: inside a quoted message pins nothing",
         goal_gate(['a peer wrote: "two scripts ran.\n\nNeither carried the GOAL: line" - look into it'], [])),
        ("defer", "a lowercase goal: mid-sentence pins nothing", goal_gate(["the goal: ship the widget export"], [])),
        ("defer", "a GOAL: in a compaction summary pins nothing", summary_pin),
        ("deny", "a GOAL: the operator sent mid-turn (a queued_command attachment) pins the goal",
         goal_records_gate([{"type": "attachment", "timestamp": iso(900), "attachment": {
             "type": "queued_command", "origin": {"kind": "human"}, "prompt": "GOAL: ship the widget export"}}])),
        ("defer", "a GOAL: in a skill injected into the transcript pins nothing",
         goal_records_gate([dict(operator(900, "the operator's pinned GOAL: ship the widget export"), isMeta=True)])),
        ("defer", "a GOAL: in a user record with no origin pins nothing",
         goal_records_gate([{"type": "user", "timestamp": iso(900),
                             "message": {"role": "user", "content": "GOAL: ship the widget export"}}])),
        ("defer", "a GOAL: in a peer session's message pins nothing",
         goal_gate(['<cross-session-message from="uds:peer">\nwhile one is pinned, a gate script must restate it on a '
                    'GOAL: line. I will say when it finishes.\n</cross-session-message>'], [])),

        # --- a session's own script that runs a gate for it, or exists to wait ----------------------
        # The operator: "we need to disallow an agent from running a script that exists just to block
        # themselves" and "disallow running scripts that exists just to wrap and run a gate".
        ("deny", "a wrapper calling the checked-in gate.ps1, its output sent to a file",
         own_script("run-stop-harness.ps1", ["# GOAL: ship the widget export",
                                             "& \"$env:USERPROFILE\\source\\repos\\crgolden\\Tools\\gate.ps1\" "
                                             "-Steps 'Stop hook harness' *> 'C:\\scratch\\gate_run.txt'",
                                             "exit $LASTEXITCODE"], pins=["GOAL: ship the widget export"])),
        ("deny", "a wrapper calling gate.ps1 is refused with no goal pinned too",
         own_script("tools-full-gate.ps1", ["& \"C:\\w\\Tools\\gate.ps1\" -Steps 'Stop hook harness'"])),
        ("deny", "a wrapper calling gate.ps1 through a variable",
         own_script("indirect.ps1", ["$gate = 'C:\\w\\Tools\\gate.ps1'", "& $gate -Steps 'Stop hook harness'"])),
        ("deny", "a wrapper invoking a harness with pwsh",
         own_script("triage-control.ps1", ["& pwsh -File (Join-Path $tools 'AlertTriage\\Test-AlertTriage.ps1') *>&1"])),
        ("deny", "a Python wrapper launching harnesses from a list of argv",
         own_script("run_merge_harnesses.py", [
             "import os, subprocess, sys", "runs = [",
             "    ('a', [sys.executable, os.path.join('C:\\\\t', 'Claude', 'test_stop_gate.py')]),", "]",
             "for name, command in runs:", "    subprocess.run(command)"], runner="python")),
        ("deny", "a Python wrapper run as python -u",
         own_script("run_misfire.py", ["import subprocess, sys",
                                       "subprocess.run([sys.executable, 'C:\\\\t\\\\Claude\\\\test_find_misfires.py'])"],
                    runner="python -u")),
        ("deny", "the data marker never exempts a script that invokes a gate",
         own_script("marked.ps1", ["# GATE-CADENCE: gate-step-strings-are-data", "& 'C:\\w\\Tools\\gate.ps1' -Steps x"])),
        ("deny", "a script polling a gate's output for its GATE: verdict",
         own_script("wait-gate.ps1", ["while ($true) {",
                                      "    if (Select-String -LiteralPath $f -Pattern '^GATE: (PASSED|FAILED)') { break }",
                                      "    Start-Sleep -Seconds 15", "}"])),
        ("deny", "a script blocking on another process",
         own_script("wait-peer-gate.ps1", ["Wait-Process -Id 9464 -Timeout 580"])),
        ("deny", "a Python script blocking on a peer's gate pid through WaitForSingleObject",
         own_script("wait_gate_pid.py", [
             "import ctypes", "import sys", "", "SYNCHRONIZE = 0x00100000", "WAIT_OBJECT_0 = 0",
             "pid = int(sys.argv[1])", "milliseconds = int(sys.argv[2])",
             "kernel32 = ctypes.WinDLL(\"kernel32\", use_last_error=True)",
             "kernel32.OpenProcess.restype = ctypes.c_void_p",
             "handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)", "if not handle:", "    sys.exit(0)",
             "result = kernel32.WaitForSingleObject(ctypes.c_void_p(handle), milliseconds)",
             "kernel32.CloseHandle(ctypes.c_void_p(handle))"], runner="python")),
        ("deny", "a script blocking on several processes through WaitForMultipleObjects",
         own_script("wait_many.py", ["import ctypes", "ctypes.windll.kernel32.WaitForMultipleObjects(2, handles, True, 600000)"],
                    runner="python")),
        ("deny", "a Python script blocking on a pid with os.waitpid",
         own_script("wait_pid.py", ["import os", "os.waitpid(13424, 0)"], runner="python")),
        ("deny", "a PowerShell script blocking on a process found by id with WaitForExit",
         own_script("wait-exit.ps1", ["[void][Diagnostics.Process]::GetProcessById(13424).WaitForExit(580000)"])),
        ("deny", "a process looked up on one line and waited on with WaitForExit on the next",
         own_script("wait-exit-two.ps1", ["$gate = [Diagnostics.Process]::GetProcessById(13424)", "$gate.WaitForExit(580000)"])),
        ("deny", "a looked-up process waited on with WaitForExitAsync",
         own_script("wait-exit-async.ps1", ["$gate = Get-Process -Id 13424", "$gate.WaitForExitAsync().Wait()"])),
        ("defer", "control: WaitForExit on a process the script started itself is no wait script",
         own_script("own-exit.ps1", ["$child = Start-Process git -ArgumentList status -PassThru", "$child.WaitForExit()"])),
        ("defer", "control: a looked-up process beside a started child's wait keys on the waited name, never any lookup",
         own_script("lookup-and-child.ps1", ["$p = Get-Process -Id 1", "$child = Start-Process git -ArgumentList status -PassThru",
                                             "$child.WaitForExit()"])),
        ("deny", "a PowerShell script blocking on Get-Process -Id with WaitForExit",
         own_script("wait-exit2.ps1", ["(Get-Process -Id 13424).WaitForExit()"])),
        ("defer", "control: a script that waits on a child it started itself is no wait script",
         own_script("own_child.py", ["import subprocess", "child = subprocess.Popen(['git', 'status'])", "child.wait()"],
                    runner="python")),
        ("defer", "control: a script that only reads a process's state is no wait script",
         own_script("alive.py", ["import ctypes", "handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, 13424)",
                                 "print(bool(handle))"], runner="python")),
        ("deny", "a Python loop sleeping on gate_status",
         own_script("await_tools_record.py", ["import subprocess, time", "while True:",
                                              "    out = open('gate_status.txt').read()", "    time.sleep(30)"],
                    runner="python")),
        ("defer", "control: a script reading gate.ps1 as data runs no gate",
         own_script("snap.ps1", ["# GATE-CADENCE: gate-step-strings-are-data",
                                 "git -C $t show \"HEAD:gate.ps1\" | Set-Content $o"])),
        ("defer", "control: a script building a gate command string as hook input runs no gate",
         own_script("bench.py", ["import subprocess",
                                 "payload = {'command': 'pwsh -File \"C:\\\\nope\\\\gate.ps1\" -Steps build'}",
                                 "subprocess.run(['git', 'status'])"], runner="python")),
        ("defer", "control: a git diff over harness file names runs no harness",
         own_script("snapshot.py", ["import subprocess", "TRACKED = ['Hooks/test_stop_gate.py', 'gate.ps1']",
                                    "subprocess.run(['git', '-C', 'C:\\\\t', 'diff', 'HEAD', '--'] + TRACKED)"],
                    runner="python")),
        ("defer", "control: compiling a harness runs nothing",
         own_script("compile_check.py", ["import subprocess, sys",
                                         "subprocess.run([sys.executable, '-m', 'py_compile', 'test_stop_gate.py'])"],
                    runner="python")),
        ("deny", "a loop that sleeps is a wait script, whatever it watches",
         own_script("watch-50212.py", ["import subprocess, time", "deadline = time.time() + 25 * 60",
                                       "while time.time() < deadline:",
                                       "    subprocess.run(['pwsh', '-NoProfile', '-Command', 'Get-NetTCPConnection'])",
                                       "    time.sleep(0.5)"], runner="python")),
        ("deny", "a PowerShell loop sleeping until a service answers is a wait script",
         own_script("cutover.ps1", ["while (-not (Test-NetConnection localhost -Port 9200).TcpTestSucceeded) {",
                                    "    Start-Sleep -Seconds 5", "}"])),
        ("defer", "control: one bounded pause outside any loop is no wait script",
         own_script("settle.ps1", ["Restart-Service Redis", "Start-Sleep -Seconds 2", "Get-Service Redis"])),
        ("defer", "control: a sub-second Python pause between two commands is no wait script",
         own_script("settle.py", ["import subprocess, time", "subprocess.run(['git', 'status'])", "time.sleep(0.5)",
                                  "subprocess.run(['git', 'log', '-1'])"], runner="python")),
        ("deny", "a script whose only work is one sleep is a wait script",
         own_script("wait_seconds.py", ["import sys", "import time", "time.sleep(int(sys.argv[1]))"],
                    runner="python")),
        ("deny", "a pause whose length is read at run time is a wait script, beside real work",
         own_script("pause.ps1", ["Restart-Service Redis", "Start-Sleep -Seconds $args[0]", "Get-Service Redis"])),
        ("deny", "a literal pause past the settle bound is a wait script, beside real work",
         own_script("long-settle.ps1", ["Restart-Service Redis", "Start-Sleep -Seconds 240", "Get-Service Redis"])),
        ("deny", "a shell sleep past the settle bound is a wait script",
         own_script("nap.ps1", ["Get-Service Redis", "sleep 20"])),
        ("deny", "a sleep imported from time is a wait script",
         own_script("doze.py", ["from time import sleep", "sleep(300)"], runner="python")),
        ("deny", "a timeout /t pause is a wait script",
         own_script("hold.ps1", ["timeout /t 120"])),
        ("deny", "a ping used as a timer is a wait script",
         own_script("ping-wait.ps1", ["Get-Service Redis", "ping -n 60 127.0.0.1"])),
        ("deny", "two short pauses are a wait script",
         own_script("twice.ps1", ["Restart-Service Redis", "Start-Sleep -Seconds 3", "Get-Service Redis",
                                  "Start-Sleep -Seconds 3"])),
        # The operator, on a session re-running gate_status.py between turns while its own gate ran: "STOP SPAMMING a
        # status script ... you can easily use \"WAITING\"".
        ("deny", "a status poll run again with nothing in between is a poll loop",
         polled('python "C:\\repo\\Tools\\Gates\\gate_status.py" Tools', [])),
        ("deny", "a status poll run again after only another status poll is still a poll loop",
         polled('python "C:\\repo\\Tools\\Gates\\gate_status.py" Tools',
                [tool_use(200, "toolu_hookstatus", "PowerShell",
                          {"command": 'python "C:\\repo\\Tools\\Claude\\skills\\hook-status\\scripts\\hook_status.py"'}),
                 tool_result(199, "toolu_hookstatus", "ok", {"stdout": "ok"})])),
        ("deny", "the same poll respelled with extra spaces is the same poll",
         polled('python  "C:\\repo\\Tools\\Gates\\gate_status.py"   Tools', [],
                earlier='python "C:\\repo\\Tools\\Gates\\gate_status.py" Tools')),
        ("deny", "harness attachments between two polls (a token reminder, a hook's success) carry no input",
         polled('python "C:\\repo\\Tools\\Gates\\gate_status.py" Tools',
                [{"type": "attachment", "timestamp": iso(150), "attachment": {"type": "total_tokens_reminder"}},
                 {"type": "attachment", "timestamp": iso(140), "attachment": {"type": "hook_success"}},
                 {"type": "attachment", "timestamp": iso(130), "attachment": {"type": "silent_turn_reminder"}}])),
        ("deny", "Stop hook feedback between two polls is no input: poll, a refused stop, poll is the loop",
         polled('python "C:\\repo\\Tools\\Gates\\gate_status.py" Tools',
                [{"type": "user", "timestamp": iso(150), "isMeta": True, "message": {"role": "user", "content":
                  "Stop hook feedback:\nSTOP GATE: nothing in your last message earns a stop."}}])),
        ("defer", "control: a peer's message, an isMeta record that carries an origin, reads something new",
         polled('python "C:\\repo\\Tools\\Gates\\gate_status.py" Tools',
                [{"type": "user", "timestamp": iso(150), "isMeta": True, "origin": {"kind": "peer", "name": "crgolden-cf"},
                  "message": {"role": "user", "content": "<cross-session-message>FINISHED</cross-session-message>"}}])),
        ("defer", "control: a mid-turn operator message, a queued_command attachment, reads something new",
         polled('python "C:\\repo\\Tools\\Gates\\gate_status.py" Tools',
                [{"type": "attachment", "timestamp": iso(150),
                  "attachment": {"type": "queued_command", "prompt": "the cutover looks hung"}}])),
        ("defer", "control: a poll after a task notification reads something new",
         polled('python "C:\\repo\\Tools\\Gates\\gate_status.py" Tools', [notification(100, "b12345678")])),
        ("defer", "control: a poll after an operator message reads something new",
         polled('python "C:\\repo\\Tools\\Gates\\gate_status.py" Tools', [operator(100, "what is the gate doing?")])),
        ("defer", "control: a poll after an edit reads something new",
         polled('python "C:\\repo\\Tools\\Gates\\gate_status.py" Tools',
                [tool_use(200, "toolu_edit", "Edit", {"file_path": "C:\\repo\\Tools\\gate.ps1"}),
                 tool_result(199, "toolu_edit", "ok", {})])),
        ("defer", "control: a poll after another command reads something new",
         polled('python "C:\\repo\\Tools\\Gates\\gate_status.py" Tools',
                [tool_use(200, "toolu_git", "PowerShell", {"command": "git -C C:\\repo\\Tools fetch origin"}),
                 tool_result(199, "toolu_git", "", {"stdout": ""})])),
        ("defer", "control: a different poll than the one already run is no repeat",
         polled('python "C:\\repo\\Tools\\Gates\\gate_status.py" Identity', [],
                earlier='python "C:\\repo\\Tools\\Gates\\gate_status.py" Tools')),
        ("defer", "control: an ordinary command run twice is no status poll",
         polled("git -C C:\\repo\\Tools status --short", [])),
        ("defer", "control: a command that only names a status script as an argument runs no poll",
         polled('python -m py_compile "C:\\repo\\Tools\\Claude\\skills\\hook-status\\scripts\\hook_status.py"', [])),
        ("deny", "a GateSlot status read run again with nothing in between is a poll loop",
         polled('pwsh -File "C:\\repo\\Tools\\Gates\\GateSlot.ps1" -Status', [])),
        # The operator, on two sessions' scripts that blocked in gh run watch over pushed CI runs: "that is a gap in
        # the hook if you were able to write and run a script that violates my rule against writing scripts that
        # sit and wait".
        ("deny", "a script looping gh run watch over CI runs is a wait script",
         own_script("Watch-PushRuns.ps1", ["$runs = [ordered]@{ Identity = '39796450150'; Inventory = '39796497086' }",
                                           "foreach ($repo in $runs.Keys) {",
                                           "    $null = gh run watch $runs[$repo] --repo \"crgolden/$repo\" "
                                           "--interval 60 2>&1",
                                           "    gh run view $runs[$repo] --repo \"crgolden/$repo\" --json conclusion",
                                           "}"])),
        ("deny", "one gh run watch in a script is a wait script, loop or not",
         own_script("watch_push_runs.ps1", ["gh run watch 39794533331 --repo crgolden/Directory --interval 60 *> $null",
                                            "gh run view 39794533331 --repo crgolden/Directory --json conclusion"])),
        ("deny", "gh pr checks --watch in a script is a wait script",
         own_script("checks.ps1", ["gh pr checks 12 --repo crgolden/Tools --watch --interval 30"])),
        ("deny", "a bare gh run watch blocks on a CI run",
         lambda: plain("gh run watch 39750244568 --repo crgolden/Churches --exit-status --interval 30")),
        ("deny", "a bare gh.exe run watch blocks on a CI run",
         lambda: plain("gh.exe run watch 39798504110 --repo crgolden/Modules --exit-status --interval 60")),
        ("deny", "a bare gh pr checks --watch blocks on a CI run",
         lambda: plain("gh pr checks 12 --repo crgolden/Tools --watch")),
        ("defer", "control: one read of a run's state is no wait",
         lambda: plain("gh run view 39750244568 --repo crgolden/Churches --json status,conclusion")),
        ("defer", "control: listing runs is no wait", lambda: plain("gh run list --repo crgolden/Directory --limit 3")),
        ("defer", "control: gh pr checks without --watch reads once", lambda: plain("gh pr checks 12 --repo crgolden/Tools")),
        ("defer", "control: a command that only mentions gh run watch in its text runs no wait",
         lambda: plain('gh pr comment 12 --repo crgolden/Tools --body "never block in gh run watch"')),
        ("deny", "a Python script passing gh run watch as an argv list is a wait script",
         own_script("watch_runs.py", ["import subprocess", "for run_id in ['39796450150', '39796497086']:",
                                      "    subprocess.run(['gh', 'run', 'watch', run_id, '--repo', 'crgolden/Tools'])"],
                    runner="python")),
        ("deny", "a Python script passing gh pr checks --watch as an argv list is a wait script",
         own_script("checks.py", ["import subprocess", "subprocess.run([\"gh\", \"pr\", \"checks\", \"12\", \"--watch\"])"],
                    runner="python")),
        ("deny", "a script blocking in az ... wait is a wait script",
         own_script("deploy-wait.ps1", ["az webapp deploy --name web --resource-group crgolden --src-path app.zip",
                                        "az deployment group wait --created --name web --resource-group crgolden"])),
        ("deny", "a script that names gh run watch in a string is judged by its text, like a gate name in a wrapper",
         own_script("help.ps1", ["Write-Output 'never use gh run watch'", "Get-Service Redis"])),
        ("defer", "control: a comment naming gh run watch is stripped before the script is judged",
         own_script("notes.ps1", ["# never use gh run watch", "Get-Service Redis"])),
        ("defer", "control: a PowerShell comment block naming gh run watch is stripped too",
         own_script("block.ps1", ["<#", "gh run watch 39750244568 --repo crgolden/Churches", "#>", "Get-Service Redis"])),
        ("deny", "gh run watch inside a here-string is no comment, so it is judged",
         own_script("here.ps1", ["$body = @'", "gh run watch 39750244568 --repo crgolden/Churches", "'@",
                                 "Get-Service Redis"])),
        ("deny", "a bare gh run watch behind the call operator blocks on a CI run",
         lambda: plain("& gh run watch 39750244568 --repo crgolden/Churches --exit-status")),
        ("deny", "a bare gh run watch through a quoted full path blocks on a CI run",
         lambda: plain('"C:\\Program Files\\GitHub CLI\\gh.exe" run watch 39750244568 --repo crgolden/Churches')),
        ("deny", "a bare gh run watch through cmd /c blocks on a CI run",
         lambda: plain("cmd /c gh.exe run watch 39750244568 --repo crgolden/Churches")),
        ("deny", "a bare az ... wait blocks on a deployment",
         lambda: plain("az deployment group wait --created --name web --resource-group crgolden")),
        ("defer", "control: wait as an argument's text, not az's verb, is no wait",
         lambda: plain('az webapp update --name web --resource-group crgolden --set tags.note="please wait"')),
        ("defer", "control: an az command with --no-wait returns at once",
         lambda: plain("az deployment group create --resource-group crgolden --template-file main.bicep --no-wait")),
        ("defer", "control: a one-shot reader of a finished task's output file is no wait script",
         own_script("tail_task.py", ["import sys", "with open(sys.argv[1]) as f:", "    lines = f.read().splitlines()",
                                     "for line in lines[-int(sys.argv[2]):]:", "    print(line)"], runner="python")),
        # The operator, on a committed wait loop run from a workspace repo a minute after its own wait script was
        # refused: "we need to determine why the hook allows this as that is the real gap".
        ("deny", "a workspace-repo script the checkout reports modified is the session's own: its wait loop is judged",
         repo_script("Invoke-Matrix.ps1", ["while (Test-Path $hold) {", "    Start-Sleep -Seconds 60", "}"],
                     state="modified")),
        ("deny", "an untracked script inside a workspace repo is the session's own too",
         repo_script("watch-matrix.ps1", ["while (Test-Path $hold) {", "    Start-Sleep -Seconds 60", "}"],
                     state="untracked")),
        ("defer", "control: the same wait loop committed in the repo is reviewed code, the repo's own harness's job",
         repo_script("Invoke-Matrix.ps1", ["while (Test-Path $hold) {", "    Start-Sleep -Seconds 60", "}"],
                     state="committed")),
        ("deny", "a modified workspace-repo script wrapping a gate is judged too",
         repo_script("run-gate.ps1", ["& 'C:\\w\\Tools\\gate.ps1' -Steps 'Stop hook harness'"], state="modified")),
        ("defer", "control: a modified workspace-repo script that neither waits nor wraps runs",
         repo_script("Get-Health.ps1", ["Get-Service Redis", "Get-ChildItem C:\\repo"], state="modified")),
        ("deny", "a Python script writing a work-product declaration skips the edit-time approval check",
         own_script("tag_items.py", ["import json, os",
                                     "path = os.path.join(os.environ['TEMP'], 'claude', 'work-products', 's.json')",
                                     "data = json.load(open(path))",
                                     "with open(path, 'w') as fh:", "    json.dump(data, fh)"], runner="python")),
        ("deny", "a PowerShell script writing a work-product declaration skips the edit-time approval check",
         own_script("tag.ps1", ["$p = Join-Path $env:TEMP 'claude\\work-products\\s.json'", "'{}' | Set-Content $p"])),
        ("defer", "control: a script only reading a work-product declaration writes nothing",
         own_script("counts.py", ["import json, os",
                                  "path = os.path.join(os.environ['TEMP'], 'claude', 'work-products', 's.json')",
                                  "print(len(json.load(open(path))['repos']))"], runner="python")),
        ("defer", "control: a script naming the declarations folder in a search pattern and writing its own file writes no declaration",
         own_script("inventory.py", ["import json, re", "pattern = re.compile(r'work-products?\\b|work_product')",
                                     "with open('inventory.json', 'w') as fh:", "    json.dump({}, fh)"], runner="python")),
        ("defer", "control: a script whose prose names the folder and writes a page elsewhere writes no declaration",
         own_script("page.py", ["text = 'the literal work-products and a file write'",
                                "with open('page.html', 'w') as fh:", "    fh.write(text)"], runner="python")),
        ("deny", "must-block: a script joining the folder name as a path segment and writing there writes a declaration",
         own_script("segment.py", ["import json, os", "path = os.path.join('C:\\\\t', 'claude', 'work-products', 's.json')",
                                   "with open(path, 'w') as fh:", "    json.dump({}, fh)"], runner="python")),

        # --- what makes a script a gate, and what counts as its code ---------------------------------
        # The operator, on the triage poll's -RegateJob refused as a gate: "why does the gate-cadence hook block running
        # invocation scripts? those are not gates" and "use a naming convention like all gate files must be named".
        ("ask", "a script loading the gate library through GATE_LIBRARY is a gate",
         custom_regate(["$gateCommon = $env:GATE_LIBRARY", ". $gateCommon", "Invoke-Owed"])),
        ("ask", "a script dot-sourcing GateCommon.ps1 by path is a gate",
         custom_regate([". (Join-Path $PSScriptRoot 'Gates\\GateCommon.ps1')", "Invoke-Owed"])),
        ("ask", "a script that runs a gate step and prints a verdict is still a gate",
         custom_regate(["dotnet test App.sln", "Write-Host 'GATE: PASSED'"])),
        ("deny", "a session script running a harness through a runner is a wrapper",
         custom_regate(["& python 'C:\\repo\\Tools\\Hooks\\test_x.py'"])),
        ("deny", "a session script assigning a harness and running it through the variable is a wrapper",
         custom_regate(["$h = Join-Path $PSScriptRoot 'Hooks\\test_x.py'", "& python $h"])),
        ("deny", "a Python script running a harness held in a variable is a wrapper",
         own_script("runner.py", ["import subprocess, sys", "harness = r'C:\\t\\Hooks\\test_x.py'",
                                  "subprocess.run([sys.executable, '-u', harness])"], runner="python")),
        ("defer", "a script that prints a GATE: line but runs nothing is no gate",
         custom_regate(["Write-Output 'GATE: PASSED'"])),
        ("defer", "a script that only compares a gate result's verdict it was handed is no gate",
         custom_regate(["$gate = Get-Content $file -Raw | ConvertFrom-Json",
                        "if ([string]$gate.Verdict -cmatch '^GATE: FAILED') { Send-Report $gate }"])),
        ("defer", "a script that only sets GATE_LIBRARY loads no gate library",
         custom_regate(["$env:GATE_LIBRARY = Join-Path $root 'Tools\\Gates\\GateCommon.ps1'", "Start-ScheduledTask -TaskName t"])),
        ("defer", "a script naming a harness as data runs no harness",
         custom_regate(["$evidence = 'Tools/AlertTriage/Test-AlertTriage.ps1:1802 pins the limit'",
                        "& python $audit record x --evidence $evidence"])),
        ("ask", "a script running steps through a variable is a gate",
         custom_regate(["foreach ($step in $steps) { npm run $step }"])),
        ("defer", "GATE: only inside a comment makes no gate",
         custom_regate(["# this probe prints no GATE: line", "<# nor GATE: here #>", "Get-ChildItem C:\\repo"])),
        ("defer", "a spec file named in a comment is not narrowing",
         narrowed_script(["# pytest here reads no conftest.py", "python -m pytest -q"])),
        ("defer", "a gate step named only in a comment makes no gate, even under a pinned goal",
         goal_gate(["GOAL: ship the widget export"], ["# dotnet build is deliberately not run here", "Get-ChildItem C:\\repo"],
                   steps=[])),
        ("deny", "a gate step in code beside the same comment is a gate under a pinned goal",
         goal_gate(["GOAL: ship the widget export"], ["# dotnet build below is the gate", "dotnet build C:\\repo\\App.slnx"],
                   steps=[])),

        # --- a gate script must run every step its repo's workflow runs on a pull request -------------
        ("deny", "a script running only the e2e step", script_in_repo(APP_WORKFLOW, ["npm run e2e"])),
        ("defer", "a script naming every required step",
         script_in_repo(APP_WORKFLOW, ["npm run lint", "dotnet test --project App.Tests.Unit -- --filter-trait Category=Unit",
                                       "npm run e2e"])),
        ("defer", "required steps behind a variable, named in the code",
         script_in_repo(APP_WORKFLOW, ["foreach ($step in @('lint', 'e2e')) { npm run $step }",
                                       "& 'C:\\out\\App.Tests.Unit.exe' -trait \"Category=Unit\""])),
        ("deny", "required steps named only in comments",
         script_in_repo(APP_WORKFLOW, ["# lint, then App.Tests.Unit Category=Unit", "<# npm run lint #>", "npm run e2e"])),
        ("deny", "a test tier run without its trait",
         script_in_repo(APP_WORKFLOW, ["npm run lint", "dotnet test --project App.Tests.Unit", "npm run e2e"])),
        ("defer", "a schedule-only workflow requires nothing", script_in_repo(SCHEDULED_WORKFLOW, ["npm run e2e"])),
        ("deny", "npm run test:coverage is not npm test",
         script_in_repo(NPM_TEST_WORKFLOW, ["npm run build", "npm run test:coverage", "# npm test is the next step"])),
        ("defer", "npm test run in the code satisfies npm test",
         script_in_repo(NPM_TEST_WORKFLOW, ["npm run build", "npm test"])),
        ("deny", "a workflow's jb inspectcode step is required of the gate",
         script_in_repo(JB_WORKFLOW, ["dotnet build --configuration Release"])),
        ("defer", "a gate running the workflow's jb inspectcode step",
         script_in_repo(JB_WORKFLOW, ["dotnet build --configuration Release",
                                      'dotnet jb inspectcode "C:\\repo\\App.slnx" --no-build -e=WARNING -o="C:\\out\\App.sarif"'])),
        ("deny", "a scratch gate for a repo that has its own checked-in gate.ps1",
         script_in_repo(NPM_TEST_WORKFLOW, ["npm run build", "npm test"], checked_in_gate=True)),
        ("defer", "the same scratch gate for a repo with no checked-in gate.ps1",
         script_in_repo(NPM_TEST_WORKFLOW, ["npm run build", "npm test"])),
        ("defer", "a plant-declared narrowed run inside a repo that has its own checked-in gate.ps1",
         script_in_repo(NPM_TEST_WORKFLOW, ["APP_CONFIG = REPO / 'src' / 'app' / 'app.config.ts'",
                                            "APP_CONFIG.write_text(original.replace(PLANT, ''))",
                                            "GATE_DELTA = ['plant:src/app/app.config.ts']",
                                            "npx vitest run src/app/app.spec.ts"], checked_in_gate=True)),
        ("deny", "a declared delta on a script that is not narrowed still redirects to the checked-in gate.ps1",
         script_in_repo(NPM_TEST_WORKFLOW, ["APP_CONFIG = REPO / 'src' / 'app' / 'app.config.ts'",
                                            "APP_CONFIG.write_text(original.replace(PLANT, ''))",
                                            "GATE_DELTA = ['plant:src/app/app.config.ts']",
                                            "npm run build", "npm test"], checked_in_gate=True)),
        ("defer", "running the repo's own checked-in gate.ps1",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"])),
        # --- every registered step declares its inputs, spelled as registered -------------------------
        ("defer", "step inputs: every registered step declared",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["Register-GateSteps @('Build (Release',",
                                                    "    'Run tests')",
                                                    "Register-StepInputs @{",
                                                    "    'Build (Release' = @('*')",
                                                    "    'Run tests'      = @('src/*', 'package.json')",
                                                    "}", "npm run build", "npm test"])),
        ("deny", "step inputs: a registered step with no entry",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["Register-GateSteps @('Build (Release',",
                                                    "    'Run tests')",
                                                    "Register-StepInputs @{",
                                                    "    'Build (Release' = @('*')",
                                                    "}", "npm run build", "npm test"])),
        ("deny", "step inputs: no Register-StepInputs block at all",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["Register-GateSteps @('Build (Release', 'Run tests')",
                                                    "npm run build", "npm test"])),
        # --- every checked-in gate run names the steps it runs ------------------------------------------
        ("deny", "-Steps: a checked-in gate launched without it",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["Register-GateSteps @('Build (Release', 'Run tests')",
                                                    "Register-StepInputs @{", "    'Build (Release' = @('*')",
                                                    "    'Run tests'      = @('*')", "}", "npm run build", "npm test"],
                                steps_argument="")),
        ("deny", "-Steps: an empty one",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["Register-GateSteps @('Build (Release', 'Run tests')",
                                                    "Register-StepInputs @{", "    'Build (Release' = @('*')",
                                                    "    'Run tests'      = @('*')", "}", "npm run build", "npm test"],
                                steps_argument=" -Steps ''")),
        ("deny", "-Steps: naming a step the gate does not have",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["Register-GateSteps @('Build (Release', 'Run tests')",
                                                    "Register-StepInputs @{", "    'Build (Release' = @('*')",
                                                    "    'Run tests'      = @('*')", "}", "npm run build", "npm test"],
                                steps_argument=" -Steps 'Run tests,Run linter'")),
        ("deny", "-Steps: owed on a gate that defines no Resolve-OwedSteps is an unknown step",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["Register-GateSteps @('Build (Release', 'Run tests')",
                                                    "Register-StepInputs @{", "    'Build (Release' = @('*')",
                                                    "    'Run tests'      = @('*')", "}", "npm run build", "npm test"],
                                steps_argument=" -Steps owed")),
        ("deny", "must-block owed gate: a gate calling Resolve-OwedSteps launched with a step list",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["Register-GateSteps @('Build (Release', 'Run tests')",
                                                    "Register-StepInputs @{", "    'Build (Release' = @('*')",
                                                    "    'Run tests'      = @('*')", "}", "Resolve-OwedSteps",
                                                    "npm run build", "npm test"],
                                steps_argument=" -Steps 'Run tests'")),
        ("deny", "must-block owed gate: a gate calling Resolve-OwedSteps launched with -Steps owed",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["Register-GateSteps @('Build (Release', 'Run tests')",
                                                    "Register-StepInputs @{", "    'Build (Release' = @('*')",
                                                    "    'Run tests'      = @('*')", "}", "Resolve-OwedSteps",
                                                    "npm run build", "npm test"],
                                steps_argument=" -Steps owed")),
        ("defer", "owed gate: a gate calling Resolve-OwedSteps launched with no -Steps",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["Register-GateSteps @('Build (Release', 'Run tests')",
                                                    "Register-StepInputs @{", "    'Build (Release' = @('*')",
                                                    "    'Run tests'      = @('*')", "}", "Resolve-OwedSteps",
                                                    "npm run build", "npm test"],
                                steps_argument="")),
        ("deny", "owed gate control: a name merely mentioning Resolve-OwedSteps still needs -Steps",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["Register-GateSteps @('Build (Release', 'Run tests')",
                                                    "Register-StepInputs @{", "    'Build (Release' = @('*')",
                                                    "    'Run tests'      = @('*')", "}",
                                                    "function Resolve-OwedSteps { }",
                                                    "npm run build", "npm test"],
                                steps_argument="")),
        ("defer", "-Steps: naming registered steps and a catalog",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["Register-GateSteps @('Build (Release', 'Run tests')",
                                                    "Register-StepInputs @{", "    'Build (Release' = @('*')",
                                                    "    'Run tests'      = @('*')", "}", "npm run build", "npm test"],
                                steps_argument=" -Steps 'Build (Release,Run tests,Comment catalog'")),
        ("deny", "step inputs: an entry spelled differently from the step it means",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["Register-GateSteps @('Build (Release', 'Run tests')",
                                                    "Register-StepInputs @{",
                                                    "    'Build (Release' = @('*')",
                                                    "    'Run test'       = @('*')",
                                                    "}", "npm run build", "npm test"])),
        # --- the declared work product: a checked-in gate runs only once every item is done ----------
        ("deny", "work product: no declaration",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"], declaration=None)),
        ("deny", "work product: a declaration naming another repo only",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"],
                                declaration={"Other": [("its deliverable", True)]})),
        ("deny", "work product: an open item",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"],
                                declaration={"App": [("the journeys", True), ("the integration move", False)]})),
        # A planted proof's item is open by design while its plant is in place; the file decides, not the claim.
        ("defer", "work product: an open plant item while its plant is in place",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"],
                                declaration={"App": [("the journeys", True), ("the planted proof", False, "Steps.cs")]},
                                repo_files={"Steps.cs": "throw new Exception(\"PLANTED failure\");\n"})),
        ("deny", "work product: an open plant item whose plant was removed",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"],
                                declaration={"App": [("the journeys", True), ("the planted proof", False, "Steps.cs")]},
                                repo_files={"Steps.cs": "return Task.CompletedTask;\n"})),
        ("deny", "work product: an open plant item naming a file that does not exist",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"],
                                declaration={"App": [("the journeys", True), ("the planted proof", False, "Gone.cs")]})),
        ("deny", "work product: a plant item claimed done while its plant is still in place",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"],
                                declaration={"App": [("the journeys", True), ("the planted proof", True, "Steps.cs")]},
                                repo_files={"Steps.cs": "throw new Exception(\"PLANTED failure\");\n"})),
        ("deny", "work product: a plant in place excuses no other open item",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"],
                                declaration={"App": [("the journeys", False), ("the planted proof", False, "Steps.cs")]},
                                repo_files={"Steps.cs": "throw new Exception(\"PLANTED failure\");\n"})),
        ("deny", "work product: a malformed declaration (done is not a boolean)",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"], declaration={"App": [("x", "yes")]})),
        ("deny", "work product: a repo declared with no items",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"], declaration={"App": []})),
        ("deny", "work product: an item removed since the hook read it",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"],
                                declaration={"App": [("the journeys", True)]},
                                ledger={"App": ["the journeys", "the integration move"]})),
        ("defer", "work product: a removed item the operator approved in a turn",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"],
                                declaration={"App": [("the journeys", True)]},
                                ledger={"App": ["the journeys", "the integration move"]},
                                records=[human_turn(600, "Fine, leave the integration move for later.")],
                                dropped={"App": [("the integration move", "leave the integration move for later")]})),
        ("defer", "work product: a removed item the operator approved mid-turn",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"],
                                declaration={"App": [("the journeys", True)]},
                                ledger={"App": ["the journeys", "the integration move"]},
                                records=[human_mid_turn(600, "drop the integration move from this gate")],
                                dropped={"App": [("the integration move", "Drop the integration move!")]})),
        ("deny", "work product: an approval the operator never said",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"],
                                declaration={"App": [("the journeys", True)]},
                                ledger={"App": ["the journeys", "the integration move"]},
                                records=[human_turn(600, "keep going")],
                                dropped={"App": [("the integration move", "leave the integration move for later")]})),
        ("deny", "work product: an approval quoted from a session's own words, not the operator's",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"],
                                declaration={"App": [("the journeys", True)]},
                                ledger={"App": ["the journeys", "the integration move"]},
                                records=[session_words(600, "leave the integration move for later")],
                                dropped={"App": [("the integration move", "leave the integration move for later")]})),
        ("deny", "work product: an approval of fewer than three words",
         checked_in_gate_itself(NPM_TEST_WORKFLOW, ["npm run build", "npm test"],
                                declaration={"App": [("the journeys", True)]},
                                ledger={"App": ["the journeys", "the integration move"]},
                                records=[human_turn(600, "ok later")],
                                dropped={"App": [("the integration move", "ok later")]})),
        ("deny", "a checked-in gate whose last record passed on exactly these inputs",
         recorded_gate()),
        ("defer", "a checked-in gate whose last passing record is stale after an edit",
         recorded_gate(edit_after_record=True)),
        ("defer", "a checked-in gate whose last record failed",
         recorded_gate(verdict="FAILED")),
        ("defer", "a checked-in gate whose last run started and never completed",
         recorded_gate(verdict="RUNNING")),
        ("deny", "must-block: a checked-in gate on a repo with no uncommitted changes",
         clean_gate()),
        ("deny", "must-block: a gate's own build leftovers do not make a committed repo dirty",
         clean_gate(leftover=True)),
        ("defer", "control: the same committed repo with one uncommitted source file is gated",
         clean_gate(pending=True)),
        ("deny", "must-block: a gate on a repo with a solution and no build in the session", built_gate()),
        ("deny", "must-block: a gate whose checkout changed after its last build", built_gate(build_ago=900)),
        ("deny", "must-block: a background build is no proof", built_gate(build_ago=300, background=True)),
        ("deny", "must-block: a failed build is no proof", built_gate(build_ago=300, failed=True)),
        ("deny", "must-block: a build of a path outside the checkout is no proof",
         built_gate(build_ago=300, build_inside=False)),
        ("deny", "must-block: a source file written after the build", built_gate(build_ago=300, changed_after="Late.cs")),
        ("defer", "control: a successful foreground build after the last change", built_gate(build_ago=300)),
        ("defer", "control: a markdown file written after the build needs no rebuild",
         built_gate(build_ago=300, changed_after="NOTES.md")),
        ("defer", "control: a gate's Stryker leftovers written after the build need no rebuild",
         built_gate(build_ago=300, changed_after=os.path.join("StrykerOutput", "report.json"))),
        ("deny", "must-block: a worktree's gate launched without -Steps is held to the checked-in rule",
         worktree_gate(steps_argument="")),
        ("defer", "control: the same worktree gate naming its own step",
         worktree_gate(steps_argument=" -Steps 'Run tests'")),
        ("deny", "must-block: a worktree gate naming a step only the main checkout's gate has",
         worktree_gate(steps_argument=" -Steps 'Build with dotnet'")),
        ("deny", "must-block: a worktree whose own record passed on exactly these inputs is not re-gated",
         worktree_gate(steps_argument=" -Steps 'Run tests'", record_for="self")),
        ("defer", "control: a record a sibling worktree wrote never stands for this worktree",
         worktree_gate(steps_argument=" -Steps 'Run tests'", record_for="sibling")),
        ("defer", "control: a worktree of a repo outside the workspace is no checked-in gate",
         worktree_gate(steps_argument="", outside_workspace=True)),
        ("deny:is not on origin yet",
         "must-block: a Tools gate while a sibling worktree passed after origin's head with its change uncommitted",
         peer_green()),
        ("deny:is not on origin yet",
         "must-block: a Tools gate while a sibling worktree passed after origin's head with its change unpushed",
         peer_green(peer_state="unpushed")),
        ("defer", "control: the sibling's passing record is older than origin's head",
         peer_green(completed="2019-01-01T00:00:00Z")),
        ("defer", "control: the sibling's newer record failed", peer_green(verdict="FAILED")),
        ("defer", "control: the sibling's newer passing change is already on origin", peer_green(peer_state="clean")),
        ("defer", "control: a sibling green its session edited past (GREEN-STALE) holds no gate, since it lands next as nothing",
         peer_green(stale=True)),
        ("defer", "control: a stale sibling green with its edit committed but unpushed holds no gate either",
         peer_green(peer_state="unpushed", stale=True)),
        ("defer", "control: a sibling's newer unlanded pass holds no gate of a repo other than Tools",
         peer_green(repo_name="App")),
        ("defer", "an edit script that only names a harness file in a gated repo runs no gate",
         script_in_repo(NPM_TEST_WORKFLOW, ["$target = 'C:\\repo\\Tools\\Claude\\Test-WidgetCatalog.ps1'"],
                        checked_in_gate=True)),
        ("defer", "the same edit script carrying the opt-out marker",
         script_in_repo(NPM_TEST_WORKFLOW, ["# GATE-CADENCE: gate-step-strings-are-data",
                                            "$target = 'C:\\repo\\Tools\\Claude\\Test-WidgetCatalog.ps1'"],
                        checked_in_gate=True)),
        ("defer", "a harness naming a repo with a checked-in gate is not redirected to it",
         script_in_repo(APP_WORKFLOW, ["CASES = ['npm run e2e']"], name="test_repo_fixture.py", runner="python",
                        checked_in_gate=True)),
        ("defer", "a harness naming a repo is not held to its workflow",
         script_in_repo(APP_WORKFLOW, ["CASES = ['npm run e2e']"], name="test_repo_fixture.py", runner="python")),

        # --- a tests-only run: a checked-in gate.ps1 given -Tests ------------------------------------
        ("deny", "tests-only: a name in no file this session edited", tests_only("'Widget_Renders'", edited=False)),
        ("deny", "tests-only: a name found only in an edited production file",
         tests_only("'Widget_Renders'", test_file="Widget.cs")),
        ("deny", "tests-only: a name found only in edited markdown", tests_only("'Widget_Renders'", test_file="TESTING.md")),
        ("deny", "tests-only: an empty -Tests", tests_only("''")),
        ("deny", "tests-only: a wildcard name", tests_only("'Widget_*'")),
        ("deny", "tests-only: a name that is a substring of an edited test", tests_only("'Widget_Render'")),
        ("deny", "tests-only: one of two names unverified", tests_only("'Widget_Renders,Gadget_Renders'")),
        ("deny", "tests-only: a qualified name whose class the file never names",
         tests_only("'App.Tests.GadgetTests.Widget_Renders'")),
        ("deny", "tests-only: while this session's own gate is in flight",
         tests_only("'Widget_Renders'", history=lambda fixture, selected, full: fixture.background_launch(
             120, fixture.gate_command)[0])),
        ("defer", "tests-only: a bare name in an edited test file", tests_only("'Widget_Renders'")),
        ("defer", "tests-only: a bare, unquoted name", tests_only("Widget_Renders")),
        ("defer", "tests-only: two double-quoted names", tests_only('"Widget_Renders, Widget_Hides"')),
        ("defer", "tests-only: a qualified name, class and method in one edited test file",
         tests_only("'App.Tests.WidgetTests.Widget_Renders'")),
        ("defer", "tests-only: a full gate after a tests-only run with no edit between",
         tests_only("'Widget_Renders'", run_full=True,
                    history=lambda fixture, selected, full: fixture.foreground_gate(300, selected))),
        ("ask", "tests-only control: a full gate after a full gate with no edit between",
         tests_only("'Widget_Renders'", run_full=True,
                    history=lambda fixture, selected, full: fixture.foreground_gate(300, full))),
        ("deny", "a scratch script calling the tests-only runner", scratch_calling_selected_runner),
        ("deny", "harness selection: a harness this session never edited", harness_selected("widget", edited=False)),
        ("deny", "harness selection: a term naming no case", harness_selected("gadget")),
        ("deny", "harness selection: one of two terms naming no case", harness_selected('widget "gadget renders"')),
        ("deny", "harness selection: --only with nothing after it", harness_selected("")),
        ("defer", "harness selection: an edited harness, a term naming its cases, a fixture delta ignored",
         harness_selected("widget")),
        ("defer", "harness selection: two quoted terms, a later flag ends the list",
         harness_selected('"widget renders" "widget hides" --verbose')),
        ("defer", "the checked-in gate carrying the tests-only runner, run in full",
         tests_only("'Widget_Renders'", run_full=True)),

        # --- a probe: a checked-in gate.ps1 given -Probe, one step run with no verdict recorded --------
        ("deny", "probe: a step the gate does not register", probe_run("'Gadget harness'")),
        ("deny", "probe: a step no file this session edited is an input of", probe_run("'Docs harness'")),
        ("deny", "probe: one of two steps unreached by the session's edits", probe_run("'Widget harness,Docs harness'")),
        ("deny", "probe: an empty -Probe", probe_run("''")),
        ("deny", "probe: no edit in this session at all", probe_run("'Widget harness'", edited_file=None)),
        ("deny", "probe: while this session's own gate is in flight",
         probe_run("'Widget harness'", history=lambda fixture, probe, full: fixture.background_launch(
             120, fixture.gate_command)[0])),
        ("defer", "probe: a registered step an edited file is an input of", probe_run("'Widget harness'")),
        ("deny", "probe: must-block: a markdown edit reaches no step, as GateCommon fingerprints none",
         probe_run("'Docs harness'", edited_file="docs/notes.md")),
        ("defer", "probe: a gate that plans its steps: a step whose derived inputs hold the edited file",
         probe_run("'Widget harness'", planned=True)),
        ("deny", "probe: must-block: a gate that plans its steps: a step whose derived inputs the edit is not in",
         probe_run("'Docs harness'", planned=True)),
        ("deny", "probe: must-block: a gate that plans its steps: a step its plan does not have",
         probe_run("'Gadget harness'", planned=True)),
        ("defer", "probe: two steps, each reached by an edit",
         probe_run("'Widget harness,Docs harness'", edited_file="docs/notes.txt",
                   history=lambda fixture, probe, full: [edit(410, os.path.join(
                       os.path.dirname(probe.split('"')[1]), "src", "widget.py"))])),
        ("defer", "probe: a full gate after a probe with no edit between is not a re-run",
         probe_run("'Widget harness'", run_full=True,
                   history=lambda fixture, probe, full: fixture.foreground_gate(300, probe))),
        ("ask", "probe control: a full gate after a full gate with no edit between",
         probe_run("'Widget harness'", run_full=True,
                   history=lambda fixture, probe, full: fixture.foreground_gate(300, full))),

        # --- a peer session on this machine: deny -------------------------------------------------
        ("deny", "peer background gate running", peer()),
        ("defer", "peer gate whose output file is not on this machine", peer(output_exists=False)),
        ("defer", "peer transcript outside the window", peer(age_seconds=gate.PEER_WINDOW_SECONDS + 600)),
        ("defer", "peer gate already finished", peer(finished=True)),
        ("deny", "peer foreground gate pending on this machine", peer(foreground=True)),
        ("defer", "peer foreground gate pending, session not on this machine",
         peer(foreground=True, local_session=False)),
        ("defer", "a resumed session's own finished gate is not a peer's in the transcript it left",
         resumed(finished_in_new=True)),
        ("deny", "a resumed session's own gate still running blocks as its own",
         resumed(finished_in_new=False)),
    ]


def build_decide_cases(fixtures):
    """decide() around the evaluator: each builder returns (decision, reason). decide() keeps no clock, so a
    gate the evaluator cannot judge because it raises must deny, never ask and never pass."""

    def gate_command():
        fixture = Fixture()
        fixtures.append(fixture)
        return fixture.gate_command

    def broken(*_):
        raise KeyError("planted")

    def fast(*_):
        return gate.DEFER, ""

    tools = os.path.join(gate.session_home.workspace_root(), gate.OPERATIONAL_PROBES_CHECKOUT)
    version_audit = os.path.join(tools, "Claude", "skills", "version-audit")
    vm_probe = os.path.join(version_audit, "scripts", "Test-VmService.ps1")

    def run(path, arguments=""):
        return 'pwsh -File "{0}"{1}'.format(path, arguments)

    def probe_through_junction():
        root = tempfile.mkdtemp(prefix="gate-cadence-junction-", dir=claude_temp.scratch_dir())
        fixtures.append(types.SimpleNamespace(root=root))
        link = os.path.join(root, "version-audit")
        import _winapi
        _winapi.CreateJunction(version_audit, link)
        try:
            return gate.decide(run(os.path.join(link, "scripts", "Test-VmService.ps1"), " -Service grafana -Since 1"),
                               None, None, evaluator=broken)
        finally:
            os.rmdir(link)

    def probe_copy():
        fixture = Fixture()
        fixtures.append(fixture)
        with open(vm_probe, "r", encoding="utf-8") as handle:
            body = handle.read()
        copy = fixture.write_file("Test-VmService.ps1", body.splitlines())
        return gate.decide(run(copy, " -Service grafana -Since 1"), None, None, evaluator=broken)

    return [
        ("deny", "decide: an evaluation that raises denies, never asks",
         lambda: gate.decide(gate_command(), None, None, evaluator=broken)),
        ("defer", "decide: a command that is not a gate never reaches the evaluator",
         lambda: gate.decide("git status --porcelain", None, None, evaluator=broken)),
        ("defer", "decide: an evaluation passes its decision through",
         lambda: gate.decide(gate_command(), None, None, evaluator=fast)),
        ("defer", "decide: probe Test-VmService.ps1 by its Tools path is no gate",
         lambda: gate.decide(run(vm_probe, " -Service grafana -Since 1"), None, None, evaluator=broken)),
        ("defer", "decide: probe Test-VmService.ps1 through a skills junction is no gate", probe_through_junction),
        ("defer", "decide: probe Test-Irs990Coverage.ps1 by its Tools path is no gate",
         lambda: gate.decide(run(os.path.join(tools, "Seeding", "Test-Irs990Coverage.ps1")), None, None,
                             evaluator=broken)),
        ("deny", "decide: probe control, a same-named copy elsewhere stays a harness", probe_copy),
        ("defer", "decide: the triage poll's request modes (-RegateJob) are an invocation, not a gate",
         lambda: gate.decide(run(os.path.join(tools, "AlertTriage", "Invoke-AlertTriagePoll.ps1"),
                                 " -RegateJob 20261026-003451-13b85455-cgldwalkerfail1"), None, None, evaluator=broken)),
        ("deny", "decide: a real repo gate.ps1 is a gate by its name",
         lambda: gate.decide(run(os.path.join(gate.TOOLS_ROOT, "gate.ps1")), None, None, evaluator=broken)),
        ("deny", "decide: probe control, a real Test- harness stays a gate",
         lambda: gate.decide(run(os.path.join(tools, "Grafana", "Test-GrafanaBackup.ps1")), None, None,
                             evaluator=broken)),
    ]


def comparable(collected):
    gates, edits = collected
    return sorted((sorted(item.items()) for item in gates), key=repr), sorted(edits, key=repr)


def build_scan_cases(fixtures):
    """The incremental scan against a full walk of the same file. Each builder returns True when the cached
    result equals the uncached one after the file changed under the cache."""

    def fresh():
        fixture = Fixture()
        fixtures.append(fixture)
        return fixture, os.path.join(fixture.root, "scan-cache")

    def append(path, text):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)

    def appended_with_a_partial_line():
        fixture, cache = fresh()
        records, task_id = fixture.background_launch(300, fixture.gate_command)
        past_the_head_hash_window = [edit(400, CODE_FILE)] * (gate.SCAN_HEAD_BYTES // 100)
        path = fixture.transcript(past_the_head_hash_window + records + [edit(200, CODE_FILE)])
        gate.collect([path], gate.classify, cache_root=cache)
        relaunch = json.dumps(shell(50, fixture.gate_command))
        append(path, json.dumps(notification(100, task_id)) + "\n" + json.dumps(edit(90, CODE_FILE)) + "\n"
               + relaunch[:40])
        partial = comparable(gate.collect([path], gate.classify, cache_root=cache))
        append(path, relaunch[40:] + "\n")
        cached = comparable(gate.collect([path], gate.classify, cache_root=cache))
        full = comparable(gate.collect([path], gate.classify))
        return cached == full and partial != full

    def rewritten_file():
        fixture, cache = fresh()
        path = fixture.transcript(fixture.foreground_gate(300, fixture.gate_command))
        gate.collect([path], gate.classify, cache_root=cache)
        with open(path, "w", encoding="utf-8") as handle:
            for record in [edit(200, CODE_FILE)] * 60 + fixture.foreground_gate(100, fixture.gate_command):
                handle.write(json.dumps(record) + "\n")
        return comparable(gate.collect([path], gate.classify, cache_root=cache)) \
            == comparable(gate.collect([path], gate.classify))

    def goal_pinned_after_the_cache():
        fixture, cache = fresh()
        path = fixture.transcript([operator(300, "GOAL: ship the widget export")])
        first = gate.pinned_goal(path, cache)
        append(path, json.dumps(operator(100, "GOAL: rename the importer")) + "\n")
        return first == "ship the widget export" and gate.pinned_goal(path, cache) == gate.pinned_goal(path) \
            == "rename the importer"

    def goal_latched_by(reader):
        fixture, cache = fresh()
        path = fixture.transcript([operator(300, "GOAL: ship the widget export")])
        goal_pin = gate.goal_pin_module()
        latch = goal_pin.latch_path(cache, None, path)
        os.makedirs(cache, exist_ok=True)
        with open(latch, "w", encoding="utf-8") as handle:
            json.dump({"reader": reader or goal_pin.pin_reader_digest(), "offset": os.path.getsize(path),
                       "command": None, "text": "a fragment"}, handle)
        return gate.pinned_goal(path, cache)

    def slash_command(seconds_ago, args_text, origin=None):
        """A slash command's user record as Claude Code writes it: no origin, <command-name> first."""
        content = ("<command-name>/goal</command-name>\n<command-message>goal</command-message>\n<command-args>"
                   + args_text + "</command-args>")
        record = {"type": "user", "timestamp": iso(seconds_ago), "message": {"role": "user", "content": content}}
        return dict(record, origin={"kind": origin}) if origin else record

    def operator_texts_with_a_slash_command():
        fixture, _ = fresh()
        path = fixture.transcript([operator(300, "finish it"), slash_command(200, "ship the widget export"),
                                   {"type": "user", "timestamp": iso(190), "message": {"role": "user", "content":
                                    "<local-command-stdout>Goal set: ship the widget export</local-command-stdout>"}},
                                   slash_command(180, "a peer quoting <command-args>", origin="peer")])
        return gate.operator_texts(path)

    return [
        ("equal", "operator words: the arguments of a slash command the operator ran are theirs",
         lambda: "ship the widget export" in operator_texts_with_a_slash_command()),
        ("equal", "operator words control: the typed turn beside the slash command still counts",
         lambda: "finish it" in operator_texts_with_a_slash_command()),
        ("equal", "operator words control: the command's printed output and a peer's record quoting the tag are not",
         lambda: not any("Goal set:" in text or "a peer quoting" in text
                         for text in operator_texts_with_a_slash_command())),
        ("equal", "scan: an append and a half-written line match a full walk", appended_with_a_partial_line),
        ("equal", "scan: a rewritten file is walked from the start", rewritten_file),
        ("equal", "scan: a goal pinned after the cache was written is read", goal_pinned_after_the_cache),
        ("equal", "scan: a goal latched by an older pin reader is read again",
         lambda: goal_latched_by("older-reader") == "ship the widget export"),
        ("equal", "scan control: a goal latched by this reader is trusted, not read again",
         lambda: goal_latched_by(None) == "a fragment"),
    ]


def build_widening_cases(fixtures):
    """The work product is fixed by its first Write: an item added or reworded after it needs the operator's
    verbatim approval, at gate time and at the edit itself. Each builder returns True when the hook decides
    as the row expects."""

    def session(approval_words):
        fixture = Fixture()
        fixtures.append(fixture)
        transcript = fixture.transcript([human_turn(900, approval_words)])
        path = gate.session_home.declaration_path(workspace_for(transcript), gate.session_stem(transcript))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        first = {"repos": {"App": [{"item": "the journeys", "done": False}]}}
        with open(transcript, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(tool_use(800, new_tool_id(), "Write", {"file_path": path, "content": json.dumps(first)})) + "\n")
        return transcript, path, first

    def write(path, items):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"repos": {"App": items}}, handle)

    def gate_time(approval, operator_words):
        transcript, path, _ = session(operator_words)
        added = dict({"item": "move the port everywhere", "done": True}, **({"approval": approval} if approval else {}))
        write(path, [{"item": "the journeys", "done": True}, added])
        return gate.work_product_problem(transcript, workspace_for(transcript), temp_root_for(transcript))

    def edit_time(new_items, operator_words):
        return keyed_edit({"App": new_items}, operator_words)

    def keyed_edit(repos, operator_words="finish it", first_write=False, dropped=None):
        transcript, path, first = session(operator_words)
        workspace = workspace_for(transcript)
        os.makedirs(os.path.join(workspace, "App", ".git"), exist_ok=True)
        worktree = os.path.join(workspace, "App-branch")
        os.makedirs(worktree, exist_ok=True)
        with open(os.path.join(worktree, ".git"), "w", encoding="utf-8") as handle:
            handle.write("gitdir: " + os.path.join(workspace, "App", ".git", "worktrees", "App-branch") + "\n")
        if not first_write:
            write(path, first["repos"]["App"])
        saved = gate.WORKSPACE
        gate.WORKSPACE = workspace
        try:
            body = dict({"repos": repos}, **({"dropped": dropped} if dropped else {}))
            return gate.work_product_edit_problem(path, {"content": json.dumps(body)}, transcript)
        finally:
            gate.WORKSPACE = saved

    def names_a_stray(reason, key):
        return bool(reason) and "name no git repo directly under the workspace" in reason and key in reason

    def first_write_judged(reject_first):
        """An empty Write of the declaration, then a Write holding the real items: when the first was refused
        (its tool result an error), the second is the first declaration and nothing was added after it."""
        fixture = Fixture()
        fixtures.append(fixture)
        transcript = fixture.transcript([human_turn(900, "finish it")])
        path = gate.session_home.declaration_path(workspace_for(transcript), gate.session_stem(transcript))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        empty_id = new_tool_id()
        records = [tool_use(850, empty_id, "Write", {"file_path": path, "content": json.dumps({"repos": {}})})]
        if reject_first:
            records.append({"type": "user", "timestamp": iso(845), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": empty_id, "is_error": True,
                 "content": "The user doesn't want to proceed with this tool use."}]}})
        real = {"repos": {"App": [{"item": "the journeys", "done": True}]}}
        records.append(tool_use(800, new_tool_id(), "Write", {"file_path": path, "content": json.dumps(real)}))
        with open(transcript, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(real, handle)
        return gate.work_product_problem(transcript, workspace_for(transcript), temp_root_for(transcript))

    def added_unapproved(reason):
        return bool(reason) and "not in this session's first declaration" in reason

    def names_no_file(reason):
        return bool(reason) and "naming no file" in reason

    def docs_key_refused(reason):
        return bool(reason) and "that name a file other than the workspace's AGENTS.md" in reason

    def first_items(*items):
        return keyed_edit({"App": [{"item": item, "done": False} for item in items]}, first_write=True)

    def declaration_command(command):
        return gate.script_abuse(command.format(declaration=r"C:\t\claude\work-products\s.json",
                                                elsewhere=r"C:\t\notes\s.json"), r"C:\w")

    def under_newer_goal(completed_write_after_goal, new_items, operator_words="finish it"):
        """The first declaration at 800s ago, then a /goal pinned at 700s ago. With completed_write_after_goal, a
        Write of journeys.ts and port.ts after the goal has completed; new_items is the Write judged now."""
        transcript, path, first = session(operator_words)
        workspace = workspace_for(transcript)
        os.makedirs(os.path.join(workspace, "App", ".git"), exist_ok=True)
        records = [goal_status(700, "address the port in port.ts")]
        answered = {"repos": {"App": first["repos"]["App"] + [{"item": "move the port in port.ts", "done": False}]}}
        if completed_write_after_goal:
            write_id = new_tool_id()
            records += [tool_use(650, write_id, "Write", {"file_path": path, "content": json.dumps(answered)}),
                        tool_result(645, write_id, "File written", {})]
        with open(transcript, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        write(path, (answered if completed_write_after_goal else first)["repos"]["App"])
        saved = gate.WORKSPACE
        gate.WORKSPACE = workspace
        try:
            edit = gate.work_product_edit_problem(path, {"content": json.dumps({"repos": {"App": new_items}})}, transcript)
        finally:
            gate.WORKSPACE = saved
        return edit, gate.work_product_problem(transcript, workspace, temp_root_for(transcript))

    def tools_home_edit(repos, approval_words="finish it", workspace_exists=True):
        """A session running in a Tools checkout declares the checkout's own name as a repo, which counts with no
        workspace present."""
        fixture = Fixture()
        fixtures.append(fixture)
        transcript = fixture.transcript([human_turn(900, approval_words)])
        tools_root = os.path.join(os.path.dirname(workspace_for(transcript)), "Kit")
        os.makedirs(os.path.join(tools_root, ".git"), exist_ok=True)
        path = gate.session_home.declaration_path(workspace_for(transcript), gate.session_stem(transcript))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        first = {"repos": {"Kit": [{"item": "the journeys in journeys.ts", "done": False}]}}
        with open(transcript, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(tool_use(800, new_tool_id(), "Write",
                                             {"file_path": path, "content": json.dumps(first)})) + "\n")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(first, handle)
        saved_root, saved_workspace = gate.session_home.CHECKOUT_ROOT, gate.WORKSPACE
        gate.session_home.CHECKOUT_ROOT = tools_root
        gate.WORKSPACE = workspace_for(transcript) if workspace_exists else os.path.join(fixture.root, "no-workspace")
        try:
            return gate.work_product_edit_problem(path, {"content": json.dumps({"repos": repos})}, transcript)
        finally:
            gate.session_home.CHECKOUT_ROOT, gate.WORKSPACE = saved_root, saved_workspace

    def freshness(steps, landed=(), break_replay=False, duplicate=False, redirty=()):
        """A real App repo with journeys.ts, port.ts and host.ts dirty (any name in `landed` committed instead), a
        first declaration 800 s ago, then `steps`: ("say", seconds_ago, words) for an operator turn and
        ("declare", seconds_ago, items) for an Edit of the declaration replacing its whole text, each item a dict.
        With break_replay, the last Edit's old_string is text the file never held, as if a script had changed it.
        With duplicate, every Edit is echoed again later by an api-request-blob record under its one tool_use id.
        Each name in `redirty` is written again after the landing commit, as a later item's work would.
        Returns the gate-time work_product_problem."""
        fixture = Fixture()
        fixtures.append(fixture)
        transcript = fixture.transcript([human_turn(900, "finish the journeys in journeys.ts")])
        workspace = workspace_for(transcript)
        repo = os.path.join(workspace, "App")
        os.makedirs(repo, exist_ok=True)
        quiet = {"capture_output": True, "text": True, "check": True}
        for args in (["init", "-q"], ["config", "core.fsmonitor", "false"], ["config", "user.email", "f@x"],
                     ["config", "user.name", "f"]):
            subprocess.run(["git", "-C", repo] + args, **quiet)
        for name in ("journeys.ts", "port.ts", "host.ts"):
            with open(os.path.join(repo, name), "w", encoding="utf-8") as handle:
                handle.write("export const v = 0;\n")
        subprocess.run(["git", "-C", repo, "add", "--all"], **quiet)
        base_date = "{0} +0000".format(int(time.time()) - 1000)
        before_declaration = dict(os.environ, GIT_AUTHOR_DATE=base_date, GIT_COMMITTER_DATE=base_date)
        subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "base"], env=before_declaration, **quiet)
        for name in ("journeys.ts", "port.ts", "host.ts"):
            with open(os.path.join(repo, name), "w", encoding="utf-8") as handle:
                handle.write("export const v = 1;\n")
        if landed:
            subprocess.run(["git", "-C", repo, "add"] + list(landed), **quiet)
            subprocess.run(["git", "-C", repo, "commit", "-q", "-m", "landed"], **quiet)
        for name in redirty:
            with open(os.path.join(repo, name), "w", encoding="utf-8") as handle:
                handle.write("export const v = 2;\n")
        path = gate.session_home.declaration_path(workspace, gate.session_stem(transcript))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        text = json.dumps({"repos": {"App": [{"item": "the journeys in journeys.ts", "done": True}]}})
        records = [tool_use(800, new_tool_id(), "Write", {"file_path": path, "content": text})]
        declares = [step for step in steps if step[0] == "declare"]
        for step in steps:
            if step[0] == "say":
                records.append(human_turn(step[1], step[2]))
                continue
            new = json.dumps({"repos": {"App": step[2]}})
            old = "a text the file never held" if break_replay and step is declares[-1] else text
            records.append(tool_use(step[1], new_tool_id(), "Edit", {"file_path": path, "old_string": old,
                                                                      "new_string": new}))
            if len(step) > 3:
                records[-1]["message"]["id"] = step[3]
            if duplicate:
                records.append({"type": "api-request-blob", "hash": new_tool_id(), "sessionId": "s",
                                "message": {"role": "assistant", "content": records[-1]["message"]["content"]}})
            text = new
        with open(transcript, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        saved = gate.WORKSPACE
        gate.WORKSPACE = workspace
        try:
            return gate.work_product_problem(transcript, workspace, temp_root_for(transcript))
        finally:
            gate.WORKSPACE = saved

    journeys_ts = {"item": "the journeys in journeys.ts", "done": True}
    port_item = {"item": "move the port in port.ts", "done": True, "approval": "please also move the port"}
    host_reusing = {"item": "move the host in host.ts", "done": True, "approval": "please also move the port"}
    host_fresh = {"item": "move the host in host.ts", "done": True, "approval": "and the host as well"}
    port_said = ("say", 700, "please also move the port in port.ts")
    both_said = ("say", 700, "please also move the port and the host")
    port_both = {"item": "move the port in port.ts", "done": True, "approval": "please also move the port and the host"}
    host_both = {"item": "move the host in host.ts", "done": True, "approval": "please also move the port and the host"}
    port_declared = ("declare", 650, [journeys_ts, port_item])
    kit_done = {"item": "the journeys in journeys.ts", "done": True}
    kit_added = {"item": "move the port in port.ts", "done": False}
    journeys_done = {"item": "the journeys", "done": True}
    journeys_open = {"item": "the journeys", "done": False}
    port_open = {"item": "move the port in port.ts", "done": False}
    return [
        ("must-block: an added item reusing an older approval, introduced by a later change, is refused while dirty",
         lambda: added_unapproved((freshness([port_said, port_declared,
                                              ("declare", 500, [journeys_ts, port_item, host_reusing])]) or ("", ""))[1])),
        ("freshness control: an item added right after the operator's words in the next change passes",
         lambda: freshness([port_said, port_declared, ("say", 600, "and the host as well, please"),
                            ("declare", 500, [journeys_ts, port_item, host_fresh])]) is None),
        ("freshness control: two items introduced in one change may share the operator's words",
         lambda: freshness([port_said, ("declare", 650, [journeys_ts, port_item, host_reusing])]) is None),
        ("freshness must-pass: two items added by separate Edits of one assistant message share the operator's words",
         lambda: freshness([both_said, ("declare", 650, [journeys_ts, port_both], "msg_batch"),
                            ("declare", 648, [journeys_ts, port_both, host_both], "msg_batch")]) is None),
        ("must-block: the same two Edits in separate assistant messages refuse the second's reused words",
         lambda: added_unapproved((freshness([both_said, ("declare", 650, [journeys_ts, port_both], "msg_first"),
                                              ("declare", 648, [journeys_ts, port_both, host_both], "msg_second")])
                                   or ("", ""))[1])),
        ("freshness control: a landed item (its file committed) is exempt even with a reused older approval",
         lambda: freshness([port_said, port_declared, ("declare", 500, [journeys_ts, port_item, host_reusing])],
                           landed=("host.ts",)) is None),
        ("freshness control: a landed item whose file a later item dirties again is still exempt",
         lambda: freshness([port_said, port_declared, ("declare", 500, [journeys_ts, port_item, host_reusing])],
                           landed=("host.ts",), redirty=("host.ts",)) is None),
        ("must-block: a commit of another file after the item appeared does not land it",
         lambda: added_unapproved((freshness([port_said, port_declared,
                                              ("declare", 500, [journeys_ts, port_item, host_reusing])],
                                             landed=("journeys.ts",)) or ("", ""))[1])),
        ("freshness control: a change flipping done alone does not move the floor past the operator's words",
         lambda: freshness([port_said, ("declare", 680, [dict(journeys_ts, done=False)]),
                            ("declare", 650, [journeys_ts]), ("declare", 600, [journeys_ts, port_item])]) is None),
        ("must-block: a reused older approval is refused when an api-request-blob echoes each Edit",
         lambda: added_unapproved((freshness([port_said, port_declared,
                                              ("declare", 500, [journeys_ts, port_item, host_reusing])],
                                             duplicate=True) or ("", ""))[1])),
        ("freshness control: an item whose introduction the transcript cannot replay is judged by every message",
         lambda: freshness([port_said, port_declared], break_replay=True) is None),
        ("must-block: an unreplayable introduction quoting words the operator never said is still refused",
         lambda: added_unapproved((freshness([port_said, ("declare", 650, [
             journeys_ts, dict(port_item, approval="nobody ever typed these words")])],
             break_replay=True) or ("", ""))[1])),
        ("must-block: an unapproved item added to a declaration under the Tools checkout is denied",
         lambda: added_unapproved(tools_home_edit({"Kit": [kit_done, kit_added]}))),
        ("tools-home control: the same item quoting the operator verbatim under the Tools checkout passes",
         lambda: tools_home_edit({"Kit": [kit_done, dict(kit_added, approval="please also move the port")]},
                                 "please also move the port in port.ts") is None),
        ("standalone: the Tools checkout's own name is a repo even with no workspace on the machine",
         lambda: tools_home_edit({"Kit": [kit_done]}, workspace_exists=False) is None),
        ("standalone control: a name that is neither the Tools checkout nor a workspace repo is still a stray",
         lambda: names_a_stray(tools_home_edit({"Kit": [kit_done], "Elsewhere": [kit_done]},
                                               workspace_exists=False), "Elsewhere")),
        ("must-block: a first declaration whose item names no file is denied when it is written",
         lambda: names_no_file(first_items("the journeys"))),
        ("must-block: an item naming only a version, never a file, is denied when it is written",
         lambda: names_no_file(first_items("Bump the SDK to 10.0.13"))),
        ("item-file control: items naming App.tsx, Login.cshtml and .gitignore are written",
         lambda: first_items("Fix App.tsx", "Fix Pages/Account/Login.cshtml", "Ignore the cache in .gitignore") is None),
        ("must-block: an Edit adding an item naming no file is denied at the edit, before any approval is read",
         lambda: names_no_file(edit_time([journeys_done, {"item": "the port", "done": False,
                                                          "approval": "also move the port"}], "also move the port please"))),
        ("must-block: a bare Move-Item onto a declaration is denied",
         lambda: "only Edit or Write may change" in (declaration_command(
             'Move-Item -LiteralPath "{elsewhere}" -Destination "{declaration}" -Force') or "")),
        ("must-block: a bare Copy-Item onto a declaration is denied",
         lambda: "only Edit or Write may change" in (declaration_command(
             'Copy-Item -LiteralPath "{elsewhere}" -Destination "{declaration}"') or "")),
        ("must-block: a bare Set-Content into a declaration is denied",
         lambda: "only Edit or Write may change" in (declaration_command("Set-Content -LiteralPath \"{declaration}\" -Value '{{}}'")
                                                     or "")),
        ("declaration-command control: reading a declaration is allowed",
         lambda: declaration_command('Get-Content -LiteralPath "{declaration}"') is None),
        ("declaration-command control: listing the declarations is allowed",
         lambda: declaration_command(r'Get-ChildItem -LiteralPath "C:\t\claude\work-products"') is None),
        ("declaration-command control: the folder's name as a word, not a path segment, writes no declaration",
         lambda: declaration_command('Set-Content -LiteralPath "{elsewhere}" -Value work-products') is None),
        ("newer goal: the first Write after a /goal pinned since the first declaration is that goal's declaration",
         lambda: under_newer_goal(False, [journeys_open, port_open])[0] is None),
        ("newer goal: an item that first Write added passes at the gate with no further approval",
         lambda: not added_unapproved((under_newer_goal(True, [journeys_open, port_open])[1] or ("", ""))[1])),
        ("must-block: after the newer goal's first Write, a further item citing the goal's words is denied",
         lambda: added_unapproved(under_newer_goal(True, [journeys_open, port_open, {
             "item": "address the server in server.ts", "done": False, "approval": "address the port in port.ts"}])[0])),
        ("newer-goal control: with no newer goal, an item added after the first declaration still needs approval",
         lambda: added_unapproved(edit_time([journeys_done, port_open], "finish it"))),

        ("widening: a rejected first Write of the declaration is no declaration, so the next Write is the first",
         lambda: first_write_judged(True) is None),
        ("widening control: an accepted empty first Write makes every later item an addition",
         lambda: bool(first_write_judged(False)) and "not in this session's first declaration" in first_write_judged(False)[1]),
        ("widening: an item added after the first declaration with no approval blocks the gate",
         lambda: bool(gate_time(None, "finish the journeys")) and "not in this session's first declaration"
         in gate_time(None, "finish the journeys")[1]),
        ("widening: an added item quoting the operator verbatim passes",
         lambda: gate_time("please also move the port", "please also move the port everywhere") is None),
        ("widening: a straight-apostrophe quote of words the operator typed with a curly one passes",
         lambda: gate_time("that's the port to move", "that’s the port to move everywhere") is None),
        ("widening control: an apostrophe quote of words the operator never typed still blocks",
         lambda: bool(gate_time("that's the port to keep", "that’s the port to move everywhere"))),
        ("widening: the pinned GOAL: never approves an item added after the first declaration",
         lambda: bool(gate_time("move the port everywhere", "GOAL: move the port everywhere and finish the journeys"))),
        ("widening: an added item whose approval the operator never said blocks",
         lambda: bool(gate_time("please also move the port", "finish the journeys"))),
        ("widening: a two-word approval naming the question passes when the operator said it",
         lambda: gate_time("approve q44", "approve q44") is None),
        ("widening: the question and the approval may come in either order",
         lambda: gate_time("q44 approved", "q44 approved") is None),
        ("widening: a short question approval quoted from a longer message that approves it passes",
         lambda: gate_time("q44 approved", "q44 approved, thanks") is None),
        ("must-block: a bare approved with no question id blocks",
         lambda: bool(gate_time("approved", "approved"))),
        ("must-block: a question id with no approval blocks",
         lambda: bool(gate_time("q44 yes", "q44 yes"))),
        ("must-block: a negated question approval blocks",
         lambda: bool(gate_time("approved q44", "not approved q44"))),
        ("must-block: a question approval the operator never said blocks",
         lambda: bool(gate_time("approve q44", "approve q43"))),
        ("must-block: a question approval matching only inside a longer id blocks",
         lambda: bool(gate_time("approve q4", "approve q44"))),
        ("widening: an Edit adding an unapproved item is denied at the edit",
         lambda: added_unapproved(edit_time([journeys_done, {"item": "move the port everywhere in port.ts", "done": False}],
                                            "finish it"))),
        ("widening: an Edit rewording an item is denied at the edit",
         lambda: added_unapproved(edit_time([{"item": "the journeys in journeys.ts, abridged", "done": False}], "finish it"))),
        ("widening control: an Edit flipping done only is allowed",
         lambda: edit_time([journeys_done], "finish it") is None),
        ("widening control: an Edit adding an item with the operator's words is allowed",
         lambda: edit_time([journeys_done, {"item": "port.ts", "done": False, "approval": "also move the port"}],
                           "also move the port please") is None),
        ("must-block: a declaration key naming no folder (a topic) is denied at the edit",
         lambda: names_a_stray(keyed_edit({"App": [journeys_done], "App-branch-rule": [journeys_done]}),
                               "App-branch-rule")),
        ("must-block: a declaration key naming a worktree folder, whose .git is a file, is denied at the edit",
         lambda: names_a_stray(keyed_edit({"App": [journeys_done], "App-branch": [journeys_done]}), "App-branch")),
        ("must-block: a declaration key for an edit outside every repo (global) is denied at the edit",
         lambda: names_a_stray(keyed_edit({"App": [journeys_done], "global": [journeys_done]}), "global")),
        ("must-block: a declaration key that is a path, not a folder name, is denied at the edit",
         lambda: names_a_stray(keyed_edit({os.path.join("App", "Hooks"): [journeys_done]}), "Hooks")),
        ("must-block: the first declaration's Write is denied when it names a key that is no repo",
         lambda: names_a_stray(keyed_edit({"global": [journeys_done]}, first_write=True), "global")),
        ("repo-key control: the first declaration's Write naming a real repo is allowed",
         lambda: keyed_edit({"App": [{"item": "the journeys in journeys.ts", "done": False}]}, first_write=True) is None),
        ("repo-key control: a stray key under dropped, not repos, is allowed, so an old one can still be dropped",
         lambda: keyed_edit({"App": [journeys_done]},
                            dropped={"global": [{"item": "the old note", "approval": "drop it"}]}) is None),
        ("docs-key control: the AGENTS key holding edits to AGENTS.md and AGENTS/*.md is allowed at the first Write",
         lambda: keyed_edit({"App": [{"item": "the journeys in journeys.ts", "done": False}], "AGENTS": [
             {"item": "point the index rows at Tools: AGENTS.md", "done": False},
             {"item": "repoint the guide: AGENTS/REPOS/Identity.md", "done": False}]}, first_write=True) is None),
        ("must-block: an AGENTS item naming a repo file is denied at the edit",
         lambda: docs_key_refused(keyed_edit({"AGENTS": [{"item": "fix App/journeys.ts", "done": False}]},
                                             first_write=True))),
        ("must-block: an AGENTS item naming a file outside the AGENTS folder by a parent step is denied",
         lambda: docs_key_refused(keyed_edit({"AGENTS": [{"item": "edit AGENTS/../CLAUDE.md", "done": False}]},
                                             first_write=True))),
        ("must-block: an AGENTS item naming a non-markdown file in the AGENTS folder is denied",
         lambda: docs_key_refused(keyed_edit({"AGENTS": [{"item": "edit AGENTS/notes.json", "done": False}]},
                                             first_write=True))),
        ("docs-key control: a lowercase spelling of an AGENTS doc is the same doc, allowed at the first Write",
         lambda: keyed_edit({"App": [{"item": "the journeys in journeys.ts", "done": False}], "AGENTS": [
             {"item": "repoint the guide: agents/repos/identity.md", "done": False}]}, first_write=True) is None),
        ("must-block: an AGENTS item naming no file is denied",
         lambda: docs_key_refused(keyed_edit({"AGENTS": [{"item": "tidy the fleet docs", "done": False}]},
                                             first_write=True))),
    ]


def build_edit_cases(fixtures):
    """An item is an edit: at gate time each item in a git repo must name a file that is uncommitted or that a
    commit since the declaration's first Write changed. Each builder returns True when the hook decides as the row
    expects."""

    disguised = ("Bring the triage agent live end to end once the Tools batch is on origin: Install-AlertTriage.ps1 "
                 "deploys the committed snapshot and re-points the three tasks off the workspace, its probe passes, "
                 "a poll -DryRun from the snapshot, the permission matrix, a triage verification run that completes "
                 "with a verdict, then enable the AlertTriage - Poll task")

    def judged(items, prepare, written_as=None):
        fixture = Fixture()
        fixtures.append(fixture)
        transcript = fixture.transcript([human_turn(900, "finish it")])
        workspace = workspace_for(transcript)
        repo = os.path.join(workspace, "App")
        os.makedirs(repo, exist_ok=True)
        git = ["git", "-C", repo, "-c", "core.fsmonitor=false", "-c", "user.name=harness", "-c", "user.email=harness@example.invalid"]
        subprocess.run(git + ["init", "--quiet"], check=True, capture_output=True)
        subprocess.run(git + ["config", "core.fsmonitor", "false"], check=True, capture_output=True)
        for name in ("old.ps1", "Install-AlertTriage.ps1"):
            with open(os.path.join(repo, name), "w", encoding="utf-8") as handle:
                handle.write("one\n")
        subprocess.run(git + ["add", "--all"], check=True, capture_output=True)
        subprocess.run(git + ["commit", "--quiet", "--message", "base"], check=True, capture_output=True,
                       env=dict(os.environ, GIT_COMMITTER_DATE="2000-01-01T00:00:00Z", GIT_AUTHOR_DATE="2000-01-01T00:00:00Z"))
        prepare(repo, git)
        path = gate.session_home.declaration_path(workspace, gate.session_stem(transcript))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        declaration = {"repos": {"App": [{"item": item, "done": True} for item in items]}}
        with open(transcript, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(tool_use(800, new_tool_id(), "Write", {
                "file_path": written_as(path) if written_as else path, "content": json.dumps(declaration)})) + "\n")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(declaration, handle)
        return gate.work_product_problem(transcript, workspace, temp_root_for(transcript), repos=["App"])

    def other_profile(path):
        return os.path.join("C:\\", "Users", "another-profile", "AppData", "Local", "Temp", "claude", "work-products",
                            os.path.basename(path))

    def other_session(path):
        return os.path.join(os.path.dirname(path), "11111111-1111-4111-8111-111111111111.json")

    def untouched(repo, git):
        pass

    def dirty(repo, git):
        with open(os.path.join(repo, "Install-AlertTriage.ps1"), "w", encoding="utf-8") as handle:
            handle.write("two\n")

    def committed_now(repo, git):
        dirty(repo, git)
        subprocess.run(git + ["commit", "--quiet", "--all", "--message", "fix"], check=True, capture_output=True)

    def dirty_in_worktree(repo, git):
        worktree = repo + "-branch"
        subprocess.run(git + ["worktree", "add", "--quiet", "-b", "branch", worktree], check=True, capture_output=True)
        subprocess.run(["git", "-C", worktree, "config", "core.fsmonitor", "false"], check=True, capture_output=True)
        with open(os.path.join(worktree, "Install-AlertTriage.ps1"), "w", encoding="utf-8") as handle:
            handle.write("two\n")

    def refused(result):
        return bool(result) and "name no file that changed" in result[1]

    def docs_judged(items, wrote, done=True, repos=None, deleted_after_seen=False):
        """The workspace's AGENTS/GATES.md exists; AGENTS/GATSE.md never does. With `wrote`, the session's transcript
        holds a landed Edit of AGENTS/GATES.md. With `deleted_after_seen`, AGENTS/TOOLING.md exists for a first
        judgment and is deleted before the second, whose result is returned. The App repo has one done item naming
        an uncommitted file."""
        fixture = Fixture()
        fixtures.append(fixture)
        transcript = fixture.transcript([human_turn(900, "finish it")])
        workspace = workspace_for(transcript)
        repo = os.path.join(workspace, "App")
        os.makedirs(repo, exist_ok=True)
        git = ["git", "-C", repo, "-c", "core.fsmonitor=false", "-c", "user.name=harness", "-c", "user.email=harness@example.invalid"]
        subprocess.run(git + ["init", "--quiet"], check=True, capture_output=True)
        subprocess.run(git + ["config", "core.fsmonitor", "false"], check=True, capture_output=True)
        with open(os.path.join(repo, "journeys.ts"), "w", encoding="utf-8") as handle:
            handle.write("one\n")
        doc = gate.session_home.workspace_doc("AGENTS/GATES.md", workspace, must_exist=False)
        os.makedirs(os.path.dirname(doc), exist_ok=True)
        with open(doc, "w", encoding="utf-8") as handle:
            handle.write("# Gates\n")
        path = gate.session_home.declaration_path(workspace, gate.session_stem(transcript))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        declaration = {"repos": {"App": [{"item": "the journeys in journeys.ts", "done": True}],
                                 "AGENTS": [{"item": item, "done": done} for item in items]}}
        records = [tool_use(800, new_tool_id(), "Write", {"file_path": path, "content": json.dumps(declaration)})]
        if wrote:
            edit_id = new_tool_id()
            records.append(tool_use(700, edit_id, "Edit", {"file_path": doc, "old_string": "# Gates", "new_string": "# Gates."}))
            records.append({"type": "user", "timestamp": iso(699), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": edit_id, "content": "The file has been updated successfully."}]}})
        with open(transcript, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(declaration, handle)
        if deleted_after_seen:
            removed = gate.session_home.workspace_doc("AGENTS/TOOLING.md", workspace, must_exist=False)
            with open(removed, "w", encoding="utf-8") as handle:
                handle.write("# Tooling\n")
            gate.work_product_problem(transcript, workspace, temp_root_for(transcript), repos=repos)
            os.remove(removed)
        return gate.work_product_problem(transcript, workspace, temp_root_for(transcript), repos=repos)

    def open_refused(result):
        return bool(result) and result[0] == "AGENTS" and "name no file that changed" not in result[1]

    return [
        ("edit: must-block the outcome item that once slipped through as a synonym for a gate run",
         lambda: refused(judged([disguised], untouched))),
        ("edit: must-block an item naming only a file this session never changed",
         lambda: refused(judged(["Tidy old.ps1"], untouched))),
        ("edit: an item naming an uncommitted file by its name passes",
         lambda: judged(["Shorten the description in Install-AlertTriage.ps1"], dirty) is None),
        ("edit: an item naming a file committed after the declaration passes",
         lambda: judged(["Shorten the description in Install-AlertTriage.ps1"], committed_now) is None),
        ("edit: an item naming a file edited only in a linked worktree of the repo passes",
         lambda: judged(["Shorten the description in Install-AlertTriage.ps1"], dirty_in_worktree) is None),
        ("edit control: the outcome item beside a real edit still blocks, naming only itself",
         lambda: refused(judged(["Shorten the description in Install-AlertTriage.ps1", "the permission matrix passes"], dirty))),
        ("edit: a file committed after a declaration first written on another machine's profile path passes",
         lambda: judged(["Shorten the description in Install-AlertTriage.ps1"], committed_now, other_profile) is None),
        ("edit: must-block an untouched file under a declaration first written on another machine's profile path",
         lambda: refused(judged(["Tidy old.ps1"], untouched, other_profile))),
        ("edit: must-block a committed file when the only Write recorded is another session's declaration",
         lambda: refused(judged(["Shorten the description in Install-AlertTriage.ps1"], committed_now, other_session))),
        ("docs edit: must-block a done AGENTS item naming a fleet doc this session never wrote",
         lambda: refused(docs_judged(["Repoint the rows in AGENTS/GATES.md"], wrote=False))),
        ("docs edit: a done AGENTS item naming a fleet doc this session wrote passes",
         lambda: docs_judged(["Repoint the rows in AGENTS/GATES.md"], wrote=True) is None),
        ("docs edit: a done AGENTS item naming a fleet doc judged present and deleted since (a deletion) passes",
         lambda: docs_judged(["Delete AGENTS/TOOLING.md"], wrote=False, deleted_after_seen=True) is None),
        ("docs edit: must-block a done AGENTS item naming a fleet doc that never existed (a typo)",
         lambda: refused(docs_judged(["Delete AGENTS/GATSE.md"], wrote=False))),
        ("docs edit control: an open AGENTS item blocks a DONE judgment as open, not as no edit",
         lambda: open_refused(docs_judged(["Repoint the rows in AGENTS/GATES.md"], wrote=False, done=False))),
        ("docs edit control: an open AGENTS item does not block a repo's own gate",
         lambda: docs_judged(["Repoint the rows in AGENTS/GATES.md"], wrote=False, done=False, repos=["App"]) is None),
    ]


def build_summary_cases(fixtures):
    """A work-product denial names the work without reprinting the declaration. Each builder returns True."""

    def open_items(count):
        fixture = Fixture()
        fixtures.append(fixture)
        path = fixture.transcript([])
        items = ["open deliverable number {0} ".format(index) + "x" * 150 for index in range(count)]
        declare(path, {"App": [(item, False) for item in items]})
        _, reason = gate.work_product_problem(path, workspace_for(path), temp_root_for(path), repos=["App"])
        return items, reason

    def many_items_summarized():
        items, reason = open_items(9)
        return ("9 open item(s)" in reason and "open deliverable number 0" in reason
                and "open deliverable number 8" not in reason and "and 7 more in" in reason
                and len(reason) < 800)

    def few_items_quoted_whole():
        items, reason = open_items(1)
        return "1 open item(s)" in reason and items[0][:gate.ITEM_CLIP - 3] in reason and "more in" not in reason

    def swapped_under_restored_metadata():
        fixture = Fixture()
        fixtures.append(fixture)
        harmless = "Write-Output 'just a status line'  \n"
        gate_step = "npm run lint; Write-Output 'go'\n"
        gate_step = gate_step + " " * (len(harmless) - len(gate_step))
        path = fixture.write_file("swap.ps1", [harmless.rstrip("\n")])
        before = os.stat(path)
        first = gate.classify('pwsh -File "{0}"'.format(path))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(gate_step)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = os.stat(path)
        second = gate.classify('pwsh -File "{0}"'.format(path))
        return (after.st_size == before.st_size and after.st_mtime_ns == before.st_mtime_ns
                and first is None and second == gate.SCRIPT)

    return [
        ("classify: a script swapped under restored mtime and size is judged by its new content",
         swapped_under_restored_metadata),
        ("summary: nine open items are summarized, not reprinted", many_items_summarized),
        ("summary: one open item is named, with nothing elided", few_items_quoted_whole),
    ]


def build_until_cases(fixtures):
    """An "until" link holds an item open while the item it quotes is open, and a link quoting no declared item is
    refused. Each builder returns True when work_product_problem decides as the row expects."""
    probe = "q100 Redis diagnosis to completion, then removal of the temporary telemetry"
    telemetry = "temporary lifecycle telemetry in instrumentation.mjs"

    def judged(entries):
        fixture = Fixture()
        fixtures.append(fixture)
        path = fixture.transcript([])
        directory = gate.session_home.work_products_dir()
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, gate.session_stem(path) + ".json"), "w", encoding="utf-8") as handle:
            json.dump({"repos": {"App": entries}}, handle)
        return gate.work_product_problem(path, workspace_for(path), temp_root_for(path), repos=["App"])

    def unresolved(problem):
        return problem is not None and "names no other item" in problem[1]

    def held_open(problem, item):
        return problem is not None and "open item(s)" in problem[1] and item[:gate.ITEM_CLIP - 3] in problem[1]

    return [
        ("until: must-block a link quoting no declared item (the investigation was never declared)",
         lambda: unresolved(judged([{"item": telemetry, "done": True, "until": "Redis diagnosis to completion"}]))),
        ("until: must-block a link quoting fewer than four words, even of a declared item",
         lambda: unresolved(judged([{"item": probe, "done": False},
                                    {"item": telemetry, "done": True, "until": "Redis diagnosis"}]))),
        ("until: must-block a link quoting only the item itself",
         lambda: unresolved(judged([{"item": telemetry, "done": True, "until": "temporary lifecycle telemetry in"}]))),
        ("until: must-block a done item whose investigation is open, named among the open items",
         lambda: held_open(judged([{"item": telemetry, "done": True, "until": "Redis diagnosis to completion"},
                                   {"item": probe, "done": False}]), telemetry)),
        ("until: must-block a done item held through a chain of links",
         lambda: held_open(judged([{"item": "rollback note in README.md", "done": True,
                                    "until": "temporary lifecycle telemetry in"},
                                   {"item": telemetry, "done": True, "until": "Redis diagnosis to completion"},
                                   {"item": probe, "done": False}]), "rollback note in README.md")),
        ("until: must-block a done item with a trailing space whose investigation is open",
         lambda: held_open(judged([{"item": telemetry + " ", "done": True, "until": "Redis diagnosis to completion"},
                                   {"item": probe, "done": False}]), telemetry)),
        ("until control: a done item whose investigation is done closes",
         lambda: judged([{"item": telemetry, "done": True, "until": "Redis diagnosis to completion"},
                         {"item": probe, "done": True}]) is None),
        ("until control: the same done items with no link close",
         lambda: judged([{"item": telemetry, "done": True}, {"item": probe, "done": True}]) is None),
    ]


def build_read_only_cases(fixtures):
    """A declaration with no items is a read-only session's and is closed only while the session edited no
    workspace repo. Each builder returns True when work_product_problem decides as the row expects."""

    def session(records, worktree_of=None):
        fixture = Fixture()
        fixtures.append(fixture)
        transcript = fixture.transcript([human_turn(900, "review the thing")])
        workspace = workspace_for(transcript)
        main = os.path.join(workspace, "App")
        registry = os.path.join(main, ".git", "worktrees", "App")
        os.makedirs(registry)
        worktree = os.path.join(fixture.root, "worktrees", "sid", "App")
        if worktree_of:
            os.makedirs(worktree)
            with open(os.path.join(registry, "gitdir"), "w", encoding="utf-8") as handle:
                handle.write(os.path.join(worktree, ".git") + "\n")
        with open(transcript, "a", encoding="utf-8") as handle:
            for record in records(workspace, worktree):
                handle.write(json.dumps(record) + "\n")
        declare(transcript, {})
        return gate.work_product_problem(transcript, workspace, temp_root_for(transcript))

    def rejected(seconds_ago, tool_id):
        return {"type": "user", "timestamp": iso(seconds_ago), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "is_error": True,
             "content": "The user doesn't want to proceed with this tool use."}]}}

    def names_app_and_file(problem, file_name):
        return problem is not None and problem[0] == "App" and file_name in problem[1] and "declares no work" in problem[1]

    def main_checkout_edit_refused():
        problem = session(lambda ws, wt: [edit(800, os.path.join(ws, "App", "src", "a.cs"))])
        return names_app_and_file(problem, "a.cs")

    def worktree_edit_refused():
        problem = session(lambda ws, wt: [edit(800, os.path.join(wt, "src", "b.cs"))], worktree_of="App")
        return names_app_and_file(problem, "b.cs")

    def write_tool_refused():
        problem = session(lambda ws, wt: [tool_use(800, new_tool_id(), "Write",
                                                   {"file_path": os.path.join(ws, "App", "new.md"), "content": "x"})])
        return names_app_and_file(problem, "new.md")

    def rejected_edit_allowed():
        tool_id = new_tool_id()
        problem = session(lambda ws, wt: [
            tool_use(800, tool_id, "Edit", {"file_path": os.path.join(ws, "App", "src", "a.cs"), "old_string": "a",
                                            "new_string": "b"}),
            rejected(790, tool_id)])
        return problem is None

    def outside_edit_allowed():
        problem = session(lambda ws, wt: [edit(800, os.path.join(ws, "notes", "probe.py")),
                                          edit(790, os.path.join(os.path.dirname(ws), "elsewhere", "report.html"))])
        return problem is None

    def no_edit_allowed():
        return session(lambda ws, wt: []) is None

    def shell(seconds_ago, command):
        return tool_use(seconds_ago, new_tool_id(), "PowerShell", {"command": command})

    def command_apply_refused():
        problem = session(lambda ws, wt: [shell(800, 'git -C "{0}" apply "C:\\scratch\\draft.diff"'.format(
            os.path.join(ws, "App")))])
        return problem is not None and problem[0] == "App" and "declares no work" in problem[1]

    def command_npm_ci_refused():
        problem = session(lambda ws, wt: [shell(800, 'npm ci --prefix "{0}"'.format(os.path.join(ws, "App")))])
        return problem is not None and problem[0] == "App"

    def dry_run_allowed():
        problem = session(lambda ws, wt: [shell(800, 'git -C "{0}" apply --check "C:\\scratch\\draft.diff"'.format(
            os.path.join(ws, "App"))), shell(790, 'git -C "{0}" status --short'.format(os.path.join(ws, "App")))])
        return problem is None

    def rejected_command_allowed():
        tool_id = new_tool_id()
        problem = session(lambda ws, wt: [
            tool_use(800, tool_id, "PowerShell", {"command": 'git -C "{0}" apply "d.diff"'.format(os.path.join(ws, "App"))}),
            rejected(790, tool_id)])
        return problem is None

    return [
        ("read-only: must-block a git apply in the repo refuses an empty declaration", command_apply_refused),
        ("read-only: must-block an npm ci --prefix into the repo refuses an empty declaration", command_npm_ci_refused),
        ("read-only: control, a git apply --check and a git status change nothing and leave it closed", dry_run_allowed),
        ("read-only: control, a rejected git apply changed nothing and leaves it closed", rejected_command_allowed),
        ("read-only: an edit in the repo's main checkout refuses an empty declaration, naming repo and file",
         main_checkout_edit_refused),
        ("read-only: an edit in a registered worktree of the repo refuses an empty declaration", worktree_edit_refused),
        ("read-only: a Write into the repo refuses an empty declaration", write_tool_refused),
        ("read-only: control, a rejected edit changed nothing and leaves an empty declaration closed",
         rejected_edit_allowed),
        ("read-only: control, edits outside every repo leave an empty declaration closed", outside_edit_allowed),
        ("read-only: control, no edit at all leaves an empty declaration closed", no_edit_allowed),
    ]


def build_path_cases(fixtures):
    """Workspace identity through an 8.3 short name, the form TEMP takes on the server. Each build returns True or
    False, or None when this volume makes no short names, which is reported SKIPPED, never PASS."""
    def short_name(path):
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        return buffer.value if ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer)) else path

    def worktree_fixture():
        root = tempfile.mkdtemp(prefix="gate-cadence-short-name-", dir=claude_temp.scratch_dir())
        fixtures.append(types.SimpleNamespace(root=root))
        workspace = os.path.join(root, "workspace-with-a-long-name")
        main = os.path.join(workspace, "App")
        os.makedirs(main)
        for args in (["init", "-q"], ["config", "core.fsmonitor", "false"], ["config", "user.email", "f@x"],
                     ["config", "user.name", "f"]):
            subprocess.run(["git", "-C", main] + args, check=True)
        with open(os.path.join(main, "code.cs"), "w", encoding="utf-8") as handle:
            handle.write("class A {}\n")
        subprocess.run(["git", "-C", main, "add", "--all"], check=True)
        subprocess.run(["git", "-C", main, "commit", "-q", "-m", "fixture"], check=True)
        checkout = os.path.join(root, "worktrees-with-a-long-name", "app-branch")
        subprocess.run(["git", "-C", main, "worktree", "add", "-q", "-b", "branch", checkout], check=True)
        subprocess.run(["git", "-C", checkout, "config", "core.fsmonitor", "false"], check=True)
        other = os.path.join(root, "another-workspace-with-a-long-name")
        os.makedirs(other)
        return workspace, other, 'pwsh -File "{0}"'.format(os.path.join(checkout, "gate.ps1"))

    def short_workspace_matches():
        workspace, _, command = worktree_fixture()
        short = short_name(workspace)
        if os.path.normcase(short) == os.path.normcase(workspace):
            return None
        return gate.gate_checkout(command, short) == ("App", os.path.dirname(command[len('pwsh -File "'):-1]))

    def short_other_workspace_does_not():
        _, other, command = worktree_fixture()
        short = short_name(other)
        if os.path.normcase(short) == os.path.normcase(other):
            return None
        return gate.gate_checkout(command, short) is None

    return [
        ("must-block: a worktree of a workspace given by its 8.3 short name is that repo's gate", short_workspace_matches),
        ("control: another folder's 8.3 short name never matches the worktree", short_other_workspace_does_not),
    ]


def edit_time_cases():
    """[(name, ok, detail)] for session_file_edit_times over a transcript holding two landed edits of one file, a
    rejected Edit and an Edit whose result is an error, keyed the way work_product_closure.dirty_session_edits looks
    them up."""
    root = tempfile.mkdtemp(prefix="gate-cadence-edit-times-", dir=claude_temp.scratch_dir())
    try:
        edited = os.path.join(root, "App", "code.cs")
        rejected = os.path.join(root, "App", "rejected.cs")
        failed = os.path.join(root, "App", "failed.cs")
        first_id, later_id, rejected_id, failed_id = new_tool_id(), new_tool_id(), new_tool_id(), new_tool_id()
        later_use = tool_use(100, later_id, "Write", {"file_path": edited, "content": "class A {}\n"})
        records = [
            tool_use(300, first_id, "Edit", {"file_path": edited, "old_string": "a", "new_string": "b"}),
            tool_result(299, first_id, "The file has been updated.", {}),
            later_use,
            tool_result(99, later_id, "File created successfully.", {}),
            tool_use(50, rejected_id, "Edit", {"file_path": rejected, "old_string": "a", "new_string": "b"}),
            tool_result(49, rejected_id, "The user doesn't want to proceed with this tool use.", {}),
            tool_use(40, failed_id, "Edit", {"file_path": failed, "old_string": "a", "new_string": "b"}),
            {"type": "user", "timestamp": iso(39), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": failed_id, "content": "String to replace not found",
                 "is_error": True}]}},
        ]
        transcript = os.path.join(root, "session.jsonl")
        with open(transcript, "w", encoding="utf-8") as handle:
            handle.write("".join(json.dumps(record) + "\n" for record in records))
        times = gate.session_file_edit_times(transcript)
        key = os.path.normcase(os.path.abspath(edited))
        later = datetime.fromisoformat(later_use["timestamp"].replace("Z", "+00:00")).timestamp()
        return [
            ("must-block: a file's edit time is its LATER edit, so an own edit after a landing is never excused",
             key in times and abs(times[key] - later) < 1, repr(times.get(key))),
            ("control: a rejected Edit records no edit time",
             os.path.normcase(os.path.abspath(rejected)) not in times, repr(sorted(times))),
            ("control: an Edit whose result is an error records no edit time",
             os.path.normcase(os.path.abspath(failed)) not in times, repr(sorted(times))),
        ]
    finally:
        shutil.rmtree(long_path_form(root), onexc=clear_readonly_and_retry)


def same_checkout_cases():
    """[(name, ok, detail)] for same_checkout_history over a real main checkout and a worktree of it."""
    root = tempfile.mkdtemp(prefix="gate-cadence-checkouts-", dir=claude_temp.scratch_dir())
    try:
        workspace = os.path.join(root, "workspace")
        main = os.path.join(workspace, "App")
        os.makedirs(main)
        for args in (["init", "-q"], ["config", "core.fsmonitor", "false"], ["config", "user.email", "f@x"],
                     ["config", "user.name", "f"]):
            subprocess.run(["git", "-C", main] + args, check=True)
        with open(os.path.join(main, "gate.ps1"), "w", encoding="utf-8") as handle:
            handle.write("npm test\n")
        subprocess.run(["git", "-C", main, "add", "--all"], check=True)
        subprocess.run(["git", "-C", main, "commit", "-q", "-m", "fixture"], check=True)
        branch = os.path.join(root, "worktrees", "app-branch")
        subprocess.run(["git", "-C", main, "worktree", "add", "-q", "-b", "branch", branch], check=True)
        subprocess.run(["git", "-C", branch, "config", "core.fsmonitor", "false"], check=True)
        main_gate = 'pwsh -File "{0}"'.format(os.path.join(main, "gate.ps1"))
        branch_gate = 'pwsh -File "{0}"'.format(os.path.join(branch, "gate.ps1"))
        scratch_gate = 'pwsh -File "{0}"'.format(os.path.join(root, "scratch-gate.ps1"))
        at = lambda seconds: datetime.fromtimestamp(1_800_000_000 + seconds, tz=timezone.utc).isoformat()
        stamp = lambda seconds: 1_800_000_000 + seconds

        def history(command, prior, edits):
            return gate.same_checkout_history(command, workspace, [{"command": c, "timestamp": at(s)} for c, s in prior],
                                              [(stamp(s), target) for s, target in edits])

        cases = []
        launched, _ = history(branch_gate, [(main_gate, 100)], [])
        cases.append(("must-not-ask: a first worktree gate after a main-checkout gate is not a re-run",
                      launched == [], repr(launched)))
        launched, _ = history(branch_gate, [(branch_gate, 100)], [])
        cases.append(("must-ask control: the worktree's own earlier gate is its last gate",
                      launched == [stamp(100)], repr(launched)))
        _, edits = history(branch_gate, [(branch_gate, 100)], [(200, os.path.join(branch, "code.cs"))])
        cases.append(("an edit inside the gated worktree counts", edits == [(stamp(200), os.path.join(branch, "code.cs"))],
                      repr(edits)))
        _, edits = history(branch_gate, [(branch_gate, 100)], [(200, os.path.join(main, "code.cs"))])
        cases.append(("an edit in another checkout does not count for this one", edits == [], repr(edits)))
        _, edits = history(branch_gate, [(branch_gate, 100)], [(200, "")])
        cases.append(("control: a shell edit with no path still counts", edits == [(stamp(200), "")], repr(edits)))
        launched, edits = history(scratch_gate, [(main_gate, 100)], [(200, os.path.join(main, "code.cs"))])
        cases.append(("control: a script that is no checked-in gate.ps1 keeps every gate and edit",
                      launched == [stamp(100)] and len(edits) == 1, "{0} {1}".format(launched, edits)))
        return cases
    finally:
        shutil.rmtree(long_path_form(root), onexc=clear_readonly_and_retry)


def build_index_cases(fixtures):
    """transcript_index.py, the resumable candidate-line index the readers share: what each call scans is read
    from its byte counter, never from a clock."""
    import transcript_index

    def setup(lines):
        root = tempfile.mkdtemp(prefix="gate-cadence-index-", dir=claude_temp.scratch_dir())
        fixtures.append(types.SimpleNamespace(root=root))
        path = os.path.join(root, "session.jsonl")
        with open(path, "wb") as handle:
            handle.write(b"".join(line + b"\n" for line in lines))
        return path, os.path.join(root, "cache")

    def append(path, data):
        with open(path, "ab") as handle:
            handle.write(data)

    needle = (b"NEEDLE",)
    needle_kind = transcript_index.needles_kind(transcript_index.as_groups(needle))

    def unchanged_scans_nothing():
        path, cache = setup([b'{"n":1,"x":"NEEDLE"}', b'{"n":2}'])
        first = transcript_index.candidate_lines(path, needle, cache)
        second = transcript_index.candidate_lines(path, needle, cache)
        return first == second == [b'{"n":1,"x":"NEEDLE"}\n'] and transcript_index.scanned_bytes["last"] == 0

    def appended_line_once_and_half_line_waits():
        path, cache = setup([b'{"n":1,"x":"NEEDLE"}'])
        transcript_index.candidate_lines(path, needle, cache)
        append(path, b'{"n":2,"x":"NEEDLE"}\n{"n":3,"x":"NEE')
        mid = transcript_index.candidate_lines(path, needle, cache)
        append(path, b'DLE"}\n')
        done = transcript_index.candidate_lines(path, needle, cache)
        return len(mid) == 2 and len(done) == 3 and done[2] == b'{"n":3,"x":"NEEDLE"}\n'

    def rewrite_past_head_rescans():
        head = b'{"pad":"' + b"p" * 5000 + b'"}'
        path, cache = setup([head, b'{"n":1,"x":"NEEDLE"}'])
        transcript_index.candidate_lines(path, needle, cache)
        with open(path, "wb") as handle:
            handle.write(head + b'\n{"n":9,"x":"other"}\n{"n":10,"x":"NEEDLE-new"}\n')
        lines = transcript_index.candidate_lines(path, needle, cache)
        return lines == [b'{"n":10,"x":"NEEDLE-new"}\n'] \
            and transcript_index.scanned_bytes["last"] == os.path.getsize(path)

    def tampered_offset_is_ignored():
        path, cache = setup([b'{"n":1,"x":"NEEDLE"}', b'{"n":2,"y":{"z":"NEEDLE"}}'])
        transcript_index.candidate_lines(path, needle, cache)
        index_file = transcript_index.index_file(cache, path, needle_kind)
        with open(index_file, "r", encoding="utf-8") as handle:
            index = json.load(handle)
        second_line = os.path.getsize(path) - len(b'{"n":2,"y":{"z":"NEEDLE"}}\n')
        index["lines"] = [second_line + 7]
        with open(index_file, "w", encoding="utf-8") as handle:
            json.dump(index, handle)
        return transcript_index.candidate_lines(path, needle, cache) == []

    def index_cannot_invent_a_line():
        path, cache = setup([b'{"n":1,"x":"NEEDLE"}', b'{"n":2,"x":"plain"}'])
        transcript_index.candidate_lines(path, needle, cache)
        index_file = transcript_index.index_file(cache, path, needle_kind)
        with open(index_file, "r", encoding="utf-8") as handle:
            index = json.load(handle)
        index["lines"] = [len(b'{"n":1,"x":"NEEDLE"}\n')]
        with open(index_file, "w", encoding="utf-8") as handle:
            json.dump(index, handle)
        return transcript_index.candidate_lines(path, needle, cache) == []

    def all_of_needle_needs_every_part():
        path, cache = setup([b'{"name":"Write","p":"work-products"}', b'{"name":"Write","p":"elsewhere"}',
                             b'{"name":"Read","p":"work-products"}'])
        lines = transcript_index.candidate_lines(path, ((b'"Write"', b"work-products"),), cache)
        return lines == [b'{"name":"Write","p":"work-products"}\n']

    plain_result = b'{"content":[{"tool_use_id":"toolu_a","type":"tool_result","content":"1 document"}]}'
    other_result = b'{"content":[{"tool_use_id": "toolu_b","type":"tool_result","content":"ok"}]}'
    paired_result = b'{"content":[{"tool_use_id":"toolu_c"},{"tool_use_id":"toolu_d"}]}'

    def result_found_by_id_whatever_its_text():
        path, cache = setup([b'{"n":0}', plain_result, other_result, paired_result])
        return transcript_index.result_lines(path, {"toolu_a"}, cache) == [plain_result + b"\n"] \
            and transcript_index.result_lines(path, {"toolu_b"}, cache) == [other_result + b"\n"] \
            and transcript_index.result_lines(path, {"toolu_d"}, cache) == [paired_result + b"\n"] \
            and transcript_index.result_lines(path, {"toolu_z"}, cache) == []

    def result_entry_cannot_name_another_line():
        path, cache = setup([plain_result, other_result])
        transcript_index.result_lines(path, {"toolu_a"}, cache)
        index_file = transcript_index.index_file(cache, path, transcript_index.RESULTS_KIND)
        with open(index_file, "r", encoding="utf-8") as handle:
            index = json.load(handle)
        index["lines"] = [[len(plain_result) + 1, ["toolu_a"]]]
        with open(index_file, "w", encoding="utf-8") as handle:
            json.dump(index, handle)
        return transcript_index.result_lines(path, {"toolu_a"}, cache) == []

    def chunk_boundaries_lose_no_line():
        lines = [b'{"n":%d,"x":"NEEDLE"}' % number for number in range(40)]
        path, cache = setup(lines)
        saved = transcript_index.CHUNK_BYTES
        transcript_index.CHUNK_BYTES = 7
        try:
            found = transcript_index.candidate_lines(path, needle, None)
        finally:
            transcript_index.CHUNK_BYTES = saved
        return found == [line + b"\n" for line in lines]

    return [
        ("index: an unchanged transcript scans no bytes and returns the same lines", unchanged_scans_nothing),
        ("index: an appended line is found once; a half-written line waits until complete",
         appended_line_once_and_half_line_waits),
        ("must-block index: a file rewritten past its head is scanned again from the start", rewrite_past_head_rescans),
        ("must-block index: a tampered offset into the middle of a line is ignored", tampered_offset_is_ignored),
        ("must-block index: an index pointing at a line without the needle returns nothing", index_cannot_invent_a_line),
        ("index: an all-of needle matches only a line holding every part", all_of_needle_needs_every_part),
        ("index: a tool result is found by its tool_use_id whatever its text says, one line holding two ids included",
         result_found_by_id_whatever_its_text),
        ("must-block index: a result entry pointing at a line without that id returns nothing",
         result_entry_cannot_name_another_line),
        ("index: lines spanning chunk boundaries are each found once", chunk_boundaries_lose_no_line),
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args()

    fixtures = []
    started = time.time()
    failures = 0
    ran = 0
    empty_temp = tempfile.mkdtemp(prefix="gate-cadence-temp-", dir=claude_temp.scratch_dir())
    import transcript_index
    transcript_index.cache_root_override["root"] = os.path.join(empty_temp, "transcript-index")

    def free_slot():
        os.environ["CLAUDE_CODE_TMPDIR"] = empty_temp
        os.environ.pop("CRGOLDEN_GATE_SLOT_DIRECTORY", None)

    free_slot()
    try:
        for pinned, name, build in build_cases(fixtures):
            expected, _, phrase = pinned.partition(":")
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            free_slot()
            command, transcript, tool_use_id = build()
            try:
                decision, reason = gate.evaluate(command, transcript, tool_use_id, temp_root=temp_root_for(transcript),
                                                 workspace=workspace_for(transcript))
            finally:
                release_held_locks()
            ok = decision == expected and (decision == "defer" or bool(reason)) and phrase in (reason or "")
            print("{0}  {1:<52} {2}".format("PASS" if ok else "FAIL", name, decision))
            if not ok:
                print("        expected={0} got={1}".format(pinned, decision))
                print("        reason={0}".format((reason or "")[:300]))
                failures += 1
        for name, ok, detail in same_checkout_cases() + edit_time_cases():
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            print("{0}  {1:<52} {2}".format("PASS" if ok else "FAIL", name, detail))
            if not ok:
                failures += 1
        hint_name = "inline step denial names the marker and the checked-in gate command"
        if not args.only or any(o.lower() in hint_name.lower() for o in args.only):
            ran += 1
            hint_fixture = Fixture()
            fixtures.append(hint_fixture)
            hint_transcript = hint_fixture.transcript([])
            try:
                decision, reason = gate.evaluate("python -m pytest tests -q", hint_transcript, None,
                                                 temp_root=temp_root_for(hint_transcript),
                                                 workspace=workspace_for(hint_transcript))
            finally:
                release_held_locks()
            ok = (decision == "deny" and gate.EXEMPT_MARKER in (reason or "")
                  and "\\gate.ps1\" -Goal" in (reason or "") and "Do not write a script of gate steps" in (reason or "")
                  and "session scratchpad" not in (reason or ""))
            print("{0}  {1:<52} {2}".format("PASS" if ok else "FAIL", hint_name, decision))
            if not ok:
                print("        expected=deny naming {0!r} got={1}: {2}".format(gate.EXEMPT_MARKER, decision, reason))
                failures += 1
        # Each refusal names the action that clears it, and the settle pause the wait reason permits is one
        # wait_call() lets through, while the same pause in a loop is still a wait.
        action_name = "refusals name their next action, and the permitted settle pause passes"
        if not args.only or any(o.lower() in action_name.lower() for o in args.only):
            ran += 1
            settle = "Restart-Service W3SVC\nStart-Sleep -Seconds 3\nInvoke-WebRequest http://localhost/health\n"
            looped = "while ($true) {\nStart-Sleep -Seconds 3\n}\nInvoke-WebRequest http://localhost/health\n"
            missing = [label for label, present in (
                ("clean: gate_status", "gate_status.py\" <Repo>" in gate.CLEAN_REASON),
                ("wait: settle pause", "Start-Sleep -Seconds N" in gate.WAIT_REASON),
                ("harness: gate command", '\\gate.ps1" -Goal' in gate.HARNESS_SELECTION_REASON),
                ("peer: ListAgents", "ListAgents" in gate.PEER_REASON and "SendMessage" in gate.PEER_REASON),
                ("slot: QUEUED", "QUEUED:" in gate.SLOT_REASON),
                ("declaration reasons: the approval route", all(
                    "qN-slug" in reason and "status == answered" in reason for reason in (
                        gate.EMPTY_DECLARATION_EDITED_REASON, gate.WORK_PRODUCT_REMOVED_REASON,
                        gate.WORK_PRODUCT_MISSING_REASON,
                        gate.NOT_AN_EDIT_REASON, gate.WORK_PRODUCT_ADDED_REASON))),
                ("settle passes", gate.wait_call(settle) is None),
                ("looped settle refused", gate.wait_call(looped) is not None)) if not present]
            print("{0}  {1:<52} {2}".format("PASS" if not missing else "FAIL", action_name, missing or "ok"))
            failures += 1 if missing else 0
        for expected, name, build in build_decide_cases(fixtures):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            free_slot()
            decision, reason = build()
            ok = decision == expected and (decision == "defer" or bool(reason))
            print("{0}  {1:<52} {2}".format("PASS" if ok else "FAIL", name, decision))
            failures += 0 if ok else 1
        for _, name, build in build_scan_cases(fixtures):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            ok = build()
            print("{0}  {1:<52} {2}".format("PASS" if ok else "FAIL", name, "equal" if ok else "differs"))
            failures += 0 if ok else 1
        for name, build in build_widening_cases(fixtures):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            ok = build()
            print("{0}  {1:<52} {2}".format("PASS" if ok else "FAIL", name, "as expected" if ok else "wrong decision"))
            failures += 0 if ok else 1
        for name, build in build_edit_cases(fixtures):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            ok = build()
            print("{0}  {1:<52} {2}".format("PASS" if ok else "FAIL", name, "as expected" if ok else "wrong decision"))
            failures += 0 if ok else 1
        for name, build in build_summary_cases(fixtures):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            ok = build()
            print("{0}  {1:<52} {2}".format("PASS" if ok else "FAIL", name, "as expected" if ok else "wrong text"))
            failures += 0 if ok else 1
        for name, build in build_until_cases(fixtures):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            ok = build()
            print("{0}  {1:<52} {2}".format("PASS" if ok else "FAIL", name, "as expected" if ok else "wrong decision"))
            failures += 0 if ok else 1
        for name, build in build_read_only_cases(fixtures):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            ok = build()
            print("{0}  {1:<52} {2}".format("PASS" if ok else "FAIL", name, "as expected" if ok else "wrong decision"))
            failures += 0 if ok else 1
        for name, build in build_index_cases(fixtures):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            ok = build()
            print("{0}  {1:<52} {2}".format("PASS" if ok else "FAIL", name, "as expected" if ok else "wrong lines"))
            failures += 0 if ok else 1
        for name, build in build_path_cases(fixtures):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            outcome = build()
            if outcome is None:
                print("SKIPPED  {0:<49} this volume makes no 8.3 short names".format(name))
                continue
            ran += 1
            print("{0}  {1:<52} {2}".format("PASS" if outcome else "FAIL", name, "as expected" if outcome else "wrong match"))
            failures += 0 if outcome else 1
    finally:
        for fixture in fixtures:
            for records in claude_temp.gate_records_under(fixture.root):
                shutil.rmtree(long_path_form(records), onexc=clear_readonly_and_retry)
            shutil.rmtree(long_path_form(fixture.root), onexc=clear_readonly_and_retry)
        shutil.rmtree(long_path_form(empty_temp), onexc=clear_readonly_and_retry)

    print("")
    print("{0} case(s) in {1:.2f}s".format(ran, time.time() - started))
    if ran == 0:
        print("No cases ran; refusing to report success on an empty selection.")
        return 1
    if failures == 0:
        print("All gate-cadence cases passed.")
        return 0
    print("{0} case(s) failed.".format(failures))
    return 1


if __name__ == "__main__":
    sys.exit(main())
