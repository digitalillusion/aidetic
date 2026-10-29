#!/usr/bin/env python3
"""The judge's evaluation set: cases read from the transcripts,
labelled by two independent draws of a strong model; where the two disagree, the case has no label
and is out of every count (no case is put to the user). Each case is put once, at
random, among the cases the judge learns from (one in five) or among those that measure it, never
both. The set holds work text, so it lives with the ledger in
~/.claude/aidetic/eval and is never committed.

  build                               add the cases the transcripts hold that the set lacks
  label [TASK] [-j N] [--model M] [--effort E] [--limit N]
                                      draw the labels still missing, two per case
                                      (--backend anthropic|openai with a key, cli by default)
  status                              per task: cases, labelled, agreed, left out, failing
  disagreements [TASK]                the cases whose two draws disagree, with both readings: left out
  show ID                             what the judge reads for one case
  decide ID[,ID ...] fail|pass|unclear [NOTE]
                                      a label by hand; unclear leaves a case out of every count
  translate LANG[,LANG] [TASK ...] [--model M] [-j N]
                                      each case once into each language, its label unchanged
  starter [--model M] [-j N]          one reading of each case of starter.json against its label
  measure [TASK] [--draws N] [-j N] [--backend B] [--model M] [--effort E]
                                      the judge of judge.json (or `claude -p` on Haiku), or the one
                                      named, on every labelled measuring case, three draws each,
                                      against the bars

Cases: *question*, every single-select question put through the tool (identical retries once);
*reply*, every reply a turn ended or stopped on with no question asked in the turn: all that the
old phrase list matches and one in SAMPLE of the rest; *undone*, every answered question whose four
hours have passed, with what followed them. Headless sessions run from a temporary folder are not
read."""
import glob
import hashlib
import json
import math
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge         # noqa: E402
import ledger        # noqa: E402
import questions     # noqa: E402
from scans import WINDOW               # noqa: E402
from transcripts import read_session   # noqa: E402

DIR = ledger.DIR / "eval"
CASES, READINGS, DECISIONS = DIR / "cases.jsonl", DIR / "readings.jsonl", DIR / "decisions.jsonl"
JUDGED = DIR / "judged.jsonl"   # the judge's own draws, measured against the labels; never labels
STARTER = Path(__file__).resolve().parent / "starter.json"   # written from scratch; ships with the code
LEARN = 5    # one case in LEARN is learned from, the others measure
SAMPLE = 6   # one reply in SAMPLE among those the phrase list does not match
DRAWS = 2


def chance(salt, cid, n):
    """A fixed coin for this case: the same on every build, and independent across salts."""
    return int(hashlib.sha1(f"{salt}|{cid}".encode()).hexdigest(), 16) % n == 0


def split(cid):
    return "learn" if chance("split", cid, LEARN) else "measure"


def load(path):
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except OSError:
        return []


def append(path, rows):
    DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        for row in rows:
            os.write(fd, (json.dumps(row, ensure_ascii=False) + "\n").encode())
    finally:
        os.close(fd)


# ---- build ---------------------------------------------------------------------------------------

def transcripts():
    for f in sorted(glob.glob(str(ledger.PROJECTS / "*" / "*.jsonl"))):
        if not Path(f).parent.name.startswith("-tmp-"):   # headless test sessions
            yield f


def minutes(t, t0):
    return round((t - t0) / 60)


def question_cases(sid, session, gate):
    for t, who, x in session:
        if who != "ask":
            continue
        for q in x["input"].get("questions") or []:
            if q.get("multiSelect"):
                continue
            cid = judge.question_id(q)
            yield dict(id=cid, task="question", input=dict(question=q), src=dict(
                sid=sid, tuid=x["id"], t=t, gate=gate.get(x["id"]),
                patterns=sorted(c.__name__ for c, _ in questions.findings([q]))))


