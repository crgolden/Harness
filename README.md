# Harness

The Claude Code harness for the `crgolden` workspace: the hooks every session runs under and the gate library every repository's `gate.ps1` loads, with the catalogs those gates run. Private; nothing here is a product. The skills, the master session, the coordination scripts and the alert-triage agent stay in the Tools repository beside this one.

The agent-facing reference is [AGENTS.md](AGENTS.md).

## Stack

PowerShell 7 (`pwsh`) and Python 3, no package manager and no build. The hooks are launched by `~/.claude/settings.json` through `python -c` bootstraps that run `Hooks/launch_dispatcher.py` (PreToolUse and PostToolUse), `Hooks/launch_stop.py` (Stop) and `Hooks/launch_prompt.py` (UserPromptSubmit), and `Hooks/Invoke-SessionStart.ps1` on SessionStart.

## Directory layout

| Directory | What it is |
|-----------|------------|
| `Hooks/` | Every PreToolUse, PostToolUse, SessionStart, UserPromptSubmit and Stop hook, their launchers, their harnesses, the Stop-gate rules card and the coordination board's configuration. Design: [Hooks/STOP-HOOK.md](Hooks/STOP-HOOK.md), [Hooks/PRETOOLUSE-HOOK.md](Hooks/PRETOOLUSE-HOOK.md) |
| `Gates/` | The gate library (`GateCommon.ps1` and its modules) that every repository's `gate.ps1` dot-sources through the `GATE_LIBRARY` environment variable, the gate record reader `gate_status.py`, the gate slot and the step planner |
| `CommentAudit/` | The comment catalog every gate runs, its harness and `Remove-XmlDoc.ps1` |
| `Claude/` | The code-style catalog every gate runs (`Get-CodeStyleCatalog.ps1`) and its harness; the skills and the other Claude Code maintenance scripts live in the Tools repository |
| `Shared/` | `ScanScopeHelper.ps1`, the scan-scope helper both catalogs dot-source |
| `AGENTS/` | [GATES.md](AGENTS/GATES.md), the gate protocol and gate records |

## Prerequisites

- Windows, PowerShell 7 on `PATH` as `pwsh`, Python 3 on `PATH` as `python`.
- `git`, and a clone of the workspace repositories beside this one: the hooks read the skills from `<workspace>\Tools\Claude\skills` and the Stop gate's commit checks read the repositories.
- Claude Code, with `~/.claude/settings.json` registering the launchers above.

## Clone

Clone into the workspace folder beside the Tools repository and the application repositories, as `<workspace>\Harness`:

```powershell
git clone https://github.com/crgolden/Harness.git
```

The hooks find the workspace as the folder this checkout sits in (`Hooks/session_home.py workspace`), or from `CRGOLDEN_WORKSPACE`.

## Secrets and environment

No secret is stored here. The hooks read the coordination board's URL from `Hooks/coordination.json` and the state share from `Hooks/state_share.json`; every session's own state lives under `%TEMP%\claude` (`Hooks/claude_temp.py`). `GATE_LIBRARY` names `Gates/GateCommon.ps1` for every repository's gate (`Configuration\Set-GateLibrary.ps1` in the Tools repository sets it once per machine).

## Run

Nothing here runs on its own: Claude Code runs the hooks and a repository's `gate.ps1` loads the gate library.

## Tests and gate

Every harness is a `Test-*.ps1` or `test_*.py` beside what it checks. The gate runs them all, in the order the step planner derives from the code:

```powershell
pwsh -File "<workspace>\Harness\gate.ps1" -Goal "<the pinned goal, verbatim>"
```

It ends with `GATE: PASSED` or `GATE: FAILED` and writes its record under `%TEMP%\claude\crgolden-gates\records` ([AGENTS/GATES.md](AGENTS/GATES.md)).
