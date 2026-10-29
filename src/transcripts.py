"""Reading the transcripts Claude Code keeps under ~/.claude/projects: a session as the sequence the
scans and `habits` need, and which file names a session has opened so far, for the gate."""
import json

import ledger

NOT_TYPED = ("<", "Another Claude session", "This session is being continued", "[Request interrupted")
RESULT = '"tool_result"'   # a tool's output: the bulk of a transcript, and never needed by the scans
ANSWERED = ('"answers"', '"questions"')   # among the outputs, the one of a question that was answered


def read_session(fn, answers=False, acts=False):
    """[(time, who, payload)] in order; who is ask (payload: the tool_use), self (the session's
    text) or user (what the user typed: not hand-backs, summaries or system text). With answers,
    also answer (payload: the question tool's result, with the `tool_use_id` it answers) and stop
    (the session stopped and its Stop hooks ran; payload None). With acts, also act (payload: the
    tool_use of every other tool call)."""
    out = []
    with open(fn, errors="ignore") as f:
        lines = [line for line in f
                 if RESULT not in line or (answers and all(a in line for a in ANSWERED))]
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        t = ledger.stamp(o.get("timestamp"))
        if answers and o.get("subtype") == "stop_hook_summary":
            out.append((t, "stop", None))
            continue
        result = o.get("toolUseResult")
        if answers and RESULT in line:
            if isinstance(result, dict) and isinstance(result.get("answers"), dict):
                use = next((b.get("tool_use_id") for b in (o.get("message") or {}).get("content") or []
                            if isinstance(b, dict) and b.get("type") == "tool_result"), None)
                out.append((t, "answer", {**result, "tool_use_id": use}))
            continue
        att = o.get("attachment") or {}
        if att.get("type") == "queued_command" and isinstance(att.get("prompt"), str):
            out.append((t, "user", att["prompt"]))   # typed while the session was working
        if o.get("isMeta") or o.get("isCompactSummary"):
            continue
        content = (o.get("message") or {}).get("content")
        if o.get("type") == "user" and isinstance(content, str):
            out.append((t, "user", content))
            continue
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                out.append((t, "ask", b))
            elif acts and b.get("type") == "tool_use":
                out.append((t, "act", b))
            elif b.get("type") == "text" and o.get("type") in ("assistant", "user"):
                out.append((t, "self" if o["type"] == "assistant" else "user", b.get("text", "")))
    return [(t, who, x) for t, who, x in out
            if t is not None and not (who == "user" and x.lstrip().startswith(NOT_TYPED))]


def opened(transcript, names):
    """The names among these that a tool call or its output has shown in the session so far. The
    questions themselves do not count. An unreadable transcript opens everything: fail open."""
    found = set()
    try:
        with open(transcript or "", errors="ignore") as f:
            for line in f:
                if found == names:
                    break
                if '"AskUserQuestion"' in line or ('"tool_use"' not in line and RESULT not in line):
                    continue
                found |= {n for n in names - found if n in line}
    except OSError:
        return set(names)
    return found
