import type { On } from 'claude-code'
import { expect, test } from 'claude-code/testing'

type Gate = { exitCode: number; stdout?: string; stderr?: string }
type Call = { command: string; input: Record<string, any>; argv: readonly string[] }

const CLEAN = '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"other.sh"}]}]}}'
const NAMED = '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"python3 ~/.claude/aidetic/choice_gate.py reply"}]}]}}'
const ASKED = [{ question: 'Which?', header: 'Pick', multiSelect: false, options: [{ label: 'A', description: 'a' }, { label: 'B', description: 'b' }] }]

const DENY = JSON.stringify({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: 'no pick named (gate event ab12cd)' } })
const GUARD = JSON.stringify({ hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny', permissionDecisionReason: 'This breaks a rule the user keeps (gate event cd34ef)' } })
const RULE = JSON.stringify({ hookSpecificOutput: { hookEventName: 'SessionStart', additionalContext: 'the rule' } })

// Nothing sits beneath the plugin: these hooks stand for the engine, and the gate is a function.
// `missing` names what is not there to find; `watched` is the text of watched.json, where there is one.
const engine = (on: On, settings: string | undefined, gate: (c: Call) => Gate = () => ({ exitCode: 0 }), missing: string[] = [], watched?: string) => {
  const calls: Call[] = []
  on('env.get', () => ({ value: '/home/test' }))
  on('fs.read', (_, e) => {
    const text = e.path.endsWith('watched.json') ? watched : settings
    return text === undefined ? { deny: 'no such file' } : { value: text }
  })
  on('fs.exists', (_, e) => ({ value: !missing.some(name => e.path.endsWith(name)) }))
  on('process.run', (_, e) => {
    const call = { command: e.argv[2], input: JSON.parse(e.init?.stdin ?? '{}'), argv: e.argv }
    calls.push(call)
    const out = gate(call)
    return { value: { exitCode: out.exitCode, stdout: out.stdout ?? '', stderr: out.stderr ?? '' } }
  })
  on('tool.call', () => ({ result: {} }))
  on('classic.SessionStart', () => ({}))
  on('classic.UserPromptSubmit', () => ({}))
  on('classic.PostToolUse', () => ({}))
  on('classic.Stop', () => ({}))
  return calls
}

// What a background wake leaves running, once the engine's hook chains have settled.
const settle = () => new Promise(done => setTimeout(done, 20))

const ask = ($: any) => $.tool.call({ tool: 'AskUserQuestion', questions: ASKED })

test('a question the gate denies is denied, with its reason', async ($, on) => {
  const calls = engine(on, CLEAN, c => (c.command === 'pre' ? { exitCode: 0, stdout: DENY } : { exitCode: 0 }))

  await $.classic.UserPromptSubmit({ prompt: 'go' })
  const result = await ask($)

  expect(result.deny).toContain('no pick named (gate event ab12cd)')
  const pre = calls.find(c => c.command === 'pre')!
  expect(pre.input.tool_name).toBe('AskUserQuestion')
  expect(pre.input.tool_input.questions).toEqual(ASKED)
  expect(pre.input.session_id).toBeTruthy()
})

test("the gate runs from the plugin's own folder", async ($, on) => {
  const calls = engine(on, CLEAN)

  await $.classic.SessionStart({ source: 'startup' })

  expect(calls[0].argv[0]).toBe('python3')
  // The plugin's folder, not a launcher kept in ~/.claude/aidetic.
  expect(calls[0].argv[1]).toMatch(/^\/.*\/src\/choice_gate\.py$/)
  expect(calls[0].argv[1]).not.toContain('/.claude/aidetic/')
})

test('a question the gate lets through goes on', async ($, on) => {
  const calls = engine(on, CLEAN)

  await $.classic.UserPromptSubmit({ prompt: 'go' })
  const result = await ask($)

  expect(result.deny).toBeUndefined()
  expect(calls.some(c => c.command === 'pre')).toBe(true)
})

test('an act at a watched point goes to the gate, and its guard can deny it', async ($, on) => {
  const calls = engine(on, CLEAN, c => (c.command === 'watch' ? { exitCode: 0, stdout: GUARD } : { exitCode: 0 }), [], '["Edit|Bash"]')

  await $.classic.UserPromptSubmit({ prompt: 'go' })
  const result = await $.tool.call({ tool: 'Bash', command: 'git commit -m x' })

  expect(result.deny).toContain('gate event cd34ef')
  const watch = calls.find(c => c.command === 'watch')!
  expect(watch.input.tool_name).toBe('Bash')
  expect(watch.input.tool_input).toEqual({ command: 'git commit -m x' })
  expect(watch.input.session_id).toBeTruthy()
})

test('an act where no habit is watched never reaches the gate', async ($, on) => {
  const calls = engine(on, CLEAN, () => ({ exitCode: 0, stdout: GUARD }), [], '["Edit"]')

  await $.classic.UserPromptSubmit({ prompt: 'go' })
  expect((await $.tool.call({ tool: 'Bash', command: 'ls' })).deny).toBeUndefined()

  expect(calls.some(c => c.command === 'watch')).toBe(false)
})

test('with no watched.json nothing is watched', async ($, on) => {
  const calls = engine(on, CLEAN, () => ({ exitCode: 0, stdout: GUARD }))

  await $.classic.UserPromptSubmit({ prompt: 'go' })
  expect((await $.tool.call({ tool: 'Bash', command: 'ls' })).deny).toBeUndefined()

  expect(calls.some(c => c.command === 'watch')).toBe(false)
})

test('a gate that cannot run lets the question through', async ($, on) => {
  engine(on, CLEAN, c => (c.command === 'pre' ? { exitCode: 1, stdout: 'Traceback (most recent call last)', stderr: 'boom' } : { exitCode: 0 }))

  await $.classic.UserPromptSubmit({ prompt: 'go' })

  expect((await ask($)).deny).toBeUndefined()
})

test('the first session sets up the command and judge.json, once, before the gate runs', async ($, on) => {
  const toasts: string[] = []
  const calls = engine(on, CLEAN, () => ({ exitCode: 0 }), ['judge.json'])
  on('ui.toast', (_, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })

  await $.classic.SessionStart({ source: 'startup' })
  await $.classic.UserPromptSubmit({ prompt: 'go' })

  expect(calls.map(c => c.command)).toEqual(['install', 'start', 'prompt'])
  expect(calls[0].argv.slice(2)).toEqual(['install', '--plugin'])
  expect(toasts).toEqual(['aidetic: set up the `aidetic` command and judge.json'])
})

test('nothing is set up where the command and judge.json are there', async ($, on) => {
  const calls = engine(on, CLEAN)

  await $.classic.SessionStart({ source: 'startup' })

  expect(calls.map(c => c.command)).toEqual(['start'])
})

test('the module stands down while settings.json names the gate', async ($, on) => {
  const calls = engine(on, NAMED, () => ({ exitCode: 0, stdout: DENY }))

  await $.classic.SessionStart({ source: 'startup' })
  await $.classic.UserPromptSubmit({ prompt: 'go' })
  const result = await ask($)
  await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'x' })
  await settle()

  expect(calls).toEqual([])
  expect(result.deny).toBeUndefined()
})

