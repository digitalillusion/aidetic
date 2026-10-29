"""Reading an AskUserQuestion call: what RULE.md asks of its input, as checks, and its answer as a
ledger row; and a reply that names its pick in prose. Pure functions over the tool's JSON and the
reply's text; no I/O."""
import re

MIN_PREVIEW = 60   # chars; a placeholder such as "fine" or "see above" does not count as a case
MAX_CHARS = 1000   # a question's characters, read in 40 s at 25 a second
REC = re.compile(r"\(recommended\)", re.I)
OTHER = re.compile(r"^\s*(other|something else|none of|neither)\b", re.I)
STATES_REC = re.compile(r"recommend|wrong if|would make (it|this) wrong", re.I)
HARD = re.compile(r"hard to undo\s*:", re.I)
NOTHING = re.compile(r"hard to undo\s*:\s*(nothing|none)\b", re.I)
READ_TAG = re.compile(r"read:\s*([^)\]\n]+)")
FILE = re.compile(r"[\w.~/-]*[\w-]+\.(?:md|py|rs|tsx?|jsx?|jsonl?|toml|ya?ml|log|txt|sql|proto|[tc]sv|sh|html|"
                  r"css|tex|go|java|kt|rb|cpp|[ch])\b")   # a name with a file's extension, not an API path
IN_CONTEXT = {"CLAUDE.md", "MEMORY.md", "RULE.md"}   # loaded with the system prompt, not by a tool
PICK = re.compile(r"\b(I recommend|I'd recommend|I would recommend|my recommendation|my pick|I'd go with|"
                  r"I would go with|I lean|I'd lean|I suggest|I'd suggest|I would suggest)\b", re.I)


def short(text, n=160):
    return " ".join(str(text or "").split())[:n]


def plain(label):
    return REC.sub("", label or "").strip()


def options(q):
    return q.get("options") or []


def real(q):
    """The options other than the Other one."""
    return [o for o in options(q) if not OTHER.search(o.get("label", ""))]


def recommended(q):
    return next((o for o in options(q) if REC.search(o.get("label", ""))), None)


def mentions_recommendation(q):
    return any("recommend" in f"{o.get('label', '')} {o.get('description', '')}".lower()
               for o in options(q))


def previews(q):
    return sum(1 for o in options(q) if (o.get("preview") or "").strip())


def on_screen(q):
    """The characters a question puts up: its text, and every option's label, description and preview."""
    return len(q.get("question", "")) + sum(len(o.get(k) or "") for o in options(q)
                                            for k in ("label", "description", "preview"))


def owes_cases(q):
    """A single-select question that recommends or already shows cases owes the rule's form."""
    return not q.get("multiSelect") and (previews(q) > 0 or mentions_recommendation(q))


def cited(o):
    """The file names an option's `read:` tags cite."""
    names = {f.split(":")[0].rstrip(".,").split("/")[-1]
             for tag in READ_TAG.findall(o.get("preview") or "") for f in FILE.findall(tag)}
    return {n for n in names if n and n not in IN_CONTEXT}


# ---- the checks: each takes the question and the cited names its session never opened, and
#      returns what is missing, as option labels ------------------------------------------------

def without_case(q, unread):
    if not mentions_recommendation(q):
        return []
    return [o.get("label", "?") for o in real(q) if len((o.get("preview") or "").strip()) < MIN_PREVIEW]


def recommendation_unsaid(q, unread):
    o = recommended(q)
    text = ((o or {}).get("preview") or "").strip()
    return [o["label"]] if o and len(text) >= MIN_PREVIEW and not STATES_REC.search(text) else []


def cost_hidden(q, unread):
    """A cost belongs in the description, which is always on screen; "nothing" may stay in the preview."""
    return [o.get("label", "?") for o in real(q)
            if not HARD.search(o.get("description") or "") and not NOTHING.search(o.get("preview") or "")]


def no_other(q, unread):
    return [] if len(real(q)) < len(options(q)) else ["Other"]


def too_long(q, unread):
    n = on_screen(q)
    return [f"{n} characters"] if n > MAX_CHARS else []


def read_unopened(q, unread):
    return [f"{o.get('label', '?')} ({', '.join(sorted(cited(o) & unread))})"
            for o in real(q) if cited(o) & unread]


