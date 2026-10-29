#!/usr/bin/env python3
"""Gate and ledger for questions that carry a recommendation: Claude Code hooks that enforce the
choice rule in RULE.md.

Install, as the user's own setup: a checkout of this repository anywhere (REPO below), then
`./install.sh` there, which runs `install` below: the hooks of hooks.json, REPO filled in, merged
into ~/.claude/settings.json; `aidetic` linked to this file in ~/.local/bin, the command RULE.md
and the loop tell Claude to call; a judge.json if there is none. The
ledger is kept in ~/.claude/aidetic wherever the code is. A hook command ends in `|| true`:
Python exits 2 when the script is missing, and a PreToolUse hook that exits 2 blocks the question.
The one that does not, `reply`, must exit 2 to send a reply back; it runs after the turn has ended,
so a missing script blocks nothing there, and Claude Code reports it once as a broken hook.

  start    SessionStart: hand RULE.md to the session. With `"off_arm": SHARE` in judge.json,
           that share of new sessions is put in the off arm instead: no rule,
           and no hook below decides anything there, while the ledger records as before; at 1,
           every session is off, those already running included
  pre      PreToolUse on AskUserQuestion: deny a question that fails the rule's checks
           (questions.py), among them a `read:` tag citing a file the session never opened
           (transcripts.py); the third identical attempt goes through. With `"decide":
           ["question"]` in judge.json the judge reads the questions instead, and the patterns
           decide only when it cannot
  post     PostToolUse on AskUserQuestion: record the answer against the recommendation
  prompt   UserPromptSubmit: note when the turn began; hand RULE.md, once per change, to a
           session that began before its last change
  stop     Stop: send back, once, a reply that names its pick in prose when nothing was asked
           through the tool since the turn began; once `aidetic hook` has installed `reply`, the
           patterns send nothing back
  reply    Stop, run in the background (`asyncRewake`): the judge reads the turn's last reply and,
           when it names a pick in prose with nothing asked through the tool, exits 2, which wakes
           the session with the reason; dropped if the user wrote again while it read
  hook [--off]   install `reply` in ~/.claude/settings.json (or remove it), with the judge's
           Stop prompt hook it replaced taken out
  install  the whole setup above, again after any update: each hook of hooks.json put in
           settings.json where it is missing or differs, run the way the settings already run the
           gate; the link and judge.json only where there are none; at a terminal, the offer to
           read the past four weeks (`habits`) while no habit is known
           `install --plugin` leaves settings.json alone, the plugin carrying the hooks, and does the rest
  learn    Stop, in the background (`asyncRewake`): the loop of loop.py reads the turn's new
           messages, and wakes the session to propose a rule, send a guarded reply back or report
           a guard switched
  watch    PreToolUse, written into settings.json only while a habit is watched at that point of
           action: the loop's verdict on an act, or its guard's decision; under the plugin, which
           has no settings entries, those points of action are in watched.json

With `"shadow": true` in ~/.claude/aidetic/judge.json, pre and stop also hand each question and
each turn end to the judge (judge.py) in a detached process, `shadow FILE`, which records its
verdict as a `judged` event and decides nothing.

  stats | recent | reversals | arms | regret ID [NOTE]     the ledger's view (report.py); stats
           is the default, and each first records what the transcripts show since (scans.py)
  correct ID|reply|missed WORDS [--quote TEXT]      record that the user called a denial or a
           send-back wrong (ID: the gate event its message names), or a reply that should have
           asked and was let through (missed), in their words (RULE.md)
  habits [--days N] [show N]     which of the user's corrections of Claude recur, and whether a
           guard where each happens could pay (habits.py)
  rule [list] | rule ID accept [--text TEXT] | decline | drop      the user's answer to a rule the
           loop proposed, run by Claude (loop.py)

Every hook fails open: a bug here must never stop a question from being asked.
"""
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # the modules, also via the symlink
import ledger        # noqa: E402
import questions     # noqa: E402
import transcripts   # noqa: E402
try:
    import judge     # the shadow's only: a judge that cannot load must not take the gate with it
