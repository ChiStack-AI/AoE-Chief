#!/usr/bin/env python3
"""AoE Chief inbox: a terminal UI over the same pass as `chief.py brief`, with one key per action.

Run it with `chief.py inbox` (or register it in AoE as its own session). It refreshes every few seconds without
running checks; automation stays in `chief.py watch`. Actions go through the same functions as the CLI and run in a
background thread, because confirming delivery to a worker can take tens of seconds. Standard library only.
"""
import curses
from datetime import datetime
import locale
import os
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata

MARK = {"red": "[!]", "draft": "[?]", "green": "[+]", "yellow": "[~]", "grey": "[ ]"}
ORDER = ("red", "draft", "green", "yellow", "grey")

# ---------- Text width (CJK characters take two cells) ----------

def cell(ch):
    if unicodedata.combining(ch) or ch in "​‍":
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1

def width(s):
    return sum(cell(c) for c in s)

def fit(s, w):
    """Cut s to w cells (with an ellipsis when cut) and pad it to exactly w."""
    s = s.replace("\n", " ").replace("\t", " ")
    if w <= 0:
        return ""
    if width(s) <= w:
        return s + " " * (w - width(s))
    out, used = "", 0
    for c in s:
        if used + cell(c) > w - 1:
            break
        out += c
        used += cell(c)
    return out + "…" + " " * (w - used - 1)

def wrap(s, w, indent=""):
    """Wrap by cells, keeping line breaks; good enough for mixed CJK and Latin text."""
    lines = []
    for para in (s or "").splitlines() or [""]:
        cur, used = indent, width(indent)
        for word in para.replace("\t", " ").split(" "):
            piece = word if cur.strip() == "" and used == width(indent) else " " + word
            if used + width(piece) <= w:
                cur += piece
                used += width(piece)
                continue
            if cur.strip():
                lines.append(cur)
            cur, used = indent, width(indent)
            for c in word:                   # a word longer than the line (or CJK text without spaces)
                if used + cell(c) > w:
                    lines.append(cur)
                    cur, used = indent, width(indent)
                cur += c
                used += cell(c)
        lines.append(cur)
    return lines

# ---------- View model: collect() result → inbox entries ----------

def entry(ch, x):
    """One inbox row and its detail, as plain data (so rendering can be tested without a terminal)."""
    L = ch.L
    log, task, git = x.get("log") or {}, x.get("task") or {}, x.get("git") or {}
    lv = ch.level_of(x)
    sigs = [s for s in sorted(x.get("signals", []), key=lambda s: ch.RANK[s[0]]) if s[0] != "info"]
    rv = task.get("review") or {}
    review = rv if rv and rv.get("head") == git.get("head") else None
    ft = log.get("footer") or {}
    is_task = bool(task.get("id")) and task.get("state") in ch.ACTIVE
    detail = []
    if task.get("id"):
        detail.append(("head", L("Task {} · {} · branch {}", "任务 {} · {} · 分支 {}").format(
            task["id"], task.get("state"), task.get("branch") or git.get("branch") or "-")))
    if task.get("goal"):
        detail.append(("text", L("Goal: ", "目标：") + task["goal"]))
    for l, m, _ in sigs:
        detail.append((l, MARK.get(l, "   ") + " " + m))
    dec = x.get("decision")
    if dec:
        detail.append(("head", L("Decision {} ({}, by {})", "决策 {}（{}，{} 判断）").format(dec["id"], dec["size"], dec.get("by"))))
        detail.append(("text", L("Q: ", "问：") + (dec.get("question") or "")))
        if dec.get("draft"):
            detail.append(("draft", L("Chief's draft: ", "chief 草稿：") + (dec.get("choice") or dec["draft"])))
        elif dec.get("why"):
            why = ch.sep().join(dec["why"]) if isinstance(dec["why"], list) else dec["why"]
            detail.append(("text", L("Why it is yours: ", "为什么要你定：") + why))
    if review:
        detail.append(("head", L("Review ({}): {}", "审查（{}）：{}").format(review.get("by"), review.get("verdict"))))
        for key, label in (("summary", L("Summary: ", "摘要：")), ("risks", L("Risks: ", "风险：")),
                           ("look", L("Look at: ", "重点看：")), ("issues", L("Issues: ", "问题："))):
            if review.get(key):
                detail.append(("text", label + review[key]))
    ck = x.get("checks") or {}
    if "results" in ck:
        bad = [r["cmd"] for r in ck["results"] if not r["ok"]]
        detail.append(("text", L("Checks {}/{}{}{}", "检查 {}/{}{}{}").format(
            ck["passed"], ck["total"], L(" (stale)", "（可能过期）") if ck.get("stale") else "",
            (L("; failing: ", "；未通过：") + ch.sep().join(bad)) if bad else "")))
    evs = [e for e in x.get("events") or [] if e["kind"] != "review"][-3:]
    for e in evs:
        detail.append(("info", L("chief: ", "chief：") + e["text"]))
    if ft.get("conclusion"):
        detail.append(("text", L("Conclusion: ", "结论：") + ft["conclusion"]))
        if ft.get("next"):
            detail.append(("text", L("Next: ", "下一步：") + ft["next"]))
    elif log.get("last_reply"):
        detail.append(("text", L("Last reply: ", "最近回复：") + ch.clip(log["last_reply"], 600)))
    return {"sid": x["id"], "task": task.get("id") if is_task else None, "title": x.get("title") or x["id"],
            "tool": x.get("tool") or "", "status": x.get("status") or "", "ago": ch.ago(log.get("last_ts")),
            "level": lv, "headline": sigs[0][1] if sigs else (ft.get("conclusion") or ""),
            "decision": dec, "draft": bool(dec and dec.get("draft")),
            "test_approval": bool(dec and dec.get("kind") == "test-approval"),
            "issues": bool(review and review.get("issues")), "detail": detail}

