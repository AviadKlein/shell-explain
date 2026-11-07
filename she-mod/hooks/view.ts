// Pure helpers for drawing an explanation: the explainshell-style breakdown
// tree (mirroring src/she/render.py), effect colours, the one-line gist and
// the redraw throttle. No `$` here.

import type { SheBasis, SheBreakdownItem } from '../types'

const INDENT = '  '
const LIST_LABEL_WIDTH = 24
const LABELLED_KINDS = new Set(['command', 'subcommand', 'option', 'function'])

/** Characters, not UTF-16 units, as Python's `len` and spans count them. */
function chars(text: string): string[] {
  return Array.from(text)
}

function size(text: string): number {
  return chars(text).length
}

/** render.py `truncate`: cut to `width` with a trailing ellipsis. */
export function truncate(line: string, width: number): string {
  const all = chars(line)

  return all.length <= width ? line : all.slice(0, Math.max(0, width - 1)).join('') + '…'
}

function padEnd(text: string, width: number): string {
  return text + ' '.repeat(Math.max(0, width - size(text)))
}

/** render.py `format_source`: `man:find(1)` → `man find(1)`; builtin/syntax are not shown. */
export function formatSource(source: string | undefined): string | undefined {
  if (source === undefined || source === '' || source === 'syntax' || source === 'builtin') return undefined
  const colon = source.indexOf(':')

  return colon === -1 ? source : `${source.slice(0, colon)} ${source.slice(colon + 1)}`
}

/** render.py `item_label`: `name: help  [source]`, `name (builtin): help`, or the help alone. */
export function itemLabel(item: SheBreakdownItem, maxWidth?: number): string {
  const help = item.help || '(no description found)'
  const words = item.text.split(/\s+/).filter(word => word !== '')
  const head = words[0] ?? item.text
  let label =
    item.source === 'builtin'
      ? `${head} (builtin): ${help}`
      : LABELLED_KINDS.has(item.kind)
        ? `${head}: ${help}`
        : help
  const source = formatSource(item.source)
  const suffix = source ? `  [${source}]` : ''
  if (maxWidth !== undefined && size(label) + size(suffix) > maxWidth) {
    label = truncate(label, Math.max(1, maxWidth - size(suffix)))
  }

  return label + suffix
}

/** render.py `list_lines`: the simple list form, `text   label`. */
export function listLines(items: readonly SheBreakdownItem[], width: number): string[] {
  const texts = items.map(item => item.text.split(/\s+/).filter(word => word !== '').join(' '))
  const column = Math.min(LIST_LABEL_WIDTH, Math.max(...texts.map(size)))

  return items.map((item, at) =>
    truncate(`${INDENT}${padEnd(texts[at]!, column)}  ${itemLabel(item, width - INDENT.length - column - 2)}`, width),
  )
}

/**
 * render.py `breakdown_lines`: underline each span, then one connector line
 * per item, right to left. Falls back to the list for multi-line commands or
 * commands wider than `width`.
 */
export function breakdownLines(command: string, items: readonly SheBreakdownItem[], width: number): string[] {
  if (items.length === 0) {
    return [...command.split('\n').map(line => truncate(INDENT + line, width)), INDENT + '(nothing to explain)']
  }
  const length = size(command)
  if (command.includes('\n') || INDENT.length + length > width) return listLines(items, width)

  const underline: string[] = new Array(length).fill(' ')
  for (const item of items) {
    for (let at = Math.max(0, item.span[0]); at < Math.min(item.span[1], length); at += 1) underline[at] = '─'
  }
  const anchors = items.map(item => item.span[0] + Math.floor(Math.max(0, item.span[1] - item.span[0] - 1) / 2))
  const lines = [INDENT + command, INDENT + underline.join('').trimEnd()]
  for (let index = items.length - 1; index >= 0; index -= 1) {
    const row: string[] = new Array(Math.max(0, anchors[index]!)).fill(' ')
    for (const earlier of anchors.slice(0, index)) {
      if (earlier >= 0 && earlier < row.length) row[earlier] = '│'
    }
    const prefix = INDENT + row.join('') + '└─ '
    lines.push(truncate(prefix + itemLabel(items[index]!, width - size(prefix)), width))
  }

  return lines
}

/** How a file or env row is drawn. */
export type EffectStyle = {
  /** A theme key, or undefined for the default colour. */
  color?: 'error' | 'warning'
  dimColor?: boolean
  italic?: boolean
  /** The tag after the row: `visible` from the syntax, `guess` from the LLM. */
  tag: 'visible' | 'guess'
}

/**
 * The colour of a file effect: delete red, write/append/chmod amber, create
 * default, read dim; an env set/unset amber, read dim. Inferred rows are italic.
 */
export function effectStyle(kind: 'file' | 'env', op: string, basis: SheBasis): EffectStyle {
  const style: EffectStyle = basis === 'inferred' ? { italic: true, tag: 'guess' } : { tag: 'visible' }
  if (kind === 'file') {
    if (op === 'delete') style.color = 'error'
    else if (op === 'write' || op === 'append' || op === 'chmod') style.color = 'warning'
    else if (op === 'read') style.dimColor = true
  } else if (op === 'set' || op === 'unset') {
    style.color = 'warning'
  } else if (op === 'read') {
    style.dimColor = true
  }

  return style
}

/** The summary's first sentence, on one line. */
export function firstSentence(summary: string): string {
  const flat = summary.trim().split(/\n\s*\n/)[0]!.replace(/\s+/g, ' ').trim()
  const match = /^(.+?[.!?])(?=\s|$)/.exec(flat)

  return match ? match[1]! : flat
}

/** Drops control characters a drawn string may not hold; keeps tab and newline. */
export function clean(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/\r\n?/g, '\n').replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/g, '')
}

/** Minimum time between two redraws of a streaming explanation. */
export const REDRAW_MS = 100

/** How long to wait before the next redraw: 0 means now. */
export function throttleWait(lastAt: number, now: number, interval = REDRAW_MS): number {
  return Math.max(0, lastAt + interval - now)
}