except Exception:
    judge = None

ESCAPE_AFTER = 2   # denials of the same problems before the next attempt goes through
STANDING = "Standing instruction of the user's, from their aidetic setup; follow it:\n\n"
KEPT = "Rules the user kept, from their aidetic setup; follow them:\n\n"


def off_share():
    """The share of new sessions put in the off arm (judge.json's `off_arm`, read whatever the judge's
    backend); none when it cannot be read."""
    try:
        return float(json.loads((ledger.DIR / "judge.json").read_text()).get("off_arm") or 0)
    except Exception:
        return 0.0


def arm(sid):
    """The session's arm, "on" or "off"; None for one that began before the arms or outside them,
    which runs as on. With `off_arm` at 1 every session is off."""
    if off_share() >= 1:
        return "off"
    return ledger.read_state().get("arms", {}).get(sid)


def start(p):
    sid = p.get("session_id", "")
    if not sid:
        return
    share = off_share() if ledger.RULES.exists() else 0.0

    def assign(state):
        arms = state.setdefault("arms", {})
        if (sid in arms or not share or (p.get("cwd") or "").startswith("/tmp/")   # headless runs stay out,
                or p.get("source") in ("resume", "compact")):   # and sessions from before the arms
            return arms.get(sid), False
        arms[sid] = "off" if random.random() < share else "on"
        for old in list(arms)[:-2000]:
            del arms[old]
        return arms[sid], True

    which, new = ledger.with_state(assign)
    if new:
        ledger.emit(kind="arm", phase="gate", src="hook", sid=sid, arm=which, source=p.get("source"),
                    cwd=p.get("cwd"))
    parts = ([STANDING + ledger.RULES.read_text()] if which != "off" and ledger.RULES.exists() else []) \
        + ([KEPT + ledger.KEPT.read_text()] if ledger.KEPT.exists() else [])
    if parts:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                 "additionalContext": "\n\n".join(parts)}}))


def pre(p):
    asked = (p.get("tool_input") or {}).get("questions") or []
    if not asked:
        return
    sid, tuid = p.get("session_id", ""), p.get("tool_use_id", "")
    if arm(sid) == "off":   # recorded, so that the answers of both arms are counted alike
        ledger.emit(kind="ask", phase="gate", src="hook", sid=sid, tuid=tuid, nq=len(asked), arm="off")
        return
    found = judged(asked, p) if judge and judge.decides("question") else None
    by = "judge" if found is not None else "patterns"
    if found is None:   # the judge does not decide questions, or could not read one: the patterns do
        cited = {n for q in asked if questions.owes_cases(q) for o in questions.real(q) for n in questions.cited(o)}
        unread = cited - transcripts.opened(p.get("transcript_path"), cited) if cited else set()
        found = questions.findings(asked, unread)
        shadow("question", lambda: [dict(id=judge.question_id(q), input=dict(question=q),
                                         patterns=bool(questions.findings([q], unread)))
                                    for q in asked if not q.get("multiSelect")], sid=sid, tuid=tuid)
    common = dict(phase="gate", src="hook", sid=sid, tuid=tuid, nq=len(asked), by=by)
    if not found:
        ledger.emit(kind="ask", **common)
        return
    common["checks"] = sorted(check.__name__ for check, _ in found)
    problems = sorted(f"{check.__name__}|{q}" for check, hits in found for q in hits)
    key = hashlib.sha1((sid + "".join(problems)).encode()).hexdigest()[:12]

    def attempt(state):
        tries = state.setdefault("denied", {})
        tries[key] = [tries.get(key, [0, 0])[0] + 1, int(time.time())]
        for k in [k for k, (_, t) in tries.items() if t < time.time() - 2 * 86400]:
            del tries[k]
        return tries[key][0]

    n = ledger.with_state(attempt)
    if n > ESCAPE_AFTER:
        ledger.emit(kind="ask_escaped", tries=n, **common)
        return
    eid = hashlib.sha1(f"{key}|{n}".encode()).hexdigest()[:6]   # named in the deny, for a correction
    ledger.emit(kind="ask_denied", tries=n, id=eid, **common)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": questions.deny_reason(found) + f" (gate event {eid})"}}))


