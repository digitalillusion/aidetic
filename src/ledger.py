"""The ledger: events.jsonl, appended one JSON line per event, and state.json, read and written
under a lock. Everything else reads and writes through here."""
import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
DIR = HOME / ".claude" / "aidetic"
EVENTS, STATE = DIR / "events.jsonl", DIR / "state.json"
RULES = Path(__file__).resolve().parent.parent / "RULE.md"   # the rule ships with the gate
KEPT = DIR / "rules.md"   # the rules the user kept through `aidetic rule` (loop.py): theirs, not the repository's
PROJECTS = Path(os.environ.get("CHOICE_PROJECTS", HOME / ".claude" / "projects"))


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stamp(ts):
    """Epoch seconds of an ISO timestamp, the ledger's or a transcript's ("…Z"); None if unreadable."""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def event_id(tuid, question):
    return hashlib.sha1(f"{tuid}|{question}".encode()).hexdigest()[:6]


def emit(**fields):
    DIR.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"t": now(), **fields}, ensure_ascii=False) + "\n"
    fd = os.open(EVENTS, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode())   # one write per line: concurrent sessions do not interleave
    finally:
        os.close(fd)


def load():
    out = []
    try:
        with open(EVENTS) as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def read_state(path=None):
    try:
        return json.loads((path or STATE).read_text())
    except (OSError, ValueError):
        return {}


def with_state(update, path=None):
    """Run update(state) under the lock and save what it leaves; returns its result. `path` names
    another such file than state.json, with a lock of its own beside it."""
    path = path or STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(path)
        result = update(state)
        path.write_text(json.dumps(state))
        return result


ASKED = ("ask", "ask_denied", "ask_escaped")   # the gate saw an AskUserQuestion call


def tail():
    """The ledger's latest events, newest first."""
    try:
        with open(EVENTS, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400_000))
            lines = f.read().decode(errors="ignore").splitlines()
    except OSError:
        return
    for line in reversed(lines):
        try:
            yield json.loads(line)
        except ValueError:
            continue


def asked_at(tuid, sid):
    """When the gate saw this tool call, from the tail of the ledger; None if it did not."""
    for e in tail():
        if e.get("kind") in ASKED and (e.get("tuid") == tuid if tuid else e.get("sid") == sid):
            return stamp(e["t"])
    return None


def asked_since(sid, since):
    """Whether this session put a question through the tool at or after `since`, epoch seconds."""
    for e in tail():
        if (stamp(e.get("t")) or 0) < since - 1:   # the ledger's times are whole seconds
            return False
        if e.get("sid") == sid and e.get("kind") in ASKED + ("answer",):
            return True
    return False