test('the module runs where there is no settings.json to name the gate', async ($, on) => {
  const calls = engine(on, undefined, () => ({ exitCode: 0 }), ['settings.json'])

  await $.classic.SessionStart({ source: 'startup' })

  expect(calls.map(c => c.command)).toEqual(['start'])
})

test('the module stands down where settings.json cannot be read', async ($, on) => {
  const calls = engine(on, undefined)

  await $.classic.SessionStart({ source: 'startup' })

  expect(calls).toEqual([])
})

test("the gate's rule reaches the model at the session's start", async ($, on) => {
  engine(on, CLEAN, c => (c.command === 'start' ? { exitCode: 0, stdout: RULE } : { exitCode: 0 }))

  const result = await $.classic.SessionStart({ source: 'startup' })

  expect(result.additionalContext).toContain('the rule')
})

test('a send-back wakes the session once, and its own turn is let through', async ($, on) => {
  const submitted: string[] = []
  // The gate's `reply` sends back only a turn that is not its own wake: `stop_hook_active` lets it through.
  const calls = engine(on, CLEAN, c =>
    c.command === 'reply' && !c.input.stop_hook_active ? { exitCode: 2, stderr: '“B”: name the pick (gate event 9f9f9f)\n' } : { exitCode: 0 },
  )
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })
  on('ui.toast', () => ({ value: undefined }))

  await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'I pick B.' })
  await settle()

  expect(submitted).toEqual(["The user's aidetic gate sent your last reply back:\n“B”: name the pick (gate event 9f9f9f)"])
  expect(calls.filter(c => c.command === 'reply' || c.command === 'learn').map(c => c.input.stop_hook_active)).toEqual([false, false])

  // The turn the wake began: both hooks hear it as `stop_hook_active`, as under `asyncRewake`.
  await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'Sorry.' })
  await settle()
  expect(calls.filter(c => c.command === 'reply' || c.command === 'learn').slice(2).map(c => c.input.stop_hook_active)).toEqual([true, true])

  // And the next one is judged again.
  await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'Fine.' })
  await settle()
  expect(calls.filter(c => c.command === 'reply' || c.command === 'learn').slice(4).map(c => c.input.stop_hook_active)).toEqual([false, false])
})

test('a reply the gate lets through wakes nothing', async ($, on) => {
  const submitted: string[] = []
  engine(on, CLEAN)
  on('prompt.submit', (_, e) => {
    submitted.push(e.text)
    return { text: e.text }
  })

  await $.classic.Stop({ stop_hook_active: false, last_assistant_message: 'Done.' })
  await settle()

  expect(submitted).toEqual([])
})
