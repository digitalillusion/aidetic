"""`aidetic habits`: which of the user's corrections of Claude recur, read from their own
transcripts, and whether a guard at the point where each happens could pay.
Nothing here knows a habit in advance. A judge reads each message the user typed, with what the
assistant had just done and written, as an objection to something already done, an instruction
that forestalls it, or neither (judge.CORRECTION); the objections and instructions are grouped into
habits (judge.GROUPS); code counts how often each habit's point of action occurred in the same days
and prices a guard there. Readings stay in ~/.claude/aidetic/habits, beside the ledger, since they
quote work text.

  aidetic habits [--days N] [--model M] [--effort E] [-j N]   read what is new, group, price
  aidetic habits show N                                        the user's words for habit N
"""
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import judge
import ledger
from evalset import append, load, option, transcripts
from transcripts import read_session

DIR = ledger.DIR / "habits"
READINGS, GROUPED, TABLE = DIR / "readings.jsonl", DIR / "groups.json", DIR / "table.json"
BATCH = 12     # messages to one reading
GROUP_SECS = 900   # the grouping reads every objection and instruction at once
FLAG = 0.01    # the share of points a judge flags wrongly, the worst per draw measured on *reply* (8.4)
NOT_TYPED = ("Stop hook feedback",)   # written into the user's turn by Claude Code

CHANGE = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
MUTATES = re.compile(r"\bgit (commit|push|reset|rebase|merge|cherry-pick|revert|stash|tag|rm)\b|\bgit branch -[dD]\b|"
                     r"(^|[;&|]\s*)(rm|mv)\s|\bgh (pr|issue) (create|merge|close|edit)\b")
SENDS = re.compile(r"(^|_)(send|post|reply|comment|publish|schedule)(_|$)")
SENDS_BASH = re.compile(r"\bgh (pr|issue) (comment|review)\b|\bgh api\b.*\b(comments|reviews)\b")


def kind(use):
    """Where a tool call acts, one of judge.ACTS, from the tool's name and, for a shell command, its words."""
    name, cmd = use.get("name", ""), (use.get("input") or {}).get("command", "")
    if name == "AskUserQuestion":
        return "question"
    if name in CHANGE:
        return "change"
    if name.startswith("mcp__") and SENDS.search(name.split("__")[-1]):
        return "message"
    if name == "Bash":
        return "message" if SENDS_BASH.search(cmd) else "change" if MUTATES.search(cmd) else "command"
    return "other"


def target(use):
    inp = use.get("input") or {}
    return " ".join(str(inp.get("file_path") or inp.get("command") or use.get("name", "")).split())[:80]


def messages(since):
    """The messages the user typed since `since` (epoch seconds), each with what the assistant did
    and wrote in the turn before it; and how often each point of action occurred in those days,
    counted in turns: a turn that answered a message and did that kind of act at least once (every
    such turn ends in a reply), since a user objects to what a turn did, not to each call in it."""
    out, seen = [], dict.fromkeys(judge.ACTS, 0)
    for f in transcripts():
        if os.path.getmtime(f) >= since:
            out += session(f, since, seen)
    return out, seen


def session(f, since, seen=None):
    """The messages of one transcript typed after `since`, as messages() gives them; `seen` counts
    their turns' points of action."""
    out, acts, said = [], [], ""
    for t, who, x in read_session(f, acts=True):
        if who in ("act", "ask"):
            acts.append(("question" if who == "ask" else kind(x), target(x)))
        elif who == "self":
            said = x or said
        elif who == "user":
            text = x.strip()
            if t > since and text and not text.startswith(NOT_TYPED):
                for a in ({a for a, _ in acts} | {"reply"}) if seen is not None else ():
                    seen[a] += 1
                out.append(dict(id=hashlib.sha1(f"{f}|{t}|{text[:200]}".encode()).hexdigest()[:10], t=t,
                                sid=Path(f).stem, project=Path(f).parent.name, text=text[:700],
                                before=said[-500:], acts=acts[-12:]))
            acts, said = [], ""
    return out