def judged(asked, p):
    """What fails, decided by the judge: its reading of each single-select question for
    the rule's form, every question read at once; the length and the cited files checked in code.
    None when a reading fails, so that the patterns decide rather than nothing."""
    qs = [q for q in asked if not q.get("multiSelect")]
    if not qs:
        return []
    try:
        config = judge.configured("question")
        with ThreadPoolExecutor(len(qs)) as pool:
            draws = list(pool.map(lambda q: judge.draw("question", dict(input=dict(question=q)), config), qs))
        hits = {}   # check name: {question text: what is missing}
        cited = {}
        for q, (reading, meta) in zip(qs, draws):
            checks = judge.question_checks(q, reading)
            ledger.emit(kind="judged", phase="gate", src="decide", task="question", case=judge.question_id(q),
                        judge=judge.key(config), v=judge.version("question"), fails=bool(checks), checks=checks,
                        secs=meta["secs"], sid=p.get("session_id", ""), tuid=p.get("tool_use_id", ""))
            if reading["recommended"] or questions.previews(q):   # the rule's form is owed
                checks.update({"too_long": questions.too_long(q, set())} if questions.on_screen(q) > questions.MAX_CHARS else {})
                cited[q.get("question", "")] = {n.split(":")[0].split("/")[-1] for n in reading["read_files"]
                                                if questions.FILE.fullmatch(n.split(":")[0])} - questions.IN_CONTEXT
            for name, labels in checks.items():
                hits.setdefault(name, {})[q.get("question", "")] = labels
        names = set().union(*cited.values()) if cited else set()
        unread = names - transcripts.opened(p.get("transcript_path"), names) if names else set()
        for text, ns in cited.items():
            if ns & unread:
                hits.setdefault("read_unopened", {})[text] = [", ".join(sorted(ns & unread))]
        return [(check, hits[check.__name__]) for check in questions.CHECKS if check.__name__ in hits]
    except Exception:
        return None


def post(p):
    asked = (p.get("tool_input") or {}).get("questions") or []
    if not asked:
        return
    sid, tuid = p.get("session_id", ""), p.get("tool_use_id", "")
    res = p.get("tool_response")
    answers = res.get("answers") if isinstance(res, dict) else None
    if not isinstance(answers, dict) or not answers:
        ledger.emit(kind="answer_unparsed", phase="gate", src="hook", sid=sid, tuid=tuid)
        return
    t0 = ledger.asked_at(tuid, sid)
    secs = round(time.time() - t0, 1) if t0 else None
    for text, row in questions.answer_rows(asked, answers, res.get("annotations") or {}):
        ledger.emit(kind="answer", phase="gate", src="hook", sid=sid, tuid=tuid, secs=secs,
                    id=ledger.event_id(tuid, text), **row)


def started(transcript):
    """When the session began: the first timestamp in its transcript."""
    try:
        with open(transcript or "") as f:
            for line in f:
                ts = ledger.stamp(json.loads(line).get("timestamp"))
                if ts is not None:
                    return ts
    except (OSError, ValueError):
        pass
    return None


def began_turn(state, sid):
    turns = state.setdefault("turns", {})
    turns[sid] = time.time()
    for old in sorted(turns, key=turns.get)[:-500]:
        del turns[old]


