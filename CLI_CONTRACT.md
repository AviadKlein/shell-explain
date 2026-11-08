# `she` — CLI contract (draft for approval)

`she` explains a shell command in plain English. It is a standalone CLI first; the
Claude Code integration (hook + mod) is a later consumer of its `--json` output and is
out of scope for this document.

Status: **DRAFT** — sections marked ❓ need a decision.

**Implementation:** Python, installed with `uv tool install` (a direct entry point; never
invoked through `uv run`, which re-checks the environment on every call).
Parser: tree-sitter-bash. LLM: a small (2–4B) quantized model served by Ollama (§6.1).

---

## 1. Principles

1. **Advisory only.** `she` describes and suggests. It never approves, blocks, or rates
   risk with authority. Every LLM-produced statement is labelled as a guess.
2. **Two layers.** A deterministic layer (parse + man pages + help pages) always runs and
   is fast. An LLM layer turns those facts into prose and guesses at effects. If the LLM
   is unavailable, the deterministic output is still printed.
3. **Reproducible.** Output is a function of
   `(command, help-page cache, local man pages, instructions, model digest)`.
   The help-page cache is a memo of what is installed on the machine, not session state.
   LLM calls use temperature 0 and a fixed seed; the model digest is reported in output.
   The answer cache (§7) stores results under a keyed hash of exactly these inputs, so a
   cache hit returns the same output a fresh run would.
4. **No logging.** `she` never stores command text. The answer cache stores only keyed
   hashes and redacted LLM output (§7). Users redirect output if they want to keep it.
5. **Fast first, complete later.** Deterministic sections are printed as soon as they are
   ready (well under 100 ms); the LLM summary streams in after them.
6. **Never executes the command being explained.** The only things it runs are
   `<tool> --version` / help invocations on tools it is learning (see §5), under strict
   rules.

---

## 2. Invocation

```
she [OPTIONS] -- <command string>
she [OPTIONS] < commands.txt         # commands on stdin; with --json the output is 1 json list
```

`--` is recommended so the explained command's flags are never read as `she` flags.
Stdin is split into commands by the parser (complete top-level statements), not by
newlines, so a heredoc or a `\` continuation stays one command (decision (b)).

### 2.1 Explain options

| Flag | Meaning |
|---|---|
| *(none)* | Full output: summary + breakdown + files + env + unknowns |
| `--parse-only` | Deterministic layer only: the explainshell-style ASCII breakdown, no LLM. |
| `--list` | Only the files section (read / written / deleted), visible + inferred |
| `--list-env` | Only the env section (read / set / unset), visible + inferred |
| `--json` | Machine-readable output (§4), printed once when complete. Combines with the three flags above to select sections (with `--list` / `--list-env` the object holds only `schema`, `command` and the selected sections) |
| `--json-stream` | NDJSON events: deterministic sections first, then summary chunks, then `done` (§4.1). For the mod |
| `--no-learn` | Do not run any help/version commands for unknown tools; report them as unknown |
| `--no-cache` | Skip the answer cache for this call (no read, no write) |
| `--model NAME` | Ollama model. Default: see §6.1 (❓ decided by eval). Also `SHE_MODEL` |
| `--instructions FILE` | Ad-hoc instructions for this call. Not repeatable. Replaces local and global instructions (§2.4) |
| `--timeout SEC` | Max wall-clock for the whole run (default 30). On timeout, print deterministic output and exit 4 |

### 2.2 Help-page cache options

| Flag | Meaning |
|---|---|
| `--parse-cli TOOL` | Learn `TOOL` now: version + recursive help crawl (§5). Prints a summary of pages stored |
| `--parse-cli TOOL --depth N` | Limit subcommand recursion depth (default 3) |
| `--has-pages TOOL` | Print whether pages exist for `TOOL`, which versions are stored, page count per version, and which version is currently installed |
| `--forget TOOL[@VERSION]` | Delete stored pages for a tool (all versions, or one) |
| `--list-tools` | List every learned tool with its versions |


Example:
```
$ she --has-pages uv
uv   installed: 0.9.4 (/opt/homebrew/bin/uv)
     stored:    0.9.4  142 pages  2026-11-07   ← current
                0.8.17 131 pages  2026-08-02
