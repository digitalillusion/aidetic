"""The commands that read the ledger: stats (before the gate against with it), recent, reversals,
arms (the off arm against the on arm), and regret, which marks an answer by hand; and correct,
which Claude calls when the user says the gate got one wrong."""
import math
import statistics
import sys

import ledger
from questions import short, unanswered
from scans import scan

FAST = 40   # seconds; a follow quicker than this cannot have read the cases
PACE = 25   # characters a second, a fast reading pace: a follow quicker than this skimmed the cases


def pct(n, d):
    return f"{n} ({100 * n // d}%)" if d else "0"


def on_screen(answers):
    """{answer id: characters}: those of the whole tool call, since its questions are read in the
    seconds the call took and each answer records those seconds."""
    calls = {}
    for e in answers:
        calls[e.get("tuid") or e["id"]] = calls.get(e.get("tuid") or e["id"], 0) + (e.get("chars") or 0)
    return {e["id"]: calls[e.get("tuid") or e["id"]] for e in answers}


def column(answers, read, undone, recs):
    # a note rides on a pick, so the pick counts; a message sent instead of answering is no pick
    picks = [e for e in answers if e.get("follow") is not None and not unanswered(e)]
    followed = [e for e in picks if e["follow"]]
    timed = [e for e in followed if e.get("secs") is not None]
    sized = [e for e in timed if e.get("chars")]   # recorded since 26 October
    read = [e for e in answers if e["id"] in read and not unanswered(e)]
    undid = lambda xs: pct(sum(1 for e in xs if e["id"] in undone), len(xs))
    secs = [e["secs"] for e in answers if e.get("secs") is not None]
    chars = on_screen(answers)
    skimmed = sum(1 for e in sized if chars[e["id"]] > PACE * e["secs"])
    prose = sum(1 for r in recs if r["via"] == "prose")
    return [
        ("questions answered", str(len(answers))),
        ("  with cases (previews)", pct(sum(1 for e in answers if e.get("prev")), len(answers))),
        ("  tagged (Recommended), single-select", str(len(picks))),
        ("followed the recommendation", pct(len(followed), len(picks))),
        (f"  in under {FAST} s", pct(sum(1 for e in timed if e["secs"] < FAST), len(timed))),
        (f"  faster than {PACE} characters a second", pct(skimmed, len(sized))),
        ("added a note", pct(sum(1 for e in answers if e.get("notes")), len(answers))),
        ("chose Other / typed their own", pct(sum(1 for e in answers if e.get("other")), len(answers))),
        ("reversed in its own session (of those read)", undid(read)),
        ("  followed picks", undid([e for e in read if e.get("follow") is True])),
        ("  picks that differ", undid([e for e in read if e.get("follow") is False])),
        ("median seconds to answer", f"{statistics.median(secs):.0f}" if secs else "-"),
        ("recommendations made in prose, not asked", pct(prose, len(recs))),
    ]


def stats(args):
    read, undone, recs = scan()
    events = ledger.load()
    if not events:
        print("no events yet")
        return
    first = min((e["t"] for e in events if e.get("phase") == "gate"), default="never")
    start = ledger.stamp(first) or float("inf")
    answers = lambda phase: [e for e in events if e.get("kind") == "answer" and e.get("phase") == phase]
    before = column(answers("before"), read, undone, [r for r in recs if r["at"] < start])
    gate = column(answers("gate"), read, undone, [r for r in recs if r["at"] >= start])
    print(f"aidetic   gate since {first[:10]}\n")
    print(f"{'':46s}{'before the gate':>18s}{'with the gate':>16s}")
    for (label, b), (_, g) in zip(before, gate):
        print(f"{label:46s}{b:>18s}{g:>16s}")
    n = lambda kind: sum(1 for e in events if e.get("kind") == kind)
    print(f"\nquestions denied by the gate: {n('ask_denied')}   let through after retries: "
          f"{n('ask_escaped')}   prose picks sent back: {n('prose_sent_back')} by the patterns, "
          f"{n('judge_sent_back')} by the judge ({n('judge_late')} more too late)")
    print(f"rule handed to running sessions: {n('rule_injected')}   "
          f"answers marked regretted (`regret ID`): {n('regret')}   corrections (`correct`): {n('correction')}"
          + (f"   answers the hook could not read: {n('answer_unparsed')}" if n("answer_unparsed") else ""))


def recent(args):
    _, undone, _ = scan()
    events = ledger.load()
    regretted = {e.get("id") for e in events if e.get("kind") == "regret"}
    for e in [e for e in events if e.get("kind") == "answer"][-12:]:
        mark = ("other   " if e.get("other") else
                {True: "followed", False: "differs ", None: "        "}[e.get("follow")])
        flags = ("N" if e.get("notes") else " ") + ("R" if e["id"] in regretted else "r" if e["id"] in undone else " ")
        print(f"{e['id']}  {e['t'][:10]}  {mark}  {flags}  {e['q'][:62]:62s} -> {e['ans'][:40]}")


