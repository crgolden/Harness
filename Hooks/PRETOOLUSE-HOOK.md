# PreToolUse Hook

Reference for the Claude Code `PreToolUse` hooks in `~/.claude/settings.json` and their regression
harnesses. Read this before editing either hook.

**There are two `PreToolUse` entries.** Most of this document is about the first; the second has its own
section at the end.

| Matcher | Gate | Harness |
|---|---|---|
| `PowerShell`, `Monitor` | `invoke_pretooluse_gate.py`: shell-command deny branches plus the allow tail | `test_pretooluse_gate.py` |
| `PowerShell`, `Monitor` | `invoke_gate_cadence_gate.py`: hold every quality-check command to `Harness/AGENTS/GATES.md` | `test_gate_cadence_gate.py` |
| `Edit\|Write` | `invoke_agents_doc_gate.py` — refuse the first edit in a repo until `AGENTS/REPOS/<Repo>.md` is read | `test_agents_doc_gate.py` |
| `Edit\|Write` | `invoke_doc_state_gate.py` — refuse to write operational state into a permanent markdown doc | `test_doc_state_gate.py` |
| `Edit\|Write` (`PostToolUse`) | `invoke_comment_reminder.py` — remind the agent of `Tools/AGENTS/COMMENTS.md` when an edit adds a comment line; context only, never a block | `test_comment_reminder.py` |
| `Edit\|Write` (`PostToolUse`) | `invoke_doc_prose_reminder.py` — remind the agent that a permanent markdown doc carries no anecdote or history when an edit adds prose to one; context only, never a block | `test_doc_prose_reminder.py` |

The two `Edit|Write` gates are separate hooks inside **one** matcher entry, so both run on every file
edit and either can deny. Keep them separate: one gate, one concern.

**Both gates are already Python.** Earlier revisions of this file describe the PowerShell gate as live and
record the Python port as "proposed and declined"; `settings.json` points both entries at `.py` files, so
read the latency figures below as history — they were measured against the `pwsh.exe` implementation.

`~/.claude/settings.json` is not version-controlled, but it **is** OneDrive-synced across all four
machines, so the hook and its timeout propagate automatically.

## What the hook does

Two jobs, in this order. Ordering is load-bearing.