```

### 2.3 Server options

| Flag | Meaning |
|---|---|
| `--warm` | Idempotently ensure Ollama is running and the model is loaded; return immediately after. Intended for the SessionStart hook |
| `--status` | Print Ollama reachability (or `No Ollama installed`), the selected model and where its name came from, whether it is installed (or `Model required: …`, listing installed models), its size, digest and weights location (`$OLLAMA_MODELS` or `~/.ollama/models`) with the `ollama rm` line that removes it, cache locations and sizes |
| `--clear-cache` | Delete the answer cache (§7). The help-page cache is managed with `--forget` |
| `--install-watchdog` / `--uninstall-watchdog` | Add or remove the launchd agent that stops an idle Ollama server (§6) |

### 2.4 Instructions

Exactly **one** instructions source is used per call: the first one found, in this order.
Sources are never merged or concatenated.

| Precedence | Source | Where |
|---|---|---|
| 1 (ad-hoc) | `--instructions FILE` | the file given on the command line |
| 1 (ad-hoc) | `SHE_INSTRUCTIONS` env var | if its value is a path to an existing file, that file; otherwise the value itself is the text. Ignored when `--instructions` is given |
| 2 (local) | `./.she/instructions.md` | in the **current directory** only (no walking up to parent dirs) |
| 3 (global) | `~/.config/she/instructions.md` | |
| — | none | no instructions |

- The source actually used is reported: `instructions: ad-hoc | local | global | none`
  in the human output footer, and as `"instructions": {"source": "local", "path": "…"}`
  in JSON.
- A source that exists but is empty still counts as chosen (an empty local file is a way
  to switch off global instructions in one directory).
- Because local depends on cwd, the answer cache key includes the instructions **text**
  (§7.1), not the source, so switching directories can't serve a wrong cached answer.

### 2.5 Editing instructions

| Flag | Meaning |
|---|---|
| `--edit-global-instructions` | Open `~/.config/she/instructions.md` in `vim`, creating the file and directory if missing |
| `--edit-local-instructions` | Open `./.she/instructions.md` (current directory) in `vim`, creating the file and directory if missing |
| `--show-instructions` | Print which source would be used from the current directory, its path, and its text |

`vim` is started in the foreground; `she` exits with vim's exit code. If `vim` isn't on
`PATH`, exit 2 with a message.

---

## 3. Human-readable output

Order of output: BREAKDOWN, FILES, ENV and UNKNOWNS print first (deterministic, instant);
the SUMMARY section prints last, at the bottom, and streams token by token. Nothing is
ever redrawn, so the output is the same on a terminal and when piped.

```
$ she -- 'find . -name "*.tmp" -delete && export CLEANED=1'

BREAKDOWN
  find . -name "*.tmp" -delete && export CLEANED=1
  ──── ─ ───────────── ─────── ── ──────────────
   │   │       │          │     │       └─ export (builtin): set shell variable and export it
   │   │       │          │     └─ run the right side only if the left side succeeds
   │   │       │          └─ -delete: delete found files            [man find(1)]
   │   │       └─ -name: match base name against pattern             [man find(1)]
   │   └─ starting directory
   └─ find: walk a file hierarchy                                     [man find(1), BSD]

FILES
  deleted   ./**/*.tmp                 visible (from -delete)
ENV
  set       CLEANED=1                  visible (export)

UNKNOWNS
  (none)

SUMMARY (LLM guess)
  Deletes every file ending in .tmp under the current directory, recursively,
  then sets CLEANED=1 in the current shell.

