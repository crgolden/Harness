# GATE-STEP: Read-scope hook harness
"""Regression harness for invoke_read_scope_gate.py. Exits non-zero on any regression.

    python Harness/Hooks/test_read_scope_gate.py

In-process against evaluate(). Each must-deny row (a Read, Grep or Glob of a temp path outside %TEMP%\\claude, in
its long, 8.3 and forward-slash spellings, or of the state share by its UNC spelling) sits beside a must-allow row
for a path the allow rules reach: every file under %TEMP%\\claude, whichever session wrote it, the Stop gate's
decisions included, and the share's drive spellings. The harness runs on the default temp root, with
CLAUDE_CODE_TMPDIR cleared.
"""

import _winapi
import ctypes
import json
import os
import shutil
import sys
import tempfile
import time

os.environ.pop("CLAUDE_CODE_TMPDIR", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import invoke_read_scope_gate as gate  # noqa: E402
import session_home  # noqa: E402

WORKSPACE = session_home.workspace_root()
TEMP = gate.long_path(tempfile.gettempdir())
CLAUDE = os.path.join(TEMP, "claude")
PROFILE_TEMP = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp")
SESSION = "00000000-0000-4000-8000-00000000abcd"
OTHER_SESSION = "00000000-0000-4000-8000-00000000ef01"
OWN_FOLDER = os.path.join(CLAUDE, "fixture-project", SESSION)
OTHER_FOLDER = os.path.join(CLAUDE, "fixture-project", OTHER_SESSION)
FOREIGN_LOG = os.path.join(TEMP, "some-other-program", "Fixture", "step.log")


def short_path(path):
    """The 8.3 spelling of an existing path (C:\\Users\\ADMINI~1\\...), or the path unchanged."""
    if os.name != "nt":
        return path
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer))
    return buffer.value if 0 < length < len(buffer) else path