def prompt(p):
    sid = p.get("session_id", "")
    if not sid:
        return
    ledger.with_state(lambda state: began_turn(state, sid))
    began, parts = started(p.get("transcript_path")), []
    for path, seen_key, applies, words in (
            (ledger.RULES, "injected", arm(sid) != "off",
             "Standing instruction of the user's, from their aidetic setup, in its current form (it "
             "changed after this session began); follow it from now on:\n\n"),
            (ledger.KEPT, "injected_kept", True,
             "Rules the user kept, from their aidetic setup, as they now read (they changed after this "
             "session began); follow them from now on:\n\n")):
        if not applies or not path.exists():
            continue
        mtime = path.stat().st_mtime
        if began is None or began >= mtime:
            continue   # the session was handed the file at start; a first prompt finds no transcript line yet

        def claim(state, key=seen_key, mtime=mtime):
            seen = state.setdefault(key, {})
            if seen.get(sid, -1) >= mtime:
                return False
            seen[sid] = mtime
            for old in sorted(seen, key=seen.get)[:-500]:
                del seen[old]
            return True

        if ledger.with_state(claim):
            ledger.emit(kind="rule_injected", phase="gate", src="hook", sid=sid, file=path.name)
            parts.append(words + path.read_text())
    if parts:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": "\n\n".join(parts)}}))


def stop(p):
    sid, text = p.get("session_id", ""), p.get("last_assistant_message") or ""
    if arm(sid) == "off":
        return
    hit = False if judging_reply() else questions.names_pick(text)   # with `reply` installed, the judge decides
    if p.get("stop_hook_active") or not (hit or shadowing()):
        return
    since = ledger.read_state().get("turns", {}).get(sid)
    if since is None or ledger.asked_since(sid, since):
        return   # a turn the prompt hook never saw, or one that asked through the tool
    if text.strip() and not (p.get("cwd") or "").startswith("/tmp/"):   # headless runs are not read
        shadow("reply", lambda: [dict(id=judge.reply_id(sid, text), input=dict(reply=text), patterns=hit)], sid=sid)
    if not hit:
        return
    eid = hashlib.sha1(f"{sid}|{time.time()}".encode()).hexdigest()[:6]
    ledger.emit(kind="prose_sent_back", phase="gate", src="hook", sid=sid, id=eid)
    print(json.dumps({"hookSpecificOutput": {   # feedback, not an error: Claude Code shows it as such
        "hookEventName": "Stop", "additionalContext": questions.PROSE + f" (gate event {eid})"}}))


def reply(p):
    """The judge on *reply*, which Claude Code runs once the turn has ended, so that the
    user waits for nothing: a reply that names its pick in prose, with nothing asked through the tool
    since the turn began, is sent back once by exiting 2, which wakes the session with what this
    writes to stderr. The turn the wake starts has `stop_hook_active` and is let through. Returns the
    exit code."""
    sid, text = p.get("session_id", ""), p.get("last_assistant_message") or ""
    if not text.strip() or p.get("stop_hook_active") or arm(sid) == "off" or judge is None:
        return 0
    since = ledger.read_state().get("turns", {}).get(sid)
    if since is None or ledger.asked_since(sid, since):
        return 0   # a turn the prompt hook never saw, or one that asked through the tool
    config, case = judge.configured("reply"), dict(input=dict(reply=text))
    common = dict(phase="gate", src="decide", task="reply", case=judge.reply_id(sid, text), sid=sid)
    try:
        reading, meta = judge.draw("reply", case, config)
    except Exception as e:   # recorded, so a backend that cannot judge shows in the ledger
        ledger.emit(kind="judge_failed", error=questions.short(e, 300), **common)
        return 0
    fails, quote = judge.verdict("reply", case, reading), questions.short(reading.get("quote"), 200)
    ledger.emit(kind="judged", judge=judge.key(config), v=judge.version("reply"), fails=fails, quote=quote,
                secs=meta["secs"], **common)
    if not fails:
        return 0
    if (ledger.read_state().get("turns", {}).get(sid) or 0) > since:
        ledger.emit(kind="judge_late", phase="gate", src="judge", sid=sid)   # the user wrote again meanwhile
        return 0
    eid = hashlib.sha1(f"{sid}|{time.time()}".encode()).hexdigest()[:6]
    ledger.emit(kind="judge_sent_back", phase="gate", src="judge", sid=sid, id=eid)
    print((f"“{quote}”: " if quote else "") + questions.PROSE + f" (gate event {eid})", file=sys.stderr)
    return 2


