"""What the transcripts show after the fact, each recorded once in the ledger.

Reversals: an answer its own session undid within four hours. The session corrected its own
recommendation, a later question asked to drop what was picked, or the user typed an undo of it.
An answer whose window has closed is not read again. While judge.json has the judge decide
*undone*, the judge reads each answer once its window closes instead, and an answer the evaluation
set has already labelled takes its label: the past recounted on the same instrument.

Recommendations: every turn that ended on one, through the tool (an option tagged "(Recommended)")
or in prose (the session's last text names its pick, and no question was asked). A transcript is
read again only when it has changed."""
import glob
import os
import re
import time
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor

import ledger
from questions import PICK, options, plain, short, unanswered
from transcripts import read_session

JUDGED = ("judge", "label")   # how a reversal found by the judge, or by its labels, is recorded

WINDOW = 4 * 3600   # seconds after the answer; later than that is a new decision
SELF = re.compile(r"correct my (own |earlier )?(recommendation|suggestion|advice)|my premise (for .{0,40})?"
                  r"(was|is) wrong|(?<!if )i was wrong|i got (that|this|it) wrong|i stand corrected", re.I)
UNDO = re.compile(r"\b(drop|revert|remove|undo|roll back|back out|scrap|rip out|get rid of)\b", re.I)
IDENT = re.compile(r"`([^`\n]{3,40})`|(?<![\w/-])([A-Za-z0-9]+(?:_[A-Za-z0-9]+)+|[a-z]+[A-Z]\w+|"
                   r"[a-z][a-z0-9]+-[a-z0-9][a-z0-9-]*|#\d{3,5}|[A-Z]{2,5}-\d+)(?![\w/-])")


def idents(text):
    """The names a text uses: `code`, snake_case, camelCase, kebab-case, #PR, TICKET-1."""
    return {(a or b).strip() for a, b in IDENT.findall(text or "") if len((a or b).strip()) >= 4}


# ---- reversals -----------------------------------------------------------------------------

def asked_to_drop(ask, ids):
    """A later question that names what was picked and offers to drop it."""
    for q in ask["input"].get("questions", []):
        text = q.get("question", "")
        labels = " ".join(o.get("label", "") for o in options(q))
        if ids & idents(text) and UNDO.search(f"{text} {labels}"):
            return short(f"{text} | {labels}", 200)
    return None


def reversals_in(events, answers):
    """Yield (answer id, how, evidence) for the answers of one session that were undone."""
    asks = {b["id"]: (t, b) for t, who, b in events if who == "ask"}
    picked = []
    for e in answers:
        if e.get("tuid") not in asks or unanswered(e):
            continue
        t0, ask = asks[e["tuid"]]
        q = next((q for q in ask["input"].get("questions", []) if short(q.get("question")) == e["q"]), {})
        o = next((o for o in options(q) if plain(o.get("label")) == plain(e["ans"])), None)
        picked.append((t0, e, idents(f"{o.get('label', '')} {o.get('description', '')}") if o else set()))
    picked.sort(key=lambda p: p[0])
    for i, (t0, e, ids) in enumerate(picked):
        # a self-correction belongs to the latest answer before it
        until_next = min([t0 + WINDOW] + [t for t, _, _ in picked[i + 1:] if t > t0])
        for t, who, x in events:
            if not t0 < t <= t0 + WINDOW:
                continue
            found = None
            if who == "self" and t <= until_next and SELF.search(x):
                found = "self-correction", short(x[max(0, SELF.search(x).start() - 60):], 200)
            elif who == "ask" and x["id"] != e["tuid"]:
                evidence = asked_to_drop(x, ids)
                found = evidence and ("asked again", evidence)
            elif who == "user" and ids & idents(x) and UNDO.search(x):
                found = "user undid", short(x, 200)
            if found:
                yield (e["id"], *found)
                break


