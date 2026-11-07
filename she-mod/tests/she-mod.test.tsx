import { expect, mock, test } from 'claude-code/testing'
import type { Engine as TestEngine } from 'claude-code/testing'
import type { On } from 'claude-code'

import type { SheBreakdownItem } from '../types'
import { breakdownLines, effectStyle, firstSentence, throttleWait } from '../hooks/view'

const PANE = {
  plugin: 'she-mod',
  component: 'Pane',
  requestId: 'she',
  props: {
    title: 'she: explain',
    isFocused: false,
    bodyColumns: 80,
    placement: 'dock',
    scroll: { offset: 0, bodyRows: 30 },
    view: {},
  },
} as const

const COMMAND = 'find . -name "*.tmp" -delete && export CLEANED=1'

const BREAKDOWN: SheBreakdownItem[] = [
  { span: [0, 4], text: 'find', kind: 'command', help: 'walk a file hierarchy', source: 'man:find(1)' },
  { span: [30, 32], text: '&&', kind: 'operator', help: 'run the right side only if the left side succeeds', source: 'syntax' },
]

/** What the contract's `she --json-stream` prints for COMMAND (§4.1). */
const EVENTS = [
  {
    event: 'deterministic',
    breakdown: BREAKDOWN,
    files: [{ path: './**/*.tmp', op: 'delete', basis: 'visible' }],
    env: [{ name: 'CLEANED', op: 'set', value: '1', basis: 'visible' }],
    unknowns: [{ text: './run.sh', reason: 'path to a local script' }],
  },
  { event: 'summary_chunk', text: 'Deletes every file ' },
  { event: 'summary_chunk', text: 'ending in .tmp. Then sets CLEANED.' },
  { event: 'inferred', files: [{ path: './cache', op: 'read', basis: 'inferred' }], env: [] },
  {
    event: 'done',
    summary: { text: 'Deletes every file ending in .tmp. Then sets CLEANED.', source: 'llm' },
    timing_ms: {},
    exit: 0,
  },
]
const STREAM = EVENTS.map(event => JSON.stringify(event) + '\n').join('')

type Spawned = { argv: readonly string[] }

/** The engine beneath the plugin: the calls it makes answered from memory. */
function engine(on: On, verdict: 'ask' | 'allow') {
  const spawned: Spawned[] = []
  const toasts: string[] = []
  const ran: string[] = []
  const ids: string[] = []
  const opened: Array<{ columns?: number }> = []
  const clock = mock.clock(on)
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('ui.open', (_, e) => {
    opened.push({ columns: e.columns })

    return { value: { isPlaced: true } }
  })
  on('ui.panes', () => ({ value: [] }))
  on('ui.toast', (_, e) => {
    toasts.push(e.text)

    return { value: undefined }
  })
  on('command.register', () => ({ value: { command: 'she' } }))
  on('tool.check', (_, e) => {
    if (e.tool_use_id !== undefined) ids.push(e.tool_use_id)

    return { decision: verdict }
  })
  // The person (played by a PermissionRequest hook) says yes to the prompt.
  on('classic.PermissionRequest', () => ({ decision: { behavior: 'allow' } }))
  on('tool.call', (_, e) => {
    ran.push(e.tool)

    return { result: { stdout: 'ran', stderr: '', interrupted: false } }
  })

  return { spawned, toasts, ran, ids, opened, clock }
}

/** A `she` that prints `text` in two pieces, then exits with `code`. */
function she(on: On, spawned: Spawned[], text: string, code = 0) {
  on('process.spawn', async function* (_, e) {
    spawned.push({ argv: e.argv })
    const cut = Math.floor(text.length / 2)
    yield { stream: 'stdout', text: text.slice(0, cut) }
    yield { stream: 'stdout', text: text.slice(cut) }

    return { value: { code, signal: null } }
  })
}