def learn(p):
    import loop   # a loop that cannot load must not take the gate with it
    return loop.learn(p)


def watch(p):
    import loop
    return loop.watch(p)


SETTINGS = ledger.HOME / ".claude" / "settings.json"
WATCHED = ledger.DIR / "watched.json"   # the matchers of `watch` for the plugin, which has no settings entries
REPLY_HOOK = {"asyncRewake": True, "timeout": 120, "rewakeSummary": "aidetic sent the reply back",
              "rewakeMessage": "The user's aidetic gate sent your last reply back:"}


def ours(h):
    """The judge's hook for *reply*: the `reply` command, or the prompt hook it replaced,
    known by the gate event that one's send-backs named."""
    return (h.get("type") == "command" and (h.get("command") or "").endswith("choice_gate.py reply")
            or h.get("type") == "prompt" and "(gate event reply)" in (h.get("prompt") or ""))


def judging_reply():
    """The judge decides *reply* (`aidetic hook`); a settings file that cannot be read says no."""
    try:
        return any(ours(h) for entry in json.loads(SETTINGS.read_text()).get("hooks", {}).get("Stop", [])
                   for h in entry.get("hooks", []))
    except Exception:
        return False


def write_hook(on):
    """Put `reply` among the Stop hooks of settings.json, run the way the gate's own `stop` is (on),
    or take it out; either way the prompt hook it replaced goes. Returns whether the file changed."""
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    stops = settings.setdefault("hooks", {}).setdefault("Stop", [])
    before = json.dumps(stops, sort_keys=True)
    for entry in stops:
        entry["hooks"] = [h for h in entry.get("hooks", []) if not ours(h)]
    stops[:] = [e for e in stops if e.get("hooks")]
    if on:
        gate = gate_command(stops)
        stops.append({"hooks": [{"type": "command", "command": f"{gate} reply", **REPLY_HOOK}]})
    if json.dumps(stops, sort_keys=True) == before:
        return False
    save_settings(settings)
    return True