def reply_cases(sid, session):
    """The last text before each stop or typed message, in a turn that asked nothing."""
    last, asked = None, False
    for t, who, x in session:
        if who == "self":
            last = (t, x)
        elif who == "ask":
            asked = True
        elif who in ("stop", "user"):
            if last and not asked and last[1].strip():
                text = last[1]
                cid = judge.reply_id(sid, text)
                hit = questions.names_pick(text)
                if hit or chance("sample", cid, SAMPLE):
                    yield dict(id=cid, task="reply", input=dict(reply=text),
                               src=dict(sid=sid, t=last[0], patterns=hit))
            last = None
            if who == "user":
                asked = False


def undone_cases(sid, session, reversed_by):
    asks = {x["id"]: x for t, who, x in session if who == "ask"}
    for i, (t0, who, res) in enumerate(session):
        if who != "answer" or res.get("tool_use_id") not in asks or t0 + WINDOW > time.time():
            continue
        after = []
        for t, w, x in session[i + 1:]:
            if t > t0 + WINDOW:
                break
            if w in ("self", "user") and x.strip():
                after.append(dict(min=minutes(t, t0), who="assistant" if w == "self" else "user", text=x))
            elif w == "answer":
                for q in x.get("questions") or []:
                    labels = " | ".join(o.get("label", "") for o in questions.options(q))
                    after.append(dict(min=minutes(t, t0), who="asked", text=f"{q.get('question', '')} — {labels}",
                                      picked=(x.get("answers") or {}).get(q.get("question", ""), "")))
        ask = asks[res["tool_use_id"]]
        for q in ask["input"].get("questions") or []:
            text = q.get("question", "")
            picked = (res.get("answers") or {}).get(text)
            if not picked:
                continue
            cid = ledger.event_id(ask["id"], text)   # the ledger's id for this answer
            note = ((res.get("annotations") or {}).get(text) or {}).get("notes")
            yield dict(id="u" + cid, task="undone",
                       input=dict(question=q, picked=picked, note=note, after=after),
                       src=dict(sid=sid, tuid=ask["id"], t=t0, patterns=reversed_by.get(cid)))


def build(args):
    have = {c["id"] for c in load(CASES)}
    events = ledger.load()
    gate = {}
    for e in events:
        if e.get("kind") in ledger.ASKED and e.get("tuid"):
            gate[e["tuid"]] = e["kind"]   # the last decision on the call: ask, denied or escaped
    reversed_by = {e["id"]: e.get("how") for e in events if e.get("kind") == "reversal"}
    new = []
    for f in transcripts():
        sid = Path(f).stem
        try:
            session = read_session(f, answers=True)
        except OSError:
            continue
        for case in [*question_cases(sid, session, gate), *reply_cases(sid, session),
                     *undone_cases(sid, session, reversed_by)]:
            if case["id"] not in have:
                have.add(case["id"])
                new.append({**case, "split": split(case["id"]), "lang": "orig", "added": ledger.now()})
    append(CASES, new)
    for task in judge.TASKS:
        print(f"{task}: {sum(c['task'] == task for c in new)} added")


# ---- labels --------------------------------------------------------------------------------------

def option(args, name, default):
    return args[args.index(name) + 1] if name in args else default