CHECKS = {   # check: what the deny says, before the list of what it found
    without_case:
     "A question here recommends an option but its options carry no concrete `preview`. "
     "Per the choice rule, before asking: for each option write a case of it playing out in "
     f"`preview` (a named actor, one situation, what they see or lose; at least {MIN_PREVIEW} "
     "characters), say what it makes hard to undo, and state what would make the recommendation "
     "wrong. Keep the recommendation on its option: the “(Recommended)” tag on the label, and as "
     "the last line of that option's preview, after its case, “Recommended: why; wrong if …”. If "
     "the choice is trivial or easy to reverse, drop the recommendation and ask again. "
     "Missing previews",
    recommendation_unsaid:
     "A question here tags an option “(Recommended)” but its preview does not carry the "
     "recommendation. Keep it on the option: after the case, end that preview with "
     "“Recommended: why; wrong if …”. Options",
    cost_hidden:
     "A question here uses previews, but a preview shows only for the option in focus, so a cost "
     "written there can go unseen. End each real option's `description`, which is always on "
     "screen, with one short “Hard to undo: …”. When the option makes nothing hard to undo, write "
     "“Hard to undo: nothing” in its preview instead and leave the description without it. Options",
    no_other:
     "A question here uses previews, and then the tool offers no free-text choice of its own. "
     "Make the last option “Other (describe in the note)”, with a one-line preview saying the "
     "note replaces the options, and keep the real options to three at most. Offer the options "
     "the user would plausibly want, a hybrid or a smaller first step included, not only the two "
     "you weighed. Questions without it",
    read_unopened:
     "A `read:` tag names a file this session has not opened, and `read:` is for what was read "
     "here. Open it before asking, or tag the fact `assumed:`. Options",
    too_long:
     f"A question here puts more than {MAX_CHARS:,} characters on screen across its labels, "
     "descriptions and previews, more than a quick answer reads. Shorten each case to a sentence "
     "or two, keeping who meets the option, the moment and what they see or lose, with its tags, "
     "and keep descriptions short. Questions",
}


def findings(questions, unread=frozenset()):
    """[(check, {question: missing labels})] for the checks that fail; empty when all pass."""
    out = []
    for check in CHECKS:
        hits = {q.get("question", ""): check(q, unread) for q in questions if owes_cases(q)}
        hits = {k: v for k, v in hits.items() if v}
        if hits:
            out.append((check, hits))
    return out


def deny_reason(found):
    return " ".join(f"{CHECKS[check]}: " + "; ".join(f"“{short(q, 60)}”: {', '.join(v)}"
                                                     for q, v in hits.items()) + "."
                    for check, hits in found)


# ---- a reply in prose ------------------------------------------------------------------------

def names_pick(text):
    """The reply names Claude's pick in so many words."""
    return PICK.search(text or "") is not None


PROSE = ("This reply names your pick in prose (“I'd go with”, “my recommendation”, “I lean” or their "
         "kin) and nothing was asked through AskUserQuestion this turn, so the user meets the "
         "recommendation without the cases beside it. If it is a choice the user could come to regret, "
         "put it to them now through AskUserQuestion in the form RULE.md asks, cases in the previews and "
         "the recommendation on its option, without restating the pick in prose first. If the phrase was "
         "not a pick (text about recommendations, a report, a quotation) or the choice is trivial or easy "
         "to reverse, say so in one line and stop.")


# ---- the answer ----------------------------------------------------------------------------

def unanswered(e):
    """The user sent a message instead of picking."""
    return e.get("ans", "").startswith("[User did not answer")


def answer_rows(questions, answers, annotations):
    """One ledger row per answered question: what was picked against what was recommended."""
    for q in questions:
        text = q.get("question", "")
        if text not in answers:
            continue
        ans, rec = answers[text], recommended(q)
        labels = [plain(o.get("label")) for o in options(q)]
        note = (annotations.get(text) or {}).get("notes")
        single = not q.get("multiSelect")
        yield text, dict(
            q=short(text), hdr=short(q.get("header"), 40), nopt=len(labels), nreal=len(real(q)),
            multi=not single, rec=plain(rec["label"]) if rec else None,
            mentioned=mentions_recommendation(q), prev=previews(q), ans=short(ans),
            chars=on_screen(q),
            other=single and (plain(ans) not in labels or OTHER.search(plain(ans)) is not None),
            follow=plain(ans) == plain(rec["label"]) if rec and single else None,
            notes=bool(note), note=short(note, 200))