1. **Deny branches** — block `git commit`/`push`, `cd`-chaining, `Push-Location`/`Pop-Location`,
   and any command containing a shell operator (`;` `|` `` ` `` `&&` `||` `$(` CR/LF, or a
   standalone `&` used for chaining or backgrounding), and any command holding a doubled backslash
   (`\\`), where Claude Code's permission matcher splits even a quoted argument, so the call prompts
   whatever the allow rules say. A `dotnet test` deny used to sit here on the
   premise the command was broken workspace-wide; the premise was disproved (every repo's
   `global.json` opts into the MTP runner, so `dotnet test` works from a repo root — it fails only
   when run from the workspace root, which has no `global.json`) and the rule was removed. The suite
   pins the removal with `dotnet test allowed` / `ps1 body dotnet test allowed`.
   **Session branch naming** is a deny branch too: `git worktree add` must create or check out the branch named
   by the session id (`-b <session-id>`, or the existing `<session-id>` branch) at the absolute path
   `%USERPROFILE%\source\worktrees\<session-id>\<Repo>`, `<Repo>` being the folder name of the command's `-C`
   directory and `Tools` with none (`worktree_repo`; a relative path counts only beside an absolute `-C`), and
   `git checkout -b|-B|--orphan` and `git switch -c|-C|--create|--force-create|--orphan` may create only that
   branch; `--detach` is refused. The session id and profile come from the hook payload and `USERPROFILE`
   through the dispatcher's `shell_gate`; with either missing the rule does not run. It reads the command line
   only, so a `worktree add` inside a script body is not judged. The harness rows are the `session branch and
   worktree naming` block.
   **Harness is the master's** (`harness_scope.py`, shared with the read-scope and main-checkout gates): a command
   naming a path in the Harness repository, its main checkout or a worktree of it in any spelling, is denied unless
   `MASTER_SESSION=1` on the master host (`master_session.runs_as`), and a `git worktree add` of it carries its own
   reason. The named entry scripts (`Gates\gate_status.py`, `Hooks\session_home.py`) pass; `GateCommon.ps1` is
   loaded through `GATE_LIBRARY`, which no command names; the skills stay in Tools and are run there. The rule runs
   before the read-only git allow, so `git -C <Harness> status` is denied too. The harness rows are the
   `harness_rows` block, against a fixture Harness.
   **Removing a worktree waits on the session audit**: `git worktree remove <path>`, and a `Remove-Item`, `rm`,
   `rd` or `del` of a path under `%USERPROFILE%\source\worktrees`, is denied while the session's audit has no
   catalog, holds a cataloged operator message with no verdict (the newest excepted; a message written after the
   last sync is the Stop gate's to judge), or holds a
   verdict whose evidence cites a file under that path, since removing the file reopens that verdict as
   stale-evidence. The command line and each line of a `pwsh -File` or `python` script body are judged. The harness
   rows are the `worktree remove` and `Remove-Item under worktrees` rows.
   **A script's body is judged too** for `pwsh -File <path>` and `python <path>`, which sit on the allow tail:
   a `git commit` or `git push` written in the file is denied unless the file carries the exempt marker.
   `RE_SCRIPT_CALL` takes the path after the interpreter's own flags (`-I`, `-B`, `-X utf8`, `-Wonce`), so a flag
   before the path never hides the body. The harness rows are the `py body commits behind ...` cases and their
   clean control.
2. **Allow tail** — for a bare, single-invocation command whose prefix is on the allow list, or a vetted
   read-only `git` invocation, emit `permissionDecision: "allow"`, which bypasses the permission prompt
   entirely.

Because the allow tail runs **last**, an allow-listed prefix can never rescue a command an earlier deny
branch rejected. `dotnet --version; Write-Output pwned` still denies.

## Why whitelisted commands prompted — the actual root cause

The original theory was that `permissions.allow` is a string prefix match and that operator splitting was
the single defect. Operator splitting is real and documented — "the recognized command separators are
`&&`, `||`, `;`, `|`, `|&`, `&`, and newlines. A rule must match each subcommand independently" — but it is
one cause among several. The permissions reference documents all of these, and several fire on bare,
operator-free PowerShell commands:

| Mechanism | Documented behavior |
|---|---|
| Operator splitting | Every subcommand must independently match an allow rule. Quoting does not protect you. |
| Unparseable commands | "When Claude Code can't fully parse a command, it asks for approval." Over 10,000 chars always prompts. |
| UNC paths | Any argument containing `\\server\share` prompts. The doc states explicitly: "The same check applies to PowerShell tool commands." |
| Exec wrappers | `watch`, `setsid`, `ionice`, `flock`, and `find -exec`/`-delete` "always prompt and can't be auto-approved by a prefix rule." |
| Env-assignment prefix | An allow rule "won't match past an assignment of any other variable" outside a known-safe set. |
| Environment runners | `npx`, `docker exec`, `mise exec` are not stripped as wrappers, so a rule covers whatever follows them. |

**The consequence: the allow list cannot be made deterministic by tuning the rules.** Too many
prompt-forcing edge cases, and in auto mode a classifier that can suspend rules wholesale. That is why
years of adding and reshaping `permissions.allow` entries never converged. The allow tail sidesteps the
matcher instead of trying to satisfy it.

### Auto mode — conditional, and not currently in play

In auto mode a classifier runs as a second gate after the permissions system, and it "suspends only the
broad rules that grant arbitrary code execution, such as `Bash(*)` or **wildcarded interpreters**."
`PowerShell(python *)`, `PowerShell(npx *)`, and `PowerShell(pwsh -File *)` are all wildcarded
interpreters. `autoMode.classifyAllShell: true` suspends every shell allow rule.

This applies **only** while auto mode is active. With `defaultMode: acceptEdits` it does not apply, and
the fix in this document is unaffected by it.

Two things to know anyway:

- **To detect it:** `/permissions` → **Recently denied**. The literal string `Blocked by classifier` is
  proof the classifier is involved. Its absence points at the table above.
- **It fits the historical `python` observations well** — `python -m pytest` never prompting, a fresh
  script path prompting, the same script with a new argument prompting again. Under a prefix-matching
  model that looks like a bug; under a content-scoring classifier it is the design. If auto mode is ever
  adopted, re-read those symptoms in that light.
- **Open question if auto mode is adopted:** a hook's `allow` bypasses the permission *prompt*. Whether it
  also bypasses the *classifier* is undocumented — the auto-mode docs place deny and ask rules before the
  classifier but say nothing about hook decisions. Test this before assuming the allow tail still covers
  you.

## The allow tail

Appended after every deny branch:

```powershell
if (-not $cmd) { exit 0 }
$t = $cmd.Trim()
if ($t -match '>(?!\s*&)')             { exit 0 }
if ($t -match '(?i)\bpip\s+install\b') { exit 0 }
if ($t -cmatch '^git\s+(-C\s+("[^"]+"|[^\s"]+)\s+)?(status|log|diff|show|rev-parse|describe|blame|
                shortlog|ls-files|ls-tree|cat-file|for-each-ref)(\s|$)'
    -and $t -cnotmatch '(^|\s)(-c\s|--exec-path|--upload-pack|--output)') { ...allow...; exit 0 }
$allowPrefixes = @('az ','dotnet ','Expand-Archive ','gcloud ','gcx ','gh ','Get-ChildItem ',
  'Get-Content ','jb ','mypy ','New-Item ','npm ','npx ','playwright-cli ','pwsh -File ','python ',
  'Resolve-Path ','ruff ','Set-Location ','sonar ','sqlpackage ','syncthing ','Test-Path ')
foreach ($p in $allowPrefixes) {
  if ($t -like ($p + '*')) { ...permissionDecision=allow...; exit 0 }
}
exit 0
```

The four `PreToolUse` decisions, per the hooks reference:

| `permissionDecision` | Semantics |
|---|---|
| `allow` | Bypasses the permission prompt entirely |
| `deny` | Blocks the call |
| `ask` | Forces a prompt |
| `defer` | Exits so the tool call can be resumed later; `updatedInput` and `additionalContext` are ignored |
| exit 0 with no JSON | Normal permission flow applies |

None of these gates emits `defer`. Their internal `DEFER` decision writes no JSON, which is the last row.

Design decisions worth preserving:

- **Prefixes, not head tokens.** `pwsh -File ` is deliberately narrower than `pwsh `; a bare `pwsh ` head
  would auto-approve `pwsh -Command <anything>`, which is arbitrary code execution.
- **The `pwsh` near-miss guard, and why a *deny* is the right shape for it.** Because the allow rule is a
  prefix over the **whole** command, any flag placed before `-File` — `pwsh -NoProfile -File …`,
  `pwsh -NonInteractive -File …`, even `pwsh.exe -File …` — misses the match and lands on the prompting
  path. Without this branch that case simply *defers*, and deferring is exactly the problem: **the cost is
  invisible to the agent.** The tool result comes back with correct output and no error, indistinguishable
  from an auto-approved run, while the operator silently absorbs an approval prompt. Every other agent
  mistake surfaces as something the agent can react to; this one externalises the entire cost and returns a
  clean signal, which is why it survived roughly ten calls in a single session and had propagated into nine
  docs and two memory files before anyone noticed. The branch converts that silent tax into an error the
  agent sees and self-corrects on, and it also catches `powershell`/`powershell.exe`, which spawn a nested
  Windows PowerShell 5.1 under a tool that is already pwsh 7. `pwsh -File …` itself stays on the allow tail.
- **Inline interpreter code is denied before the allow tail.** `python -c "<code>"` with no shell operator
  matches the `python ` prefix, and the script-body intent scan reads only a file path, so without this
  branch inline code was approved unread. `runs_inline_code()` tokenizes with `shlex` (a quoted value stays
  one token) and treats an untokenizable command as inline code. For `python`, `pythonw` and the `py`
  launcher it reads options up to the first positional argument or `-m`, skipping the value of `-W`, `-X`
  and `--check-hash-based-pycs` so a value shaped like a script path cannot end the scan; `-c` is caught
  alone, combined (`-Ic`) or glued to its code (`-cCODE`). Python flags are case-sensitive, so `-E` and `-I`
  alone stay allowed, and anything after the script path is that script's own arguments. Node has too many
  value-taking options to list, so every token up to a bare `--` is read, and any short cluster holding `e`
  or `p` (`-pe`) or `--eval`/`--print` denies; a script's own `-e` or `-p` argument is denied with it, which
  fails safe. The interpreter is matched by the last segment of its path, so a full path such as
  `"C:\Python\python.exe" -c` denies rather than prompting.
- **The prefix list mirrors `permissions.allow` exactly.** This is not a widening — it makes
  already-declared intent deterministic. `ssh`, `psql`, `mongosh`, `sqlcmd`, `func`, and raw test `.exe`s
  are deliberately absent and still prompt.
- **`git` is handled by a subcommand regex, not a prefix — and this is why.** A `'git '` prefix would
  auto-approve `git reset --hard`, `git clean -fd`, and `git checkout -- .`, so for months `git` was left
  off the list entirely and every `git status` cost an approval prompt (six denials in one week of
  transcripts). The branch added 2026-08-19 allows only subcommands that cannot mutate the repo or the
  working tree, with or without a leading `-C <path>`, and refuses four option-embedded escape vectors:
  - `git fetch` / `git pull` are **excluded on purpose.** They look read-only and are not:
    `--upload-pack='<cmd>'` and `ext::` remote URLs both execute arbitrary commands.
  - `-c key=value` is refused because `git -c core.pager=<cmd> log` runs `<cmd>`. The check must be
    **case-sensitive** (`-cmatch` / `-cnotmatch`) — a case-insensitive match cannot tell the `-c` config
    override from the `-C` path argument the allow path depends on.
  - `--exec-path=<dir>` relocates the git binary directory; `--output=<file>` makes `git log`/`git diff`
    an arbitrary file write.
  `git commit` and `git push` remain denied by their own earlier branch, which runs first.
- **The leading `&` carve-out — a guardrail that used to contradict the hook's own advice.** The operator
  regex was `(^|\s)&(\s|$)`, which matched PowerShell's **call operator** at the start of a command and
  hard-denied it. `& "<abs path>.exe"` is the only way to invoke a quoted absolute path — and the hook's
  own `dotnet test` denial message (a rule since removed) *instructed the agent to do exactly that* ("run
  the compiled exe directly"). So the hook denied the pattern it told you to use; five denials in one week of transcripts
  were this. Corrected 2026-08-19 to `(?<!^\s*)\s&(\s|$)`: a `&` preceded only by start-of-string
  whitespace is the call operator and passes; anywhere else it is chaining or backgrounding and still
  denies. .NET variable-length lookbehind makes the leading-whitespace form work too.

  **It defers, it does not allow.** Nothing should auto-approve executing an arbitrary binary, so the
  carve-out only removes a wrong hard-deny and returns the command to the normal permission flow. Harness
  cases pin both halves: three `defer` cases for the call operator, and the pre-existing
  `dotnet --version & Write-Output pwned` and trailing `dotnet --version &` cases still `deny`.
- **The redirect guard** keeps `dotnet --version > C:\anything` on the prompting path rather than making
  it an auto-approved arbitrary file write. `2>&1` is exempt: the lookahead ignores the stderr-merge form.
- **The `pip install` guard** keeps package installs prompting, since they fetch and execute remote code.
- **Deny branches emit JSON and `exit 1`.** Docs prescribe exit 0 with JSON as one convention and
  exit-code-only as the other; the hook mixes them. Normalizing to `exit 0` is hygiene, **not** a live bug
  — the denials demonstrably work. It deserves its own separately-verified change.

## The harness

`test_pretooluse_gate.py` reads the real hook out of `settings.json` and pipes a `tool_input` payload into
`pwsh -NoProfile -NonInteractive -Command <hookCommand>` exactly as Claude Code does. Exits non-zero on any
regression. **Run it after any hook edit.**

```
python Tools\Hooks\test_pretooluse_gate.py
```

Run it in the **background** — it spawns one `pwsh.exe` per case and exceeds a 2-minute foreground tool
timeout.

Decisions are asserted four-way: `allow` / `deny` / `defer` / `error`. No case expects `error`, so a hook
crash or malformed emit surfaces as a failure instead of being silently read as a deny.

**The `allow` versus `defer` distinction is the whole point.** An earlier version computed
`$denied = $exitCode -ne 0 -or $text -match 'deny'`, collapsing both into "allow". Every positive case
reported PASS while still reaching a permission prompt in real use, and any allow path added later would
have gone green whether or not it fired. A harness that cannot separate "the hook approved this" from "the
hook said nothing" cannot verify an allow path at all.

## Latency and the timeout

Every invocation spawns a fresh `pwsh.exe`. Measured on `DESKTOP-O8LCMOT`, idle, warmed, round-robin:

| Path | min | median | max |
|---|---|---|---|
| allow tail hit | 3.76s | 3.96s | 4.56s |
| deny branch hit | 3.65s | 3.91s | 4.68s |
| defer fallthrough | 3.68s | 3.89s | 4.27s |

All three paths are statistically identical, so **the cost is process startup, not hook logic** — adding or
removing branches will not help. To attack latency rather than the budget, the lever is a persistent hook
process or an `http` hook.

**Production timings from transcripts, not a bench — and they are worse than the bench.** Aggregating the
`hook_success` / `hook_cancelled` attachment records across 19 session transcripts (2026-08-12 → 08-19):

| | value |
|---|---|
| completed runs | 1,787 at a **3,966ms median**, 29.2s max |
| timed-out runs | **260** (`timedOut: true`, 30s budget), 15.5s median duration recorded |
| timeout rate | **12.7%** of all invocations |
| total blocking time in the window | **~4.1 hours** |

The synthetic bench and the live median agree at ~3.9s, so the bench is sound — but it measured an idle
machine and missed that one call in eight blows the budget entirely under real load.

**The tax is not PreToolUse-specific — it is every `pwsh.exe` hook, and the obvious SessionStart
explanation is ruled out.** `SessionStart:startup` runs at a **6,159ms** production median (6 timeouts
against its 10s budget). The natural theory was that its `$env:AGENT_MACHINE_TYPE` guard was failing, so it
re-ran `Get-CimInstance Win32_OperatingSystem` plus a User-scope env write on every session — a User-scope
write lands in the registry and would not appear in an already-running process, so it would never
self-heal. **Measured 2026-08-19: that theory is wrong.** The variable is present in the process
environment (`PROCESS=len24`, `$env:` truthy), so the **fast branch is taken** and the WMI query is not
being paid at all:

| | median |
|---|---|
| fast branch, full hook wall time | 2,059ms |
| slow branch (WMI, no env write) | 2,093ms |
| `Get-CimInstance Win32_OperatingSystem` alone | 268–630ms (630 = cold first call) |
| User-scope registry *read* | ~0ms |

The two branches are indistinguishable, so the branch is not the variable. The synthetic fast branch is
2.06s against a **6.16s** production median — the same ~3× gap PreToolUse shows between its idle bench
(3.9s) and its live median under load. **One shared cause, three hooks**, and attacking it in the
PreToolUse hook alone would leave two-thirds of it in place. The User-scope env *write* was deliberately
not measured: doing so means mutating machine state, and it is on the branch that is not being taken.

**And the median was hiding the real number: `SessionStart:startup` times out on 35% of runs** — more
than one session start in three loses its hook entirely. An 8-day aggregation
(`hook_latency_report.py`, below) puts the whole picture well past what any single median suggests:

Timings below are over **successful** runs only; see the `durationMs` caveat that follows.

| event / hook | runs | median | max | timeout rate | blocked |
|---|---|---|---|---|---|
| `PreToolUse:PowerShell` | 2296 | 4044ms | 29161ms | 11.4% | 331min |
| `Stop` | 268 | 5808ms | 17556ms | 10.8% | 16min |
| `SessionStart:startup` | 21 | 7044ms | 9707ms | **33.3%** | 3min |
| `SessionStart:compact` | 37 | 2333ms | 9544ms | 16.2% | 3min |

**~5.9 hours blocked in hooks across the window**, and **every hook here fails open on 10–33% of runs**.

**`durationMs` on a timed-out record is not execution time**, and reading it as such produces nonsense.
It logs wall-clock until the cancellation was recorded: every record over 60s in this workspace's history
is a `timedOut` hook against a 30000ms budget, topping out at 320859ms — a 30s timeout whose record
landed five minutes later, most plausibly across a machine suspend. Others log *less* than their budget.
A first pass that pooled those with successful runs reported a `Stop` median of 33.4s and the conclusion
"failing open more often than it fired"; the real figure is 5.8s and roughly one run in nine. Take
timings over successful runs, and charge a timeout its budget rather than its logged duration —
`hook_latency_report.py` does both.

**The interpreter is the dominant lever, and it is measurable.** Controls on `DESKTOP-O8LCMOT`:

| | median |
|---|---|
| `pwsh -NoProfile -NonInteractive -Command "exit 0"` | **1,369ms** |
| `python -c pass` | **129ms** |

So ~1.4s of the ~4.0s is irreducible `pwsh.exe` startup, paid ~2,050 times a week. The remaining ~2.6s is
**unattributed** — the control used a tiny command string and the live hook passes a ~7KB one, so parse
cost and work done are confounded and no measurement here separates them. Do not repeat the earlier
mistake of building an inference on that gap. What the controls do establish: porting the hook body to a
`.py` file invoked by `python` attacks both the 1.4s floor and whatever the 2.6s is, at roughly 10× on
startup alone. **Proposed 2026-08-19 and declined by the user for now** — the two correctness fixes shipped
without it, so the median and the 12.7% timeout rate are unchanged and still open.

No hook entry sets a `timeout`: a short one turns a slow but sound check into a skipped or blocked call, while
a hook that truly hangs is ended by Claude Code's default and, on `PreToolUse`, blocked by `onFailure: "block"`
(Failure behavior below).

Two measurement tools, and they answer different questions — do not substitute one for the other:

- **`Measure-HookLatency.ps1`** benches one tool call as the host runs it: every hook `settings.json`
  registers for that tool, started at once, timed from the first start to the last exit, idle and with one
  CPU-bound worker per core. It reads the registration rather than naming scripts, so it measures whatever
  is registered. It warms each probe and measures round-robin, because measuring one probe's iterations
  consecutively confounds the code path with the position and charges the first probe the cold start.
- **`hook_latency_report.py`** aggregates what the hooks actually cost in production, from the
  `hook_success`/`hook_cancelled` attachment records in session transcripts, keyed on `timedOut` to
  separate a blown budget from a user Esc. `python Harness/Hooks/hook_latency_report.py --days 8`.

**The synthetic number is not the one that matters, and a harness cannot substitute for either.** A
regression case that runs a hook at idle clears any sane margin and could never fail, so adding one to
`test_pretooluse_gate.py` would assert nothing while looking like coverage. Load-dependent cost is only
visible in aggregate, after the fact — which is why the production report exists as a separate tool.

## Failure behavior

**Without `onFailure`, a `PreToolUse` hook that crashes or times out lets the call through.** The hooks
reference ("Timeouts", "Block the action when a hook fails"): a command hook that exits other than 0 or 2, or
reaches its `timeout`, is a non-blocking failure, and the call continues through the normal permission flow,
where an allow rule passes it. The probe at the end of this section shows both sides: with `onFailure` unset,
a `PreToolUse` hook on `Read` that sleeps past its `timeout` lets the read return the file; set to `"block"`,
the read fails with "timed out; blocking because onFailure is \"block\"".

**So the dispatcher's `PreToolUse` entry carries `"onFailure": "block"`** (Claude Code 2.1.295 or later on
every machine), and no hook entry sets a `timeout`, so Claude Code's own default (600 s for a command hook)
applies. A hook that hangs blocks the call when that limit passes; a hook that crashes blocks it at once. No
gate keeps a deadline of its own: an inner deadline only turns a slow but sound check into a refusal or a
prompt. `onFailure` has no effect on `Stop` hooks, whose failure still allows the stop.

To re-establish the behavior after a Claude Code upgrade, run the same probe in a scratch folder under
`claude -p --setting-sources local --settings <file>`, with a **separate, temporary** sleeping hook and the
`onFailure` unset and set; never by breaking the live hook, which syncs to every machine.

## Known trade-offs — intentional denials

Legitimate commands denied by design, because they contain `;` or `|` and would have prompted anyway:

- `sqlpackage /TargetConnectionString:"Server=x;Database=y"` (this exact form appears in `Identity/README.md`)
- `dotnet build -p:NoWarn="CS1591;CS0618"` — MSBuild property lists
- `gh api user --jq ".name | ascii_downcase"` — jq pipes
- `az account list --query "[0] | [0]"` — JMESPath pipes

**Escape hatch:** put them in a `.ps1` and run `pwsh -File <abs path>`, which the allow tail covers.

## Traps

1. **Settings reload lag.** `settings.json` edits are not picked up instantly. A live tool call fired
   immediately after editing exercises the *previous* hook and reports a working fix as broken. This
   produced a false "the bypass is still open" conclusion that was chased for several turns. Trust the
   harness — it reads the file directly. Re-check live only after a delay.
2. **Don't trust a single live observation.** A backtick-escaped `` `; `` was called a fourth bypass on one
   live result; the harness showed it had been denied correctly all along.
3. **Seeing what the hook actually receives.** Temporarily insert a first branch
   `if ($cmd -match 'DIAGECHO') { deny with reason = $cmd }`, then run a command containing `DIAGECHO` to
   echo back the exact string. This disproved a theory that the tool pre-expands `$(...)` before the hook
   runs — it does not. Remove the branch afterwards.
4. **A Python hook reads stdin as `sys.stdin.buffer.read().decode("utf-8", errors="replace")`, never
   `sys.stdin.read()`.** Claude Code pipes UTF-8 and the hook's stdin defaults to the ANSI code page
   (cp1252), so every non-ASCII character arrives garbled: a curly quote the doc-state gate or the stop
   gate's commit-pointer veto must strip, an em-dash `prose_lint.py` must flag. An in-process harness case
   cannot see it; the discriminating case runs the script in a child with `PYTHONUTF8=0`.
5. **Don't build inferences on top of sound measurements without checking them against data you already
   have.** Two claims died this way: that the deny branches' `exit 1` made them inert (contradicted by the
   `git commit` block working for months), and that a hook timeout would be observationally invisible
   (contradicted by the deny-and-allow-rule overlap above). Both times the disproof was already in hand.

