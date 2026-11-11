"""The loop the machine runs itself on any habit `habits` finds:

1. *read*: each message the user types is read once the turn that answers it has ended (`learn`, a
   Stop hook Claude Code runs in the background), and a signal, an objection or an instruction given
   in advance, is put with a known habit or opens one;
2. *propose*: on a habit's 2nd, 4th, 8th … signal the session is woken once to put a rule for it to
   the user, unless the draft says the habit is about the work's subject (what a bot should do in its
   game) rather than how the assistant works; only the user adds one (`aidetic rule ID accept`), for
   every project or only the one the session runs in (scope.py), and the sessions it applies to are
   handed it then; the user can also write one of their own (`aidetic rule add`);
3. *watch*: at the end of each turn one judge call reads the whole turn, every tool call and the
   reply, against every proposed rule kept for that project, with all the user wrote in the session,
   and records a verdict on each, deciding nothing (`learn`); a rule the user wrote themselves is not
   watched;
4. *guard*: the guard switches itself on when its verdicts, set against the user's own objections,
   clear the bars below, and off when the user corrects it or they stop clearing them; on, it sends
   the turn back, naming a gate event that `aidetic correct` takes.

The state is ~/.claude/aidetic/habits/live.json, under a lock of its own; the `start` and `prompt`
hooks hand each session the kept rules that apply to its project (kept()), and ~/.claude/aidetic/rules.md
lists them all for the user.

  rule [list] | rule ID accept [--text TEXT] [--here | --project PATH] | rule ID decline | rule ID never
       | rule ID drop | rule ID scope all|here|PATH | rule add TEXT [--here | --project PATH]
           what Claude runs on the user's answer to a proposal, or when they write, drop or move a rule
"""
import functools
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import habits
import judge
import ledger
import scope
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
OWN_TURN = 2       # seconds: a turn begun this close to an objection is the one the objection began, not the one it answers
SAID, DID, ONE = 12000, 9000, 1500   # characters the turn judge reads of what the user wrote, of the turn's
                                     # tool calls, and of any one message or call
TURN_SECS = 180    # the least time the turn judge is given: it reads every rule against the whole turn

PROPOSE = ("The user has signalled this habit {n} times: {name}. In their words: {words}. {origins}Ask them now, "
           "through AskUserQuestion, whether new sessions should keep this rule: “{rule}”. Offer to keep it as "
           "written in every project, to keep it only in {here}, not now, and never ask again for this habit, "
           "with Other for their own wording; then run `aidetic rule {k} accept` (adding --project '{cwd}' to "
           "keep it only in {here}, and --text \"their wording\" if they wrote one), `aidetic rule {k} decline` "
           "or `aidetic rule {k} never`. If they ask what this is: aidetic proposes a rule on a habit's 2nd, "
           "4th, 8th … signal and adds one only when they accept; `aidetic rule {k} scope` changes where it "
           "applies later.")
SWITCHED = "Tell the user in one line, and nothing more: aidetic switched {state} its guard for “{name}” ({why})."
HEAD = ("# Rules you kept\n\nEach was put to you by aidetic on a habit it found in your corrections and added when "
        "you accepted it, or written by you.\n\n")
USAGE = ("aidetic rule ID accept [--text TEXT] [--here | --project PATH] | decline | never | drop | scope all|here|PATH\n"
         "aidetic rule add TEXT [--here | --project PATH]      (IDs: `aidetic rule list`)")


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


def assign(signals, cfg, project=None):
    """Put each new signal with a known habit, or open one for it; returns {signal id: habit id}. A
    signal is [reading id, kind, time, session, project key]; the project when the caller knows it."""
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
                hs[k]["signals"].append([r["id"], r["kind"], r["at"], r.get("sid", ""), project])
            placed[r["id"]] = k
    update(put)
    return placed


