// Pure helpers: split `she --json-stream` output into NDJSON events and fold
// each event into an explanation (CLI_CONTRACT.md §4, §4.1). No `$` here.

import type { SheBasis, SheBreakdownItem, SheEnv, SheExplanation, SheFile } from '../types'

/** How many explanations the pane keeps. */
export const KEEP = 5

export function started(id: string, command: string): SheExplanation {
  return { id, command, status: 'running', summary: '', hasParse: false, breakdown: [], files: [], env: [], unknowns: [] }
}

/**
 * Splits streamed text into complete lines; returns the lines and the
 * unfinished tail to carry into the next piece.
 */
export function splitLines(buffer: string, text: string): { lines: string[]; rest: string } {
  const all = (buffer + text).split('\n')
  const rest = all.pop() ?? ''

  return { lines: all.map(line => line.trim()).filter(line => line !== ''), rest }
}

/** Parses one NDJSON line; undefined for anything that is not a JSON object. */
export function parseEvent(line: string): Record<string, unknown> | undefined {
  try {
    const value: unknown = JSON.parse(line)

    return typeof value === 'object' && value !== null && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : undefined
  } catch {
    return undefined
  }
}

function text(value: unknown): string {
  if (typeof value === 'string') return value
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') {
    const one = value as Record<string, unknown>
    for (const field of ['text', 'name', 'word', 'reason', 'message']) {
      if (typeof one[field] === 'string') return one[field] as string
    }
  }

  return JSON.stringify(value)
}

/** An unknown as one line: `./run.sh: path to a local script`. */
function unknown(value: unknown): string {
  if (typeof value === 'object' && value !== null) {
    const one = value as Record<string, unknown>
    const what = text(one.text ?? one.name ?? one.word)
    const why = typeof one.reason === 'string' ? one.reason : ''
    if (what !== '') return why !== '' ? `${what}: ${why}` : what
  }

  return text(value)
}

function basisOf(value: unknown, fallback: SheBasis): SheBasis {
  return value === 'visible' || value === 'inferred' ? value : fallback
}

function files(value: unknown, fallback: SheBasis): SheFile[] {
  if (!Array.isArray(value)) return []

  return value.map(item => {
    const one = (typeof item === 'object' && item !== null ? item : { path: item }) as Record<string, unknown>

    return { path: text(one.path), op: text(one.op ?? '?'), basis: basisOf(one.basis, fallback) }
  })
}

function envs(value: unknown, fallback: SheBasis): SheEnv[] {
  if (!Array.isArray(value)) return []

  return value.map(item => {
    const one = (typeof item === 'object' && item !== null ? item : { name: item }) as Record<string, unknown>
    const entry: SheEnv = { name: text(one.name), op: text(one.op ?? '?'), basis: basisOf(one.basis, fallback) }
    if (one.value !== undefined && one.value !== null) entry.value = text(one.value)

    return entry
  })
}

function breakdown(value: unknown): SheBreakdownItem[] {
  if (!Array.isArray(value)) return []

  return value.flatMap(item => {
    if (typeof item !== 'object' || item === null) return []
    const one = item as Record<string, unknown>
    const span = Array.isArray(one.span) ? one.span : []
    const [start, end] = span
    if (typeof start !== 'number' || typeof end !== 'number') return []
    const entry: SheBreakdownItem = {
      span: [start, end],
      text: text(one.text),
      kind: text(one.kind),
      help: typeof one.help === 'string' ? one.help : '',
    }
    if (typeof one.source === 'string' && one.source !== '') entry.source = one.source

    return [entry]
  })
}

/** Folds one `--json-stream` event into the explanation. */
export function apply(x: SheExplanation, event: Record<string, unknown>): SheExplanation {
  switch (event.event) {
    case 'deterministic':
      return {
        ...x,
        hasParse: true,
        breakdown: breakdown(event.breakdown),
        files: files(event.files, 'visible'),
        env: envs(event.env, 'visible'),
        unknowns: Array.isArray(event.unknowns) ? event.unknowns.map(unknown) : [],
      }
    case 'summary_chunk':
      return { ...x, summary: x.summary + text(event.text) }
    case 'inferred':
      return {
        ...x,
        files: [...x.files, ...files(event.files, 'inferred')],
        env: [...x.env, ...envs(event.env, 'inferred')],
      }
    case 'done': {
      const summary = event.summary as Record<string, unknown> | null | undefined
      const whole = summary && typeof summary.text === 'string' ? summary.text : ''
      const exit = typeof event.exit === 'number' ? event.exit : x.exit

      const llm = llmReason(event.warnings)
      const note = exit === 4 && llm !== undefined ? llm : noteFor(exit)

      return { ...x, summary: whole !== '' ? whole : x.summary, exit, status: 'done', note: note ?? x.note }
    }
    default:
      return x
  }
}

const LLM_WARNING = 'LLM unavailable: '

/**
 * Why the LLM step failed, from the `done` event's warnings: `she`'s own words,
 * such as "Model required: …" or "No Ollama installed: …".
 */
export function llmReason(warnings: unknown): string | undefined {
  if (!Array.isArray(warnings)) return undefined
  const hit = warnings.find((w): w is string => typeof w === 'string' && w.startsWith(LLM_WARNING))

  return hit?.slice(LLM_WARNING.length)
}

/** What `she`'s exit code means for the pane (CLI_CONTRACT.md §8). */
export function noteFor(exit: number | undefined): string | undefined {
  switch (exit) {
    case undefined:
    case 0:
      return undefined
    case 3:
      return 'she could not fully parse this command'
    case 4:
      return 'no LLM summary (model unavailable or timed out)'
    default:
      return `she exited with code ${exit}`
  }
}

/** Ends an explanation once the process has exited. */
export function finished(x: SheExplanation, code: number | null, stderr: string): SheExplanation {
  const exit = x.exit ?? code ?? undefined
  if (x.status === 'done') return { ...x, exit }
  const isEmpty = x.summary === '' && x.files.length === 0 && x.unknowns.length === 0
  const failed = isEmpty && exit !== 0
  const why = noteFor(exit) ?? (code === null ? 'she was stopped' : undefined)
  const first = stderr.trim().split('\n')[0]?.slice(0, 160)

  return { ...x, exit, status: failed ? 'failed' : 'done', note: failed && first ? first : why }
}

/** Puts an explanation in the list, newest last, keeping KEEP. */
export function upsert(list: SheExplanation[], x: SheExplanation): SheExplanation[] {
  const at = list.findIndex(one => one.id === x.id)
  if (at === -1) return [...list, x].slice(-KEEP)

  return list.map(one => (one.id === x.id ? x : one))
}
