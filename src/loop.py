"""The loop the machine runs itself on any habit `habits` finds:

1. *read*: each message the user types is read once the turn that answers it has ended (`learn`, a
   Stop hook Claude Code runs in the background), and a signal, an objection or an instruction given
   in advance, is put with a known habit or opens one;
2. *propose*: on a habit's 2nd, 4th, 8th … signal the session is woken once to put a rule for it to
   the user; only the user adds one (`aidetic rule ID accept`), and every session is handed it then;
3. *watch*: a habit with a rule has a judge at its point of action that records a verdict on the
   first such act of each turn and decides nothing (`watch`, a PreToolUse hook installed only while a
   habit is watched there, and `learn` for a turn's reply);
4. *guard*: the guard switches itself on when its verdicts, set against the user's own objections,
   clear the bars below, and off when the user corrects it or they stop clearing them; on, it denies
   the act or sends the reply back, naming a gate event that `aidetic correct` takes.

The state is ~/.claude/aidetic/habits/live.json, under a lock of its own; the rules the user kept are
~/.claude/aidetic/rules.md, which the `start` and `prompt` hooks hand to sessions.

  rule [list] | rule ID accept [--text TEXT] | rule ID decline | rule ID drop
           what Claude runs on the user's answer to a proposal, or when they drop a rule
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import habits
import judge
import ledger
from evalset import append, load, option
from transcripts import read_session

LIVE = habits.DIR / "live.json"

# The guard's bars, fixed before any habit is watched.
MIN_TURNS, MIN_OBJECTED = 40, 3   # watched turns, and turns the user objected to among them, before it switches on
CATCH = 2 / 3                     # the share of the objected turns its verdicts must have flagged
MARGIN = 2                        # its wrong flags must stay under the objection share × its catch, halved
CORRECTED = 2                     # corrections of its decisions, among the latest WINDOW, that switch it off
WINDOW = 40
FIRST_LOOK = 600   # seconds back a session's first reading goes: a message typed while the session worked
                   # reaches its turn only when the turn before has ended
MATCHERS = {"change": "Edit|Write|NotebookEdit|MultiEdit|Bash", "message": "mcp__.*|Bash", "command": "Bash",
            "question": "AskUserQuestion", "other": ".*"}   # where `watch` runs for each point of action

PROPOSE = ("The user has signalled this habit {n} times: {name}. In their words: {words}. Ask them now, "
           "through AskUserQuestion, whether every new session should keep this rule: “{rule}”. Offer to "
           "keep it as written and not now, with Other for their own wording; then run `aidetic rule {k} "
           "accept` (adding --text \"their wording\" if they wrote one) or `aidetic rule {k} decline`. If "
           "they ask what this is: aidetic proposes a rule on a habit's 2nd, 4th, 8th … signal and adds "
           "one only when they accept.")
SWITCHED = "Tell the user in one line, and nothing more: aidetic switched {state} its guard for “{name}” ({why})."


def state():
    return ledger.read_state(LIVE)


def update(fn):
    return ledger.with_state(fn, LIVE)


def config():
    return judge.configured("learn")


def hid(name):
    return "h" + hashlib.sha1(name.strip().lower().encode()).hexdigest()[:5]


def blank(name, act):
    return dict(name=name, act=act, signals=[], proposed=[], answers=[], rule=None, draft=None, guard="off")


def seed(groups, found):
    """The habits of a first `aidetic habits` table, put in the state when it holds none; later
    signals come one by one through assign(). Returns whether it seeded."""
    of = {r["id"]: r for r in found}

    def put(st):
        if st.get("habits"):
            return False
        st["habits"] = {}
        for g in groups:
            h = st["habits"].setdefault(hid(g["name"]), blank(g["name"], g["act"]))
            h["signals"] += [[i, of[i]["kind"], of[i]["at"], of[i].get("sid", "")] for i in g["ids"] if i in of]
        return True
    return update(put)


def groups():
    """The state's habits in the form habits.price() reads."""
    return [dict(name=h["name"], act=h["act"], ids=[s[0] for s in h["signals"]])
            for h in state().get("habits", {}).values()]