def due(h):
    """A proposal is due on the 2nd, 4th, 8th … signal: the next comes at the first power of two at
    least twice the count of the last. A habit seeded past its 2nd without one has it at its next
    signal (at 31 seeded, the 32nd; then the 64th). A draft skipped as about the subject counts as
    a proposal for the schedule. Never for a habit the user said never to ask about again."""
    n = len(h["signals"])
    if h.get("rule") or h.get("never") or n < 2:
        return False
    asked = h.get("proposed", []) + h.get("skipped", [])
    if not asked:
        return True
    return n >= 1 << (2 * max(asked) - 1).bit_length()


def draft(h, cfg):
    """A rule for the habit from the user's own words, those words, and whether the habit is about
    the work's subject rather than how the assistant works."""
    of = {r["id"]: r for r in load(habits.READINGS)}
    words = [of[s[0]]["quote"] for s in h["signals"] if s[0] in of and of[s[0]]["quote"]][-12:]
    text = f"THE BEHAVIOUR: {h['name']} (where: {h['act']})\nTHE USER'S WORDS:\n" + "\n".join(f"- {w}" for w in words)
    reading, _ = judge.draw("draft", dict(input=dict(text=text)), cfg)
    return " ".join(reading["rule"].split())[:400], words, bool(reading.get("subject"))


@functools.lru_cache(maxsize=None)
def folder_project(folder):
    """The project of a folder of transcripts, which Claude Code keeps one of per working folder: the
    working folder its latest transcripts record, as a project key; None once none is left. For the
    signals read before signals carried their project."""
    for f in sorted((ledger.PROJECTS / folder).glob("*.jsonl"), key=os.path.getmtime, reverse=True)[:5]:
        try:
            with open(f, errors="ignore") as lines:
                for line in lines:
                    if '"cwd"' in line:   # most lines carry none: parse only those that can
                        cwd = json.loads(line).get("cwd")
                        if cwd:
                            return scope.project(cwd)
        except (OSError, ValueError):
            pass
    return None


def origins(h):
    """How many of the habit's signals came from each project, by name, most first; a signal whose
    project can no longer be known is left out."""
    of = {r["id"]: r.get("project") for r in load(habits.READINGS)}
    keys = [(s[4] if len(s) > 4 and s[4] else folder_project(of[s[0]]) if of.get(s[0]) else None) for s in h["signals"]]
    return Counter(scope.name(k) for k in keys if k).most_common()


# ---- what the rules are, and where ----------------------------------------------------------------

def write_rules():
    """rules.md: every rule the user kept, with where it applies when that is not every project;
    gone when there are none. For the user to read: the hooks hand sessions kept() instead."""
    rules = [h for h in state().get("habits", {}).values() if h.get("rule")]
    if not rules:
        ledger.KEPT.unlink(missing_ok=True)
        return
    ledger.KEPT.write_text(HEAD + "".join(f"- {h['rule']}" + (f" ({scope.where(h)})" if h.get("scope") else "") + "\n"
                                          for h in rules))


def kept(project):
    """What a session in this project (its key) is handed: the kept rules that apply there; empty
    when none do."""
    rules = [h["rule"] for h in state().get("habits", {}).values() if scope.applies(h, project)]
    return HEAD + "".join(f"- {r}\n" for r in rules) if rules else ""


def watching(project):
    """The rules watched in this project: proposed ones kept for it, the user's own left out."""
    return [(k, h) for k, h in state().get("habits", {}).items()
            if scope.applies(h, project) and h.get("guard") in ("watch", "on")]


# ---- step 3: the turn's verdict -------------------------------------------------------------------

