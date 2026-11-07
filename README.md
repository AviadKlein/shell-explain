# she — shell explain

`she` explains a shell command in plain English. A deterministic layer (tree-sitter parse,
local man pages, learned `--help` pages) prints a breakdown instantly; a small local LLM
(Ollama) then streams a short summary and guesses at effects the syntax doesn't show.
It never runs the command it explains.

The full specification is in [CLI_CONTRACT.md](CLI_CONTRACT.md).

## Install

Requires macOS, Python ≥ 3.12, and [Ollama](https://ollama.com) with the default model:

```sh
ollama pull qwen3.5:4b
uv tool install .
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

Exit codes: 0 ok, 1 internal error, 2 usage error, 3 parse error, 4 LLM unavailable or timed out.

## Environment

| Variable | Default |
|---|---|
| `SHE_MODEL` | `qwen3.5:4b` |
| `SHE_OLLAMA_URL` | `http://localhost:11434` |
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
```