def label(args):
    tasks = [a for a in args if a in judge.TASKS] or list(judge.TASKS)
    model, effort = option(args, "--model", "opus"), option(args, "--effort", "high")
    jobs, limit = int(option(args, "-j", "4")), int(option(args, "--limit", "0"))
    backend = option(args, "--backend", "cli")
    done = {(r["id"], r["draw"]) for r in load(READINGS)
            if r["v"] == judge.version(r["task"]) and r["model_asked"] == model}
    todo = [(c, d) for c in load(CASES) if c["task"] in tasks and c["lang"] == "orig"
            for d in range(1, DRAWS + 1) if (c["id"], d) not in done]
    random.Random(0).shuffle(todo)   # a stop part-way leaves every task partly labelled
    todo = todo[:limit] if limit else todo
    print(f"{len(todo)} readings to draw on {model}, {jobs} at a time", file=sys.stderr)

    def one(case, draw):
        reading, meta = judge.read(case["task"], case, backend=backend, model=model, effort=effort)
        return dict(id=case["id"], task=case["task"], draw=draw, v=judge.version(case["task"]),
                    model_asked=model, effort=effort, reading=reading,
                    fails=judge.verdict(case["task"], case, reading), t=ledger.now(), **meta)

    failed = 0
    with ThreadPoolExecutor(jobs) as pool:
        futures = {pool.submit(one, c, d): c["id"] for c, d in todo}
        for n, fut in enumerate(as_completed(futures)):
            try:
                append(READINGS, [fut.result()])
            except Exception as e:   # the next run draws it again
                failed += 1
                print(f"\n{futures[fut]}: {str(e)[:200]}", file=sys.stderr)
            if (n + 1) % 50 == 0 or n + 1 == len(todo):
                print(f"{n + 1}/{len(todo)} drawn, {failed} failed", file=sys.stderr, flush=True)


def labelled():
    """{case id: [the current readings]}, {case id: the user's decision}."""
    readings = {}
    for r in load(READINGS):
        if r["v"] == judge.version(r["task"]):
            readings.setdefault(r["id"], []).append(r)
    return readings, {d["id"]: d for d in load(DECISIONS)}


def original(cid):
    return cid.split("@")[0]   # a translation's id is its original's, with "@" and the language


def final(cid, readings, decisions):
    """The case's label: True (fails), False, or None while the draws are short or disagree
    and the user has not decided. A translation has its original's label."""
    cid = original(cid)
    if cid in decisions:
        return decisions[cid]["fails"]
    rs = readings.get(cid, [])
    verdicts = {r["fails"] for r in rs}
    return verdicts.pop() if len(rs) >= DRAWS and len(verdicts) == 1 else None


def status(args):
    cases = [c for c in load(CASES) if c["lang"] == "orig"]
    readings, decisions = labelled()
    print(f"{'':10s}{'cases':>7s}{'learn':>7s}{'drawn':>7s}{'agreed':>8s}{'decided':>9s}{'left out':>10s}"
          f"{'fail':>6s}{'pass':>6s}{'unclear':>9s}")
    for task in judge.TASKS:
        cs = [c for c in cases if c["task"] == task]
        full = [c for c in cs if len(readings.get(c["id"], [])) >= DRAWS]
        agreed = [c for c in full if len({r["fails"] for r in readings[c["id"]]}) == 1]
        open_ = [c for c in full if c not in agreed and c["id"] not in decisions]
        labels = [final(c["id"], readings, decisions) for c in cs]
        print(f"{task:10s}{len(cs):7d}{sum(c['split'] == 'learn' for c in cs):7d}{len(full):7d}{len(agreed):8d}"
              f"{sum(c['id'] in decisions for c in cs):9d}{len(open_):10d}"
              f"{labels.count(True):6d}{labels.count(False):6d}"
              f"{sum(decisions.get(c['id'], {}).get('fails', 0) is None for c in cs):9d}")
    langs = {}
    for c in load(CASES):
        if c["lang"] != "orig":
            langs.setdefault(c["lang"], []).append(c["task"])
    for lang, tasks in sorted(langs.items()):
        print(f"translated to {lang}: " + ", ".join(f"{tasks.count(t)} {t}" for t in judge.TASKS))


def brief(task, reading):
    if task == "question":
        opts = "; ".join(f"{o['label'][:30]}:{'C' if o['case'] else '-'}{'U' if o['undo'] else '-'}"
                         f"{'F' if o['free_text'] else ''}" for o in reading["options"])
        return f"rec={reading['recommended']!r} reasoned={reading['reasoned']} [{opts}]"
    return json.dumps(reading, ensure_ascii=False)[:300]