def turn(path):
    """What the turn judge reads of a transcript: (what the user wrote before the turn, the turn).
    The first is their messages and their answers to questions, oldest first, as many of the latest
    as fit; the second runs from their last message: every tool call and any answer given within it,
    in order, and when the calls do not all fit, those that change, send or ask go in first."""
    entries = read_session(path, answers=True, acts=True)
    typed = lambda who, x: who == "user" and x.strip() and not x.strip().startswith(habits.NOT_TYPED)
    start = max((i + 1 for i, (_, who, x) in enumerate(entries) if typed(who, x)), default=0)

    def answered(x):
        notes = x.get("annotations") or {}
        return [f"THE USER ANSWERED “{q[:300]}”: {a}"
                + (f" (their note: {notes[q]['notes']})" if (notes.get(q) or {}).get("notes") else "")
                for q, a in (x.get("answers") or {}).items()]

    said = []
    for _, who, x in entries[:start]:
        if typed(who, x):
            said.append(f"THE USER WROTE: {x.strip()}"[:ONE])
        elif who == "answer":
            said += [a[:ONE] for a in answered(x)]
    shown, size = [], 0
    for line in reversed(said):
        if size + len(line) > SAID:
            shown.append("(their earlier messages are not shown)")
            break
        shown.append(line)
        size += len(line)
    calls = []   # (changes, sends or asks; the line)
    for _, who, x in entries[start:]:
        if who in ("act", "ask"):
            use = dict(name=x.get("name", ""), input=x.get("input") or {})
            weighs = who == "ask" or habits.kind(use) in ("change", "message")
            calls.append((weighs, f"{use['name']}: {json.dumps(use['input'], ensure_ascii=False)}"[:ONE]))
        elif who == "answer":
            calls += [(True, a[:ONE]) for a in answered(x)]
    keep, size = set(), 0
    for first in (True, False):
        for i, (weighs, line) in enumerate(calls):
            if weighs == first and size + len(line) <= DID:
                keep.add(i)
                size += len(line)
    did = [line for i, (_, line) in enumerate(calls) if i in keep]
    if len(keep) < len(calls):
        did.append(f"({len(calls) - len(keep)} more tool calls are not shown)")
    return "\n".join(reversed(shown)), "\n".join(did)


def watch(p, project, cfg):
    """One judge call on the turn that just ended, for every rule watched in its project, recorded as
    one `watched_turn` event; returns a note sending the turn back for each guard on that it breaks."""
    sid, path = p["session_id"], p["transcript_path"]
    hs = dict(watching(project))
    began = ledger.read_state().get("turns", {}).get(sid)
    if not hs or began is None:
        return []
    before, did = turn(path)
    reply = (p.get("last_assistant_message") or "").strip()
    if not did and not reply:
        return []
    text = ("THE RULES:\n" + "\n".join(f"[{k}] {h['rule']}" for k, h in hs.items())
            + f"\n\nWHAT THE USER WROTE BEFORE THE TURN, oldest first:\n{before or '(nothing)'}"
            + f"\n\nTHE TURN'S TOOL CALLS, in order:\n{did or '(none)'}"
            + f"\n\nTHE REPLY THAT ENDED IT:\n{reply[-3000:] or '(none)'}")
    common = dict(phase="loop", sid=sid, turn=began)
    try:
        reading, meta = judge.draw("turn", dict(input=dict(text=text)), {**cfg, "timeout": max(cfg.get("timeout", 60), TURN_SECS)})
    except Exception as e:
        ledger.emit(kind="judge_failed", task="turn", error=str(e)[:300], **common)
        return []
    verdicts = {r["id"]: [bool(r["breaks"]), r["why"][:200]] for r in reading["rules"] if r["id"] in hs}
    ledger.emit(kind="watched_turn", verdicts=verdicts, guards={k: hs[k]["guard"] for k in verdicts},
                secs=meta["secs"], cost=meta.get("cost"), **common)
    notes = []
    for k, (breaks, why) in verdicts.items():
        if breaks and hs[k]["guard"] == "on":
            e = eid(sid, k)
            ledger.emit(kind="guard_sent_back", phase="loop", habit=k, sid=sid, id=e)
            notes.append(f"Your last turn breaks a rule the user keeps: “{hs[k]['rule']}” ({why}). "
                         f"Put it right now (gate event {e}).")
    return notes


def eid(*parts):
    return hashlib.sha1("|".join(map(str, parts + (time.time(),))).encode()).hexdigest()[:6]


# ---- step 1, 2, 3 and 4: at the end of a turn ------------------------------------------------------

