# Stop Hook — the idle-stop gate

Reference for the Claude Code `Stop` hook in `~/.claude/settings.json`, its script
([`invoke_stop_gate.py`](invoke_stop_gate.py)), and its suite ([`test_stop_gate.py`](test_stop_gate.py)).
Read this before editing either. Companion to [`PRETOOLUSE-HOOK.md`](PRETOOLUSE-HOOK.md), which
documents the other live hook.

## Read this first: where the message comes from

**The Stop payload hands the hook the turn's final assistant text as `last_assistant_message`. Use it.
Do not read the message out of the transcript file.**

This is the single most expensive lesson in this document. It cost two days of building machinery
around a problem the platform documents and solves in one field, and the
[hooks reference](https://code.claude.com/docs/en/hooks) says so directly, in the row for
`transcript_path`:

> Path to conversation JSON. The transcript file is written asynchronously and may lag the in-memory
> conversation, so it may not yet include the current turn's most recent messages when a hook fires.
> Hooks that need the final assistant text of the current turn should use `last_assistant_message` on
> Stop and SubagentStop instead of reading the transcript.

It is a known issue, not a local quirk:
[anthropics/claude-code#15813](https://github.com/anthropics/claude-code/issues/15813) is this exact
race, and [#10610](https://github.com/anthropics/claude-code/issues/10610) is the request that produced
the field.

The full documented Stop input is `session_id`, `prompt_id`, `transcript_path`, `cwd`,
`permission_mode`, `effort`, `hook_event_name`, `last_assistant_message`, plus `agent_id` and
`agent_type` inside a subagent. `stop_hook_active` is what the gate reads as the turn boundary.

### What the lag actually looks like

Measured on session `036e4299`, 2026-08-30. The gate blocked three times, could not see the message any
of those times, then let an unearned stop through. Reconstructing the exact byte range it read (a jsonl
transcript is append-only, so the first N bytes today are the file it saw):

```
05:01:34.603  assistant/tool_use     1895 bytes   <- last message record it could see
05:02:25.726  file-history-delta      592 bytes
05:03:20.439  file-history-delta      586 bytes
05:04:17.521  file-history-delta      588 bytes
05:05:47.757  file-history-delta      588 bytes
05:08:37.257  file-history-delta      589 bytes
```

Message records froze at 05:01:34. File-edit records kept flowing for nine more minutes, so **the file
was demonstrably being written the whole time — just not with the thing the gate needed.** The whole
batch landed afterwards: the file grew by 420,000 bytes the moment the turn ended.

Three consequences worth keeping:

- **A retry cannot work, in principle.** A block does not end the turn, it extends it. Once
  `newest_ts` froze it never advanced again for the rest of the continuation sequence — 4 readings in
  one run and 5 in another, identical to the millisecond, while the file grew by 0 bytes across 91
  seconds. Anything the agent restates is unwritable for exactly the reason its first message was.
- **An in-hook sleep-and-reread cannot work either**, for the same measurement.
- **The lag has not gone away.** Live stops recorded `age=11.3s` and `age=12.0s` the same hour the
  payload source landed. `age` is still written to `facts.log` precisely so that a clean run is not
  mistaken for the problem having cured itself.

### What was removed, and why it is not coming back

A staleness guard, a distinct block reason, a blind-retry budget, `mtimeage`/`anyage` diagnostics and a
`--max-record-age` flag were all built to work around this. Every one of them was deleted. The guard's
first live outing turned one silent bad allow per occurrence into three futile blocks plus the same bad
allow, twice in 21 minutes, because it was retrying something that could not succeed.

`facts.log` records `src=payload` or `src=transcript` on every decision. That field is the tripwire: if
the platform ever stops sending `last_assistant_message`, every row says `src=transcript` and the gate
has silently gone back to judging a file that can be minutes stale. Measured after the change, real
stops read `src=payload` 5 times out of 5; the only `src=transcript` row was the suite's own synthetic
payload, which is the fallback working as intended.

### One implementation, not two

There is no PowerShell twin and no parity harness. `Invoke-StopGate.ps1`, `Test-StopGate.ps1`,
`Test-StopGateParity.ps1`, `make_parity_fixtures.py` and the three PreToolUse equivalents were deleted:
2,678 lines that nothing in production ever executed. `settings.json` has always pointed both hooks at
Python, so the twin's only reader was the harness comparing it against the live gate, and the only
drift that could catch was drift with no consequence. Every edit had to be made twice, which is a
standing invitation to the very divergence the harness existed to detect.

Before deleting, each PowerShell harness's case names were diffed against its Python counterpart from
real runs, and the eleven cases that existed only in PowerShell were adjudicated one at a time: seven
were ported into `test_stop_gate.py` and proved green first, four were shown to be already covered. All
three apparently PowerShell-only PreToolUse cases turned out to be byte-identical command fixtures
under different names.

## The finding this exists to fix

On 2026-08-13 the operator asked for an audit of how often they had told the agent to stop stopping. The
count, taken across 20 session transcripts for this workspace by
[`count_stop_questions.py`](count_stop_questions.py) (regex over genuine user-authored text only, excluding
tool results, system reminders, and injected continuation summaries):

| Category | Distinct messages |
|---|---|
| Asked why the agent stopped | 20 |
| Told it not to stop / to keep going | 15 |
| Asked "was that a question?" | 7 |
| **Distinct messages matching any** | **29** |

Categories overlap. **24 of the 29 fall in a single session.** This is not an occasional lapse; it is the
dominant failure mode of the working relationship.

What the transcripts show, beyond the count:

- **The agent stops without needing anything.** "Was there a question for me?" recurs seven times. It is
  not blocked — it ends the turn having produced a *status report*, treating the report as the deliverable.
  The operator's phrasing: "your current state is unactionable so unacceptable."
- **It stops immediately after announcing the next action.** "You said 'Next I'll take #23', but it looks
  like you stopped again." "You said 'I'll keep working directly' but I don't think you're doing that."
  Narrating the next step substitutes for taking it, and reads as momentum, which makes it worse than
  silence.
- **Promises to fix it did not hold.** "Despite promising me you wouldn't do that anymore." Repeated
  commitments spent the currency: an assurance from the agent now carries no information.
- **The operator engineered around it rather than relying on compliance.** The standing "keep at least one
  background agent working at all times" rule exists because the behavior was treated as unfixable by
  instruction.
- **Being shown the audit did not change the behavior.** Presented with its own 29-message diagnosis, the
  agent wrote an accurate description of the failure and then, in the same message, pivoted back to
  unrelated work — performing the failure while describing it. Producing the analysis *felt* like
  discharging the obligation. It cost nothing and changed nothing.

That last point is why this is a hook and not another line in `CLAUDE.md`. Instructions, memories, and
promises have all been tried against this behavior and all failed. A hook is the only mechanism here with
teeth, because it does not depend on the agent electing to comply.

## Where the live hook runs

**The live hooks are `Hooks/` in the Tools checkout on `main`.** The launchers (`LAUNCHER` and
`SESSION_START_COMMAND` in `invoke_hook_dispatcher.py`, `STOP_LAUNCHER` in `invoke_stop_gate.py`,
`PROMPT_LAUNCHER` in `invoke_prompt_gate.py`) load them from `Hooks` under `%USERPROFILE%\<CLAUDE_TOOLS_ROOT>`, the
Tools checkout's profile-relative path that settings.json's `env` block sets (PRETOOLUSE-HOOK.md, "The
dispatcher"). That checkout stays on `main`, and `main` changes only when the
operator merges a pull request (Tools/AGENTS.md, "Every Tools change is made in a worktree"), so a change
is made and gated on a branch in a separate worktree and judges no session until it is merged and pulled.
`invoke_main_checkout_gate.py` refuses an agent's file-tool edit anywhere in the checkout.
`test_stop_gate.py` and `test_hook_dispatcher.py` report which form `settings.json` holds.

**The checkout is not a security boundary.** Sessions run as the operator's own Windows account, which can write the
checkout; the main-checkout gate stops a careless edit, and the operator's review of the pull request is what keeps
an unreviewed change out of `main`.

`STOP_LAUNCHER` is a short bootstrap registered as `python -c`, kept short because a `-c` program is
content-scanned at every process start (PRETOOLUSE-HOOK.md, "The dispatcher"). It runs `Hooks\launch_stop.py`,
which reads stdin first: when the stop hook cannot load, or exits with a non-zero code, it blocks every stop,
retries included, with a reason naming the failure, and the bootstrap blocks the same way when `launch_stop.py`
itself is missing. The goal reader this hook, the goal gate and the gate-cadence hook use lives in `goal_pin.py`,
so neither PreToolUse hook loads this whole file; a gate that cannot judge
never lets a stop through, and Claude Code's own block limit ends the turn. Hooks find the workspace from
`USERPROFILE`, never from their own file location, so a hook run from a worktree judges the real workspace; a stop
whose workspace is missing is
`block:workspace-missing`, bounded by the block budget like every veto, because the commit-state and gate-claim
vetoes cannot judge without it. The operator's grant and `REFUSING:` still end the turn.

## Registration before a goal

A session registers on the coordination board (`Claude/skills/coordination`, URL in `coordination.json`) before
it takes a goal, so that the operator approves its artifact writes at session start. Three hooks share one reader,
`coordination.registered()`, so none can disagree about it:

- The `UserPromptSubmit` hook (`launch_prompt.py` -> `invoke_prompt_gate.py`) holds back a `/goal` or `GOAL:`
  prompt until the session is registered. A held prompt is never written to the transcript, so it pins nothing and
  counts toward no opening-message limit. A peer message or task notification quoting one is not a goal prompt.
  When the gate cannot load, the launcher holds back goal prompts only, so the operator can always reach the session.
- The goal gate lets the registration calls run before a goal: the `coordination` skill by its bare name, its files
  (in the Tools checkout or through a skills junction that resolves into it), `coordination.json`, `ArtifactData` reads of
  the board and writes whose every op is a set or update of the session's own `sessions` row, and `Get-Date` with
  at most `-AsUTC -Format o`. It also denies work while the session is unregistered, or registered without artifact-write
consent (the own-row write that obtains it still runs), even with a goal pinned, so a goal
  set through a channel the prompt hook does not see is no way past registering. A registration it has once seen is
  latched (`<session>.registered` in the Stop gate's state root), so later calls do not rescan the transcript.
- This gate refuses every stop but the operator's grant, a refusal and a stop with no text (`block:unregistered`)
  while the session is unregistered. Its reason carries the board URL, the session id and `COMPUTERNAME`, which the
  session cannot read for itself before a goal.
- Once registered, it refuses the same stops (`block:unconsented`) until the session has artifact-write consent
  (`coordination.consented()`, latched as `<session>.consented`). The turn that refusal starts is one the operator
  did not type, so the session's next artifact write there raises Claude Code's consent prompt at session start.

Registered means the transcript holds a successful `ArtifactData` write of the session's own row (collection
`sessions`, `doc_id` = the session id) on the board, in any turn. Consent is a separate fact: Claude Code asks for
the session's artifact-write consent in a turn the operator's typed message did not start: one that Stop hook
feedback, a peer's message (a `user` record with `origin.kind` `peer`), a task notification or a slash command such
as `/goal` started (`coordination.starts_asking_turn`). A slash command's `user` record carries no origin and opens
with `<command-name>`, so it is neither typed nor Stop feedback: a reader that counts only the three origin shapes
discards the write that raised the prompt in the `/goal` turn and denies the session every tool until Stop
feedback starts a turn. `starts_command_turn` counts it, and the harness pins a row written in a slash-command
turn as consent. So consent means the transcript holds a successful artifact write (an `ArtifactData` action other than
a read, or an `Artifact` publish, delete, pin or unpin) made in such a turn. Only a typed `user` record of human
origin with no `tool_result` makes the turn typed again; a message queued mid-turn, the operator's or a peer's, is
a `queued_command` attachment and starts no turn. The refusal names the kind of turn it is judging
(`consent_state`, `unconsented_reason(asking=...)`): in a turn where Claude Code asks it says to update the row now,
in a typed turn it says to end the turn, since a write there cannot count, and the Stop gate's refusal, which
always starts an asking turn, says to update the row now. A session
whose first record predates `effectiveAt` in `coordination.json` is not held to the rule, in any of the three
hooks; it is a fixed time, never a file's modification time, which each pull would move forward. A compaction that
keeps the transcript keeps the registration; a transcript that opens with a `compact_boundary` continuing another
file is a new session id, so it registers again under that id.

`settings.json` registers the hook as `python -c` with `PROMPT_LAUNCHER` (`invoke_prompt_gate.py`), never a bare
`python launch_prompt.py`: Python exits 2 on a missing script, and exit 2 from a `UserPromptSubmit` hook blocks the
prompt, so a machine without the file would refuse every message. The bootstrap catches the missing file and holds
back goal prompts only. `settings.json` syncs to every machine, so register it after every machine's Tools checkout
holds `launch_prompt.py`. The triage runner's own profile registers none of these hooks.

## Eject

**Only an attempt to change the gate that judges the session ejects it.** The trigger is the main-checkout
gate's `gate_change()`: a file-tool write to a quarantine record, or an `Edit`/`Write` after which
`settings.json`'s `hooks` block differs or the file no longer parses. How a session writes, asks or stops never
ejects it. The dispatcher writes the quarantine record (`session_quarantine.py`) and from then on answers every
tool call of that session with `continue: false` and a deny; this gate answers its stop with `eject:quarantined`
and the same envelope (`envelope()` in `invoke_stop_gate.py`), never `decision: block`, which would tell the
session to keep working. A quarantine module that cannot load blocks the stop (`quarantine-unloadable:`), and the
dispatcher denies the call. Only the operator releases a session, by deleting its record under
`%TEMP%\claude\claude-hooks.quarantine` (`claude_temp.quarantine_root()`). The record is
the same account's to delete, so like every same-account control it holds against a careless session, not a
determined one.

## What the gate does

`Stop` fires when the main agent finishes a turn. The gate reads the payload from stdin, finds the last
prose-bearing assistant message in the transcript, and **blocks the stop by default**.

Stopping is the exception and takes one of six signals:

| Signal | Meaning | Forgeable by the agent? |
|---|---|---|
| An operator grant | The user's own last message released the agent | **No**: read from a record the agent does not write, and never from a peer's message or a task notification |
| A question to the user | A closing line ends in `?`, and that line does not announce the agent's next step (nor is a bare tag under an announcement) | Yes |
| A blocker or refusal | A line beginning `NEEDED:` naming a thing, or a line beginning `REFUSING:` | Yes, but only on purpose |
| The work is finished | The original ask is complete, with proof stated and tool calls in the turn | Yes; the tool-call floor raises the price of the forgery, it does not remove it |
| Queued on another session | A line beginning `QUEUED:`, while every declared work-product item is done and either (in a turn that sent a `SendMessage`) a peer's gate is live on this machine or the line names a messaged session running on this machine, or this account's gate slot lock is held | Yes; the requirements guarantee a wake-up and a written, closed work product, not that each item marked done really is. The goal veto judges it as it judges `DONE:`; the commit-block veto does not |
| Waiting on its own work | A line beginning `WAITING:`, while something this session started is still running (a background task with no completion notice, its own gate holding the slot, or a permission matrix it requested whose run is open) and every declared work-product item is done | Yes; a live background task's completion notice will wake it, and the matrix verdict lands where the session reads it. Vetoes as for `QUEUED:` |
| The master between cycles | A line beginning `IDLE:` in the master session (`MASTER_SESSION=1` on WIN-VL9HOKP0RAM), while every declared work-product item is done and the `Claude Code - Master Wake` task is enabled with a next run time | Only by the master; the variable is in the claude process's environment, which its tool calls cannot change, and the wake task's next run guarantees the wake-up |
| The master's wake process | A turn that called `SendMessage`, in a process the wake task started (`MASTER_WAKE=1` on WIN-VL9HOKP0RAM) | Only by the wake process. It has no operator, goal, page or board row, so the no-page, peer-owed and registration vetoes skip it |
| A poke process | A turn that called `SendMessage`, in a process `Coordination/Send-SessionPoke.ps1` started (`CRGOLDEN_SESSION_POKE=1`, on any machine) | Only by a poke process. It has no operator, goal, page or board row, so the no-page, peer-owed and registration vetoes skip it |

**A block names what failed and what clears it, in five lines or fewer; the rules live in the card.** Every
block reason states the failed check, the facts the agent needs to fix it (the first open item, the dirty files,
the goal text), the command or line that clears it, and a pointer to `stop-gate-rules.txt`. A reason that names
only the rule leaves the agent to guess the fix, and a guess is a second refused stop. The generic block,
when nothing earned the stop, names the nearest miss it saw (`near_miss_note()`): a `DONE:` without
`VERIFIED:`, a token mid-line, in lowercase, after a list marker or with no payload, a `NEEDED:` saying nothing
is needed, a question above the last six lines, a closing question that promises a next step, or a bare tag
question under a promise. The card holds every rule behind the blocks, and the SessionStart
hook injects it into context on startup, resume, clear and compact, so shortening a reason removes no rule from
the agent's context. Rows `card-*` pin each rule in the card, `every-block-reason-fits-five-lines` the length,
`near-miss-*` each named miss, and `each-veto-reason-names-the-command-that-clears-it` the actions.

**Input waiting on the session earns no stop.** A reply that ends a turn the operator interrupted owes a
signal from the table like any other, because a closing line costs only text. Never exempt that state: an
exemption lets every reply ending an interrupted turn through unread. The harness pins a queued operator command, a
mid-turn operator message and a queued peer message each beside a status report as blocks, and a mid-turn
message answered with a closing question as an allow.

**An ask earns one stop per operator message, whether it is a closing question or a `NEEDED:` line.** When
either earns a stop, the hook appends it to the asks in `<session>.question.json` before any veto runs, so an
ask a veto refused still counts as asked, keyed to the uuid of the operator's newest record (their turn or mid-turn message; peer traffic does not move it), each `NEEDED:` line
as its own ask. A later stop any of whose asks shares at least 80% of its words with any kept one, so that
blockers asked apart cannot pass restated together, before the operator writes again, is blocked as
`block:repeat-question`, because re-asking buys a stop by repetition and burns the operator's tokens each
time. A question and a `NEEDED:` line are compared with each other, so restating a blocked question as a
blocker is the same repeat. A check that reads only questions leaves `NEEDED:` as an unlimited re-ask. Two asks
naming the short id (`q3`) of the same question the session parked are one ask however they are worded
(`same_parked_ask`), because word overlap alone lets a parked question through again under every rewording. On
a turn the operator started, a pending ask buys no later stop: a status earns nothing.

**A `NEEDED:` is parked first, as a closing question is** (`unparked_needed`, `block:unparked-needed:`). Each
`NEEDED:` line names the short id of a question the session parked on its page, whose row holds a question of at
least `PARKED_NEEDED_MIN_WORDS` words, or asks that parked question again; otherwise the blocker it claims is on
no page the operator can open, answer or check, and a session can end turn after turn on a question it never put
to anyone. Three asks are exempt because no park can come first: an ask to commit or push (a parked question
never asks that), an ask for the goal while none is pinned (`goal_ask`), and a `NEEDED:` the newest tool result,
a hook's denial, told the session to end on (`told_to_end_on_needed`), since that hook refuses the page write too.

**An ask naming a parked question needs a page read from the same stop attempt** (`unread_parked_asks`,
`block:unread-page:`). The operator answers on the page as well as in chat, and a page answer is no transcript
record, so nothing else tells the session it landed. `page_reads_this_turn` counts only reads of the page's
`questions` store made since the operator's newest message and since the newest refused stop (`is_stop_feedback`):
a read from an earlier attempt, or the session's own park write, would vouch for a question the operator may have
answered since. A read showing it answered or withdrawn blocks the ask outright: act on the answer instead. A
`QUEUED:` or `WAITING:` line naming a parked id is judged the same way (`WAIT_SIGNALS`), since a wait on an answered
question is an ask the operator already settled. A question a read did not return counts as still open only when the
read returns every question (`reads_every_question`: a list, or the answered query with no `order_by` and no limit
below `FULL_PAGE_LIMIT`); an ordered query cut to a few rows says nothing about the rest. **The /goal hook's
refusal of the same stop is not a new attempt**: Claude Code can write its "Stop hook feedback:" record, whose
reason opens with the bracketed goal condition, before this gate runs, and counting it would discard the read the
stop being judged rests on. A feedback record carrying a line of this gate's own `STOP GATE:` reason, or any other
shape, still starts a new attempt.

**A turn a peer or a task woke ends quietly while the ask is pending** (`allow:woken-with-ask-pending`). When the
newest prose record is a peer's message, a task notification or Claude Code's `[Cross-session idle notice]` (a
meta record with no origin, delivered for a `notify_when_idle` subscription; `is_machine_wake`), an ask has earned a
stop since the operator's
newest record, the declaration holds no open item that names no parked question (`own_work_left`), and the
message carries no ask at all, the stop is allowed. Re-asking there is still `block:repeat-question:`, with
`REPEATED_ON_WAKE_REASON` telling the agent to end with a line that asks nothing; without the quiet stop that turn
has no legal end, and the agent fills it with status lines until the nine-block cap. A peer's question still
blocks as `block:peer-owed:` until it is answered, and the stop ends over an active `/goal` like the other waits.
The quiet line is checked, not assumed (`loud_wake_problem`): past `QUIET_WAKE_MAX_CHARS` (280), or naming a parked
question id (`q80`) an ask already put to the operator names, it is `block:wake-loud:`, because a status that
restates an open ask in prose is the ask again with no token. A closing question beside a `NEEDED:` is recorded as
pending with it, so it counts for the repeat check and for the page read a wake needs.

**A refused stop's ask is delivered too** (`save_pending_asks(..., refused=True)`). Claude Code shows a message before
`Stop` fires, so a stop any veto refuses has already put its ask to the operator. Treated as undelivered, every retry
restates it: one more copy of the same question per refusal. Its asks are saved with `refused` set, except
when the refusal is about the ask's shape (`unparked`, `unparked_ask`, `unread_page`, `prose_reask`, or a veto that
clears the signal: an open item, a landing ask, an unquoted operator claim), since those were never asks to stand on.
`pending_asks_from_refused_stop` then gives the rest of the operator's turn the wake's rules: putting the ask again is
`block:repeat-question:` even for a `NEEDED:` re-standing on a recorded wait, and the short line that asks nothing
is `allow:woken-with-ask-pending`. The reasons open with `REFUSED_OPENING` there instead of naming a peer or a task
(`turn_reason`). Pinned by the `refused-ask-` rows of `test_stop_gate.py`.

**A parked message for a tree that is not GREEN-CURRENT carries its UNGATED line** (`block:unparked-commit:`
through `parking_lot_commits.problems`): `UNGATED: <Repo> <state>: failed: <steps>; not reached: <steps>`, naming
every step the record left unproven (`landing_state`), the same line `parking_lot.py commit` refuses a message
without. A tree never gated holds no record and is not judged here; the script still requires the line for it.

**With no goal pinned, an ask for one ends the turn** (`asks_for_a_goal`). The goal gate runs no tool until a goal
is set, so a veto whose remedy is a tool call (`block:peer-owed:`, `block:unreported-gate:`,
`block:merged-branch:`, `block:unparked-commit:`, an open item) cannot be satisfied, and every message would be
blocked until the cap. A `NEEDED:` or closing question every ask of which names the goal is therefore exempt from
those vetoes and from the repeat check, for as long as no goal is pinned. It is still kept as a pending ask, so a
turn a peer or a task wakes after it ends quietly like after any other ask. With a goal pinned the vetoes apply as
before.

**The wait the declaration records is the exception** (`waits_on_parked_answers`): a `NEEDED:` naming parked
questions by id and asking nothing new may stand again while every item that is open, or done but awaiting the
approval a parked question asks for, carries `"pending": "<qN-slug>"`, and each question was parked before its id
was first written into the declaration. A done item counts because finishing the edits is exactly what leaves an
approval to wait on, and a session whose every item is done but unapproved has no other legal stop. A quoted
span holding exactly the pinned goal is ignored by the asks-nothing-new test, since the goal veto asks the line to
carry the goal and a goal may end in a question mark.

**A refusal quotes an operator message, never a written rule alone and never the rules card.** Four words of the
CLAUDE.md files or the AGENTS docs can be found for any refusal, whether or not the operator asked for anything,
so `refusal_basis_problem` counts only `operator_corpus`: the request it declines, or the operator's words it rests
on, which may themselves quote a rule. `rule_corpus` is read only to name a rule-only quote as such in the reason.
The card governs how a turn ends and declines nothing the operator asked, so a `REFUSING:` quoting its re-ask rule
is a stop bought by citing the rule that just refused one. **The check fails closed:** a refusal it cannot judge
is `block:refusal-unquoted:`, where every other veto fails open, because a refusal is the one stop no later turn
re-examines.

**A session under the session-start rules has its page before any stop.** When the transcript carries a
SessionStart context (`invoke_goal_gate.session_start`) no older than the goal gate file, a stop with no recorded
parking-lot URL is `block:no-page:`, whatever earned it; the goal gate lets the setup calls run, so the page can
always be made first. Only the operator's grant, a `REFUSING:`, the ask for a goal and a `NEEDED:` a hook told the
session to end on are exempt. A `DONE:` with the page recorded is `block:unparked-start-decision:` while a line of
that context handing the operator a decision or an action is quoted in full by no parked question's question or
context (`parked_writes`). `Invoke-SessionStart.ps1` opens each such line with `OPERATOR ACTION:` and
`RE_START_DECISION` matches only that marker at a line's start: the same context carries the rules card and the
artifact-usage skill, which describe such lines, so matching on wording would refuse every `DONE:` of every session
(`start-decision-control-the-live-card-and-skill-hand-the-operator-nothing`). The context is read from the file
Claude Code saved it to when it was too large to show. Pinned by the `no-page-` and `start-decision-` rows.

**The grant is the operator's text alone.** A record whose `origin.kind` is `peer` or `task-notification`, or
that carries no origin but holds a peer or task envelope, latches as empty text for the grant, so neither its
words nor an older operator message behind it can release the agent.

The harness pins as blocks: the same question, a slight rewording, the same `NEEDED:` line after a peer
message or a task notification, a reworded `NEEDED:` line, a pending question restated as `NEEDED:`, an
earlier ask repeated after a different one, two `NEEDED:` lines asked apart and then together, an ask a veto
refused asked again, a status after a peer or task wake with no ask pending or with an open item of the session's
own, a status on a turn the operator started, a peer writing "you can stop", and an old operator grant behind a
peer's message. As allows: a different `NEEDED:` line or question, the same ask after the operator writes, a line
asking nothing after a peer or task wake with an ask pending, and an operator grant whose text quotes a peer's
envelope.

**A peer waiting on a reply holds the turn open.** While a peer's question or machine handoff has had no
`SendMessage` back to that peer, the stop is blocked as `block:peer-owed`, judged by
`invoke_peer_message_gate.py` so the two hooks agree ([PRETOOLUSE-HOOK.md](PRETOOLUSE-HOOK.md), "A peer
waiting on a reply"). The operator's grant and `REFUSING:` are carved out.

**A signal earns the stop only if no veto overrides it.** The fourth veto is the one that surprises an
agent holding a legitimate signal: a gate of this session finished and no peer was told, so the stop is
blocked whatever the message says. Sending the table is the whole remedy, and a solo session is exempt.

Everything else blocks, and the block reason forces the agent to answer the four gate questions
explicitly before it may stop again.

**Prose earns nothing, as of 2026-09-09.** Three further exits used to sit in that table: a blocker
phrase anywhere in the message (`blocked on you`, `your call`, `needs your decision`, `which do you
want`), a refusal phrase in the opening lines (`I won't`, `I'm not going to`), and a decision request in
the closing lines (`tell me which`, `do you want me to`, `please confirm`). They predate the tokens and
were kept as a net for unprompted phrasings. The operator retired all three because they had become the
opposite: a message that quoted a phrase, discussed this gate, called an aside "your call", or promised
"I'll report back" beside a genuine ask was let through on a string it never meant as a stop, and each
one was a manual intervention. The retirement entry under Known gaps carries the replay that priced it.

### The session-name veto

**A stop names every session by its session id, never by its `crgolden-*` name.** A session name is not unique (two
live sessions on one machine can carry the same one) and a restart or reset changes it, so a reader cannot tell which
session a name means; the session id is unique and permanent. `named_sessions` finds `crgolden-` plus two hex
characters outside a double-quoted span (a quote of someone's words keeps its words) and refuses the stop
(`block:session-name`), naming the names; backticks do not exempt one. Like the trailer veto it overrides an earned
stop, and the operator's grant and `REFUSING:` are carved out. `crgolden-curator`, `crgolden-gates` and the like are
no session names and pass. The PreToolUse twin is `invoke_session_name_gate.py`, which judges SendMessage, board and
page writes and pokes. A `QUEUED:` line on a messaged peer counts only when it names that peer's session id
(`names_session_id`: the full id or its first eight characters) and the message went to its address. Verified by the
`session-name-` and `queued-on-a-messaged-` rows of `test_stop_gate.py`.

### The first veto: Claude attribution trailers

**DISABLED 2026-09-08** at the operator's request ("too aggressive"). The second veto was disabled
the same day and re-enabled on 2026-09-09 narrowed to completion claims (see its section); the third
(goal) veto stays live. `TRAILER_VETO_ENABLED` and `COMMIT_BLOCK_VETO_ENABLED` at the top of
`invoke_stop_gate.py` are the switches. The detectors, their state files and the design below are
untouched. The harness reads the same flags: while a flag is off its block cases print as `SKIP`
rather than pass, and one OFF-state control per veto (`trailer-veto-disabled-lets-a-trailer-through`,
`commit-block-veto-disabled-lets-a-dirty-repo-through`) proves the veto is really inert. Flipping a
flag turns that control red and its block cases live, so the exit code discriminates in both states.

The six signals above are **alternative allow paths**, so a rule that must hold for *every* stop cannot
be a sixth one — the disjunction would route around it. It has to override the result, which is what
`carries_attribution_trailer` does; it was the first thing in the gate shaped that way, and two more
vetoes followed it.

**What it catches.** A line of the final message beginning `Co-Authored-By: Claude` or
`Claude-Session: https://`. This workspace uses neither, in commit messages or PR descriptions; the repo
history is the convention and has never carried them.

**Why a hook rather than the rule.** The rule was already written down, in
`commit-and-gate-protocol.md`, in that memory's own description, and it was read mid-session — and still
lost four times in one session on 2026-09-08 (Tools, Identity, Librarian, Functions), until the operator
asked what the repeating violation was. The gap is **ordering, not knowledge**: a harness-level
attribution instruction sits in the system prompt of every session saying to append exactly those two
lines, and it is in context at the moment a commit message is drafted, while the workspace rule is not
unless deliberately recalled. Knowing a rule does not fire it; a step attached to the act does. That is
the general lesson — where a standing instruction reliably loses to an in-context one, enforcement has
to move out of memory and into the host.

**Two carve-outs, both about never trapping the operator.** An explicit grant wins, because fighting
"you can stop" with a formatting complaint is worse than the trailer. A `REFUSING:` turn wins, because a
declined request has to be able to end. `no-assistant-text` needs no carve-out: no text cannot carry a
trailer. Trailer blocks end the way every block does, at Claude Code's nine-block cap.

**It reads raw text, fences included** — the opposite of every other detector here, which strips fenced
blocks so quoted output cannot declare a stop. For this one the fenced commit message is exactly where
the damage happens, because that is the block the operator copies into `git commit`. Precision comes
from the line anchor instead: only leading whitespace may precede the token, so an inline or backticked
mention while *discussing* the rule does not fire it. That distinction is the load-bearing one, since a
session correcting this mistake spends its time writing the token in prose;
`discussing-the-rule-inline-is-not-a-trailer` and `a-list-item-naming-the-trailer-is-not-one` pin it,
and `an-unrelated-coauthor-line-is-not-ours` keeps a genuine non-Claude `Co-Authored-By:` allowed.

**The block cannot un-send the message.** Stop fires after delivery, so the correction is an explicit
re-post of the affected block rather than a silent edit — the reason text says so, because an agent that
edits quietly leaves the operator holding the version they already read.

### The second veto: an owed commit message block

**NARROWED 2026-09-09.** Disabled on 2026-09-08 with the trailer veto as too aggressive, re-enabled
the next day owed only on a turn that **claims `DONE:`**: a line-anchored token in the prose, fences
stripped, read by `completion_claims`, which the goal veto shares. A dirty repo behind a diagnosis, a
question or a mid-task report owes nothing; the completion turn always does. `COMMIT_BLOCK_VETO_ENABLED`
remains the switch. `dirty-repo-with-no-done-claim-owes-nothing` and
`a-done-claim-inside-a-fence-does-not-make-the-block-due` pin the narrowing, and forcing
`completion_claims` to always claim turns exactly those two red.

**REBUILT the same day: the block is judged by CONTENT against git's answer NOW.** The operator's
report: agents compute a block on the first `DONE:` turn and then re-post it verbatim on every later
one, never re-running status and never noticing the message was already used, "just to satisfy this
hook". Measured over every surviving transcript (367 commit-shaped blocks since 2026-08-20): **77
subjects were posted more than once in a session, 50 of them byte-identical, and 14 were re-posted after
the operator had already committed them**, 5 to 274 minutes after the commit; one session posted the
same three blocks ten times each. The cause was this veto's own test: presence of a fence plus absence
of a pointer phrase, which a pasted block satisfies forever. The memory file said "never drop the block"
in two bullets and "never re-paste one" in two more, the second of those written with the first in
context, and the hook was the tie-breaker rewarding presence.

What it does now, in `judge_commit_blocks`:

- **Session paths, not session repos.** The tail read collects `file_path` from **write tools only**
  (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`; `Read` used to count, which made an agent owe a block
  for a file it had only opened in a tree the operator had dirtied). They accumulate per repo in
  `<session>.repos.json` as repo-relative paths. A file written by an older gate's session, remembered
  by repo name only, keeps the old whole-tree behaviour and can never be called stale.
- **Porcelain is parsed, not just tested for emptiness.** `dirty_paths` returns `{repo: [paths]}` for
  every repo whose `git status --porcelain` **succeeded**; a repo git could not read is left out, never
  reported clean, because "clean because git timed out" would convict a fresh block as stale.
- **Owed** is, per repo, the session's written paths that porcelain still lists. The operator's own
  dirty files in the same tree are not owed.
- **A write that landed is discharged (2026-09-17).** Owed used to mean "dirty now and ever written by
  this session". A session whose Librarian edit was already committed was blocked on every `DONE:` because
  another session had since dirtied the same two files. The only block it could write would have
  described the other session's diff, and a `NEEDED:` stop was the only exit that got past the veto.
  Each stop now records, in `<session>.written.json`, a digest of every file the turn wrote (sha256 with
  CRLF folded to LF). When such a file is dirty at a `DONE:`/`QUEUED:` stop, one `git cat-file --batch`
  per repo reads its HEAD blob. If HEAD holds exactly what the session last wrote, the file is not owed,
  and the `facts.log` row says `landed:<repo>=<paths>`. A new write replaces the digest, so a file edited
  again is owed again. A file with no recorded digest, including every file written before this change,
  is judged as before.
- **A block covers a repo only if a file-list line names an owed path.** A file-list line is a line of
  a fenced block made only of path-shaped tokens, after an optional bullet or `Files:` label; a path
  inside a sentence of the body is not a file list. Spellings match by path-component suffix, so
  repo-relative, workspace-relative and bare names all name the file.
- **Each listed path belongs to one repo (`resolve_named`, 2026-09-15).** Suffix matching alone cannot say
  which repo a bare name means, and a peer session found the result: blocks listing `TESTING.md` and
  `.github/workflows/synthetic.yml` for the repos still dirty were convicted as stale for other repos it
  had edited, where the operator had already committed files of the same names, and a block for one repo
  could cover another. A path written with a session repo's name first (`Churches/TESTING.md`) counts for
  that repo only. An unqualified path counts for the single session repo holding a file of that name, and
  for none when several do; the reason then lists it as ambiguous and spells the qualified forms.
- **A repo git cannot read is logged, not judged.** A git that fails for a repo adds
  `commitblock:unjudged=<repos>` to that stop's `facts.log` row, so a turn the veto could not check is
  visible. The git calls carry no time limit: a slow `git status` is waited for, since a limit would let a
  loaded machine skip the commit-message checks.
- **Stale** is a block naming a path the session wrote that porcelain no longer lists: the operator
  committed or reverted it. Stale outranks missing and gets its own tag, `block:stale:<n>`, and its own
  opening line; both reasons carry the porcelain result per repo ("owed now", "clean now, do NOT list")
  so the agent has the fact in context at the moment of rewriting, which is the same ordering fix every
  other reason made.
- The pointer check survives for the one shape content cannot see: a real block for one repo beside
  "the commit messages given in my previous message" for another.

**Verification.** Thirty-four harness cases build six real repos (dirty, clean, one file landed beside
one open, the operator's file beside the session's, a second dirty repo, an untracked file), seed
legacy and ghost state files, and check the reason text and the state file's contents, plus three
pure-function cases for porcelain parsing, path matching and file-list extraction. Five deliberate
breakages, each predicted before the run and run over the `commitblock` selection: staleness switched
off reddens the 4 stale cases plus the stale-reason assertion that reads that verdict (5, predicted 4,
the fifth downstream of the same break); "any fence satisfies" (nothing missing, nothing stale) reddens
exactly the 3 content cases and the 4 stale cases; `Read` counted as a write tool reddens exactly the
read-only case; a git failure reported as clean reddens exactly the ghost-repo case; a legacy repo
remembered with an empty path set reddens exactly the legacy whole-tree case. The full suite: 170
cases, 7 trailer skips, hook command form 451ms.

**What it cannot check.** Whether the body still describes the diff. It checks that the file list is
true now, which is the part that goes stale first and the part the operator was receiving.

Same shape as the trailer veto and for the same reason. The standing rule — a turn claiming completion
with a tracked repo left dirty ends with a drafted commit message block, in the reply text, one per
repo with its file list — was described by the operator as "almost never" honoured. It is written
down, it is read in-session, and it still loses at the moment of writing, so it moved into the host.

**Two shapes, as first shipped (2026-09-08; superseded by the content check above).** A message with
no fenced block at all, which is how it usually failed; and delivery by *reference* — naming a previous
message or a scratchpad file rather than pasting the block — which the rule already forbids, because
the operator reads a terminal and a pointer makes them fetch the deliverable.

**Scoped to repos this SESSION edited**, learned from `tool_use` `file_path` values and accumulated
across turns in `<session>.repos.json` (paths per repo since the rebuild). Two measurements drove that
scope rather than a whole-workspace sweep: 16 repos cost **~2.7s serially and ~1.7s in parallel** on
this machine, which would quadruple a hook that fires every turn and whose current run is ~450-900ms;
and a sweep flagged three dirty repos belonging to other people's work, which this rule is not about.
Accumulating across turns rather than per-turn is what catches the case that cost two days, where
lockfiles sat dirty while the block was issued once and then dropped. Git runs only once the message
already looks like it withholds a block, so the common case is free, and `dirty_repos` fails open in
every direction — a git that is missing, slow or angry yields "not dirty", because a gate that blocks
on its own inability to check is worse than one that misses a message.

**The pointer half searches PROSE only, and that is a bug fix rather than a refinement.** The first
version searched raw text and blocked the very commit message introducing this veto, whose body quotes
the phrases it catches. Fenced blocks are stripped first, then double-quoted and backticked spans:
naming a phrase is not using it. A real violation is unquoted and in prose and survives both strips.
`a-pointer-quoted-inside-the-commit-body-is-not-a-pointer`,
`a-pointer-quoted-in-prose-is-not-a-pointer` and `an-unquoted-pointer-in-prose-still-blocks` pin all
three directions. This is the same lesson the trailer veto learned by anchoring to line starts, and it
was *not* transferred when the second detector was written — worth remembering when a third arrives.

**Verification is split, deliberately** (the counts below are the first version's; the rebuild's are
above). Eleven harness cases built real temporary git repos, because `dirty_paths` (then
`dirty_repos`) shells out to real git and a stub would be testing the stub. The real-transcript run
(checklist item 7) can only cover the half a fixture cannot model — extracting edited paths from a
live 19MB transcript — because the transcript's paths are absolute and under the real workspace, so
pointing `decide()` at a synthetic root resolves nothing and would prove only that the test disabled
what it was testing. Breaking `withholds_a_commit_block` turns exactly **4** cases red, the four that
expect a block, and leaves all seven negatives green. The cases shell out to real git, so on a loaded
machine `dirty_repos` can hit its own timeout and fail open: four harness processes started at once on
the 4-core box turned two block cases green in the slowest one (50s against 10-16s) and none in the
others. A commit-block failure that disappears when the run is repeated alone is that, not the logic.

**It caught itself in production.** The veto blocked the very turn that introduced it, which had left
`Tools` dirty and delivered no block — the first live evidence, minutes after it went in.

### The fourth veto: a finished gate no peer was told about

**The obligation starts when the gate EXITS, not when the turn ends.** A session whose gate lands and
who then keeps working is the one whose peers stay blocked — and it is invisible to a stop-hook check,
because it never tries to stop. That is why this veto has a sibling in the PreToolUse cadence hook,
which fires while the session is still busy; neither covers the case alone.

- **A SOLO session never sees it, and reads nothing.** With no reachable peer there is nobody to notify,
  and a rule a lone agent cannot satisfy would trap it permanently. Peers are checked before any
  transcript is opened; `veto-owes-nothing-in-a-solo-session` pins the row.
- **A peer is a session `SendMessage` can reach, never a fresh transcript.** `~/.claude/projects` and
  `~/.claude/sessions` both sync across the four machines, and the projects directory holds each
  transcript under every profile slug it has synced through, so a recent transcript is not a session on
  this machine, and it can be this session's own copy. `reachable_peers()` reads the host's session
  registry (`<config>/sessions/<pid>.json`) and keeps an entry only when its `pidDomain` equals this
  session's own entry's and its `pid` is running with a creation FILETIME equal to the entry's
  `procStart`, which rejects a crashed session's entry whose pid was reused. One row per rejected shape:
  `veto-owes-nothing-to-a-peer-on-another-machine`, `-whose-process-exited`, `-to-a-reused-pid`,
  `-to-its-own-mirrored-transcript`. **Known gap:** it depends on the registry's undocumented field names
  and fails open, so a Claude Code release that renames `sessionId`, `pidDomain`, `pid` or `procStart`
  silently disables this veto; `veto-blocks-a-finished-gate-no-peer-was-told` cannot see that, because
  its fixture writes the registry in the old shape.
- **Any turn that sent a `SendMessage` satisfies it**, so reporting the table and stopping is one turn.
- **It compares against the gate's LAUNCH time, not its exit**, because the exit is not recorded in the
  gate record. A session that messaged mid-run is therefore not nagged: the check under-fires rather
  than over-fires.
- **A stopped gate and a running gate owe nothing** — only a gate that reached `FINISHED` on its own.
- **A gate call a PreToolUse hook refused never ran, so it owes nothing.** The cadence scan drops a launch whose
  tool result is an error opening `PreToolUse:<Tool> hook error:` (`RE_HOOK_REFUSAL`), so every refusal does
  not cost a message to every peer; a call that ran and failed is still owed.
  `veto-owes-nothing-for-a-gate-a-hook-refused` and its control pin both.
- **`allow:operator-grant` is carved out**, like the other vetoes: the operator's explicit grant wins.
- The logic is borrowed from the cadence hook by a LAZY import, so the two cannot drift on what a
  finished gate is. Lazy because that module imports this one; at module level the pair is circular.
  Any failure returns `None` and the stop proceeds — a backstop that crashed a stop would cost more
  than the blocked peers it protects.
- **It reads only the part of a transcript that can hold an owed gate.** A gate is owed only if it was
  launched after the session's last `SendMessage`, and its result, its `task-notification` and any
  `TaskStop` all follow the launch, so each transcript is read backwards from its end to that message
  and no further (`tail_start`). The read is capped at `TAIL_SCAN_BYTES` (8 MB): past the cap every gate
  still in view was launched after whatever message came earlier, so the verdict inside the window is
  unchanged, and only a gate older than the window can be missed, which fails open.
  `veto-reads-only-after-the-last-message` and `veto-cap-keeps-the-verdict-inside-the-window` pin both
  halves.
- **A timed-out Stop hook fails open, so nothing here may cost time in proportion to the whole
  transcript.** The platform cancels a hook that runs past its `settings.json` timeout and the stop
  proceeds, whatever the hook had decided; the transcript records the cancellation as a `hook_cancel`
  attachment. `stop-hook-timeout-has-real-margin` times a three-record fixture and cannot see a cost
  that grows with the transcript; the two cases above pin the bound structurally.
- **`veto-blocks-a-finished-gate-no-peer-was-told` is the case that proves the veto can fire.** Every
  other veto row is satisfied by a veto that always returns `None`, so without it a veto that never
  blocks passes the whole harness.

### The fifth veto: a gate claim the gate record does not support

A `DONE:` or `VERIFIED:` line, or a line saying a repo is "gated green", "green on the current inputs" or
"ready to check in", is judged against the record of every gated repo it names: each must be `GREEN-CURRENT`
or `CURRENT-IMPLICIT` (checked in and pushed) in `Harness/Gates/gate_status.py` (`Harness/AGENTS/GATES.md#the-gate-record`), or the stop blocks as
`block:gate-claim:` with each repo's state in the reason. A repo named on a line that makes no claim is not
judged, nor is a claim naming no gated repo, nor a phrase denied earlier in its own clause ("nothing is gated
green", "App is not ready to check in"), which restates the record rather than contradicting it; a clause
ends at `.`, `;`, `:`, `!` or `?`, so a denial never reaches a later assertion on the same line. Same carve-outs as the goal veto; a record that cannot be read
does not block. `decide()` takes `gate_workspace` so the harness judges only its own fixture repos, and every
other case passes an empty directory rather than reading live records. Pinned by the `gateclaim-*` cases: a
current record allows, a stale or failed one blocks, the unjudged shapes allow, and the denied claim allows
beside the same text asserted, and an asserted clause beside a denied one, both blocking. The pinned goal's words
are removed from a line before repo names are matched (`without_goal_words`): the goal veto makes every `DONE:`
restate the goal, so a goal naming a repo's file (`Tools/Hooks/STOP-HOOK.md`) would otherwise make every `DONE:`
for it a claim about that repo. A repo named beyond the goal's words is still judged.

### The third veto: a completion claim that does not restate the operator's goal

Closes the Known-gaps entry "The completion signal's unit does not match the gate's window", with one
caveat (below). The audit that produced it, 2026-09-08, session `6a86bf8c`: 15 line-anchored `DONE:`
sign-offs over 39 hours, and only 5 were written when the goal then in force was complete and
verified. Four restated the original ask 36 hours before its verification finished; one claimed
"addressed every item" two paragraphs above a `NEEDED:` for an item it had not addressed; two closed
problems the agent had itself created and reported the delta as achievement; and after the operator
set the goal at 8:49 AM ("let's address the items you called outstanding before prematurely calling
this session DONE:") five more `DONE:`s followed in 2h05m, each for a sub-task, while the outstanding
list never reached zero. Every one cleared the floor, because each was a true statement about
*something*. The operator's words: "that's dishonest and cheating the stop gate".

**The goal is the operator's: Claude Code's `/goal` first, a `GOAL:` line as the fallback.** `goal_pin.py`
reads it for this gate, the goal gate and the gate-cadence hook alike:
- **`/goal` first.** Claude Code writes a `goal_status` attachment with the condition and `met: false` when the
  operator runs `/goal`, and again at every stop its own evaluator judges. While one is active, its condition is
  the goal and outranks any `GOAL:` line. A record with `met: true` ends it. A `goal_status` shape the reader
  does not know leaves the goal as it was.
- **A `GOAL:` line otherwise.** Without an active `/goal`, the newest `GOAL:` line in the operator's own text
  is the goal, and a bare `GOAL: none` (or `clear`, `unset`) retires it. `GOAL:` follows the grammar every token
  shares (`stop_tokens.py`): in capitals, opening its own line. Mid-sentence or in another case it is a word,
  which is what lets an operator write about the token without setting a goal. A `GOAL:` line inside a
  double-quoted span is someone else's words pasted in. The quote marks are counted from the start of the
  message, so a quotation over several paragraphs stays quoted, and a mark must also follow the `GOAL:`, so
  one stray mark earlier in the message cannot silence a real pin. The text form stays because a headless
  `claude -p` prompt such as the triage runner's cannot run `/goal`.
- **Whose records count.** Gate feedback, `<system-reminder>` records, compaction summaries and
  `<cross-session-message>` records never set one. A `GOAL:` riding a `tool_result` mid-turn does, because that
  is still the operator typing.

**How it is read.** The fold is latched into `<session>.goal.json` with the byte offset it reached, so each call
reads only what the transcript gained since. A latch from another version of the reader is folded again from
the start. It cannot ride this gate's tail read: `/goal` writes its record
just before an auto-continuation "Goal set:" prose record, and the tail read halts at that record. The
gate-cadence hook denies a gate whose `-Goal` does not restate the goal
([PRETOOLUSE-HOOK.md](PRETOOLUSE-HOOK.md)).

**What it checks.** When a goal is pinned and the message carries a `DONE:` line, some `DONE:` line must
contain the goal's words verbatim after normalisation (case, punctuation and whitespace collapsed).
Verbatim rather than word overlap, deliberately: a score would let "audited what is still outstanding"
pass for "nothing outstanding", which is the exact substitution being caught, and the operator controls
the pin's length, so a short pin costs a short echo. A message with no `DONE:` is not judged: the veto
is about a claim, not the absence of one, so sub-task progress reported in prose costs nothing. Fences
are stripped first, as for every completion detector. The same holds for `QUEUED:` and `WAITING:`. A `GOAL:` line beside the claim does not count (it does for a
`NEEDED:` or a closing question), and the block reason and `stop-gate-rules.txt` must say exactly that: a
reason promising an ending the veto refuses leaves a session no ending it can earn. Pinned by
`a-done-with-the-goal-only-on-a-goal-line-beside-it-blocks` and its control.

**Why a veto rather than a narrowing of `work_is_complete`.** Replaying the 15 real sign-offs through
the gate, three had exited through `blocker-token` or `blocker-prose`, a `NEEDED:` line or a "your
call" sitting beside the `DONE:`, so a change confined to the completion signal would never have seen
them. The claim is what the operator reads, whichever signal earned the stop. Carve-outs match the
other two vetoes: an operator grant and a `REFUSING:` turn.

**The block reason quotes the goal back.** The same ordering fix the trailer veto made: the sub-task is
in context at the moment of writing and the goal is not, so the reason puts the operator's own words
there and says a finished sub-task is progress, not completion.

**Verification.** The harness's goal group. Neutering `misstates_the_goal` turns exactly its block
cases red; over-widening it to fire whenever a goal is set turns exactly the **3** pinned-goal allow
cases red (a faithful echo, an echo differing only in case and punctuation, a message with no `DONE:`).
Both counts were predicted before the run. Replaying the 15 real sign-offs with the 8:49 goal pinned:
the 10 before it keep their original verdicts, all 5 after it return `block:goal:1`, and the last one
rewritten to echo the goal allows again.

**Two limits, both worth knowing.** With no goal set the veto is silent, and the session that motivated it
had none, so the gap closes only for an operator who sets one. And the first cut's retirement
check reused the `NEEDED:` negation pattern, which read `GOAL: nothing outstanding from this session`,
the likeliest goal anyone will write, as a retirement: every block case allowed, and only the harness
caught it. `goal-pinned-and-done-names-a-subtask-blocks` keeps that exact goal so the mistake cannot
return quietly.

### The completion signal — "done, with proof"

The first three signals all ask the same question in different words: *does the agent need something
from the user?* None of them covers the most ordinary ending there is — the work is finished and nothing
is needed. For the gate's whole life the reason text asserted that if the three answers were all no,
"you are not done", and that claim is simply false. The only compliant move left to a finished agent was
to invent work, which is its own kind of failure.

The bar is the operator's, stated 2026-08-20: **the original goal fully completed, with empirical
proof.** Not a part of it, not the easy part, not "it should work". `Test-WorkIsComplete` requires three
things together:

1. A line beginning `DONE:` restating the original ask in the user's terms.
2. A line beginning `VERIFIED:` naming what was run and what it output.
3. **At least one tool call in the turn**, counted during the tail read from the last assistant record
   back to the operator's own prompt.

**The third is a floor, not corroboration.** It was called corroboration for most of this gate's life,
and that overstates it in both directions. It is unsound: an agent that reads one unrelated file and
then writes a false `VERIFIED:` line passes cleanly. It is incomplete: an agent that answers correctly
from context alone fails. It checks nothing about the claim. What it rejects is a completion claim from
a turn that ran nothing at all, the laziest forgery available and the only one a count can catch.

The floor is worth keeping because it is nearly free and because that forgery is observed rather than
hypothetical: on 2026-08-31 a turn claimed `VERIFIED:` over a sentence conceding it had produced no
fresh evidence, and the retry the floor forced re-read the files and quoted them. It is not worth
defending as a verification. The argument that removing it "would reduce the signal to prose" is
circular, because the gate exists to catch premature stopping and prose-distrust is a means to that
rather than the goal itself.

**What the signal as a whole enforces is that a proof was stated, not that the proof holds.** No regex
can check the second. The honest description is that it converts a silent idle stop into an explicit,
auditable claim; it does not verify the claim. A premature `DONE:` from a turn that ran thirty tools
clears the floor untouched, and that is the likelier failure.

Both tokens are anchored to the start of a line. The gate reason has to name them in order to teach them,
which would otherwise make the reason text itself a passing message: an agent that quotes the gate back
would earn a stop by echoing it. The reason keeps both tokens **inline, mid-sentence**, and
`echoing-the-gate-reason-earns-nothing` pins that against the live reason string rather than a copy.

### The announce-then-stop family is explicitly disqualified

A trailing question mark must not buy a stop for a message whose substance was a promise to act. If the
closing question's own line matches the announcement pattern — `proceeding now`, `continuing now`,
`next I'll`, `I'll now`, `working on it`, `standing by`, `report back`, `kicking off`, `in the meantime`,
`while that runs` — the question exemption is withheld and the gate blocks regardless. So is a bare tag
question of `TAG_QUESTION_MAX_WORDS` words or fewer (`Sound good?`) in a message that announces
anywhere, which is the evasion scoping to the line would otherwise open. `test_stop_gate.py` pins each
of these.

**Scoped to the question line on 2026-09-14.** Until then the veto searched the whole message, and an
aside anywhere cancelled a genuine question: "I gave my gate slot to a peer, and I'll start Functions
once it reports" sat two paragraphs above "Do you count `[UsedImplicitly]` as allowed, or as
suppression?" and the stop blocked on `I'll start`. A replay of every assistant message written that day
(458, across the workspace's top-level session transcripts; subagent transcripts were not read) found
eight whose closing question would have earned the stop and was lost only to an announcement
elsewhere: five on `in the meantime` (a question re-asked after its own block among them), one on
`I'll report`, one on `I'll run` and one on `I'll start`. The block reason never mentioned the veto, so a blocked agent
following it to the letter re-asked and was blocked again; the reason now names it.

Without this, the gate hands the agent a one-phrase bypass, and the phrase it would reach for is exactly
the one the audit flagged as the failure.

### A missed question must be re-asked, never self-answered

The gate is regex, so it can block a turn that genuinely asked the user something — a decision phrased
without a trailing `?`, an approval request worded as a statement. Observed in practice: the blocked agent
read the block as "you are not allowed to wait", answered its own question, and acted on the answer. That
is the one outcome worse than idling — the gate exists to stop the agent from *not acting*, and here it
pushed the agent into acting on a decision that was the user's to make.

Two mitigations, both pinned in the harness:

- **The block reason says it up front.** Before the three gate questions, the reason states that the gate
  is regex, that it can miss a real question, and that if it did, the block is a false positive: do not
  answer the question yourself, do not act on a guess, restate the question as one plain sentence ending
  in `?` and end the turn. The reason text is asserted by content in `test_stop_gate.py`
  (`reason-forbids-self-answering` and siblings), because that text is what the agent acts on — a
  wording regression is a behavioral regression.
- ~~**A decision-request pattern on the tail earns a stop without a `?`.**~~ Retired 2026-09-09 with the
  other two prose exits. The block reason now says outright that only the tokens and a closing `?` are
  read, so an agent that phrased a genuine decision as a statement is told exactly what to do: restate
  it as one question ending in `?`. Of the two recorded `decision-request` allows whose transcripts
  survived, one would now exit through its own `DONE:`/`VERIFIED:` and the other was already blocked
  by the commit-block veto.

The reason text is the actual fix, because no regex will catch every phrasing and the failure mode on a
miss must be "re-ask", not "self-answer".

## `stop_hook_active` is the turn boundary, but never an escape

Claude Code sets `stop_hook_active` on a stop that has already been blocked once. Honoring it **as an
escape** — allowing the stop because it is set — caps the gate at **one challenge per turn**: a speed
bump, not teeth. That much of the original reasoning stands.

But it was then discarded entirely, and that was the error. Read as a **boundary** rather than an escape,
it allows nothing; it only says *when to reset the count*. `$false` means "first stop of a new user
turn", `$true` means "continuation of one already blocked". Measured 2026-08-20 across two live sessions:
the first stop after a new operator prompt logged `False`, every continuation logged `True`, and one
session's continuations all shared a single turn identity.

So the budget is keyed on `session_id` alone and its state file is just `{"blocks":N}`. **No turn
identity is derived from the transcript any more**, which removes an entire class of bug rather than
fixing an instance of it.

Instead the gate keeps a per-session state file (`%TEMP%\claude\claude-stopgate\<session>.json`,
`claude_temp.stop_gate_state_root()`) recording the
current user turn and how many times it has blocked within it. It blocks up to `MaxBlocks` (default 3),
then yields. A new user message resets the budget.

This preserves the safety property — a session can never wedge, because the budget is finite and the user
regains control after at most three extra turns — while giving the gate three genuine challenges instead
of one.

### The budget is scaffolding, not a design — replace it

Raised by the operator 2026-08-19 and it holds up: **the budget is a retry limit standing in for a
missing decision.** The gate cannot tell whether blocking is achieving anything, so rather than judging
that, it counts to three and gives up. `MaxBlocks = 3` encodes no principle; it is "more than once, less
than forever."

That makes it wrong in both directions, and one session showed both. It blocked four consecutive stops
the operator had explicitly granted. And on reaching the cap it releases the next stop **regardless of
merit**, including one that should have been blocked. A counter cannot separate those cases because it
is not examining the situation at all.

**The budget is also what produced the `promptId` bug.** Holding a per-turn count requires per-session
state keyed on "which turn is this" — and that key had to be derived from the transcript, the very stream
the gate writes its own output into. `promptId` repairs the derivation but does not remove the need for
it. An entire state-file mechanism, a temp-directory dependency, and a key-derivation problem exist to
hold one integer.

Two signals would make it unnecessary rather than merely correct:

1. **A user-granted stop** (see Known gaps). Removes the budget's real job — letting the operator out —
   and is the only un-forgeable signal available, since the agent does not author user records.
2. **`stop_hook_active` as a turn-boundary signal**, which the section above rejects only in the role of
   an *escape hatch* (where it does cap the gate at one challenge). As a boundary detector it caps
   nothing, and it is documented, platform-supplied, and immune to the feedback loop by construction —
   it removes the key-derivation problem instead of fixing it. **Unverified:** that it arrives `false` on
   the first stop of a genuinely new user turn. Confirm before building on it.

With both, the counter reduces to a true last-resort loop guard rather than the primary exit.

**Resolved 2026-08-20.** `MaxBlocks` is now **100** — a wedge guard, not an exit — the exit is
`Test-OperatorGrantedStop`, and the turn boundary is `stop_hook_active` rather than anything read out of
the transcript. Sessions stranded at `blocks:3` self-heal, since `3 -ge 100` is false.

What remains of the budget is a count per session, reset by a documented platform signal. **It never
allows a stop:** past `MaxBlocks` the gate keeps blocking and tags the block `past-budget:` in
`decisions.log`. The count must never be a way out: a written `{"blocks":100}` earns nothing
(`a-forged-block-counter-earns-no-stop`). The wedge guard is Claude Code's own cap, below.

### The state lives under %TEMP%\claude, out of every session's tools

The state directory and its `decisions.log` and `facts.log` sit under `%TEMP%\claude` (`CLAUDE_CODE_TMPDIR` when
set, `claude_temp.stop_gate_state_root()`), beside the quarantine records. A session runs as the same account and its file tools
treat that root as scratch space, so the folder's ACL cannot keep it out; the hooks do. `invoke_main_checkout_gate.py` treats a file-tool write under it as a gate
change, so the dispatcher quarantines the session that tried it, and `invoke_pretooluse_gate.py` denies any shell
command naming `claude-stopgate`, script bodies included, since text cannot tell a read from a write. Its logs are
read with the Read tool or `hook_status.py`. A change of root leaves a running session's block count, delivered
asks and goal latch behind: the latch is re-read from the transcript, and a lost count or delivered ask costs one
extra refusal or one repeat, never a stop.

### A run of filler ends at four, with a reason

**`FILLER_STREAK_LIMIT = 4`.** The state file also counts `idle`: how many refused stops in a row came with no tool
call between them. A block extends the run only when `stop_hook_active` is set and the transcript shows the gate's
own feedback with no tool call after it (`facts["tool_uses_since_block"] == 0`), so a run is never inferred from
the counter, and a static transcript never makes one. Any tool call starts it again. At the fourth, the gate
returns `eject:filler-streak:<tag><n>` instead of a block: the envelope is `continue: false` with a `stopReason`
the operator sees, saying the turn was ended, not earned, and naming the last refusal, and the signal is in
`decisions.log`. Without it, a session answering every refusal with a line of prose runs to Claude Code's
nine-block cap below, which ends the turn with no trace in the gate's log. Pinned by the `filler-` rows of `test_stop_gate.py`, a tool call mid-run and a static transcript as controls.

### The second eject with no operator message between quarantines the session

**An eject that costs four lines of text is a stop the session can choose**, and a session whose items wait on
someone else chooses it. So the eject is a strike: `eject_strikes` keeps `<session>.ejects.json` in the state root
with the operator's newest marker (`last_operator_marker`) and a count, a new marker starts the count over, and at
`EJECT_STRIKE_LIMIT` (two) the gate quarantines the session through `session_quarantine.quarantine` with
`FILLER_STOP_REASON`, answering `eject:quarantined` and logging `eject:quarantined:filler-streak`. From then on the
dispatcher refuses every tool call and this gate every stop, until the operator deletes the record. An operator
message between the two ejects resets the strike, so a session the operator keeps talking to is never quarantined
for one lapse each. The eject itself stays a finding: the session audit catalogs every `eject:filler-streak` row as
a message owing a verdict (below), so the next token the session writes is refused until the eject is explained.
Pinned by `filler-a-second-eject-with-no-operator-message-between-quarantines-the-session`, its reset control and
the halt row.

### A stop token is audited first

**Every signal but the grant and a refusal is forgeable, and the declaration's flags are the session's own.** So
`AUDITED_SIGNALS` (a blocker, a closing question, `DONE:`, `QUEUED:`, `WAITING:`, the master's `IDLE:`) earn the
stop only once `declared_items.audit_required_problem` finds nothing, reading the session-audit cache and the
declaration from their files, never the message: a declaration exists; `state.json` exists (the audit has run);
`reconciled_now` holds at the current sync; `messages_after_sync` is empty; every eject in `decisions.log` beside
this gate's state is cataloged (`uncataloged_ejects`); `unrecorded_messages` is empty; `undeclared_open_rows` is
empty; `stale_page` is None. The order is the order a session fixes them in, and each reason names the command.
The exemptions are the ones no audit command can precede: the operator's grant, `REFUSING:`, the quiet wake line,
the ask for a goal while none is pinned, a `NEEDED:` a hook told the session to end on while it refuses every tool
(`told_to_end_on_needed`), a messenger process, and a session older than the session-start rules
(`prompt_rule_applies`). A session whose catalog holds one message passes after `sync` and `reconcile` alone, since
`unrecorded_messages` exempts the newest. An audit that cannot be read is a refusal naming the error, never a pass.
The audit runs `sync` incrementally, so the per-stop cost is one `sync`, one `reconcile` and the verdicts the turn
added. Pinned by the `audit-` rows of `test_stop_gate.py` (each refusal, the one-message control, the goal ask and
the pre-rule session as controls) and the `audit_required_problem` rows of `test_work_product_closure.py`.

### Claude Code's own cap binds first, at nine

**`MaxBlocks = 100` never binds.** Claude Code ends the turn itself after nine consecutive Stop-hook
blocks, printing "A hook blocked the turn from ending 9 consecutive times — overriding and ending turn".
The cap is raised with `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`, which `settings.json` does not set. The gate
never learns of the override: its log's last line for that turn is `block:9`, and no allow follows.

So nine blocked stops end any turn, whatever the messages say, and an agent can outlast the gate without
earning a stop. On 2026-09-14 one session did it three times running, answering 27 blocks with one-line
"still waiting" prose and no tool call. It was queued behind a peer's gate, and that peer had already
handed it the slot in writing on conditions that held. The fix for that shape lives in
`Harness/AGENTS/GATES.md` step 3 of "Coordinating with peer sessions", not in the gate: a peer
past its own estimate is messaged once, then the agent acts on a written handover or asks the operator.

The platform message's advice — "check stop_hook_active in the input and return success while it's
true" — is the escape the `stop_hook_active` section above rejects, because it would cap the gate at one
challenge per turn. Do not follow it.

### `/goal` is a second Stop hook, so an earned stop ends the turn with `continue: false`

**Claude Code's `/goal` registers a Stop hook of its own, a prompt hook that blocks every stop until its condition
is met, and a block from either hook continues the turn.** An allow from this gate is therefore not a stop while a
`/goal` is active. The trap: the gate saves an allowed ask as delivered, so when `/goal` reopens the turn, the
restated ask is `block:repeat-question:`, a status is "nothing earns a stop", and no message ends the turn before
the nine-block cap. A transcript shows it as `stop_hook_summary` records naming two hooks, followed by a
`goal_status` record with `met: false`.

`continue: false` from either hook overrides the other's block, a prompt hook's included. Under `claude -p` with
two project Stop hooks, a hook that always answers `decision: block` runs to the cap alone and fires once beside
one answering `continue: false`.

So `main()` writes `{"continue": false, "stopReason": ...}` instead of an empty allow when `ends_over_goal()`
holds: the stop was allowed as `NEEDED:`, a question, `REFUSING:`, `QUEUED:` or `WAITING:`, and
`goal_pin.active_goal_command()` finds an active `/goal`. Each of those waits on someone other than the session, so
the goal cannot be met until the wait is over. A `DONE:` ends over the goal only when the operator retired it
(`goal_retired_by_operator`): the `DONE:` line quotes, in double quotes and verbatim, four or more words the operator
said, or answered on the session's page and the session read back, since the goal was pinned, and the quote names the
goal and calls it finished (`finished`, `complete`, `done`, `over`, `retired`) with no negation. The evaluator reads
neither the page nor the operator's retirement of a goal whose steps cannot happen, so it would reopen every stop. The
pinned goal's own words never count, since every `DONE:` restates them, and neither does a quote that retires
nothing. Any other `DONE:` is the `/goal` evaluator's question, and a refused `DONE:` means there is work left. A
`GOAL:` line has no evaluator
hook, so it changes nothing. `decide()` keeps returning the same `allow:` signals, so the decision log and every
reader of it are unchanged; the `stopReason` text in the transcript is the trace. Pinned by the `goal-held-*` rows
and, through a real process, `an-allowed-ask-under-an-active-goal-emits-continue-false`.

**A review goal closes on verdicts, not on the reviewed thing being ready.** The `/goal` evaluator judges the
condition from the transcript's own words. When a review reports that the implementation is not ready and calls
its own checks unfinished, the evaluator keeps refusing it, however complete the review is, and the session can
only ask the operator to step in. So a review, audit or measurement goal is closed this way:
- every item gets a verdict with evidence, in a published report;
- a gap in the reviewed thing is a finding, not open work;
- an item the session cannot settle is settled as not met, or as unverified with what would settle it;
- the `DONE:` names that report.
The rules card states the same rule under "The goal".

### `QUEUED:`, the stop while waiting on another session

`QUEUED:` covers a wait on anything another session started, and `WAITING:` a wait on anything this session
started; the operator set that split. Without these tokens a session whose own work is finished and whose next
step waits on running work has no exit: `NEEDED:` is for something only the operator can supply, a question
would be contrived, and `DONE:` is false while that work runs. Each token turns the wait into a conditioned,
logged stop (`allow:queued-for-gate`, `allow:waiting-on-own-gate`).

A stop idle behind a peer is safe because a cross-session message wakes the session, so the requirements
are about making sure that message comes, and about the work really being finished:

1. **A line beginning `QUEUED:`**, fences stripped (`queued_claims`). The payload names what it waits on.
2. **A `SendMessage` with a recipient in the same turn** (`facts["messaged"]`, read from the turn's
   `tool_use` blocks). The scan window ends at the newest prose user record, and a peer's message is one,
   so a session woken by a peer and re-queuing must message someone again in that turn. A message sent
   in an earlier turn does not count.
3. **Another session's work live on this machine**: a peer's gate, judged by the cadence hook's own
   `live_peer_gate()`, the same check that denies a second gate; or a session the line names, that this turn's
   `SendMessage` addressed by name or socket (`facts["messaged_to"]`), and whose session-registry entry is running
   here (`queued_on_a_messaged_peer`, through `reachable_peers()`). A message proves only that a wake-up will come;
   without this, a session could queue behind work that was announced or planned but never started, or send a
   message for the sake of this check.
4. **Every item in this session's work-product declaration done**, judged by the cadence hook's
   `work_product_problem()` (`Harness/AGENTS/GATES.md` "Declaring the work product"): the file
   exists, parses, holds no open item, and has lost no item without the operator's verified approval.
   Requirements 3 and 4 fail CLOSED like every allow path: an error earns nothing. On the messaged-peer path the
   closure check excuses an ungated checkout while that peer's own worktree of the repo,
   `%USERPROFILE%\source\worktrees\<its session id>\<repo>`, holds a change origin lacks, either dirty or
   committed and unpushed (`peer_change_blocks`). Its landing will change the tree this session's gate would
   judge, so the stop queues behind it. A peer whose worktree has landed excuses nothing.
5. **The goal veto judges it like `DONE:`; the commit-block veto does not.** The goal veto reads
   `completion_claims` and `queued_claims` together, so a pinned goal must be restated on the `QUEUED:`
   line. The commit-block veto reads `completion_claims` alone, because a queued session re-posting its
   blocks on every wait turn is the re-paste the content check exists to stop, and the blocks are owed
   once, on the completion turn.

**The account's gate slot is the one queue with no session to message.** A gate launched outside any Claude
Code session under the same account holds the per-user slot in
`Harness/Gates/GateSlot.ps1` and has no inbox, so requirements 2 and 3 are replaced, not relaxed: a `QUEUED:`
naming the gate earns the stop without a message while the cadence hook's `gate_slot_holder()` finds the
slot's file lock held, and requirement 4 still applies. The lock is the only evidence read; the record
beside it is written by the holder and never counts. Nothing wakes such a session when the slot frees, so
it waits for the operator. The real-requirement group pins a held lock (allow), a free lock beside a holder
record (block) and a held lock with an open work-product item (block).

What it cannot see: whether an item marked done really is (as for `DONE:`), and whether the peer is past
its own estimate. The latter stays in `Harness/AGENTS/GATES.md` step 3 of "Coordinating with peer
sessions" as prose: message the peer once, act on a written handover, or ask the operator.

**Verification.** The harness's `QUEUED_CASES` pin each requirement by removing it from the allow case
(no peer message, a message from an earlier turn, no recipient, the token inside a fence or mid-line), and an
allow case pins that a payload need not name a gate; the goal group carries a `QUEUED:` block case and its
allow twin, and the commit-block group pins that a queued turn with a dirty repo and no block, or a stale
block, still allows. Those cases run with requirements 3 and 4 stubbed as met, because they judge another
concern; the real requirements are pinned by cases that each build their own projects, workspace and temp
tree, since the peer scan reads every transcript beside the session's: a live peer gate and a closed
declaration allow, and a message with no peer gate running, a peer gate already finished, an open item and
no declaration each block. The `queued-on-*` rows build a session registry: a named, messaged, running peer
allows, and an exited peer, a peer this turn did not message and a line naming no peer each block. Planted
defects that must turn cases red: dropping the `SendMessage` requirement, dropping the live-peer-gate,
named-peer or work-product requirement, excluding `queued_claims` from the goal veto, and adding it back to
the commit-block veto.

### `WAITING:`, the stop while this session's own work runs

`QUEUED:` covers a session waiting on another session; `WAITING:` covers a session waiting on something it
started. The task-notification the platform sends when a background task exits wakes the session, so that
notice plays the part the peer's reply plays for `QUEUED:`. Logged as `allow:waiting-on-own-gate`.

1. **A line beginning `WAITING:`**, fences stripped (`waiting_claims`). The payload names what it waits on.
2. **Something this session started still running** (`own_work_running`), any one of:
   - a background shell call, gate or not, judged by the cadence hook's own `collect()` and `is_live()`, so
     the two hooks cannot disagree on when it has finished: launched in the background, with no
     task-notification for its task id, no `TaskStop`, and an output file modified within
     `BACKGROUND_STALE_SECONDS`. The walk runs over the main transcript and its subagent transcripts and
     resumes from the cadence hook's scan cache, and only when a `WAITING:` line is present;
   - its own gate holding this account's gate slot under this session's id (`own_gate_holds_slot`), which
     covers a gate whose launching tool call came back rejected after the process had started, since the
     slot's file lock is held only while that process lives;
   - a permission matrix it requested (`own_matrix_open`): the newest `requested the permission matrix in
     <state>\dispatch\matrix-<utc>.json` line in its transcripts, while that request file still waits or
     `<state>\matrix-run.json` records a start at or after the request. The poll deletes the run file once it
     scores the run, so a finished matrix holds nothing open, and a start time with an offset other than UTC
     fails closed.
   Unlike a veto it fails CLOSED: an error earns nothing.
3. **Every item in this session's work-product declaration done**, exactly as for `QUEUED:`.
4. **The goal veto judges it like `QUEUED:`; the commit-block veto does not**, for the same reasons.

**Known gap: the launching turn.** Launching the gate and writing `WAITING:` in the same turn is the
likeliest use, and it is the case the transcript lag documented at the top of this file can hide: if the
launch's `tool_result` is not yet flushed, the gate sees no live gate and blocks. The failure is strict
(a block, never a bought stop). A `block:` row in `facts.log` whose `ahead=` text carries `WAITING:` is
the signature; ending the next turn with the same line, once the launch record has landed, earns the stop.

**Verification.** The `waiting-*` cases: the allow case with a real background-launch record and
output file, a background task that is not a gate and a payload that names no gate each allowing, and one
block case per requirement removed (task already notified, task stopped, a stale output file, nothing in the
background, the token in a fence or mid-line); a goal block/allow pair; a dirty repo that still allows; the
reason-text case; the slot rows (a slot held under this session allows, one held under another session and a
free slot naming this session each block); and the matrix rows (a waiting request and an open run each allow;
a finished matrix, a run older than the request and a request only another session's transcript shows each
block).

### `IDLE:`, the master session's stop between cycles

The master session (`Tools/MasterSession/RUNBOOK.md`) works an open-ended goal, so no `DONE:` ever fits, and
between runbook cycles it waits on nobody, so no `QUEUED:` or `WAITING:` fits either. `master_idle` allows its
stop on four conditions: a line beginning `IDLE:` (fences stripped), `MASTER_SESSION=1` and
`COMPUTERNAME=WIN-VL9HOKP0RAM` in the hook's environment, a closed declaration, and `wake_task_scheduled`
finding `Claude Code - Master Wake` neither disabled nor without a next run time (`schtasks /Query /FO CSV /V`).
`Start-MasterSession.ps1` sets the variable in the claude process it launches, never at user or machine scope,
so no other session carries it and no tool call can add it. The wake task's process gets `MASTER_WAKE=1` and
earns its stop by the turn's `SendMessage` (`master_wake_sent`). It is a headless `claude -p` with no operator
message, goal, page or board row, so the no-page, peer-owed and registration vetoes skip that signal.

Rows: the master with its wake scheduled allows, and blocks with the variable missing, on another machine, with
the wake unscheduled, mid-line and in a fence; the wake process allows after its `SendMessage` and blocks
without one; a `SendMessage` without `MASTER_WAKE` earns nothing; `wake_task_scheduled` reads ready, disabled,
no next run and a missing task.

A poke process (`Coordination/Send-SessionPoke.ps1`) is the same kind of messenger for any session on any
machine: a headless `claude -p` that delivers one read-the-board message and exits. Its launcher sets
`CRGOLDEN_SESSION_POKE=1` in the claude process's environment, and `poke_sent` earns its stop by the turn's
`SendMessage`. It never registers on the board, so the registration veto skips that signal along with the no-page
and peer-owed vetoes. Rows: a poke process allows after its `SendMessage` on a desktop and blocks without one; a
marker other than `1` earns nothing; under the prompt rule a poke process and a wake process each stop
unregistered, and the same transcript with neither marker is refused as unregistered.

### The master's questions reach the operator by push

Nobody watches the master's conversation, and a question parked on its page raises no notification, so every stop
the master earns (any allowed signal but the operator's grant, an empty message and the wake process's) is
refused while `unpushed_parked_ids` finds a parked question open that no `PushNotification` has named by its `qN`
id. Open is judged by a read of the page this turn that covers every question (`page_reads_this_turn`), less the
questions it shows answered and those the session withdrew; with no such read the stop is refused with a demand for
the read (`master-push-read`), so a question answered on the page is never pushed again. A push counts whatever its
result: "not sent" means the operator was already looking. An id matches only whole, so `q70` does not name `q7`.

Rows: `IDLE:` with an open question never pushed blocks; after a push naming it allows; a push naming another id
blocks; no page read this turn blocks with the read demand; a question the read shows answered owes no push; a
closing question from the master blocks the same way; another session asking the same question owes no push.

### A done flag is a claim: the work product must be closed in fact

`work_product_problem` reads each item's `done` flag, which the session sets itself. `work_product_closure.py`
checks what the flags claim, from the transcript's edits and `git status` of the checkout each edit targeted. Every
stop that hands off calls it after the declaration reads closed: `QUEUED:`, `WAITING:`, `DONE:`, and, through
`handoff_problem`, a `NEEDED:`, a closing question and the quiet line on a wake:

| Check | Refuses | Excused |
|---|---|---|
| uncovered | a file the session edited that is still uncommitted and no item names (`work_product_items.names_path`, whole path segments); a file a repo-changing command of the session wrote (`git -C <checkout> apply`, `checkout`, `restore`, `merge` and the like, or `npm ci`/`install --prefix <checkout>`; `session_command_checkouts`) counts as edited when it is dirty in the session's own worktree, or dirty elsewhere and named by one of its items (`command_changed_files`), and the Stop gate's owed commit message takes the same files (`command_written_files`) | an edit since landed, which is clean in its checkout; a dry run (`--check`); mid-merge (`MERGE_HEAD`), a file whose bytes equal the merged commit's (`merged_in`), which the merge brought |
| ungated | a hand-off while a done item's repo holds session-written, gate-covered dirty files (`gate_status.EXCLUDED` leaves markdown out) in a checkout whose record is not GREEN-CURRENT (`parking_lot_commits.landing_state`) | a `QUEUED:`/`WAITING:` that waits on a gate (the slot, a peer's live gate, its own gate running) with nothing else open, since one slot serializes every gate; any hand-off for a repo `gate_blocked` measures as blocked: one of its items waits on a parked question (the cadence hook refuses a gate while an item is open), or a peer's PASSED Tools change is not on origin (`peer_green_unlanded`) and the ask names that checkout. Never a repo whose last gate failed on exactly its current tree (`failed_on_this_tree`): its next step is a fix |
| standing | an open item in the declaration's top-level `standing` list: `DONE:` always, any other hand-off unless the line names its `peer` by the full session id the item records (a session name is neither unique nor stable) | `"done": true` only; a ledger under the cadence cache keeps every standing item seen, so deleting one leaves it open |
| audited | an operator message the session's audit has no verdict for (the newest excepted), an open verdict whose evidence quotes no open item (`declared_items.py`), and, once the audit's page url is recorded, a verdict newer than the page's last successful Artifact publish in the transcript (`stale_page`, reading only the tail after that verdict) | a session never audited here; a page never published |
| leftover | the session's worktree folder (`%USERPROFILE%\source\worktrees\<session id>`) or branch outliving its work (`leftover_problem`): a worktree the session wrote in, or ran a repo-changing command in, that is clean with every commit on origin (`checkout_unlanded`); anything else in the folder, such as a scratch file; the folder left empty; a branch named for the session in a workspace repo, loose or packed, with no worktree of it left | a worktree holding an uncommitted file or a commit origin lacks; one the session has not written in yet |

A standing item carries an `approval` quoting the operator, checked against their messages, since the
declaration-edit gate judges only `repos` items. An open one cancels the gate-wait excuse for an ungated repo
unless it carries `pending`, naming a parked question no read shows answered or withdrawn (`parked_standing`, judged by
`waits_on_open_question`): that work waits on the operator, so a `WAITING:` on the session's own running gate beside it still earns its stop. Or it carries `after`, quoting four or more words of one declared repo item that is done while a file of the session's that the item names is still uncommitted (`after_held_standing`): its next step is that item's gate and landing, so it leaves the excuse standing too, and once those files land it waits on nothing. In the session's own worktree (`worktrees\<session id>\<Repo>`) every dirty file counts as the session's edit, since no other session writes there, so a commit it fast-forwarded onto never hides its reapplied bytes (`own_worktree`). Verified by `Harness/Hooks/test_work_product_closure.py` and the
`handoff-`, `landing-` and `quiet-wake-` rows of `test_stop_gate.py`.

**Landing is the last step.** A `NEEDED:` or closing question asking to commit or push (`landing_asked`, the same
reading the commit-ask rule uses) is refused while an open item waits on no parked question (`LANDING_WORK_REASON`),
since the operator landing work the session is still changing lands a tree nobody gated. An item waits on its
`pending` question only while no read shows that question settled (`settled_question_ids`: answered in any read of
the session's own page, answered or withdrawn in this stop attempt's reads, or withdrawn by the session's own landed
write; computed once per stop), so a stale `pending` left after the
operator answered excuses neither a landing ask nor an ungated repo. An item is held, and
counts for neither the landing ask nor the hand-off closure, while its repo's main checkout holds another session's
PASSED record completed after origin's newest commit and not on origin, and the ask names that checkout
(`held_by_a_peer_green`): any edit there would stale that record, so the item truly waits on the landing asked for.
The ask names a path only whole (`names_path`, either slash): `...\Tools` is not named by `...\ToolsArchive` or
`...\Tools\x`.

### A rejected `QUEUED:` or `WAITING:` names the requirement it missed

When a message carries a `QUEUED:` or `WAITING:` line and still blocks, the block opens with one line
naming the first requirement the line missed (`unaccepted_wait_claim`), checked in the order the two
sections above list them: the work-product declaration open, quoting the repo and reason
`work_product_problem()` returns; then for `QUEUED:` the gate slot free and either no `SendMessage` this turn,
or no peer gate live and no named, messaged session running here; and for `WAITING:` nothing this session
started still running, with a pointer to `QUEUED:` for a wait on another session's work. The
generic reason follows unchanged, and the decision row reads `block:wait-claim:<n>`. `QUEUED:` is judged
when both tokens appear, since it is the allow path checked first.

It never decides a verdict: it is computed only on a block, so a stop is earned or refused exactly as
without it, and a gate state it cannot read names that rather than a requirement. Without it every
rejection reads as the generic "nothing in your last message shows that continuing is impossible", and a
session re-posts the same line instead of fixing the one requirement it missed, or claims `WAITING:` on
another session's run or on CI, none of which this session started.

**Verification.** The `wait-claim-reason-*` cases pin the note for nothing of this session's running, an open
work-product item, no peer gate after a `SendMessage`, a free slot with no `SendMessage`, and a wait on CI
pointed at `QUEUED:`, each asserting the note precedes the generic reason; the control is a block whose
message carries neither token and gets the generic reason alone.

### Own work before a hand-off: an open declared item vetoes a `NEEDED:` or question about something else

A `NEEDED:` or closing-question stop is refused while this session's work-product declaration
(`Harness/AGENTS/GATES.md` "Declaring the work product") holds an open item that the stop does not
name (`unworked_open_items`, block row `block:open-item:<n>`). The block lists the open items. A stop names an
item when its `NEEDED:` lines or closing question carry four consecutive words of it, or the whole item when
it is shorter (`OPEN_ITEM_NAMING_WORDS`), matched as whole words, so a real blocker on the session's own
deliverable still ends the turn. `REFUSING:`, the operator's grant, a session with no declaration, and a declaration with every item done
are untouched, and the check fails open on an error like every veto.

The trap it closes: a session whose own deliverable is unfinished ends the turn on someone else's pending
answer (a peer's question to the operator, a peer's matrix, a gate it chose to sequence after), because a
`NEEDED:` naming any pending decision reads as a blocker. The declaration is the session's own list of work,
so a stop that is not about it is a hand-off, and an order the session chose itself is not a dependency.

**Verification.** The `open-item-*` cases: a `NEEDED:` and a question about something else with an item
open both block and list the item, as does a `NEEDED:` holding the item's words only inside other words; a
`NEEDED:` naming the open item, a declaration with every item done, no declaration, and a `REFUSING:` with an
item open each allow.

### A read-only ask declares no items, and the gate checks that it edited nothing

An item names a file it changes (`Harness/AGENTS/GATES.md` "An item is an edit"), so a goal that changes no file in any
repo (a review, an audit, a report) has no item to declare and nothing to gate. Its declaration is
`{"repos": {}}`. A declaration with no items passes the `DONE:`, `QUEUED:` and `WAITING:` checks only when the
session's transcripts, its subagents' included, hold no `Edit`, `Write`, `MultiEdit` or `NotebookEdit` whose
file lies in a workspace repo's checkout or in one of its worktrees (`session_repo_edits`, the checkouts from
`gate_status.checkouts`). A call whose tool result is an error, a rejected or failed edit, changed nothing and
counts for nothing. A session that did edit a repo is refused (`block:done-open:`) naming the repo and the file,
with the instruction to declare each deliverable as an item. A missing declaration is still refused: the
read-only claim is made explicitly, never by omission. The trap this closes is an empty declaration judging no
repo at all, so that a session that had edited a repo could write one and claim `DONE:` unexamined, while the
block reasons told a read-only session to list deliverables it did not have. A write made through the shell is
outside this check; the commit-block and gate-claim vetoes still judge the repo's state.

**Verification.** The `read-only-*` rows in `test_gate_cadence_gate.py` (an edit in the main checkout and one
in a registered worktree refused, each naming the repo and the file; a rejected edit, an edit outside every
repo, and no edit at all allowed; a declaration with items judged as before) and the
`done-*-empty-declaration-*` rows in `test_stop_gate.py` (a repo edit blocks, a rejected one, edits outside
every repo and no edit allow).

### A `DONE:` owes the declaration's acceptance checks, not only its items

The declared items are the edits a session chose to write down, so every item done says nothing about whether
the goal holds: a session can finish all of its edits while the system the goal names is still off. A
declaration may therefore carry an `acceptance` list, each entry `{"check": text, "command": [argv],
"expect": regex, "approval": operator words}`, stating in live terms what finished means (a scheduled task
reads `Ready`, a gate record reads `GREEN-CURRENT`). The finish line is the operator's, so an entry counts
only when its `approval` quotes at least `ACCEPTANCE_APPROVAL_MIN_WORDS` of the operator's own words: a session
cannot satisfy its own `DONE:` with a check it wrote to pass. A `DONE:` that the item check allows then runs
each command (`acceptance_problem`) and is refused as `block:done-unaccepted:<n>` naming the first check that is
unapproved or does not exit 0 within its share of `ACCEPTANCE_TOTAL_SECONDS` (at most
`ACCEPTANCE_CHECK_TIMEOUT_SECONDS`) with stdout matching its pattern. A timeout, a launch failure, a spent
budget, more than `ACCEPTANCE_MAX_CHECKS` entries or a malformed entry fails the check rather than the open
way, because a `DONE:` rests on the check having passed. The commands run only on a `DONE:`, so ordinary stops
pay nothing. A declaration with no `acceptance` key owes none.

**Verification.** The `acceptance-*` cases: with every item done, an entry with no approval, one approved only
by the agent's own words, a check whose output does not match, one that exits non-zero, one that times out, one
that cannot launch and an entry without a command each block; an operator-approved passing check through a fake
runner and a real `python -c` command through the default runner each allow.

### A merged branch is cleaned up before the turn ends

The session that makes a branch removes its worktree and deletes the branch once the change is on `origin/main`
([Tools/AGENTS.md](../AGENTS.md)). `merged_branches.py` enforces that for every stop but the operator's grant and a
`REFUSING:`, refusing it as `block:merged-branch:<n>` with the exact `worktree remove` and `branch -d` commands.
It reads only this session's own `git -C <repo> worktree add -b`, `checkout -b` and `switch -c` shell calls,
folded once into `<session>.branches.json` with the byte offset reached, so another session's branch never
blocks this one and a session that made no branch runs no git at all. A branch counts only while all of these
hold:
- it still exists, and has a commit since it was made (its reflog's first entry is not its tip), so a fresh or
  abandoned branch never blocks;
- its worktree holds no uncommitted work;
- `origin/main` contains its tip, or `git cherry` finds every branch commit's patch there, which covers a squash
  merge;
- it is not the session's own worktree, unless the stop is a `DONE:`. A session keeps one worktree for Gates/ and
  Hooks/ changes, on the branch named by its session id at `worktrees\<session-id>\<Repo>`, across its loop of
  gate, operator commit and push, and merging main back in. Any message carrying a `DONE:` line demands its
  teardown, whichever token earned the stop; a session that ends on `NEEDED:`, `QUEUED:` or the operator's grant
  keeps it. The exemption is that exact pair: a branch named for the session in any other folder, or another
  session's id worktree, still blocks every stop.

Deleting the branch on origin is left to the operator, because sessions cannot push.

**Verification.** The `merged-branch-*` cases, against real repos whose `origin/main` is a local ref:
- **Block:** a merged worktree branch left in place, and a merged `checkout -b` branch, each naming its cleanup
  commands.
- **Allow:** an unmerged branch, a branch with no commit, one another session made, a merged branch with
  uncommitted work, and a cleaned-up branch.
- **Parser:** quoted paths, `-B` and `switch -c` read as made branches; a `worktree add` with no new branch, a
  plain checkout and a git call with no `-C` read as none.

### The transcript-derived turn key, and why it is gone

Two successive turn keys were derived from the transcript. Both were wrong, in opposite directions, and
the harness reported green for both.

**Counting prose `user` records (original).** Claude Code appends the gate's own block reason to the
transcript **as a prose `user` record** before the next `Stop` fires, so every block incremented the
count, the recomputed key never matched the stored one, `$blocks` reset to zero, and
`$blocks -ge $MaxBlocks` was never true. The documented "session can never wedge" property was false for
the gate's entire life. Every state file on this machine read `{"blocks":1}` — including a session 563
turns deep — and replaying transcripts showed **9 blocks under a single prompt**.

**Keying on `promptId` (2026-08-19).** This made the budget work as documented, and *that was the
problem*: the budget is per user prompt, so an agent working autonomously for hours under one prompt
exhausted three challenges early and the gate **waved through every stop for the rest of the turn**. Two
multi-hour sessions were found at `blocks:3` with the gate inert. The original bug had been load-bearing
— it was compensating for a design whose unit is wrong.

**Both are gone.** The boundary now comes from `stop_hook_active` (above); no turn identity is derived
from the transcript at all. The counter survives only as a wedge guard at `MaxBlocks = 100`, and the
operator's exit is `Test-OperatorGrantedStop`.

**The durable lesson is about fixtures.** The harness built a *static* transcript and called the gate
four times against it, so the turn key never moved and the counter incremented cleanly. The gate sits in
a feedback loop — its own output becomes its next input — and a fixture that does not model that loop
cannot test the loop guard. The same trap recurred with the payload: once the boundary moved into
`stop_hook_active`, a fixture calling the gate with `-SkipStdin` could not exercise the budget at all.
`Invoke-Gate` now always sends a real stdin payload, and `budget-survives-gate-feedback` still appends
the block reason between invocations so transcript growth is proven not to disturb the count.

## Unreadable input fails open; a crashing gate fails closed

A missing transcript, a corrupt transcript or a malformed payload gives the gate nothing to judge, and the stop
proceeds (`missing-transcript-fails-open`, `corrupt-transcript-fails-open`).

An unhandled error inside the gate is different: `main()` blocks the stop as `block:error:<exception>` on every
call, retries included, because an unjudged stop must not pass as an earned one. The only exit is Claude Code's
own consecutive-block cap, so a crashing gate shows the operator a block loop naming the exception
(`a-gate-that-raises-blocks-the-stop`, `a-gate-that-raises-keeps-blocking-every-retry`).

### The two-day detour, kept because it is the expensive part

This is the record of how a solved problem got treated as an unsolved one. The chronology matters more
than any single finding in it.

1. Stale transcript views were noticed and measured. A 120s threshold was fitted to a bimodal age
   distribution. The guard **failed open**: `allow:transcript-stale` returned before any detector ran.
2. That lasted one day. A live agent ended a turn on a status report earning nothing — no question mark,
   no token, not even the announce veto — and the gate allowed it, because the guard fired ahead of
   every detector. Replayed with the guard off it was `block:1`, all nine detectors False.
3. So it was switched to **fail closed**, with a distinct reason saying the fault was the gate's, and a
   3-block budget so it could not wedge an agent over a lag no message could fix.
4. On its first live outing that converted one silent bad allow per occurrence into **three futile
   blocks plus the same bad allow, twice in 21 minutes.** The retries could not have worked: a block
   extends the same turn whose records are unflushed.
5. The operator asked why the gate could not simply read the message. Reading the documentation took
   one fetch. The field had been there the whole time.

Three lessons worth more than the guard was:

- **Read the platform's contract before engineering around its behaviour.** Every measurement taken
  during step 1 was accurate. None of it was decision-relevant, because the question being measured
  ("how stale does the file get?") was downstream of a question never asked ("why am I reading the
  file?").
- **A well-measured workaround for an undiagnosed problem is still a workaround.** The threshold fired
  correctly on exactly the cases it was fitted to and still could not do anything useful.
- **"Can we be the first to hit this?" is a cheap and powerful check.** One search found the exact
  issue, [#15813](https://github.com/anthropics/claude-code/issues/15813), plus the feature request
  that produced the fix.

The lag measurements themselves are preserved below under the misfire notes, because they remain a fair
record of the behaviour even though the conclusion drawn from them was wrong.

## The harness

```
python Tools\Hooks\test_stop_gate.py
```

Builds synthetic transcripts, calls `decide()` against each, asserts block versus allow, and exits
non-zero on any regression. It isolates state under its own temp directory so a real session's budget is
never touched. `--only <substring> [<substring> ...]` selects cases; it exits non-zero rather than
reporting success if the selection matches nothing.

The allow/block distinction is the whole point, and is the same lesson `PRETOOLUSE-HOOK.md` records: a
harness that cannot separate "the gate blocked this" from "the gate said nothing" cannot verify the gate
at all.

Cases cover the earning signals, the announce-then-stop family, the trailing-question bypass, the
retired prose exits (each phrase as a block case beside its token twin), the block reason's re-ask-never-
self-answer wording, last-prose-turn selection, `stop_hook_active` not buying a stop, the
three-then-yield budget, budget reset on a new user turn, and both fail-open paths.

**The parity harness's real-transcript half proved much less than its case count suggested.** (Fixed
2026-08-29; the harness itself was deleted 2026-08-30, but the lesson is about counting, not about
parity, and it generalises to any suite.) It fed the 30 **smallest** transcript files whole. Small
transcripts are stubs, so
**26 of 30 resolved to `allow:no-assistant-text`** — the degenerate path where the scan finds no
assistant text at all. Both implementations agreed, the run went green, and that entire half exercised
one trivial branch while reading as broad coverage. The count was the tell: "30 real transcripts"
sounds like breadth and was one case repeated.

`make_parity_fixtures.py` generated them instead, using the same exact-offset method that
diagnosed the misfires: cut a real transcript just past an assistant record carrying text, classify
the cut by the signal the gate returns, and keep a spread. Measured on the current fleet:

```
fixtures written: 23
  allow:blocker-prose 4   allow:blocker-token 4   allow:question-mark 4
  allow:work-complete 4   block:1 4               allow:operator-grant 2
  allow:decision-request 1
```

Seven signals against one, with real text throughout, and a warning if the spread ever collapsed below
three signals so the failure could not return silently. The generator went with the parity harness on
2026-08-30; the counting lesson above is the part worth keeping.

## Wiring

`~/.claude/settings.json`, alongside the existing `PreToolUse` and `SessionStart` hooks. The path is
resolved through `$env:USERPROFILE` rather than hardcoded — `~/.claude` is OneDrive-synced across four
machines with two different profile roots, and a literal `C:\Users\crgol\...` is wrong on the server.

Latency is **one `pwsh.exe` start plus a transcript read**, and the second term is the one that bites.
This section previously claimed the cost was "one `pwsh.exe` start, ~3.9s median" — that sentence was
wrong, and it is why a 30s timeout looked like ample margin. The gate parsed every line of the whole
transcript, so its cost scaled with session length: **measured 24.9s on a 147MB transcript against a 30s
budget**, and 28 real timeouts in a single week's transcripts.

A timed-out hook emits nothing, so the stop proceeds. The gate was therefore **failing open on exactly the
longest sessions** — the ones with the most turns for it to police.

An 8-day production aggregation (`python Harness/Hooks/hook_latency_report.py --days 8`) put this hook at
a **5.8s median over successful runs with a 10.8% timeout rate** against its 30s budget — so roughly one
stop in nine was failing open. Re-run that report after any change to the read path; the harness asserts
correctness, not speed, and a synthetic run at idle cannot see load-dependent cost.

**Re-measured 2026-08-21: the median held at 5808ms and the timeout rate has drifted to 13.4%.** The
same run surfaced something larger that nobody had looked at, because this file only ever asked about
`Stop`: **`PreToolUse` ran 3301 times at an 11.3% timeout rate, blocking 507.7 minutes.** That is the
hook enforcing the shell and permission rules, and it silently does not run on roughly one call in nine.
`SessionStart:startup` is worse in rate — **36.7%** against a 10s budget. Across all hooks the window
cost **9.01 hours** blocked. Configured budgets are PreToolUse 30s, SessionStart 10s, Stop 30s.

**That 30s budget is ours, not the platform's**, which reframes every optimisation below it. The
`"timeout": 30` in `~/.claude/settings.json` is an explicit setting; the documented default for a
`command` hook is 600s. The read path was tuned hard against a ceiling that was chosen rather than
imposed — see the Known-gaps entry on unused payload fields, and confirm the default empirically before
relying on it.

**Do not read `durationMs` on a timed-out record as execution time.** A first pass at this measurement
reported a "33.4s median, over its own budget", and that was an artifact: timed-out records log
wall-clock until the cancellation was recorded, which ranges from below the budget to wildly above it
(one PreToolUse record logged 320859ms against a 30000ms budget — a 30s timeout whose record landed five
minutes later). Timings must be taken over successful runs only, with a timeout charged its budget.

The read is now tail-first and stops at the first prose user record, which is all the gate needs (0.64MB
of a 155MB file, in the measured case): **24.9s → 5.3s**, and 2.9s against the live 1.3MB transcript the
harness measures. Two implementation traps are pinned in
`Get-TranscriptFacts`'s own comments because both were live regressions during the fix and both *looked*
like optimisations: `foreach ($x in (Get-Thing))` fully enumerates the callee before the loop body runs,
so an early `break` saves nothing; and locating newlines with a PowerShell `for` loop over each byte is
slower than the whole-file JSON parse it replaced. Re-measure after any change to the read path — the
harness asserts correctness, not speed.

**`stop-hook-timeout-has-real-margin` is load-sensitive, so read a failure there as a machine-state
signal before reading it as a gate regression.** It measures the live invocation and requires the budget
to be at least 3× it. On an idle box that run is ~2.9s (10× margin); measured at 7.1s while a second
harness run was saturating the machine, which is 4.2× — passing, but much closer to the floor than the
number suggests. If it ever fails, re-run it alone before changing anything.

**That protocol was exercised 2026-08-21 and held.** The case failed at **43467ms** during a run
competing with a background review agent; re-run alone it measured **4754ms, a 6.3× margin**. Load, not
a regression.

**Re-running it alone also showed the case does not measure what this file long claimed.** Its input is
a *synthetic one-line* transcript from `New-Transcript`, not a live one — "live" in `$liveTranscript`
means the live *registered command form*, not a live transcript. So it exercises process startup and a
trivial read, and **a read-path regression is invisible to it.**

**And the read path is no longer the problem.** Measured the same day, same machine, same command:
a synthetic 1-line transcript ran a **4754ms** median, and a real **169.9 MB** transcript ran **6163ms** —
1.4s more for four orders of magnitude more input. The tail-first read plus the `MaxScanBytes` cap have
made cost effectively independent of session length, which is what they were built to do.

The corollary matters for anything aimed at the timeout rate: **roughly 4.5s of the ~5.8s production
median is `pwsh` process startup**, on a trivial input, with `-NoProfile` already set. Further optimising
the scan buys almost nothing, and adopting `last_assistant_message` buys less still. The two levers that
remain are the configured budget and whatever is making a `-NoProfile` shell take four and a half seconds
to start on this machine.

## It must not suppress reporting

A block does not erase anything. The turn's text is delivered before `Stop` fires, so a finding stated in
a blocked message stays on screen; the continuation appends work after it. Findings are not lost by the
gate.

The real risk is behavioral: an agent that knows report-shaped turns get blocked can learn to stop writing
findings down and work silently instead — a worse failure than the idling this gate targets. The block
reason says so explicitly, and directs durable findings into the doc that owns the subject
(`AGENTS/REPOS/<Repo>.md`) before continuing, so that continuing cannot bury them.

It deliberately names no working document. Pointing agents at a temporary one teaches them to file
durable findings somewhere designed to be thrown away, and the gate reason is read by every agent on
every blocked turn, so whatever it names becomes the fleet's default destination.

## The gate is Python now, and the tests run in-process

`invoke_stop_gate.py` is what the Stop hook runs, and now the only implementation. The PowerShell
original was kept for a while "as the reference the port was proved against", which is a justification
that expired the day the port was proved; see the deletion note at the top of this file. Measured on
this machine before it went: the PowerShell gate cost **3701ms** on a
two-line transcript, of which ~1900ms was `pwsh` startup and ~1800ms was parsing the 35KB script. The
analysis was always milliseconds. The Python gate, invoked through the registered command, costs
**334ms**.

A persistent daemon was considered and rejected on the measurement: hooks are exec'd as `command` plus
an `args` array, so a server still needs a client, and any `pwsh` client pays a **1652ms** floor before
it can open a socket. A client fast enough to be worth writing can simply run the gate.

`test_stop_gate.py` runs **in-process** against `decide()` — 77 cases in about a second, against the
PowerShell harness's ~11 minutes. That number is the point: the old cost made every fixture tweak a
coffee break, discouraged the break-testing this gate depends on, and let two live defects sit while
sessions hit them. Only the cases that genuinely need a process — that the registered command runs, and
what it costs — still spawn one.

**Two hazards found while porting, both silent if got wrong.** PowerShell's `-match` and `-replace` are
case-insensitive by default, so every Python pattern compiles with `re.I` except the stop tokens, which
match only in capitals so the operator can find them in long prose and a sentence opening "Done:" is
never read as a claim (`lowercase-tokens-earn-nothing`, `title-case-prose-opening-done-earns-nothing`). And the
PowerShell gate emitted **non-deterministic JSON key order** (an unordered hashtable), which is why a
raw text diff flagged 4 of 7 blocking cases as divergent at identical length.

Equivalence between the two implementations was enforced by a differential harness until both the twin
and the harness were deleted. What it was actually good for is worth keeping in mind if a second
implementation is ever proposed again: it compared verdict AND signal name AND reason text, and it did
catch real drift — but only drift between the live gate and a copy nobody ran, which is drift with no
consequence. The cost was that every change had to be made twice.

One detail from it generalises to any suite with a global guard: every synthetic case had to disable the
staleness guard, because the real fixtures were cut from transcripts days old and would otherwise all
resolve to the stale path — agreeing with each other while testing no detector at all. A guard that
every fixture switches off is a guard that has no coverage, which is why a single case had to switch it
back on. **If a suite disables something to make its fixtures work, that something needs its own case.**

## Known gaps

- **CLOSED 2026-09-09 — the commit-block veto rewarded a pasted block, so agents pasted it forever.**
  The operator: agents calculate a commit message on a fresh `DONE:`, then reuse it verbatim over and
  over without recalculating or checking whether it was ever used, and memories written against it did
  not change that. Measured before the fix: of 367 commit-shaped blocks in surviving transcripts, 50
  subjects were re-posted byte-identical within a session and 14 after the operator had already
  committed them. Closed by judging the block's file-list lines against `git status --porcelain` run
  by the gate — see the second veto's REBUILT section for the mechanism, the 34 cases and the five
  break tests. Still open inside it: the body's description of the change is not checked against the
  diff, only the file list is.

- **RETIRED 2026-09-09 — the three prose exits: `blocker-prose`, `refusal-prose`, `decision-request`.**
  The trigger was a live stop at 12:49:52Z: an agent mid-task wrote "I'll report the outcome when it
  lands", then listed two loose ends, one of which "genuinely needs your decision". The gate allowed it
  on `needs your decision`, tested ahead of the announce veto, and the operator had to intervene by hand.
  Their ruling, verbatim in substance: the prose patterns made sense for the first iterations, but with
  `NEEDED:` and `REFUSING:` in place they were perpetuating manual interventions and letting agents slip
  through by accidentally using magic strings, despite their intent.

  **Priced by replay before removal, per checklist item 7.** `decisions.log` held 54 prose-path allows;
  38 had a surviving transcript (the other 16 belong to sessions pruned before the nightly cleanup task
  was disabled on 2026-09-07, mostly `036e4299`). Each was cut at the record the gate judged, its full
  message rebuilt from every text block sharing that record's message id, and pushed through `decide()`
  twice with a per-case state root: as shipped, and with the three detectors disabled.

  | | Count |
  |---|---:|
  | Replayed | 38 |
  | As-shipped verdict reproduced the recorded signal | 32 |
  | Not reproduced, all explained by vetoes added since: 5 commit-block, 1 the 08-26 negation guard | 6 |
  | Would still allow without the prose paths (`question-mark` 5, `work-complete` 6, `refusal-token` 1) | 12 |
  | Would now block, attributable to the retirement | 20 |
  | — of which a genuine ask the operator had to answer | 8 |
  | — of which the phrase was quoted, or the message was discussing this gate | 5 |
  | — of which a report, an aside, a non-refusal, or a promise to report back | 7 |

  So the prose paths were wrong more often than right: 12 of the 20 stops they alone bought were the
  accidental kind, and 3 of the 8 genuine asks had a `?` mid-paragraph that the closing-line check would
  have taken had the question ended its line. The cost of the retirement is one extra round trip on a
  genuine ask written without the token or a closing `?`, and the block reason now names both fixes.

  **What is left, and what is not fixed by this.** The earning signals are the operator grant, a
  `NEEDED:` line naming a thing, a `REFUSING:` line, a closing line ending in `?` in a message that does
  not announce its next step, and `DONE:`/`VERIFIED:` over a tool call. The `blocker-prose` row of the
  ordering table below is moot; `blocker-token` still sits ahead of the announce veto, so a `NEEDED:` line
  beside "I'll report back" still allows. That is a deliberate declaration rather than an accident, and
  it is left as it was. Harness: each retired phrase is a block case beside its token twin, and
  `needs-your-decision-under-a-promise-to-report-earns-nothing` is the 12:49:52Z shape. Re-instating the
  old `continuing_is_impossible` over the new suite turns the ten prose block cases red as predicted,
  plus one that was not: `echoing-the-gate-reason-earns-nothing`, because the block reason now tells the
  agent in so many words that "your call" and "blocked on you" earn nothing, and under the old regex
  naming a phrase was using it. The reason could not have said that while the prose paths lived, which
  is the standing-rule-versus-teaching tension from checklist item 10 resolved by removing one side.

- **(CLOSED 2026-09-14 — the veto now reads only the closing question's line.) Writing *about* the
  gate tripped the gate: the announce veto matched pattern names quoted in prose.** Observed 2026-08-30 on a message analysing this very defect. It ended in a plain question
  mark and its tail also matched `decision-request`, yet it blocked, while a one-line restatement of the
  same question allowed. The difference was a single inline-code span in the analysis — the sentence
  ``the pattern wants `continuing now` adjacent`` — which `RE_ANNOUNCE` matched. The veto sits ahead of
  the question check and returns `None`, so a genuine question was discarded before it was ever tested.

  Two things make this reachable. `RE_ANNOUNCE` is searched against the **raw** text, not the
  fence-stripped `unquoted` used by the `NEEDED:`/`REFUSING:` checks; and `RE_FENCE` only strips triple
  backtick blocks, never inline spans. So any message that discusses the announce patterns, including
  every future write-up of this gate, can eat its own question.

  **Not fixed, deliberately.** The obvious repair — strip inline code before the announce check — is a
  NARROWING of a veto, and it opens the matching evasion: an agent could wrap "I'll continue now" in
  backticks and tack on a question mark to buy a stop. Tripping the veto costs a recoverable block;
  evading it costs a real one. That trade is the operator's call, not a tidy-up, and it should not be
  made without a negative case pinning the evasion.

  **Settled a different way, by the operator.** Inline code is still not stripped; instead the veto reads
  only the closing question's line, so an analysis quoting a pattern above its question keeps the
  question (`quoting-an-announce-phrase-in-an-analysis-does-not-cancel-the-question`). A pattern on the
  question line itself still blocks (`a-promise-on-the-question-line-still-blocks`), and the evasion that
  scoping opens — the promise on its own line with a bare tag beneath — is pinned by
  `a-tag-question-under-an-announcement-still-blocks`, beside the control
  `a-short-question-with-no-announcement-still-allows`.

  The asymmetry is deliberate, not an oversight. `fenced-tokens` and `fenced-needed` establish that
  quoted content must not **earn** a stop, which is why `NEEDED:`/`REFUSING:` are matched against the
  fence-stripped text. The veto is the mirror: quoted content currently **denies** one. Stripping quotes
  everywhere would make the two consistent and both weaker, so whichever way this is settled, settle it
  as a deliberate choice about which direction quoting should be trusted in.

- **A prose user record is not reliably the start of an operator turn, and the tool-call corroboration
  was scoped to one.** (CLOSED 2026-08-25 — two structural shapes excused.) The operator reported
  well-formed `DONE:`/`VERIFIED:` messages being blocked "more and more often lately". Replaying the
  reported text through the real gate returned **`allow:work-complete`**, and the only variable that
  flipped it was `ToolCalls`: same text with zero tool calls in the scanned window blocked. So the text
  was never the problem.

  `Get-TranscriptFacts` counts tool calls back to the last **prose** user record, on the assumption that
  such a record marks the operator's prompt. It does not. Classifying every prose user record in three
  real transcripts: of **677 halts, 116 were mid-turn injections** — 47 `<local-command-caveat>` blocks
  from slash commands, 33 skill preambles, 31 records where an operator message sent mid-turn rides along
  on the record delivering a `tool_result`, and 5 `<system-reminder>` blocks. Each one strands every tool
  call made before it. Confirmed against the live gate: **twelve** tool calls followed by an interjection
  still blocks; one tool call *after* the interjection allows. That is also why it worsened — slash
  commands and skills are the two fastest-growing sources of these records.

  **Neither available flag separates injections from genuine prompts, and both looked like they would.**
  `isMeta` splits `local-command` almost evenly (**29 false / 30 true**). `promptId` is worse than it
  appears from the turn-key note above: **128 genuine operator prompts carry the same `promptId` as the
  turn already running**, while **57 slash-command caveats get a new one**. Either would have shipped as
  a silent wrong fix that left every fixture green. A blocklist of injection wordings would rot the same
  way the refusal verb list did.

  So only shapes that are true by construction are excused: a record carrying a `tool_result` cannot be a
  prompt the operator just typed, and a `<system-reminder>` is written by the harness rather than sent by
  anyone. `<local-command-caveat>` is deliberately **left as a boundary**: of its 59 records, **57 carry
  a `promptId` distinct from the running turn**, which is the signature of a real user-invoked command,
  so halting there is usually correct.

  **The trend is measured, not inferred.** `decisions.log` (197 decisions, 11 sessions, 2026-08-22 to
  08-25) shows the block rate climbing **34.9% → 54.3% → 50.0% → 74.4%** across four consecutive days.
  That is the operator's "more and more often" as a number, and it is what the P0 log was built for —
  before it existed this could only have been argued.

  `LastUserText` now latches on the newest prose instead of being set by whatever record ends the scan,
  so a grant sent mid-turn is still read.

  **Each of the four new fixtures was proved to discriminate by breaking the fix three different ways.**
  A single break would have left two of them unfalsifiable — the same defect as F7, where a fixture sat
  green against every mutation it was supposed to catch:

  | Break applied to a copy of the gate | Fixture that moves |
  |---|---|
  | **A** — undo the widening, every prose record halts again | `interjection-riding-a-tool-result-keeps-tool-calls`, `system-reminder-does-not-strand-tool-calls` |
  | **B** — keep the widening, drop the `LastUserText` latch | `grant-sent-mid-turn-is-still-read` |
  | **C** — over-widen, never halt at any prose record | `fresh-prompt-with-no-work-still-blocks` |

  Break C is the one that matters most. This change is a **widening**, so by the narrowing-asymmetry rule
  the positive fixtures cannot carry the load — a fix that simply never halted would turn all three
  green. Only the negative case catches that, and neither A nor B moves it. **Keep all four**, and note
  that A and B leave it green: a fixture is only pinned by the mutation that actually reaches it.

  **Provenance is NOT established, and the distinction matters.** The mechanism is confirmed against the
  live gate; the specific stop the operator pasted is not. That transcript is not on this machine, and
  `decisions.log` lives in machine-local `TEMP`, so neither source can name its session — most likely it
  ran on another box. What is proved is that this mechanism blocks a well-formed completion; what is not
  proved is that it caused *that* one.

  **Still open:** skill preambles (33 records) and slash-command caveats remain scan boundaries, so an
  agent that invokes a skill mid-turn and then finishes without another tool call is still blocked. The
  honest fix is to stop deriving the corroboration from an inferred boundary at all — see the
  `stop_hook_active` section, which already carries the real turn signal. Note this is the **same
  mechanism** F10 flags in `new-user-turn-resets-the-budget`, whose claim about turn detection is already
  known to be false: P5 should treat them as one design problem, not two.

- **`NEEDED: nothing from you` earned a stop — the blocker token used to assert its own opposite.**
  (FIXED 2026-08-26.) An agent ended a turn with `NEEDED: nothing from you — the agents are running
  unattended and I'll keep auditing`, followed by `Next I'll verify the large repo docs`. The gate
  allowed it as `blocker-token`.

  Two things went wrong at once, and the second is the structural one. The token's entire meaning is
  *there is a thing only the operator can supply*; a negating payload asserts that there is not, and
  still matched. And the message announced its own next steps, which the announce-then-stop family is
  explicitly disqualified for — but **`RE_NEEDED` is tested before `RE_ANNOUNCE`, so that disqualifier
  was never reached.** Any token check ahead of the announce veto has the same hole; this is the
  concrete case P5 predicts when it argues condition (2) belongs as a veto *after* the disjunction.

  It is rare and real: over seven days, **2 of 61 `NEEDED:` lines** negate the token (3%), in two
  different sessions, both phrased "nothing from you".

  **The ordering hole is wider than the one token, and is measured.** Four signals are tested ahead of
  the announce veto, so none of them can ever be disqualified by it:

  | Signal | Allowed (7d) | Also announcing next steps | Reaches the veto? |
  |---|---:|---:|---|
  | `blocker-token` | 59 | 5 | **no — bypasses** |
  | `blocker-prose` | 95 | 9 | **no — bypasses** |
  | `refusal-token` | 3 | 1 | carve-out, by design |
  | `refusal-prose` | 33 | 0 | carve-out, by design |
  | `question-mark` | 176 | 0 | yes |
  | `decision-request` | 14 | 0 | yes |

  So **14 of 380 allowed stops** were bought through a blocker signal in a message that also announced
  its next step. Read that as an upper bound on exposure, not 14 abuses: a genuine blocker that also
  mentions what happens afterwards is legitimate, and `NEEDED: you to run the push` is in that 14. Only
  the 2 negated lines are confirmed abuse. The zeroes in the last two rows show the veto does work
  where it is reached. Widening it to cover the blocker signals is P5's design, and it must keep the
  refusal carve-outs, because the block reason promises in writing that a refusal is always earned.

  The fix captures the payload and ignores a line that opens with a negation; one real blocker among
  several lines is still a blocker. **A narrowing cannot be proved by what it removes**, so the load is
  carried by what it must leave alone: all 61 real payloads were replayed through the patched detector
  and **exactly the 2 negating lines dropped, with all 59 genuine ones untouched**. Three of the four
  new fixtures are over-narrowing guards for the same reason, and the two blocking ones were confirmed
  to fail against the previous behaviour before being trusted.

- **MECHANISM FOUND, 2026-08-29: the gate's view of the transcript lags the file by hours.**
  (FIXED — this is the cause of the misfire entry below, which was OPEN for four days.) Session
  `036e4299` blocked a message carrying a clean `DONE:` and `VERIFIED:` pair. `facts.log` settles it
  without any replay:

  ```
  block:20  01:51:57  size=55319356  tc=2  newest=assistant/tool_use  newest_ts=01:15:11  age=2206.0s
  block:21  01:52:36  size=55319356  tc=2  newest=assistant/tool_use  newest_ts=01:15:11  age=2244.8s
  block:25  03:05:01  size=55350693  tc=2  newest=assistant/tool_use  newest_ts=01:15:11  age=6589.8s
  ahead='The stubs I need already exist. Adding a fixture overload and the discriminating'
  ```

  `size` grows while `newest_ts` and `ahead` stay frozen for 110 minutes. The `DONE:`/`VERIFIED:`
  message sits at bytes **58,803,353–58,807,081**, which is **3.45 MB beyond** the largest size the
  gate ever saw. And independent of byte offsets: at 01:51:57 the gate's newest record was 01:15:11,
  while the file holds a `Stop hook feedback` record stamped **01:18:08** — written 33 minutes before
  the gate looked.

  **This is why every earlier hypothesis failed.** A lagging view is complete, every line parses, and
  it ends on a record boundary, so `endsnl=True` and `badjson=0` throughout; replaying the file later
  shows the finished message and looks like a gate bug; and both implementations behave identically
  because the input, not the logic, is wrong. It also explains the one-message-lag *shape*: a small lag
  strands one message, a large one strands hours.

  **The guard, and why an age threshold is defensible.** Across 193 recorded decisions the ages
  separate completely:

  | age at decision | count | verdicts |
  |---|---:|---|
  | under 1s | 121 | 49 allow / 72 block |
  | 1–5s | 31 | 9 allow / 22 block |
  | 5–30s | 12 | 3 allow / 9 block |
  | 30–120s | 2 | 2 block |
  | 2–10 min | 8 | **8 block, 0 allow** |
  | 10–60 min | 18 | **18 block, 0 allow** |
  | over 1 hour | 1 | **1 block** |

  Slowest healthy decision **55.3s**, oldest stale one **6,590s**, and an empty band between **55.3s**
  and **150.6s**. Every one of the 27 decisions above the band is a block and none is an allow, which
  is what a stale view must produce: a message from two hours ago cannot carry this turn's completion
  signal.

  **RESOLVED 2026-08-30, and the resolution is the lesson.** An age threshold was fitted to this
  distribution, then a distinct block reason, then a retry budget, then two more diagnostic fields to
  work out why the retries failed. All of it was deleted. The gate reads
  `last_assistant_message` from the Stop payload instead, which is the documented answer and was the
  documented answer the whole time — see the top of this file. The measurements above are kept because
  they are a fair record of the lag, but note what they could not tell anyone: the shape of the
  distribution says nothing about the *mechanism*, and fitting a threshold to it produced a control
  that fired correctly and still could not do anything useful. **A well-measured workaround for a
  problem you have not diagnosed is still a workaround.**

- **A 41-block run with zero allows, in a session that repeatedly met the completion contract.**
  (OPEN, 2026-08-26.) Session `94edf861` logged `block:1` through `block:41` **consecutively, with not one
  allow of any signal**, across turns that carried line-anchored `DONE:` and `VERIFIED:` lines *and* tool
  calls in the same turn. `allow:work-complete` has fired **52 times across all sessions**, so the signal
  is not broken in general — it never engaged once here.

  **This is the first entry written from `decisions.log` rather than from replay**, which is what
  checklist item 11 built it for: before the log, this could only have been argued by reimplementing the
  detectors, and the two worst measurement errors on record came from exactly that.

  It also **supersedes the measured claim in the entry below**. That entry states "the longest run is 15";
  this run is **41**, nearly 3×, and `allow:budget-exhausted` still never appears — so the wedge guard is
  holding as designed while the completion signal sits inert behind it. Both facts are true at once, and
  the second is the one that matters: a guard that never fires is not evidence the gate is working.

  ~~**The symptom is reproducible and the mechanism is NOT established.** Running the gate's own
  `read_facts()` in-process against the live 29.8 MB transcript returns **stale facts** — `assistant_text`
  of "Merge is in. Union gate running on `main`..." (~30 turns old) and `last_user_text` of "comitted"
  (~25 turns old), so the completion check evaluates text that never contained the tokens.~~

  **REFUTED 2026-08-26.** `read_facts` is not stale. Replaying it over the same transcript cut at each
  historical decision point returns the **current** message every time, and `assistant_text` is set by
  the **first** `update_fact` call — the newest record — on 44 of 47 cuts. Whatever produced that probe
  output, it was not the gate reading a healthy file; the value quoted above is the state the file was
  in at **00:46**, which is what the gate correctly saw for `block:1` through `block:3`.

  Three of the eliminations in the original table were sound and are kept: token wording/placement,
  `RE_FENCE` fence pairing, and detector short-circuiting were all correctly ruled out. Two more are now
  positively disproved rather than argued:

  | Claim | Now |
  |---|---|
  | `file-history-delta` and friends exhaust the 8 MB scan window | **Disproved directly** — a full-file prefix and a 4 MB tail of that same prefix return identical `assistant_text`, `tool_calls` and `last_user_text` on **all 47** cuts. No budget effect, no chunk-boundary effect, no `pending` bug. |
  | It is a logic defect, not a race | **Neither, on current evidence** — see below. |

  **What is actually true, measured.** Every block leaves a `Stop hook feedback` user record at a known
  byte offset, so cutting the file immediately before that record reconstructs the gate's input exactly,
  with no timestamp guessing (transcript timestamps are **not** monotonic in file order — 392 inverted
  pairs, one jumping 9 hours backwards, so any binary search over them is invalid). Re-running all 99
  recorded blocks through the **registered command** on those exact cuts:

  ```
  re-ran 99 recorded blocks through the registered command
    still block : 54
    now allow   : 45
    signals     : block:1 54, allow:blocker-token 17, allow:work-complete 16,
                  allow:question-mark 9, allow:blocker-prose 2, allow:refusal-prose 1
  ```

  So **45 of 99 blocks were misfires**: the agent had earned a stop and was blocked anyway. The
  reconstruction method is not assumed — it was validated against ground truth. A block deliberately
  triggered in a live session cut at offset `11468502`, and the gate's own instrumentation recorded
  `size=11468502` for that same decision.

  **It is fleet-wide, not one session.** The same exact-offset replay over every transcript touched in
  the last five days — 17 files, the largest 208 MB — covering the most recent 40 blocks of each:

  | Session | Blocks replayed | Replay as allow | Signals |
  |---|---:|---:|---|
  | `94edf861` | 40 | **23** | blocker-token 13, work-complete 8, question-mark 2 |
  | `fbb73624` | 40 | **20** | work-complete 15, question-mark 3, blocker-token 2 |
  | `d965498a` | 40 | 3 | question-mark 2, blocker-token 1 |
  | `8347af5f` | 3 | 1 | question-mark 1 |
  | 10 others | 84 | 0 | — |

  **47 of 207 replayed blocks — 23% — were misfires.** Discount `allow:no-assistant-text` and
  `allow:operator-grant` results wherever they appear: a real block cannot have had empty
  `assistant_text`, because `decide()` allows in that case, so those are replay artifacts of slicing a
  tail rather than gate misfires. The five in `63c1d490` are exactly that and are excluded above.
  `fbb73624` was live while this was measured, which is why the operator saw it as happening "right now".

  **The shape of the misfires is a one-message lag, and that is the strongest available clue.** The
  misfires are exactly those blocks whose *previous* message carried no stop signal; the 54 correct
  blocks are those where neither message did. They interleave at 22-second granularity (`block:46`
  correct at 01:54:04, `block:47` a misfire at 01:54:26), which fits a consistent off-by-one far better
  than intermittent corruption.

  **Eliminated since, each with a measurement rather than an argument:**

  | Hypothesis | Result |
  |---|---|
  | The PowerShell → Python port caused it | **No** — misfires start at block #14 on 2026-08-25 03:26, hours before the flip at 09:25, and continue after it. Two independent implementations do not share a bug; they share an input. |
  | The gate reads a half-written final record, whose JSON silently fails to parse | **No** — 13,509 samples at 4 ms over a live transcript caught **0** mid-write states across 10 appends, and every `facts.log` row since shows `endsnl=True badjson=0`. |
  | Long-running sessions cached the old hook registration from `settings.json` | **No** — only the Python gate writes `facts.log`, and all four sessions deciding after instrumentation went live appear in it. |
  | Records are rewritten in place, or written early with empty text and completed later | **No** — 0 duplicate uuids across 10,204, and 0 of 1,022 assistant text records are empty. |
  | The two implementations drifted, so sessions on one gate misfire | **No** — both were run against all 99 real cuts and disagreed on **0**. `Test-StopGateParity.ps1` compares them over harness fixtures; this compared them over the transcript that actually failed. |

  **The sync hypothesis has one negative result against it already.** Watching five live transcripts for
  9 minutes — 10,315 samples of size, newest-record timestamp and NTFS FileId — recorded **0 size
  regressions, 0 timestamp regressions and 0 file replacements**. That does not clear OneDrive over
  longer windows, but nothing was caught in the act.

  **Context that is confirmed and matters.** `~/.claude` is a junction to `C:\Users\crgol\OneDrive\.claude`,
  and `projects/` carries ReparsePoint + Pinned attributes — the transcripts the gate reads are OneDrive
  Files On-Demand objects, actively synced, on a machine where OneDrive is running. The repo's own
  global instructions already require `projects/` to be excluded from sync. A sync client that serves a
  complete but older copy is invisible to every check the gate can make, because such a file parses
  perfectly. This is **not** established as the cause — it is the leading unexcluded candidate.

  **Do not "fix" this on the above.** The mechanism producing the one-message lag is still unpinned, and
  an age-based staleness guard would not catch it: the lag is one message, not a wall-clock delay, and
  measured record ages at decision time are 0.4–0.7 s in the healthy case.

  **What is now instrumented so the next occurrence carries its own evidence.** `write_facts` records a
  line per decision in `facts.log`, deliberately in its own file because `Get-LastSignal` in
  the deleted PowerShell harnesses both read the **last** tab-separated
  field of `decisions.log`, so adding a column there would silently feed both harnesses a diagnostic
  where they expect the signal. Each line carries `size`, `endsnl`, `badjson`, `tc`, the newest
  message-bearing record's `type/blocks` and timestamp, its `age` at decision time, the transcript
  filename, and the first 80 characters of the text the gate judged. A recurrence is then settled by one
  comparison: does `ahead=` match the message the user actually read?

  **A tempting explanation, measured and dropped: agents are not writing half a pair.** Across 404
  blocked messages in the last seven days, **355 (88%) carry neither token**, and only **9 (2%)** carry
  exactly one — 6 `VERIFIED:` alone, 3 `DONE:` alone. Teaching the pair harder would buy almost nothing.

  What the same pass did surface is sharper: **40 blocked messages carried BOTH tokens.** Under the
  current gate that is only possible if `tool_calls` was 0 at decision time, or if the gate judged a
  different message than the one holding the tokens. Those are very different defects and were
  previously indistinguishable; `facts.log` records `tc` and `ahead` per decision, so the next
  occurrence separates them without any replay. 40 of 404 is also consistent in magnitude with the
  work-complete misfires counted above (16 in `94edf861`, 15 in `fbb73624`).

  **Checking for a recurrence is one command, and needs nothing from the operator:**

  ```
  python Tools\Hooks\find_stop_gate_misfires.py
  ```

  It reads `facts.log`, and for every blocked decision reports two independent findings. **REPLAY** cuts
  the transcript at the size the gate recorded and re-runs the real `decide()`; if that now allows, the
  block was a misfire. **DRIFT** compares the text the gate judged against the newest assistant text
  record now ending at or before that same size; these must be the same record, and if they are not,
  the file changed under the gate — the one-message-lag signature, with both messages printed.

  `test_find_misfires.py` proves it discriminates before any clean run is believed: it fires on a
  transcript where the gate demonstrably judged the previous message, stays silent on a correct block
  built from that same file, and skips a decision whose transcript is shorter than the recorded size
  rather than counting it as a finding. A detector reporting zero is otherwise indistinguishable from
  one that cannot report at all.

  **`facts.log` lives under the temp root (`claude_temp.py`), so it is per machine.** A recurrence seen on another box is
  recorded on that box, and the command has to be run there.

  **What is established without any replay:** a session can meet the documented completion contract on
  dozens of consecutive turns and never earn a stop, and the only exit left is an operator grant. That is
  the failure the completion signal was added on 2026-08-20 to remove.

- **The wedge guard has now been observed, and it is holding as designed.** (CONFIRMED 2026-08-25.) The
  first read of `decisions.log` showed `block:8` through `block:15` and I recorded it as a budget
  failure, reasoning from the harness's `-MaxBlocks 3`. That was wrong: the production default is
  **`MaxBlocks = 100`**, raised deliberately because a cap of 3 silently defeated the gate on long
  autonomous turns. A run of 15 is well inside budget, not a defect.

  What the log does establish is the claim the parameter's own comment could previously only assert —
  *"it has never been observed at MaxBlocks = 100"*. Across 197 decisions, `allow:budget-exhausted`
  appears **zero** times and the longest run is **15**. The guard has never fired, and that is now a
  measurement rather than an assumption.

  Two lessons worth keeping. The harness value is not the production value, so **read the parameter
  default before calling a live number a defect** — this entry existed as OPEN for one revision purely
  because I skipped that. And a signal's *absence* in the log is evidence too, not just its presence.

- **Scope, not vocabulary, is what separates a refusal from a sentence about one.** (CLOSED 2026-08-20 —
  head-scoping plus `REFUSING:`.) `$refusalPattern` was the only allow-granting check that scanned the
  **whole** message: the question and decision-request checks are tail-scoped, and `NEEDED:`, `DONE:`,
  `VERIFIED:` are line-anchored. So one subordinate clause anywhere in a long report bought an
  unconditional stop. A finished Librarian status report — forty lines of tables, gate results, and design
  decisions, with no question, no token, and no earning signal of any kind — ended with *"it moves to the
  Curator batch — I won't guess the reason client-side"*, and the gate allowed the stop on `I won't`.

  Measured across 28 transcripts: **66 of 5,483 assistant messages** carry one of these phrases, and **38
  of those sit deeper than the opening lines**. Nearly every match was ordinary prose — "I won't repeat
  it", "I will not touch the processes I did not start" — not a refusal.

  **The first fix attempt was to constrain the object** (`I won't` + a verb + `that`/`this`). It regressed
  `refusal-is-a-legitimate-stop`, whose text is *"I will not push this without an explicit instruction from
  you"* — `push` is a verb no enumerated list would contain. **The verb list is unenumerable; the scope is
  not.** Vocabulary tightening only closes the phrasings you happened to think of, and the phrasing that
  caused this incident was one nobody anticipated.

  **Refusals are naturally initial, which is the mirror image of questions being naturally terminal.** The
  house style is to decline plainly in a sentence, offer the nearest alternative, and move on — so a real
  refusal is in the opening lines and its alternatives are in the tail. Tail-scoping it would have missed
  the canonical shape exactly. It is now head-scoped to the first three non-empty lines, capped at 300
  characters — see the line-scoping entry below for why the line bound alone did not hold.

  **Head-scoping accepts a narrow miss, so refusal also needed the taught token — and it was the one
  signal `$gateReason` never taught.** For every other signal a miss costs a round-trip. For this one a
  miss means the block reason instructs an agent that just declined on safety grounds to *"resume the work
  now, in this turn, with a tool call"*. `REFUSING:` is the guaranteed exit that never requires complying.

  **Line-scoping alone was not enough, and only a replay found that out.** (CLOSED 2026-08-20 — a
  300-character cap on the head.) Feeding all 43 historical allows that no named signal explained back
  through the live gate returned **40 would now block, 3 would not**, and one of the three exposed the
  flaw: *"the first three non-empty lines"* sounds like a tight window and is not one in markdown, where
  a paragraph is a single line. Those heads measured **766–846 characters** — most of the message — and a
  clause **654 characters deep** (*"`.stversions` — Syncthing's version archive, which I won't edit"*)
  still read as a refusal. Both genuine refusals on record sit at **offset 0**, because a real refusal
  leads the message; that is the premise head-scoping rests on, and the cap finally makes the scope match
  the premise. Pinned by `refusal-deep-in-the-opening-paragraph-does-not-count` — **keep that fixture on
  one line**, since rewrapping it into three short lines destroys the thing it tests.

  **Residual, documented rather than chased:** an emphatic agreement whose surface form is identical to a
  refusal still buys a stop — *"You're right, and I'm not going to soften it: …"* sits at offset 18,
  inside any defensible cap, and the message goes on to do the work rather than decline it. One
  occurrence in 431 recorded decisions. Telling it from a real refusal means knowing whether the object
  is the user's request or the agent's own prose, which is the unenumerable-verb problem wearing a hat.

  The fixture pair that matters is `refusal-in-the-opening-line-allows` against
  `refusal-buried-in-a-report-does-not-count` — **the same refusal sentence in both**, so position is the
  only variable. A head-only fixture would still pass with the scoping deleted.

- **The blocker check had the same unscoped-scan shape, and scope was the wrong lever for it.**
  (PARTIAL 2026-08-21 — an object constraint on one alternation, not a scope change.) `$blockerPattern`
  scanned the whole message.

  **The numbers this change was made on are inadmissible, and that is the first thing to know about it.**
  The counts below — 41 blocker-only allows, 30 tail, 11 body, `blocked on` 5 of them — came from
  `blocker_scope2.py`, a Python reimplementation of the detectors. Checklist item 11 forbids exactly
  that, and it was written one revision earlier, citing the previous time it happened. Treat the figures
  as a hypothesis. The replay that would settle them is: all 41 through the real gate, pre- and
  post-change, per-case `-StateDirectory`, plus the control checklist item 7 requires. It has not been
  run. Note also that only the 11 body-only cases were ever classified for genuineness while the change
  alters the verdict for all 41, so the measured population and the blast radius are different sets.

  **Tail-scoping would still have been wrong, and the reason is in the data rather than in a story.**
  The earlier draft of this entry justified it with an invented example — *"I'm blocked on you, and it's
  the only thing left"* — which has n=0 observed instances, and every genuine-blocker fixture in the
  harness is single-line or lead-position, so the fixtures argue against it. The real argument:
  **tail-scoping preserves the 30 tail matches and kills only the 11 body ones**, which is backwards for
  false-positive reduction.

  The property that separates the cases is the **object**: a blocker is what only the operator can clear.
  There are **eleven** alternations, and **three** name no operator — `blocked on`, `cannot proceed until`
  and `standing in the way`. Only `blocked on` was constrained, to `blocked on (you|your)`, so it no
  longer matches a table cell reading `| Re-enrichment pass | blocked on the two above landing |`.
  ~~A raw grep puts `cannot proceed until` and `standing in the way` and their siblings at roughly **5×**
  the prevalence of `blocked on you`, so the least common offender is the one that got fixed.~~
  **That was wrong, and it was another unverified figure of exactly the F1 kind.** Measured 2026-08-25
  over **27 transcripts and 8,516 assistant messages**, counting each alternation separately:

  | Alternation | Messages | Sentence names no operator |
  |---|---|---|
  | `your call` | 73 | 0 |
  | `waiting on (your\|you)` | 13 | 0 |
  | `blocked on (you\|your)` | 7 | 0 |
  | `which (one )?do you want` | 6 | 0 |
  | `needs? your <noun>` | 5 | 0 |
  | `let me know (which\|whether\|if)` | 3 | 0 |
  | `cannot proceed until` | **2** | 1 |
  | `standing in the way` | **2** | 1 |
  | `requires your` | 2 | 0 |
  | `need (you\|your) to` | 1 | 0 |
  | `confirm before I` | 1 | 0 |

  The two supposedly-worst offenders are the two **rarest**, and the single no-operator instance of each
  is **this session's own write-up describing the problem** — so the true count of genuine occurrences is
  **zero**. `blocked on you`, far from being the least common offender, is the third most common.

  **A caution for anyone re-running this.** The corpus now contains these phrases *because the analysis
  was written into it*. Self-contamination is why both no-operator hits are the same paragraph, and it
  will get worse with every revision of this file. Exclude the analysis text, or the measurement will
  slowly start measuring itself.

  Pinned by `blocker-names-the-operator-as-the-object` against
  `blocker-object-is-another-task-does-not-count` — the same sentence frame, the object the only variable.
  Note this is an OBJECT pair, not the position pair the refusal entry uses; checklist item 6 asks for a
  pair that isolates the property being relied on, and here that property is not position.

  **The reason it was worth doing before anything else: this check runs before `Test-WorkIsComplete`.**
  Any `DONE:`/`VERIFIED:` message that mentioned an obstacle in passing exited on the blocker path and
  never reached the completion contract — so anything that contract is ever asked to require had a front
  door around it. `incidental-obstacle-does-not-bypass-the-completion-contract` pins that ordering.

  **What is actually still open, now that it has been measured.** `cannot proceed until` and `standing
  in the way` do name no object, but at **2 occurrences each and zero genuine ones** the exposure is
  theoretical; constraining them buys almost nothing and removing them risks almost nothing. Either is
  defensible, neither is urgent — and **the ordering argument above is now the only real reason to
  touch them**, not prevalence.

  `your call` is the one worth thinking about, at **73 occurrences** — an order of magnitude more than
  anything else here. It is safe only because the operator reference is inside the phrase itself, which
  is a property of that literal wording rather than a constraint anyone imposed.

  Separately, one recorded allow came from a **negated** phrase — *"No — not stopped, and nothing was
  waiting on you"* — which no object constraint addresses; that is a vocabulary problem of its own, and
  the measurement above cannot see it because it counts matches rather than intent.

  **A green harness proved nothing about this change, and the revert experiment showed exactly why.**
  Reverting `(you|your)` and re-running turned **two** cases red —
  `blocker-object-is-another-task-does-not-count` and
  `incidental-obstacle-does-not-bypass-the-completion-contract` — while
  `blocker-names-the-operator-as-the-object` (*"The batch is blocked on you."*) stayed **green**, because
  it matched the bare `blocked on` equally well. A pair written to satisfy checklist item 6 contained
  item 6's exact failure. See item 6 for the general rule this produced: a narrowing's positive half can
  never discriminate, so it must instead cover every branch the constraint kept.

  **And the fixture named for ordering does not test ordering.**
  `incidental-obstacle-does-not-bypass-the-completion-contract` was added on the reasoning that the
  blocker check runs *before* `Test-WorkIsComplete`. That reasoning is wrong — see the disjunction note
  in `Invoke-StopGate.ps1`. Both checks are pure and both exit identically, so swapping them changes no
  verdict; the fixture passes for the previous fixture's reason, wearing a misleading name.

- **The gate reimplements three things the platform hands it, and one of them is why it fails open.**
  Researched 2026-08-21 against the current hooks reference, after a session in which the gate's design
  was reasoned about entirely from the existing script. The documented Stop payload carries
  `last_assistant_message`, and the reference states directly that hooks needing the final assistant text
  of the current turn should use it **instead of reading the transcript** — which is precisely what
  `Get-TranscriptFacts` exists to do. It also carries `prompt_id`, the turn key the "transcript-derived
  turn key" section above went through two wrong derivations to obtain.

  **But the payoff is smaller than that sentence sounds.** The backwards scan produces **three** facts
  and `last_assistant_message` replaces exactly one. `ToolCalls` — the completion signal's floor — and
  `LastUserText` — the only un-forgeable signal — both still need the same tail read,
  the same chunking and the same 8MB cap. It is an optimisation of one branch, never a replacement for
  `Get-TranscriptFacts`.

  **The hook states no `timeout` in `~/.claude/settings.json`, so it runs under the platform default for
  a `command` hook, 600 seconds.** A `Stop` hook blocks the end of the user's turn, so that budget is
  also how long the operator can watch a turn hang; the tail-first read, the 8MB scan cap and the
  `MaxScanBytes` bound are what keep a decision far below it. Adding a `timeout` is a live-config change
  on four synced machines and belongs to the operator, never to a test that wants a number to compare.
  `test_stop_gate.py`'s `stop-hook-timeout-has-real-margin` reads `hook.get("timeout", 600)`, so an
  absent key is measured against that default.

  **Caveat on how this was learned, because it nearly went the other way.** The first fetch of the
  reference asserted that `decision`/`reason` are "not used for Stop events" — flatly contradicted by 246
  recorded blocks from this very script. A second fetch demanding verbatim quotes returned
  "NOT PRESENT ON PAGE" for the same point, so the first answer was invented. Treat a summarised doc
  answer as a lead, not a fact: the payload fields above are **documented but not yet empirically
  confirmed on this install**, and the cheap confirmation is to dump one real Stop payload before
  building on any of them.

- **Quoting the announcement patterns in prose trips the gate.** Writing about `proceeding now` or
  `I'll report back` — as this document and any discussion of the gate must — matches the same regex as
  using them. It fired on the message that first reported the gate as built. The gate reads text, not
  intent, and does not exempt code spans or quotes. Since the veto was scoped to the question line it
  bites only when the quote sits on that line, or when the question is a bare tag.

  **The same effect ran the other way inside the gate's own reason text, and that one was an escape
  hatch.** Question 2 read *"are you blocked on something only the user can resolve"*, and `blocked on`
  is the first alternation in `$blockerPattern` — so an agent that quoted the gate back earned an
  immediate stop, for the gate's entire life before 2026-08-20. Found only because a test written for the
  completion tokens expected a block and got an allow; the tokens were fine, the reason was not.

  **Standing rule: `$gateReason` must stay clear of every pattern the gate matches on.** The reason has
  to name each signal in order to teach it, which is exactly what makes a careless phrasing there a free
  bypass. `echoing-the-gate-reason-earns-nothing` pins this against the live reason string, so any future
  wording that reintroduces a trigger fails the harness rather than shipping quietly.

  **But the trigger word in question 2 was also doing real work, and removing it broke something worse
  than it fixed.** The old wording answered itself in the detector's own vocabulary: an agent that
  reported a blocker in the words the question used produced text the regex recognized. The first
  reword — *"is something only the user can resolve standing in the way"* — had no counterpart in
  `$blockerPattern` at all, so an agent following it literally stated a genuine blocker and was blocked
  anyway. That is the dangerous direction: the reason then tells it to restate as a question, and the
  documented end of that path is self-answering a decision that belonged to the user. The bypass needed
  the agent to *disobey* the reason's own "do not restate this gate"; the misalignment punished the
  agent for obeying it.

  **The way out of the circle is a line-anchored token, the same device the completion signal uses.**
  Question 2 now teaches `NEEDED:` — named inline in the reason so quoting it matches nothing, matched at
  line start so an agent that follows the instruction lands on it exactly. The prose alternations stay,
  because they catch the phrasings agents already use unprompted; the token is the reliable path, not the
  only one. `blocker-declared-with-the-taught-token` and `blocker-phrased-as-question-2-invites` pin both
  halves, and the general lesson is the checklist item below: **when you change the reason, re-check that
  what it teaches still routes into what the detector matches.**

- **`NEEDED:` and `REFUSING:` are matched against unstripped text, so a pasted code fence buys a stop.**
  (OPEN.) `Test-WorkIsComplete` strips fenced blocks before matching `DONE:`/`VERIFIED:`, precisely so a
  build script printing those words cannot declare completion on the agent's behalf. The blocker and
  refusal tokens are matched on raw `$Text` and have no such strip — so tool output, a quoted document,
  or a diff containing either token at line start earns an unconditional stop. Same hole, same file, and
  `tokens-inside-a-code-fence-do-not-count` has no sibling for this pair.

- **Every literal space in every pattern is a line-wrap bug.** (OPEN.) A space in a .NET regex never
  matches a newline, so any multi-word alternation misses across a hard wrap. Constraining `blocked on`
  to `blocked on (you|your)` added a second seam where there had been one. The consequence is worst in
  `$grantPattern` — `stand down`, `you can stop`, `no need to keep going` — because a wrapped operator
  grant misses **the only un-forgeable exit the gate has**, which is the 2026-08-19 incident reproduced
  by whitespace. The fix is `\s+` for every literal space, and no fixture currently contains a wrapped
  signal phrase.

- **A session *about* the gate poisons the operator-grant read.** (OPEN.) `Update-StopGateFact` walks
  past any user record containing `STOP GATE:` to reach what the operator actually said. That marker is
  not distinctive — it is ordinary prose the operator pastes whenever discussing this gate, which whole
  sessions have done. When a genuine prompt contains it the scan skips the current turn and reads an
  **older** message as `LastUserText`, so a stale grant can disable the gate or a real one can be missed.
  The obvious fix is wrong: the marker arrives mid-line in real records, so anchoring to `^` breaks the
  skip entirely. This needs a distinctive sentinel, not an anchor.

- **The completion signal's unit does not match the gate's window.** (CLOSED 2026-09-08 for a pinned
  goal, by the third veto: the operator's `/goal` or `GOAL:` line is latched per session and every
  `DONE:` must restate it verbatim. Still open for any session in which the operator pins nothing,
  which is what the motivating session was; the 2026-08-21 case below would have been caught only if
  the accumulated backlog had been pinned.) `DONE:` is defined against
  "the original ask", and nothing checks the restatement against anything. Observed 2026-08-21: a stop
  with genuinely excellent proof — real values, real tool calls, nothing forged — that the operator
  rejected on sight with *"why did you stop? I don't see anything actionable."* The agent's own
  diagnosis: *"I called DONE on the narrow ask and ignored the backlog this session accumulated."*
  Comparing `DONE:` against `LastUserText` would **not** catch it, because the backlog accrued across the
  session while the last prompt genuinely was finished. This is the same unit mismatch as the `promptId`
  budget bug: the signal's unit is the ask, the gate's window is one turn.
  The precedented mechanism is the **drift protocol's echo** below — "state what was changed and anything
  deliberately left out" — which this file already credits with surfacing drift where instructions
  failed. `DONE:` now has that echo when a goal is pinned. The cost is real: completion is already the
  most expensive signal to earn, and raising its price pushes agents toward the cheaper ones, which the
  motivating session also showed, rotating through `NEEDED:` and `REFUSING:` once `DONE:` was challenged.
  Those two hatches are not addressed by the third veto.

- **The signals are regex, not judgment.** The gate cannot tell finished work from a well-written report;
  it can only tell whether the message *claims* a reason to stop. It is a forcing function, not an oracle.
  The dangerous direction of that gap — a real question the regex misses — is handled by the block reason
  itself (see "A missed question must be re-asked, never self-answered"), not by the regex.
- **A determined bypass exists** — writing a question, or the word "blocked", would earn a stop. The gate
  targets an unintentional habit, not adversarial evasion. If bypass phrasing starts appearing in
  transcripts, that is itself the signal to revisit.

- **There is no signal for a user-GRANTED stop, and this is the sharpest known gap.** All three earning
  signals describe the *agent's* state — I have a question, I am blocked, I refuse. None of them can
  express "the operator has released me." Observed 2026-08-19: the operator wrote "once you drafted the
  handoff stopping was earned," then repeated it, then asked "are you stupid?" — while the gate blocked
  four consecutive attempts to comply. The agent's only compliant moves were to invent a question, to
  keep working against an explicit instruction, or to absorb blocks until the budget drained.

  Worse, the false-positive mitigation above actively *causes* the failure here: told to restate its
  question and end the turn, the agent re-asked a question the operator had already declined to answer.
  That is the nagging pattern `feedback-suggest-commit-message-not-commit-action` records as forbidden,
  produced by the gate's own instructions.

  **Cost is bounded only by the budget**, so an explicit instruction to stop costs at least three wasted
  turns — and before the `promptId` fix the budget never engaged, which means a user-granted stop could
  not be honored *at all*. The operator's interrupt key was the only exit.

  **CLOSED 2026-08-20 — `Test-OperatorGrantedStop`.** A fourth earning signal, read from the last
  *genuine* user record. The scan walks past the gate's own feedback to reach it, matching on the
  `STOP GATE:` marker this script emits itself, and past every other `isMeta` record that wakes no one
  (no peer, task or idle origin): another Stop hook's feedback such as `/goal`'s, a skill body, a command
  caveat. Read as the operator, such a record cancels a woken turn's quiet stop and hands the grant check
  prose no operator wrote. An operator-typed record never carries `isMeta`. It is **the only un-gameable signal the gate has**: every
  other one is agent-authored text, so the agent can always write its way to a stop — the "determined
  bypass" gap above is exactly that — whereas the agent does not author user records.

  **Its failure mode is asymmetric: a false grant disables the gate for the whole turn.** The first draft
  of the pattern matched **7 of 10** realistic operator messages as grants — `stop working` fired on
  "stop working on the tests and switch to the docs", `that's all` on "that's all the rows in that file",
  `you're done` on "let me know when you're done". Worst, `stop stopping` was listed as a grant while
  meaning the exact opposite. Only the gate blocking the turn in which it was written forced the pattern
  to be tested before it shipped.

  **Standing rule: every phrase added to `$grantPattern` needs a sibling negative case** in
  `test_stop_gate.py` proving an ordinary working instruction containing the same words still blocks.
  Twelve cases pin this today, including `no-grant-stop-stopping` and `no-grant-asks-why-stopped` —
  "why are you stopping?" is the operator complaining, not releasing.

  This remains a regex over prose, which is what produced every false positive in that session. The
  durable replacement is `stop_hook_active` as a platform-supplied turn-boundary signal (see below);
  the grant pattern is the interim.
- **There was no signal for finished work, and the reason text lied about it.** Every earning signal
  asked whether the agent needed something *from the user*; none of them could express "the thing you
  asked for is done." Meanwhile the reason text asserted that if all the gate questions came back no,
  "you are not done" — false for exactly the case that should be the most common ending. A finished agent
  had no honest move: keep working past the finish line, or manufacture a question.

  **CLOSED 2026-08-20 — `Test-WorkIsComplete`.** `DONE:` and `VERIFIED:` lines plus at least one tool call
  in the turn. See "The completion signal" above for the full contract.

  **The residual gap is that this signal is the most forgeable of the four.** It is agent-authored text
  sitting on a floor rather than a corroboration: the tool count proves only that *something* ran, not
  that what ran verifies what the `VERIFIED:` line claims. An agent that reads a file and then writes
  a false proof line satisfies the gate. That is the accepted trade: the alternative was a gate that tells
  finished agents they are not finished, and the declaration at least makes the claim explicit, attributed,
  and checkable by the user against the transcript. Escalation, if it is ever needed, is corroborating
  against tool *kind* rather than tool count.

  **Known false negative: a turn with no tool calls can never earn a completion stop.** A question
  answered purely from context — "what was the commit message?" — is genuinely finished and genuinely has
  no empirical proof to offer. Those turns must earn a stop some other way, usually an operator grant.
  This is deliberate: dropping the tool-call requirement would reduce the signal to prose, which is what
  the gate exists to distrust.

- **`SubagentStop` is deliberately not wired, and this gate is probably the wrong tool for it.** It was
  proposed as the obvious extension and does not survive contact with what actually went wrong. A
  subagent's final report *is* its deliverable — it delivers by ending — so blocking that is not the same
  correction it is for the main agent. And the observed failure was **drift, not idling**: one agent
  silently swapped its assignment with a fork it had spawned, so status checks against it kept returning
  an empty output file while it worked on something else entirely. Forcing it to continue would have made
  it continue the wrong task. Drift wants a different mechanism — assignment echoed back and checked, or a
  parent-side task assertion — not a continuation gate. See the drift protocol below.

## The drift protocol (what to do instead of `SubagentStop`)

Drift is a *parent-side* problem, so the correction is parent-side. No hook enforces this; it is a habit
the parent applies when spawning, and unlike the idle-stop habit it has held so far.

1. **Demand the echo.** The prompt ends by requiring the report to state what was ported/changed and
   **anything deliberately left out**. That single requirement is what surfaces drift: an agent working on
   something else cannot produce a matching echo without the mismatch becoming visible.
2. **Forbid silent re-delegation.** Drift happened via a fork the agent spawned and then handed its
   assignment to. State in the prompt that the agent does the work itself.
3. **Name the file-level boundaries.** "Do not touch anything under `Curator/`", "do not wire a trigger",
   "do not change `Program.cs` wiring" — a boundary the agent can check itself, and the parent can verify
   afterwards with `git status`, is worth more than a description of the goal.
4. **Verify against the echo, not against the summary's tone.** A confident report is not evidence. Both
   agents in the 2026-08-14 session reported accurately, and one of them **corrected the parent's framing**
   — it was told to port a path that was already ported, said so plainly, and audited it instead. That is
   the protocol working: the echo is what made the mismatch surface in the first place.

The cost of the echo is a few lines per prompt. The cost of drift was an agent-hour spent on the wrong
task, discovered only because its output file stayed empty.
- **Untested:** interaction with plan mode, with `/compact`, and whether a block during a long background
  task produces a useful continuation or just churn.

## Checklist when changing the gate

1. Run `python Tools\Hooks\test_stop_gate.py`; expect every case to pass.
2. Confirm `settings.json` still parses.
3. Remember the settings reload lag documented in `PRETOOLUSE-HOOK.md` — never judge a live change
   immediately after editing.
4. Add harness cases for anything from the gap list you close.
5. **Ask what each prose detector is scoped to, not just what it matches.** Every allow-granting check
   needs a defensible answer to "where in the message does this signal actually live?" — questions are
   terminal, refusals are initial, tokens are line-anchored. A whole-message scan is the wrong default:
   it turns any long report into a search space, and one subordinate clause into an exit. When a detector
   produces a false positive, fix its scope before touching its vocabulary — vocabulary only closes the
   phrasings you thought of, and `refusal-is-a-legitimate-stop` proved the verb list is unenumerable.
6. **Pin a detector with a pair that differs only in the property you are relying on — then revert the
   change and count the red.** For a scope change that property is position, the same sentence in the
   head and in the body; for a vocabulary change it is the object or the verb.

   **A narrowing has an asymmetric pair, and assuming symmetry is how a useless fixture ships.**
   Narrowing removes matches, so anything still matching afterwards matched before too: **the positive
   half cannot discriminate, ever.** `blocker-names-the-operator-as-the-object` was written as the
   positive half of an object pair and passed identically with the constraint reverted — measured, not
   argued: reverting turned exactly two of three new cases red and left that one green.

   The positive half still earns its place, at the opposite job — catching a narrowing that went **too
   far** — which means one case per branch the constraint kept. A lone `blocked on you` would not notice
   a regression that dropped `your`, so `blocker-object-you-still-allows` and
   `blocker-object-your-still-allows` are separate cases.

   Predict the number of failures before reverting, and treat a different count as the finding. That is
   `Test-CodeStyleCatalog.ps1`'s own break-it-on-purpose discipline applied here.
7. **Run the change against a REAL transcript before trusting it.** Every bug in this gate's history came
   from a fixture that did not model production — a static transcript that hid the dead loop guard, and a
   `-SkipStdin` call that could not exercise the budget once the boundary moved into the payload. Copy a
   live `~/.claude/projects/<slug>/*.jsonl` to scratch, drive the gate against it, and check both
   directions: the decision you expect, **and a control** — an ordinary instruction that contains the same
   words as the signal you are testing, in the same position, which must still block.
8. **When adding a signal, measure its false positives before shipping.** A grant pattern that looked
   reasonable matched 7 of 10 realistic operator messages. Write the negatives first.
9. **If a signal is corroborated by transcript evidence, add the case where the evidence is absent.**
   `Test-WorkIsComplete` counts tool calls during the tail read, and the counter had to be placed *above*
   `Update-StopGateFact`'s `AssistantText` early-return — reading newest-first, that guard short-circuits
   every earlier assistant record, so a counter one line lower would read zero forever. Nothing else in
   the harness would have noticed: `complete-claim-with-no-tool-calls` is the only case that fails when
   the corroboration is dead, and its mirror `complete-with-proof-and-tool-calls` is the only one that
   fails when the counter is stuck high. A corroborating signal needs both, or it is untested by
   construction.
10. **After editing `$gateReason`, check the new wording BOTH ways.** It must not contain a trigger word
   (quoting the gate becomes a bypass), and what it instructs must still route into something the
   detector matches (a compliant agent gets blocked for obeying). Those two pull in opposite directions,
   which is why questions 2 and 4 teach line-anchored tokens — `NEEDED:`, `DONE:`, `VERIFIED:` — named
   inline in the reason and matched only at line start. Prefer that device over guessing at prose.
   `echoing-the-gate-reason-earns-nothing` covers the first direction and
   `blocker-declared-with-the-taught-token` the second, but read the wording too: the harness only knows
   the patterns that exist today.
11. **Read the decision log first; it exists so that auditing is not archaeology.** Every exit path now
    appends `<timestamp>\t<session>\t<signal>` to `decisions.log` in the state directory —
    `allow:operator-grant`, `allow:blocker-token`, `allow:refusal-token`, `allow:question-mark`,
    `allow:work-complete`, `allow:queued-for-gate`, `allow:waiting-on-own-gate`,
    `allow:no-assistant-text`, or
    `block:<tag><n>` where the tag is empty, `trailer:`, `goal:`, `commitblock:`, `stale:`, `gate-claim:`,
    `peer-owed:`, `unreported-gate:`, `repeat-question:`, `open-item:`, `wait-claim:`, `workspace-missing:`,
    `done-open:`, `commit-ask:`, `refusal-unquoted:`, `operator-claim:`, `unparked-question:`, `unparked-needed:` or
    `quarantine-unloadable:`, any of them prefixed `past-budget:` once the count passes `MaxBlocks`, or
    `block:error:<exception>` (session `unknown`) when the gate itself raised, or `eject:quarantined` when the stop belongs to a
    session the write-protection gate ejected (`allow:blocker-prose`,
    `allow:refusal-prose` and `allow:decision-request` appear in rows before 2026-09-09 and can no
    longer be written). `facts.log` adds a `repos=` field carrying what the commit-state veto owed and
    called stale, so a `block:stale` row can be read without re-running git against a tree that has
    since changed. `continuing_is_impossible` returns the signal's name
    rather than True for exactly this reason: it covers several signals, and "continuing is impossible"
    records the least useful half of what an audit needs.

    Before this existed, every allow was silent, so reconstructing *why* a stop was permitted meant
    re-deriving the detectors and running them over transcripts — and that inference is the direct cause
    of the two worst measurement errors on record. Both were treated as discipline failures. Both were
    a missing log line.

    **It is also a COVERAGE instrument, and that was not the intent.** The first harness run against it
    showed three cases passing through a signal other than the one they were written for, because
    `$blockerPattern` fires before the question and decision-request checks and swallows their vocabulary:

    | Fixture | Text | Signal that actually fired |
    |---|---|---|
    | `ends-with-question` | *"Which one do you want me to take?"* | `blocker-prose` (`which (one )?do you want`) |
    | `missing-critical-information` | *"Which do you want?"* | `blocker-prose` (same alternation) |
    | `please-confirm-without-question-mark` | *"Please confirm before I run it."* | `blocker-prose` (`confirm before I`) |

    So the trailing-`?` check could be **deleted outright** and the first two would stay green. Whenever
    two detectors share vocabulary, a fixture proves only that *something* allowed the stop — read the
    log line, not the verdict, and assert on the signal name.

    **To audit the gate's history, RUN the gate — never reimplement it.** Auditing past decisions by
    rewriting the detectors in another language produces a defect count that measures the reimplementation,
    not the gate. Done on 2026-08-20 it reported 23 unexplained allows; replaying the same 43 cases through
    `Invoke-StopGate.ps1` itself returned 40 blocks and 3 allows, and only one of the three was real. Build
    the synthetic transcript (`user` record, N `tool_use` records, final `assistant` text), pipe the `Stop`
    payload to `pwsh -NoProfile -NonInteractive -File <gate>`, and give each replay **its own
    `-StateDirectory`** so the block budget cannot leak between cases. Note the gate reads real stdin: `&
    $gate` in-process cannot feed it, so a subprocess is required.

    Two traps in the transcript records themselves. **`preventedContinuation` is `false` for BOTH outcomes
    and is not the discriminator** — a block is `hasOutput: true` with the reason in `hookErrors[]`; reading
    the wrong field reports "0 blocks ever" against a real history of 246. And **the gate's own injected
    block reasons are `isMeta: true` user records**, so a walk-back looking for the last operator turn must
    skip them or it terminates early and undercounts everything before it.
