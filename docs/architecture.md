# Architecture: task-centric supervision

## In one sentence

The unit of management is the **task**, not the session. You spend judgment at two points only: when you
**define** a task and when you **accept** the result. Chief handles execution, verification, small decisions and
dispatch in between. Sessions are disposable executors.

## Problems with the session-centric approach

| Session-centric reality | Consequence |
|---|---|
| Open a session first, describe the task in chat, write down the goal later (or never) | The supervisor has to guess what "done" means |
| Task state is inferred from the conversation | Accuracy depends on how the agent happens to phrase things |
| Several tasks share one working directory | Parallel work collides, and changes cannot be attributed |
| Every decision and every "continue" goes through you | Your attention stays the bottleneck |
| Long tasks run in a single session | The context grows and quality drops late in the task |

## The loop

```
         you: only at the two ends
   ┌──────────────┐                  ┌──────────────┐
   │ 1 define     │                  │ 6 accept      │
   │ intent →     │                  │ review card → │
   │ contract     │                  │ merge         │
   └──────┬───────┘                  └──────▲───────┘
          │                                 │
┌─────────▼─────────────────────────────────┴─────────┐
│ task board (single source of truth)  tasks/<id>/      │
│ task.json · decisions.json · events.jsonl · review.md │
│ draft → running → needs-you / review → done/abandoned │
└─────────┬─────────────────────────────────▲─────────┘
          │ 2 dispatch                       │ 4 verify + 5 decide
┌─────────▼──────────┐   end of turn  ┌─────┴────────────┐
│ worker (disposable)│ ─────────────► │ chief loop        │
│ own worktree/branch│ ◄───────────── │ check · triage ·  │
│ fresh session when │  next message  │ dispatch          │
│ context grows      │                └──────────────────┘
└────────────────────┘
```

### 1. Define: move accuracy to the front

Your judgment is worth the most at the start, so a task begins as a contract:

```json
{
  "name": "csv-import",
  "goal": "Import the legacy CSV export into the new schema without data loss",
  "checks": ["pytest tests/import -q", "python scripts/verify_counts.py"],
  "scope": "src/importer",
  "protected": ["tests/"],
  "budget": {"rounds": 15, "hours": 6},
  "autonomy": "suggest",
  "small_decisions": ["implementation details", "retry strategy", "report layout"],
  "milestones": ["the importer runs end to end", "all checks pass", "the report is written"]
}
```

- You do not have to write it by hand. `chief new "<one sentence>"` drafts it: checks and protected paths are
  inferred from the repository (pytest or unittest, `package.json`, a Makefile `test` target, `tests/`), then an
  AI reads the repository to refine the draft. If the AI fails, the inferred defaults are used. You confirm with
  Enter, or edit the draft.
- **Test first.** By default a task starts in a test phase. The worker writes failing acceptance tests, commits
  them, and asks you to approve them. That approval is always a big decision with no draft, in every autonomy
  mode, and Chief neither auto-continues nor reviews before it. Approving locks the test files as protected and
  resets the scope base to that commit. Reviewing a short test takes less time than writing it, and it is the
  point where "done" gets defined.
- Split a large task into several small ones, each with its own checks.
- `chief new` creates the task branch locally from the current HEAD (so unpushed commits are included), opens a
  git worktree for it (`aoe add -w <branch> --launch`), sends a kickoff prompt that states the delegated-worker
  rules, and registers the task on the board.

### 2. Execute: isolation and fresh context

- **One task, one worktree and branch.** Parallel tasks do not interfere, every change belongs to one task, and
  rolling back means deleting the branch.
- **A fresh session when the context grows.** The worker updates `.chief/progress.md` every turn. When context
  use passes a threshold, Chief starts a new session in the same worktree that continues from the progress file.

### 3. Rules for delegated workers (sent in the kickoff prompt)

- Make routine choices inside the scope yourself, and record them in `progress.md`.
- Stop and ask in `Decision needed` only when a step is out of scope, risky or irreversible, or when the goal is
  ambiguous. Always give the options and a recommendation.
- Do not claim done before the checks pass. Never modify protected files.

### 4. Verify: checks decide what "done" means

- Chief runs executable checks after each turn. They run in their own process group and are killed as a group on
  timeout.
- A scope check compares the diff from the task's starting commit against `scope` and `protected`.
- Stuck detection: if the same check fails the same way N turns in a row, the task is stuck.
- The failure signature consists of the command, the exit code and the last three output lines. Durations and
  addresses are stripped, but failure counts are kept, because a falling count means progress.

### 5. Decide: three tiers

| Tier | Who decides | When |
|---|---|---|
| Trivial | Worker | Routine choices inside the scope |
| Small | Chief drafts, you confirm with one command | All of these hold: inside the scope, reversible, does not touch protected paths, within budget, the worker gave a recommendation, and no rule flags it |
| Big | You | Everything else: irreversible changes, changes to the goal or checks, effects outside the repository, red signals |

- Rules can veto a decision on their own: pushes, deletions, test changes, goal, acceptance or budget changes,
  protected paths, or any other red signal.
- AI drafting is optional (`decision_ai`). The AI can only upgrade a decision from small to big, never downgrade
  one.
- Every resolution is recorded. Overrides and dismissals with reasons go to `preferences.md`, which the AI drafter
  reads. Acceptance rates per category decide which categories may later run without confirmation.