def learn(p):
    """The Stop hook, run in the background: read the turn's new messages, judge the turn, switch
    guards, propose a rule when one is due. Returns the exit code: 2 wakes the session with what
    this writes to stderr."""
    sid, path, cwd = p.get("session_id", ""), p.get("transcript_path") or "", p.get("cwd") or ""
    if not sid or not os.path.exists(path) or p.get("stop_hook_active") or cwd.startswith("/tmp/"):
        return 0
    cfg, project = config(), scope.project(cwd)
    since = state().get("read", {}).get(sid)
    first = since is None   # a session first seen: its latest messages only; `aidetic habits` reads the past
    msgs = habits.session(path, time.time() - FIRST_LOOK if first else since)[-3 if first else 0:]
    placed = {}
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
        placed = assign(signals, cfg, project) if signals else {}   # one placed already waits for its habit's next
    notes = watch(p, project, cfg)
    notes += settle()
    for k in dict.fromkeys(placed.values()):
        h = state()["habits"][k]
        if due(h):
            rule, words, subject = draft(h, cfg)
            n = len(h["signals"])
            if subject:   # not the user's habit with the assistant: not put to them, nothing said
                update(lambda st, k=k, n=n: st["habits"][k].setdefault("skipped", []).append(n))
                ledger.emit(kind="rule_skipped", phase="loop", habit=k, sid=sid, signals=n, why="subject")
                continue

            def mark(st, k=k, n=n, rule=rule):
                st["habits"][k]["proposed"].append(n)
                st["habits"][k]["draft"] = rule
            update(mark)
            ledger.emit(kind="rule_proposed", phase="loop", habit=k, sid=sid, signals=n)
            came = origins(h)
            notes.append(PROPOSE.format(
                n=n, name=h["name"], k=k, rule=rule, here=scope.name(project) if project else "this project", cwd=cwd,
                words="; ".join(f"“{w[:160]}”" for w in words[-4:]),
                origins=("Its signals came from: " + ", ".join(f"{p} {c}" for p, c in came) + ". ") if came else ""))
            break   # one proposal at a time
    if not notes:
        return 0
    print("\n\n".join(notes), file=sys.stderr)
    return 2


# ---- step 4: the switch ---------------------------------------------------------------------------

def score(k, h, events=None):
    """What the habit's verdicts say against the user's objections. An objection marks the turn it
    answers: the latest turn of its session judged before it, the turn the objection itself began
    left out, counted when that turn has a verdict on this habit. Only the turn judge's verdicts
    count (`watched_turn`): the per-act verdicts that came before it saw one act and one message."""
    events = ledger.load() if events is None else events
    flags, judged = {}, {}
    for e in events:
        if e.get("kind") == "watched_turn" and e.get("turn") is not None:
            judged.setdefault(e["sid"], set()).add(e["turn"])
            if k in e.get("verdicts", {}):
                key = (e["sid"], e["turn"])
                flags[key] = flags.get(key, False) or bool(e["verdicts"][k][0])
    objected = set()
    for s in h["signals"]:
        kind, at, sid = s[1], s[2], s[3]
        before = [t for t in judged.get(sid, ()) if t < at - OWN_TURN] if kind == "objection" and sid else []
        if before and (sid, max(before)) in flags:
            objected.add((sid, max(before)))
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

def named(args):
    """The project --here (the folder the command runs in) or --project PATH names, as its key;
    None for neither."""
    if "--here" in args:
        return scope.project(os.getcwd())
    path = option(args, "--project", None)
    return None if path is None else folder(path)


def folder(path):
    path = Path(path).expanduser()
    if not path.is_dir():
        sys.exit(f"aidetic rule: {path} is not a folder")
    return scope.project(str(path))


