# she — shell explain

`she` explains a shell command in plain English. A deterministic layer (tree-sitter parse,
local man pages, learned `--help` pages) prints a breakdown instantly; a small local LLM
(Ollama) then streams a short summary and guesses at effects the syntax doesn't show.
It never runs the command it explains.

The full specification is in [CLI_CONTRACT.md](CLI_CONTRACT.md).

## Install

Requires macOS, Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/). `she` does not ship
or download a model: you install Ollama and choose one yourself (step 1).

### 1. Ollama and a model

Install [Ollama](https://ollama.com), then pull a model. Any chat model Ollama runs will
do; `qwen3.5:4b` (about 4 GB) is the default `she` was tuned on:

```sh
ollama pull qwen3.5:4b
```

To use another model, pull it and point `she` at it (add the line to `~/.zshrc`):

```sh
ollama pull gemma3:4b
export SHE_MODEL=gemma3:4b        # or per call: she --model gemma3:4b -- '...'
```

Ollama keeps the weights in `~/.ollama/models`, or in `$OLLAMA_MODELS` when that is set
(set it before Ollama starts, e.g. to keep models on another disk). A model from a local
GGUF file works too once Ollama knows it: `ollama create mymodel -f Modelfile`, then
`SHE_MODEL=mymodel`.

### 2. `she` and the Claude Code mod

One line installs a released `she` and, if Claude Code is installed, the mod. It then
reports whether Ollama and your model are ready:

```sh
curl -fsSL https://raw.githubusercontent.com/AviadKlein/shell-explain/v0.1.0/install.sh | sh
```

Or the same by hand:

```sh
uv tool install "git+https://github.com/AviadKlein/shell-explain@v0.1.0"
claude plugin install she-mod --marketplace AviadKlein/shell-explain
```

Inside a Claude Code session the mod's line is
`/plugin install she-mod --marketplace AviadKlein/shell-explain` (answer `y` to add the
marketplace, then pick the user scope). The mod starts working in new sessions; it needs
`she` on `PATH`.

In the mod's pane, the `⧉` buttons copy a file's full path, an env variable (`NAME=value`),
or the command wrapped as `she -- '<command>'`, so pasting it explains the command
instead of running it.

`/she` opens the pane; `/she off` stops new explanations for the rest of the session and
`/she on` resumes them (the pane has the same switch). To turn the mod off for good:
`claude plugin disable she-mod`.

### 3. Check

```sh
she --status
```

It says `No Ollama installed` or `Model required: …` when something is missing, and
otherwise where the model's weights are and how to remove them.

### Updating and removing

```sh
uv tool install --force "git+https://github.com/AviadKlein/shell-explain@v0.2.0"   # a newer tag
claude plugin marketplace update shell-explain                                     # newer mod
uv tool uninstall shell-explain
claude plugin uninstall she-mod
ollama rm qwen3.5:4b              # frees the model's disk space
```

## Usage

```sh
she -- 'find . -name "*.tmp" -delete && export CLEANED=1'
she --parse-only -- 'tar -czf out.tgz src'      # no LLM, ~100 ms
she --json -- 'git log --graph --oneline'       # she/v1 JSON
she --json < commands.sh                        # one JSON list; heredocs stay whole
she --warm                                      # start Ollama and load the model
```

Put the command after `--` so its flags are never read as `she` flags.

## Flags

| Group | Flags |
|---|---|
| Explain | `--parse-only`, `--list` (files only), `--list-env` (env only), `--json`, `--json-stream`, `--no-learn`, `--no-cache`, `--model NAME`, `--instructions FILE`, `--timeout SEC` |
| Help pages | `--parse-cli TOOL [--depth N]`, `--has-pages TOOL`, `--forget TOOL[@VERSION]`, `--list-tools` |
| Server | `--warm`, `--status`, `--clear-cache`, `--install-watchdog`, `--uninstall-watchdog` |
| Instructions | `--edit-global-instructions`, `--edit-local-instructions`, `--show-instructions` |

Exit codes: 0 ok, 1 internal error, 2 usage error, 3 parse error, 4 LLM unavailable or timed out
(`No Ollama installed`, `Model required`, or a timeout; the reason is on stderr).

## Environment

| Variable | Default |
|---|---|
| `SHE_MODEL` | `qwen3.5:4b`; any model you pulled into Ollama |
| `SHE_OLLAMA_URL` | `http://localhost:11434` |
| `OLLAMA_MODELS` | `~/.ollama/models`: Ollama's own setting, where model weights live; `she --status` reads it |
| `SHE_KEEP_ALIVE` | `10m` |
| `SHE_INSTRUCTIONS` | (unset) a file path or literal instructions text |
| `SHE_CACHE_TTL` | `7` (days) |
| `SHE_CACHE_MAX_MB` | `50` |
| `SHE_SERVER_IDLE` | `30` (minutes, for the watchdog) |
| `SHE_CACHE_DIR` / `SHE_STATE_DIR` / `SHE_CONFIG_DIR` | `~/Library/Caches/she`, `~/Library/Application Support/she`, `~/.config/she` |

## Development

```sh
uv sync
uv run pytest
uv tool install .                        # your working copy as `she`
claude plugin test she-mod               # the mod's tests
claude --plugin-dir she-mod              # a session with the working-copy mod
```

## Releasing

Bump `version` in `pyproject.toml` and `src/she/__init__.py` (and `she-mod/.claude-plugin/plugin.json`
for a mod change), update the tag in `install.sh` and this README, commit, then
`git tag -a vX.Y.Z -m 'she X.Y.Z' && git push origin vX.Y.Z`.
