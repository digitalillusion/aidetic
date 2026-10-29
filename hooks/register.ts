import type { EngineInterface, Register } from 'claude-code'

// aidetic's gate stays the Python in src/ (choice_gate.py), run from this plugin's own folder. This
// module hands it each event a settings hook would get, on stdin, and maps what it prints and its
// exit code back. Every hook fails open, as the gate's own do: a gate that cannot run must never
// stop a question.
//
// Not carried over: `stop`, whose patterns decide nothing once the judge reads replies (`reply`).

const gate = ($: EngineInterface) => `${$.plugin.root}/src/choice_gate.py`

type Ran = { code: number; out: string; err: string }

// The `reply` and `learn` hooks, which settings run with `asyncRewake`: after the turn has ended,
// and a send-back (exit 2) wakes the session with what the gate wrote to stderr.
const WAKES = [
  { command: 'reply', timeoutMs: 120_000, summary: 'aidetic sent the reply back', message: "The user's aidetic gate sent your last reply back:" },
  { command: 'learn', timeoutMs: 300_000, summary: 'aidetic, from your habits', message: "The user's aidetic loop:" },
] as const

// The settings hooks and this module would each record every event, so one carries the gate at a
// time: the module stands down while settings.json names it, and where the file cannot be read. The
// gate keeps to the same rule for what it writes (settings_carry_gate in choice_gate.py).
let settings: Promise<boolean> | undefined

const standsDown = ($: EngineInterface, home: string) =>
  (settings ??= (async () => {
    const path = `${home}/.claude/settings.json`
    try {
      return (await $.fs.exists(path)) && String(await $.fs.read(path)).includes('choice_gate.py')
    } catch {
      return true
    }
  })())

// The points of action a habit is watched at: the matchers `write_watch` keeps in watched.json, where
// the settings hooks have them as PreToolUse entries. Anchored, as a settings matcher is.
const isWatched = async ($: EngineInterface, home: string, tool: string) => {
  try {
    const matchers: unknown = JSON.parse(String(await $.fs.read(`${home}/.claude/aidetic/watched.json`)))
    return Array.isArray(matchers) && matchers.some(matcher => new RegExp(`^(?:${matcher})$`).test(tool))
  } catch {
    return false // none watched, or none yet
  }
}

// What an installed plugin still needs that hooks cannot give: the `aidetic` command, which the rule
// and the loop tell Claude to run, and judge.json. `install --plugin` writes them, once, and never
// over what is there; the first event waits for it, so that `start` reads the judge's settings.
let setUp: Promise<void> | undefined

const setup = ($: EngineInterface, home: string) =>
  (setUp ??= (async () => {
    try {
      const there = await Promise.all([`${home}/.local/bin/aidetic`, `${home}/.claude/aidetic/judge.json`].map(path => $.fs.exists(path)))
      if (there.every(Boolean)) return
      const ran = await $.process.run(['python3', gate($), 'install', '--plugin'], { timeoutMs: 30_000 })
      if (ran.exitCode === 0) $.ui.toast('aidetic: set up the `aidetic` command and judge.json')
    } catch {
      // the gate's own hooks work without either
    }
  })())

const run = async ($: EngineInterface, command: string, input: unknown, timeoutMs = 30_000): Promise<Ran | undefined> => {
  const home = await $.env.get('HOME')
  if (!home || (await standsDown($, home))) return undefined
  await setup($, home)
  try {
    const ran = await $.process.run(['python3', gate($), command], { stdin: JSON.stringify(input), timeoutMs })
    return { code: ran.exitCode, out: ran.stdout, err: ran.stderr }
  } catch {
    return undefined
  }
}

const output = (out: string): Record<string, unknown> | undefined => {
  try {
    return JSON.parse(out)?.hookSpecificOutput
  } catch {
    return undefined
  }
}

const denial = (ran: Ran | undefined, what: string): string | undefined => {
  const found = output(ran?.out ?? '')
  return found?.permissionDecision === 'deny' ? String(found.permissionDecisionReason ?? `aidetic denied the ${what}`) : undefined
}

const added = (ran: Ran | undefined): string[] => {
  const context = output(ran?.out ?? '')?.additionalContext
  return typeof context === 'string' && context ? [context] : []
}

export const register: Register = on => {
  // The envelope of a tool call carries no session, transcript or cwd; the prompt hook sets them.
  let base: { session_id: string; transcript_path: string; cwd: string } | undefined
  // The turn a send-back starts is let through, as `stop_hook_active` does for `asyncRewake`.
  let woken = false

  const remember = (e: { session_id: string; transcript_path: string; cwd: string }) => {
    base = { session_id: e.session_id, transcript_path: e.transcript_path, cwd: e.cwd }
  }

  on('classic.SessionStart', async ($, e, next) => {
    remember(e)
    const ran = await run($, 'start', e)
    const result = await next(e)
    const context = added(ran)
    return context.length ? { ...result, additionalContext: [...(result.additionalContext ?? []), ...context] } : result
  }).catch(($, e, next) => next(e))

  on('classic.UserPromptSubmit', async ($, e, next) => {
    remember(e)
    const ran = await run($, 'prompt', e)
    const result = await next(e)
    const context = added(ran)
    return context.length ? { ...result, additionalContext: [...(result.additionalContext ?? []), ...context] } : result
  }).catch(($, e, next) => next(e))

  on('tool.call', { tool: 'AskUserQuestion' }, async ($, e, next) => {
    if (!base) return next(e)
    const ran = await run($, 'pre', {
      hook_event_name: 'PreToolUse',
      ...base,
      tool_name: e.tool,
      tool_use_id: e.tool_use_id,
      tool_input: { questions: e.questions },
    }, 90_000)
    const reason = denial(ran, 'question')
    return reason ? { deny: reason } : next(e)
  }).catch(($, e, next) => next(e))

  // `watch`: the loop's verdict on an act, or its guard's decision, where a habit is watched.
  on('tool.call', async ($, e, next) => {
    const home = await $.env.get('HOME')
    if (!home || !base || !(await isWatched($, home, e.tool))) return next(e)
    const input: Record<string, unknown> = { ...e }
    for (const key of ['tool', 'tool_use_id', 'agentId', 'requestMeta']) delete input[key]
    const ran = await run($, 'watch', { hook_event_name: 'PreToolUse', ...base, tool_name: e.tool, tool_use_id: e.tool_use_id, tool_input: input }, 90_000)
    const reason = denial(ran, 'act')
    return reason ? { deny: reason } : next(e)
  }).catch(($, e, next) => next(e))

  on('classic.PostToolUse', async ($, e, next) => {
    if (e.tool_name === 'AskUserQuestion') await run($, 'post', e, 5_000)
    return next(e)
  }).catch(($, e, next) => next(e))

  on('classic.Stop', ($, e, next) => {
    const input = { ...e, stop_hook_active: e.stop_hook_active || woken }
    woken = false
    for (const wake of WAKES) {
      // Not awaited: the turn is over, and the user waits for nothing.
      void run($, wake.command, input, wake.timeoutMs).then(async ran => {
        if (ran?.code !== 2) return
        woken = true
        $.ui.toast(wake.summary)
        await $.prompt.submit({ text: `${wake.message}\n${ran.err.trim()}` }).catch(() => undefined)
      })
    }
    return next(e)
  }).catch(($, e, next) => next(e))
}