Loop rules:
- Checks fail and nothing blocks the task → continue automatically.
- The turn was interrupted → recover once.
- The task is stuck or the budget is spent → hand it to you.
- All checks pass → cross review.

### 6. Accept: cross review before it reaches you

- When all checks pass, a **different model** reviews the diff against the contract. Work written by Codex is
  reviewed by Claude, and the other way around.
- The reviewer sees the commit list, the diff, the progress notes and the decisions that were actually approved.
  It writes a card with the verdict (pass, concerns or fail), a one-sentence summary, the risks, where to look,
  and issues to send back.
- You decide whether to accept (`close`) or send the work back (`nudge --review`, or `nudge "..."`). Pushing and
  merging are always yours.

### Your interface: one inbox

- **🔴** big decisions, stuck tasks, protected files touched, failed reviews.
- **📝 / ✅** drafted small decisions and review cards, handled in batches.
- The brief also lists what Chief did automatically since the last brief.
- `chief inbox` is the interactive form of the same triage: a curses list with one key per action. It runs the
  same pass as the brief without checks, every 15 seconds, and sends in a background thread. Automation stays in
  `watch`, so closing the inbox never stops a task.
- `?` opens a question input. An owned worker calls `pick_model` and `generate_ai(read_files=False)` with
  explicit README/architecture documentation, a board snapshot including draft tasks, pending decisions,
  recent events and review cards. All evidence and recent conversation are redacted and encoded in a single
  untrusted JSON boundary; source delimiters are escaped. The worker emits answer data or a localized error,
  never command, editor, acceptance, decision or reply requests. Navigation and scrolling remain on the UI thread.
- The UI admits suggested commands only through `parse_inbox_command`. Keys `1`–`9` prefill `:` without submitting;
  the existing Enter handler parses edited input again and retains all `y/N` guards. Questions alone have no
  authority to act. Esc detaches the request before killing its owned process group, including descendants;
  late events cannot update a newer request. A deadline bounds the whole AI process tree. The last 12 question
  turns stay in memory across board refreshes and selection changes; PgUp/PgDn scroll them.
- Each board task's level is mirrored onto its AoE color dot (red: needs you, amber: working, green: to accept),
  so the AoE dashboard shows it too. Chief calls AoE only when the color changes.
- Both the inbox and `watch` can run as standing AoE sessions (custom agents), so tasks keep moving when no
  terminal of yours is open.

## Where accuracy comes from

1. Executable checks and protected tests: completion is objective, and the agent cannot pass by editing the tests.
   In the test phase you approve the tests themselves, so the checks measure what you meant.
2. Isolated worktrees: every change has an owner and can be rolled back.
3. Cross-model review: the same model never writes and reviews the same work.
4. Fresh context plus `progress.md`: long tasks do not degrade late in the task.
5. Labelled evidence: claims are marked as verified, inferred or guessed.

## Where efficiency comes from

1. Auto-continue: nobody has to type "continue".
2. Small decisions are drafted for you: only big decisions need thought.
3. Batching: decisions and acceptances are handled together.

## Costs and risks

- **Defining tasks takes more effort.** Writing contracts and checks takes time. The work moves from "watching
  in the middle" to "thinking clearly at the start". That pays off for delegated tasks, not for open-ended
  research.
- **Research tasks rarely have objective checks.** Approximate them with cross review against a rubric, and keep
  autonomy at `observe` or `suggest`.
- **Automation can run away.** Budgets, autonomy levels, limits on automatic continues, and the rule that pushes
  and merges are always manual keep it bounded. Permissions open up per decision category, based on how often
  you agree with Chief.

## Phases

| Phase | Content | Status |
|---|---|---|
| A | Task contracts, `chief new`, kickoff prompt, progress file, executable checks, scope and protected paths, budgets | done |
| B | Decision queue and suggest mode: queue `Decision needed`, size it, draft it, send it on your confirmation | done |
| C | `tick` / `watch`: auto-continue, recovery, stuck detection, session rotation, cross review cards, `nudge` | done |
| D | Push notifications for 🔴 items (AoE status hooks), per-category automatic decisions based on acceptance data | planned |

## Implementation notes

- Transcripts are normalized into prompt / reply / tool / done / error events.
  - Claude sessions follow `continued-in` links and skip title-only stub files.
  - Codex sessions use `task_complete` and `turn_aborted`.
- AoE may report `idle` before the agent's input box is ready, and `waiting` after a finished turn. Readiness is
  therefore judged from the screen: the input box is visible (Claude's box itself counts, since auto mode hides the
  "? for shortcuts" hint), there is no approval prompt and no trust prompt. Approval words count only at the start
  of a line, so "Decision needed: Approve the tests?" in a reply is prose, not a prompt.
- Secrets are masked where agent text enters Chief (both transcript parsers and the screen capture), so the brief,
  inbox, state files, decisions, review material and AI prompts never carry them. Masked: values after
  password-like keywords, well-known token formats and private key blocks.
- Kickoffs and decisions are sent with `aoe send`. Delivery is confirmed by the session turning `running`, or by
  the message appearing on screen.
- A pending decision is marked superseded if the session moved on, for example because you answered inside the
  session.
- `tick` keeps its own "last seen" state (`tick.json`), so it does not affect the brief's "changed" detection.
  Check results are cached in `checks.json`, keyed to the fingerprint at the time they ran.