def disagreements(args):
    readings, decisions = labelled()
    for c in load(CASES):
        rs = readings.get(c["id"], [])
        if (args and c["task"] not in args) or c["id"] in decisions or len(rs) < DRAWS:
            continue
        if len({r["fails"] for r in rs}) > 1:
            print(f"{c['id']}  {c['task']}  {c['split']}")
            for r in rs:
                print(f"   draw {r['draw']}: {'FAILS' if r['fails'] else 'passes'}  {brief(c['task'], r['reading'])}")


def show(args):
    case = next((c for c in load(CASES) if c["id"] == (args or [""])[0]), None)
    if not case:
        sys.exit("show: no such case")
    print(judge.render(case["task"], case))


VERDICTS = {"fail": True, "pass": False, "unclear": None}   # unclear: no label, out of every count


def decide(args):
    if len(args) < 2 or args[1] not in VERDICTS:
        sys.exit("decide ID[,ID ...] fail|pass|unclear [NOTE]")
    ids, have = args[0].split(","), {c["id"] for c in load(CASES)}
    if not set(ids) <= have:
        sys.exit(f"decide: no such case: {', '.join(sorted(set(ids) - have))}")
    append(DECISIONS, [dict(id=cid, fails=VERDICTS[args[1]], note=" ".join(args[2:]), by="user", t=ledger.now())
                       for cid in ids])


# ---- translations --------------------------------------------------------------------------------

LANGUAGES = {"it": "Italian", "ja": "Japanese", "es": "Spanish", "de": "German", "fr": "French",
             "pt": "Portuguese", "zh": "Chinese"}
FIXED = ("who", "min", "multiSelect")   # values that are not text: copied back, never translated
TRANSLATE = """Translate into {language} every piece of natural-language text in the JSON document \
the user sends: questions, headers, labels, descriptions, previews, notes, replies and messages. \
Write it as a native speaker would in that situation, including tags and set phrases such as \
"(Recommended)", "Hard to undo:", "read:", "assumed:" or "Other (describe in the note)". Keep code, \
identifiers, file names, paths, commands, URLs and numbers exactly as they are. Translate a string \
the same way wherever it repeats, so that a picked answer still matches its option's label. Change \
nothing else: add, drop, fix or soften nothing. Return the same JSON document, with the same keys \
and the same lists in the same order, as the string `json`."""