/** Every shown text of a drawing: Text, Markdown and Code. */
async function shown(ui: { findAll: (q: { type: string }) => Promise<Array<{ text: string }>> }): Promise<string> {
  const parts = [
    ...(await ui.findAll({ type: 'Text' })),
    ...(await ui.findAll({ type: 'Markdown' })),
    ...(await ui.findAll({ type: 'Code' })),
  ]

  return parts.map(found => found.text).join('\n')
}

/** The pane once `match` shows in it (the explanation runs in the background). */
async function paneText($: TestEngine, match: RegExp): Promise<string> {
  let all = ''
  for (let i = 0; i < 50; i += 1) {
    const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
    all = await shown(ui)
    await ui.unmount()
    if (match.test(all)) return all
  }

  return all
}

function toolRow(id: string, tool = 'Bash') {
  return {
    plugin: 'she-mod',
    surface: 'terminal',
    component: 'ToolUse',
    requestId: id,
    props: {
      tool_use_id: id,
      tool,
      input: tool === 'Bash' ? { command: COMMAND } : { file_path: '/etc/hosts' },
      isRunning: false,
      isErrored: false,
      isInterrupted: false,
    },
  } as const
}

test('a Bash call that will ask streams she into the pane', async ($, on) => {
  const { spawned, ran, opened } = engine(on, 'ask')
  she(on, spawned, STREAM)

  const result = await $.tool.call({ tool: 'Bash', command: COMMAND })
  expect(result.deny).toBeUndefined()
  expect(ran).toEqual(['Bash'])

  const text = await paneText($, /Deletes every file ending in \.tmp\./)
  expect(spawned.length).toBe(1)
  // The command is one argv element after `--`, never a shell string.
  expect(spawned[0]!.argv).toEqual(['she', '--json-stream', '--', COMMAND])
  expect(text).toMatch(/Deletes every file ending in \.tmp\./)
  expect(text).toMatch(/LLM guess/)
  // The pane asks for a fixed docked width.
  expect(opened[0]?.columns).toBe(60)

  // Files, Env and Unknowns are tabs; each row marked visible or guess.
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'files' })
  expect((await ui.find({ type: 'Text', text: /\.\/\*\*\/\*\.tmp/ }))?.text).toMatch(/delete.*\[visible\]/)
  expect((await ui.find({ type: 'Text', text: /\.\/cache/ }))?.text).toMatch(/\[guess\]/)
  await ui.press({ key: 'env' })
  expect((await ui.find({ type: 'Text', text: /set +CLEANED/ }))?.text).toMatch(/CLEANED=1.*\[visible\]/)
  await ui.press({ key: 'unknowns' })
  expect(await shown(ui)).toMatch(/\.\/run\.sh: path to a local script/)
  await ui.unmount()
})

test('tabs: hotkeys 1-5, inactive dim, choice kept across redraws, reset by a new explanation', async ($, on) => {
  const { spawned } = engine(on, 'ask')
  she(on, spawned, STREAM)
  await $.tool.call({ tool: 'Bash', command: COMMAND })
  await paneText($, /Deletes every file/)

  let ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const tabs = ['summary', 'breakdown', 'files', 'env', 'unknowns']
  for (const [n, key] of tabs.entries()) {
    const button = await ui.find({ type: 'Button', key })
    expect(button?.props.hotkey).toBe(String(n + 1))
    expect(button?.props.dimColor).toBe(key !== 'summary')
  }
  expect(await ui.find({ type: 'Markdown' })).toBeDefined()

  await ui.press({ key: 'breakdown' })
  expect((await ui.find({ type: 'Button', key: 'breakdown' }))?.props.dimColor).toBe(false)
  expect((await ui.find({ type: 'Button', key: 'summary' }))?.props.dimColor).toBe(true)
  await ui.unmount()

  // A fresh drawing keeps the chosen tab.
  ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  const tree = await ui.find({ type: 'Code' })
  expect(tree?.text).toBe(breakdownLines(COMMAND, BREAKDOWN, 80).join('\n'))
  expect(tree?.text).toMatch(/└─ find: walk a file hierarchy {2}\[man find\(1\)\]/)
  await ui.unmount()

  // A new explanation resets the tab to Summary.
  await $.tool.call({ tool: 'Bash', command: 'rm -rf build' })
  await paneText($, /2\/2/)
  ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect((await ui.find({ type: 'Button', key: 'summary' }))?.props.dimColor).toBe(false)
  expect(await ui.find({ type: 'Code' })).toBeUndefined()
  await ui.unmount()
})

