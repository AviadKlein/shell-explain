// The she-mod's state contract: every value it keeps in `$.state`.

/** Where a file or env entry came from: the command's syntax, or an LLM guess. */
export type SheBasis = 'visible' | 'inferred'

/** One file the explained command touches, per CLI_CONTRACT.md §4. */
export type SheFile = { path: string; op: string; basis: SheBasis }

/** One environment variable the explained command touches. */
export type SheEnv = { name: string; op: string; value?: string; basis: SheBasis }

/** One breakdown item, per CLI_CONTRACT.md §4: a span of the command and its help. */
export type SheBreakdownItem = {
  /** [start, end) character offsets into the command. */
  span: [number, number]
  text: string
  kind: string
  help: string
  /** `man:tar(1)`, `builtin`, `syntax`, ...; absent when none. */
  source?: string
}

/** One explanation, built up from `she --json-stream` events as they arrive. */
export type SheExplanation = {
  /** The Bash call's tool_use_id. */
  id: string
  command: string
  status: 'running' | 'done' | 'failed'
  /** The LLM summary so far (a guess). */
  summary: string
  /** True once the deterministic event (breakdown, files, env) arrived. */
  hasParse: boolean
  breakdown: SheBreakdownItem[]
  files: SheFile[]
  env: SheEnv[]
  unknowns: string[]
  /** `she`'s exit code, once known. */
  exit?: number
  /** A short reason when `she` failed or gave no summary. */
  note?: string
}

/** The pane's tabs. */
export type SheTab = 'summary' | 'breakdown' | 'files' | 'env' | 'unknowns'

declare module 'claude-code' {
  interface PluginState {
    'she-mod': {
      explanations: Shaped<SheExplanation[]>
      /** Id of the explanation shown; null shows the newest. */
      selected: string | null
      /** The pane's chosen tab; reset to summary by a new explanation. */
      tab: SheTab
      /** The one-line notice when `she` is missing or failing; null when fine. */
      notice: string | null
      /** Id of the newest explanation the person dismissed from the band. */
      dismissed: string | null
      /** `she --version`'s line (`she 0.2.0`), read at session start; null until known or if it failed. */
      cliVersion: string | null
      /** False after `/she off` (or the pane's button): no new explanations this session. */
      isOn: boolean
      /** Per Bash tool_use_id: the line drawn under its transcript row; null draws none. */
      gist: StateFamily<string | null>
    }
  }
}