def reversals(args):
    _, undone, _ = scan()
    answers = {e["id"]: e for e in ledger.load() if e.get("kind") == "answer"}
    for eid, r in sorted(undone.items(), key=lambda x: answers.get(x[0], {}).get("t", "")):
        e = answers.get(eid, {})
        print(f"{eid}  {e.get('t', '')[:10]}  {r.get('how', ''):15s} {e.get('q', '')[:60]} -> "
              f"{e.get('ans', '')[:30]}\n        {r.get('evidence', '')[:150]}")


ARM_SIZE = 326   # answers read in each arm before the verdict


def fisher_lower(a, b, c, d):
    """One-sided Fisher's exact p: the chance that the first row's share of the first column is
    as low as a / (a + b) or lower, the margins held."""
    n1, n2, k = a + b, c + d, a + c
    return sum(math.comb(n1, x) * math.comb(n2, k - x)
               for x in range(max(0, k - n2), a + 1)) / math.comb(n1 + n2, k)


def arms(args):
    """The off arm against the on arm: the answers of each arm's sessions, those whose four hours
    the judge has read, and the share it found undone in their own session."""
    read, undone, _ = scan()
    events = ledger.load()
    of = {e["sid"]: e["arm"] for e in events if e.get("kind") == "arm"}
    rows = {}
    for which in ("on", "off"):
        xs = [e for e in events if e.get("kind") == "answer" and of.get(e.get("sid")) == which
              and not unanswered(e)]
        done = [e for e in xs if e["id"] in read]
        rows[which] = (sum(1 for s in of.values() if s == which), len(xs), len(done),
                       sum(1 for e in done if e["id"] in undone))
    print(f"{'':12s}{'sessions':>10s}{'answers':>10s}{'read':>8s}{'undone':>12s}")
    for which, (s, a, r, u) in rows.items():
        print(f"{which + ' arm':12s}{s:>10d}{a:>10d}{r:>8d}{pct(u, r):>12s}")
    (_, _, r_on, u_on), (_, _, r_off, u_off) = rows["on"], rows["off"]
    p = fisher_lower(u_on, r_on - u_on, u_off, r_off - u_off) if r_on and r_off else None
    print(f"\nthe on arm undone less, one-sided Fisher p: {p:.3f}" if p is not None else "\nno answer read in one arm yet")
    print(f"read so far against the {ARM_SIZE} an arm of the verdict: {r_on} on, {r_off} off")


def regret(args):
    ids = {e.get("id") for e in ledger.load() if e.get("kind") == "answer"}
    hit = [i for i in ids if i.startswith(args[0])] if args else []
    if len(hit) != 1:
        sys.exit("regret: give the id of exactly one answer (see `recent`)")
    ledger.emit(kind="regret", id=hit[0], note=short(" ".join(args[1:]), 300))
    print(f"marked {hit[0]}")


CORRECTED = ("ask_denied", "prose_sent_back", "judge_sent_back",   # what the gate did that the user can call wrong
             "guard_denied", "guard_sent_back")


def correct(args):
    """correct ID|reply|missed WORDS [--quote TEXT]: the user's correction of the gate, in their words.
    ID is a denial or a send-back, whose message names it; reply is the judge's latest send-back,
    whose message says "gate event reply"; missed is a reply that should have asked and was let
    through, which --quote names by its words. Which sentence the rule gains from it is
    the session's to draft and the user's to accept."""
    quote = None
    if "--quote" in args:
        i = args.index("--quote")
        quote, args = " ".join(args[i + 1:]), args[:i]
    if len(args) < 2:
        sys.exit("correct ID|missed WORDS [--quote TEXT]")
    if args[0] == "missed":
        of, way = None, "missed"
    elif args[0] == "reply":   # the judge's prompt hook names no id of its own: its latest send-back
        latest = [e for e in ledger.load() if e.get("kind") == "judge_sent_back"]
        if not latest:
            sys.exit("correct: the judge has sent no reply back")
        of, way = latest[-1]["id"], "wrong"
    else:
        ids = {e["id"]: e["kind"] for e in ledger.load() if e.get("kind") in CORRECTED and e.get("id")}
        hit = [i for i in ids if i.startswith(args[0])]
        if len(hit) != 1:
            sys.exit("correct: give the gate event the denial or send-back named, or `missed`")
        of, way = hit[0], "wrong"
    ledger.emit(kind="correction", of=of, way=way, words=short(" ".join(args[1:]), 500),
                quote=short(quote, 500) if quote else None)
    print(f"recorded: {way}" + (f" {of}" if of else ""))