def cases():
    short_temp = short_path(TEMP)
    own = SESSION
    stop_gate = gate.claude_temp.stop_gate_state_root()
    return [
        ("deny", "Read of another program's file in the temp folder", ("Read", {"file_path": FOREIGN_LOG}, own)),
        ("deny", "Read through the 8.3 spelling of the temp folder",
         ("Read", {"file_path": os.path.join(short_temp, "other", "x.output")}, own)),
        ("deny", "Read through forward slashes", ("Read", {"file_path": FOREIGN_LOG.replace("\\", "/")}, own)),
        ("deny", "Grep of another program's folder in the temp folder",
         ("Grep", {"pattern": "GATE:", "path": os.path.dirname(FOREIGN_LOG)}, own)),
        ("deny", "Glob under the temp folder", ("Glob", {"pattern": "**/*.output", "path": TEMP}, own)),
        ("deny", "Grep of the temp folder itself", ("Grep", {"pattern": "x", "path": TEMP}, own)),
        ("deny", "Glob with an absolute pattern into the temp folder and no path",
         ("Glob", {"pattern": TEMP.replace("\\", "/") + "/other/**/*.output"}, own)),
        ("deny", "Glob climbing out of the workspace into the temp folder",
         ("Glob", {"pattern": os.path.relpath(TEMP, WORKSPACE).replace("\\", "/") + "/**/*.output", "path": WORKSPACE},
          own)),
        ("deny", "Read under the profile's own Temp, outside its claude folder",
         ("Read", {"file_path": os.path.join(PROFILE_TEMP, "fixture.log")}, own)),
        ("deny", "must-block: a folder whose name only starts like claude is another program's",
         ("Read", {"file_path": os.path.join(TEMP, "claude-other", "x.json")}, own)),
        ("deny", "must-block: climbing out of %TEMP%\\claude into another program's folder",
         ("Read", {"file_path": os.path.join(CLAUDE, "..", "some-other-program", "x.log")}, own)),
        ("deny", "must-block: another program's folder in a per-session Temp\\<n> under the profile's Temp",
         ("Read", {"file_path": os.path.join(PROFILE_TEMP, "7", "some-other-program", "x.log")}, own)),
        ("deny", "must-block: a claude folder two levels under the profile's Temp, which no allow rule reaches",
         ("Read", {"file_path": os.path.join(PROFILE_TEMP, "a", "b", "claude", "x.json")}, own)),
        ("defer", "must-allow: another session's scratchpad",
         ("Read", {"file_path": os.path.join(OTHER_FOLDER, "scratchpad", "notes.py")}, own)),
        ("defer", "must-allow: another session's work-product declaration",
         ("Read", {"file_path": os.path.join(CLAUDE, "work-products", OTHER_SESSION + ".json")}, own)),
        ("defer", "must-allow: another session's parking lot",
         ("Read", {"file_path": os.path.join(CLAUDE, "parking-lot", OTHER_SESSION, "state.json")}, own)),
        ("defer", "must-allow: the Stop gate's decisions log",
         ("Read", {"file_path": os.path.join(stop_gate, "decisions.log")}, own)),
        ("defer", "must-allow: a Grep of the Stop gate's decisions log",
         ("Grep", {"pattern": SESSION, "path": os.path.join(stop_gate, "decisions.log")}, own)),
        ("defer", "must-allow: the Stop gate's per-session state",
         ("Read", {"file_path": os.path.join(stop_gate, SESSION + ".json")}, own)),
        ("defer", "must-allow: a quarantine record", ("Read", {"file_path": os.path.join(
            gate.claude_temp.quarantine_root(), SESSION + ".json")}, own)),
        ("defer", "must-allow: a Grep of all of %TEMP%\\claude", ("Grep", {"pattern": "x", "path": CLAUDE}, own)),
        ("defer", "must-allow: a Glob over every session's folders",
         ("Glob", {"pattern": "**/*.output", "path": os.path.join(CLAUDE, "fixture-project")}, own)),
        ("defer", "must-allow: a file under %TEMP%\\claude with no session id named",
         ("Read", {"file_path": os.path.join(OWN_FOLDER, "scratchpad", "notes.py")}, None)),
        ("defer", "must-allow: the profile's own Temp\\claude",
         ("Read", {"file_path": os.path.join(PROFILE_TEMP, "claude", "x.json")}, own)),
        ("defer", "must-allow: a per-session Temp\\<n>\\claude under the profile's Temp",
         ("Read", {"file_path": os.path.join(PROFILE_TEMP, "7", "claude", "x.json")}, own)),
        ("defer", "control: a session reads its own scratchpad",
         ("Read", {"file_path": os.path.join(OWN_FOLDER, "scratchpad", "notes.py")}, own)),
        ("defer", "control: a session reads its own folder through forward slashes",
         ("Read", {"file_path": os.path.join(OWN_FOLDER, "scratchpad", "notes.py").replace("\\", "/")}, own)),
        ("defer", "control: a gate record under %TEMP%\\claude",
         ("Read", {"file_path": os.path.join(gate.claude_temp.gate_records_dir(WORKSPACE), "Tools.HOST.last.json")}, own)),
        ("defer", "control: Glob with a relative pattern inside the workspace",
         ("Glob", {"pattern": "**/*.md", "path": WORKSPACE}, own)),
        ("defer", "control: Grep of the workspace", ("Grep", {"pattern": "GATE:", "path": WORKSPACE}, own)),
        ("defer", "control: Glob with no path searches the working directory", ("Glob", {"pattern": "**/*.md"}, own)),
        ("defer", "control: a sibling whose name only starts like the temp folder",
         ("Read", {"file_path": os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp-other", "x.txt")}, own)),
        ("defer", "control: a shell command is not a file tool",
         ("PowerShell", {"command": "Get-Content \"" + FOREIGN_LOG + "\""}, own)),
        ("defer", "garbage input never raises", ("Read", None, own)),
    ], short_temp


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


def share_rows():
    """The state share: on an enabled machine, on the server and on a desktop, every path under the share is
    readable through the server disk and the mapped drive, whichever machine or session wrote it, and denied through
    the UNC spelling with a reason naming the spelling an allow rule matches (the server disk on the server, Z: on a
    desktop); a UNC share beside it is not judged; and a disabled machine, or an enabled machine's user outside the
    config's users, keeps the state under %TEMP%\\claude yet is still denied the UNC spelling, which no allow rule
    matches for any account."""
    rows = []
    claude_temp = gate.claude_temp
    kept = {key: os.environ.get(key) for key in ("COMPUTERNAME", "USERNAME", "CLAUDE_CODE_TMPDIR")}
    os.environ.pop("CLAUDE_CODE_TMPDIR", None)
    kept_config = getattr(claude_temp, "SHARE_CONFIG", None)
    base = tempfile.mkdtemp(prefix="read-scope-share-", dir=claude_temp.scratch_dir())

    def judge(tool, tool_input):
        try:
            return gate.evaluate(tool, tool_input, session_id=SESSION)
        except Exception as error:
            return "raised {0}: {1}".format(type(error).__name__, error), ""

    def run_as(config_path, machine, user):
        os.environ["COMPUTERNAME"] = machine
        os.environ["USERNAME"] = user
        claude_temp.SHARE_CONFIG = config_path

    enabled = share_fixture(base, [SHARE_SERVER, SHARE_DESKTOP])
    real_cut_over = claude_temp.cut_over
    real_legacy_cut_over = claude_temp.legacy_cut_over
    try:
        for machine, root in ((SHARE_SERVER, SHARE_DISK), (SHARE_DESKTOP, "Z:\\")):
            run_as(enabled, machine, "Administrator")
            share = claude_temp.share_root()
            rows.append(("share: {0}'s share root is {1}".format(machine, root),
                         share is not None and os.path.normcase(share) == os.path.normcase(root), share))
            claude_temp.cut_over = lambda folder: False
            claude_temp.legacy_cut_over = lambda folder: False
            rows.append(("must-block: on {0}, an enabled account not yet cut over keeps its state under "
                         "%TEMP%\\claude, so the enable strands nothing".format(machine),
                         os.path.normcase(gate.long_path(claude_temp.claude_root())) == os.path.normcase(CLAUDE),
                         claude_temp.claude_root()))
            claude_temp.legacy_cut_over = lambda folder: bool(folder)
            per_machine = os.path.join(root, machine, "administrator", "claude")
            rows.append(("must-block: on {0}, an account on the per-machine layout keeps reading its per-machine folder "
                         "until its own flat marker exists, so landing the flat layout moves no session's state "
                         "out from under it".format(machine),
                         os.path.normcase(claude_temp.claude_root()) == os.path.normcase(per_machine),
                         claude_temp.claude_root()))
            claude_temp.cut_over = lambda folder: bool(folder)
            rows.append(("control: on {0}, the same account once cut over to the flat layout keeps its state in the "
                         "one shared folder".format(machine),
                         os.path.normcase(claude_temp.claude_root()) == os.path.normcase(os.path.join(root, "claude")),
                         claude_temp.claude_root()))
            claude_temp.legacy_cut_over = real_legacy_cut_over
            unreachable = os.path.join(base, "unreachable-share", "cutover", machine + ".administrator")
            marked = os.path.join(base, "raw-marked-" + machine)
            unmarked = os.path.join(base, "raw-unmarked-" + machine)
            os.makedirs(marked, exist_ok=True)
            os.makedirs(unmarked, exist_ok=True)
            legacy_marked = os.path.join(base, "raw-legacy-marked-" + machine)
            os.makedirs(legacy_marked, exist_ok=True)
            with open(os.path.join(legacy_marked, claude_temp.LOCAL_CUTOVER_MARKER), "w", encoding="utf-8") as handle:
                handle.write("{}")
            rows.append(("must-block: on {0}, a local marker naming no layout vouches only for the per-machine folder, so "
                         "an unreachable share never sends a per-machine account to the flat folder".format(machine),
                         real_cut_over(unreachable, raw_root=legacy_marked) is False
                         and real_legacy_cut_over(unreachable, raw_root=legacy_marked) is True, legacy_marked))
            damaged = os.path.join(base, "raw-damaged-" + machine)
            os.makedirs(damaged, exist_ok=True)
            with open(os.path.join(damaged, claude_temp.LOCAL_CUTOVER_MARKER), "w", encoding="utf-8") as handle:
                handle.write("{not json")
            rows.append(("must-block: on {0}, a local marker that cannot be read, with the share unreachable, never "
                         "falls back to the per-machine folder, so a damaged marker forks no state".format(machine),
                         real_cut_over(unreachable, raw_root=damaged) is True
                         and real_legacy_cut_over(unreachable, raw_root=damaged) is False, damaged))
            with open(os.path.join(marked, claude_temp.LOCAL_CUTOVER_MARKER), "w", encoding="utf-8") as handle:
                json.dump({"layout": claude_temp.FLAT_LAYOUT}, handle)
            rows.append(("must-block: on {0}, a flat-cut-over account whose share is unreachable still reads as cut over "
                         "from its local marker, so it never forks a local copy of its state".format(machine),
                         real_cut_over(unreachable, raw_root=marked) is True, unreachable))
            rows.append(("control: on {0}, with no local marker and the share unreachable, the account reads as not "
                         "cut over, so a never-cut-over account keeps its local state (the dispatcher denies every tool "
                         "meanwhile)".format(machine), real_cut_over(unreachable, raw_root=unmarked) is False, unreachable))
            reachable = os.path.join(base, "reachable-share", "cutover", machine + ".administrator")
            fresh = os.path.join(base, "raw-fresh-" + machine)
            os.makedirs(reachable, exist_ok=True)
            with open(os.path.join(reachable, claude_temp.CUTOVER_MARKER), "w", encoding="utf-8") as handle:
                handle.write("{}")
            healed = real_cut_over(reachable, raw_root=fresh)
            os.remove(os.path.join(reachable, claude_temp.CUTOVER_MARKER))
            rows.append(("must-block: on {0}, a raw root made after the cutover writes its own marker the first time it "
                         "sees the share's, and still reads as cut over once the share is gone".format(machine),
                         healed is True and real_cut_over(reachable, raw_root=fresh) is True
                         and os.path.isfile(os.path.join(fresh, claude_temp.LOCAL_CUTOVER_MARKER)), fresh))
            override = os.path.join(base, "override-temp")
            os.environ["CLAUDE_CODE_TMPDIR"] = override
            rows.append(("must-block: on {0}, a CLAUDE_CODE_TMPDIR outside the account's temp folder beats the cut-over share, so a harness "
                         "fixture never touches the live share".format(machine),
                         os.path.normcase(claude_temp.claude_root()) ==
                         os.path.normcase(os.path.join(claude_temp.long_path(override), "claude")),
                         claude_temp.claude_root()))
            os.environ.pop("CLAUDE_CODE_TMPDIR", None)
            kept_temp = os.environ.get("TEMP")
            os.environ["TEMP"] = override
            rows.append(("must-block: on {0}, a TEMP redirected away from the account's own temp folder beats the "
                         "cut-over share too".format(machine),
                         os.path.normcase(claude_temp.claude_root()) ==
                         os.path.normcase(os.path.join(claude_temp.long_path(override), "claude")),
                         claude_temp.claude_root()))
            os.environ["TEMP"] = kept_temp
            state = "claude"
            stop_gate = os.path.basename(claude_temp.stop_gate_state_root())
            paths = (
                ("another session's scratchpad", "Read",
                 os.path.join(state, "fixture-project", OTHER_SESSION, "scratchpad", "notes.py")),
                ("another session's declaration", "Read", os.path.join(state, "work-products", OTHER_SESSION + ".json")),
                ("the Stop gate's decisions log", "Read", os.path.join(state, stop_gate, "decisions.log")),
                ("another machine's gate record", "Read",
                 os.path.join(state, "crgolden-gates", "records", "other-machine-c--users-crgol-tools", "Tools.OTHER-MACHINE.last.json")),
                ("a Grep of every session's state", "Grep", state),
                ("a Glob over every session's state", "Glob", state))
            for label, tool, relative in paths:
                for spelling, target in share_spellings(relative):
                    tool_input = ({"file_path": target} if tool == "Read" else
                                  {"pattern": "x", "path": target} if tool == "Grep" else
                                  {"pattern": "**/*.json", "path": target})
                    decision, reason = judge(tool, tool_input)
                    if spelling == "UNC":
                        drive = (SHARE_DISK + "\\" if machine == SHARE_SERVER else "Z:\\") + relative
                        rows.append(("must-block: on {0}, {1} through the UNC spelling names {2}".format(
                            machine, label, drive), decision == gate.DENY and drive in reason, target))
                    else:
                        rows.append(("must-allow: on {0}, {1} through {2}".format(machine, label, spelling),
                                     decision == gate.DEFER, target))
            sibling = "\\\\" + SHARE_SERVER + "\\crgolden-state-other\\" + machine + "\\claude\\x.json"
            rows.append(("control: on {0}, a UNC share beside the state share is not judged".format(machine),
                         judge("Read", {"file_path": sibling})[0] == gate.DEFER, sibling))
        claude_temp.cut_over = real_cut_over
        target = share_spellings("claude\\work-products\\" + OTHER_SESSION + ".json")[2][1]
        for name, config_path, user in (
                ("a disabled machine", share_fixture(base, []), "Administrator"),
                ("an enabled machine's user outside the config's users", enabled, "crgolden-triage")):
            run_as(config_path, SHARE_SERVER, user)
            share = claude_temp.share_root()
            rows.append(("control: for {0} there is no share root".format(name), share is None, share))
            rows.append(("control: for {0} the state stays under %TEMP%\\claude".format(name),
                         os.path.normcase(gate.long_path(claude_temp.claude_root())) == os.path.normcase(CLAUDE),
                         claude_temp.claude_root()))
            listed = user != "crgolden-triage"
            decision, reason = judge("Read", {"file_path": target})
            if listed:
                rows.append(("must-block: for {0} a UNC Read of a share path is still denied, naming {1}".format(
                    name, SHARE_DISK), decision == gate.DENY and SHARE_DISK in reason, target))
            else:
                rows.append(("must-block: for {0} a UNC Read is denied as not this account's share".format(name),
                             decision == gate.DENY and "not this account's" in reason and SHARE_DISK not in reason,
                             target))
            os.environ["COMPUTERNAME"] = SHARE_DESKTOP
            decision, reason = judge("Read", {"file_path": target})
            rows.append(("must-block: for {0} on a desktop a UNC Read is denied{1}".format(
                name, " naming the Z: spelling" if listed else " as not this account's share"),
                decision == gate.DENY and (("Z:\\" in reason) if listed else ("not this account's" in reason)),
                target))
            sibling = "\\\\" + SHARE_SERVER + "\\crgolden-state-other\\x.json"
            rows.append(("control: for {0} a UNC share beside the state share is not judged".format(name),
                         judge("Read", {"file_path": sibling})[0] == gate.DEFER, sibling))
    finally:
        claude_temp.cut_over = real_cut_over
        claude_temp.legacy_cut_over = real_legacy_cut_over
        for key, value in kept.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        claude_temp.SHARE_CONFIG = kept_config
        shutil.rmtree(base)
    return rows


MASTER_ENVIRON = {"MASTER_SESSION": "1", "COMPUTERNAME": "WIN-VL9HOKP0RAM"}
ELSEWHERE_ENVIRON = {"MASTER_SESSION": "1", "COMPUTERNAME": "DESKTOP-ELSEWHERE"}


def harness_rows():
    """(name, ok, detail) against a fixture Harness: its main checkout, a worktree of it, and a junction elsewhere that
    resolves into its Hooks."""
    base = tempfile.mkdtemp(prefix="read-scope-harness-", dir=gate.claude_temp.scratch_dir())
    harness = os.path.join(base, "Harness")
    hook = os.path.join(harness, "Hooks", "invoke_stop_gate.py")
    rogue = os.path.join(base, ".claude", "skills", "rogue")
    worktree = os.path.join(base, "source", "worktrees", SESSION, "Harness")
    tools = os.path.join(base, "Tools")
    tools_hook = os.path.join(tools, "Hooks", "invoke_stop_gate.py")
    for folder in (os.path.dirname(hook), os.path.dirname(rogue), os.path.join(worktree, "Hooks"),
                   os.path.dirname(tools_hook)):
        os.makedirs(folder, exist_ok=True)
    for path in (hook, os.path.join(worktree, "Hooks", "x.py"), tools_hook):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x\n")
    _winapi.CreateJunction(os.path.join(harness, "Hooks"), rogue)

    def judge(tool, tool_input, cwd=None, environ=None):
        return gate.evaluate(tool, tool_input, cwd, SESSION, environ=environ if environ is not None else {},
                             harness=harness)

    def denied(result):
        return result[0] == gate.DENY and "Harness" in result[1]

    def deferred(result):
        return result[0] == gate.DEFER

    try:
        return [
            ("must-block: a Read of a hook in the Harness main checkout", denied(judge("Read", {"file_path": hook})), hook),
            ("must-block: the same Read through forward slashes",
             denied(judge("Read", {"file_path": hook.replace("\\", "/")})), hook),
            ("must-block: a Grep of the Harness checkout", denied(judge("Grep", {"pattern": "x", "path": harness})), harness),
            ("must-block: a Glob under Harness", denied(judge("Glob", {"pattern": "**/*.py", "path": harness})), harness),
            ("must-block: a Glob whose pattern climbs into Harness from a sibling",
             denied(judge("Glob", {"pattern": "../Harness/**/*.py", "path": tools})), harness),
            ("must-block: a Read in a worktree of Harness",
             denied(judge("Read", {"file_path": os.path.join(worktree, "Hooks", "x.py")})), worktree),
            ("must-block: a Read through a junction elsewhere that resolves into Harness\\Hooks",
             denied(judge("Read", {"file_path": os.path.join(rogue, "invoke_stop_gate.py")})), rogue),
            ("must-block: MASTER_SESSION=1 on another host is not the master",
             denied(judge("Read", {"file_path": hook}, environ=ELSEWHERE_ENVIRON)), hook),
            ("must-block: EnterWorktree by path into a Harness worktree",
             denied(judge("EnterWorktree", {"path": worktree})), worktree),
            ("must-block: EnterWorktree creating a worktree from a working directory in Harness",
             denied(judge("EnterWorktree", {"name": "fix"}, cwd=harness)), harness),
            ("control: the master reads the hook", deferred(judge("Read", {"file_path": hook}, environ=MASTER_ENVIRON)), hook),
            ("control: the master enters a Harness worktree",
             deferred(judge("EnterWorktree", {"path": worktree}, environ=MASTER_ENVIRON)), worktree),
            ("control: EnterWorktree from a working directory outside Harness",
             deferred(judge("EnterWorktree", {"name": "fix"}, cwd=tools)), tools),
            ("control: a hook in the Tools checkout beside Harness", deferred(judge("Read", {"file_path": tools_hook})), tools_hook),
            ("control: a sibling whose name only starts like Harness",
             deferred(judge("Read", {"file_path": os.path.join(base, "HarnessArchive", "x.py")})), base),
        ]
    finally:
        gate.claude_temp.remove_fixture_tree(base)


def main():
    started = time.time()
    rows, short_temp = cases()
    failures = 0
    if short_temp == TEMP:
        print("NOTE  the temp folder has no distinct 8.3 spelling here; the 8.3 rows repeat the long ones")
    for expected, name, (tool, tool_input, session) in rows:
        try:
            decision, reason = gate.evaluate(tool, tool_input, session_id=session)
            ok = decision == expected and (decision == gate.DEFER or gate.claude_temp.raw_claude_root() in reason)
        except Exception as error:
            decision, ok = "raised {0}: {1}".format(type(error).__name__, error), False
        print("{0}  {1:<66} {2}".format("PASS" if ok else "FAIL", name, decision))
        failures += 0 if ok else 1
    extra = share_rows() + harness_rows()
    for name, ok, detail in extra:
        print("{0}  {1:<66} {2}".format("PASS" if ok else "FAIL", name, detail))
        failures += 0 if ok else 1
    print("")
    print("{0} case(s) in {1:.2f}s".format(len(rows) + len(extra), time.time() - started))
    if failures == 0:
        print("All read-scope-gate cases passed.")
        return 0
    print("{0} case(s) failed.".format(failures))
    return 1


if __name__ == "__main__":
    sys.exit(main())