def render(batch):
    parts = []
    for n, m in enumerate(batch, 1):
        did = ", ".join(f"{a} ({t})" for a, t in m["acts"]) or "no tool calls"
        parts.append(f"[{n}]\nTHE ASSISTANT'S LAST TURN: {did}\nIT LAST WROTE: …{m['before']}\n"
                     f"THE USER WROTE: {m['text']}")
    return "\n\n".join(parts)


def read(batch, config):
    """One reading of a batch of messages; a message the reading leaves out is read again next time."""
    reading, _ = judge.draw("correction", dict(input=dict(text=render(batch))), config)
    by = {r["i"]: r for r in reading["items"]}
    return [dict(id=m["id"], v=judge.version("correction"), judge=judge.key(config), at=m["t"], sid=m["sid"],
                 project=m["project"], kind=by[n]["kind"], habit=by[n]["habit"], act=by[n]["act"],
                 quote=by[n]["quote"][:300]) for n, m in enumerate(batch, 1) if n in by]


def group(found, config):
    """[{name, act, ids}] for the objections and instructions found; drawn again only when they
    change. An item the reading leaves out joins the others of its own name."""
    items = [dict(id=r["id"], kind=r["kind"], act=r["act"], habit=r["habit"], quote=r["quote"][:200]) for r in found]
    key = hashlib.sha1((judge.version("habits") + json.dumps(items, sort_keys=True)).encode()).hexdigest()[:12]
    try:
        old = json.loads(GROUPED.read_text())
        if old.get("key") == key:
            return old["groups"]
    except (OSError, ValueError):
        pass
    text = "\n".join(" | ".join(x[k] for k in ("id", "kind", "act", "habit", "quote")) for x in items)
    config = {**config, "timeout": max(config.get("timeout", 60), GROUP_SECS)}   # one draw over every item
    reading, _ = judge.draw("habits", dict(input=dict(text=text)), config)
    groups, placed = [], set()
    for g in reading["groups"]:
        ids = [i for i in g["ids"] if i in {x["id"] for x in items} and i not in placed]
        placed |= set(ids)
        if ids:
            groups.append(dict(name=g["name"], act=g["act"], ids=ids))
    for x in items:
        if x["id"] not in placed:
            same = next((g for g in groups if g["name"].lower() == x["habit"].lower()), None)
            if same:
                same["ids"].append(x["id"])
            else:
                groups.append(dict(name=x["habit"], act=x["act"], ids=[x["id"]]))
    DIR.mkdir(parents=True, exist_ok=True)
    GROUPED.write_text(json.dumps(dict(key=key, groups=groups), ensure_ascii=False, indent=1))
    return groups


def verdict(objected, share):
    if not objected:
        return "forewarned only"
    if share < FLAG:
        return "no: rarer than a judge's wrong flags"
    return "worth measuring" if share >= 3 * FLAG else "marginal"


def price(groups, found, seen, days):
    """A row per habit: its objections, its instructions given in advance, and the share of its points
    of action objected to, against what a judge flags wrongly there."""
    of, rows = {r["id"]: r for r in found}, []
    for g in groups:
        members = [of[i] for i in g["ids"] if i in of]
        objected = sum(m["kind"] == "objection" for m in members)
        points = seen.get(g["act"], 0)
        share = objected / points if points else 0.0
        rows.append(dict(name=g["name"], act=g["act"], objected=objected, points=points, per_day=points / days,
                         forewarned=sum(m["kind"] == "instruction" for m in members), share=share,
                         verdict=verdict(objected, share), ids=g["ids"]))
    return sorted(rows, key=lambda r: (-r["share"], -r["objected"], -r["forewarned"]))


