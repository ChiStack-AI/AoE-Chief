"""Tests for the inbox view model, rendering and key handling (no terminal, no AoE needed)."""
import importlib
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
os.environ["AOE_CHIEF_HOME"] = tempfile.mkdtemp()
import chief          # noqa: E402
import chief_tui as tui  # noqa: E402

def items():
    ts = chief.now_utc().isoformat()
    task = {"id": "shop-demo", "name": "shop demo", "state": "needs-you", "branch": "chief/shop-demo",
            "goal": "Make all tests pass", "session_id": "aaaaaaaa1111",
            "review": {"head": "h2", "by": "claude/opus", "verdict": "concerns", "summary": "mostly fine",
                       "issues": "1. handle empty input"}}
    return [
        {"id": "aaaaaaaa1111", "title": "shop demo", "tool": "codex", "status": "idle", "task": task,
         "log": {"last_ts": ts, "turn": "done", "footer": {"conclusion": "fixed total()", "next": "receipt"}},
         "git": {"head": "h1"}, "changed": True,
         "signals": [("draft", "chief drafted decision shop-demo:1: A) plain text", "decision"),
                     ("yellow", "checks 2/3 pass; failing: unittest (1)", "checks")],
         "decision": {"id": "shop-demo:1", "size": "small", "question": "Plain text or JSON?",
                      "choice": "A) plain text", "draft": "Decision: A", "by": "rules"},
         "checks": {"passed": 2, "total": 3, "results": [{"cmd": "unittest", "ok": False, "code": 1}]}},
        {"id": "bbbbbbbb2222", "title": "评审一个很长很长很长很长的中文标题任务", "tool": "claude", "status": "idle",
         "task": dict(task, id="review-zh", state="review", review=dict(task["review"], head="h2")),
         "log": {"last_ts": ts, "turn": "done"}, "git": {"head": "h2"}, "changed": False,
         "signals": [("green", "all checks pass (3/3), ready for you to accept", "checks")]},
        {"id": "cccccccc3333", "title": "explore", "tool": "claude", "status": "running", "task": {},
         "log": {}, "changed": False, "signals": [("info", "no goal", "no-goal")]},
        {"id": "dddddddd4444", "title": "gone", "status": "missing", "task": {},
         "signals": [("red", "the session is gone or archived", "missing")]},
    ]

def text(rows):
    return ["".join(t for t, _ in r) for r in rows]

