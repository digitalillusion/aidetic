# aidetic

Hooks for [Claude Code](https://claude.com/claude-code) that watch how you work with Claude, learn
from what you correct, and step in only where your own record says it pays. Everything runs on your
machine, from your own transcripts; nothing leaves it except to the model judge you choose, and
nothing it learns about you goes into the repository.

## What it does

**Habits and the loop.** aidetic reads the messages you type to Claude and finds the ones that
object to something Claude already did ("I didn't ask you to commit") or forestall it ("don't
implement yet, just tell me"). It groups them into habits, and prices each habit against how often
the point where it happens comes up, so you can see which ones a guard could pay for at all.

Then it runs on its own:

1. At the end of every turn it reads your newest message in the background; you never wait for it.
2. On a habit's 2nd, 4th, 8th, 16th … signal, Claude asks you once whether to keep a short
   rule for it, drafted from your own words, and says which projects the habit's signals came
   from. You keep it for every project or only for the one you are in. A habit about the subject
   of the work rather than how Claude works with you, such as how a bot you are building should
   play its game, is not put to you. Only you add a rule; declined, it is asked again when the
   count has doubled, and answered "never", that habit is not asked about again. You can also
   write a rule of your own (`aidetic rule add`), and move any kept rule between projects later.
3. A kept rule is handed to every new session in a project it applies to. At the end of each turn
   one judge call then reads the whole turn, every tool call and the reply, against the rules kept
   for that project, with everything you wrote in the session, your answers to questions included,
   and records a verdict on each rule, deciding nothing. Your own rules are handed over, not
   watched.
4. A guard switches itself on only when its verdicts, set against your own later objections, clear
   fixed bars (at least 40 watched turns and 3 you objected to, two thirds of those caught, few
   wrong flags), and off again when you correct it twice. On, it sends a turn that breaks the rule
   back to Claude. It tells you in one line when it switches.

A project is known by its git remote `origin`, so the clones and worktrees of one repository share
their rules; a repository without one by its main folder; a folder outside git by itself.

**The choice rule and its gate.** When Claude asks you to choose, it shows each option as one
concrete case with its facts sourced, says what each makes hard to undo, and puts its
recommendation last. A gate on the question tool denies a question that skips that, and a
background check sends back a pick written in prose instead of asked. The rule's text is
`RULE.md`; a correction you accept is added at its end.

**The judge.** Every check that reads language is done by a model, not by pattern matching, so it
works in any language: through `claude -p` on your own login, or through Anthropic's API or any
OpenAI-compatible one with a key. After a call fails, the hooks leave that task of the judge
alone for a minute, then twice as long after each next failure in a row, up to an hour, so that a
spent usage limit is not called at every hook; meanwhile the gate falls back to its patterns. The repository
also builds an evaluation set from your own transcripts and measures any judge against it.

## Requirements

Claude Code with its `claude` command logged in, and Python 3.10 or newer. Nothing to install from
PyPI unless you choose the `anthropic` or `openai` backend, which import their package. The plugin
route needs a Claude Code that loads plugin hook modules (tried on 2.1.312).

## Install

aidetic is a set of hooks, and Claude Code can carry them in two ways. Which one works depends on
your account:

| Your Claude Code account | Install with |
|---|---|
| A **Team or Enterprise organization** | [`./install.sh`](#with-installsh-any-account): hooks in `settings.json` |
| Any other account (tried on Max) | `./install.sh`, or [the plugin](#as-a-plugin-accounts-outside-a-team-or-enterprise-organization), which leaves `settings.json` alone |

Under a Team or Enterprise organization Claude Code seats a policy plugin that bypasses the
`classic.*` events for any user plugin. Those are the only events a plugin gets that carry the
session id and the transcript path the ledger is keyed on, so the plugin records nothing there.
Ordinary hooks in `settings.json` are not bypassed.

### With `install.sh` (any account)

This is the route for a Team or Enterprise organization. Clone this repository anywhere, and from
the clone run:

```sh
./install.sh
```

It puts the hooks of `src/hooks.json` into `~/.claude/settings.json`, keeping any hooks you already
have; links the `aidetic` command into `~/.local/bin`, which must be on your `PATH`, since the rule
and the loop tell Claude to run it when you answer them; writes `~/.claude/aidetic/judge.json` if
there is none; and, at a terminal, offers to read the messages you typed in the past four weeks
(about 20 minutes for 2,000 messages on Sonnet), which groups what it finds into habits and seeds
the loop with them. Run it again after an update, as `aidetic install`: only what is missing or
differs changes. A new session then picks everything up.

The judge it writes runs on your own Claude login, Opus on questions and Sonnet on the rest:

```json
{"backend": "cli", "model": "opus", "effort": "high", "timeout": 60,
 "decide": {"question": {}, "reply": {"model": "sonnet", "effort": "high"},
            "undone": {"model": "sonnet", "effort": "high"},
            "learn": {"model": "sonnet", "effort": "high"},
            "habits": {"model": "sonnet", "effort": "high"}}}
```

Without a `judge.json` every task runs on Haiku at low effort, which is cheap but weak at these
tasks.

Under an organization, one setting can still stop it: managed settings that allow only managed
hooks (`allowManagedHooksOnly`) load no user hooks of any kind, whichever way they are installed.
Your administrator would have to ship aidetic as a managed hook. To check that the hooks run, start
a session with `claude --debug` and look for the lines of `choice_gate.py`, or look in
`~/.claude/aidetic/state.json`, where a session's `turns` entry appears when its first prompt is
sent.

If you also installed the plugin on such an account, run `claude plugin disable aidetic`: its hooks
are bypassed there, and each skipped hook adds a notice to the model's context.

### As a plugin (accounts outside a Team or Enterprise organization)

The repository is also a Claude Code plugin (`.claude-plugin/`, `hooks/`): the same gate, run by a
hooks module from the plugin's own `src/`, with nothing merged into `settings.json`.

```sh
claude plugin marketplace add <this clone, or owner/repo>
claude plugin install aidetic@aidetic
```

The marketplace is the clone itself, so the plugin runs its working tree, as the settings hooks do.
The first session after it writes what hooks cannot give, the `aidetic` command in `~/.local/bin`
and `~/.claude/aidetic/judge.json` (the one above; `aidetic install --plugin` does the same by
hand), and never over what is there.

Check in a new session with `claude --debug` that the plugin's hooks run: a line `aidetic: classic.SessionStart
bypassed by cc-plugin-sec-default` means an organization's policy skips them, and the settings route
above is the one to use.

The plugin and the settings hooks are two carriers of one gate, and each would record every event:
while `settings.json` names the gate (the entries of `src/hooks.json`), the plugin stands down, and
the gate writes no settings entries of its own. To move from the settings hooks to the plugin,
install the plugin, which stands down, check in `claude --debug` of a new session that its hooks
run, remove those entries from `settings.json`, and check that a new session's turn start reaches
`state.json`; if it does not, put the entries back. The loop works there too, its turn judge
included, from the plugin's Stop hook. `aidetic hook` is for the settings hooks; the plugin carries
`reply`.

To work on it: `claude --plugin-dir <clone>` loads it for one session, and `claude plugin test
<clone>` runs `hooks/register.test.ts`.

## Settings in `judge.json`

| Key | What it does |
|---|---|
| `backend` | `cli` (`claude -p` on your login), `anthropic` or `openai` (an API key) |
| `model`, `effort`, `timeout` | the judge's model, effort and seconds per call, for every task |
| `key_env`, `base_url` | the variable holding the key, and the address of an OpenAI-compatible server |
| `decide` | the tasks the judge decides, each with its own options: `question`, `reply`, `undone`, `learn` (the loop: reading your messages, drafting rules, judging turns), `habits` |
| `off_arm` | the share of new sessions run without the choice rule and its gate; `1` turns them off for every session, while the ledger, the habits loop and your kept rules go on |
| `shadow` | `true` runs the judge beside the patterns and records what it would decide |

## Commands

| Command | What it shows or does |
|---|---|
| `aidetic` or `aidetic stats` | the choice rule's ledger: before the gate against with it |
| `aidetic recent`, `aidetic reversals` | the latest answers, and the picks undone in their own session |
| `aidetic habits [--days N]` | your habits, what you objected to and forewarned, and whether a guard could pay |
| `aidetic habits show N` | your own words for habit N |
| `aidetic rule list` | every habit the loop knows, its signals, its rule, where the rule applies and its guard |
| `aidetic rule ID accept [--text …] [--here \| --project PATH]`, `decline`, `never`, `drop` | your answer to a proposed rule, run by Claude; `--here` or `--project` keeps it for that project only |
| `aidetic rule ID scope all \| here \| PATH` | where a kept rule applies: `all` puts it in every project; `here` or a folder adds that project to a rule kept for some, or limits one kept for every project to it |
| `aidetic rule add "…" [--here \| --project PATH]` | a rule of your own, kept at once, for every project or the one named; handed over, never watched |
| `aidetic correct ID "…"` | a denial, send-back or guard decision you say was wrong, in your words |
| `aidetic arms` | the off arm against the on arm, when `off_arm` is between 0 and 1 |
| `aidetic install` | the whole setup again, after an update; only what is missing or differs changes |
| `aidetic hook [--off]` | put the background `reply` check in `settings.json`, or take it out |

## Where things live

Everything about you stays in `~/.claude/aidetic`, never in the repository: the ledger
(`events.jsonl`, `state.json`), `judge.json`, the evaluation set (`eval/`), the habits' readings
and state (`habits/`, where `live.json` holds each habit, its rule and where that applies), and
`rules.md`, every rule you kept with where it applies, for you to read: each session is handed only
the ones for its project. These files quote your work, so do not commit or share them.

## Turning it off

- The choice rule and its gate: `"off_arm": 1` in `judge.json`.
- The loop, on the settings hooks: remove the `learn` entry from the `Stop` hooks in
  `~/.claude/settings.json`. Dropping a rule (`aidetic rule ID drop`) stops it being handed over
  and judged.
- The background reply check, on the settings hooks: `aidetic hook --off`.
- All of it, on the settings hooks: remove the entries of `src/hooks.json` from `settings.json`.
  Every hook fails open, so a broken or missing clone never blocks your work.
- All of it, on the plugin: `claude plugin disable aidetic`.

## Working on it

`python3 -m unittest discover -s tests` writes only to a temporary home. The hooks run the clone's
working tree in every session, so test a change before saving it. The plugin's own tests, against
stand-ins for the engine and never your ledger, are `claude plugin test .`. A change to
`src/questions.py` is checked against your real questions before it is committed: every
`AskUserQuestion` input in your transcripts run through the old and the new `choice_gate.py pre`,
each in a fresh temporary `HOME`, and the decisions compared.

## License

aidetic is free software, released under the GNU General Public License, version 3 (SPDX
`GPL-3.0-only`); the text is in [`LICENSE`](LICENSE). You may run, study, change and share it, and
what you distribute that is based on it must be offered under the same license, with its source.
