# AoE Chief

A standby "chief of staff" for parallel AI coding agents running in **Agent of Empires (AoE)**.

You run several Codex / Claude Code sessions at once, but you cannot keep switching context between them. AoE Chief
watches the sessions for you. It drives delegated tasks forward on its own and only interrupts you when a human
decision is actually needed. Its reports are thin first, thick on demand.

## Why

It addresses two problems:

1. **Too many parallel agents, one human brain.** Checking every session to see who is stuck, who is done and who is
   waiting for an answer does not scale. A supervisor should do that and report back.
2. **Replies that are complete but not useful.** Agents tend to dump everything. A shared communication protocol
   makes them lead with the conclusion and end every turn with a machine-readable closing block, so that both you
   and Chief can triage quickly.

## How it works

```
   you: define the task                         you: accept or send back
            │                                              ▲
            ▼                                              │
   ┌──────────────────────────── task board ───────────────┴──────┐
   │ tasks/<id>/task.json · decisions.json · events.jsonl · review.md │
   └───────┬──────────────────────────────────────────▲───────────┘
           │ chief new: worktree + session + kickoff   │ every tick
           ▼                                           │
   ┌──────────────────┐   end of turn    ┌─────────────┴────────────┐
   │ worker (Codex /  │ ───────────────► │ chief: run checks, triage │
   │ Claude) in its   │ ◄─────────────── │ continue / recover /      │
   │ own git worktree │   next message   │ rotate / review           │
   └──────────────────┘                  └──────────────────────────┘
```

- **Read-only by default.** Chief reads AoE state, the agents' native transcripts (`~/.claude/projects`,
  `~/.codex/sessions`), the live tmux screen and git. It only acts on tasks you created with `chief new`.
- **Deterministic rules before AI.** Rules decide what needs you. AI is used only to write briefs, to optionally
  draft decisions, and to review finished work.
- **Done means checks pass.** Each task has executable checks. A task is done when the checks pass, not when the
  agent says so. Protected paths, such as tests, are monitored, so the agent cannot game the checks.

Signal levels in the brief:

| Level | Meaning |
|---|---|
| 🔴 needs you | Big decision, blocker, budget exceeded, protected file touched, stuck, or review failed |
| 📝 to confirm | Chief drafted a small decision; one command approves it |
| ✅ to accept | All checks pass and a cross-model review card is ready |
| 🟡 changed | Progress since the last brief |
| ⚪ unchanged | Nothing new |

## Requirements

- Linux or macOS, Python 3.7+ (standard library only), git 2.20+
- Agent of Empires CLI (`aoe`, tested with 1.21) with a profile (default `main`)
- At least one agent CLI: `codex` and/or `claude`. Using both lets them review each other's work.

## Install

```bash
git clone <this repo> ~/aoe-chief
alias chief="python3 ~/aoe-chief/chief.py"
chief init

# Communication rules for the agents (closing block, thin-first replies). Merge them into your existing files if you
# have any.
cp rules/communication.md ~/.claude/CLAUDE.md
cp rules/communication.md ~/.codex/AGENTS.md
```

If another program called `aoe` comes earlier in your `PATH`, Chief prefers `~/.local/bin/aoe`. You can also set
`AOE_BIN`.

## Quick start: the demo

`demo.sh` creates a small repository with three bugs and delegates it to Codex. The worker is told to fix one test
per turn and to ask a question before one of the fixes. That exercises auto-continue, the decision queue and the
cross review. Its state lives in a separate directory, so your real board is untouched.

```bash
./demo.sh                                   # create ~/workspace/chief-demo and start the task
export AOE_CHIEF_HOME=~/.local/state/aoe-chief-demo
chief watch --every 30                      # in another terminal: let Chief drive
chief brief                                 # see what needs you
chief decisions                             # answer with: chief decide <id> --accept | --reply "..."
cat $AOE_CHIEF_HOME/tasks/shop-demo/review.md
chief close shop-demo                       # accept, or: chief nudge shop-demo "please also ..."
./demo.sh clean                             # remove the demo sessions, worktree, branch and state
```

## Daily use

**Delegated tasks** have a clear goal and objective checks. Create each one as a task:

```bash
chief new --repo ~/workspace/myproj --name "parser fix" \
  --goal "Make the parser accept trailing commas" \
  --check "pytest tests/parser -q" --scope src/parser --protected tests \
  --rounds 10 --hours 3          # add --dry-run to preview the contract and kickoff prompt
chief watch --every 300          # keep this running in a tmux window
chief brief                      # check in when convenient
```