test('the breakdown tree matches she render.py', () => {
  const git: SheBreakdownItem[] = [
    { span: [0, 3], text: 'git', kind: 'command', help: 'the stupid content tracker', source: 'man:git(1)' },
    { span: [4, 7], text: 'log', kind: 'subcommand', help: 'Show commit logs', source: 'man:git-log(1)' },
    {
      span: [8, 15],
      text: '--graph',
      kind: 'option',
      help: 'Draw a text-based graphical representation of the commit history on the left hand side of the output',
      source: 'man:git-log(1)',
    },
    {
      span: [16, 32],
      text: '--pretty=oneline',
      kind: 'option',
      help: 'Pretty-print the contents of the commit logs in a given format, where <format> can be one of oneline, short',
      source: 'man:git-log(1)',
    },
  ]
  const command = 'git log --graph --pretty=oneline origin..mybranch'
  // `COLUMNS=72 she --parse-only --no-learn -- '<command>'`, BREAKDOWN block.
  expect(breakdownLines(command, git, 72)).toEqual([
    '  git log --graph --pretty=oneline origin..mybranch',
    '  ─── ─── ─────── ────────────────',
    '   │   │     │           └─ --pretty=oneline: Pretty-…  [man git-log(1)]',
    '   │   │     └─ --graph: Draw a text-based graphical …  [man git-log(1)]',
    '   │   └─ log: Show commit logs  [man git-log(1)]',
    '   └─ git: the stupid content tracker  [man git(1)]',
  ])
  // Too wide for the columns: the list form (`COLUMNS=40`).
  expect(breakdownLines(command, git, 40)).toEqual([
    '  git               git: …  [man git(1)]',
    '  log               l…  [man git-log(1)]',
    '  --graph           -…  [man git-log(1)]',
    '  --pretty=oneline  -…  [man git-log(1)]',
  ])

  const tar: SheBreakdownItem[] = [
    { span: [0, 3], text: 'tar', kind: 'command', help: 'manipulate tape archives', source: 'man:tar(1)' },
    {
      span: [4, 16],
      text: '-czf out.tgz',
      kind: 'option',
      help: '-c: Create a new archive containing the specified items; -z: (c mode only) Compress',
      source: 'man:tar(1)',
    },
    { span: [21, 23], text: '&&', kind: 'operator', help: 'run the right side only if the left side succeeds', source: 'syntax' },
    { span: [24, 26], text: 'rm', kind: 'command', help: 'remove directory entries', source: 'man:rm(1)' },
    { span: [27, 30], text: '-rf', kind: 'option', help: '-r: Equivalent to -R.; -f: Attempt to remove the files', source: 'man:rm(1)' },
  ]
  expect(breakdownLines('tar -czf out.tgz src && rm -rf src', tar, 72)).toEqual([
    '  tar -czf out.tgz src && rm -rf src',
    '  ─── ────────────     ── ── ───',
    '   │       │           │  │   └─ -rf: -r: Equivalent to -R…  [man rm(1)]',
    '   │       │           │  └─ rm: remove directory entries  [man rm(1)]',
    '   │       │           └─ run the right side only if the left side succ…',
    '   │       └─ -czf: -c: Create a new archive containing t…  [man tar(1)]',
    '   └─ tar: manipulate tape archives  [man tar(1)]',
  ])
  // Multi-line commands use the list; nothing parsed says so.
  expect(breakdownLines('ls\npwd', [git[0]!], 72)).toEqual(['  git  git: the stupid content tracker  [man git(1)]'])
  expect(breakdownLines('x', [], 72)).toEqual(['  x', '  (nothing to explain)'])
})