sources: man find(1) · model qwen…@sha256:ab12… · instructions: global
```

`UNKNOWNS` lists anything the deterministic layer couldn't resolve:
no man page and no help pages, aliases/functions (not visible to `she`), relative-path
scripts (`./run.sh`), `eval` / `bash -c` / `$(...)` contents, parse errors.
These are also passed to the LLM so it can say "I'm guessing here".

---

## 4. JSON output (`--json`)

```json
{
  "schema": "she/v1",
  "command": "find . -name \"*.tmp\" -delete && export CLEANED=1",
  "summary": { "text": "…", "source": "llm", "model": "name@sha256:…", "cache": "hit" },
  "instructions": { "source": "global", "path": "~/.config/she/instructions.md" },
  "breakdown": [
    { "span": [0, 4], "text": "find", "kind": "command",
      "help": "walk a file hierarchy", "source": "man:find(1)" },
    { "span": [21, 28], "text": "-delete", "kind": "option",
      "help": "delete found files", "source": "man:find(1)" }
  ],
  "files": [
    { "path": "./**/*.tmp", "op": "delete", "basis": "visible" }
  ],
  "env": [
    { "name": "CLEANED", "op": "set", "value": "1", "basis": "visible" }
  ],
  "unknowns": [],
  "warnings": [],
  "timing_ms": { "parse": 4, "lookup": 11, "llm": 1830 }
}
```

- `op`: files → `read | write | append | delete | create | chmod`; env → `read | set | unset`
- File and env entries also carry an optional `detail` (e.g. `"from -delete"`, `"export"`).
- `basis`: `visible` (from syntax/man/help) or `inferred` (LLM guess)
- With `--parse-only`, `summary` is `null` and no `inferred` entries appear.
- `summary.cache`: `hit | miss | off`. On a hit, `timing_ms.llm` is 0.
- The schema is versioned; breaking changes bump `she/v2`.

### 4.1 Streaming (`--json-stream`)

One JSON object per line:
```
{"event":"deterministic","command":"…","breakdown":[…],"files":[…],"env":[…],"unknowns":[…]}
{"event":"summary_chunk","text":"Deletes every file "}
{"event":"summary_chunk","text":"ending in .tmp …"}
{"event":"inferred","files":[…],"env":[…]}
{"event":"done","summary":{…},"warnings":[…],"timing_ms":{…},"exit":0}
```
On a cache hit, the summary arrives as a single `summary_chunk`.

---

## 4b. Performance

Targets on an Apple Silicon Mac (to be confirmed by measurement):

| Path | Target |
|---|---|
| `--parse-only`, tools already known | < 200 ms |
| Full explain, cache hit | < 250 ms |
| Full explain, cache miss, model loaded | first summary token < 1 s, done < 3 s |
| Full explain, model not loaded | + model load time (seconds); `--warm` avoids this |

How:
1. **Deterministic first, streaming summary** (§3, §4.1).
2. **Output cap:** the summary is 2–3 sentences; requests set `num_predict` (default 160
   tokens, covering summary + inferred files/env). Generation time grows with output
   length, so this is the main LLM lever.
3. **Short prompt:** send only the extracted facts (the tokens, the matched flag
   descriptions, visible files/env, unknowns, instructions), never whole man or help pages.
4. **Small model:** 2–4B at 4-bit quantization by default (§6.1), with thinking mode off.
5. **Lazy imports:** the HTTP client and LLM code are imported only when needed, so
   `--parse-only` and cache hits don't pay for them.
6. **Answer cache** (§7).

---

## 5. Help-page learning

### 5.1 Lookup order for a command word
1. Shell builtin / keyword → built-in table (`cd`, `export`, `source`, `[[`, …)
2. Local man page → scan the man directories (`MANPATH`, `../share/man` next to each `PATH`
   entry, system defaults) for `man{1,8,6}/<tool>.<section>[.gz]`, and `<tool>-<sub>` for
   subcommands (e.g. `git-log`). Only if that finds nothing for the tool itself, ask
   `man -w <tool>` (≈150 ms on macOS, too slow for every lookup); its "not found" answers are
   cached for 24 h. Pages are rendered with `mandoc -T ascii` (fallback `man <tool>`).
3. Learned help pages in the cache, for the **installed** version
4. Not known → learn it (§5.2), unless `--no-learn`
5. Still nothing → add to `UNKNOWNS`

### 5.2 Learning a tool (auto, or via `--parse-cli`)

1. **Resolve** the tool to an absolute path on `PATH`.
   **Never learn** relative paths (`./x`, `../x`), paths inside the cwd, or anything not on
   `PATH`. Those go straight to `UNKNOWNS`.
2. **Version**: try `--version`, then `-V`, then `version`. Take the first line that looks
   like a version. If none, use the binary fingerprint (§5.4) as the version key.
3. **Help**: try `--help`, then `help`, then `-h`.
   (`-h` is last because it means something else for `du`, `ls`, `sort`, …)
4. **Subcommands**: parse the help text for a commands section (`Commands:`,
   `Available Commands:`, `SUBCOMMANDS`, …) and recurse with `<tool> <sub> --help`.
5. **Store** every page keyed by `(tool, version, subcommand path)`.

**On-demand vs full crawl.** When auto-learning during an explain, only the subcommand
path actually used is fetched (`uv pip install` → `uv`, `uv pip`, `uv pip install`).
`--parse-cli` does the full recursive crawl. This keeps the first explain of a huge CLI
(`aws`, `gcloud`) fast.

### 5.3 Sandboxing the help invocations
Every version/help run uses:
- `stdin` = `/dev/null`, timeout 5s per call, output capped (e.g. 256 KB)
- `PAGER=cat MANPAGER=cat GIT_PAGER=cat TERM=dumb NO_COLOR=1` (e.g. `git --help` opens a pager)
- Strip ANSI escapes from output
- Crawl limits: depth (default 3), max pages per tool (default 500)
- Run under `sandbox-exec` (macOS Seatbelt) with a profile that denies network access
  and denies file writes outside a throwaway temp dir (decided: yes). Writes are allowed only
  to the scratch dir, `/dev/null`, `/dev/zero`, `/dev/tty*` and `/dev/fd/*`; cwd is the
  scratch dir. If `sandbox-exec` is unavailable, nothing is probed. A tool that misbehaves on
  `--help` (runs instead of printing help, phones home, writes config) has those actions
  fail; its help text, if any, is still captured. A tool that crashes under the sandbox is
  reported as UNKNOWN rather than retried unsandboxed.

### 5.4 Version check on reuse
On every later encounter:
1. Compute a cheap **binary fingerprint** of the resolved path: realpath + size + mtime.
   If it matches a stored entry → use those pages, run nothing.
2. If it doesn't match → run the version probe. Known version → reuse the pages and
   record the new fingerprint. New version → re-learn.

**Shims.** Version managers (pyenv, mise, asdf, rbenv) put a small script on `PATH` that
never changes across versions, so its fingerprint can't detect an upgrade. When the
resolved file is a script rather than a Mach-O binary, `she` skips step 1 and always runs
the version probe.

### 5.5 Storage
- SQLite at `~/Library/Caches/she/pages.db` (or `$XDG_CACHE_HOME/she/` if set)
- Tables: `tools(name, version, path, learned_at)`,
  `fingerprints(path, fingerprint, name, version)` (fingerprint = realpath:size:mtime_ns),
  `pages(tool, version, subpath, text, parsed_json)`, `missing(tool, version, subpath)`
  (negative cache: probed, no help), `man_pages(path, mtime_ns, size, text, parsed_json)`
  (rendered man pages) and `man_missing(name, checked_at)`.
- It's a cache. Deleting it loses nothing that can't be re-learned.

---

## 6. Ollama lifecycle

- `she` talks to its own server at `http://127.0.0.1:11435`. `SHE_OLLAMA_PORT` changes the
  port; `SHE_OLLAMA_URL` sets a full URL (local or remote) and wins when both are set. An
  invalid value is a usage error (exit 2).
- On any LLM call (and on `--warm`): probe `GET /api/version`. If it's down and the URL's
  host is local (`localhost`, `127.0.0.1`, `::1`), start `ollama serve` detached with
  `OLLAMA_HOST=<host>:<port>`, and record `<pid> <url>` in the PID file. This is
  idempotent: a second start is skipped because the probe succeeds. A remote server is
  never started: a remote URL that doesn't answer → exit 4.
- Requests send `keep_alive` (default `10m`, override `SHE_KEEP_ALIVE`), so Ollama unloads
  the model on its own after idle time.
- **Watchdog:** a launchd user agent (`~/Library/LaunchAgents/`, `StartInterval` 300 s)
  checks `/api/ps` on the URL recorded in the PID file. If no model has been loaded for 30 minutes (`SHE_SERVER_IDLE`), it
  stops the server. It only stops a server that `she` started (tracked by a PID file in
  `~/Library/Application Support/she/`, and checked with `ps` to still be an ollama process), so
  an Ollama you run yourself for other work is left alone. Installed with
  `she --install-watchdog`, removed with `she --uninstall-watchdog`.
- Ollama not installed (no server answers and `ollama` is not on PATH) → exit 4,
  `No Ollama installed: …` with the install URL.
- Model missing → exit 4, `Model required: '<model>' is not installed in Ollama (installed: …)`,
  telling the user to `ollama pull <model>` or set `SHE_MODEL` to an installed model.
  `she` never pulls or ships models; the user picks any model Ollama supports.
- Requests set `think: false`. Qwen3.5 and similar models have a reasoning mode that
  produces hundreds of hidden tokens before answering, which would blow the latency targets.

### 6.1 Model ❓

Candidates for the default, all in the Ollama library, all used at `q4_K_M`:

| Model | Size | Released | Why consider it |
|---|---|---|---|
| `qwen3.5:4b` | 4B | Feb 2026 | Current generation, most-downloaded small Qwen. Leading candidate |
| `qwen3.5:2b` | 2B | Feb 2026 | Same family, about twice as fast; check whether quality holds |
| `qwen2.5-coder:3b` | 3B | Nov 2024 | Code-specialised, exactly 3B. Older; baseline for comparison |
| `llama3.2:3b` | 3B | Sep 2024 | General-purpose baseline |

There is no current-generation 3B: the newest small families come in 2B and 4B. The task
is explaining text in plain English, not writing code, so a general instruct model may
beat a coder model. The default is picked by the eval: about 200 real commands from
Claude Code transcripts, the same prompt for each model, graded on accuracy and
latency.

---

## 7. Answer cache (v1)

Caches the LLM part of the output (summary + inferred files/env). The deterministic part
is always recomputed; it's fast and reflects the current machine.

### 7.1 Key
```
key = HMAC-SHA256(machine_key,
        prompt_template_version
      ‖ command text (exact bytes)
      ‖ model digest (sha256, not the tag)
      ‖ hash of the facts sent to the LLM (flag descriptions, pages' tool+version, unknowns)
      ‖ hash of the instructions text
      ‖ num_predict)
```
Each part is length-prefixed before hashing. `--list` / `--list-env` don't change the
prompt, so they share one cached answer with the full output.
- Any change to the command, the installed tool versions, the instructions, the model or
  the prompt template produces a different key, so stale answers are never served.
- **Why HMAC and not a plain SHA-256:** many commands are short and guessable
  (`rm -rf ~`, `git push --force`). With a plain hash, anyone who can read the cache can
  hash candidate commands and confirm which ones were explained. A keyed hash prevents that.
- `machine_key`: 32 random bytes, created on first use at
  `~/Library/Application Support/she/key` with mode 600. Deleting it invalidates the whole
  cache (every key changes), which is harmless.

### 7.2 Value
- The LLM output (summary text + inferred files/env), **redacted** before it's stored:
  the summary can quote paths, URLs or tokens from the command. Same redaction rules as
  agreed earlier (known secret formats, parse-tree positions, entropy), replacing hits
  with `<REDACTED:kind>`.
- Redacting the stored value means a cache hit can show `<REDACTED:…>` where the fresh
  answer showed the value.
- Never stored: the command text, cwd, environment values, timestamps of individual calls.

### 7.3 Storage and expiry
- SQLite at `~/Library/Caches/she/answers.db`, file mode 600, separate from `pages.db`.
- Each row: `key, value, model_digest, created_at`. (`created_at` is per row and is needed
  for expiry; it records when an answer was produced, not which command.)
- **Expiry:** entries older than 7 days (`SHE_CACHE_TTL`, in days) are ignored on read and deleted
  on write, in the same transaction. No external scheduler is needed because nothing
  depends on the data being deleted at an exact time.
- **Size cap:** 50 MB (`SHE_CACHE_MAX_MB`); oldest entries are evicted first (measured as
  the summed length of stored keys and values).
- `--no-cache` bypasses it; `--clear-cache` deletes the file.

---

## 8. Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (UNKNOWNS may be non-empty; that's not an error) |
| 1 | Internal error |
| 2 | Usage error (bad flags) |
| 3 | Command could not be parsed (partial output still printed) |
| 4 | LLM unavailable or timed out (deterministic output still printed) |

Note for later: in Claude Code settings hooks, exit code 2 means "block". The hook wrapper
must translate `she`'s exit codes, not pass them through.

---

## 9. Out of scope

- Logging / history of explained commands (the answer cache stores no command text)
- Risk scoring, approve/deny decisions
- Running the explained command, dry-runs, or tracing
- The Claude Code hook and mod (separate document, consumes `--json`)

---

## 10. Open decisions (summary)

1. ~~Sandbox help/version probes with `sandbox-exec` (§5.3)?~~ Decided: yes.
2. Default model, decided by eval (§6.1). Implemented default: `qwen3.5:4b`.
3. ~~Stdin: one command, or one per line (§2)?~~ Decided: (b), split by the parser.
