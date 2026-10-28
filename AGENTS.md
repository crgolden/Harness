# AGENTS.md

Reference for agents operating on this repository. The human-facing entry point is [README.md](README.md); this file is the deep reference, and nothing is duplicated between the two.

Everything here judges every Claude Code session in the `crgolden` workspace: the hooks in `Hooks/` run on every tool call, prompt, session start and stop, and the library in `Gates/` is what every repository's `gate.ps1` runs. A change here changes what every session may do, so this repository is edited by the master session alone, in a worktree of its own, and lands by pull request. The skills, the master session's scripts, the coordination scripts and the alert-triage agent are not harness code and stay in the Tools repository beside this one, where every session reads them.

## Repository layout

| Directory | Purpose |
|-----------|---------|
| `Hooks/` | The live hooks and their launchers. `settings.json` runs `launch_dispatcher.py` for PreToolUse and PostToolUse, `launch_stop.py` for Stop, `launch_prompt.py` for UserPromptSubmit and `Invoke-SessionStart.ps1` for SessionStart; each launcher reads stdin first and fails closed when the hook beside it cannot load. The design and the evidence behind every gate: [Hooks/STOP-HOOK.md](Hooks/STOP-HOOK.md) and [Hooks/PRETOOLUSE-HOOK.md](Hooks/PRETOOLUSE-HOOK.md); the rules every session is held to: [Hooks/stop-gate-rules.txt](Hooks/stop-gate-rules.txt), which the SessionStart hook puts in context. `coordination.json` names the coordination board; `state_share.json` the state share. Both hook suites read the live `settings.json`. A hook that reaches a skill (the session audit, the parking-lot template, the docs-audit prose lint, the coordination skill, the fleet audit's repo sync) reads it from `<workspace>\Tools\Claude\skills`, never from this checkout. |
| `Gates/` | The gate library every repository's `gate.ps1` loads through `GATE_LIBRARY` (`GateCommon.ps1` and one module per concern), `gate_status.py` (the one reader of gate records), `GateSlot.ps1` (the account's one gate slot) and `step_inputs.py` (the step plan derived from the code). `GateCatalog.ps1` resolves the two catalog steps from this checkout's root. Owning doc: [AGENTS/GATES.md](AGENTS/GATES.md). |
| `CommentAudit/` | `Get-CommentCatalog.ps1`, the comment catalog every gate runs, `Test-CommentCatalog.ps1` and `Remove-XmlDoc.ps1`. The comment policy it enforces is the Tools repository's `AGENTS/COMMENTS.md`. |
| `Claude/` | `Get-CodeStyleCatalog.ps1`, the code-style catalog every gate runs, and `Test-CodeStyleCatalog.ps1`, which fails when the two catalogs' exempt-repo lists differ. The rules it enforces are the Tools repository's `AGENTS/CODE-STYLE.md`. |
| `Shared/` | `ScanScopeHelper.ps1`, the scan-scope helper both catalogs dot-source. |
| `AGENTS/` | [GATES.md](AGENTS/GATES.md), the gate protocol and gate records, which the workspace's `AGENTS.md` index points at. |

## Operating rules

- **Only the master session reads this repository or creates a worktree of it.** The read-scope gate denies every other session a `Read`, `Grep`, `Glob` or shell command naming a path under this checkout, and the PreToolUse gate denies a `git worktree add` or `EnterWorktree` of it, unless `MASTER_SESSION=1` is in the hook's environment. A session runs the named entry scripts (`Gates\gate_status.py`, `Hooks\session_home.py`, `GateCommon.ps1` through `GATE_LIBRARY`) by name only. The rules card and the artifact-usage skill reach every session through the SessionStart hook's context.
- **Nothing is edited on the main checkout, the master included.** The live hooks run from `main`, and `Hooks/invoke_main_checkout_gate.py` denies a file-tool write anywhere in it. The master makes every change in its own worktree at `%USERPROFILE%\source\worktrees\<session-id>\Harness`, on the branch named by its session id, gates it there, and the operator merges it as a pull request. A session that is not the master makes no change here: it reports the defect to the master through the coordination board.
- **A hook change is verified by its harness with a planted defect** before the gate runs, and the harnesses are steps of `gate.ps1`, never run on their own ([AGENTS/GATES.md](AGENTS/GATES.md)).
- **Comments follow the workspace comment policy**, which lives in the Tools repository's `AGENTS/COMMENTS.md`.
- **Every path to this checkout resolves from the checkout's own root** (`$PSScriptRoot`, `__file__`), never from a user-profile path: `~/.claude` syncs to every machine and the profiles differ. A path into the Tools repository resolves from the workspace root (`Hooks/session_home.py workspace`).