def assign(signals, cfg):
    """Put each new signal with a known habit, or open one for it; returns {signal id: habit id}."""
    hs = state().get("habits", {})
    have = {s[0] for h in hs.values() for s in h["signals"]}
    signals = [r for r in signals if r["id"] not in have]
    if not signals:
        return {}
    by = {}
    if hs:
        known = "\n".join(f"{k} | {h['name']}" for k, h in hs.items())
        new = "\n".join(" | ".join(str(r[x]) for x in ("id", "kind", "act", "habit", "quote")) for r in signals)
        reading, _ = judge.draw("match", dict(input=dict(text=f"KNOWN HABITS:\n{known}\n\nNEW ITEMS:\n{new}")),
                                {**cfg, "timeout": max(cfg.get("timeout", 60), 300)})
        by = {x["id"]: x for x in reading["items"]}
    placed = {}

    def put(st):
        hs = st.setdefault("habits", {})
        for r in signals:
            x = by.get(r["id"], {})
            k = x.get("habit") if x.get("habit") in hs else None
            if k is None:
                name = (x.get("name") or r["habit"] or "unnamed").strip()
                k = hid(name)
                hs.setdefault(k, blank(name, r["act"]))
            if r["id"] not in {s[0] for s in hs[k]["signals"]}:
                hs[k]["signals"].append([r["id"], r["kind"], r["at"], r.get("sid", "")])
            placed[r["id"]] = k
    update(put)
    return placed


def due(h):
    """A proposal is due on the 2nd, 4th, 8th … signal: the next comes at the first power of two at
    least twice the count of the last. A habit seeded past its 2nd without one has it at its next
    signal (at 31 seeded, the 32nd; then the 64th)."""
    n = len(h["signals"])
    if h.get("rule") or n < 2:
        return False
    if not h.get("proposed"):
        return True
    return n >= 1 << (2 * max(h["proposed"]) - 1).bit_length()


def draft(h, cfg):
    """A rule for the habit from the user's own words, and those words."""
    of = {r["id"]: r for r in load(habits.READINGS)}
    words = [of[s[0]]["quote"] for s in h["signals"] if s[0] in of and of[s[0]]["quote"]][-12:]
    text = f"THE BEHAVIOUR: {h['name']} (where: {h['act']})\nTHE USER'S WORDS:\n" + "\n".join(f"- {w}" for w in words)
    reading, _ = judge.draw("draft", dict(input=dict(text=text)), cfg)
    return " ".join(reading["rule"].split())[:400], words


def watching(act):
    return [(k, h) for k, h in state().get("habits", {}).items()
            if h.get("rule") and h.get("guard") in ("watch", "on") and h["act"] == act]


def verdict(rule, act, said, cfg):
    text = f"THE RULE: {rule}\n\nTHE USER'S LAST MESSAGE: {said[-1500:]}\n\nTHE ACT: {act[:3000]}"
    reading, meta = judge.draw("watch", dict(input=dict(text=text)), cfg)
    return reading, meta


def last_typed(path):
    try:
        return next((x for t, who, x in reversed(read_session(path)) if who == "user"), "")
    except OSError:
        return ""


def eid(*parts):
    return hashlib.sha1("|".join(map(str, parts + (time.time(),))).encode()).hexdigest()[:6]


# ---- step 1, 2 and the reply's watch: at the end of a turn ----------------------------------------

def learn(p):
    """The Stop hook, run in the background: read the turn's new messages, watch or guard its reply,
    switch guards, propose a rule when one is due. Returns the exit code: 2 wakes the session with
    what this writes to stderr."""
    sid, path = p.get("session_id", ""), p.get("transcript_path") or ""
    if not sid or not os.path.exists(path) or p.get("stop_hook_active") or (p.get("cwd") or "").startswith("/tmp/"):
        return 0
    cfg = config()
    since = state().get("read", {}).get(sid)
    first = since is None   # a session first seen: its latest messages only; `aidetic habits` reads the past
    msgs = habits.session(path, time.time() - FIRST_LOOK if first else since)[-3 if first else 0:]
    placed, notes = {}, []
    if msgs:
        # `aidetic habits`, run while this turn worked, may have read and placed its messages already
        v = judge.version("correction")
        done = {r["id"]: r for r in load(habits.READINGS) if r.get("v") == v}
        todo, rows = [m for m in msgs if m["id"] not in done], [done[m["id"]] for m in msgs if m["id"] in done]
        new = []
        for i in range(0, len(todo), habits.BATCH):
            new += habits.read(todo[i:i + habits.BATCH], cfg)
        habits.DIR.mkdir(parents=True, exist_ok=True)
        append(habits.READINGS, new)
        update(lambda st: st.setdefault("read", {}).__setitem__(sid, max(m["t"] for m in msgs)))
        signals = [r for r in rows + new if r["kind"] != "none"]
        placed = assign(signals, cfg) if signals else {}   # one placed already waits for its habit's next
    said = msgs[-1]["text"] if msgs else last_typed(path)
    text = p.get("last_assistant_message") or ""
    for k, h in watching("reply") if text.strip() else []:
        reading, meta = verdict(h["rule"], text, said, cfg)
        ledger.emit(kind="watched", phase="loop", habit=k, sid=sid, turn=ledger.read_state().get("turns", {}).get(sid),
                    breaks=reading["breaks"], why=reading["why"][:200], guard=h["guard"], secs=meta["secs"])
        if h["guard"] == "on" and reading["breaks"]:
            e = eid(sid, k)
            ledger.emit(kind="guard_sent_back", phase="loop", habit=k, sid=sid, id=e)
            notes.append(f"Your last reply breaks a rule the user keeps: “{h['rule']}” ({reading['why']}). "
                         f"Put it right now (gate event {e}).")
    notes += settle()
    for k in dict.fromkeys(placed.values()):
        h = state()["habits"][k]
        if due(h):
            rule, words = draft(h, cfg)
            n = len(h["signals"])

            def mark(st, k=k, n=n, rule=rule):
                st["habits"][k]["proposed"].append(n)
                st["habits"][k]["draft"] = rule
            update(mark)
            ledger.emit(kind="rule_proposed", phase="loop", habit=k, sid=sid, signals=n)
            notes.append(PROPOSE.format(n=n, name=h["name"], k=k, rule=rule,
                                        words="; ".join(f"“{w[:160]}”" for w in words[-4:])))
            break   # one proposal at a time
    if not notes:
        return 0
    print("\n\n".join(notes), file=sys.stderr)
    return 2


