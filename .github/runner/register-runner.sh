#!/bin/bash
# Register the Trading production runner with GitHub.
#
# This Mac hosts the live stack (deploy/docker-compose.yml), so the deploy job
# of .github/workflows/deploy.yml must run ON this machine: it rebuilds the
# images and restarts the containers bound to 127.0.0.1. The runner is therefore
# registered at REPOSITORY level, exactly like the Culia one.
#
# 1. Generate a registration token (repo admin only):
#    https://github.com/ms-tristan/trading/settings/actions/runners
#      -> "New self-hosted runner" -> macOS / ARM64 -> copy the --token value
# 2. Run this script and paste the token when prompted:
#      ~/actions-runner-trading/register.sh
#    or non-interactively:
#      ACTIONS_RUNNER_TOKEN=<token> ~/actions-runner-trading/register.sh
#
# The labels passed below are the ones deploy.yml targets:
#   runs-on: [self-hosted, macOS, ARM64]
set -euo pipefail

cd "$(dirname "$0")"

REPO_URL="https://github.com/ms-tristan/trading"
RUNNER_NAME="trading-prod-mac"

if [[ -f .runner ]]; then
  echo "Runner already registered: $(grep -o '"agentName": *"[^"]*"' .runner)"
  exit 0
fi

TOKEN="${ACTIONS_RUNNER_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
  read -r -s -p "Registration token: " TOKEN
  echo
fi

./config.sh \
  --url "$REPO_URL" \
  --token "$TOKEN" \
  --name "$RUNNER_NAME" \
  --labels self-hosted,macOS,ARM64 \
  --work _work \
  --unattended \
  --replace

echo
echo "Runner registered. Start it now (foreground test):"
echo "  ~/actions-runner-trading/run.sh"
echo "or load the launchd agent so it survives reboots:"
echo "  ./svc.sh install && ./svc.sh start"
