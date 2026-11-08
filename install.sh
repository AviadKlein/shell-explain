#!/bin/sh
# Installs a released `she` and, when Claude Code is present, the she-mod.
# Ollama and the model are the user's to install: this script only checks for them.
#
#   curl -fsSL https://raw.githubusercontent.com/AviadKlein/shell-explain/v0.1.0/install.sh | sh
#
# SHE_VERSION picks the release tag (default below); SHE_NO_MOD=1 skips the mod.
set -eu

REPO="AviadKlein/shell-explain"
VERSION="${SHE_VERSION:-v0.1.0}"
MODEL="${SHE_MODEL:-qwen3.5:4b}"

say() { printf '%s\n' "$*"; }

if ! command -v uv >/dev/null 2>&1; then
  say "uv is required: https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi

say "==> she $VERSION"
uv tool install --force "git+https://github.com/$REPO@$VERSION"

if [ "${SHE_NO_MOD:-}" = "1" ]; then
  say "==> she-mod skipped (SHE_NO_MOD=1)"
elif command -v claude >/dev/null 2>&1; then
  say "==> she-mod (Claude Code plugin)"
  claude plugin install she-mod --marketplace "$REPO"
else
  say "==> she-mod skipped: Claude Code (claude) is not on PATH"
fi

say "==> checking Ollama and the model"
if ! command -v ollama >/dev/null 2>&1; then
  say "No Ollama installed. Install Ollama, then:"
  say "  ollama pull $MODEL        # or any model Ollama supports; then export SHE_MODEL=<name>"
elif ! ollama list 2>/dev/null | awk 'NR > 1 { print $1 }' | grep -qx -e "$MODEL" -e "$MODEL:latest"; then
  say "Model required: $MODEL is not pulled. Either:"
  say "  ollama pull $MODEL"
  say "  export SHE_MODEL=<a model you have>   # see: ollama list"
else
  say "Ollama and $MODEL are ready."
fi

say "Done. Check everything with: she --status"