# ---- step 3 and 4 at a tool call ---------------------------------------------------------------

def watch(p):
    """The PreToolUse hook of watched habits: record a verdict on the first act of their kind in each
    turn, in a detached process; with the guard on, decide it now and deny what breaks the rule."""
    use = dict(name=p.get("tool_name", ""), input=p.get("tool_input") or {})
    hs = watching(habits.kind(use))
    if not hs:
        return 0
    sid = p.get("session_id", "")
    turn = ledger.read_state().get("turns", {}).get(sid)
    act = f"{use['name']}: {json.dumps(use['input'], ensure_ascii=False)}"
    for k, h in hs:
        if h["guard"] == "on":
            reading, meta = verdict(h["rule"], act, last_typed(p.get("transcript_path") or ""), config())
            ledger.emit(kind="watched", phase="loop", habit=k, sid=sid, turn=turn, breaks=reading["breaks"],
                        why=reading["why"][:200], guard="on", secs=meta["secs"])
            if reading["breaks"]:
                e = eid(sid, k)
                ledger.emit(kind="guard_denied", phase="loop", habit=k, sid=sid, id=e)
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": f"This breaks a rule the user keeps: “{h['rule']}” "
                                                f"({reading['why']}). (gate event {e})"}}))
                return 0
            continue

        def claim(st, k=k):
            seen = st.setdefault("watched", {})
            if seen.get(f"{k}|{sid}") == turn:
                return False
            seen[f"{k}|{sid}"] = turn
            for old in list(seen)[:-2000]:
                del seen[old]
            return True
        if turn is not None and update(claim):
            spawn(dict(habit=k, sid=sid, turn=turn, act=act, transcript=p.get("transcript_path") or ""))
    return 0


