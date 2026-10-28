# GATE-STEP: PreToolUse hook harness
"""Regression harness for invoke_pretooluse_gate.py. Exits non-zero on any regression.

Runs IN-PROCESS against evaluate(). The PowerShell harness spawned a fresh gate per case at ~2.5s each.

Three decisions, all asserted: deny (output, exit 1), allow (output, exit 0), and DEFER -- no output
at all, exit 0, handing the call back to Claude Code's normal permission flow. Defer is the one a port
loses silently, because it looks like nothing happened.

This file contains git-write strings as FIXTURES, which is what the marker declares.

    PRETOOLUSE-GATE: git-write-strings-are-test-data

    python Harness/Hooks/test_pretooluse_gate.py
    python Harness/Hooks/test_pretooluse_gate.py --only git
"""

import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_temp  # noqa: E402
import invoke_pretooluse_gate as gate  # noqa: E402

MARKER = "PRETOOLUSE-GATE: git-write-strings-are-test-data"

SESSION = "0f3c2a91-5b7e-4d2a-9c1e-7a4b8d6e2f10"
OTHER_SESSION = "7d1e9b40-2c3a-4f5e-8a6b-1c2d3e4f5a6b"
PROFILE = "C:\\Users\\harness"
SESSION_PATH = gate.session_worktree_path(PROFILE, SESSION)
OTHER_SESSION_PATH = gate.session_worktree_path(PROFILE, OTHER_SESSION)