def save_settings(settings):
    """Written beside and renamed over, so a reader never sees half."""
    tmp = SETTINGS.with_suffix(".json.aidetic-tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    tmp.replace(SETTINGS)


def gate_command(hooks):
    """How the installer's settings run this gate: the command of its own `stop` hook, without the
    subcommand; the path of this file when there is none."""
    return next((h["command"].split(" || ")[0].rsplit(" ", 1)[0] for e in hooks for h in e.get("hooks", [])
                 if (h.get("command") or "").split(" || ")[0].endswith("choice_gate.py stop")),
                f"python3 {Path(__file__).resolve()}")


def settings_carry_gate():
    """The settings hooks run the gate: settings.json names it, which is also when the plugin stands
    down, so that one carrier records at a time. Missing or unreadable, they do not."""
    try:
        return "choice_gate.py" in SETTINGS.read_text()
    except OSError:
        return False


def write_watched(matchers):
    """The plugin's `watch` hook reads these at each tool call. Written beside and renamed over."""
    text = json.dumps(matchers) + "\n"
    if WATCHED.exists() and WATCHED.read_text() == text:
        return False
    WATCHED.parent.mkdir(parents=True, exist_ok=True)
    tmp = WATCHED.with_suffix(".json.aidetic-tmp")
    tmp.write_text(text)
    tmp.replace(WATCHED)
    return True


def write_watch(acts):
    """The PreToolUse entries of `watch` in settings.json: one for each point of action a habit is
    watched at (loop.MATCHERS), none when no habit is. With the plugin carrying the gate, no entry in
    settings.json, which would stand it down: the matchers go to watched.json. Returns whether the
    file changed."""
    import loop
    if not settings_carry_gate():
        return write_watched(list(dict.fromkeys(loop.MATCHERS[a] for a in acts)))
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    pres = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    before = json.dumps(pres, sort_keys=True)
    mine = lambda h: (h.get("command") or "").split(" || ")[0].endswith("choice_gate.py watch")
    for entry in pres:
        entry["hooks"] = [h for h in entry.get("hooks", []) if not mine(h)]
    pres[:] = [e for e in pres if e.get("hooks")]
    gate = gate_command(settings["hooks"].get("Stop", []))
    for matcher in dict.fromkeys(loop.MATCHERS[a] for a in acts):
        pres.append({"matcher": matcher, "hooks": [{"type": "command", "command": f"{gate} watch || true",
                                                    "timeout": 90}]})
    if json.dumps(pres, sort_keys=True) == before:
        return False
    save_settings(settings)
    return True


def install_hook(args):
    if not settings_carry_gate():
        sys.exit("aidetic hook: the plugin carries `reply`; `claude plugin disable aidetic` turns it off")
    if "--off" not in args and judge is None:
        sys.exit("aidetic hook: judge.py cannot be loaded")
    changed = write_hook("--off" not in args)
    print(("removed" if "--off" in args else "installed") + (" the judge's `reply` hook" if changed else ": no change"))


STARTING_JUDGE = {"backend": "cli", "model": "opus", "effort": "high", "timeout": 60,
                  "decide": {"question": {}, **{t: {"model": "sonnet", "effort": "high"}
                                                for t in ("reply", "undone", "learn", "habits")}}}


def install(args):
    """The whole setup, one line a step, and safe to run again (`install` above). With `--plugin` the
    hooks are the plugin's: settings.json is left as it is."""
    plugin = "--plugin" in args
    here = Path(__file__).resolve()
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    hooks = settings.setdefault("hooks", {})
    gate, put = gate_command(hooks.get("Stop", [])), []
    for event, entries in ({} if plugin else json.loads((here.parent / "hooks.json").read_text())["hooks"]).items():
        have = hooks.setdefault(event, [])
        for entry in entries:
            h = {**entry["hooks"][0], "command": entry["hooks"][0]["command"].replace("python3 REPO/choice_gate.py", gate)}
            sub = h["command"].split(" || ")[0].rsplit(" ", 1)[1]
            if any(e.get("matcher") == entry.get("matcher") and h in e.get("hooks", []) for e in have):
                continue
            mine = lambda x: ((x.get("command") or "").split(" || ")[0].endswith(f"choice_gate.py {sub}")
                              or sub == "reply" and ours(x))
            placed = False   # where it stands, if it is there in another form; at the end if not
            for e in have:
                kept = []
                for x in e.get("hooks", []):
                    if not mine(x):
                        kept.append(x)
                    elif not placed and e.get("matcher") == entry.get("matcher"):
                        kept.append(h)
                        placed = True
                e["hooks"] = kept
            have[:] = [e for e in have if e.get("hooks")]
            if not placed:
                have.append({**{k: v for k, v in entry.items() if k != "hooks"}, "hooks": [h]})
            put.append(sub)
    if put:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        save_settings(settings)
    if plugin:   # two carriers of one gate record every event twice: the plugin stands down while settings name it
        print(f"hooks: {SETTINGS} still names the gate, so the plugin stands down until those entries are removed"
              if "choice_gate.py" in json.dumps(hooks) else f"hooks: the plugin's own; {SETTINGS} left as it is")
    else:
        print(f"hooks: {', '.join(put)} put in {SETTINGS}" if put else f"hooks: all in place in {SETTINGS}")
    link = ledger.HOME / ".local" / "bin" / "aidetic"
    if link.exists() or link.is_symlink():
        print(f"command: {link} is there already, to {os.path.realpath(link)}")
    else:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(here)
        print(f"command: {link} linked to {here}")
    if str(link.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"         {link.parent} is not on your PATH; add it, or Claude cannot run `aidetic`")
    config = ledger.DIR / "judge.json"
    if config.exists():
        print(f"judge: {config} kept")
    else:
        ledger.DIR.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps(STARTING_JUDGE) + "\n")
        print(f"judge: {config} written, Opus on questions and Sonnet on the rest, on your Claude login")
    import loop
    if loop.state().get("habits"):
        print("habits: known already (`aidetic rule list`)")
    elif sys.stdin.isatty() and input("habits: read the messages you typed in the past four weeks now? About 20 "
                                      "minutes for 2,000 messages. [y/N] ").strip().lower()[:1] == "y":
        import habits
        habits.main([])
    else:
        print("habits: run `aidetic habits` once, to read the past four weeks and seed the loop")
    print("done: new sessions pick it up")


