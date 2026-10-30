# Development

[Back to README](../README.md) · [Architecture](architecture.md)

## 🧪 Tests

Run from the repository root with Python 3.7+; no Python dependencies are needed:

```bash
python3 -m unittest discover -s tests  # Full suite: the completion check
python3 tests/fast.py                 # Fast iteration
```

The fast runner uses four isolated processes and excludes exactly the three real-subprocess inbox suites
listed below. Every test from every other discovered module runs, including acceptance tests; test and import
failures produce a nonzero exit status. Normal discovery still runs the entire suite.

Measured on Python 3.7.5, Linux aarch64, HiSilicon Kunpeng-920 (96 CPUs):

| Measurement | Before | After |
|---|---:|---:|
| Full command | 169.85 s · 363 tests | 87.89 s · 376 tests |
| Fast subset | 42.57 s · 276 tests, serial | 14.97 s · 289 tests, four workers |
| `test_inbox_ai_assistant_acceptance` | 59.18 s | 20.29 s |
| `test_inbox_unified_log_acceptance` | 39.53 s | 18.11 s |
| `test_inbox_command_bar_acceptance` | 30.48 s | 17.47 s |

Full/fast values are command wall times; slow-module values are summed per-test times from complete-suite
execution audits. The fast baseline selected the same modules before `fast.py` existed. All 363 original IDs
remain collected and executed; the additional 13 are the approved latency/runner acceptance tests. Existing
tests and support files are unchanged. Both targets, full under 90 s and fast under 20 s, were met.

Inbox improvements include 10 ms active polling with 250 ms idle waits, reusable command parsers, cached
Python loading for command workers, and a lightweight owned AI supervisor. Fresh Git scans overlap while
only immutable commit data is cached. Real engines, command workers and process-tree cancellation remain exercised.

## 🗺️ Roadmap

- [x] **Observe:** briefs built from native transcripts, the screen and git
- [x] **A:** task contracts, `chief new`, executable checks, scope and protected paths, budgets
- [x] **B:** decision queue and suggest mode
- [x] **C:** auto-continue, recovery, stuck detection, session rotation, cross-model review
- [ ] **D:** push notifications for 🔴 items, and per-category automatic decisions unlocked by your acceptance rate
- [x] **Inbox:** an interactive terminal inbox and AoE color dots
- [x] **Intent tasks:** `chief new "<one sentence>"` with drafted contracts, and a test-first approval phase
- [x] **Secret masking** at ingestion
- [x] A test suite in the repository (`python3 -m unittest discover -s tests`)
- [x] Inbox: `N` to delegate a task in one line, and separate groups for delegated and thinking work
- [x] Recognise Claude's and Codex's newer trust prompts; report Codex's startup update menu without touching it

The design rationale is in [architecture.md](architecture.md).