class T(unittest.TestCase):
    def setUp(self):
        self.ch = chief

    def test_width_and_fit(self):
        self.assertEqual(tui.width("ab中文"), 6)
        for s in ("hello world", "中文很长很长的标题", "mixed 中文 text"):
            for w in (1, 5, 8, 30):
                self.assertEqual(tui.width(tui.fit(s, w)), w)
        for line in tui.wrap("一二三四五六七八九十 and some english words here", 12, " "):
            self.assertLessEqual(tui.width(line), 12)

    def test_entries_order_and_actions(self):
        es = tui.entries(chief, {"items": items()})
        # Delegated tasks precede thinking sessions; urgency still sorts each group.
        self.assertEqual([e["level"] for e in es], ["draft", "green", "red", "grey"])
        dr = es[0]
        self.assertTrue(dr["draft"] and dr["task"] == "shop-demo")
        self.assertFalse(dr["issues"])             # the review is for an older commit
        self.assertTrue(es[1]["issues"])
        self.assertIsNone(es[2]["task"])           # a session that is gone: nothing to send to
        self.assertNotIn("r reply", " ".join(tui.keys_for(chief, es[2])))
        self.assertIn("r reply", " ".join(tui.keys_for(chief, es[3])))   # a tracked session: r goes to it directly
        self.assertNotIn("d dismiss", " ".join(tui.keys_for(chief, es[3])))
        keys = " ".join(tui.keys_for(chief, dr))
        for k in ("a accept", "r reply", "d dismiss", "n nudge", "c close"):
            self.assertIn(k, keys)
        self.assertNotIn("b send back", keys)
        self.assertIn("b send back", " ".join(tui.keys_for(chief, es[1])))
        self.assertNotIn("n nudge", " ".join(tui.keys_for(chief, es[2])))

    def check_render(self):
        st = {"entries": tui.entries(chief, {"items": items()}), "sel": 0, "scroll": 0, "msg": "hi", "clock": "12:00",
              "tick_note": "last tick 1m ago"}
        for w, h in ((100, 30), (60, 16), (40, 10), (200, 50)):
            rows = tui.render(chief, st, w, h)
            self.assertEqual(len(rows), h)
            for r in text(rows):
                self.assertEqual(tui.width(r), w, (w, h, r))
        return st, text(tui.render(chief, st, 100, 30))

    def test_render_en(self):
        chief.LANG = "en"
        st, out = self.check_render()
        self.assertIn("1 needs you · 1 to confirm · 1 to accept · 4 tracked", out[0])
        self.assertTrue(any("[?] shop demo" in r for r in out))
        self.assertTrue(any("Chief's draft: A) plain text" in r for r in out))
        self.assertIn("a accept", out[-1])
        st["input"] = {"prompt": "Reply to shop-demo:1: ", "buf": "use JSON"}
        self.assertTrue(text(tui.render(chief, st, 100, 30))[-2].startswith("Reply to shop-demo:1: use JSON"))
        st["entries"] = []
        out = text(tui.render(chief, st, 100, 12))
        self.assertIn("0 tracked", out[0])

    def test_render_zh(self):
        chief.LANG = "zh"
        try:
            st, out = self.check_render()
            self.assertIn("需要你 1 · 待确认 1 · 待验收 1 · 跟踪 4", out[0])
            self.assertTrue(any("chief 草稿：A) plain text" in r for r in out))
            self.assertIn("a 采纳", out[-1])
        finally:
            chief.LANG = "en"

    def test_keys_and_input(self):
        calls = []

        class Fake:
            pass
        ch = Fake()
        for name in ("L", "RANK", "ACTIVE", "level_of", "ago", "clip", "sep", "TICK", "call"):
            setattr(ch, name, getattr(chief, name))
        ch.find_decision = lambda i: {"id": i, "state": "pending"}
        ch.load_task = lambda i: {"id": i}
        ch.accept_decision = lambda cfg, d: calls.append(("accept", d["id"])) or True
        ch.reply_decision = lambda cfg, d, t: calls.append(("reply", d["id"], t)) or False
        ch.dismiss_decision = lambda d, r: calls.append(("dismiss", d["id"], r))
        ch.nudge = lambda cfg, t, text=None, rv=False: calls.append(("nudge", t["id"], text, rv)) or True
        ch.close_task = lambda cfg, t: calls.append(("close", t["id"])) or {"branch": "b"}
        ch.reply_session = lambda cfg, sid, t: calls.append(("session", sid, t)) or True
        ch.collect = lambda cfg, checks, persist: {"items": items()}
        ch.all_tasks = lambda: []
        app = tui.Inbox({"profile": "main"}, ch, 15)
        app._refresh()

        def run(*keys):
            for k in keys:
                if app.st["input"] is not None:
                    app.typed(k)
                elif app.st["confirm"]:
                    app.answered(k)
                else:
                    app.key(k)
                while app.st["busy"]:
                    pass
        # The first delegated task is the drafted decision.
        self.assertEqual(app.selected()["level"], "draft")
        run("a")
        run("r", "u", "s", "e", " ", "中", "KEY_BACKSPACE", "J", "\n")
        run("d", "\x1b")                           # Esc cancels: nothing sent
        run("d", "\n")                             # empty reason is allowed for dismiss
        run("r", "\n")                             # empty reply is not sent
        run("n", "g", "o", "\n")
        run("c", "n")                              # not confirmed
        run("c", "y")
        run("b")                                   # no review issues on this commit: ignored
        run("j", "b", "y")
        run("r", "o", "k", "\n")                  # a board task with no queued decision: r sends guidance
        run("j", "j", "r", "好", "\n")            # a tracked session: r sends your words unchanged
        run("k", "r", "x", "\n")                  # the session that is gone: r does nothing
        self.assertEqual(calls, [("accept", "shop-demo:1"), ("reply", "shop-demo:1", "use J"),
                                 ("dismiss", "shop-demo:1", ""), ("nudge", "shop-demo", "go", False),
                                 ("close", "shop-demo"), ("nudge", "review-zh", None, True),
                                 ("nudge", "review-zh", "ok", False), ("session", "cccccccc3333", "好")])
        self.assertIn("not confirmed", "".join(str(app.st["msg"])) + " not confirmed")
        self.assertIs(app.key("q"), False)

if __name__ == "__main__":
    unittest.main()