test('effect colours: each op and basis', () => {
  expect(effectStyle('file', 'delete', 'visible')).toEqual({ color: 'error', tag: 'visible' })
  for (const op of ['write', 'append', 'chmod']) {
    expect(effectStyle('file', op, 'visible')).toEqual({ color: 'warning', tag: 'visible' })
  }
  expect(effectStyle('file', 'create', 'visible')).toEqual({ tag: 'visible' })
  expect(effectStyle('file', 'read', 'visible')).toEqual({ dimColor: true, tag: 'visible' })
  expect(effectStyle('file', 'delete', 'inferred')).toEqual({ color: 'error', italic: true, tag: 'guess' })
  expect(effectStyle('env', 'set', 'visible')).toEqual({ color: 'warning', tag: 'visible' })
  expect(effectStyle('env', 'unset', 'inferred')).toEqual({ color: 'warning', italic: true, tag: 'guess' })
  expect(effectStyle('env', 'read', 'visible')).toEqual({ dimColor: true, tag: 'visible' })
})

test('effect colours reach the drawn rows', async ($, on) => {
  const { spawned } = engine(on, 'ask')
  she(on, spawned, STREAM)
  await $.tool.call({ tool: 'Bash', command: COMMAND })
  await paneText($, /Deletes every file/)

  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  await ui.press({ key: 'files' })
  const deleted = await ui.find({ type: 'Text', text: /\.\/\*\*\/\*\.tmp/ })
  expect(deleted?.props.color).toBe('error')
  expect(deleted?.props.italic).toBeFalsy()
  const guessed = await ui.find({ type: 'Text', text: /\.\/cache/ })
  expect(guessed?.props.dimColor).toBe(true)
  expect(guessed?.props.italic).toBe(true)
  await ui.press({ key: 'env' })
  expect((await ui.find({ type: 'Text', text: /set +CLEANED/ }))?.props.color).toBe('warning')
  await ui.unmount()
})

test('the Bash row gets a she line only when she explained it', async ($, on) => {
  const { spawned, ids } = engine(on, 'ask')
  // The engine's own row, beneath the plugin.
  on('ui.render', { component: 'ToolUse' }, ($, e) => {
    const { Text } = $.ui.resolve(e)

    return <Text key="engine-row">{e.props.tool}(row)</Text>
  })
  let release: () => void = () => undefined
  const gate = new Promise<void>(resolve => (release = resolve))
  on('process.spawn', async function* (_, e) {
    spawned.push({ argv: e.argv })
    await gate
    yield { stream: 'stdout', text: STREAM }

    return { value: { code: 0, signal: null } }
  })
  await $.tool.call({ tool: 'Bash', command: COMMAND })
  const id = ids[0]!

  // While she runs.
  let ui = await $.ui.mount(toolRow(id))
  expect((await ui.find({ type: 'Text', text: /^she:/ }))?.text).toBe('she: explaining…')
  await ui.unmount()

  release()
  await paneText($, /Then sets CLEANED/)
  ui = await $.ui.mount(toolRow(id))
  expect((await ui.find({ type: 'Text', text: /^she:/ }))?.text).toBe('she: Deletes every file ending in .tmp.')
  await ui.unmount()

  // A Bash row she never ran on, and a Read row: drawn as the engine drew them.
  for (const target of [toolRow('toolu_other'), toolRow(id, 'Read')]) {
    ui = await $.ui.mount(target)
    expect(await ui.findAll({ type: 'Text', text: /she:/ })).toHaveLength(0)
    expect(await ui.drawn()).toMatchObject({ type: 'Text', children: [target.props.tool, '(row)'] })
    await ui.unmount()
  }
})

test('first sentence of a summary', () => {
  expect(firstSentence('Deletes files. Then more.')).toBe('Deletes files.')
  expect(firstSentence('  Lists\nthe dir\n\nSecond para.')).toBe('Lists the dir')
  expect(firstSentence('Version 1.2 is set! Ok')).toBe('Version 1.2 is set!')
})