## Not yet tested

The harness covers the vectors that have been thought of, not a proof of completeness. Untested:

- Unicode line separators (U+2028 / U+2029) as newline substitutes
- The PowerShell stop-parsing token `--%`
- `Invoke-Expression` / `iex` of a string variable, `Start-Job -ScriptBlock`
- `$ENV:`-based indirection
- Very long commands, or non-ASCII encodings
- Hook timeout behavior (see Failure behavior above)
- Whether a hook `allow` bypasses the auto-mode classifier

## The second entry: `Edit|Write` -> `invoke_agents_doc_gate.py`

Denies the first `Edit`/`Write` in a repo until that session has read `AGENTS/REPOS/<Repo>.md` end to end.
Added 2026-09-01. The rationale and the coverage rule live in the gate's module docstring; what belongs
here is the wiring and the gaps.

- **Matcher `Edit|Write`, not `PowerShell`.** It fires on file edits, which are far rarer than shell
  calls: the `PowerShell` entry ran 2,296 times in eight days. It runs inside the dispatcher, whose entry sets
  no `timeout`, so Claude Code's default applies: a timeout clock includes Claude Code's own event-loop stalls,
  and a hook past it is cancelled and its output discarded even when that output has already arrived. The transcript records that as `hook_cancelled`
  with no `timedOut` flag; `claude --debug hooks --debug-file <path>` logs the output beside the cancel.
- **The transcript is the record**, read from the hook payload's `transcript_path`. That avoids a second
  hook to track reads, and it means the gate credits only the **agent's own** `Read` tool_use blocks — a
  read the operator issues on the agent's behalf does not count, deliberately.
- **Scope is narrow by construction.** Only `<workspace>/<Repo>/…` where `AGENTS/REPOS/<Repo>.md` exists is
  gated, so `AGENTS/` itself, `Tools/` (which uses `Tools/CLAUDE.md`), the scratchpad and everything
  outside the workspace defer. Adding an `AGENTS/REPOS/Tools.md` would silently bring `Tools/` under the gate;
  `test_agents_doc_gate.py` asserts that file's absence so the change surfaces as a failure instead.
- **Known gap, pinned rather than fixed: an unreadable transcript fails open.** A parse failure on
  individual lines is skipped, but a missing or unopenable transcript defers. Failing closed there would
  hard-block every edit in the workspace on a transient read error. Do not "fix" this without first
  measuring how often that read actually fails.
- **Known gap: it cannot tell reading from skimming.** It checks that the line ranges were requested, not
  that anything was understood. It raises the floor; it is not a comprehension test.

```
python Tools\Hooks\test_agents_doc_gate.py
```

Fourteen cases, both directions, in-process and fast enough for the foreground. The must-fire half
includes the shape that produced the gate — three chunked reads covering lines 1-36, 36-95 and 95-169 of
a 231-line file, reported at the time as having read it "end to end".

**The fixtures cannot validate the transcript format, because they encode the same assumption the gate
does.** The first version of this gate passed 14/14 while being unable to read a real transcript at all.
After any change to the record-walking code, run it against a live transcript and confirm it returns
`True` for a doc read in full and `False` for one never read — a scratch script doing exactly that is the
only thing that caught it. Two traps found that way: picking the newest `*.jsonl` by mtime lands on a
different session, and `Read` blocks carry `limit`/`offset` far more often than expected.

## The third gate: `Edit|Write` -> `invoke_doc_state_gate.py`

Denies a markdown edit that writes operational state into a permanent doc. The rationale lives in the
gate's module docstring; what belongs here is the wiring, the precision argument and the gaps.

**It enforces a mechanical tell, not a motive, and a green run is not evidence the motive is handled.**
It matches a shouted status token beside a date, a heading asserting a present condition, a pasted HTTP
header, and a phrase naming the writer's own git or deploy state. It cannot detect "banking
proof-of-work"; it detects `DONE` next to `2026-09-10`. Treat it as a floor, exactly like the
reading-coverage gate, which raises the floor without being a comprehension test.

**Precision was measured against the real corpus before it shipped, and three candidate checks were cut
on the evidence.** Probing every non-exempt `.md` under `AGENTS/` and `Tools/`:

| Candidate check | Corpus hits | Verdict |
|---|---|---|
| thousand-separated integer | 200 | **cut** — MC/DC truth-table rows, vendor quotas |
| `N rows` / `N clicks` / `N impressions` | 81 | **cut** — the same truth tables |
| bare verification stamp (`verified <date>`) | 46 | **cut** — the legitimate "Measured `<date>`: that theory is wrong" genre, where the date is provenance for a durable finding |
| the shipped set (intersection + announcement form) | 2 | kept; both genuine |

Two design consequences worth preserving:

- **A stamp only counts beside a status assertion.** Dating a durable finding is provenance and is
  encouraged elsewhere in these docs; dating a *status* is the defect. No regex separates the two, so
  the gate requires both signals on one line.
- **The announcement form is the discriminator.** A SHOUTED or bolded token fires; lowercase narrative
  ("that shipped on `<date>`") does not, because that is a different defect (history), and the
  `(CLOSED <date> — reason)` design-log convention used throughout this file stays legal.

`test_doc_state_gate.py` pins all three cut checks as **must-not-fire**, so re-adding one means
arguing with a failing test rather than a comment.

- **Deny, not ask.** The remedy is a judgement call, which normally argues for `ask`; it denies because
  the measured false-positive rate is near zero and `ask` would spend an operator prompt per hit. A
  check that cannot be made precise enough to deny is not ready — three were cut on that rule.
- **Only the new text is scanned** (`content` for Write, `new_string` for Edit), so nobody is blocked
  for prose they are not touching.
- **Fence state is reconstructed for an Edit.** Every pattern fires inside a command example, and an
  Edit sees a fragment whose enclosing fence may open outside it. The gate finds `old_string` in the
  file and counts preceding fences; landing inside one defers the whole check.
