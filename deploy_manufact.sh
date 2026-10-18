#!/usr/bin/env bash
# Deploy Moot's MCP server (moot.manufact_server, via the root Dockerfile) to Manufact Cloud.
#
#   ./deploy_manufact.sh            # first deploy: uploads local source (--no-github) as server "moot"
#   ./deploy_manufact.sh --open     # extra flags are passed to `mcp-use deploy`
#
# Reads MANUFACT_API_KEY and provider keys from .env. Secrets go to Manufact through a temporary env file that is
# deleted on exit; they are never written into the repo or the upload (the CLI also excludes .env* files).
set -euo pipefail
cd "$(dirname "$0")"
ENV_FILE=.env

get() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | sed -e 's/^["'"'"']//' -e 's/["'"'"']$//'; }

MCP_USE_API_KEY="$(get MANUFACT_API_KEY)"
[ -n "$MCP_USE_API_KEY" ] || MCP_USE_API_KEY="$(get MCP_USE_API_KEY)"
[ -n "$MCP_USE_API_KEY" ] || { echo "Set MANUFACT_API_KEY in $ENV_FILE (Manufact dashboard → API keys)" >&2; exit 1; }
export MCP_USE_API_KEY

# The cloud endpoint is public, so the server refuses to start without a bearer token. Keep it in .env so
# `moot.studio_agent --endpoint https://…/mcp` sends the same token.
if [ -z "$(get MOOT_MCP_TOKEN)" ]; then
  printf '\nMOOT_MCP_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> "$ENV_FILE"
  echo "Generated MOOT_MCP_TOKEN in $ENV_FILE (clients send it as: Authorization: Bearer <token>)"
fi

SECRETS="$(mktemp)"
trap 'rm -f "$SECRETS"' EXIT
chmod 600 "$SECRETS"
for k in MOOT_MCP_TOKEN ANTHROPIC_API_KEY OPENAI_API_KEY GROQ_API_KEY ZAI_API_KEY PRIMITIVE_API_KEY PRIMITIVE_DOMAIN \
         MOOT_CLAUDE_MODEL MOOT_OPENAI_MODEL MOOT_GROQ_MODEL; do
  v="$(get "$k")"
  [ -n "$v" ] && printf '%s=%s\n' "$k" "$v" >> "$SECRETS"
done
echo "Sending these variables to Manufact: $(cut -d= -f1 "$SECRETS" | tr '\n' ' ')"

npx -y mcp-use@2.10.1 whoami
npx -y mcp-use@2.10.1 deploy --no-github --name moot --dockerfile Dockerfile --env-file "$SECRETS" "$@"