def same_shape(a, b):
    if isinstance(a, dict):
        return isinstance(b, dict) and a.keys() == b.keys() and all(same_shape(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(map(same_shape, a, b))
    return isinstance(b, str) if isinstance(a, str) else a == b


def restore(a, b):
    """Copy back into the translation b the values of a that are not text."""
    if isinstance(a, dict):
        for k in a:
            b[k] = a[k] if k in FIXED else restore(a[k], b[k])
    elif isinstance(a, list):
        b[:] = [restore(x, y) for x, y in zip(a, b)]
    return b


def translated(case, lang, model, effort):
    out, meta = judge.claude_cli(TRANSLATE.format(language=LANGUAGES[lang]),
                                 json.dumps(case["input"], ensure_ascii=False, indent=1),
                                 {"type": "object", "additionalProperties": False, "required": ["json"],
                                  "properties": {"json": {"type": "string"}}},
                                 model=model, effort=effort, timeout=1800)
    x = json.loads(out["json"])
    if not same_shape(case["input"], x):
        raise ValueError("the translation changed the document's shape")
    x = restore(case["input"], x)
    if case["task"] == "undone":   # the pick names its option's label, in the label's translation
        labels = [o.get("label") for o in questions.options(case["input"]["question"])]
        if case["input"]["picked"] in labels:
            x["picked"] = questions.options(x["question"])[labels.index(case["input"]["picked"])]["label"]
    return {**case, "id": f"{case['id']}@{lang}", "lang": lang, "input": x, "added": ledger.now(),
            "src": {"of": case["id"], "model": meta["model"], "cost": meta["cost"]}}


def translate(args):
    """translate LANG[,LANG] [TASK ...]: each original case once into each language, label unchanged."""
    langs = (args or [""])[0].split(",")
    if not all(lang in LANGUAGES for lang in langs):
        sys.exit(f"translate LANG[,LANG] [TASK ...]   languages: {', '.join(LANGUAGES)}")
    tasks = [a for a in args if a in judge.TASKS] or list(judge.TASKS)
    model, effort = option(args, "--model", "opus"), option(args, "--effort", "medium")
    cases = load(CASES)
    have = {c["id"] for c in cases}
    todo = [(c, lang) for c in cases if c["lang"] == "orig" and c["task"] in tasks
            for lang in langs if f"{c['id']}@{lang}" not in have]
    print(f"{len(todo)} translations on {model}", file=sys.stderr)
    failed = 0
    with ThreadPoolExecutor(int(option(args, "-j", "4"))) as pool:
        futures = {pool.submit(translated, c, lang, model, effort): c["id"] for c, lang in todo}
        for n, fut in enumerate(as_completed(futures)):
            try:
                append(CASES, [fut.result()])
            except Exception as e:   # the next run tries again
                failed += 1
                print(f"{futures[fut]}: {str(e)[:200]}", file=sys.stderr)
            if (n + 1) % 50 == 0 or n + 1 == len(todo):
                print(f"{n + 1}/{len(todo)} translated, {failed} failed", file=sys.stderr, flush=True)


def starter(args):
    """One reading of each starter case, against the label it was written with."""
    model, effort = option(args, "--model", "opus"), option(args, "--effort", "high")
    cases = json.loads(STARTER.read_text())
    with ThreadPoolExecutor(int(option(args, "-j", "4"))) as pool:
        readings = list(pool.map(lambda c: judge.read(c["task"], c, model=model, effort=effort)[0], cases))
    agree = 0
    for c, reading in zip(cases, readings):
        fails = judge.verdict(c["task"], c, reading)
        agree += fails == c["fails"]
        if fails != c["fails"]:
            print(f"{c['id']}: written to {'fail' if c['fails'] else 'pass'}, read as "
                  f"{'failing' if fails else 'passing'}  {brief(c['task'], reading)}")
    print(f"{agree} of {len(cases)} as written, on {model}")


# ---- the judge against the labels ----------------------------------------------------------------

BARS = dict(caught=0.95, false=0.02, flips=0.05, secs=2.0)   # one-sided 95% bounds


def bound(k, n, upper, level=0.95):
    """The one-sided exact (Clopper-Pearson) bound on a rate seen k times in n: the lowest rate under
    which k or more is still likely, or with upper the highest under which k or fewer is."""
    def tail(p):   # P(X >= k), or P(X <= k) for the upper bound
        span = range(0, k + 1) if upper else range(k, n + 1)
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in span)
    if n == 0 or (upper and k == n) or (not upper and k == 0):
        return 1.0 if upper else 0.0
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if (tail(mid) > 1 - level) == upper:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def measure(args):
    """The configured judge on every labelled measuring case, DRAWS times each. A case counts as caught
    (or as wrongly flagged) only when every draw agrees, so a case that changes between draws counts
    against the judge in the bounds as well as in the flips."""
    tasks = [a for a in args if a in judge.TASKS] or list(judge.TASKS)
    draws, jobs = int(option(args, "--draws", "3")), int(option(args, "-j", "3"))
    config = judge.configured()
    config.update({k: option(args, f"--{k}", None) for k in ("backend", "model", "effort") if f"--{k}" in args})
    key = judge.key(config)
    readings, decisions = labelled()
    cases = [c for c in load(CASES) if c["task"] in tasks and c["lang"] == "orig" and c["split"] == "measure"
             and final(c["id"], readings, decisions) is not None]
    done = {(r["id"], r["draw"]) for r in load(JUDGED) if r["v"] == judge.version(r["task"]) and r["judge"] == key}
    todo = [(c, d) for c in cases for d in range(1, draws + 1) if (c["id"], d) not in done]
    print(f"{len(todo)} draws of {key} on {len(cases)} measuring cases", file=sys.stderr)

    def one(case, draw):
        reading, meta = judge.draw(case["task"], case, config)
        return dict(id=case["id"], task=case["task"], draw=draw, v=judge.version(case["task"]), judge=key,
                    reading=reading, fails=judge.verdict(case["task"], case, reading), t=ledger.now(), **meta)

    with ThreadPoolExecutor(jobs) as pool:
        futures = {pool.submit(one, c, d): c["id"] for c, d in todo}
        for n, fut in enumerate(as_completed(futures)):
            try:
                append(JUDGED, [fut.result()])
            except Exception as e:   # the next run draws it again
                print(f"\n{futures[fut]}: {str(e)[:200]}", file=sys.stderr)
            if (n + 1) % 50 == 0 or n + 1 == len(todo):
                print(f"{n + 1}/{len(todo)} drawn", file=sys.stderr, flush=True)
    report(cases, readings, decisions, key, tasks)


def report(cases, readings, decisions, key, tasks):
    by = {}
    for r in load(JUDGED):
        if r["v"] == judge.version(r["task"]) and r["judge"] == key:
            by.setdefault(r["id"], []).append(r)
    print(f"{key}: point estimates per draw; bounds per case (every draw right), one-sided 95%")
    for task in tasks:
        rows = [(final(c["id"], readings, decisions), by[c["id"]]) for c in cases if c["task"] == task and c["id"] in by]
        if not rows:
            continue
        fails = [rs for label, rs in rows if label]
        passes = [rs for label, rs in rows if not label]
        caught = sum(r["fails"] for rs in fails for r in rs), sum(len(rs) for rs in fails)
        flagged = sum(r["fails"] for rs in passes for r in rs), sum(len(rs) for rs in passes)
        always = sum(all(r["fails"] for r in rs) for rs in fails)
        never = sum(not any(r["fails"] for r in rs) for rs in passes)
        flips = sum(len({r["fails"] for r in rs}) > 1 for _, rs in rows)
        secs = sorted(r["secs"] for _, rs in rows for r in rs if r.get("secs") is not None)
        median = secs[len(secs) // 2] if secs else None
        low, high = bound(always, len(fails), False), bound(len(passes) - never, len(passes), True)
        print(f"  {task}: {len(rows)} cases ({len(fails)} failing, {len(passes)} passing)")
        print(f"    failures caught  {caught[0]}/{caught[1]} draws = {caught[0] / max(caught[1], 1):.1%};"
              f"  lower bound {low:.1%} on {always}/{len(fails)} cases  (bar {BARS['caught']:.0%})")
        print(f"    passes flagged   {flagged[0]}/{flagged[1]} draws = {flagged[0] / max(flagged[1], 1):.1%};"
              f"  upper bound {high:.1%} on {len(passes) - never}/{len(passes)} cases  (bar {BARS['false']:.0%})")
        print(f"    changed between draws  {flips}/{len(rows)} = {flips / len(rows):.1%}  (bar {BARS['flips']:.0%})")
        if median is not None:
            print(f"    median seconds a call  {median}  (bar at a turn's end {BARS['secs']})")
        labels = {c["id"]: final(c["id"], readings, decisions) for c in cases if c["task"] == task}
        live = [e for e in ledger.load() if e.get("kind") == "judged" and e.get("task") == task
                and e.get("judge") == key and e.get("v") == judge.version(task)]
        matched = [e for e in live if e.get("case") in labels]
        if live:
            secs = sorted(e["secs"] for e in live if e.get("secs") is not None)
            print(f"    live, beside the patterns: {len(live)} verdicts, {len(matched)} on labelled measuring "
                  f"cases, {sum(e['fails'] == labels[e['case']] for e in matched)} agree; "
                  f"median seconds {secs[len(secs) // 2] if secs else '-'}")


COMMANDS = {"build": build, "label": label, "status": status, "disagreements": disagreements,
            "show": show, "decide": decide, "translate": translate, "starter": starter, "measure": measure}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[cmd](sys.argv[2:])