- **Exempt by name: docs whose content IS status** — a backlog, an audit ledger, a review round, a
  decommission inventory, a coverage baseline, plus the memory directory and the scratchpad. The
  harness asserts those fixtures still exist, so retiring one fails a test instead of silently widening
  the gate. Adding to that list is a deliberate decision, not a way around a hit.
- **Known gap: it reads one line at a time.** A status assertion split across two lines by a wrap is
  invisible. Narrowing to a line was what made the false-positive rate acceptable; widening to a
  paragraph would need its own corpus measurement first.
- **Known gap: it cannot see a reading with no status word.** A bare pasted count in prose passes,
  because every pattern that caught those swamped the signal. The prohibition still binds; the gate
  just does not enforce that half.

```
python Tools\Hooks\test_doc_state_gate.py
```

**Live-check this gate from `$env:TEMP`, never from inside a repo.** A harness pass proves the logic,
not that the hook is wired, so the spot-check has to be a real `Write`. Two constraints collide: the
session scratchpad is on the exempt list, so it cannot exercise the deny path at all, and a workspace
repo is version-controlled and syncs to four machines. `$env:TEMP\<name>.md` satisfies both — outside
every repo, and not exempt. Run **two writes of opposite sign**: a violating line, which must be denied
and must leave no file, and a clean line, which must succeed. The second one is what proves the gate is
not simply denying every markdown write. Delete the file afterwards.

## The fourth gate: `PowerShell` -> `invoke_gate_cadence_gate.py`

Enforces the gate protocol. The rules and why they exist live in `Harness/AGENTS/GATES.md`;
what belongs here is the wiring, the state model and the gaps.

- **A second hook inside the `PowerShell` matcher entry**, beside the shell gate, one concern each. Claude
  Code runs every matching hook to completion and merges `PreToolUse` decisions most restrictive first,
  in the order `deny`, `defer`, `ask`, `allow` (hooks guide: "the most restrictive answer applies"). So
  this gate's `deny` and `ask` survive the shell gate's `allow`.
- **The cheap path comes first.** Only a command the classifier calls a gate step opens any transcript;
  everything else defers before touching the disk, because this entry fires on every shell call.
- **A gate the hook cannot judge is denied, never asked about and never let through.** `decide()` runs
  `evaluate()` to completion with no clock of its own; an exception denies with the error's name. A hang is
  bounded by Claude Code's hook timeout, which the dispatcher entry's `onFailure: "block"` turns into a block
  (Failure behavior above). It never answers `ask`: a session nobody watches, such as the master, cannot
  answer a prompt and would wait on it for good.
- **The declaration, operator and page readers read only what the transcript gained.** `first_declaration`,
  `first_declaration_time`, `first_write_under_newer_goal`, `operator_texts`, `goal_pin`'s goal readers and
  `invoke_artifact_gate.page_answers` read through `transcript_index.py`, which keeps, per transcript, the byte
  offsets of the lines each reader's needles match and of every tool-result line keyed by its `tool_use_id`s,
  with the offset scanned to. A reader finds a call by its needles and the call's result by id, never by what
  the result text happens to say. The index holds offsets, never content, and every line is read back from the
  transcript from an offset that must start a line and must still match, so a tampered index can hide a line
  but never invent one. A transcript whose first bytes or the bytes before the scanned offset
  changed is scanned again from the start.
- **Each transcript is read once, then only what was appended.** `scan_transcript()` caches a file's
  record walk (gate launches, results, stopped and finished task ids, edits, the goal pin) under
  `<temp root>\claude\gate-cadence-cache\` (`claude_temp.py`: `CLAUDE_CODE_TMPDIR` when set, else `%TEMP%`) with
  the byte offset it reached, and resumes from it. Only complete
  lines are consumed, so a line still being written is read whole next time. A file whose first 4 KB
  hash differs, or that is shorter than the offset, is walked from the start. The cache is written to a
  temporary file and renamed, so two hooks racing on it each leave a consistent scan. `invoke_stop_gate.py`
  calls `collect()` without a cache root, which walks in full.
- **The expensive path scales with history, so it classifies as little of it as it can.** A gate launch
  reads this session's transcripts and every peer's, and classifying each past shell call is what costs:
  a call that runs a script reads and scans the script's whole body. `collect()` therefore resolves each
  call's state before classifying it, and the peer check passes `unfinished_only=True`, which skips every
  finished call because `is_live()` rejects it whatever it is. The gate-step patterns run as one merged
  regex (`RE_GATE_STEP`), and `script_code()` reads each script once per file version within a call.
- **Classification.** *Inline*: a gate-step verb in the command itself (`dotnet test`, `npm run lint`,
  `pytest`, `jb inspectcode`, a `*.Tests.*.exe` ...). A plain build is not inline: one `dotnet build <path>`
  with no `-t` target (a target can run anything), or one `npm run build --prefix <path>` (`RE_PLAIN_BUILD`,
  `plain_build_target`). It is the proof a gate needs, so it may run at any time. *Script*: `pwsh -File` or `python` of a file named like a harness
  (`test_*.py`, `Test-*.ps1`), or whose body carries a gate-step verb, names a harness file it runs, prints a
  `GATE:` verdict, or runs its steps through a variable (`& npm run $step`, `& dotnet $args`). The last two
  exist because a Librarian gate built on a `Step` helper named no step beside a runner, so it went
  unclassified on every launch and none of the rules below ever judged it. Comment lines and `<# #>` blocks
  are stripped before the verdict, variable, narrowing and workflow checks read a body.
  A `Test-`-named operational probe in `OPERATIONAL_PROBES` (`Claude/skills/version-audit/scripts/Test-VmService.ps1`,
  `Seeding/Test-Irs990Coverage.ps1`) reads a live system, not the tree, so it is no harness: matched by its
  resolved path under the hook's own Tools root (a skills junction resolves to it, a copy elsewhere
  does not), then judged by its body like any other script.
  A harness named in a command that only reads it (`Get-Content test_x.py`) is not a gate, and neither is
  any command headed by a read-only reader (`git diff`, `git show`, `git log`, `Get-Content`,
  `Select-String`, `rg` ...) whatever its arguments name, unless the command also pipes, chains or hands
  what it reads to an interpreter (`| Invoke-Expression`, `; npm run lint`): the catalog-script pattern
  used to match a `git diff -- Claude/Get-CodeStyleCatalog.ps1` as a step run on its own. A script's
  classification by step verb reads its code, so a step named only in a comment makes no gate; a script
  that quotes runner strings as data opts out with the `GATE-CADENCE:` marker, which every script denial
  names. A package install names packages, not steps: every `pip install ...` segment, up to the next shell
  separator, is removed before steps are matched, so `pip install pytest` runs nothing while
  `pip install pytest; python -m pytest` is still a step. Setup (`npm ci`, `dotnet restore`),
  maintenance (`dotnet build-server`) and the synthetic walkers are not gate steps, and the harness pins
  each of those as must-not-fire. `jb cleanupcode` is not a gate step either: it rewrites files, so the
  record walk counts a shell call naming it as an edit, which keeps the unchanged-tree ask honest.
- **Narrowing.** A gate script whose body runs a test runner narrowed to a name or a file (`--grep`,
  `--filter-method`, `FullyQualifiedName`, a vitest or Playwright spec path, a pytest file or `-k`, an exe
  `-method`, `jb inspectcode --include` or `--project`) is denied unless its code declares the delta it is
  narrowed to: `GATE_DELTA = [...]` or `$GateDelta = @(...)`, read from code after the comment strip. Each
  entry is verified against the session's own transcripts, subagents included: a plain entry must match a
  non-markdown `Edit`/`Write` path (the same path, or a path ending in the entry at a directory boundary)
  timestamped after the session's previous gate launch, and a `plant:<file>` entry must be a file whose
  basename the script's code names beside a write call (`write_text`, `Set-Content`, `Out-File` ...). An
  unverifiable entry or an empty declaration is denied with the entry named. The declaration does not
  exempt the script from the workflow-step and unchanged-tree rules: a plant-only delta after a gate with
  no edit since still asks. CI's tier filter `--filter-trait` is not narrowing. A harness is judged by its
  command line (`--only`, `-Only`, `-Rule`), never by its body, whose fixtures quote narrowing flags as data.
  The other false-positive source a replay of real transcripts turned up is an analysis script that quotes
  runner strings as data; the line `GATE-CADENCE: gate-step-strings-are-data` anywhere in a script's body
  declares that and takes the script out of classification altogether, the same explicit exemption the shell
  gate uses for git-write strings. Comments are never narrowing: before the strip, a full Curator gate was
  denied for a comment naming `conftest.py` and let through once the comment changed.
- **Workflow steps.** A gate script is denied when its code leaves out a gate step its repo's workflow runs.
  The repos are the workspace directories its code names (`crgolden\<Repo>`) that carry `.github/workflows`.
  A step is required when it runs on its workflow's ordinary trigger, `pull_request` where the workflow has
  one and otherwise `push`: a step whose own or job `if:` keeps it off that trigger (dispatch-only, schedule,
  push-only, a `github.ref` deploy job) is not, and a schedule-only workflow requires nothing. Continuation
  lines are joined before a step is read. A step is matched in the script's code by identity,
  case-insensitively: a test tier by its `*.Tests.*` project and `Category=` trait, an npm script by its name
  as a token, anything else by its tool (`ruff check`, `mypy`, `pytest`, `vitest`, `dotnet build`,
  `stryker` ...). A step with no identity is left unchecked rather than guessed at. A harness-named script is
  exempt, because its body names repos as fixture data.
- **A checked-in gate.** A gate script whose code names a workspace repo (`crgolden\<Repo>`) that has its
  own `<Repo>/gate.ps1` is denied unless it is that file, and the reason quotes the command to run instead.
  `git status` lists only changed files, so a committed `gate.ps1` never appears there; reading its absence
  from that output as "this repo has no gate" is the mistake this rule catches. Checked before the
  workflow-step rule, so a scratch copy gets the redirect rather than a missing-step list. A harness is
  exempt, because its body names repos as fixture data; a repo with no `gate.ps1` is not redirected.
- **A gate on a clean repo.** A repo's own checked-in `gate.ps1` is denied while its checkout holds no
  uncommitted change, a gate's `StrykerOutput/` and `.sonarqube/` leftovers aside (`clean_checkout`, the same
  count as GateCommon's `Get-CleanRepoRefusal`, which refuses the run itself). Pinned in
  `test_gate_cadence_gate.py`: a committed repo denies, one holding only leftovers denies, one uncommitted
  source file lets the gate run.