test('the throttle: at most one write per 100 ms while streaming, plus a final one', async ($, on) => {
  const { clock } = engine(on, 'ask')
  expect(throttleWait(-Infinity, 0)).toBe(0)
  expect(throttleWait(0, 30)).toBe(70)
  expect(throttleWait(0, 100)).toBe(0)

  let writes = 0
  on('state.set', { plugin: 'she-mod', key: 'explanations' }, (_, e, next) => {
    writes += 1

    return next(e)
  })
  let reached: () => void = () => undefined
  const atGate = new Promise<void>(resolve => (reached = resolve))
  let release: () => void = () => undefined
  const gate = new Promise<void>(resolve => (release = resolve))
  const chunks = Array.from({ length: 40 }, (_, n) => ({ event: 'summary_chunk', text: `w${n} ` }))
  on('process.spawn', async function* () {
    yield { stream: 'stdout', text: JSON.stringify(EVENTS[0]) + '\n' }
    for (const chunk of chunks) yield { stream: 'stdout', text: JSON.stringify(chunk) + '\n' }
    reached()
    await gate
    yield { stream: 'stdout', text: JSON.stringify(EVENTS[EVENTS.length - 1]) + '\n' }

    return { value: { code: 0, signal: null } }
  })

  await $.tool.call({ tool: 'Bash', command: COMMAND })
  await atGate
  await clock.settle()
  // The start, then the first change; the 40 chunks wait on one timer.
  expect(writes).toBe(2)
  await clock.advance(100)
  expect(writes).toBe(3)
  expect(await paneText($, /w39/)).toMatch(/w0 w1 .* w39/)

  release()
  await paneText($, /Then sets CLEANED/)
  expect(writes).toBe(4)
})

test('a Bash call the rules allow, and non-Bash calls, are ignored', async ($, on) => {
  const allowed = engine(on, 'allow')
  she(on, allowed.spawned, STREAM)
  await $.tool.call({ tool: 'Bash', command: 'ls -la' })
  await $.tool.call({ tool: 'Read', file_path: '/etc/hosts' })
  expect(allowed.spawned.length).toBe(0)
  expect(allowed.ran).toEqual(['Bash', 'Read'])
})

test('a non-Bash call that asks is ignored', async ($, on) => {
  const { spawned } = engine(on, 'ask')
  she(on, spawned, STREAM)
  await $.tool.call({ tool: 'Write', file_path: '/tmp/x', content: 'x' })
  expect(spawned.length).toBe(0)
})

test('the verdict is handed back untouched and the call is never held', async ($, on) => {
  const { spawned, ran } = engine(on, 'ask')
  let release: () => void = () => undefined
  const never = new Promise<void>(resolve => (release = resolve))
  // A she that hangs until the test ends.
  on('process.spawn', async function* (_, e) {
    spawned.push({ argv: e.argv })
    await never

    return { value: { code: 0, signal: null } }
  })

  const verdict = await $.tool.check({ tool: 'Bash', input: { command: 'rm -rf build' } })
  expect(verdict.decision).toBe('ask')

  const result = await $.tool.call({ tool: 'Bash', command: 'rm -rf build' })
  expect(result.deny).toBeUndefined()
  expect(ran).toEqual(['Bash'])
  release()
})

test('she missing: one notice, nothing breaks', async ($, on) => {
  const { toasts, ran } = engine(on, 'ask')
  on('process.spawn', async function* () {
    throw new Error('spawn she ENOENT')
  })
  on('process.run', () => {
    throw new Error('spawn she ENOENT')
  })

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  const first = await $.tool.call({ tool: 'Bash', command: 'rm -rf build' })
  const second = await $.tool.call({ tool: 'Bash', command: 'rm -rf dist' })
  expect(first.deny).toBeUndefined()
  expect(second.deny).toBeUndefined()
  expect(ran).toEqual(['Bash', 'Bash'])

  const text = await paneText($, /not on PATH/)
  expect(text).toMatch(/not on PATH/)
  expect(toasts.length).toBe(1)
  expect(toasts[0]).toMatch(/not found on PATH/)
})
