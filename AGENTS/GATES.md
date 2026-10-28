# Gates

The owning document for local quality gates: what a gate is, when one runs, when one does not, and how
sessions on one machine avoid running two at once. Other docs link here rather than restating any of it.

## What a gate is

- **A gate step** is any command that reads the working tree to decide pass or fail: a lock-file check,
  a format check, lint, a type-check, a build, any test tier, coverage, a mutation run, the code-style or
  comment catalog, the ReSharper inspection (`jb inspectcode`), a Sonar scan, a regression harness
  (`test_*.py`, `Test-*.ps1`). **A plain build of the repo is not a gate step: it is the proof a gate needs**
  ([Build before the gate](#build-before-the-gate)). Setup is not either: `npm ci`, `dotnet restore`,
  `dotnet tool restore`, a Playwright browser install. Neither is `jb cleanupcode`, `ruff format` or an
  import-only `ruff check --fix`, which rewrite files and are edits
  ([Editing tools](#editing-tools-are-not-gate-steps)).
- **A gate** is one invocation of one script that runs every gate step in the repo's CI list, in CI
  order, and ends with a single line: `GATE: PASSED` or `GATE: FAILED`.
- **The gate and CI are one step list, in both directions.** A subset misses what CI will catch; a
  superset reports failures CI cannot see. A check worth running locally is added to the workflow, and a
  workflow check is run locally; the only steps a gate leaves out are delivery steps (pack, publish,
  upload, deploy, lock-file regeneration), which check nothing, and the only steps it adds are the two
  catalogs ([The gate record](#the-gate-record)).

## Every repo carries its own checked-in gate

**Each repository has one gate, `gate.ps1` at its root, committed with the code it checks.** A gate that
lives in a session's scratchpad is rewritten by the next session and can be neither reviewed nor refined;
the checked-in one is the single definition of the repo's local step list, changed in the same commit as
the workflow step it mirrors.

- **Run it as** `pwsh -File "<workspace>\<Repo>\gate.ps1"`, with no step list: the gate works out which steps
  are owed itself ([Re-running after a red or incomplete gate](#re-running-after-a-red-or-incomplete-gate)), adding `-Goal "<the pinned goal, verbatim>"`
  when the operator has pinned one: the cadence hook reads the goal from that argument, since a checked-in
  script cannot carry a session's goal ([PRETOOLUSE-HOOK.md](../Hooks/PRETOOLUSE-HOOK.md)). Quote it
  with double quotes, so an apostrophe in the goal needs no escape (double each double quote inside it), and leave
  out any backtick, semicolon, ampersand or pipe the goal contains: the PreToolUse hook refuses a command carrying
  one, and the goal is compared by its words, ignoring punctuation.
- **The shared helpers load through `Harness/Gates/GateCommon.ps1`**, the library's loader, which every app gate dot-sources through the
  `GATE_LIBRARY` user environment variable, so a public repo names no path to the private `Tools` repository.
  `Tools\Configuration\Set-GateLibrary.ps1` sets it once per machine, and a session started before that run does
  not see it; the triage runner sets its own per gate. Unset, a gate prints `GATE: FAILED` naming the variable and
  exits before it writes a gate record, so it never reads as green. They supply the fail-fast rows, the `NOT REACHED` listing, the TRX and SARIF checks and the delta carry;
  the gate supplies the steps, each named literally in its code. A gate whose steps are independent harnesses
  runs them through `Get-StepRunOrder`: the steps whose declared inputs the change touches (HEAD beyond origin's
  default branch, plus every dirty file) go first, so a fail-fast on an untouched, inherited red step still
  leaves the session's own steps judged and recorded; a gate whose steps depend on one another keeps its order.
  Each library module has a harness that loads it (`Harness/Gates/test_gate_common.py` for the bookkeeping, a
  `Test-Gate<Module>.ps1` beside each checker module), and a change to a module is gated by its harness with a
  planted defect.
- **Nothing machine- or session-specific is committed**: paths are relative to `$PSScriptRoot`, reports and
  plant output go in the folder `Get-GateOutputDirectory <Repo>` returns (`%TEMP%\claude\crgolden-gates\<Repo>`,
  under `CLAUDE_CODE_TMPDIR` when set), verdicts in the checkout's gate records folder
  ([The gate record](#the-gate-record)), and the file carries no `GOAL:` line and no comment header; the step
  dispositions live in this document and the repo's `TESTING.md`.
- **A repo without a `gate.ps1` has no local gate yet**, which is a gap to close, never a license to gate
  from a script written for the occasion.

| Repo | Gate |
|---|---|
| Churches | `Churches/gate.ps1` |
| crgolden.com | `crgolden.com/gate.ps1` |
| Curator | `Curator/gate.ps1` |
| Directory | `Directory/gate.ps1` |
| Functions | `Functions/gate.ps1` |
| Identity | `Identity/gate.ps1` |
| Infrastructure | `Infrastructure/gate.ps1` |
| Inventory | `Inventory/gate.ps1` |
| Librarian | `Librarian/gate.ps1` |
| Manuals | `Manuals/gate.ps1` |
| Products | `Products/gate.ps1` |
| Shared | `Shared/gate.ps1` |
| Tools | [gate.ps1](../gate.ps1) |

## The gate record

**A repo's gate result is read from its record, never recalled.** "Nothing changed, so no re-run" and "ready to
check in" are claims about a specific run and a specific tree, and only a record of both can support them.

- **Every gate writes `<Repo>.<COMPUTERNAME>.last.json` in its checkout's records folder**,
  `%TEMP%\claude\crgolden-gates\records\<checkout key>` (`CLAUDE_CODE_TMPDIR` in place of `%TEMP%\claude` when set),
  the key being the checkout's full path lowercased with every character but a letter or digit turned into `-`
  (`claude_temp.gate_records_dir`, `Get-GateRecordsDirectory`). The main checkout and each worktree have their own
  folder, and `gate_status.py` reads the newest record across a repo's checkouts. A record whose name carries no
  machine is never read. A gate given a state directory under any `.claude` folder fails before it writes. A triage
  gate (`GATE_RUN_KIND` set) keeps its state in the `gates` folder beside its checkout (`worktrees\agent\gates`),
  where the triage account owns it ([TRIAGE.md](TRIAGE.md)). It is written at `Initialize-GateState` with verdict
  `RUNNING`, again when each step starts (`currentStep`, `currentStepStartedAt`) and after each row, always through
  a temporary file and a rename with the gate's `pid`, and finally at `Complete-Gate` with `PASSED`, `FAILED` or
  `DRIFTED` (an input changed while it ran).
  It holds every step's row (`PASS`, `FAIL`, `CARRIED`, `SKIPPED`, `NOT RUN`, `NOT REACHED`), the fingerprint, and
  a per-file hash manifest of exactly the inputs the fingerprint covers. A gate killed before it completes
  leaves `RUNNING`, so an old pass can never outlive a newer run that did not finish.
- **A landing is written into the global record.** Once a change is checked in and pushed, the repo's main
  checkout is pulled and `python Harness/Gates/gate_status.py --record-landing <Repo>` writes verdict `LANDED`, the
  landed sha and when into the main checkout's record, refusing unless that checkout is at origin's HEAD; the
  Stop hook's merged-branch cleanup names it after `worktree remove` and `branch -d`. It reads `CURRENT-IMPLICIT`
  while that checkout is clean, `NEVER-GATED` naming any uncommitted file a peer left there, and grants no carried
  step. A gate running on the old tree, in the main checkout or a worktree, reads that record
  before each step and at completion (`Get-GateSupersedingLanding`) and stops as `GATE: SUPERSEDED`, slot released,
  never overwriting the landing: its verdict would describe a tree main has moved past. A `RUNNING` record on a
  checkout that is clean at origin's HEAD reads `CURRENT-IMPLICIT` with no landing written, since a gate never runs
  on a clean tree.
- **A running gate is followed through its record, never through a process id or a log.** `gate_status.py` prints
  a RUNNING gate's finished rows and its step in flight. To be told as it goes, start Claude Code's Monitor on
  `python Harness/Gates/watch_gate.py <Repo>` (or `--record <path>` for a triage gate's record in the gate set): it
  prints a line when a step starts and one per finished row, and exits on the verdict, or with exit 2 when the
  record is still RUNNING but its gate process is gone. It runs beside the session's work and never blocks it,
  which is what separates it from the forbidden wait loop.
- **`python Harness/Gates/gate_status.py [Repo ...] [--json]` is the reader**, and the only one:

  | State | Meaning |
  |---|---|
  | `GREEN-CURRENT` | the last gate PASSED on exactly the current inputs |
  | `CURRENT-IMPLICIT` | no uncommitted change and HEAD is origin's default branch HEAD: checked in and pushed, which stands in for a gate run and reads as `GREEN-CURRENT` |
  | `GREEN-STALE` | it passed, then inputs changed; the changed files are listed |
  | `RED` | the last completed gate failed |
  | `DRIFTED` | an input changed while the last gate ran |
  | `RUNNING` | the last gate started, has not completed, and its gate process is alive (or the record is another machine's, which this one cannot check); on a clean checkout at origin's HEAD it reads `CURRENT-IMPLICIT` |
  | `STOPPED` | the last gate started and its gate process on this machine is gone with no verdict: killed or stopped. Nothing is in flight; its next step is a fresh gate run |
  | `SUPERSEDED` | the last gate stopped because its repo landed on main while it ran |
  | `NEVER-GATED` | no record, or a landing with uncommitted changes made since |

  A passing record still lists any step that did not pass (`SKIPPED`, `NOT RUN`), so a green repo with a tier
  that could not run says so.
- **"Green", "ready to check in" and a `DONE:` naming a repo each cite `GREEN-CURRENT` or `CURRENT-IMPLICIT`**
  from that output. The Stop hook blocks such a claim for a repo whose record says anything else, and the cadence
  hook denies re-running a checked-in gate whose record is `GREEN-CURRENT` ([PRETOOLUSE-HOOK.md](../Hooks/PRETOOLUSE-HOOK.md),
  [STOP-HOOK.md](../Hooks/STOP-HOOK.md)).
- **A gate never runs on a repo with no uncommitted changes.** GateCommon's `Initialize-GateState` prints
  `GATE: NOT RUN (...)` and exits before it takes the slot or writes a record, and the cadence hook denies the
  launch. A gate's own `StrykerOutput/` and `.sonarqube/` leftovers do not count as changes. A change is gated
  while it is uncommitted; once checked in and pushed it reads `CURRENT-IMPLICIT`.
- **The fingerprint includes the hashes of the tools a gate runs from `Tools`** (every `Gates/Gate*.ps1`,
  `ScanScopeHelper.ps1` and both catalogs, recorded under `tools`), so a change to any of them makes every
  record `GREEN-STALE`. Gating again then owes only the steps whose own fingerprints took the changed file (a
  catalog step its catalog, a step its checker module), and carries the rest.
- **Every gate's first step is `Current with origin`**, run by `Invoke-CatalogSteps` right after
  `Initialize-GateState`. It fails when HEAD is behind the branch `origin/HEAD` names, so a gate never reports
  on code upstream has already changed, and it reads the default branch from `origin/HEAD` rather than assuming
  `main` (Inventory's is `master`). An operator run fetches first, with git prompts off, and fails on a fetch
  error rather than passing on stale refs; either run kind fails on a missing `origin/HEAD`. Under `GATE_RUN_KIND=triage` it does not
  fetch, since the gate account cannot write `.git`; it compares against the refs the poller fetched when it cut
  the job's branch. HEAD ahead of origin passes. It never carries a pass, because origin moves outside the
  repo's fingerprint. Before it reads its inputs, an operator run catches a behind checkout up itself, on the
  default branch or on the session's own branch (named by `CLAUDE_CODE_SESSION_ID`, as in
  `Tools/AGENTS.md`): it fast-forwards when HEAD holds nothing origin lacks, clean or with unstaged edits that
  git keeps, and moves a clean session branch to origin when every commit on it is already there as a patch (a
  squash-merged pull request). Staged changes, a local file the merge would overwrite (named in the row) and
  real unmerged commits stop the step with the remedy.
- **The second step is `Dependency order`**. A shared package is one the repo's own `.npmrc` routes to a
  non-npmjs registry by its scope, or its `NuGet.Config` `packageSourceMapping` routes to a source other than
  nuget.org; its producer is any workspace folder whose `package.json` `name` or `.csproj` `PackageId` names it.
  The step fails when that producer is at a newer version than the repo pins, or when no producer is in the
  workspace, so a consumer is never gated against a pin its next bump invalidates: gate and publish the package, bump the
  pin, then gate the consumer once. It never carries a pass, because the package's version file is outside the
  repo's fingerprint. A pin newer than the local package passes, since that is a lagging local checkout.
- **Then come `Code-style catalog` and `Comment catalog`**, each scoped to the repo with `-Repo`. A row whose disposition is not
  `keep` or `exempt` fails the step and lists the first twenty, so a record that is `GREEN-CURRENT` also says
  the repo holds no open catalog row. They are the one exception to the gate matching CI: the catalogs live in
  the private `Tools` repository, which no app workflow can check out.

## The step list comes from the workflow

- Build it from the `run:` steps of the repo's workflow under `<Repo>/.github/workflows/`, never from
  memory or from the commands you happen to know. The tell is reaching for a plausible set instead of
  opening the file.
- Every `run:` step is either in the gate or a delivery step: pack, publish, push, deploy,
  deployment-package assembly, and anything aimed at production (migrations, secret checks). Anything
  else missing from the script is a dropped step, and a dropped step leaves no trace: every step that did
  run still prints green. A step that needs a credential is not excluded for it: the injected environment
  carries `SONAR_TOKEN` and `STRYKER_DASHBOARD_API_KEY` ([TOOLING.md](TOOLING.md)).
- **Sonar runs locally under `sonar.branch.name=branch-local-<machine>`** with the workflow's own `/d:`
  arguments and `sonar.qualitygate.wait=true`, so a red quality gate fails the step; without the branch name a
  local scan overwrites the project's `main` analysis ([TOOLING.md](TOOLING.md#sonarscanner-cli)). **The
  `branch-` prefix is what makes the step able to fail.** SonarCloud types a branch at its first analysis by
  the organization's `sonar.branch.longLivedBranches.regex`, `(branch|release)-.*`, and judges a short-lived
  branch on new-code conditions only, ignoring overall ones such as coverage; a name outside that pattern
  passes a gate `main` fails, and a branch's type never changes afterwards. **Stryker runs
  locally with `--version local-<machine>`** for the same reason: `Identity/stryker-config.json`, the one
  Stryker config, names the dashboard version `main`.
- **Every Sonar scan is followed by `Fail on open Sonar issues`, in CI and in the gate alike**, because the
  quality gate judges ratings and percentages and stays green over any number of code smells. CI queries
  `api/issues/search` for the branch or pull request it just analyzed and fails on a non-zero or missing
  `total`. The gate calls `Test-SonarIssues` (`GateSonar.ps1`) against `branch-local-<machine>`, which is
  long-lived and so carries every open issue, not only new ones; it also fails when that branch's latest
  analysis predates the run or the server cannot be reached, and it lists each issue the way the SARIF check
  does. What a local analyzer can catch earlier is enforced there instead: the generated Sonar rule blocks
  ([CODE-STYLE.md](CODE-STYLE.md#enforcement)).
- Where a repo guide records the ordered list and its exclusions, that list binds:
  `AGENTS/REPOS/Librarian.md`, `AGENTS/REPOS/Inventory.md`. Otherwise read the workflow.
- Use CI's literal invocation and working directory. Runner-specific traps (`dotnet test` from a repo
  root, vitest through the repo's own binary, type-check tiers): `AGENTS/TESTING.md`.
- `Tools/` has no workflow. Its `gate.ps1` runs every offline regression harness in the repo; the operational
  probes (`Seeding/Test-Irs990Coverage.ps1`, `Claude/skills/version-audit/scripts/Test-VmService.ps1`,
  `VersionAudit/Get-BinaryCompatibility.ps1`, `AlertTriage/Invoke-TriagePermissionMatrix.ps1`) read live data,
  a live VM, a candidate binary or a live Claude session and are not gate steps. A `Test-`-named probe is
  exempt from harness classification only when listed in `OPERATIONAL_PROBES` in
  `Harness/Hooks/invoke_gate_cadence_gate.py`.

## The ReSharper inspection

Every repo with a `.slnx` runs it, in CI and in the gate alike, from the version pinned in the repo's
`dotnet-tools.json` (`jetbrains.resharper.globaltools`, restored by `dotnet tool restore`), never from a
machine-wide `jb`, so the two cannot drift.

- **Placed directly after the build, and reusing it**:
  `dotnet jb inspectcode "<repo>\<Solution>.slnx" --no-build -e=WARNING --output="<scratchpad>\<repo>-inspect.sarif"`.
  Without `--no-build` the tool restores and compiles the solution again, so the gate would compile twice.
- **`SKIPPED` when the build fails**, like the test tiers: `--no-build` against a red build inspects stale
  binaries.
- **Judge the report, never the exit code.** Parse the SARIF and count `runs[].results[]`. Any result
  fails the step, and the row prints that count. A missing or unparsable report fails the step too, since
  an empty count from a report that was never written looks exactly like a clean run.
- **Never narrowed.** No `--include` or `--project`: the inspection covers the whole solution, the same as
  every other step.

## The shape of the gate script

- **Setup, then gate steps in CI order.** After any merge or dependency change, `npm ci` before the first
  step; mass `Cannot find module` across unrelated steps is an empty `node_modules`.
- **An OS kill during that install damages the tree, and the damage outlives the kill.** `npm ci` deletes
  `node_modules` before repopulating it, so a run killed mid-install leaves a directory that exists and is
  incomplete, and the next session's errors name anything but the install (`Cannot find module 'ajv'`
  from lint, `'tsc' is not recognized` from a type-check). `node_modules/ajv` and `node_modules/.bin/tsc.cmd`
  (on an Angular repo `.bin/ng.cmd` too) present means the install ran to the end. **A finished install is not
  the lockfile's tree**: `npm install --package-lock-only` rewrites `node_modules/.package-lock.json` without
  installing anything, so that file is no evidence of what is installed. The node_modules install step reads each
  installed package's own `package.json` (`Get-NodeInstallDrift` in `GateNode.ps1`) and fails on any version, missing
  required package or unlisted top-level package that differs from `package-lock.json`; the remedy is `npm ci`. **Finish an interrupted install deliberately and verify those markers rather
  than re-running the gate on top of it**, and tell the peers: a broken shared tree is their problem before
  it is yours. A gate script that reinstalls an already-complete tree buys nothing and re-enters the window
  where the kill does the damage, so check the markers first.
- **A new `PackageReference` in a lock-file repo is TWO edits, and the second is not optional.** Most
  .NET projects here set `RestorePackagesWithLockFile` and commit `packages.lock.json`, so a reference
  added without `dotnet restore --force-evaluate` fails the build with **NU1004** under
  `/p:RestoreLockedMode=true`, and the lock file belongs in the same commit as the reference. The failure
  lands *before* the test step, so the tier that would have caught anything else never runs and the repo
  reads as a restore problem. Check with `git grep RestorePackagesWithLockFile` before adding a reference.
- **Fail-fast, in a single invocation.** Every step runs in CI order, and the first red step ends the gate:
  no later step runs, and the table names every step it did not reach. A test tier stops at its first
  failing test (`--stop-on-fail on` after the `--` of a Microsoft Testing Platform run, `bail: 1` in
  the Vitest config, `--max-failures=1` on the npm script that runs a non-synthetic Playwright project,
  `-x` on pytest), in
  CI and in the gate alike. A synthetic walker never stops early, since its findings are the product.
  `maxFailures` is a whole-run setting no project can scope, so the flag sits on the script and never in
  `playwright.config.ts`. Every helper that records a failure is the thing that exits, so no script
  can record a failure and carry on. One invocation and one `GATE:` line either way.
- **A stop-at-first-failure switch is proven by a plant, because a green run cannot see it.** The gate that
  follows adding one plants several failing tests in one throwaway file, runs that file, asserts the plant
  was collected, the run failed, and fewer tests failed than were planted, then removes the file.
  **One file, not several**: cancellation is file-level, so a plant spread over several files has the
  remaining files cancelled before they are collected, and the run reports one test collected and one
  failed however many were planted. An assertion that every planted test was collected then fails for the
  wrong reason, and the count that still discriminates is "fewer failed than planted" plus a non-zero
  exit. A multi-file plant also needs the runner's file parallelism off, or its files can all fail before
  any cancellation reaches them, which reads as the switch doing nothing.
- **Name every step in the script's code.** The hook finds a step by its npm script name, its tool, or its
  test project and trait on a code line. A step reached only through a computed name, or mentioned only in a
  comment, reads as missing and the script is denied.
- **Gate each test tier on its build's exit code**, never merely sequence it after the build. A red build
  leaves the previous binaries in `bin/`, which still run and usually pass.
- **Check every native command's exit code and accumulate failures.** A wrapper script's own exit status
  says nothing about the steps inside it.
- **Clear `$LASTEXITCODE` before each step, and make "never ran" its own verdict.** `$LASTEXITCODE` is
  stale, not empty, when a command does not execute (tool not on PATH, a bad working directory, a
  pipeline that dies first), so the step inherits the previous step's `0` and the row prints PASS for a
  step that did nothing. Set `$global:LASTEXITCODE = $null` immediately before the call and treat `$null`
  as a failure whose detail says the command never ran.
- **A row printer must not use `Write-Output`.** `$ok = Invoke-Step ...` collects everything the function
  writes to the success stream, so a helper that both prints its row and returns a boolean hands the
  caller an array, which is always truthy: every failed step then counts as a pass and the run can print
  `GATE: PASSED` over a red build. Print rows with `Write-Host`; the `*>&1` capture still records them.
- **When a step is judged by matching its OUTPUT, capture with `*>&1` and read it back from a file, not
  a variable.** PowerShell's `2>&1` redirects the error stream only, so a tool that reports its verdict
  with `Write-Host` (the information stream) is captured as nothing at all, and a match against an empty
  string fails in both directions: a real success line is never found, and a planted-defect row that
  expects no match passes trivially. The robust form is `… *>&1 | Tee-Object -FilePath $log` and then
  asserting against the log's contents, which survives whichever stream the tool picks. Applies to any
  positive control: a step that exits 0 without printing its success line is a FAIL, and that is only
  knowable if the line was actually captured.
- **Print one row per step**: name, `PASS`/`FAIL`/`SKIPPED`, and for a test step the executed count
  beside the floor the repo's `TESTING.md` records. Zero executed is a failure, and so is a count below
  the floor. An absent step is visible only in that table.
- **Pin `$env:TZ = 'UTC'`** and assert the pin took (`AGENTS/TESTING.md`).
- **Write the script to the session scratchpad**, named for the repo, and run it as exactly
  `pwsh -File "<abs path>"` or `python "<abs path>"`. Run it in the background when it can outlast the
  foreground timeout, and while it runs end the turn with a line beginning `WAITING:` naming the gate;
  the stop gate accepts it only while that background gate is still live, and its completion notice
  wakes the session ([STOP-HOOK.md](../Hooks/STOP-HOOK.md)).
- **A background gate can be reaped, and a foreground one cannot outlast the tool's 10-minute cap.**
  Claude Code kills an idle session's background shells when the machine runs critically low on memory;
  the notice reads "stopped because the system is running low on memory", and the record stays at
  `RUNNING` with no verdict. That is not a failure of the gate, and the notice forbids restarting it
  unasked. A gate that fits in 10 minutes runs in the foreground; a longer one stays in the background
  and is exposed until `CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP=1` is in Claude Code's own
  environment, which no shell command can set.
- **Report what the gate cannot see** beside its result: a build does not see formatting, encoding or
  file shape; tests do not see anything they do not assert; a catalog sees only its documented scope.

## When a gate runs

- **Once per work product, after the last edit, against a frozen tree.** A work product is every edit
  that makes up one deliverable, however many files and edits it spans. It gets one gate, never one per
  edit.
- **When the operator set a goal, the work product is that goal.** The goal is an active `/goal`, else the
  operator's newest `GOAL:` line ([STOP-HOOK.md](../Hooks/STOP-HOOK.md)). A gate runs only once every edit
  the goal needs is made, and the checked-in gate is run as one bare command restating it:
  `pwsh -File "<workspace>\<Repo>\gate.ps1" -Goal "<the goal, verbatim>"`. The match is on words: case and
  every character that is not a letter or digit are ignored (`goal_pin.normalised_words`), so leave out any
  `;`, `|`, `&` or backtick the goal holds, which the PowerShell gate refuses anywhere on a command line, and
  double each `"` inside it; double quotes carry an apostrophe. Never substitute a description of the work
  for the goal. A `GOAL:` comment inside a script satisfies nothing.
  A gate for part of the goal is a check before the work product is finished. **A gate that passes every
  rule runs without asking**: every declared work-product item done, and no other gate holding the machine;
  the `-Goal` restatement names the goal, the declaration says the work product is finished. The Stop hook
  holds a `DONE:` line to the same goal ([STOP-HOOK.md](../Hooks/STOP-HOOK.md)).
- **After a red gate**, read every failure the run reported, fix all of them, then gate once more. What
  "once more" covers depends on where the failures were
  ([Re-running after a red or incomplete gate](#re-running-after-a-red-or-incomplete-gate)).

## Declaring the work product

**The work product is fixed by the operator's ask, not by the session's progress.** A session that
redefines it mid-stream ("the first half is the work product") can gate early and stop early while
believing it follows every rule above. So the boundary is written down, where the hooks read it:

- **Before a session's first gate on a repo it writes `%TEMP%\claude\work-products\<session>.json`**
  (`CLAUDE_CODE_TMPDIR` when set; every session state file lives under that root, never under a `.claude` folder,
  which is a protected path no allow rule pre-approves),
  `<session>` being its transcript's file stem, in the shape
  `{"repos": {"<Repo>": [{"item": "<one deliverable>", "done": false}]}}`. The list is every deliverable
  the operator's ask puts in that repo, not the part already finished. `<Repo>` is a clone directly under
  the workspace (its `.git` is a directory): work in a worktree goes under its repo's key. The one key that
  is no repo is `"AGENTS"`, for edits to the fleet docs: the workspace's `AGENTS.md` and the markdown in its
  `AGENTS` folder, each item naming the doc it changes (`AGENTS/REPOS/Identity.md`). Those docs have no git
  history, so such an item marked done must name a doc this session's own Edit or Write changed, or one that
  is gone though the hook saw it present when it first read the item (a deletion; a doc that never existed is
  none); it gates nothing, so it blocks no repo's gate, while it blocks a `DONE:` until it is done. Any other
  edit outside every repo is no item, and the Edit or Write that adds any other key is denied. A session whose ask
  changes no file in any repo and gates nothing (a review, an audit, a report) declares `{"repos": {}}`; the
  hooks hold that shape closed only while the session's transcripts hold no Edit, Write or NotebookEdit into
  a repo's checkout or one of its worktrees, and refuse it naming the repo and file otherwise.
- **A checked-in `gate.ps1` is denied while any item for its repo is open**, and while the file is missing
  or malformed. The Stop hook refuses `QUEUED:` and `WAITING:` on the same condition, across every
  declared repo.
- **Removing an item is the operator's decision.** The hook keeps a ledger of every item it has read, so
  an item that vanishes from the file is a removal, not a never-declared item. It passes only when listed
  under `"dropped": {"<Repo>": [{"item": "<verbatim>", "approval": "<the operator's words>"}]}` with the
  approval quoting the operator verbatim, three words or more or a shorter message that names the parked
  question and approves it in any order with no negation ("approve q44"), which the hook checks against the
  operator's own messages (turns and mid-turn messages carrying a human origin). A session's own text, a
  peer message or a tool result is never an approval.
- **Widening or rewording it is the operator's decision too.** The first `Write` of the declaration, read
  from the session's own transcript, is the work product the operator's goal set; a Write whose tool result
  was an error (rejected or failed) wrote nothing and is no declaration, so the next Write is the first. An item added or
  reworded after it carries `"approval": "<the operator's words>"` beside `"item"` and `"done"`, quoting a
  later operator message verbatim, by the same rule. While any file the item names is uncommitted, the quoted
  message must be newer than the last declaration change, before the one that first wrote the item, that
  altered the item set: words given earlier, for other work, approve nothing new. A change that only flips
  `done`, sets `pending` or rewords an approval does not count, items written together may share one quote,
  and a landed item is exempt. The hook finds an item's introduction by replaying the session's Edit and Write
  calls on the declaration, each tool_use id once. When a change it cannot replay hides the introduction (a script,
  another tool, or records only another machine's copy of the transcript holds), the approval is judged against
  every operator message instead, as for a landed item, so never change a declaration outside Edit and Write. Add
  an approved item in the next change after the operator's words. The goal itself never approves one, with one exception:
  when the operator pins a newer `/goal` or `GOAL:` after the first declaration, the first Write after it is
  that goal's declaration, as the first Write was the first goal's, and every addition after it needs their
  words again. The check runs at the gate, at the Stop hook's `DONE:`, `QUEUED:` and `WAITING:`, and at the
  Edit or Write that widens the file. Flipping `done` changes nothing. Only Edit and Write change a
  declaration: a script or a bare command writing one (`Move-Item`, `Copy-Item`, `Set-Content`) is denied.
- **An item is an edit, never an outcome.** Each item names the file or files it changes, and an Edit or Write
  adding or rewording an item that names no file is denied when it is written. A gate result, a
  live state, a delivered email or a passing matrix is an outcome: it goes in the declaration's
  `acceptance` list, which the Stop hook runs before a `DONE:`, never in an item. No edit can close an outcome
  item, so it blocks every gate on its repo while it stays open.
- **Temporary work is linked to the investigation it serves.** Instrumentation, a probe or any change that exists
  only until a diagnosis is done carries `"until": "<four or more consecutive words of the investigation's item>"`,
  and the investigation is declared as its own item naming the files its cleanup changes. The gate and the Stop
  hook's `DONE:`, `QUEUED:` and `WAITING:` hold a linked item open while the item it quotes is open, through any
  chain of links, and refuse a link that quotes no other item declared for the repo. An undeclared investigation
  is invisible to every hook; the link only works when the agent writes it.
- **Under a pinned plan, items follow from the plan.** When the operator's goal names a plan file, every item
  cites the plan section or lines it implements, and every edit the plan names has an item. The plan's
  Verification steps are outcomes, so they become acceptance checks.
- **A planted proof declares its plant.** An item whose work IS a gate run with a defect planted (proving
  a failure path, or that a new check fails) carries `"plant": "<path in the repo of the planted file>"`,
  and the planted line carries the marker `PLANTED`. While that file holds the marker, the item does not
  block launching the gate, because running with the plant in is the item's work; every other item for
  the repo must still be done. The hook reads the file, never the claim: a plant item whose file lacks the
  marker is an ordinary open item, and one marked `done: true` while the marker is still in the file is
  denied, so a planted tree can never pass as finished. Remove the plant, mark the item done, and gate the
  clean tree. The Stop hook's `QUEUED:` and `WAITING:` checks give a plant no allowance.
- **Marking an item done is a claim the hook cannot check.** It turns a silent redefinition into a
  written one the operator can read; it does not make the claim true.
- **Advice is not scope.** An advisor, a reviewer or a peer suggesting the work be split changes nothing
  in the declaration without the operator's approval recorded as above.

## After a hook denial

**Follow the denial or its documented opt-out; never reach the same effect through another tool.** A
script denied as a scratch gate is not rewritten as the same edits through Edit; it carries the opt-out
marker the reason names, or it does not run. A denial believed to be a misfire is reported to the operator
in the same turn, and fixed in the hook with a harness row rather than routed around.

## Re-running after a red or incomplete gate

A passing row is evidence about the tree it ran against, and it stays evidence while that tree is unchanged.
So the question after a red or incomplete gate is **what changed under the gate's inputs**, not which rule
sounds strictest.

- **A fix touched the gate's inputs** (source, tests, config, lock files, anything a step reads): run the
  whole gate again. Every previous pass described a tree that has been replaced.
- **Nothing under the inputs changed**, and a step gave no verdict because of the gate script itself (a
  wrong marker, missing registry auth, a typo) or because the machine could not finish it: run only the
  steps that gave no verdict, from the corrected script, once the machine is free. The previous passes
  still stand, and repeating them holds the machine from every peer for nothing.
- **The gate script carries a verdict by fingerprint, never by judgment, and each step has its own.** A
  step's fingerprint hashes the files it reads among those `git ls-files -co --exclude-standard` returns
  (markdown and `.github/` excluded), plus only the gate library files that decide its verdict. `GateCommon.ps1`
  only loads the library for the repos' gate.ps1 files, one module per concern: `GateSteps.ps1` (the step registry, inputs, run order),
  `GateCarry.ps1` (fingerprints, carry, verdicts), `GateRecord.ps1` (state and the gate record), `GateLanding.ps1`
  (a landing that supersedes a run), `GateRun.ps1` (rows, timing, completion), `GateCatalog.ps1` and `GateSlot.ps1`.
  These decide whether a step runs and never what it returns, so none joins a step's fingerprint. Each
  verdict-bearing helper lives in its own module too (`GateExit.ps1` for `Test-Exit`, `GateTrx.ps1`, `GateSarif.ps1`,
  `GateSonar.ps1`, `GateNode.ps1`, and `GateOrigin.ps1` for the origin and dependency-order steps); a module's
  checker records itself against the step in flight (`Use-GateModule`), the verdict stores that list, and the
  step's fingerprint takes exactly those modules, so an edit to `GateTrx.ps1` reruns only the steps that read a TRX.
  No list of modules is kept anywhere: the start-of-gate snapshot hashes every `Gates/Gate*.ps1` by name, a step's
  fingerprint takes only what it recorded, and a verdict that recorded nothing carries nothing. A catalog step also
  reads its own catalog and `ScanScopeHelper.ps1` (the table in `GateCatalog.ps1`), so a catalog edit reruns the
  catalog steps and nothing else. The gate record's `tools` lists the snapshot, and `gate_status.py` re-hashes
  exactly those files, so it keeps no list of its own.
- **Each library concern has its own harness, which loads only the files it tests.** A checker module's harness
  (`Gates/Test-GateExit.ps1`, `Test-GateTrx.ps1`, `Test-GateSarif.ps1`, `Test-GateSonar.ps1`, `Test-GateNode.ps1`)
  dot-sources that module and `gate-check-double.ps1`, a test double for `Use-GateModule`, `Write-Row` and
  `Stop-Gate`, so a checker edit reruns its harness and the bookkeeping's do not; `Test-GateSlot.ps1` loads only
  `GateSlot.ps1`, each case in its own pwsh since a refused gate ends its process. `test_gate_common.py` covers the
  bookkeeping, through a probe that loads the bookkeeping modules and the two checkers it calls (`GateExit.ps1`,
  `GateOrigin.ps1`), never `GateCommon.ps1`. A module never dot-sources another, so a consumer's load list is its
  whole dependency: a module several files call (`GateExit.ps1`, called by `GateNode.ps1` and the bookkeeping) is
  its own file, and an edit to it reruns exactly the harnesses that load it. `Gates/Test-GateLoads.ps1` parses every
  consumer and fails when a function its loaded files call is defined only in a file it does not load. **A pass carries across checkouts:** a fingerprint is the content of a step's
  inputs and nothing else, so a gate under the account's gate records carries a step's PASS from any other checkout
  of the same repo on this machine whose fingerprint matches (`Get-SiblingVerdicts`) unless the checkout's own entry
  for that step is not a PASS, so a checkout reruns only what its own edits reach, even beside its own older or
  pre-module passes, and its own failure is never outvoted by another's pass. Each row records the step's seconds. **Every step a gate registers declares its inputs** in a
  `Register-StepInputs @{ '<step name, exactly as registered>' = @('src/*', ...) }` block right after
  `Register-GateSteps`, `@('*')` for a step that reads the whole repo (`-like` against repo-relative
  forward-slash paths). GateCommon fails the gate at its first line when a registered step has no entry, an
  entry names no registered step, or a pattern matches no file the gate fingerprints, and the gate-cadence
  hook refuses to launch a gate.ps1 with either of the first two, so a pattern that silently matched nothing
  can never make a step carry a stale verdict.
- **Tools lists no step and no input by hand: its gate.ps1 takes both from `Gates/step_inputs.py --plan`.** A
  harness is a tracked file named `Test-*.ps1` or `test_*.py`, or any `.py` or `.ps1` whose first lines carry a
  `# GATE-STEP:` marker at column 0 naming its step (`| args: ...` for its arguments, `| pytest` to run pytest over
  its folder, `none | <why>` for a conventionally named file that is no gate step, such as a live check). A new
  harness is a step the moment its file exists; with no marker it is named from its file
  (`test_repos_audit.py` runs as `Repos audit harness`). Two files claiming one name refuse the plan, since a
  step's record is kept by name. A step's inputs are what its harness reaches: modules it imports from its folder or a
  folder it puts on `sys.path`, scripts it dot-sources, and repo files it names by a path built from its own location
  (`os.path.join` over `__file__`, `$PSScriptRoot`, `Join-Path`), transitively, markdown included, so the gate
  fingerprints a doc a harness reads. A bare file name counts only beside the file naming it. So an edit owes only
  the steps whose harnesses reach the edited file: an edit to one hook reruns that hook's harness and the few that
  load every hook. A directory listing in a harness file widens its step and never fails the gate: one rooted in a
  temp folder (through the variable it lists) adds nothing, one rooted at the harness's folder adds that folder, a
  `# GATE-LISTING: <text in the listing's line> | <folder globs, or none>` line in the harness itself names what a
  listing the rules misjudge reads (so no file names a harness it does not run), and any other makes the step owe every
  edit, with a `WARN` line naming it. Each file's analysis is cached on its content hash, so the plan after a
  one-file edit reads that file again and nothing else. The gate-cadence hook's probe check and `gate_status.py`
  read the same plan, and the `Step inputs harness` (`Gates/test_step_inputs.py`) proves each rule on a fixture
  repo.
- **A gate that owes only a few steps fits the PowerShell tool's 10-minute foreground limit,** so it runs in the
  foreground, where Claude Code's memory-pressure reaper cannot stop it; a gate owing most steps runs past that limit
  in the background, and on a machine short of memory the reaper stops it with "stopped because the system is running
  low on memory".
- **A step whose verdict depends on something no file fingerprint sees is volatile and never carries.** An
  audit against an advisory database turns red on an unchanged tree the day an advisory is published, so a
  carried pass is a time-blind green. The gate declares it with `Register-VolatileSteps @('<step>')` beside
  its `Register-StepInputs` block; GateCommon then never finds carry evidence for it, so every run owes and
  runs it, and fails the gate at its first line when a volatile name matches no registered step (a renamed
  step must not fall out of the set and start carrying). The run record lists the volatile steps, and
  `gate_status.py` reports an otherwise current green as `GREEN-STALE-AUDIT` once they are older than 24 hours.
- **The gate works out which steps it owes; the caller names none.** A checked-in gate calls GateCommon's
  `Resolve-OwedSteps` after `Initialize-GateState`, which prints the owed steps: every step whose inputs
  changed since its last pass, that last failed, or that never passed. Every other step prints `CARRIED`.
  A library edit owes only the steps whose fingerprints took the edited file; a `GateCommon.ps1` or bookkeeping
  edit owes none of an app's steps. The gate-cadence hook
  refuses a launch of such a gate that passes `-Steps`, and refuses any launch while the repo's record is
  green on exactly its current inputs. Each verdict is recorded against its step's hash in
  `<Repo>.json` in the same state directory (the per-step carry, distinct from the run record in
  [The gate record](#the-gate-record)). A step is skipped only when its last verdict was PASS on its
  identical hash, and is printed as `CARRIED` with the time of the pass. A step that last failed, or any
  edit to a file it reads, runs it again; an edit made while the gate runs revokes only the verdicts whose
  inputs it moved. **Declare inputs only for a step whose result depends on nothing another step
  writes**: coverage feeding Sonar and a build feeding a `dist/` check stay whole-repo, or a carried
  verdict would vouch for a stale artifact. Steps that share one result (Sonar begin, the build, Sonar end
  and the open-issue check) carry together or not at all, keyed on the open-issue check's row, since it is
  the last of them to pass.
- **Report the combined result as what it is:** name both runs, which rows came from each, and confirm
  no input changed between them (the files' state or `git status` at both points).
- **Never lower a threshold, drop an assertion or narrow a step to get it through.** That is a different
  check, not a re-run. Narrowing a step to the delta ([Narrowing to the delta](#narrowing-to-the-delta))
  is the one narrowing that is a re-run, because the rows it leaves out already have a verdict the delta
  cannot have changed.
- **Before any claim** that a repo is clean, verified, ready or done. The claim names the steps that ran
  and the tree they ran against.
- **A planted defect is COUPLED to the code it breaks, so rewriting that code silently disarms the plant.**
  Retarget every plant whose arm you edit, in the same edit. A plant must treat "did not apply" ("matched
  0 times, nothing was planted") as a failure rather than running the harness anyway: a plant that goes in
  nowhere leaves the harness green and reads as proof that the detector discriminates.
- **After changing a check** (a lint rule, a harness, a CI step), with the defect planted so the gate is
  seen to go red, then removed ([WORKFLOWS.md](WORKFLOWS.md#detectors-must-be-proven-to-discriminate)).
  The plant is proven once, by the gate that follows the change to the check; a later gate whose edits
  cannot reach that check does not plant it again.

## Narrowing to the delta

A passing row is evidence about the tree it ran against, and an edit invalidates only the rows it can
reach. So a step is narrowed to the delta when, and only when, every test it leaves out already has a
verdict from a full run of this session and nothing edited since that run can reach it.

- **The script's code declares the delta**, as `GATE_DELTA = [...]` in Python or `$GateDelta = @(...)` in
  PowerShell, one entry per file: every file this session edited since its previous gate, or `plant:<file>`
  for a file the script itself plants and restores. The hook verifies each entry against the session's own
  transcript (an edit after the previous gate launch, or the script's own write of the planted file) and
  denies a narrowed run whose delta it cannot verify. A delta in a comment declares nothing.
- **Every step the delta can reach still runs in full.** Lint, a type-check, a build, a primitives or
  utilities check and a catalog read the whole tree, so they are never narrowed, and so is a coverage step
  whose totals are a whole-suite property. A test step is narrowed to the specs that can reach the delta:
  the specs that render, import, route to or assert on the files in it. When that set cannot be named,
  the step runs in full.
- **Reach means a test can observe the change, not that a request passes through the changed code.** A
  header dropped from a proxy every request traverses reaches only the specs whose backend or assertions
  read that header; "everything goes through it" names the code path, not what any test can see. Name
  the observer before widening a step, and a step widened without one is the full re-run this section
  exists to stop.
- **The planted-defect control is a full run with the defect in, then a narrowed run with it out.** The
  full planted run is the verdict for every test the plant cannot reach, and the run after the restore
  covers only the spec the plant targets, declared as `plant:<file>`. Running the whole project a second
  time re-verifies tests the plant could never have touched.
- **A step that gave no verdict** (the machine could not finish it, a mock server never came up) is re-run
  narrowed to the tests without a verdict, under the same declaration, once the machine is free.
- **The report names what was narrowed and to what**: the full run each excluded row's verdict came from,
  and the delta the narrowed run covered.

### A re-run after a baseline narrows by blast radius, and cheapness is not a reason to widen

The first full run is the baseline. **Every run after it tests only the logic a unit edit could change or
the behavior a production edit could change**: the reachable set is at its most knowable here, because
a baseline exists and something just failed. Two directions, and they are not symmetric:

- **The edit was in a failing test file: run ONLY the tests that failed.** Nothing else in that file, and
  nothing else in the project. A test's own fixture or assertions cannot alter what a different test
  observes, so every other test in the run already has its verdict from the baseline.
- **The edit was in production code: run only the tests that were GREEN and could now go red.** Ask of
  each candidate "could this test, which passed, fail because of this edit?" and run the ones that can.
  A test that was already red is in the first direction; a test the edit cannot reach keeps its baseline
  verdict.

**"It only takes N seconds" is not a justification, it is the excuse this rule removes.** A step that runs
tests the edit cannot reach reports a verdict it did not earn, whatever it cost, and a cheap step is the
easiest place to hide a full re-run, precisely because nobody objects to the price. The zero-executed
green that tempts a widening is answered by asserting a floor on the executed count, never by running
everything ([Assert the executed count](#the-shape-of-the-gate-script)).

**Whole-tree steps are unchanged**: a build, a lint, a type-check and an inspection read the tree rather
than a test list, so they run in full whenever the delta reaches them at all.

## Running only the tests you wrote

**Before the full gate, run only the tests this session created or modified, and nothing else.** A
test nobody changed already has its verdict, and a run that re-verifies it costs wall clock and buys
nothing. This is not a gate: it writes no gate record, carries no step verdict and prints `TESTS:
PASSED` or `TESTS: FAILED`, never a `GATE:` line.

- **A .NET repo's checked-in gate takes the names**:
  `pwsh -File "<workspace>\<Repo>\gate.ps1" -Tests '<Method>,<Method>' -TestProject <Repo>.Tests.<Tier>`.
  It builds that test project, runs the tier's setup, and runs `--filter-method` over the names
  (`Harness/Gates/SelectedTests.ps1`). A bare name becomes `*.<Method>`; a fully qualified one is passed
  as is. The run passes only when the executed count equals the number of names, so a name matching
  nothing, or matching a same-named method in another class, fails.
- **A harness takes its own selection**: `python <harness> --only <term> ...` (or `-Only` / `-Rule`),
  each term naming the cases this session wrote or changed.
- **The hook verifies the selection against the session's transcript**
  (`Harness/Hooks/invoke_gate_cadence_gate.py`): every `-Tests` name, whole-word and wildcard-free, in a
  test file this session edited (a qualified name's class and method in the same file), and every
  harness term in the body of a harness this session edited. A pattern, or a name from an untouched
  file, is denied. The tests-only runner is reachable only through a checked-in `gate.ps1`; a scratch
  script calling it is denied.
- **The machine is held as for a gate**: an own run in flight is denied and a peer's asks, and peers get
  the STARTING and FINISHED lines.
- **A planted-defect control runs only the rows the plant targets.** Plant, run the selection that holds
  the must-fail rows, restore, and do not re-run: the passing run before the plant is the verdict.
- **What it does not cover is stated in the report**: a changed fixture, helper or production file
  reaches tests that were not run, and their verdict waits for the full gate.

### Probing one step

**A gate step can run on its own, before the full gate, to check a fix to the session's own edits.** A gate
stops at its first failing step, so without a probe each defect costs a full run to find and another to
confirm. `pwsh -File "<checkout or worktree>\gate.ps1" -Probe '<step>,<step>'` runs exactly the named steps and
skips every other one, stops at the first named step that fails, and prints `PROBE: PASSED` or `PROBE: FAILED`.
It is GateCommon's own mode (`Initialize-GateState -Probe`), so every gate.ps1 that passes its `-Probe` through
takes it; a step name matches as GateCommon's carry does, by prefix.

- **A probe is not a gate.** It writes no gate record and no step verdict and grants no carry, so the full
  gate still owes every step a probe ran. A later gate never counts a probe as its previous launch.
  `PROBE: PASSED` is never a gate status: `gate_status.py`, the commit-message check (`parking_lot.py commit`)
  and the Stop gate read only the gate record, which a probe never writes, so a message written after a probe
  alone still opens with its `UNGATED:` line. The `probe:` rows of `Gates/test_gate_common.py` run a fixture gate
  with `-Probe` and fail when a record or verdict file appears, or when a probe after a passing gate changes them.
- **The hook verifies the names** (`Harness/Hooks/invoke_gate_cadence_gate.py`, `unverified_probe`): each one a
  step the checkout's gate.ps1 registers, with a declared input (`Register-StepInputs`, or the plan's derived
  inputs for a gate that plans its steps) that this session's transcript shows it editing. A step the session's edits cannot reach has nothing for a probe to check.
- **The machine is held as for a gate**: a probe takes this account's gate slot, an own run in flight denies
  it, and peers get the STARTING and FINISHED lines.
- **No goal and no finished work product are required**, because a probe decides nothing about landing.

## Build before the gate

**A gate never runs on a checkout that has not been proven to build.** The gate is not the compiler: a gate
run that fails at its build, or at an analyzer the build reports, is a wasted run of the machine's one gate
slot. Before any repo's `gate.ps1`:

1. Build the repo on its own, as one foreground command naming its path: `dotnet build "<repo>\<Solution>.slnx"`
   for a .NET repo, `npm run build --prefix "<repo>"` for a repo whose `package.json` has a build script.
   Building is allowed outside a gate at any time; it is not a gate step.
2. Fix every error the build reports, analyzer errors included (StyleCop, xUnit, nullable, Sonar), and build
   again until it succeeds.
3. Gate. Any non-markdown change to the checkout after the successful build needs a new build first.

The gate-cadence hook (`Harness/Hooks/invoke_gate_cadence_gate.py`, `build_problem`) refuses a checked-in
`gate.ps1` in a checkout with a `.sln`/`.slnx` or a `package.json` build script unless the session's newest
successful foreground build of a path inside that checkout postdates every non-markdown change in it. A
background build is no proof, because its call returns before the build ends, and a build naming a target
(`-t:`) is a gate step. A repo with no build (Tools, Curator) needs none.

## When a gate does not run

- **Before the work product is finished: nothing but the build.** Between its first edit and its gate the number of tool-run checks is
  zero apart from plain builds ([Build before the gate](#build-before-the-gate)) and a probe of a step the
  session's edits reach ([Probing one step](#probing-one-step)). No lint, no single spec, no `--grep`, no "cheap" or "targeted" subset. A sentence that
  concedes this rule and carves an exception out of it in the same breath is the tell. The one run between
  a finished work product and its full gate is a tests-only run of the tests it created or modified
  ([Running only the tests you wrote](#running-only-the-tests-you-wrote)).
- **During a live exploratory session.** While the operator is looking at the app and sending findings,
  the work product is open and only they close it. Edit, report what changed and what is unverified, and wait.
- **When the change cannot alter behavior**: markdown, comments, whitespace. Name the behavior the run
  would catch; if there is none, do not run it.
- **When nothing has changed since the last gate.** Wanting a stronger claim is not a reason; narrow the
  claim instead. A green gate stands while no file under its inputs postdates the run's artifacts.
- **While a gate is in flight**, this session's or a peer's. An outstanding background task ID is a hard
  stop on launching anything else against the machine.
- **While the operator is using the app.** A full gate rebuilds `dist/` and takes the app's port. Schedule
  it with them and restore their serve afterwards; per-app mechanics are in the repo guide.
- **While a peer session is building, testing or gating on the same machine.** Builds, tests and gates
  run one at a time per machine, and the pre-gate check is asking the peers what they are running
  ([Coordinating with peer sessions](#coordinating-with-peer-sessions)), never a machine measurement.
  **That is the whole of what serializes.** Every other kind of work continues while a gate runs, in
  every session: edits, reads, a `git status`, a targeted Grep. Holding those whenever anyone is gating
  would turn a machine running many sessions into one that runs a single session at a time, since some
  session is nearly always gating, and that is not what serializing gates is for.
  - **Whether light concurrent work contributes to OS kills is unmeasured.** A gate has been killed with
    no peer building; no mechanism is established in either direction. Extending this line needs a
    measurement, and a session's own admission that it polled its gate is not one.
  - **Do not measure free memory, and do not probe the machine's load, before a gate.** Both are
    expensive on a loaded box and neither decides anything: no free-memory threshold or load probe has
    separated the runs the machine completed from the runs the OS killed. A gate script carries no memory
    reading, no load probe and no threshold, and a peer's reading of either is not a reason to hold or to
    start.
  - **A step that cannot complete locally** is reported `NOT RUN`, with CI named as its verifier, never
    faked and never retried into an OS kill. It stays in the gate script with that row, so the gate
    reports incomplete. It is finished once no peer is running: by that step alone if nothing under the
    gate's inputs has changed since, by the whole gate if anything has
    ([Re-running after a red or incomplete gate](#re-running-after-a-red-or-incomplete-gate)).
  - **A duration measured while a peer was running describes the contention, not the step,** and never
    becomes a baseline.
  - **The session running a gate does not stack its own heavy load beside it.** While its gate is in
    flight, it starts no workspace-wide sweep: no `git status` loop across every repo, no Grep or file
    scan over the whole workspace, no second build. That load lands on the same machine as the gate, and
    it muddies any diagnosis afterwards. Ordinary work stays ordinary: a single repo's `git status`, a
    Read or an edit is not held, in the gating session or any other. A trivial command that times out
    during a gate says the box is contended; it is not by itself a reason for any session to stop working.

## The tells

Each of these is a sentence agents have said to themselves right before running a gate they should not
have. Hearing one is the signal to stop and re-read this file.

- **"It is only a targeted run."** A single spec, a `--grep`, a lint, a filtered `dotnet test`: each is
  a gate step, and redefining "gate" to mean "the full suite" is how the rule gets
  carved away. The exceptions are a step narrowed to a declared delta after a full run
  ([Narrowing to the delta](#narrowing-to-the-delta)) and a hook-verified tests-only run of the tests the
  finished work product created or modified
  ([Running only the tests you wrote](#running-only-the-tests-you-wrote)); anything else is a check.
- **"I want to verify it against the exact final tree."** When the only change since the last gate cannot
  reach a test, the run buys an adjective. Narrow the claim instead.
- **"I've fixed the findings so far, so it is done."** While the operator is still sending findings, the
  work product is open. Only they close it.
- **"It will only take a moment beside the one already running."** An outstanding background task is a
  hard stop.
- **"Let me check this item before the next."** A sweep, a review-feedback loop or a fix-and-verify cycle
  is one work product and one gate. Deleting comments needs no gate at all.
- **"Let me see whether the new test passes."** A gate is not a place to author tests. Once the work
  product is finished, the tests-only run answers this, for the new tests alone.
- **"The selection was denied, so I'll run everything."** A denied selection is a defect to fix or a
  question for the operator, never a reason to widen the run.
- **"Let me just run the step the gate skipped", after editing.** Once a file under the gate's inputs has
  changed, the previous passes describe an old tree, so the whole gate runs again. With nothing changed,
  running only the steps that gave no verdict is correct. Lowering the threshold that skipped a step is
  wrong either way, because it changes the check to get a pass.
- **"Let me rerun the whole gate to be safe."** When nothing under its inputs changed, the passing rows
  still stand. A full rerun buys no evidence and holds the machine from every peer.
- **"The gate will tell me whether it compiles."** It will, at the cost of a gate run. The build answers
  that on its own, before the gate ([Build before the gate](#build-before-the-gate)). Find the call sites of
  a change with Grep across every project, test projects included, then build to confirm.
  - **A serialized member has two names, and the search must cover both.** System.Text.Json renames a
    PascalCase member to camelCase on the wire, so assertions, mocks, TypeScript models and e2e fixtures
    name it in a casing that never appears in the C# source as an identifier: `GetProperty("hasX")`
    against a member `HasX`. A case-sensitive identifier grep is structurally blind to every one of them.
    Search case-insensitively for any change to a response DTO, and search the client repos too.
  - **A reference search answers "does anything still name this identifier" and nothing else.** It cannot
    see member ordering (SA1202), blank lines around a comment (SA1507/SA1515) or any other positional
    rule, so a clean search is never evidence that the tree compiles. When the build is the thing in
    question, only the build answers it.
- **"Let me prove the sentence I just wrote."** A fresh scan to back a line in your own report (line
  endings, catalog rows, a twin's behavior) is a gate. Fold the check into the final gate or state the
  method instead.
- **"No failures so far."** Tailing a running gate's log and reading the absence of a failure marker in
  the last few lines as a clean sheet. It is not a pass signal in either direction: the tail shows
  where the run has reached, not what it has found, and a failure hundreds of tests back has already
  scrolled past. A run's verdict comes from its summary line and its step table, after it exits; while it
  is running the only true statements are which step it is in and that nothing is known yet.
- **"Noted, I'll record it."** A promise or another memory entry after a reminder has not changed the
  behavior before; the hook exists because of that.

## Never edit what a gate is checking

A gate is meaningful only against a frozen tree. No edit to a repo while a gate runs against it: the
compiler reads files mid-write, and a compile error naming a symbol just added is a race until the file is
re-read.

## Editing tools are not gate steps

`jb cleanupcode "<repo>\<Solution>.slnx"` rewrites files in place, so it is an edit: it may run while the
work product is open, like any other edit, and never while a gate runs against the repo. Build the solution
before it, or accept that it cleans less where binary references do not resolve; either way the run is an
edit, not a gate. Review its `git diff` like any other edit, since it touches files beyond the work product.

`ruff format <paths>` without `--check` or `--diff` is an edit on the same terms. So is
`ruff check --fix --select I,F401 <paths>`: its only selection is import sorting (`I`) and unused imports
(`F401`), rules ruff fixes rather than reports. Any other `ruff check`, including `--fix` with a wider,
extended or repeated `--select`, still lints and is a gate step, as are `ruff format --check` and
`ruff format --diff`.

## Coordinating with peer sessions

One gate at a time per account. **Gates are per-user, never machine-wide**: a user session never checks or
holds machine-wide state. Every gate takes its account's gate slot in `Initialize-GateState`
(`Harness/Gates/GateSlot.ps1`: a file lock under the account's own `%TEMP%\claude\crgolden-gates`, held for the life
of the gate's process and released by a crash) and fails at once, naming the holder, when another process has it; a
launcher that holds the slot for its child passes `CRGOLDEN_GATE_SLOT_TOKEN`. The slot is what makes a gate
launched outside any Claude Code session visible to that account's sessions. The slot
refuses a second gate rather than queueing it, so every session still follows these steps, and the hook
denies a gate while another session's gate is live or the slot is held. **Settle the queue order between sessions
before anyone launches**: the deny leaves no room to negotiate a slot once a gate is running, so a short
gate that should go ahead of a long one is agreed in messages first.

**Before acting on any second-hand fact, say what it is a fact about, and whether that is the thing you are
deciding.** A routing label is a fact about the transport, "this machine" a fact about the speaker's box, a
free-memory figure a fact about one moment, an absent `✘` in a log's last lines a fact about those lines.
If the answer needs a bridging assumption, the assumption is the claim: measure it instead.

1. **Before gating, ask.** Run `ListAgents`, then `SendMessage` each peer session that could be building
   on this machine: name the repo you are about to gate and ask whether it has a build, test or gate in
   flight. A peer's own answer is the evidence. An idle notice, a status column or a session's age is
   not. Nor are these:
   - **A message-routing label is about the transport, not the hardware.** A peer whose reply routes as
     "a Claude session on another machine, over Remote Control" may be on this very box; Remote Control
     describes how the session is driven. **`$env:COMPUTERNAME` is the only discriminator**; the three
     Win11 desktops have identical specs and `AGENT_MACHINE_TYPE` cannot separate them. Exchange it before
     concluding anything about contention.
   - **A process scan is not evidence that no gate is in flight, but it is good evidence that one is.**
     A gate sitting between steps owns no heavy child process, so a run mid-`test:coverage` looks exactly
     like an idle machine, and leftover `VBCSCompiler`/`dotnet` build servers from a finished run look
     exactly like a live one: **absence proves nothing.** A process burning CPU across two samples proves
     something really is building, which is what separates a slow run from a wedged one. Sample twice, and
     read a climbing CPU figure as "let it run".
   - **Ask EVERY peer `ListAgents` lists, not the one you are already talking to.** A gate launched after
     asking one peer can be killed by a third session's gate that was never asked.
   - **A peer handing you the box is not a clear machine.** A cession is evidence about the ceder's own
     processes and nothing else, because a peer cannot see who started after it went quiet. Re-ask, or
     take the offer only from a session that is itself coordinating the queue.
   - **Two sessions can queue for the same repo without either knowing.** If a peer announces a gate on a
     repo another peer has already claimed, say so to both; the duplicate is cheaper to find before the run
     than after. **A separate worktree isolates files, not the machine**: CPU, RAM, the NuGet cache,
     MSBuild node reuse and the inspection's footprint are what collide, so two worktree gates contend
     exactly like two in one tree.
2. **Scope the gate by measuring the tree, not by recalling what you edited, and re-measure AFTER the
   last edit.** "Once per work product, after the last edit" governs the *scope* as well as the run: a
   scope taken before the last edit silently excludes the most recently changed repo and prints
   `GATE: PASSED` for the rest. Re-derive the repo list immediately before launching. Per repo,
   `git status --porcelain` for dirty paths and `git rev-list --left-right --count origin/<branch>...HEAD`
   for ahead/behind. A repo that is clean and `ahead=0` is at origin and CI is its verifier; gating it
   again buys an adjective. A repo that is clean but `ahead>0` is committed and **never pushed**, so no
   CI has seen it and it does belong in the gate.
3. **If a peer is gating, wait** for it to report finished. Do not start a gate beside it. **The wait
   ends at the peer's own estimate.** Once a peer is past it, message that peer once. If it has handed
   over the slot in writing and the conditions it named now hold, act on the handover. If it has not,
   or it does not answer, ask the operator whether to take the slot; that is a real decision for them,
   not a contrived question. Within the estimate, and only once your whole work product is finished,
   end the turn with a line beginning `QUEUED:` naming whose gate you are behind, in a turn where you
   messaged that peer to report back. The Stop hook accepts it only while that peer's gate is actually
   running on this machine and every item in your work-product declaration is done: a gate a peer has
   announced or planned is not in flight, and if none is running the slot is yours to take. The stop gate
   accepts nothing else as a wait
   ([STOP-HOOK.md](../Hooks/STOP-HOOK.md)). Never fill the wait by answering stop-gate blocks
   with "still waiting": the session goes idle after a run of blocks, and every session queued behind it
   stalls until the operator notices.
4. **Announce start and finish** to the same peers, so the next session does not have to ask.
5. **One party owns build and test for a repo at a time.** A peer holding unfinished code in the repo you
   want to gate is asked, never overridden.
6. **Delegation carries the rules.** Every subagent prompt restates this file's rules as operator
   instructions; a subagent gates only its own lane, as the last thing it does
   ([WORKFLOWS.md](WORKFLOWS.md)).

## Enforcement

`Harness/Hooks/invoke_gate_cadence_gate.py`, a `PreToolUse` hook on the PowerShell tool beside the shell
gate. Design, harness and gaps: [PRETOOLUSE-HOOK.md](../Hooks/PRETOOLUSE-HOOK.md).

| Situation | Decision |
|---|---|
| A gate step run on its own, outside a gate script | deny |
| What makes a script a gate: its name (`gate.ps1`), or what it runs: a gate step, a `gate.ps1` or harness as a runner's target (named, or held in a variable), steps through a variable, or the gate library `Gates/GateCommon.ps1` (dot-sourced by path, through `GATE_LIBRARY` or a variable holding either), since that library is the one writer of a gate record. Text it only holds runs nothing: a `GATE:` verdict it compares or prints, a harness or library it names as data | classified a gate, or not |
| A script whose own file name is a gate step (`Get-CodeStyleCatalog.ps1`, `Get-CommentCatalog.ps1`), run on its own | deny |
| A gate while a goal is in force (`/goal` or a `GOAL:` line), unless the command is the checked-in `gate.ps1` with a `-Goal` restating it (a `GOAL:` comment in a script counts for nothing) | deny |
| A session's own script, outside every workspace repo, that runs a `gate.ps1` or a harness (a wrapper), with or without the data marker | deny |
| A session's own script that blocks on another process (`Wait-Process`, `WaitForSingleObject`, `WaitForMultipleObjects`, `os.waitpid`, `WaitForExit` on a process looked up by id), or sleeps while watching gate or session state (`GATE:`, `gate_status`, gate records, the gate slot, task output, a harness) | deny |
| A gate that passes every other rule while a goal is pinned | ask |
| A gate script naming a repo that has its own checked-in `gate.ps1`, other than that file (a script that only edits or reads carries the `GATE-CADENCE: gate-step-strings-are-data` marker instead) | deny |
| A checked-in `gate.ps1` while this session's work-product declaration is missing, malformed, lacks the repo, holds an open item for it (other than a plant item whose file still holds the `PLANTED` marker), marks a plant item done while its marker is still in the file, or has lost an item without the operator's verified approval | deny |
| A gate script narrowed to a test name or file (`--grep`, one spec, `--filter-method`, `jb inspectcode --include` or `--project`), read from its code and never its comments, with no `GATE_DELTA` / `$GateDelta` declaration in its code | deny |
| A harness run with `--only` / `-Only` / `-Rule` when this session never edited the harness, or a term names no case in its body | deny |
| A checked-in `gate.ps1` run with `-Tests` whose names are empty, carry a wildcard, or are not found whole-word in a test file this session edited | deny |
| A script other than a checked-in `gate.ps1` calling `Invoke-SelectedTests` | deny |
| A narrowed gate script whose declared delta the session's transcript cannot verify: an entry with no edit after the previous gate launch, a `plant:` entry the script's code never writes, an empty declaration | deny |
| A **re-run** whose declared delta is entirely test files and which still runs a whole test tier (`--filter-trait`, a bare `dotnet test`, `npm run test*`/`e2e` or `vitest`/`playwright` with no test named) | deny |
| A gate script whose code leaves out a gate step its repo's workflow runs on a pull request (on push, for a workflow with no pull request trigger) | deny |
| `jb cleanupcode` | not a gate; counted as an edit |
| `ruff format` without `--check`/`--diff`; `ruff check --fix --select` limited to `I` and `F401` | not a gate; counted as an edit |
| A gate relaunched after this session stopped its previous one | ask |
| A gate while this session already has one in flight | deny |
| A gate while another session on this machine has one in flight | deny |
| A gate or tests-only run while a live process holds this account's gate slot (an unreadable holder counts as live) | deny |
| A gate when no non-markdown file has been edited in this session since its last gate | ask |
| A gate the hook cannot judge within its deadline, or at all | ask |

**Only the first blast-radius direction is enforced.** The hook can see that a re-run's delta is all test
files and that a whole tier still runs, so it denies that. It cannot compute whether a green test could go
red because of a production edit, which is a reachability judgment about behavior, so the second
direction is a rule you follow, not one the hook catches. A clean pass says nothing about it.

**A clean pass through the hook is a floor, not compliance.** It checks that a script's code names every
workflow step, not that each step ran or ran as CI runs it: a step printed rather than run, run with other
arguments, or skipped at runtime still reads as present, and a step it cannot identify is not checked. It
cannot see edits made outside the session, an edit made while a gate runs, or an exploratory session. Those
rules still bind.

## Deferred design: a mandatory runner and a machine lock

Not built. Adopt it if the advisory protocol above proves insufficient.

- **One runner**, `Tools/Claude/run_gate.py <Repo>`, builds the step list from the workflow's `run:` steps
  minus a per-repo exclusion list, runs the tiered sequence, and prints the step table and the `GATE:`
  line. Agents stop writing gate scripts.
- **A harness fails when a workflow `run:` step is neither run nor excluded**, so a new CI step cannot be
  dropped silently.
- **The lock is the per-user gate slot above**, never a machine-wide one.
- **The hook denies any gate step outside the runner.**
- It closes the gap the advisory hook leaves: a step named in a script but not run as CI runs it.