- **A gate before a build.** A repo's own checked-in `gate.ps1`, in a checkout with a `.sln`/`.slnx` or a
  `package.json` build script (`needs_build`), is denied unless the session, subagents included, ran a
  foreground plain build of a path inside that checkout whose call came back without an error, and no
  non-markdown file in the checkout changed after it: neither an edit of the session nor a file git sees
  written later, a gate's `StrykerOutput/` and `.sonarqube/` leftovers aside (`build_problem`). The record
  walk keeps each build in the cached scan (`builds`, the result's timestamp as its finish). A background
  build is no proof, because its call returns before the build ends. Pinned in `test_gate_cadence_gate.py`:
  no build, a change after the build, a background build, a failed build, a build of another path and a
  source file written after the build each deny; a build after the change, and a markdown file or a
  Stryker leftover written after it, let the gate run.
- **A gate already green on these inputs.** A repo's own checked-in `gate.ps1` is denied while its gate record
  is `GREEN-CURRENT` (`Harness/Gates/gate_status.py`, `Harness/AGENTS/GATES.md#the-gate-record`): the
  last run passed on exactly the current inputs, so a re-run can only repeat it. `GREEN-STALE`, `RED`,
  `DRIFTED`, `RUNNING` and `NEVER-GATED` all let the gate run. Pinned both ways in `test_gate_cadence_gate.py`
  (a current record denies; a stale, failed or unfinished one does not).
- **A Tools gate ahead of a peer's green change.** A Tools gate launch is denied while another checkout of
  Tools (`gate_status.checkouts`) holds a `PASSED` record completed after the commit time of origin's head
  (origin/HEAD, else origin/main, as last fetched) that `gate_status.status` still reads GREEN-CURRENT on that
  checkout, so a green its session abandoned or edited past holds nobody, and that checkout's change is not on
  origin: anything
  uncommitted beyond a gate's leftovers, or a HEAD that is not an ancestor of origin's head
  (`peer_green_unlanded`). That change lands next, so the gate waits for it, then runs once on top of it. Pinned
  in `test_gate_cadence_gate.py`: a sibling's newer pass with its change uncommitted or unpushed denies; a pass
  older than origin's head, a failed record, a change already on origin and a repo other than Tools let the
  gate run.
- **A worktree's gate is its repo's checked-in gate.** `gate_checkout` reads a worktree's one-line `.git` file
  (`gitdir: <workspace>\<Repo>\.git\worktrees\<name>`), so a branch gate run from anywhere is held to the
  step-list, step-inputs, green-record and work-product rules against its own `gate.ps1` and its own state
  directory (`gate_status.checkout_state_dir`). A record whose `repoPath` names another checkout never stands
  for this one, since fleet worktrees beside each other share a state directory. The Stop gate's green-claim
  check and the parking-lot rule read the repo's newest record across its checkouts (`latest_checkout`).
- **A stopped gate.** A `TaskStop` whose `task_id` names a gate's `backgroundTaskId` marks that gate finished
  and stopped; relaunching after it asks. Stopping a gate's own task does not reliably produce a
  `<task-notification>` with a distinct status, so the `TaskStop` record is the signal.
- **A goal in force.** When the operator's `/goal` or `GOAL:` line is in force, a gate is denied unless the
  command is the checked-in `<Repo>/gate.ps1` restating the goal as a `-Goal "<goal>"` argument
  (`goal_argument`, judged by the verbatim-words rule with markdown backticks dropped from both sides; a
  single-quoted argument reads `''` as one apostrophe). A `GOAL:` comment inside a script counts for
  nothing, because any wrapper can carry one.
- **A session's own wrapper or wait script** (`script_abuse`), judged before anything else for any
  `pwsh -File` or `python [flags]` run of a script outside every workspace repo, and of a workspace-repo script
  its checkout reports modified or untracked (`uncommitted_in_repo`): a committed script is reviewed code and
  its own repo's harness holds it, while a dirty one is the session's own whatever folder it sits in; without
  that, the wait loop a session edits into a checked-in script runs while the same loop in a scratch script is
  refused. A git status that cannot be read counts as dirty. A wrapper is a script
  whose code launches a process naming a `gate.ps1` or a harness on the same line, or through a variable
  an earlier line assigned one, or, in Python, a script that launches processes and builds an argv pairing
  `sys.executable` (or a quoted `pwsh`/`python`) with one. The data marker exempts none of them. A wait
  script blocks on another process (`Wait-Process`, a native `WaitForSingleObject` or `WaitForMultipleObjects`,
  `os.waitpid`, `WaitForExit` on a process looked up by id, while waiting on a child it started is ordinary
  work: `RE_PROCESS_WAIT`), blocks on a CI run or a deployment (`gh run watch`,
  `gh pr checks --watch`, `az ... wait`: `RE_CLI_WAIT`, which also matches them written as a Python argv list),
  or sleeps (`time.sleep`, a bare `sleep(`, `Start-Sleep`, `sleep N`, `timeout /t`, `ping -n`, and the rest of
  `RE_SLEEP_CALL`). A string naming one of those waits counts; a comment does not, since comments are stripped. A
  bare command that runs one of those waits (`RE_CLI_WAIT_COMMAND`: directly, behind `&`, through a quoted full
  path or through `cmd /c`) is refused the same way, in the foreground or the background, because the CLI does
  the waiting itself. One read of the state (`gh run view`, `gh run list`, `az ... --no-wait`) is no wait. The one pause allowed is a settle
  pause (`is_settle_pause`): a single literal of at most `SETTLE_PAUSE_SECONDS` (5 s) on its own line, outside
  any loop and away from gate or session state, in a script that does something besides import and sleep. A
  script whose only work is a sleep, or whose pause is read at run time, exists to pass time.
- **A repeated status poll** (`repeated_poll`, `REPEATED_POLL_REASON`) passes time the same way: a status command
  (`RE_STATUS_POLL`: `gate_status.py`, `hook_status.py`, `GateSlot.ps1 -Status`, `Get-ScheduledTaskInfo`) is refused
  when the same command, spaces aside, already ran since the last thing that could change its answer. That is an
  `Edit`, `Write` or `NotebookEdit`, any command that is not itself a status poll, or any input the session took in:
  a user record carrying more than tool results, a `queue-operation` record, or a `queued_command` attachment (a
  mid-turn operator message). Stop-hook feedback and an auto-continuation are `isMeta` records with no input origin,
  and every other attachment (token reminders, hook results) is harness noise, so neither breaks the loop. It reads
  newest first from the transcript's last `POLL_TAIL_BYTES`, and the reason names `WAITING:` as the stop.
  Naming a gate as data runs nothing: `git` launches, command strings built as hook input, `py_compile`. The reason quotes the goal so it is in context at the
  moment the agent decides whether the work is finished. The goal is read by `goal_pin.goal_in_force` and
  compared with `goal_pin.normalised_words`, the reader the Stop gate and the goal gate use, so no two hooks
  can disagree about what the goal is. The check sits in the script, not in the prose before the call, because the prose an
  agent writes before a tool call is not in the transcript when `PreToolUse` fires; a live transcript
  showed the `tool_use` records with no text record beside them. The goal is folded into this hook's own latch
  under its scan cache directory, so each gate reads only what the transcript gained since the last one.
  **A gate that restates the goal and passes every other rule defers**, so it runs without asking once every
  declared work-product item is done and no other gate holds the machine. The restatement names the goal; the
  declaration, not the restatement, says the work product is finished.
- **A re-run with nothing changed asks** (`UNCHANGED_REASON`): no Edit or Write of a non-markdown file since this
  session's last gate, and nothing git changed in the gated script's checkout since then
  (`checkout_changed_since`: a HEAD committed later, or a changed or untracked file written later). A pull,
  merge or rebase is a change, so the gate after it runs without asking. For a repo's checked-in `gate.ps1`
  the comparison is per checkout (`same_checkout_history`): only earlier gates of that same checkout, and only
  edits inside it plus shell edits with no path, so the first gate of a worktree after a gate of the main
  checkout is not a re-run. Any other gate script compares against every earlier gate and edit.
- **A script named for a gate step.** `classify()` judges a `pwsh -File`/`python` script by its body, so a
  catalog script (`Get-CodeStyleCatalog.ps1`, `Get-CommentCatalog.ps1`), whose body names no gate step,
  would read as ordinary. A script whose own file name matches a gate-step pattern is therefore that step
  run on its own and is denied before its body is read; the harness pins it with a catalog file that exists
  on disk, since an absent path falls through to the command-line match and cannot exercise the body branch.
- **A script is a gate by its name or by what it runs, never by text it holds.** `script_entry` counts a script
  named `gate.ps1`, one whose code runs a gate step, runs a `gate.ps1` or a harness (`invoked_gate_target`: its
  name beside a runner, or a variable holding it as the run target right after the runner), runs steps
  through a variable, or loads `Gates/GateCommon.ps1` (`loads_gate_library`: dot-sourced by path, through
  `GATE_LIBRARY` or a variable assigned from either), since that library is the one writer of a gate record and
  the record, not a printed line, is what every reader trusts. An invocation script that only compares a `GATE:`
  verdict it was handed (the triage poll's request modes), names a harness as data or sets `GATE_LIBRARY` for a
  task runs no gate and is not judged as one.
- **Rules measured and not adopted.** A re-gate soon after a green gate fired on roughly half of all gate
  launches, most of them consecutive scripts that together formed one gate, so it cannot separate a
  violation from ordinary work. Treating comment-only edits like markdown fails for the tools that exist to
  check comments (the comment catalog, the doc-state harness), so edits still count unless they are
  markdown.
- **State model.** The session is its transcript plus `<session>/subagents/*.jsonl`. A launch is in
  flight while it has no `tool_result` and is younger than the foreground tool limit, or while its
  `toolUseResult.backgroundTaskId` has no `<task-notification>` naming it and its task output file exists
  on this machine and was written within the last hour. The call being judged may already sit in the
  transcript; it is excluded by `tool_use_id` when the payload carries one (the field is undocumented) and
  otherwise by matching the latest pending launch of the same command.
- **Peers.** Every other transcript under the projects directory modified within the peer window,
  subagent transcripts included. **A peer's background gate counts only while its task output file exists
  locally**: `~/.claude/projects` syncs across machines and task output under the temp root does not. A peer's
  pending foreground gate counts only while that session's temp directory
  (`<temp root>\claude\<slug>\<session>`, the root Claude Code itself uses: `CLAUDE_CODE_TMPDIR` when set, else
  `%TEMP%`) exists here, for the same reason. **That discriminator is proven by
  two harness cases of opposite sign, not by field data**: an offline replay of real transcripts found no
  cross-session overlap involving a session without a local temp directory, so the corpus never exercised
  it.
- **Deny versus ask** follows the precision rule the doc-state gate set: deny only where a hit is a
  violation by construction. An inline gate step, a second gate beside one's own and a gate beside a
  peer's live gate are violations by construction. A re-run with no edit has legitimate exceptions only
  the operator can confirm (a pull or an operator edit), so it asks. **A peer conflict denies, never asks**:
  the operator cannot see another session's processes, so an ask hands them a check only the sessions can
  make. The deny clears on its own when the peer's gate exits; its one false case, a peer whose session
  died mid-gate, lasts until that gate's output file is `BACKGROUND_STALE_SECONDS` old, and the reason tells
  the session to raise it with the operator rather than wait it out. A resumed session is not its own peer:
  resuming starts a new transcript that copies the old one's records, and a gate's completion notice lands
  only in the new one, so a launch whose tool_use id the session's own transcript also holds is judged there,
  never as a peer's gate in the transcript it left.
- **Work product.** A checked-in `<workspace>\<Repo>\gate.ps1`, or a worktree's, is judged against the session's declaration
  in `work-products/<session>.json` under `%TEMP%\claude` (`session_home.py`; `work_product_problem`; rules in
  `Harness/AGENTS/GATES.md` "Declaring the work product"). The ledger of every item read lives
  under the scan cache (`gate-cadence-cache/work-products/<session>.json`), so a removal is caught even
  though the declaration file itself is rewritten. The operator's approval of a removal is matched against
  records carrying `origin.kind == "human"`: a `user` turn with string content, or a `queued_command`
  attachment for a message sent mid-turn. The checked-in gate is judged whether it runs in full or with
  `-Tests`; a scratch gate for a repo that has one is already denied as a bypass. An item carrying
  `"plant": "<path in the repo>"` is a planted proof: `plant_in_place` reads that file for the `PLANTED`
  marker, and only the launch check passes `allow_plants=True`, so the planted run can start while every
  other item is done, a plant item claimed done with its marker still in the file is denied in every mode,
  and the Stop hook's finished-work checks give a plant no allowance.
- **The declaration itself.** `work_product_edit_problem` judges every Edit or Write of a declaration
  before it lands: a new or reworded item that names no file (`work_product_items.item_paths`, shared with the
  launch check's `names_changed_path` and with `parking_lot_commits.py`) is denied there, so the gap surfaces
  while the agent can still fix it rather than at the gate. A Write into the retired `crgolden/.work-products`
  is denied, and `script_abuse` denies a bare command, not only a script, that names the declarations folder as a
  path segment (`RE_DECLARATION_NAME`: `work-products` between separators or quotes, never the word in prose or in a
  search pattern, so a script that reports on declarations is not refused for naming them) and writes a file
  (`Move-Item`, `Copy-Item`, `Set-Content`, ...). The baseline an addition is judged against
  (`declaration_baseline`) is the first Write, joined by the first Write after each `/goal` or `GOAL:` the
  operator pinned since (`goal_pin.goal_set_time`): that Write answers the newer goal as the first answered the
  first, and every later addition still needs the operator's own words.
- **Measuring it.** Replay real transcripts in order through `classify()` and the own-session rules, and
  read the inline-command frequency list for anything that is not a gate step. The replay cannot see
  task output files, so it overstates background liveness; cap it at the stale limit before reading the
  in-flight count.

**An `api-request-blob` record is a copy, never a launch or a result.** Claude Code writes one per API request:
the request's `tool_use` and `tool_result` blocks again, with no `timestamp` and no `toolUseResult`. The scan
keys launches and results by tool-use id, so a copy read as a record replaces the real launch's timestamp and the
real result's `backgroundTaskId`, and a running background gate reads as finished: its own `WAITING:` is refused
and peers do not see it in flight. `absorb()` skips the type (`REQUEST_COPY_TYPE`).

**The gate slot is the one state this hook reads that no transcript carries.** `machine_busy` also
denies while `Harness/Gates/GateSlot.ps1`'s lock file (the account's own
`%LOCALAPPDATA%\Temp\claude\crgolden-gates\gate-slot.lock`, `claude_temp.gate_slot_root()`; gates are per account, never machine-wide or on the share) is held: the holder opens it with no sharing, so the hook's
ordinary open fails with a sharing violation exactly while a holder lives, and a crash frees it. **Only the
lock decides.** The `gate-slot.json` record beside it is written by whoever holds the slot, including the
alert-triage gate account that runs draft code, so a record alone (forged, or left by a crash) never blocks
a gate; it only names the holder in the denial. Access denied on the lock reads as held. The harness points
the variable at an empty directory for every case, because the Tools gate that runs it holds the real slot,
and holds a lock exclusively for the must-deny rows beside a forged record over a free lock that must defer.

Known gaps, pinned rather than fixed:

- **A named step is not a run step.** The workflow check reads each step's identity in the script's code,
  so a step printed in a `Write-Output` string, run with arguments CI does not pass, or skipped at runtime
  still reads as present, and a step whose command has no identity is not checked. The deferred runner in
  GATES.md closes both.
- **An edit made while this session's gate runs is not blocked.** The `Edit|Write` gates do not consult
  this state.
- **A lost completion notification** leaves a background gate in flight until its output file goes an
  hour without a write.
- **`live_peer_gate` answers "should I hold off launching", not "is a gate running".** A foreground call
  whose own PreToolUse hooks are still being evaluated and one that is executing are the same transcript
  record, a `tool_use` with no `tool_result` yet, so for those seconds any script `classify()` counts as a
  gate (one whose body merely names a gate step included) reads as live to every peer. Separating the two
  needs the process table, which this hook does not query on every call. To learn whether a gate occupies
  the machine, name the process: its PID, command line and start time.
- **A script body is judged as it is now**, not as it was when a past launch ran it, so a deleted scratch
  script drops out of the history.
- **The goal restatement is an echo, not proof.** It puts the goal in front of the agent; it cannot tell
  whether the goal is finished, the same limit the Stop hook's `DONE:` echo has. That is why a restating
  gate asks the operator instead of deferring.
- **`jb cleanupcode` inside a script body is not counted as an edit**; only a shell call naming it is.

```
python Tools\Hooks\test_gate_cadence_gate.py
```

In-process and fast enough for the foreground. Each case builds its own fixture root, because the peer
scan reads every transcript under a projects directory and shared fixtures leak live gates into later
cases. **The fixtures encode the same transcript assumptions as the gate**, so after any change to the
record walk, check the record shapes against a live transcript: a background launch's `tool_result`
with `toolUseResult.backgroundTaskId`, and the `queue-operation` record whose content carries
`<task-notification>` and `<task-id>`.

## A `PostToolUse` reminder: `Edit|Write` -> `invoke_comment_reminder.py`

Not a gate, and documented here because it shares the `Edit|Write` matcher and the harness conventions.
When an edit adds a comment line in scope, it adds a reminder to the agent's context: make the comment
compliant with `Tools/AGENTS/COMMENTS.md` now, or it will have to be removed later.

- **`PostToolUse`, not `PreToolUse`.** The hooks reference adds a `PreToolUse` hook's `additionalContext`
  "alongside the tool result", so it reaches the agent no earlier than a `PostToolUse` reminder, and
  `PostToolUse` sees the result's `structuredPatch`. The operator chose context only, so the comment is
  already written when the reminder arrives.
- **Added lines come from `tool_response.structuredPatch`.** A live transcript showed both tools' results
  carrying it, with `+` lines for additions; a `Write` that creates a file has an empty patch and a null
  `originalFile`, so all of its content counts as added. Without a patch, an `Edit`'s `new_string` lines
  absent from its `old_string` count.
- **Scope is read, never copied**: the extension map from `Tools/Shared/ScanScopeHelper.ps1`, the exempt
  repos and the four allowed patterns from `Tools/CommentAudit/Get-CommentCatalog.ps1`. The harness fails
  if either file stops parsing, which would otherwise silence the reminder everywhere.
- **Detection is by line and loose on purpose.** A false reminder costs a sentence. It does not see a
  trailing comment after anything but `;`, `{`, `}`, `)` or `,`, an HTML comment, or a docstring, and it
  skips numbered SQL migrations entirely rather than locating the end of the header.

```
python Tools\Hooks\test_comment_reminder.py
```

## A `PostToolUse` reminder: `Edit|Write` -> `invoke_doc_prose_reminder.py`

Not a gate. When an edit adds prose to a permanent markdown doc, it adds a reminder to the agent's context:
permanent docs carry no anecdote, history, narrative or status, per criterion 3 of the docs-audit skill
(`Tools/Claude/skills/docs-audit/SKILL.md`), and a line that fails its pre-write test is a violation.

- **Every added prose line fires, by design.** No detector separates an anecdote from a durable finding
  precisely enough to deny (see the doc-state gate's cut checks). The reminder costs a paragraph and blocks
  nothing, so it fires unconditionally, and quotes the definitive `prose_lint.py` tells among the added
  lines when there are any.
- **Transient docs are silent, and banking progress there is the intended use**: anything under the temp
  tree (the session scratchpad included), `~/.claude/plans`, and any `memory` directory. The status-bearing
  docs are silent too, by the doc-state gate's `EXEMPT_BASENAMES` and `EXEMPT_PREFIXES`, imported rather
  than copied. The doc-state gate's `retired` and `coverage` directory exemptions are deliberately not
  imported: those docs are permanent, and history is the defect most likely to land in them.
- **Added lines come from `invoke_comment_reminder.added_lines`**, and lines inside a fence added in the
  same edit are skipped. A fence opened outside the edited fragment is not reconstructed, so a code-only
  edit inside an existing block still reminds.
- **The harness loads the real `prose_lint.py`**, so moving or renaming the docs-audit linter fails a case
  instead of silently dropping the tells.

```
python Tools\Hooks\test_doc_prose_reminder.py
```

## Operator input interrupts nothing

**No hook denies a tool call because the operator typed.** A message the operator sends mid-turn rides a
tool result and is answered in the turn; a slash command waits in the input queue until the turn ends.
Denying every tool while either waits turns any keystroke into a forced stop, and the Stop hook then has
to exempt that stop, which lets its reply through unread ([STOP-HOOK.md](STOP-HOOK.md)).

## A peer waiting on a reply: every tool -> `invoke_peer_message_gate.py`

**While a peer session's question or machine handoff has had no reply, every tool call except the reply is
denied.** A session absorbed in its own sequence otherwise treats peer messages as something to handle at a
natural break, and the peers sit idle with the machine free.

| Situation | Decision |
|---|---|
| A peer message holding a `?` outside its backtick code spans, or handing over the machine ("the machine is yours", "go ahead", "you're next"), with no `SendMessage` to that peer since; "the machine is free" is a broadcast that asks nothing, and a `?.` inside code is an operator | deny every tool but `SendMessage`, `ListAgents` and `ToolSearch`, quoting it |
| A peer notice that asks nothing | defer |
| A transcript the hook cannot read | defer |

- **A peer message is a record no session writes for itself**: a `user` record with `origin.kind == "peer"`,
  or a `queued_command` attachment with that origin, carrying `<cross-session-message from=... from-name=...>`.
- **A `SendMessage` whose `to` is the sender's address, its name, or its name with a ` [ref]`** answers
  every earlier message from that sender. The attempt counts whether or not it is delivered, so a peer
  whose session has ended is cleared by one reply attempt and can never lock a session out. An operator turn
  answers nothing.
- **The Stop hook's `block:peer-owed` veto is the other half**: a turn cannot end while a reply is owed,
  so ending it is not a way around answering.
- **The scan resumes from a cached byte offset** under `<temp root>\claude\peer-message-cache` (`claude_temp.py`:
  `CLAUDE_CODE_TMPDIR` when set, else `%TEMP%`), keyed by
  transcript path and invalidated when the file shrinks or its first 4 KB change, and its pre-filter passes
  only lines naming `cross-session-message` or `SendMessage`.

**Proving it discriminates takes real transcripts**: cut a session where a peer handed over the machine and
was not answered, and it must deny there and defer once the reply is in the transcript.

```
python Tools\Hooks\test_peer_message_gate.py
```

## The dispatcher: `invoke_hook_dispatcher.py`

One process per hook call instead of one per gate. Registered once per event, it runs every `PreToolUse` gate
and every `PostToolUse` reminder in the table at the top in-process, through the same functions each gate's
own `main()` calls, so every gate keeps its harness.

| Event | Matcher | `args` | `onFailure` | `timeout` |
|---|---|---|---|---|
| `PreToolUse` | `*` | `["-c", LAUNCHER]` | `"block"` | unset (Claude Code's default) |
| `PostToolUse` | `*` | `["-c", LAUNCHER]` | unset | unset (Claude Code's default) |

- **Why one process.** Every gate's verdict is merged into one decision with one precedence and one fail-closed
  policy, which separate hook processes cannot give. It is not a speedup by itself: on a busy machine the
  scheduler shares CPU per process, so three gate processes win more of it from CPU-bound work than one does,
  and the single process only matches them by doing less work per call. Judge a change by benching the shapes
  side by side in one interleaved run, idle and with every core busy; a figure from another run, machine or gate
  set is not a baseline.
- **A `python -c` program is content-scanned at every process start**, so its cost grows with what the text
  says, not only its length, and a file it runs is not rescanned that way. The registered `LAUNCHER` is
  therefore a short bootstrap that runs `Hooks\launch_dispatcher.py`, which holds the fail-closed logic below;
  the harness caps the bootstrap's length.
- **Routing reproduces the matchers.** The peer gate and the goal gate judge every tool; the shell and cadence gates judge
  `PowerShell` and `Monitor` (`SHELL_TOOLS`), since a Monitor runs a shell command of its own, which otherwise
  reaches the operator as a permission prompt instead of a refusal; the agents-doc and doc-state gates judge `Edit` and `Write`; the main-checkout gate judges
  `Edit`, `Write` and `NotebookEdit`; the read-scope gate judges `Read`, `Grep` and `Glob`, so the dispatcher's
  `PreToolUse` registration matches every tool. The dispatcher owns gates and hooks only, and the harness pins
  its list to exactly these, so a gate is added only by an approved change to that row. `Edit|Write` is an exact-name
  matcher (hooks reference, "Matcher"), so routing by exact tool name changes nothing.
- **Precedence is the host's.** Deny over ask over allow, every reason of the winning decision kept.
- **It fails closed.** A gate that cannot be imported, raises, or returns something that is not a verdict
  turns the call into a deny that names the gate and quotes the traceback, and tells the agent to end the turn
  with `NEEDED:`. A gate's own out-of-scope defer is a verdict, not a failure. The reply tools (`SendMessage`,
  `ListAgents`, `ToolSearch`) are the one carve-out, so a broken gate cannot cut a session off from its
  peers; the harness pins that tuple equal to the peer gate's own.
- **It keeps no clock.** A live call waits for every gate; a gate that hangs is bounded only by Claude Code's
  hook timeout, which the entry's `onFailure: "block"` turns into a block (Failure behavior above). `run()`
  takes a deadline only so a harness can plant a gate that never returns without hanging itself, and the
  harness pins that every live entry point defaults it to none. The registration check fails a `PreToolUse`
  entry without `onFailure: "block"` and any entry that sets a `timeout`.
- **`PostToolUse` cannot undo an edit**, so a failing reminder is surfaced as a `systemMessage` to the
  operator and as context to the agent rather than vanishing.
- **The launcher is the only part a stale checkout cannot lose.** A hook that cannot start is a non-blocking
  error and the call proceeds (hooks reference, "Exit codes"), and `settings.json` syncs to every machine
  while `Tools` does not, so a checkout behind origin runs the synced hooks against gate files it lacks, and
  each missing gate passes every call. So `launch_dispatcher.py` reads stdin first and wraps the dispatcher's
  load: when the dispatcher is missing, does not load or exits non-zero it denies every tool call except the
  reply tools, and exits 0, never 1 (non-blocking) or 2. The `LAUNCHER` bootstrap registered in `settings.json`
  denies the same way when `launch_dispatcher.py` itself is missing, parsing the payload so that only a real
  reply-tool call passes. No
  repair command is exempt: a pull runs the checkout's git hooks, which would not pass through the gates that
  failed to load. The deny names the commands the operator runs outside Claude Code: put the `Tools` checkout
  back on `main`, then pull it.
- **Live hooks run from the Tools checkout on `main`.** `LAUNCHER` loads the dispatcher from
  `Hooks` under `%USERPROFILE%\<CLAUDE_TOOLS_ROOT>`: settings.json's `env` block sets `CLAUDE_TOOLS_ROOT` to the
  Tools checkout's path relative to the profile, since Claude Code copies an `env` value as written and the
  profile differs per machine. With it unset, the launcher denies like a missing launcher file. `main` changes only through a pull request the operator
  merges (Tools/AGENTS.md), so a change is made and gated on a branch in a separate worktree and judges nobody
  until it is merged and pulled. Every gate finds the workspace from `USERPROFILE`, never from its own path, so a
  hook run from a worktree judges the real workspace.
- **SessionStart runs from the checkout too.** `SESSION_START_COMMAND` (registered as
  `pwsh -NoProfile -NonInteractive -Command <it>`) runs the checkout's `Hooks\Invoke-SessionStart.ps1`.
  SessionStart cannot block, so when the script is missing or throws it reports to both readers instead: a
  `systemMessage` to the operator, and context telling the agent the Stop-gate rules are not loaded and where to
  read them.
- **`invoke_main_checkout_gate.py` keeps the shared Tools checkout out of a session's reach.** It denies an
  `Edit`, `Write` or `NotebookEdit` of any path in the checkout on `main` (resolved, so a skill edited through a
  skills junction counts), pointing to the session's branch worktree, and an `Edit` or
  `Write` after which `settings.json`'s `hooks` block differs or the file no longer parses, and, as a gate change
  that ejects, a file-tool write under the Stop gate's state directory (`claude_temp.stop_gate_state_root()`) or
  the hooks' transcript index (`claude_temp.transcript_index_root()`), whose edited offsets would hide a line from
  every reader using it. Shell commands are not judged here: running a gate's harness from the checkout is ordinary
  work, and text cannot tell a read from a write. The shell rules on that state are `invoke_pretooluse_gate.py`'s:
  a command or script body naming `claude-stopgate` or `transcript-index` is denied.
  The operator's review of the pull request, not this gate, is the protection.
  `python Tools\Hooks\test_main_checkout_gate.py`.
- **No tool runs without a goal.** `invoke_goal_gate.py` denies every call but the reply tools and
  `AskUserQuestion` until a goal is in force: the operator's `/goal`, else their newest `GOAL:` line, read
  by `goal_pin.goal_in_force` (STOP-HOOK.md, "The goal is the operator's"). The session must open with one,
  before its third operator message; `GOAL: none` retires a text goal and the session is refused again. The Stop
  gate holds every earned stop to restating the goal (`stop-gate-rules.txt`, "The goal").
- **With a goal in force, the session-start steps come before any work.** `invoke_goal_gate.session_start` reads
  the newest `hook_additional_context` attachment whose `hookEvent` is `SessionStart`. When its content names the
  file Claude Code saved it to ("Full output saved to: <path>"), every call but the reply tools and a `Read` of
  exactly that path is denied until a `Read` of it, made after that attachment, comes back without `is_error`; a
  compaction or resume adds a new attachment whose file is read again. Then, until
  `%TEMP%\claude\parking-lot\<session>\state.json` records a URL, only the setup calls run: `Read`, `Skill`,
  `Artifact`, `ArtifactData`, and a PowerShell command opening `python ...parking_lot.py render|record-url|show` or
  `Get-Date`. A SessionStart attachment older than the goal gate file's modification time is held to neither step,
  and a transcript with none (a fixture, the triage runner, which has its own hooks) is unaffected. The dispatcher
  passes `tool_input` to the goal gate for this. Pinned by the `start` and `page` rows of `test_hook_dispatcher.py`.
- **The eject fires only on an attempt to change the gate that judges the session.** Before any gate runs,
  the dispatcher halts a session that has a quarantine record (`session_quarantine.py`) with `continue: false`
  and a deny, so it cannot delete its own record. It writes that record when a `PreToolUse` call is a gate change
  (`invoke_main_checkout_gate.gate_change`): a file-tool write to a quarantine record, the Stop gate's state or the transcript index, or an `Edit` or `Write`
  after which `settings.json`'s `hooks` block differs or the file no longer parses. An edit to a main-checkout
  gate or hook is denied but never ejects. Nothing about how a session writes or stops ejects it. The record lives
  in a folder the same account can write, so it holds against a careless session, not a determined one.
- **When a repair command fails, only the operator can repair it.** A checkout behind origin because GitHub
  authentication is failing leaves the session nothing it may run but a pull that fails. Both deny reasons
  say so and tell the agent to end the turn with `NEEDED:` instead of retrying.
- **Switching order.** `settings.json` syncs to every machine, so it names `Hooks/` only after the pull request
  is merged; a machine that has not pulled yet denies every tool until it does, and the deny says to pull. Until
  the switch the harness reports registration as `SKIPPED`, never `PASS`.

```
python Tools\Hooks\test_hook_dispatcher.py
```

In-process and fast enough for the foreground, except the launcher rows, which run the real `LAUNCHER` text
in a child against a fixture `USERPROFILE`. Before trusting a green run after a change to the merge or the
failure policy, mutate it (reverse `PRECEDENCE`, swallow failures, widen `REPLY_TOOLS`) and watch the stub
rows fail.

## The protected-path gate: `Edit|Write|NotebookEdit` -> `invoke_protected_path_gate.py`

**The master session never makes a file-tool write that no allow rule can pre-approve.** Claude Code never
auto-approves a Write, Edit or NotebookEdit to a protected path, whatever `settings.json` allows (an
`Edit(~/.claude/**)` rule and an `additionalDirectories` entry included; permission-modes docs, "Protected paths"),
while reads there pass the allow rules and `.github` is an ordinary folder. The protected paths are the folders
`.claude`, `.git`, `.config\git`, `.vscode`, `.idea`, `.husky`, `.cargo`, `.devcontainer`, `.yarn` and `.mvn`, and the
files in `PROTECTED_FILES`: git, shell and package-manager startup files, hook-runner configs, `.mcp.json` and
`.claude.json`, plus a worktree's `.git` file. A prompt in the master waits for good, since nobody answers it, so for
a session `master_session.runs_as` names the master the gate denies such a write in any spelling of its path (as
given, long, resolved through a junction). It lets through the `.claude` exceptions a call can be checked against:
Claude's own worktrees under `.claude\worktrees`, and a markdown file in the calling session's own project memory
folder, the one its transcript sits in; memory markdown of another project still prompts. The other documented exceptions belong to the current session's own plan file or background-job scratch,
which a call does not show, so they stay denied: a deny never hangs the master. For `.claude` the reason names a
script run through PowerShell; for the rest, whose files can run as code or change how a tool runs later, it says to
leave the write out and report it for a session the operator attends, since no route may stand in for that approval.
Every other session is left to the normal permission flow, where the operator answers.

## The read-scope gate: `Read|Grep|Glob|EnterWorktree` -> `invoke_read_scope_gate.py`

**A file tool never reads a path no allow rule reaches, nor the Harness repository; it reads everything else.** A
read outside the working directories and the allow rules is an approval prompt that freezes the session until the
operator answers, and a read of Harness is a map of the session's own exits. The gate is a blocklist of exactly those
paths, never a list of what may be read, so a new folder under `%TEMP%\claude` needs no change here:

- **A path in the Harness repository** (`harness_scope.py`: `<workspace>\Harness` or a worktree of it, in any spelling,
  a junction that resolves into it included) is denied for every session but the master (`MASTER_SESSION=1` on the
  master host). The skills stay in Tools and are read there. **`EnterWorktree`** is judged here too: entering a
  Harness worktree by `path`, or creating one by `name` from a working directory in Harness, is the master's alone.
  The rows are `harness_rows`, against a fixture Harness with a junction into its Hooks.

- **A temp path outside `%TEMP%\claude`** (another program's files, the temp folder itself): denied, in its long,
  realpath and forward-slash spellings, from `tempfile.gettempdir()`, `TEMP`, `TMP` and the profile's own `Temp`.
  `Read(~/AppData/Local/Temp/claude/**)` and `Read(~/AppData/Local/Temp/*/claude/**)` in `~/.claude/settings.json`
  reach every file under `<Temp>\claude` and `<Temp>\<n>\claude`, so every session reads every file there: its own,
  another session's, the gate records, the audits, the handoffs, the quarantine records and the Stop gate's own
  decisions (`claude-stopgate\decisions.log`), which is how a session finds a bug in a hook.
- **The state share by its UNC spelling** (`AGENTS/TOOLING.md`, "The state share"): denied for every account, enabled
  or not, since `Read(//z/**)` and `Read(//d/crgolden-state/**)` match only the drive and server-disk spellings. The
  reason names the server's disk path on the server, which maps no `Z:`, and `Z:` elsewhere. Through those, every
  machine's folder on the share is readable.
- **An 8.3 spelling** (`C:\Users\ADMINI~1\...`) is the short-path gate's, below, a Glob pattern's literal folder
  included. Every script names the long form.

Writes are not this gate's: the main-checkout gate refuses a file-tool write under the Stop gate's state, and the
declaration changes only through Edit or Write. With `CLAUDE_CODE_TMPDIR` naming a machine-wide root, the SessionStart
hook sets its ACL to the running account, `Administrators` and `SYSTEM` with inheritance cut.

- A shell command is not a file tool and defers; `Get-Content` on an allow-listed prefix does not prompt.

```
python Tools\Hooks\test_read_scope_gate.py
```

## The short-path gate: `Read|Edit|Write|NotebookEdit|Grep|Glob` -> `invoke_short_path_gate.py`

**A file tool names a path in its long spelling.** Claude Code matches a permission rule against the path as the
tool names it, so `Edit(~/AppData/Local/Temp/claude/**)` matches `C:\Users\Administrator\...` and not
`C:\Users\ADMINI~1\...`, the spelling `TEMP` carries on a profile whose name is longer than eight characters. A
session that builds a path from `$env:TEMP` gets the short spelling, and the call would prompt. The gate denies a path, or
a Glob pattern's literal folder before its first wildcard, holding an 8.3 segment (`NAME~N`) that `claude_temp.long_path` expands to a different spelling, and the reason names
the long spelling, so the retry matches the rule and nobody is asked. A tilde segment with no different expansion,
and every shell command, defer.

```
python Tools\Hooks\test_short_path_gate.py
```

## The push gate: `PushNotification` -> `invoke_push_gate.py`

**A push asks the operator something new, once.** A push reaches the operator's phone and desktop, so a status,
a running gate or an idle cycle is noise they must read and dismiss. The gate denies a push repeating an earlier
push's text (case and spacing ignored), and one naming no question id (`qN`) absent from every earlier push of the
session and no landing or decision for the operator (commit, push, merge, approve, answer, decide, choose, sign in,
run). A push naming only question ids an earlier push named is denied unless it asks a landing (commit, push,
merge), since a pushed question is answered on the page, not re-asked. The Stop gate's master rule asks for exactly
the pushes this one lets through: each parked question, once.

```
python Tools\Hooks\test_push_gate.py
```

## The artifact gate: `ArtifactData|Artifact|Edit|Write` -> `invoke_artifact_gate.py`

**An agent never answers a parking-lot question, and uses the page only as it is built to be used.** An agent
writes a page's store as the owner, exactly as the operator does, so the store cannot tell their writes apart. The
operator answers with the page's own controls in the browser, which no hook sees; an agent writes through tool calls,
which this gate sees first. So every agent write to a recorded page (any session's `parking-lot/<session>/state.json`
URL) is held to the page's shape, and none may set, change or remove an answer:
- **Questions:** no `answer` or `answeredAt` from an agent, in `data`, `file_path`, a `batch` entry or a
  `str_replace`; a new question is `qN-slug`, `status: "open"`, with its context, recommendation, options,
  `askedBy` and a measured `askedAt`; an update sets status only to `withdrawn`, with a `withdrawnReason`; no
  question is deleted.
- **The rest of the page:** a ledger entry is `NN-slug` with a measured `at`, `summary` and `evidence`, and is never
  deleted; a commit message is exactly the object `parking_lot.py commit` wrote, or the landed copy
  `parking_lot.py committed` wrote (`<key>.landed`), which folds the card; `session/goal` carries its fields; no
  other collection is written.
- **The write consent is Claude Code's, not this gate's.** In a turn the operator's own message did not start (a
  task notification, a peer message, a Stop-hook continuation), Claude Code asks once for consent to edit artifact
  data ("Approving covers data edits to any artifact for the rest of this session"), with
  `requires_user_interaction` and `suppress_always_allow_rule` set, so no allow rule or hook grants it.
- **Data goes inline, never as `file_path`.** Claude Code asks the operator to approve every store write that reads
  a local file, one at a time, whatever the allow rules say; inline data asks nothing. So every write to a page
  is refused with `file_path`, and a commit message is sent as the object `parking_lot.py commit` prints.
- **The page itself:** a publish is the rendered template unchanged (`rendered_template`), with no assets, and the
  page is never deleted; no file tool edits a page's `state.json`, and `parking_lot.py record-url` refuses to
  replace a recorded URL.

**Page answers count as the operator's words.** `page_answers` returns every answered question this session read
back from a page it published itself (the URL comes from Claude Code's own publish result, never a file a session
writes), leaving out any question this session ever wrote an answer to. `invoke_gate_cadence_gate.operator_texts`
adds them to the operator's chat messages, so a work-product approval, an acceptance check or a dropped item can
quote a page answer exactly as it quotes a chat message. The rules for agents are the artifact-usage skill, which
the SessionStart hook puts in every session's context.

```
python Tools\Hooks\test_artifact_gate.py
```

## The overlap gate: `Edit|Write|NotebookEdit|PowerShell` -> `invoke_overlap_gate.py`

**No session changes a Tools file another session is changing without telling it first.** Each session makes every
Tools change in its own worktree (`%USERPROFILE%\source\worktrees\<session-id>\Tools`), so two sessions can hold
uncommitted edits to the same file and learn of it only when one rebases onto the other. The gate reads `git status`
of every other worktree there and refuses:
- an edit of a file another worktree holds uncommitted;
- a `git worktree add` while another worktree holds any uncommitted change, so a session's first worktree costs
  one `SendMessage` to each peer whose Tools worktree is dirty;

until this session has sent a `SendMessage` whose message names that worktree's session id (its first eight
characters). A session id never changes, while a session's display name does, so the id is what names a peer. A
session's own worktree never blocks it.

```
python Tools\Hooks\test_overlap_gate.py
```

## The session-name gate: `SendMessage|ArtifactData|PushNotification|PowerShell` -> `invoke_session_name_gate.py`

**A session refers to and messages another session by its session id, never by its name.** A session name
(`crgolden-` plus two hex characters) is not unique, since two live sessions on one machine can carry the same one,
and a restart or reset gives a session a new one, so a name written into a message or a row reaches the wrong session
or none. The session id is unique and permanent, so audits, worktrees, gate records and declarations key on it. The gate denies a SendMessage whose `to`, `message` or `summary` holds a session name, an ArtifactData write
(`set`, `update`, `str_replace`, `batch`) whose doc id or data holds one, a PushNotification holding one, and a
PowerShell command running
`Send-SessionPoke.ps1` that holds one or passes `-CallerName`. Its refusal resolves each name to the session ids that
carry it now, from the session registry (`~/.claude/sessions`). A crgolden name that is no session's (an app such as
`crgolden-curator`, a folder such as `crgolden-gates`, the workspace) passes, and so does every read.

```
python Tools\Hooks\test_session_name_gate.py
```

## Checklist when changing the hook

1. Run `python Tools\Hooks\test_pretooluse_gate.py` in the background; expect all cases to pass. For a
   change to the dispatcher or to how any gate is called, run `python Tools\Hooks\test_hook_dispatcher.py`. For a
   change to either `Edit|Write` gate run `python Tools\Hooks\test_agents_doc_gate.py` **and**
   `python Tools\Hooks\test_doc_state_gate.py` as well as the shell-gate harness — any `settings.json`
   edit touches all three, and the shell harness is what proves the file still parses. For a change to
   the gate-cadence gate run `python Tools\Hooks\test_gate_cadence_gate.py` too, and for the comment
   reminder `python Tools\Hooks\test_comment_reminder.py`. The doc-prose reminder imports from both the
   comment reminder and the doc-state gate, so a change to either also runs
   `python Tools\Hooks\test_doc_prose_reminder.py`.
2. Confirm `settings.json` still parses — the harness throws if not.
3. Spot-check live **after a delay**, never immediately after an edit.
4. Add harness cases for anything from the gap list you close.
5. Re-run `pwsh -File Tools\Hooks\Measure-HookLatency.ps1` if you added work to a hook or changed what is
   registered.