def main(args):
    if args[:1] == ["show"]:
        return show(args[1:])
    days, jobs = float(option(args, "--days", "28")), int(option(args, "-j", "4"))
    config = judge.configured("habits")
    config.update({k: option(args, f"--{k}", None) for k in ("model", "effort") if f"--{k}" in args})
    msgs, seen = messages(time.time() - days * 86400)
    v = judge.version("correction")
    done = {r["id"]: r for r in load(READINGS) if r.get("v") == v}
    todo = [m for m in msgs if m["id"] not in done]
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    print(f"{len(msgs)} messages typed in {days:g} days; {len(todo)} to read in {len(batches)} readings "
          f"on {judge.key(config)}, {jobs} at a time", file=sys.stderr)
    DIR.mkdir(parents=True, exist_ok=True)
    failed = 0
    with ThreadPoolExecutor(jobs) as pool:
        for n, fut in enumerate(as_completed([pool.submit(read, b, config) for b in batches])):
            try:
                rows = fut.result()
                append(READINGS, rows)
                done.update({r["id"]: r for r in rows})
            except Exception as e:   # the next run reads the batch again
                failed += 1
                print(f"a reading failed: {str(e)[:200]}", file=sys.stderr)
            if (n + 1) % 10 == 0 or n + 1 == len(batches):
                print(f"{n + 1}/{len(batches)} read, {failed} failed", file=sys.stderr, flush=True)
    found = [done[m["id"]] for m in msgs if m["id"] in done and done[m["id"]]["kind"] != "none"]
    import loop   # the loop's habits, once seeded from a first table, are the ones priced
    if loop.state().get("habits"):
        loop.assign(found, config)
        grouped = loop.groups()
    else:
        grouped = group(found, config) if found else []
        loop.seed(grouped, found)
    rows = price(grouped, found, seen, days)
    unread = sum(m["id"] not in done for m in msgs)
    print(f"habits in {days:g} days: {len(msgs)} messages typed ({unread} unread), "
          f"{sum(r['kind'] == 'objection' for r in found)} objections, "
          f"{sum(r['kind'] == 'instruction' for r in found)} instructions given in advance\n")
    TABLE.write_text(json.dumps([{k: r[k] for k in ("name", "act", "objected", "forewarned", "ids")} for r in rows],
                                ensure_ascii=False))   # what `show N` numbers by
    print(f"{'#':>3}  {'habit':48s}{'where':>9s}{'objected':>10s}{'forewarned':>12s}{'pts/day':>9s}{'share':>8s}  guard")
    for n, r in enumerate(rows, 1):
        print(f"{n:>3}  {r['name'][:47]:48s}{r['act']:>9s}{r['objected']:>10d}{r['forewarned']:>12d}"
              f"{r['per_day']:>9.1f}{100 * r['share']:>7.1f}%  {r['verdict']}")
    print(f"\npoints of action in those days: " + ", ".join(f"{a} {seen[a]}" for a in judge.ACTS)
          + f".\nshare: objections over the points of action where the habit happens. A guard there wrongly "
          f"flags about {100 * FLAG:g}% of points\n(the judge on *reply*), so it pays only "
          f"above that; 'worth measuring' from {300 * FLAG:g}%. `aidetic habits show N` for the user's words.")


def show(args):
    try:
        rows = json.loads(TABLE.read_text())
    except (OSError, ValueError):
        sys.exit("aidetic habits show: no habits yet; run `aidetic habits` first")
    if not args or not args[0].isdigit() or not 1 <= int(args[0]) <= len(rows):
        sys.exit(f"aidetic habits show N: N from 1 to {len(rows)}")
    r, of = rows[int(args[0]) - 1], {x["id"]: x for x in load(READINGS)}
    print(f"{r['name']} ({r['act']}): {r['objected']} objected, {r['forewarned']} forewarned\n")
    for x in sorted((of[i] for i in r["ids"] if i in of), key=lambda x: x["at"]):
        when = time.strftime("%d %b %H:%M", time.gmtime(x["at"]))
        print(f"  {when}  {x['kind']:11s} {x['project'][:28]:28s}  {x['quote'][:200]}")
