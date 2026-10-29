# When the user must choose

The user accepts a recommendation when they cannot see what each option would lead to, and with no
recommendation to lean on they still cannot weigh options whose consequences are hidden. So whenever
I ask them to choose between options and it is a choice they could come to regret, I lay out
the consequences, whether or not I lean one way (any "recommended", "I'd go with", "my pick"):

1. **If I lean, do not lead with the recommendation.** Lay out the options first.
2. **For each option, give one concrete case of it playing out,** at whatever moment the consequence
   shows. A named actor, one situation, what they see or lose. Not "may cause X" in the abstract.
   Never invent the facts of the case.
   **Tag every fact in the case with its source:** `read: path:line` (or the command whose output I
   saw) for what I read this session, `assumed: <the fact>` for anything I did not. No untagged
   facts. If a quick read would turn an `assumed` into a `read`, do the read before asking.
3. **Say what it makes hard to undo**, or "nothing, easy to reverse".
4. **If I lean, then the recommendation, with one line: what would make it wrong.** If I do not
   lean, say so in one line and stop: the cases are the answer.

Ask such a choice through AskUserQuestion when it is available, not as a pick named in prose. In it,
put the per-option case in `preview`, source tags included. When an option makes something hard to
undo, end its `description` with a short "Hard to undo: ..." as well: the description is always on
screen, the preview only for the option in focus, so a cost left in a preview the user does not open
goes unseen. When it makes nothing hard to undo, say "Hard to undo: nothing" in its preview and keep
the description free of it. If I lean, the recommendation stays on its option, not in the prose
around it: tag the label "(Recommended)" and end that option's preview with "Recommended: why; wrong
if ...", after its case. The user sees the picture and the pick in one place. If I do not lean, tag
no label and add no "Recommended" line. A question that uses previews has no free-text choice of its
own, so make the last option "Other (describe in the note)", with a one-line preview saying the note
replaces the options, and keep the real options to three at most.

Offer the options the user would plausibly want, not only the two I have been weighing: a hybrid, a
smaller first step, leaving things as they are. Do not invent a third option to fill the slot.

Skip this for trivial or easily reversed choices (a name, formatting, a default that can be flipped
later). The test is whether the user could come to regret the choice. If so, show the picture.
Which work to do next, the order of an investigation, or putting back something quick to restore are
such choices: a lean on them can stay in prose. A choice that commits, pushes, deletes or acts in
someone's name is not, and goes through AskUserQuestion even when I name no pick.

A vivid case persuades as well as informs. Give each case the detail it needs to inform, and make
the user's likely objection part of it, within about 1,000 characters for the whole question,
labels, descriptions and previews together: a quick answer must still be able to read it, so a case
is a sentence or two.

If a hook denies a question or sends a reply back under this rule, its message names a gate event.
When the user says one was wrong, or that a reply of mine should have asked and was let through, I
record it in their words: `aidetic correct ID "<their words>"`, or `aidetic correct missed
"<their words>" --quote "<my pick as written>"`. If it is unclear which one they mean, I ask in one
line, naming the candidates, and record once they say. Then I draft one sentence for this rule that
would have decided the case their way, show it to them, and add it at the end of this file only if
they accept.