def rule(args):
    hs = state().get("habits", {})
    if not args or args[0] == "list":
        for k, h in sorted(hs.items(), key=lambda kv: -len(kv[1]["signals"])):
            mark = (f"{'own rule' if h.get('own') else 'rule'} ({scope.where(h)}), guard {h['guard']}: {h['rule']}"
                    if h.get("rule") else "never asked again" if h.get("never") else " ".join(
                        ([f"proposed at {', '.join(map(str, h['proposed']))}"] if h.get("proposed") else [])
                        + ([f"skipped at {', '.join(map(str, h['skipped']))} (about the subject)"] if h.get("skipped") else [])))
            print(f"{k}  {len(h['signals']):>3} signals  {h['name'][:60]:60s}  {mark}")
        return
    if args[0] == "add":
        return add(args[1:])
    k, verb = args[0], (args[1] if len(args) > 1 else "")
    if k not in hs or verb not in ("accept", "decline", "never", "drop", "scope"):
        sys.exit(USAGE)
    if verb == "scope":
        return rescope(k, args[2:])
    text = " ".join((option(args, "--text", "") or hs[k].get("draft") or "").split())
    if verb == "accept" and not text:
        sys.exit("aidetic rule: no rule drafted for this habit yet; give it with --text")
    project = named(args) if verb == "accept" else None

    def put(st):
        h = st["habits"][k]
        h["answers"].append([len(h["signals"]), verb, ledger.now()])
        if verb == "accept":
            h["rule"], h["guard"], h["never"], h["scope"] = text, "watch", False, [project] if project else None
        elif verb == "never":
            h["never"] = True
        elif verb == "drop" and h.get("own") and not h["signals"]:
            del st["habits"][k]   # nothing but the rule the user wrote: nothing left to keep
        elif verb == "drop":
            h["rule"], h["guard"], h["scope"] = None, "off", None
    update(put)
    write_rules()
    ledger.emit(kind=f"rule_{verb}", phase="loop", habit=k, rule=text if verb == "accept" else None,
                scope=[project] if project else None)
    where = scope.where(dict(rule=text, scope=[project] if project else None))
    print({"accept": f"kept ({where}): {text}", "decline": "declined; asked again at its next doubling",
           "never": "never asked again; `aidetic rule ID accept --text …` still keeps one", "drop": "dropped"}[verb])


def rescope(k, args):
    """`scope all` puts the rule in every project; `scope here` or `scope PATH` adds that project to a
    rule kept for some, or limits one kept for every project to it."""
    if not args:
        sys.exit(USAGE)
    project = None if args[0] == "all" else folder(os.getcwd() if args[0] == "here" else args[0])

    def put(st):
        h = st["habits"][k]
        if not h.get("rule"):
            return False, None
        h["scope"] = None if project is None else list(dict.fromkeys((h.get("scope") or []) + [project]))
        return True, h["scope"]
    kept_one, put_in = update(put)
    if not kept_one:
        sys.exit("aidetic rule: no rule is kept for this habit; accept one first")
    write_rules()
    ledger.emit(kind="rule_scope", phase="loop", habit=k, scope=put_in)
    print(f"applies {scope.where(dict(scope=put_in))}")


def add(args):
    """A rule the user writes themselves: kept at once, for every project or the one named, and not
    watched, since no habit of theirs stands behind it to score a guard against."""
    project, words, skip = named(args), [], False
    for a in args:
        if skip or a == "--here":
            skip = False
            continue
        if a == "--project":
            skip = True
            continue
        words.append(a)
    text = " ".join(" ".join(words).split())[:400]
    if not text:
        sys.exit(USAGE)
    k = hid(text)

    def put(st):
        hs = st.setdefault("habits", {})
        if (hs.get(k) or {}).get("rule"):
            return False
        h = hs.setdefault(k, blank(text, "other"))
        h.update(rule=text, own=True, guard="off", never=False, scope=[project] if project else None)
        h["answers"].append([len(h["signals"]), "add", ledger.now()])
        return True
    if not update(put):
        sys.exit(f"aidetic rule: that rule is kept already, as {k}")
    write_rules()
    ledger.emit(kind="rule_add", phase="loop", habit=k, rule=text, scope=[project] if project else None)
    print(f"kept as {k} ({scope.where(dict(scope=[project] if project else None))}): {text}")