**Thinking tasks** are open-ended conversations. You talk to the agent directly. Chief can still watch those
sessions and summarize them (`chief track <session>`), but it does not drive them.

Answer the agents' questions **through Chief** (`chief decide`), not inside the session. Every accept, override
and dismissal is recorded, and that record is the data used to decide which kinds of decisions Chief may make on
its own later (`chief decisions --stats`).

## Commands

| Area | Command | What it does |
|---|---|---|
| Observe | `sessions [--all]`, `track`, `untrack` | Pick AoE sessions to watch |
| | `brief [--ai] [--level 1-3]` | Triage report; `--ai` has an AI write it from the facts |
| | `show <id> [--turns N]` | Drill into one session or task: progress file, checks, decisions, recent turns |
| | `task <id> --goal --check --scope --protected` | Attach a goal and checks to a session you did not create with `new` |
| | `check <id>` | Run a task's checks now |
| Tasks | `new ...`, `tasks`, `kickoff <id>`, `close <id> [--abandon] [--remove-session]` | Task lifecycle |
| Drive | `tick [--dry-run]`, `watch --every SEC` | One round, or a loop, of automatic actions |
| | `nudge <id> [TEXT] [--review]` | Send guidance or review issues back, and resume automation |
| | `rotate <id>`, `review <id>` | Start a fresh session from the progress file; run a cross review now |
| Decide | `decisions [--all] [--stats]` | The decision queue |
| | `decide <id> --accept / --reply TEXT / --dismiss [REASON] / --redraft / --ai` | Resolve one decision |
| AI | `engine [codex\|claude]` | Default engine for briefs and drafts, and the model used at each level |

## What `tick` does

For each active task, `tick` does at most one thing. It does nothing for tasks with `autonomy: observe`.

| Situation | Action |
|---|---|
| Any red signal, or a pending decision | Nothing; the task waits for you |
| The turn was interrupted or errored | Recover once; if it fails again, the task goes to you |
| Checks fail and the turn ended | Send the failing output back and ask the worker to continue |
| The same failure `stuck_after` (3) times in a row | Mark the task stuck and hand it to you |
| `max_auto_continues` (8) continues without your input | Pause and hand it to you |
| Context use ≥ `rotate_at` (70%) | Start a fresh session in the same worktree; it resumes from `.chief/progress.md` |
| All checks pass and HEAD has not been reviewed | The other model reviews the diff against the contract and writes `review.md` |

## Configuration

Config lives in `$AOE_CHIEF_HOME/config.json` (default `~/.local/state/aoe-chief/`).

| Key | Default | Meaning |
|---|---|---|
| `profile` | `main` | AoE profile |
| `engine` | `codex` | AI engine for briefs and decision drafts |
| `models` | see `chief engine` | Per-engine, per-level model and effort overrides |
| `check_timeout` | 300 | Seconds per check command |
| `scope_ignore` | `[]` | Extra patterns ignored by the scope check |
| `decision_ai` | `false` | Let AI re-judge and draft new decisions |
| `stuck_after`, `max_auto_continues`, `rotate_at` | 3, 8, 70 | Automation limits |
| `review`, `review_engine`, `review_level` | `true`, the other engine, 2 | Cross review |

## Safety model

- Chief never pushes, merges or deploys, and tells workers not to do so either. Accepting work is always your
  decision.
- Check commands run as you, so only register commands you trust.
- Transcript, screen and diff content is treated as untrusted data in every AI prompt.
- Codex's "trust this folder" prompt is answered only when you pass `--trust`.
- Big decisions cannot be auto-accepted. Rules flag them: pushes, deletions, test changes, goal or budget changes,
  protected paths, or other red signals. An AI may upgrade a decision from small to big, but never downgrade one.

## State layout

```
$AOE_CHIEF_HOME/
  config.json            settings and sessions you track
  latest.md, reports/    the latest brief and the brief archive
  checks.json            check result cache, keyed by session
  preferences.md         your overrides of Chief's drafts; AI drafting reads this
  tasks/<id>/
    task.json            the contract and state
    decisions.json       the decision queue and its history
    events.jsonl         everything Chief did automatically
    review.md            the latest cross review card
<worktree>/.chief/progress.md   the worker's handoff file (git-ignored locally)
```

## Status

The core loop is implemented and tested end to end with real Codex and Claude sessions: task contracts, executable
checks, the decision queue, auto-continue, stuck detection, session rotation and cross review. Next is phase D:
push notifications for 🔴 items and per-category automatic decisions, unlocked by your acceptance rate. See
[docs/architecture.md](docs/architecture.md).

Chief's command output and code comments are currently in Chinese.

## License

MIT. See [LICENSE](LICENSE).