def judge_reversals(events, files):
    """Each answer whose window has closed, read once: by its label in the evaluation set when it has
    one, else by the judge on the session's next four hours. Records `reversal` (how judge or label)
    for those undone; an answer whose reading fails is read again at the next scan."""
    import evalset   # imports this module: loaded here, not at the top
    import judge
    state = ledger.read_state()
    done = set(state.get("judged_undone", [])) | set(state.get("unreadable_undone", []))
    have = {e["id"] for e in events if e.get("kind") == "reversal" and e.get("how") in JUDGED}
    due = [e for e in events if e.get("kind") == "answer" and e["id"] not in done | have and not unanswered(e)
           and (ledger.stamp(e["t"]) or 0) < time.time() - WINDOW]
    if not due:
        return
    readings, decisions = evalset.labelled()
    known = {c["id"]: c for c in evalset.load(evalset.CASES) if c["task"] == "undone" and c["lang"] == "orig"}
    # the baseline's rows took their id from the question's first 160 characters (`short`), later rows
    # from its whole text: an answer finds its case either way
    alias = {ledger.event_id(c["src"]["tuid"], short(c["input"]["question"].get("question", ""))): cid
             for cid, c in known.items()}
    case_of = {e["id"]: ("u" + e["id"]) if ("u" + e["id"]) in known else alias.get(e["id"], "u" + e["id"]) for e in due}
    answer_of = {cid: eid for eid, cid in case_of.items()}
    finished, unread, ask = [], [], {}
    for e in due:
        label = evalset.final(case_of[e["id"]], readings, decisions)
        if label is not None:
            if label:
                ledger.emit(kind="reversal", id=e["id"], how="label", evidence="")
            finished.append(e["id"])
        elif e.get("sid") in files:
            ask.setdefault(e["sid"], []).append(e)
        else:
            unread.append(e["id"])   # its transcript is gone: not read, and out of every count
    cases = {}
    for sid, es in ask.items():
        try:
            session = read_session(files[sid], answers=True)
        except OSError:
            continue
        want = {case_of[e["id"]] for e in es}
        cases.update({c["id"]: c for c in evalset.undone_cases(sid, session, {}) if c["id"] in want})
    config = judge.configured("undone")

    def one(case):
        reading, meta = judge.draw("undone", case, config)
        return case, reading, meta
    with ThreadPoolExecutor(4) as pool:
        for fut in [pool.submit(one, c) for c in cases.values()]:
            try:
                case, reading, meta = fut.result()
            except Exception:
                continue
            eid = answer_of[case["id"]]
            ledger.emit(kind="judged", phase="gate", src="decide", task="undone", case=case["id"],
                        judge=judge.key(config), v=judge.version("undone"), fails=bool(reading["undone"]),
                        secs=meta["secs"])
            if reading["undone"]:
                ledger.emit(kind="reversal", id=eid, how="judge", evidence=short(reading.get("evidence"), 200))
            finished.append(eid)
    def keep(st):
        st["judged_undone"] = sorted(set(st.get("judged_undone", [])) | set(finished))
        st["unreadable_undone"] = sorted(set(st.get("unreadable_undone", [])) | set(unread))
    if finished or unread:
        ledger.with_state(keep)


# ---- recommendations -----------------------------------------------------------------------

def recommends(ask):
    return any("(recommended)" in o.get("label", "").lower()
               for q in ask["input"].get("questions", []) for o in options(q))


def recommendations_in(events, after):
    """Yield (time, via) for each turn that ended, at a message the user typed after `after`,
    on a recommendation: via "tool" or "prose"."""
    last, asked, recommended = "", False, False
    for t, who, x in events:
        if who == "self":
            last = x
        elif who == "ask":
            asked, recommended = True, recommended or recommends(x)
        elif who == "user":
            if t > after and (recommended or (not asked and PICK.search(last))):
                yield t, "tool" if recommended else "prose"
            last, asked, recommended = "", False, False


# ---- the scan ------------------------------------------------------------------------------

def scan():
    """Record what is new. Returns (ids of the answers read for reversals, {id: reversal},
    [recommendation events]); while the judge decides *undone*, only its reversals and the answers
    it has read."""
    import judge
    judging = judge.decides("undone")
    events = ledger.load()
    state = ledger.read_state()
    undone = {e["id"] for e in events if e.get("kind") == "reversal"}
    closed = set(state.get("scanned", []))
    seen = state.get("recommendations", {})   # transcript: [its mtime when read, last typed message]
    files = {Path(f).stem: f for f in glob.glob(str(ledger.PROJECTS / "*" / "*.jsonl"))}
    open_answers = {}
    for e in events:
        if e.get("kind") == "answer" and e.get("sid") in files and e["id"] not in closed | undone:
            open_answers.setdefault(e["sid"], []).append(e)
    changed = {sid for sid, f in files.items() if os.path.getmtime(f) > seen.get(sid, [0, 0])[0]}
    read, now_closed, read_now = set(), set(), {}
    for sid in set(open_answers) | changed:
        try:
            mtime, session = os.path.getmtime(files[sid]), read_session(files[sid])
        except OSError:   # a transcript Claude Code removed while we listed them
            continue
        for eid, how, evidence in ([] if judging else reversals_in(session, open_answers.get(sid, []))):
            ledger.emit(kind="reversal", id=eid, how=how, evidence=evidence)
        answers = open_answers.get(sid, [])
        read |= {e["id"] for e in answers}
        now_closed |= {e["id"] for e in answers if (ledger.stamp(e["t"]) or 0) < time.time() - WINDOW}
        if sid in changed:
            after = seen.get(sid, [0, 0])[1]
            for t, via in recommendations_in(session, after):
                ledger.emit(kind="recommendation", via=via, sid=sid, at=t)
            read_now[sid] = [mtime, max([after] + [t for t, who, _ in session if who == "user"])]

    def keep(s):
        s["scanned"] = sorted(set(s.get("scanned", [])) | now_closed)
        s.setdefault("recommendations", {}).update(read_now)
    if now_closed or read_now:
        ledger.with_state(keep)
    if judging:
        judge_reversals(ledger.load(), files)
    events = ledger.load()
    recs = [e for e in events if e.get("kind") == "recommendation"]
    if judging:
        reversals = {e["id"]: e for e in events if e.get("kind") == "reversal" and e.get("how") in JUDGED}
        return set(ledger.read_state().get("judged_undone", [])) | set(reversals), reversals, recs
    reversals = {e["id"]: e for e in events if e.get("kind") == "reversal"}
    return closed | read | set(reversals), reversals, recs