def write(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def build_fixtures(root):
    f = {}
    f["commit"] = write(os.path.join(root, "commit.ps1"),
                        ["$ErrorActionPreference = 'Stop'", 'git -C "C:\\repo" commit -m "auto"'])
    f["push"] = write(os.path.join(root, "push.ps1"),
                      ["$ErrorActionPreference = 'Stop'", "git push origin main"])
    f["dotnet"] = write(os.path.join(root, "dt.ps1"), ["dotnet test --project App.Tests"])
    f["pycommit"] = write(os.path.join(root, "commit.py"),
                          ["import subprocess",
                           'subprocess.run(["git","commit","-m","auto"])'])
    f["pyclean"] = write(os.path.join(root, "clean.py"), ["print('innocuous')"])
    # Hygiene rules must NOT reach into a body: this legitimately uses a pipe, a Set-Location and a
    # subexpression, and runs a read-only git command.
    f["clean"] = write(os.path.join(root, "clean.ps1"),
                       ["Set-Location 'C:\\repo'",
                        "$n = $(git rev-parse --abbrev-ref HEAD)",
                        "Get-ChildItem . | Select-Object -First 3",
                        "Write-Output $n"])
    body = ["$fixture = 'git -C \"C:\\repo\" commit -m x'"]
    f["marked"] = write(os.path.join(root, "marked.ps1"), ["# " + MARKER] + body)
    f["unmarked"] = write(os.path.join(root, "unmarked.ps1"), body)
    return f


def cases(f):
    return [
        # --- allow tail: single bare invocations -------------------------------------------------
        ("allow", "bare dotnet", "dotnet --version"),
        ("allow", "bare playwright-cli", "playwright-cli list"),
        ("allow", "python script file", 'python "C:\\abs\\path\\script.py"'),
        ("allow", "pwsh script file", 'pwsh -File "C:\\abs\\path\\script.ps1"'),
        ("allow", "run-code --filename", "playwright-cli --raw run-code --filename=probe.js"),
        ("allow", "Get-Content head", 'Get-Content "C:\\file.txt" -TotalCount 3'),
        ("allow", "Get-ChildItem filter", 'Get-ChildItem "C:\\dir" -Recurse -Filter *.js -Name'),
        ("allow", "Get-Date UTC stamp", "Get-Date -AsUTC -Format o"),
        ("allow", "dotnet build w/ path",
         'dotnet build "C:\\repo\\App" --configuration Release --nologo'),
        ("allow", "stderr redirect 2>&1", "dotnet --version 2>&1"),
        ("allow", "gh simple", "gh run list --limit 5"),
        ("allow", "az simple", "az account show"),
        ("allow", "quoted path w/ spaces", 'dotnet build "C:\\Program Files\\App\\App.csproj"'),
        ("allow", "ampersand inside URL", 'playwright-cli goto "https://example.com/?a=1&b=2"'),
        ("allow", "az query no pipe", 'az account list --query "[0].name"'),
        ("allow", "npx version", "npx --version"),
        ("allow", "New-Item directory", 'New-Item -ItemType Directory -Force "C:\\some\\dir"'),
        ("allow", "standalone Set-Location", 'Set-Location "C:\\repo"'),
        ("allow", "jb inspectcode",
         'jb inspectcode "C:\\repo\\App.slnx" --no-build -e=WARNING -o="C:\\out\\App-inspect.sarif"'),

        # --- git read-only fast path -------------------------------------------------------------
        ("allow", "git status", "git status --porcelain"),
        ("allow", "git status -C path", 'git -C "C:\\repo\\Identity" status --porcelain'),
        ("allow", "git log -C path", 'git -C "C:\\repo\\Curator" log --oneline -1 -- src/x.py'),
        ("allow", "git diff", "git diff --stat"),
        ("allow", "git show", "git show HEAD --name-only"),
        ("allow", "git rev-parse", "git rev-parse --abbrev-ref HEAD"),
        ("allow", "git log --grep=commit", "git log --grep=commit"),

        # The read-only allow uses -cmatch in the original, so it is CASE-SENSITIVE on purpose.
        # Upper-case git is not on the fast path and no allow prefix covers bare `git`, so it defers.
        ("defer", "GIT STATUS is case-sensitive", "GIT STATUS --porcelain"),

        # --- git flags that must not take the fast path ------------------------------------------
        ("defer", "git log --output", 'git log --output="C:\\out.txt"'),
        ("defer", "git -c pager exec", "git -c core.pager=calc.exe log"),
        ("defer", "git --exec-path", "git --exec-path=C:\\evil log"),
        ("defer", "git fetch", "git fetch origin"),
        ("defer", "git checkout", "git checkout -- ."),
        ("defer", "git branch delete", "git branch -D feature"),

        # --- defer: unlisted or redirecting ------------------------------------------------------
        ("defer", "call operator exe", '& "C:\\repo\\App.Tests.Unit.exe" -trait "Category=Unit"'),
        ("defer", "call operator ps1", '& "C:\\repo\\playwright.ps1" install chromium'),
        ("defer", "call operator no space", '&"C:\\repo\\App.exe"'),
        ("defer", "Remove-Item", 'Remove-Item "C:\\f.md" -Force'),
        ("defer", "test exe with trait", 'Identity.Tests.Unit.exe -trait "Category=Unit"'),
        ("defer", "unlisted head", "curl https://example.com"),
        ("defer", "file redirect >", 'dotnet --version > "C:\\out.txt"'),
        ("defer", "append redirect >>", 'dotnet --version >> "C:\\out.txt"'),
        ("defer", "pip install", "python -m pip install requests"),
        ("defer", "empty command", ""),

        # --- deny: the Stop gate's state directory is out of every shell's reach -----------------
        ("deny", "Set-Content into the Stop gate state",
         'Set-Content -Path "C:\\Users\\u\\AppData\\Local\\crgolden\\claude-stopgate\\s.json" -Value "{}"'),
        ("deny", "Remove-Item of the Stop gate state", 'Remove-Item "C:\\x\\CLAUDE-STOPGATE" -Recurse'),
        ("deny", "Get-Content of the decision log", 'Get-Content "C:\\x\\claude-stopgate\\decisions.log"'),
        ("defer", "control: a sibling folder that only shares a word", 'Remove-Item "C:\\x\\claude-stop"'),

        # --- deny: the hooks' transcript index is out of every shell's reach ----------------------
        ("deny", "Set-Content into the transcript index",
         'Set-Content -Path "C:\\Users\\u\\AppData\\Local\\Temp\\claude\\transcript-index\\a.json" -Value "{}"'),
        ("deny", "Remove-Item of the transcript index", 'Remove-Item "C:\\x\\TRANSCRIPT-INDEX" -Recurse'),
        ("defer", "control: a folder that only shares a word with the index", 'Remove-Item "C:\\x\\transcript"'),

        # --- deny: an allow prefix must not launder a second command -----------------------------
        ("deny", "allow prefix + semicolon", "dotnet --version; Write-Output pwned"),
        ("deny", "jb prefix + semicolon", 'jb inspectcode "C:\\repo\\App.slnx"; Write-Output pwned'),
        ("deny", "allow prefix + newline", "dotnet --version\nWrite-Output pwned"),
        ("deny", "allow prefix + subexpr", "dotnet --version $(Write-Output pwned)"),
        ("deny", "allow prefix + bare amp", "dotnet --version & Write-Output pwned"),
        ("deny", "semicolon chain", "dotnet --version; dotnet --version"),
        ("deny", "ampersand-and chain", "dotnet --version && dotnet --version"),
        ("deny", "or chain", "dotnet --version || dotnet --version"),
        ("deny", "pipeline", 'Get-ChildItem "C:\\d" | Select-Object -First 1'),
        ("deny", "Get-Date prefix + semicolon", "Get-Date -AsUTC; Remove-Item C:\\x"),
        ("deny", "inline JS payload",
         "playwright-cli --raw run-code 'async page => { const a = 1; return a; }'"),
        ("deny", "sqlpackage connstring",
         'sqlpackage /TargetConnectionString:"Server=x;Database=y"'),
        ("deny", "msbuild property list", 'dotnet build -p:NoWarn="CS1591;CS0618"'),
        ("deny", "gh --jq pipe", 'gh api user --jq ".name | ascii_downcase"'),
        ("deny", "az --query pipe", 'az account list --query "[0] | [0]"'),
        ("deny", "newline chain (CRLF)", "dotnet --version\r\nWrite-Output pwned"),
        ("deny", "backtick-escaped semi", "dotnet --version `; Write-Output pwned"),
        ("deny", "trailing background &", "dotnet --version &"),
        ("deny", "python inline -c", 'python -c "import os; print(1)"'),

        # --- deny: a doubled backslash, where Claude Code's matcher splits a quoted argument ------
        ("deny", "doubled backslash: the master's parked git grep",
         'git -C "C:\\Users\\Administrator\\source\\repos\\crgolden\\Tools" grep -n -e "def audits_root" '
         '-e "repos\\\\crgolden" -e "Modules\\\\package.json" -e "Shared\\\\Shared\\\\Shared.csproj" HEAD '
         '-- Hooks/claude_temp.py Claude/skills/fleet-audit/scripts/Get-FleetAudit.ps1 Gates/GateCommon.ps1'),
        ("deny", "doubled backslash in an allowed git log",
         'git -C "C:\\repo\\Tools" log --oneline -1 --grep="repos\\\\crgolden"'),
        ("allow", "control: the same git log with a single backslash",
         'git -C "C:\\repo\\Tools" log --oneline -1 --grep="repos\\crgolden"'),
        ("allow", "control: a single-backslash path argument", 'git -C "C:\\Users\\x\\Tools" status --porcelain'),

        # --- deny: inline interpreter code with no shell operator to trip on -------------------
        ("deny", "python -c no operator", 'python -c "print(1)"'),
        ("deny", "PYTHON upper -c", "PYTHON -c x"),
        ("deny", "python flag before -c", "python -I -c x"),
        ("deny", "python combined -Ic", "python -Ic x"),
        ("deny", "python -W value then -c", "python -W ignore -c x"),
        ("deny", "python -c glued quote", 'python -c"print(1)"'),
        ("deny", "py launcher -c", "py -c x"),
        ("deny", "node -e", 'node -e "console.log(1)"'),
        ("deny", "node --eval=", "node --eval=x"),
        ("deny", "python -c glued code", "python -cprint(1)"),
        ("deny", "pythonw -c", "pythonw -c x"),
        ("deny", "python long option value then -c", "python --check-hash-based-pycs default -c x"),
        ("deny", "python quoted -W value then -c", 'python -W "ignore: x" -c y'),
        ("deny", "python -X value shaped like a script", "python -X a.py -c x"),
        ("deny", "python unbalanced quote", 'python -c "x'),
        ("deny", "node -pe cluster", 'node -pe "1+1"'),
        ("deny", "node -r value then -e", "node -r dotenv/config -e x"),
        ("deny", "node -r value shaped like a script", "node -r ./x.js -e x"),
        ("deny", "full-path python.exe -c", '"C:\\Python\\python.exe" -c x'),
        ("deny", "node script argument -e fails safe", "node C:\\x\\s.js -e"),
        ("allow", "python glued -W value holding c", 'python -Wonce "C:\\abs\\path\\script.py"'),
        ("allow", "python quoted path with a space", 'python "C:\\abs\\my path\\script.py" -c foo'),
        ("allow", "python script arg -c", 'python "C:\\abs\\path\\script.py" -c foo'),
        ("allow", "python -m module arg -c", "python -m pytest -k c"),
        ("allow", "python -E alone", 'python -E "C:\\abs\\path\\script.py"'),
        ("allow", "python -I alone", 'python -I "C:\\abs\\path\\script.py"'),

        # --- deny: pwsh invocation form ----------------------------------------------------------
        ("deny", "pwsh -NoProfile -File", 'pwsh -NoProfile -File "C:\\abs\\path\\script.ps1"'),
        ("deny", "pwsh -NonInteractive", 'pwsh -NonInteractive -File "C:\\abs\\path\\script.ps1"'),
        ("deny", "pwsh.exe -File", 'pwsh.exe -File "C:\\abs\\path\\script.ps1"'),
        ("deny", "powershell -File (5.1)", 'powershell -File "C:\\abs\\path\\script.ps1"'),
        ("deny", "powershell.exe -File", 'powershell.exe -File "C:\\abs\\path\\script.ps1"'),
        ("deny", "pwsh -Command not -File", 'pwsh -Command "Write-Output hi"'),

        # --- deny: intent rules, every spelling --------------------------------------------------
        ("deny", "git commit", 'git commit -m "message"'),
        ("deny", "git push", "git push origin main"),
        ("deny", "git -C commit", 'git -C "C:\\repo\\Curator" commit -F msg.txt'),
        ("deny", "git -C push", 'git -C "C:\\repo\\Curator" push origin main'),
        ("deny", "git -C push -u", 'git -C "C:\\repo" push -u origin feature'),
        ("deny", "git -c override commit", "git -c user.name=x commit -m y"),
        ("deny", "git commit --amend", "git commit --amend"),
        ("deny", "git --git-dir commit", "git --git-dir=C:\\r\\.git commit -m y"),
        ("deny", "git -C bare path commit", "git -C C:\\repo commit -m y"),
        ("deny", "tokenized git commit", 'subprocess.run(["git","commit","-m","x"])'),
        ("deny", "cd chaining", "cd C:\\repo && dotnet build"),
        ("deny", "Push-Location", "Push-Location C:\\repo"),

        # dotnet test is deliberately NOT an intent rule any more: every repo's global.json opts
        # into the MTP runner, so it works from a repo root. These two pin the removal.
        ("allow", "dotnet test allowed", "dotnet test --project App.Tests"),

        # --- script-body scan: intent rules follow the payload into the file ---------------------
        ("deny", "ps1 body commits", 'pwsh -File "{0}"'.format(f["commit"])),
        ("deny", "ps1 body pushes", 'pwsh -File "{0}"'.format(f["push"])),
        ("allow", "ps1 body dotnet test allowed", 'pwsh -File "{0}"'.format(f["dotnet"])),
        ("deny", "py body commits", 'python "{0}"'.format(f["pycommit"])),
        ("deny", "py body commits behind an interpreter flag", 'python -I "{0}"'.format(f["pycommit"])),
        ("deny", "py body commits behind a flag and its value", 'python -X utf8 "{0}"'.format(f["pycommit"])),
        ("deny", "py body commits behind a glued -W value", 'python -Wonce "{0}"'.format(f["pycommit"])),
        ("deny", "py body commits behind two flags", 'python -I -B "{0}"'.format(f["pycommit"])),
        ("allow", "py body is innocuous behind an interpreter flag", 'python -I "{0}"'.format(f["pyclean"])),
        ("allow", "ps1 body is innocuous", 'pwsh -File "{0}"'.format(f["clean"])),

        # Same body, one marker apart, opposite decisions. The pair is what stops the exemption
        # becoming a blanket hole.
        ("allow", "marked as test data", 'pwsh -File "{0}"'.format(f["marked"])),
        ("deny", "same body, no marker", 'pwsh -File "{0}"'.format(f["unmarked"])),

        # --- session branch and worktree naming ----------------------------------------------------
        ("defer", "control: worktree add, session branch and path",
         'git -C "C:\\repo\\Tools" worktree add -b {0} "{1}" origin/main'.format(SESSION, SESSION_PATH)),
        ("defer", "control: worktree add of the existing session branch",
         'git -C "C:\\repo\\Tools" worktree add "{0}" {1}'.format(SESSION_PATH, SESSION)),
        ("defer", "control: checkout -b session id", "git checkout -b {0}".format(SESSION)),
        ("defer", "control: switch -c session id", "git switch -c {0}".format(SESSION)),
        ("defer", "control: switch --create=session id", "git switch --create={0}".format(SESSION)),
        ("defer", "control: checkout of a file is untouched", "git checkout -- ."),
        ("defer", "control: worktree list is untouched", 'git -C "C:\\repo\\Tools" worktree list'),
        ("deny", "must-block: worktree add, wrong branch",
         'git -C "C:\\repo\\Tools" worktree add -b tools-67/fix "{0}" origin/main'.format(SESSION_PATH)),
        ("deny", "must-block: worktree add, wrong path",
         'git -C "C:\\repo\\Tools" worktree add -b {0} "C:\\repo\\crgolden\\worktrees\\x" origin/main'.format(SESSION)),
        ("deny", "must-block: worktree add, another session's path",
         'git -C "C:\\repo\\Tools" worktree add -b {0} "{1}" origin/main'.format(SESSION, OTHER_SESSION_PATH)),
        ("deny", "must-block: worktree add, relative path with no -C",
         "git worktree add -b {0} ..\\wt origin/main".format(SESSION)),
        ("deny", "must-block: worktree add with no branch", 'git worktree add "{0}"'.format(SESSION_PATH)),
        ("deny", "must-block: worktree add --detach",
         'git worktree add --detach "{0}" origin/main'.format(SESSION_PATH)),
        ("deny", "must-block: worktree add -B wrong branch",
         'git worktree add -B feature "{0}" origin/main'.format(SESSION_PATH)),
        ("deny", "must-block: checkout -b wrong branch", "git checkout -b feature/x"),
        ("deny", "must-block: checkout -B wrong branch", 'git -C "C:\\repo\\Tools" checkout -B feature/x'),
        ("deny", "must-block: switch -c wrong branch", "git switch -c feature/x"),
        ("deny", "must-block: switch --create=wrong branch", "git switch --create=feature/x"),
        ("deny", "must-block: git.exe worktree add wrong branch",
         'git.exe worktree add -b feature "{0}"'.format(SESSION_PATH)),
    ]


MASTER_ENVIRON = {"MASTER_SESSION": "1", "COMPUTERNAME": "WIN-VL9HOKP0RAM"}
ELSEWHERE_ENVIRON = {"MASTER_SESSION": "1", "COMPUTERNAME": "DESKTOP-ELSEWHERE"}


def harness_rows(base):
    """(expected, name, command, environ) against a fixture Harness under base: the named entry scripts, a worktree of
    it at the session's path, and the master's own commands."""
    harness = os.path.join(base, "Harness")
    hook = os.path.join(harness, "Hooks", "invoke_stop_gate.py")
    harness_worktree = gate.session_worktree_path(PROFILE, SESSION, "Harness")
    worktree_add = 'git -C "{0}" worktree add -b {1} "{2}" origin/main'
    return [
        ("deny", "must-block: a script in Harness", 'python "{0}"'.format(os.path.join(harness, "Hooks", "test_stop_gate.py")), {}),
        ("deny", "must-block: Get-Content of a Harness hook", 'Get-Content "{0}"'.format(hook), {}),
        ("deny", "must-block: read-only git in Harness", 'git -C "{0}" status'.format(harness), {}),
        ("deny", "must-block: a worktree add of Harness", worktree_add.format(harness, SESSION, harness_worktree), {}),
        ("deny", "must-block: a script in a Harness worktree",
         'python "{0}"'.format(os.path.join(harness_worktree, "Hooks", "x.py")), {}),
        ("deny", "must-block: MASTER_SESSION=1 off the master host", 'Get-Content "{0}"'.format(hook), ELSEWHERE_ENVIRON),
        ("deny", "must-block: Set-Location Harness, no separator", "Set-Location Harness", {}),
        ("deny", "must-block: cd .\\Harness from the workspace", "cd .\\Harness", {}),
        ("deny", "must-block: a $env: path into Harness",
         'Get-Content "$env:HARNESS_FIXTURE\\Hooks\\invoke_stop_gate.py"', {"HARNESS_FIXTURE": harness}),
        ("deny", "must-block: a bare relative script run from a working directory in Harness",
         "python Hooks\\invoke_stop_gate.py", {"CWD": os.path.join(harness, "Hooks")}),
        ("deny", "must-block: a relative path climbing into Harness from a sibling",
         'python "..\\Harness\\Hooks\\invoke_stop_gate.py"', {"CWD": os.path.join(base, "Tools")}),
        ("allow", "control: a bare relative script run from a working directory outside Harness",
         "python Hooks\\x.py", {"CWD": os.path.join(base, "Tools")}),
        ("allow", "control: a word that only starts like Harness", "Get-ChildItem HarnessArchive -Name", {}),
        ("allow", "control: a URL is no path into Harness", "gh api https://api.github.com/repos/crgolden/Harness", {}),
        ("deny", "must-block: a Tools-named script path inside Harness",
         'pwsh -File "{0}" -SessionId a -CallerSessionId b'.format(os.path.join(harness, "Coordination", "Send-SessionPoke.ps1")), {}),
        ("allow", "must-allow: gate_status.py by name", 'python "{0}" Tools'.format(os.path.join(harness, "Gates", "gate_status.py")), {}),
        ("allow", "must-allow: session_home.py by name",
         'python "{0}" workspace'.format(os.path.join(harness, "Hooks", "session_home.py")), {}),
        ("allow", "control: the master reads a Harness hook", 'Get-Content "{0}"'.format(hook), MASTER_ENVIRON),
        ("defer", "control: the master's worktree add ends in Harness",
         worktree_add.format(harness, SESSION, harness_worktree), MASTER_ENVIRON),
        ("deny", "must-block: the master's worktree add at a Tools path", worktree_add.format(harness, SESSION, SESSION_PATH),
         MASTER_ENVIRON),
        ("allow", "control: a script in the Tools checkout beside it",
         'python "{0}"'.format(os.path.join(base, "Tools", "Hooks", "x.py")), {}),
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args()

    root = tempfile.mkdtemp(prefix="pretool-py-", dir=claude_temp.scratch_dir())
    started = time.time()
    failures = 0
    ran = 0
    try:
        for expected, name, command in cases(build_fixtures(root)):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            decision, reason, code = gate.evaluate(command, SESSION, PROFILE)

            ok = decision == expected
            # Exit code and decision must not drift apart: deny is the only non-zero, and a deny or
            # allow without a reason would reach the agent as an empty explanation.
            if decision == "deny" and code != 1:
                ok = False
            if decision in ("allow", "defer") and code != 0:
                ok = False
            if decision in ("deny", "allow") and not reason:
                ok = False

            print("{0}  {1:<34} {2}".format("PASS" if ok else "FAIL", name, decision))
            if not ok:
                print("        expected={0} got={1} code={2}".format(expected, decision, code))
                failures += 1
        name = "shell operator reason says only the working directory persists"
        if not args.only or any(o.lower() in name.lower() for o in args.only):
            ran += 1
            _, reason, _ = gate.evaluate("git status; git log", SESSION, PROFILE)
            ok = ("shell variables and functions do not" in reason and "shell state persist" not in reason
                  and "The script's body is judged too" in reason)
            print("{0}  {1:<34} {2}".format("PASS" if ok else "FAIL", name, "ok" if ok else "wrong text"))
            failures += 0 if ok else 1
        name = "doubled backslash is the reason the master's git grep is denied"
        if not args.only or any(o.lower() in name.lower() for o in args.only):
            ran += 1
            grep = ('git -C "C:\\repo\\Tools" grep -n -e "def audits_root" -e "repos{0}crgolden" HEAD '
                    '-- Hooks/claude_temp.py')
            _, doubled, _ = gate.evaluate(grep.format("\\\\"), SESSION, PROFILE)
            _, single, _ = gate.evaluate(grep.format("\\"), SESSION, PROFILE)
            ok = doubled == gate.DOUBLED_BACKSLASH_REASON and single != gate.DOUBLED_BACKSLASH_REASON
            print("{0}  {1:<34} {2}".format("PASS" if ok else "FAIL", name, "ok" if ok else "wrong reason"))
            failures += 0 if ok else 1
        # Commands other gates' refusals advise must pass this one: the gate command with a goal whose ; | & and
        # backticks are written as spaces (the controls keep them and are refused), and a squash-merged branch's
        # branch -D.
        gate_command = 'pwsh -File "C:\\ws\\Tools\\gate.ps1" -Goal '
        for expected, name, command in (
                ("allow", "advised -Goal with operators as spaces", gate_command + '"review crgolden-37  fix a   b"'),
                ("allow", "advised -Goal keeping an apostrophe", gate_command + "\"review crgolden-37's plan\""),
                ("deny", "control: -Goal keeping ; and |", gate_command + '"review crgolden-37; fix a | b"'),
                ("deny", "control: -Goal keeping a backtick", gate_command + '"review `crgolden-37`"'),
                ("defer", "advised branch -D of a merged branch", 'git -C "C:\\ws\\Tools" branch -D ' + SESSION)):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            decision, _, _ = gate.evaluate(command, SESSION, PROFILE)
            ok = decision == expected
            print("{0}  {1:<34} {2}".format("PASS" if ok else "FAIL", name, decision))
            failures += 0 if ok else 1
        for expected, name, command, environ in harness_rows(os.path.join(root, "harness-fixture")):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            cwd = environ.get("CWD", os.path.join(root, "harness-fixture"))
            decision, reason, _ = gate.evaluate(command, SESSION, PROFILE, environ=environ, cwd=cwd,
                                                harness=os.path.join(root, "harness-fixture", "Harness"))
            ok = decision == expected and (decision == "defer" or bool(reason))
            print("{0}  {1:<34} {2}".format("PASS" if ok else "FAIL", name, decision))
            failures += 0 if ok else 1
        # A worktree is removed only once the session's audit has run against it: a catalog exists, every operator
        # message but the newest has a verdict, and no verdict cites a file under the worktree.
        audits = os.path.join(root, "audits")
        folder = os.path.join(audits, SESSION)
        os.makedirs(folder)

        def audit(messages, verdicts):
            with open(os.path.join(folder, "messages.jsonl"), "w", encoding="utf-8") as fh:
                for seq, message_id in enumerate(messages, 1):
                    fh.write('{{"id": "{0}", "seq": {1}, "ts": "2026-01-01T00:0{1}:00Z", "text": "ask"}}\n'.format(
                        message_id, seq))
            with open(os.path.join(folder, "verdicts.jsonl"), "w", encoding="utf-8") as fh:
                for message_id, evidence in verdicts:
                    fh.write('{{"id": "{0}", "status": "addressed", "evidence": {1}}}\n'.format(
                        message_id, '"' + evidence.replace("\\", "\\\\") + '"'))

        remove = 'git -C "C:\\repo\\Tools" worktree remove "{0}"'.format(SESSION_PATH)
        removal_script = write(os.path.join(root, "remove.ps1"), [remove])
        cited = SESSION_PATH + "\\Hooks\\invoke_stop_gate.py:3 holds the fix"
        landed = "C:\\repo\\Tools\\Hooks\\invoke_stop_gate.py:3 holds the fix"
        sibling = SESSION_PATH + "2\\Hooks\\invoke_stop_gate.py:3 holds the fix"
        first, newest = "aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002"
        for expected, name, setup, command in (
                ("deny", "must-block: worktree remove, no audit run", None, remove),
                ("deny", "must-block: worktree remove, unrecorded message", ([first, newest], []), remove),
                ("deny", "must-block: worktree remove, verdict cites it", ([first, newest], [(first, cited)]), remove),
                ("defer", "control: verdicts cite where it landed", ([first, newest], [(first, landed)]), remove),
                ("defer", "control: a sibling path is not under it", ([first, newest], [(first, sibling)]), remove),
                ("deny", "must-block: Remove-Item under worktrees", ([first, newest], [(first, cited)]),
                 'Remove-Item -Recurse -Force "{0}"'.format(SESSION_PATH)),
                ("deny", "must-block: worktree remove in a script", ([first, newest], [(first, cited)]),
                 'pwsh -File "{0}"'.format(removal_script)),
                ("allow", "control: the script once re-recorded", ([first, newest], [(first, landed)]),
                 'pwsh -File "{0}"'.format(removal_script))):
            if args.only and not any(o.lower() in name.lower() for o in args.only):
                continue
            ran += 1
            for leaf in ("messages.jsonl", "verdicts.jsonl"):
                if os.path.exists(os.path.join(folder, leaf)):
                    os.remove(os.path.join(folder, leaf))
            if setup:
                audit(*setup)
            decision, reason, _ = gate.evaluate(command, SESSION, PROFILE, audit_root=audits)
            ok = decision == expected and (decision != "deny" or "session's audit has not been run" in reason)
            print("{0}  {1:<34} {2}".format("PASS" if ok else "FAIL", name, decision))
            failures += 0 if ok else 1
    finally:
        claude_temp.remove_fixture_tree(root)

    print("")
    print("{0} case(s) in {1:.2f}s".format(ran, time.time() - started))
    if args.only and ran == 0:
        print("--only matched no cases; refusing to report success on an empty selection.")
        return 1
    if failures == 0:
        print("All PreToolUse cases passed.")
        return 0
    print("{0} case(s) failed.".format(failures))
    return 1


if __name__ == "__main__":
    sys.exit(main())
