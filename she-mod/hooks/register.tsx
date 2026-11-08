// she-mod: shows `she`'s explanation of a Bash command that is about to reach a
// permission prompt. Advisory only: it never approves, denies or holds a call.

import { atom, memberOf, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { SheExplanation, SheTab } from '../types'
import { apply, finished, parseEvent, splitLines, started, upsert } from './stream'
import { breakdownLines, clean, effectStyle, firstSentence, sheTemplate, throttleWait } from './view'

const PANE = 'she'
const TITLE = 'she: explain'
/** The docked pane's body width it asks for; a width the person set wins. */
const PANE_COLUMNS = 60
/** Kill a `she` run that has not finished by then (its own default timeout is 30 s). */
const RUN_LIMIT_MS = 90_000
const WARM_LIMIT_MS = 120_000

const TABS: ReadonlyArray<{ id: SheTab; label: string; hotkey: string }> = [
  { id: 'summary', label: 'Summary', hotkey: '1' },
  { id: 'breakdown', label: 'Breakdown', hotkey: '2' },
  { id: 'files', label: 'Files', hotkey: '3' },
  { id: 'env', label: 'Env', hotkey: '4' },
  { id: 'unknowns', label: 'Unknowns', hotkey: '5' },
]

const explanations = atom({ plugin: 'she-mod', key: 'explanations' } as const, [], { shape: 'explanations-2' })
const selected = atom({ plugin: 'she-mod', key: 'selected' } as const, null)
const tab = atom({ plugin: 'she-mod', key: 'tab' } as const, 'summary')
const notice = atom({ plugin: 'she-mod', key: 'notice' } as const, null)
const dismissed = atom({ plugin: 'she-mod', key: 'dismissed' } as const, null)
/** Session-wide switch: `/she on|off`, or the pane's button. */
const isOn = atom({ plugin: 'she-mod', key: 'isOn' } as const, true)
/** One member per Bash tool_use_id: the line under its transcript row. */
const gist = atom({ plugin: 'she-mod', key: 'gist' } as const, null)

type $ = EngineInterface

function commandOf(input: unknown): string | undefined {
  if (typeof input !== 'object' || input === null) return undefined
  const command = (input as { command?: unknown }).command

  return typeof command === 'string' && command.trim() !== '' ? command : undefined
}

function openPane($: $) {
  return $.ui.open({ id: PANE, title: TITLE, columns: PANE_COLUMNS })
}

/** The line under the Bash row once the explanation ended. */
function gistOf(x: SheExplanation): string {
  if (x.status === 'failed') return `no explanation (${x.note ?? 'she failed'})`
  if (x.summary.trim() !== '') return firstSentence(clean(x.summary))

  return x.note ?? 'no summary'
}

// Module variable on purpose: one toast per module load is enough.
let hasToasted = false

/** Copies `text` to the clipboard of the surface the press came from, and says so. */
async function copy($: $, text: string, what: string, surface: Parameters<$['ui']['copy']>[0]['surface']) {
  const result = await $.ui.copy({ text, surface })
  $.ui.toast(result.isCopied ? `she: copied ${what}` : `she: could not copy (${result.reason})`)
}

async function warn($: $, text: string): Promise<void> {
  await update($, notice, () => text)
  if (!hasToasted) {
    hasToasted = true
    $.ui.toast(text)
  }
}

async function explain($: $, id: string, command: string): Promise<void> {
  let x: SheExplanation = started(id, command)
  const row = memberOf(gist, { requestId: id })
  const save = () => update($, explanations, list => upsert(list, x))
  const end = async () => {
    await save()
    await update($, row, () => gistOf(x))
  }

  // Streaming writes are throttled: each write redraws the pane, so at most
  // one per REDRAW_MS, the last change held for a trailing write.
  let savedAt = -Infinity
  let pending: { cancel: () => void } | undefined
  const saveSoon = async () => {
    const now = await $.clock.now()
    const wait = throttleWait(savedAt, now)
    if (wait === 0) {
      pending?.cancel()
      pending = undefined
      savedAt = now
      await save()
    } else if (pending === undefined) {
      pending = $.clock.after(wait, () => {
        pending = undefined
        savedAt = now + wait
        void save().catch(() => undefined)
      })
    }
  }

  try {
    await save()
    await update($, row, () => 'explaining…')
    await update($, selected, () => null)
    await update($, tab, () => 'summary')
    void openPane($).catch(() => undefined)

    // argv, never a shell string: the command is one element after `--`.
    const child = $.process.spawn({ argv: ['she', '--json-stream', '--', command] })
    const limit = $.clock.after(RUN_LIMIT_MS, () => void child.return(undefined as never).catch(() => undefined))
    let buffer = ''
    let stderr = ''
    let code: number | null = null

    try {
      for await (const piece of child) {
        if (piece.stream === 'stderr') {
          stderr = (stderr + piece.text).slice(-4000)
          continue
        }
        const { lines, rest } = splitLines(buffer, piece.text)
        buffer = rest
        let changed = false
        for (const line of lines) {
          const event = parseEvent(line)
          if (event !== undefined) {
            x = apply(x, event)
            changed = true
          }
        }
        if (changed) await saveSoon()
      }
      const tail = parseEvent(buffer.trim())
      if (tail !== undefined) x = apply(x, tail)
      code = (await child.result.catch(() => ({ code: null, signal: null }))).code
    } catch (error) {
      // The first pull rejects when `she` cannot start: not on PATH.
      x = { ...x, status: 'failed', note: 'she is not on PATH' }
      await end()
      await warn($, 'she-mod: `she` not found on PATH; install it with `uv tool install` to see explanations.')

      return
    } finally {
      limit.cancel()
      pending?.cancel()
      pending = undefined
    }

    x = finished(x, code, stderr)
    await end()
    if (x.status === 'failed') {
      await warn($, `she-mod: \`she\` failed (${x.note ?? 'no output'}).`)
    } else {
      await update($, notice, () => null)
    }
  } catch {
    // Never let an explanation break the session.
    pending?.cancel()
    x = { ...x, status: 'failed', note: x.note ?? 'she-mod error' }
    await end().catch(() => undefined)
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const result = await next(e)

    void $.command
      .register({
        name: 'she',
        description: 'she explanations: open the pane, or turn them on/off for this session',
        argumentHint: '[on|off]',
        immediate: true,
      })
      .catch(() => undefined)

    // Warm the model without holding session start; failures are only noted.
    void $.process
      .run(['she', '--warm'], { timeoutMs: WARM_LIMIT_MS })
      .then(run => (run.exitCode === 0 ? undefined : $.ui.log(`she --warm exited ${run.exitCode}`, { to: 'debug' })))
      .catch(() => warn($, 'she-mod: `she` not found on PATH; explanations are off.'))
      .catch(() => undefined)

    return result
  })

  on('command.run', { command: 'she' }, async ($, e) => {
    const word = e.args.trim().toLowerCase()
    if (word === 'on' || word === 'off') {
      await update($, isOn, () => word === 'on')

      return { text: word === 'on' ? 'she: explanations on for this session.' : 'she: explanations off for this session (/she on to resume).' }
    }
    if (word !== '') return { text: `she: unknown argument "${e.args.trim()}"; use /she, /she on or /she off.` }
    const opened = await openPane($)
    const state = (await read($, isOn)) ? 'on' : 'off'

    return { text: opened.isPlaced ? `she pane opened (explanations ${state}).` : `she pane waits: ${opened.reason}` }
  })

  // The trigger: the engine's own verdict for a Bash call. An `ask` starts the
  // explanation, unless a host's permission callback answers it
  // (`approver: 'host'`, e.g. VS Code), where the mod stays quiet.
  //
  // Deliberately not `classic.PermissionRequest`: another plugin that answers
  // it without `next` (a security plugin, say) would hide it from this mod.
  // The cost: in auto mode the classifier settles many asks silently, and
  // those get explained too.
  //
  // The verdict is handed back untouched; nothing is held.
  on('tool.check', { tool: 'Bash' }, async ($, e, next) => {
    const verdict = await next(e)
    const command = commandOf(e.input)
    if (
      verdict.decision === 'ask' &&
      e.approver !== 'host' &&
      e.tool_use_id !== undefined &&
      command !== undefined &&
      (await read($, isOn))
    ) {
      void explain($, e.tool_use_id, command)
    }

    return verdict
  }).catch(($, e, next) => next(e))

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Code, Markdown, Text } = $.ui.resolve(e)
    const list = await read($, explanations)
    const chosen = await read($, selected)
    const current = await read($, tab)
    const warning = await read($, notice)
    const isEnabled = await read($, isOn)
    const width = Math.max(20, e.props.bodyColumns)
    const toggle = (
      <Box key="switch">
        <Text dimColor={isEnabled} color={isEnabled ? undefined : 'warning'}>
          {isEnabled ? 'explanations on ' : 'explanations off (this session) '}
        </Text>
        <Button key="toggle" plain dimColor label={isEnabled ? 'turn off' : 'turn on'} onPress={() => update($, isOn, () => !isEnabled)} />
      </Box>
    )
    const copyButton = (key: string, text: string, what: string) => (
      <Button key={`copy-${key}`} plain dimColor label="⧉" onPress={press => void copy($, text, what, press.surface)} />
    )

    const at = chosen === null ? list.length - 1 : list.findIndex(one => one.id === chosen)
    const shown = at >= 0 ? list[at] : list[list.length - 1]
    const index = shown === undefined ? -1 : list.indexOf(shown)
    const select = (to: number) => () => update($, selected, () => (to >= list.length - 1 ? null : list[to]!.id))
    const choose = (to: SheTab) => () => update($, tab, () => to)

    if (shown === undefined) {
      return (
        <Box flexDirection="column">
          {toggle}
          {warning !== null && <Text color="warning">{warning}</Text>}
          <Text dimColor>No Bash command has needed approval yet.</Text>
        </Box>
      )
    }

    const isRunning = shown.status === 'running'
    const status = isRunning ? 'explaining…' : shown.status === 'failed' ? 'failed' : 'done'
    const command = clean(shown.command)
    const counts: Partial<Record<SheTab, number>> = {
      files: shown.files.length,
      env: shown.env.length,
      unknowns: shown.unknowns.length,
    }
    const waiting = <Text dimColor>{isRunning ? 'waiting for she…' : '(no data)'}</Text>

    let body
    if (current === 'breakdown') {
      body =
        !shown.hasParse ? (
          waiting
        ) : (
          <Code key="tree" source={breakdownLines(command, shown.breakdown, width).join('\n')} />
        )
    } else if (current === 'files') {
      body = !shown.hasParse ? (
        waiting
      ) : shown.files.length === 0 ? (
        <Text dimColor>no files{isRunning ? ' yet' : ''}</Text>
      ) : (
        <Box flexDirection="column">
          {shown.files.map((f, n) => {
            const style = effectStyle('file', f.op, f.basis)

            return (
              <Box key={`file-${n}`}>
                {copyButton(`file-${n}`, f.path, f.path)}
                <Text wrap="truncate-end" color={style.color} dimColor={style.dimColor} italic={style.italic}>
                  {' '}
                  {f.op.padEnd(7)} {clean(f.path)} <Text dimColor>[{style.tag}]</Text>
                </Text>
              </Box>
            )
          })}
        </Box>
      )
    } else if (current === 'env') {
      body = !shown.hasParse ? (
        waiting
      ) : shown.env.length === 0 ? (
        <Text dimColor>no env vars{isRunning ? ' yet' : ''}</Text>
      ) : (
        <Box flexDirection="column">
          {shown.env.map((v, n) => {
            const style = effectStyle('env', v.op, v.basis)

            const text = v.value !== undefined ? `${v.name}=${v.value}` : v.name

            return (
              <Box key={`env-${n}`}>
                {copyButton(`env-${n}`, text, v.name)}
                <Text wrap="truncate-end" color={style.color} dimColor={style.dimColor} italic={style.italic}>
                  {' '}
                  {v.op.padEnd(7)} {clean(v.name)}
                  {v.value !== undefined ? `=${clean(v.value)}` : ''} <Text dimColor>[{style.tag}]</Text>
                </Text>
              </Box>
            )
          })}
        </Box>
      )
    } else if (current === 'unknowns') {
      body = !shown.hasParse ? (
        waiting
      ) : shown.unknowns.length === 0 ? (
        <Text dimColor>(none)</Text>
      ) : (
        <Box flexDirection="column">
          {shown.unknowns.map((u, n) => (
            <Text key={`unknown-${n}`} wrap="truncate-end">
              • {clean(u)}
            </Text>
          ))}
        </Box>
      )
    } else {
      const summary = clean(shown.summary).trim()
      body = (
        <Box flexDirection="column">
          <Text dimColor italic>
            LLM guess, may be wrong:
          </Text>
          {summary !== '' ? (
            <Markdown key="summary" text={summary} />
          ) : (
            <Text dimColor>{isRunning ? '…' : '(no summary)'}</Text>
          )}
        </Box>
      )
    }

    return (
      <Box flexDirection="column" width={width}>
        {toggle}
        {warning !== null && <Text color="warning">{warning}</Text>}
        <Box>
          <Button key="older" plain label="‹ older" onPress={select(index - 1)} dimColor={index <= 0} />
          <Text dimColor>
            {' '}
            {index + 1}/{list.length} · {status}{' '}
          </Text>
          <Button key="newer" plain label="newer ›" onPress={select(index + 1)} dimColor={index >= list.length - 1} />
        </Box>
        <Box>
          {copyButton('command', sheTemplate(shown.command), 'as she -- …')}
          <Text bold wrap="truncate-end">
            {' '}$ {command}
          </Text>
        </Box>
        {shown.note !== undefined && <Text color="warning">{clean(shown.note)}</Text>}

        <Box marginTop={1} flexWrap="wrap">
          {TABS.map(t => (
            <Box key={`tab-${t.id}`} marginRight={1}>
              <Button
                key={t.id}
                plain
                hotkey={t.hotkey}
                label={counts[t.id] !== undefined ? `${t.label} (${counts[t.id]})` : t.label}
                dimColor={t.id !== current}
                onPress={choose(t.id)}
              />
            </Box>
          ))}
        </Box>
        <Box marginTop={1} flexDirection="column">
          {body}
        </Box>
      </Box>
    )
  })

  // One dim line under a Bash row `she` explained; every other row as drawn.
  on('ui.render', { component: 'ToolUse' }, async ($, e, next) => {
    const drawn = await next(e)
    try {
      if (e.props.tool !== 'Bash') return drawn
      const line = await read($, memberOf(gist, { requestId: e.props.tool_use_id }))
      if (line === null) return drawn
      const { Box, Text } = $.ui.resolve(e)

      return (
        <Box flexDirection="column">
          {drawn}
          <Text dimColor wrap="truncate-end">
            she: {clean(line).replace(/\s+/g, ' ')}
          </Text>
        </Box>
      )
    } catch {
      return drawn
    }
  }).catch(($, e, next) => next(e))

  // On a narrow terminal an unasked pane waits undrawn; the band says one line
  // about the newest explanation and offers to open the pane.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const list = await read($, explanations)
    const newest = list[list.length - 1]
    const gone = await read($, dismissed)
    if (e.props.hasSurvey || newest === undefined || newest.id === gone || !(await read($, isOn))) return next(e)

    const panes = await $.ui.panes().catch(() => [])
    if (panes.some(p => p.id === PANE && p.isPlaced && p.isShown)) return next(e)

    const { Box, Button, Text } = $.ui.resolve(e)
    const gistText =
      newest.summary !== ''
        ? clean(newest.summary).split('\n')[0]
        : newest.status === 'running'
          ? 'explaining…'
          : clean(newest.note ?? 'no summary')

    return (
      <Box>
        <Text dimColor wrap="truncate-end">
          she (guess): {gistText}{' '}
        </Text>
        <Button key="open" plain label="details" onPress={() => void openPane($)} />
        <Text> </Text>
        <Button key="dismiss" plain label="×" onPress={() => update($, dismissed, () => newest.id)} />
      </Box>
    )
  })
}