def spawn(job):
    """watched() in a detached process, so the act waits for nothing."""
    try:
        habits.DIR.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="watch-", suffix=".json", dir=habits.DIR)
        with os.fdopen(fd, "w") as f:
            json.dump(job, f)
        gate = Path(__file__).resolve().parent / "choice_gate.py"
        subprocess.Popen([sys.executable, str(gate), "watched", path], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def watched(path):
    job = json.loads(Path(path).read_text())
    Path(path).unlink()
    h = state().get("habits", {}).get(job["habit"])
    if not h or not h.get("rule"):
        return
    common = dict(phase="loop", habit=job["habit"], sid=job["sid"], turn=job["turn"], guard="watch")
    try:
        reading, meta = verdict(h["rule"], job["act"], last_typed(job["transcript"]), config())
        ledger.emit(kind="watched", breaks=reading["breaks"], why=reading["why"][:200], secs=meta["secs"], **common)
    except Exception as e:
        ledger.emit(kind="judge_failed", error=str(e)[:300], **common)


# ---- step 4: the switch ---------------------------------------------------------------------------

def score(k, h, events=None):
    """What the habit's watched turns say against the user's objections: each objection marks the
    latest watched turn of its session before it."""
    events = ledger.load() if events is None else events
    flags = {}
    for e in events:
        if e.get("kind") == "watched" and e.get("habit") == k and e.get("turn") is not None:
            key = (e["sid"], e["turn"])
            flags[key] = flags.get(key, False) or bool(e.get("breaks"))
    objected = set()
    for _, kind, at, sid in h["signals"]:
        if kind == "objection" and sid:
            before = [key for key in flags if key[0] == sid and key[1] < at]
            if before:
                objected.add(max(before, key=lambda key: key[1]))
    n, pos = len(flags), len(objected)
    caught = sum(flags[key] for key in objected)
    wrong = sum(v for key, v in flags.items() if key not in objected)
    share, catch = (pos / n if n else 0.0), (caught / pos if pos else 0.0)
    wrong_rate = wrong / (n - pos) if n > pos else 0.0
    pays = n >= MIN_TURNS and pos >= MIN_OBJECTED and catch >= CATCH and wrong_rate <= share * catch / MARGIN
    corrected = 0
    decided = [e["id"] for e in events if e.get("kind") in ("guard_denied", "guard_sent_back") and e.get("habit") == k]
    if decided:
        latest = set(decided[-WINDOW:])
        corrected = sum(1 for e in events if e.get("kind") == "correction" and e.get("of") in latest)
    return dict(turns=n, objected=pos, catch=catch, wrong=wrong_rate, share=share, pays=pays, corrected=corrected)


def settle():
    """Switch each watched habit's guard by its score; returns a note for the user for each switch."""
    notes = []
    events = ledger.load()
    for k, h in state().get("habits", {}).items():
        if not h.get("rule") or h.get("guard") not in ("watch", "on"):
            continue
        s = score(k, h, events)
        if h["guard"] == "watch" and s["pays"]:
            to, why = "on", f"it flagged {s['catch']:.0%} of the turns you objected to and {s['wrong']:.1%} of the others"
        elif h["guard"] == "on" and (s["corrected"] >= CORRECTED or not s["pays"]):
            to, why = "watch", (f"you corrected it {s['corrected']} times" if s["corrected"] >= CORRECTED
                                else "its verdicts no longer clear the bars")
        else:
            continue
        update(lambda st, k=k, to=to: st["habits"][k].__setitem__("guard", to))
        ledger.emit(kind=f"guard_{'on' if to == 'on' else 'off'}", phase="loop", habit=k, **s)
        notes.append(SWITCHED.format(state="on" if to == "on" else "off", name=h["name"], why=why))
    return notes


# ---- the user's answer -------------------------------------------------------------------------

def write_rules():
    """rules.md from the rules the user kept; gone when there are none."""
    kept = [h for h in state().get("habits", {}).values() if h.get("rule")]
    if not kept:
        ledger.KEPT.unlink(missing_ok=True)
        return
    ledger.KEPT.write_text("# Rules you kept\n\nEach was put to you by aidetic on a habit it found in your "
                           "corrections, and added when you accepted it.\n\n"
                           + "".join(f"- {h['rule']}\n" for h in kept))


def rule(args):
    hs = state().get("habits", {})
    if not args or args[0] == "list":
        for k, h in sorted(hs.items(), key=lambda kv: -len(kv[1]["signals"])):
            mark = f"rule, guard {h['guard']}: {h['rule']}" if h.get("rule") else (
                f"proposed at {', '.join(map(str, h['proposed']))}" if h.get("proposed") else "")
            print(f"{k}  {len(h['signals']):>3} signals  {h['name'][:60]:60s}  {mark}")
        return
    k, verb = args[0], (args[1] if len(args) > 1 else "")
    if k not in hs or verb not in ("accept", "decline", "drop"):
        sys.exit("aidetic rule ID accept [--text TEXT] | decline | drop   (IDs: `aidetic rule list`)")
    text = " ".join((option(args, "--text", "") or hs[k].get("draft") or "").split())
    if verb == "accept" and not text:
        sys.exit("aidetic rule: no rule drafted for this habit yet; give it with --text")

    def put(st):
        h = st["habits"][k]
        h["answers"].append([len(h["signals"]), verb, ledger.now()])
        if verb == "accept":
            h["rule"], h["guard"] = text, "watch"
        elif verb == "drop":
            h["rule"], h["guard"] = None, "off"
    update(put)
    write_rules()
    import choice_gate
    choice_gate.write_watch(sorted({h["act"] for h in state()["habits"].values()
                                    if h.get("rule") and h["act"] in MATCHERS}))
    ledger.emit(kind=f"rule_{verb}", phase="loop", habit=k, rule=text if verb == "accept" else None)
    print({"accept": f"kept: {text}", "decline": "declined; asked again at its next doubling",
           "drop": "dropped"}[verb])
