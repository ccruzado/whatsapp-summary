#!/bin/bash
# Lanza Claude Code a través de OmniRoute usando Gemini (fallback cuando se agotan los tokens de Anthropic).
# Requiere OMNIROUTE_API_KEY (variable de entorno, o definida en omniroute/.env.client).
DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
[ -f "$DIR/.env.client" ] && set -a && . "$DIR/.env.client" && set +a
: "${OMNIROUTE_API_KEY:?Define OMNIROUTE_API_KEY (ver omniroute/.env.client.example)}"
MODEL="${OMNIROUTE_MODEL:-gemini/gemini-3.8-flash}"
export ANTHROPIC_BASE_URL="${OMNIROUTE_URL:-http://localhost:20128}"
export ANTHROPIC_AUTH_TOKEN="$OMNIROUTE_API_KEY"
export ANTHROPIC_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$MODEL"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$MODEL"
export ANTHROPIC_SMALL_FAST_MODEL="$MODEL"
exec claude "$@"