def shadowing():
    """judge.json turns the shadow on; a judge that cannot load or a config it cannot read leaves it off."""
    try:
        return bool(judge and judge.configured().get("shadow"))
    except Exception:
        return False


def shadow(task, cases, **src):
    """Hand the cases (built by cases()) to the judge in a detached process, so the hook waits for
    nothing; only while the shadow is on, and never at the gate's expense."""
    if not shadowing():
        return
    try:
        cases = cases()
        if not cases:
            return
        ledger.DIR.mkdir(parents=True, exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="shadow-", suffix=".json", dir=ledger.DIR)
        with os.fdopen(fd, "w") as f:
            json.dump(dict(task=task, cases=cases, src=src), f)
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "shadow", path], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def run_shadow(path):
    """The detached half of shadow(): one draw of the configured judge on each case, recorded as a
    `judged` event beside what the patterns decided, or as `judge_failed`."""
    job = json.loads(Path(path).read_text())
    Path(path).unlink()
    task, config = job["task"], judge.configured()
    for case in job["cases"]:
        common = dict(phase="gate", src="shadow", task=task, case=case["id"], judge=judge.key(config),
                      v=judge.version(task), patterns=case["patterns"], **job["src"])
        try:
            reading, meta = judge.draw(task, case, config)
            extra = ({"checks": judge.question_checks(case["input"]["question"], reading)} if task == "question"
                     else {"quote": questions.short(reading.get("quote"), 200)})
            ledger.emit(kind="judged", fails=judge.verdict(task, case, reading), secs=meta["secs"], **extra, **common)
        except Exception as e:   # recorded, so a backend that cannot judge shows in the ledger
            ledger.emit(kind="judge_failed", error=questions.short(e, 300), **common)


HOOKS = {"start": start, "pre": pre, "post": post, "prompt": prompt, "stop": stop, "reply": reply,
         "learn": learn, "watch": watch}


def main():
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else "stats"), sys.argv[2:]
    if cmd in HOOKS:
        try:
            code = HOOKS[cmd](json.loads(sys.stdin.read() or "{}"))
        except Exception:   # fail open
            code = 0
        sys.exit(code or 0)
    if cmd == "shadow":
        run_shadow(args[0])
        return
    if cmd == "habits":
        import habits
        habits.main(args)
        return
    if cmd in ("rule", "watched"):
        import loop
        loop.rule(args) if cmd == "rule" else loop.watched(args[0])
        return
    import report   # the hooks never load the transcript scan
    commands = {"stats": report.stats, "recent": report.recent, "reversals": report.reversals,
                "arms": report.arms, "regret": report.regret, "correct": report.correct, "hook": install_hook,
                "install": install}
    if cmd not in commands:
        sys.exit(__doc__)
    commands[cmd](args)


if __name__ == "__main__":
    main()