def entries(ch, result):
    es = [entry(ch, x) for x in result.get("items", [])]
    return sorted(es, key=lambda e: ORDER.index(e["level"]))

def keys_for(ch, e):
    """The footer shows only the keys that do something for the selected entry."""
    L = ch.L
    k = []
    if e and e["decision"] and (e["draft"] or e.get("test_approval")):
        k.append(L("a accept", "a 采纳"))
    if e and e["status"] != "missing":
        k.append(L("r reply", "r 回复"))       # a pending decision, a board task or any tracked session
    if e and e["decision"]:
        k.append(L("d dismiss", "d 关闭决策"))
    if e and e["task"]:
        k.append(L("n nudge", "n 指示"))
        if e["issues"]:
            k.append(L("b send back", "b 退回"))
        k.append(L("c close", "c 验收"))
    if e:
        k.append(L("o open", "o 进入"))
    return k + [L("t tick", "t tick"), L("g refresh", "g 刷新"), L("q quit", "q 退出")]

# ---------- Rendering: state → rows of (text, style) segments ----------

def render(ch, st, w, h):
    L, es = ch.L, st.get("entries") or []
    rows = []
    n = {lv: sum(1 for e in es if e["level"] == lv) for lv in ORDER}
    left = " " + L("AoE Chief · {} needs you · {} to confirm · {} to accept · {} tracked",
                   "AoE Chief · 需要你 {} · 待确认 {} · 待验收 {} · 跟踪 {}").format(
        n["red"], n["draft"], n["green"], len(es))
    right = "{} · {} ".format(st.get("tick_note", ""), st.get("clock", ""))
    rows.append([(fit(left, max(0, w - width(right))), "header"), (fit(right, min(w, width(right))), "header")])
    if not es:
        rows.append([(fit(" " + (st.get("empty") or L("Loading…", "加载中…")), w), "info")])
    list_h = min(len(es), max(3, (h - 4) // 3)) if es else 0
    sel = min(st.get("sel", 0), max(0, len(es) - 1))
    top = min(max(0, sel - list_h + 1), max(0, len(es) - list_h))
    top = min(top, sel)
    for i in range(top, top + list_h):
        e = es[i]
        lead = " {} ".format(MARK[e["level"]])
        if w >= 90:                          # narrow terminals drop the metadata before the headline
            meta = " " + "  ".join((fit(e["sid"][:8], 8), fit(e["tool"], 6), fit(e["status"], 8), fit(e["ago"], 10))) + "  "
        elif w >= 60:
            meta = " " + fit(e["status"], 8) + "  "
        else:
            meta = " "
        tw = min(28, max(8, w // 4))
        rest = max(0, w - width(lead) - tw - width(meta))
        segs = [(lead, e["level"]), (fit(e["title"], tw), "title"), (meta, "dim"), (fit(e["headline"], rest), "text")]
        if i == sel:
            segs = [(t, "sel") for t, _ in segs]
        rows.append(segs)
    e = es[sel] if es else None
    bar = "── " + fit(e["title"], max(0, w - 8)).rstrip() + " " if e else ""
    rows.append([(fit(bar + "─" * max(0, w - width(bar)), w), "dim")])
    body_h = max(0, h - len(rows) - 2)
    body = []
    for style, text in (e["detail"] if e else []):
        for line in wrap(text, max(10, w - 2), " "):
            body.append((style, line))
    scroll = min(st.get("scroll", 0), max(0, len(body) - body_h))
    st["scroll"] = scroll
    for style, line in body[scroll:scroll + body_h]:
        rows.append([(fit(line, w), style)])
    while len(rows) < h - 2:
        rows.append([(" " * w, "text")])
    if st.get("input") is not None:
        prompt = st["input"]["prompt"]
        rows.append([(prompt, "prompt"), (fit(st["input"]["buf"], max(0, w - width(prompt))), "text")])
    elif st.get("confirm"):
        rows.append([(fit(st["confirm"]["prompt"], w), "prompt")])
    else:
        msg = st.get("msg", "")
        if st.get("busy"):
            msg = "{} {}… ({}s)".format("-\\|/"[int(st.get("now", 0)) % 4], st["busy"], int(st.get("now", 0) - st.get("busy_since", 0)))
        rows.append([(fit(" " + msg, w), "msg")])
    more = L(" PgUp/PgDn scroll", " PgUp/PgDn 滚动") if len(body) > body_h else ""
    rows.append([(fit(" " + "  ".join(keys_for(ch, e)) + L("  ↑↓ move", "  ↑↓ 选择") + more, w), "footer")])
    return rows[:h]

# ---------- The app ----------

class Inbox:
    def __init__(self, cfg, ch, every):
        self.cfg, self.ch, self.every = cfg, ch, max(5, every)
        self.st = {"entries": [], "sel": 0, "scroll": 0, "msg": "", "busy": None, "input": None, "confirm": None}
        self.last_refresh = 0
        self.lock = threading.Lock()

    # Background work: one job at a time; every job ends with a refresh.
    def job(self, label, fn, refresh=True):
        if not self.lock.acquire(blocking=False):
            self.st["msg"] = self.ch.L("busy: {}", "忙：{}").format(self.st.get("busy"))
            return
        self.st.update(busy=label, busy_since=time.time())

        def go():
            try:
                msg = fn()
                if refresh:
                    self._refresh()
                if msg is not None:
                    self.st["msg"] = msg
            except Exception as e:      # noqa: BLE001 — the UI must survive any failed action and show why
                self.st["msg"] = self.ch.L("failed: ", "失败：") + self.ch.clip(str(e), 300)
            finally:
                self.st["busy"] = None
                self.lock.release()
        threading.Thread(target=go, daemon=True).start()

    def _refresh(self):
        res = self.ch.collect(self.cfg, checks=False, persist=False)
        es = entries(self.ch, res)
        cur = self.selected()
        self.st["entries"] = es
        if cur:                              # keep the selection on the same session when the order changes
            self.st["sel"] = next((i for i, e in enumerate(es) if e["sid"] == cur["sid"]), self.st["sel"])
        self.st["empty"] = self.ch.L("Nothing tracked yet: chief.py new … or chief.py track <ID>",
                                     "还没有跟踪任何会话：chief.py new … 或 chief.py track <ID>")
        self.last_refresh = time.time()

    def refresh(self):
        self.job(self.ch.L("refreshing", "刷新中"), lambda: None)

    def selected(self):
        es = self.st["entries"]
        return es[min(self.st["sel"], len(es) - 1)] if es else None

    def tick_note(self):
        L = self.ch.L
        try:
            age = time.time() - os.path.getmtime(str(self.ch.TICK))
        except OSError:
            return L("no tick yet: run chief.py watch", "还没 tick：运行 chief.py watch")
        mins = int(age // 60)
        note = L("last tick {}m ago", "上次 tick {} 分钟前").format(mins)
        return note + (L(" (is watch running?)", "（watch 在运行吗？）") if age > 3600 else "")

    # Actions
    def sent(self, ok, what):
        L = self.ch.L
        return what + ("" if ok else L(", but delivery was not confirmed; press o to check", "，但没确认送达，按 o 进去看看"))

    def decision(self, e):
        d = self.ch.find_decision(e["decision"]["id"])
        if d["state"] != "pending":
            raise RuntimeError(self.ch.L("{} is already {}", "{} 已经是 {} 状态").format(d["id"], d["state"]))
        return d

    def task(self, e):
        return self.ch.load_task(e["task"])

    def key(self, k):
        ch, L, e = self.ch, self.ch.L, self.selected()
        es = self.st["entries"]
        if k in ("KEY_DOWN", "j"):
            self.st.update(sel=min(len(es) - 1, self.st["sel"] + 1), scroll=0)
        elif k in ("KEY_UP", "k"):
            self.st.update(sel=max(0, self.st["sel"] - 1), scroll=0)
        elif k in ("KEY_NPAGE", " "):
            self.st["scroll"] += 5
        elif k == "KEY_PPAGE":
            self.st["scroll"] = max(0, self.st["scroll"] - 5)
        elif k == "q":
            return False
        elif k == "g":
            self.refresh()
        elif k == "t":
            self.job(L("ticking", "tick 中"), lambda: "tick: " + ("; ".join(ch.tick(self.cfg)) or L("nothing to do", "无需处理")))
        elif k == "a" and e and (e["draft"] or e.get("test_approval")):
            approval = e.get("test_approval")
            self.job(L("approving the acceptance tests", "批准验收测试") if approval else L("sending the draft", "发送草稿"),
                     lambda: self.sent(ch.accept_decision(self.cfg, self.decision(e)),
                                       L("approved the acceptance tests", "已批准验收测试") if approval else
                                       L("sent chief's draft", "已发送 chief 的草稿")))
        elif k == "r" and e and e["decision"]:      # answers the queued decision, and records how you answered
            self.ask(L("Reply to {}: ", "回复 {}：").format(e["decision"]["id"]),
                     lambda text: self.job(L("sending your reply", "发送回复"),
                                           lambda: self.sent(ch.reply_decision(self.cfg, self.decision(e), text),
                                                             L("sent your decision", "已发送你的决定"))))
        elif k == "r" and e and e["task"]:          # no queued decision: your words go to the worker as guidance
            self.ask(L("Reply to {}: ", "回复 {}：").format(e["task"]),
                     lambda text: self.job(L("sending your reply", "发送回复"),
                                           lambda: self.sent(ch.nudge(self.cfg, self.task(e), text), L("sent", "已发送"))))
        elif k == "r" and e and e["status"] != "missing":   # a tracked session: your words go in unchanged
            self.ask(L("Reply to {}: ", "回复 {}：").format(e["title"]),
                     lambda text: self.job(L("sending your reply", "发送回复"),
                                           lambda: self.sent(ch.reply_session(self.cfg, e["sid"], text), L("sent", "已发送"))))
        elif k == "d" and e and e["decision"]:
            def dismiss(text):
                ch.dismiss_decision(self.decision(e), text)
                return L("closed {} (nothing was sent)", "已关闭 {}（未发送任何消息）").format(e["decision"]["id"])
            self.ask(L("Dismiss {} without sending; reason (optional, Esc cancels): ", "不发送直接关闭 {}；原因（可空，Esc 取消）：").format(
                e["decision"]["id"]), lambda text: self.job(L("dismissing", "关闭中"), lambda: dismiss(text)), allow_empty=True)
        elif k == "n" and e and e["task"]:
            self.ask(L("Message to {}: ", "发给 {} 的指示：").format(e["task"]),
                     lambda text: self.job(L("sending", "发送中"),
                                           lambda: self.sent(ch.nudge(self.cfg, self.task(e), text), L("sent", "已发送"))))
        elif k == "b" and e and e["issues"]:
            self.confirm(L("Send the review issues back to {}? (y/N)", "把审查问题退回给 {}？(y/N)").format(e["task"]),
                         lambda: self.job(L("sending back", "退回中"),
                                          lambda: self.sent(ch.nudge(self.cfg, self.task(e), None, True), L("sent back", "已退回"))))
        elif k == "c" and e and e["task"]:
            self.confirm(L("Close {} as done (session, worktree and branch stay)? (y/N)",
                           "把 {} 标记为完成（会话、worktree、分支都保留）？(y/N)").format(e["task"]),
                         lambda: self.job(L("closing", "验收中"), lambda: L("{} closed; branch {} kept", "{} 已完成；分支 {} 保留").format(
                             e["task"], ch.close_task(self.cfg, self.task(e)).get("branch"))))
        elif k == "o" and e:
            return "open"
        return True

    def ask(self, prompt, then, allow_empty=False):
        self.st["input"] = {"prompt": prompt, "buf": "", "then": then, "empty": allow_empty}

    def confirm(self, prompt, then):
        self.st["confirm"] = {"prompt": prompt, "then": then}

    def typed(self, k):
        inp = self.st["input"]
        if k in ("\n", "\r", "KEY_ENTER"):
            self.st["input"] = None
            text = inp["buf"].strip()
            if text or inp["empty"]:
                inp["then"](text)
        elif k == "\x1b":
            self.st["input"] = None
            self.st["msg"] = self.ch.L("cancelled", "已取消")
        elif k in ("KEY_BACKSPACE", "\x7f", "\b"):
            inp["buf"] = inp["buf"][:-1]
        elif k == "\x15":                    # Ctrl-U clears the line
            inp["buf"] = ""
        elif len(k) == 1 and k.isprintable():
            inp["buf"] += k

    def answered(self, k):
        c, self.st["confirm"] = self.st["confirm"], None
        if k in ("y", "Y"):
            c["then"]()
        else:
            self.st["msg"] = self.ch.L("cancelled", "已取消")

    def open_session(self, scr, e):
        """Inside tmux: switch to the agent's tmux session. Otherwise: attach with AoE, and come back on detach."""
        ch, L = self.ch, self.ch.L
        target = ch.tmux_target(e["sid"]) if os.environ.get("TMUX") else None
        if target:
            subprocess.call(["tmux", "switch-client", "-t", target])
            self.st["msg"] = L("switched to {}; tmux prefix + L comes back here", "已切到 {}；tmux 前缀键 + L 回到这里").format(target)
            return
        curses.def_prog_mode()
        curses.endwin()
        subprocess.call([ch.aoe_bin(), "-p", self.cfg["profile"], "session", "attach", e["sid"]])
        curses.reset_prog_mode()
        scr.clear()
        self.st["msg"] = L("back from {}", "已从 {} 返回").format(e["title"])

    def loop(self, scr):
        styles = setup_colors()
        curses.curs_set(0)
        scr.timeout(250)
        self.refresh()
        while True:
            now = time.time()
            if now - self.last_refresh > self.every and not self.st["busy"] and not self.st["input"] and not self.st["confirm"]:
                self.refresh()
            self.st.update(now=now, clock=datetime.now().strftime("%H:%M"), tick_note=self.tick_note())
            h, w = scr.getmaxyx()
            draw(scr, render(self.ch, self.st, w, h), styles, w, h)
            try:
                k = scr.get_wch()
            except curses.error:
                continue
            if k == curses.KEY_RESIZE:
                continue
            k = curses.keyname(k).decode() if isinstance(k, int) else k
            if self.st["input"] is not None:
                self.typed(k)
            elif self.st["confirm"]:
                self.answered(k)
            else:
                r = self.key(k)
                if r is False:
                    return 0
                if r == "open":
                    self.open_session(scr, self.selected())

def setup_colors():
    s = {"text": curses.A_NORMAL, "title": curses.A_BOLD, "dim": curses.A_DIM, "info": curses.A_DIM,
         "header": curses.A_REVERSE | curses.A_BOLD, "footer": curses.A_REVERSE, "sel": curses.A_REVERSE,
         "prompt": curses.A_BOLD, "msg": curses.A_NORMAL, "head": curses.A_BOLD, "grey": curses.A_DIM}
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        for i, (name, fg) in enumerate((("red", curses.COLOR_RED), ("draft", curses.COLOR_YELLOW),
                                        ("green", curses.COLOR_GREEN), ("yellow", curses.COLOR_CYAN),
                                        ("prompt", curses.COLOR_YELLOW), ("head", curses.COLOR_BLUE)), 1):
            curses.init_pair(i, fg, bg)
            s[name] = curses.color_pair(i) | (curses.A_BOLD if name in ("red", "prompt", "head") else 0)
    else:
        s.update(red=curses.A_BOLD, draft=curses.A_BOLD, green=curses.A_BOLD, yellow=curses.A_NORMAL)
    return s

def draw(scr, rows, styles, w, h):
    scr.erase()
    for y, segs in enumerate(rows[:h]):
        x = 0
        for text, style in segs:
            if x >= w:
                break
            try:
                scr.addstr(y, x, text, styles.get(style, curses.A_NORMAL))
            except curses.error:             # writing the bottom-right cell raises; the text is still drawn
                pass
            x += width(text)
    scr.refresh()

def run(cfg, ch, every=15):
    if not sys.stdout.isatty():
        raise RuntimeError(ch.L("the inbox needs a terminal", "收件箱需要在终端里运行"))
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")      # Esc cancels input without the default one-second wait
    return curses.wrapper(Inbox(cfg, ch, every).loop)

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import chief
    sys.exit(run(chief.config(), chief, int(sys.argv[1]) if len(sys.argv) > 1 else 15))
