#!/usr/bin/env bash
# Strata schemachange deploy entrypoint.
#
# Usage: ./deploy_and_verify.sh <env> [secret_name]
#   <env>          preprod | prod
#   [secret_name]  AWS SecretsManager secret to fetch STRATA_SSH_USER's private
#                  key + account locator from. Omit for LOCAL runs where the
#                  key is already on disk and env is set via .env / shell.
#
# LOCAL usage (no AWS): export SNOWFLAKE_ACCOUNT + SNOWFLAKE_PRIVATE_KEY_PATH
# first, then `./deploy_and_verify.sh preprod`.
set -euo pipefail
cd "$(dirname "$0")"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'
BOLD='\033[1m'; NC='\033[0m'

usage() {
  echo "Usage: $0 <preprod|prod> [aws_secret_name]" >&2
  exit 1
}

if [[ $# -lt 1 ]]; then usage; fi
ENV_NAME=$1
SECRET_NAME=${2:-}

case "$ENV_NAME" in
  preprod|prod) ;;
  *) echo "unknown env: $ENV_NAME" >&2; usage;;
esac

CONF_FILE="schemachange/schemachange-config.${ENV_NAME}.yml"
if [[ ! -f "$CONF_FILE" ]]; then
  echo "config file not found: $CONF_FILE" >&2
  exit 1
fi

echo -e "\n${BLUE}${BOLD}==== Strata deploy: env=$ENV_NAME conf=$CONF_FILE ====${NC}\n"

# 1. Credentials: from AWS SecretsManager if a secret name is given, else assume
#    they're already in the environment.
if [[ -n "$SECRET_NAME" ]]; then
  echo -e "${YELLOW}→ fetching Snowflake creds from AWS Secrets Manager ($SECRET_NAME)${NC}"
  : "${AWS_REGION:?AWS_REGION must be set when fetching a secret}"
  content=$(aws secretsmanager get-secret-value \
    --secret-id "$SECRET_NAME" --region "$AWS_REGION" \
    | jq -r '.SecretString')
  export SNOWFLAKE_ACCOUNT
  SNOWFLAKE_ACCOUNT=$(jq -r '.SNOWFLAKE_ACCOUNT' <<< "$content")
  # Write key to a temp file with tight perms; schemachange reads it by path.
  key_path=$(mktemp -t strata_key.XXXXXX)
  jq -r '.STRATA_SSH_USER_KEY' <<< "$content" > "$key_path"
  chmod 600 "$key_path"
  export SNOWFLAKE_PRIVATE_KEY_PATH=$key_path
  trap 'rm -f "$key_path"' EXIT
fi

: "${SNOWFLAKE_ACCOUNT:?SNOWFLAKE_ACCOUNT must be set (env or secret)}"
: "${SNOWFLAKE_PRIVATE_KEY_PATH:?SNOWFLAKE_PRIVATE_KEY_PATH must be set (env or secret)}"

# 2. Render projects/ into schemachange/changes/. STRATA_DEPLOY_PROJECT (set by the
#    orchestrator) scopes the render to ONE project; unset = render all (legacy).
echo -e "\n${YELLOW}→ rendering changes for env=$ENV_NAME${STRATA_DEPLOY_PROJECT:+ project=$STRATA_DEPLOY_PROJECT}${NC}"
if [[ -n "${STRATA_DEPLOY_PROJECT:-}" ]]; then
  python3 scripts/render_changes.py --env "$ENV_NAME" --project "$STRATA_DEPLOY_PROJECT"
else
  python3 scripts/render_changes.py --env "$ENV_NAME"
fi

# 2b. Optionally commit the rendered migrations back to the deploy branch so the
# repo always shows exactly what schemachange is about to apply. Gated on
# STRATA_COMMIT_RENDERED_BRANCH (set by the Celery worker). Runs BEFORE deploy so
# the branch reflects the attempt; ``[skip ci]`` avoids re-triggering repo CI.
if [[ -n "${STRATA_COMMIT_RENDERED_BRANCH:-}" ]]; then
  echo -e "\n${YELLOW}→ committing rendered changes/ to ${STRATA_COMMIT_RENDERED_BRANCH}${NC}"
  git add schemachange/changes
  if git diff --cached --quiet; then
    echo "  (no rendered-file changes to commit)"
  else
    git -c user.name="strata-deploy-bot" -c user.email="strata-deploy@joveo.com" \
      commit -m "chore(deploy): rendered ${ENV_NAME} migrations [skip ci]"
    # Best-effort: a lost push race (branch advanced under us) or a transient
    # git error must NOT block the actual schemachange deploy that follows. The
    # next deploy re-renders and re-commits, so a skipped push self-heals.
    if git push origin "HEAD:${STRATA_COMMIT_RENDERED_BRANCH}"; then
      echo "  committed + pushed rendered changes/ to ${STRATA_COMMIT_RENDERED_BRANCH}"
    else
      echo -e "${YELLOW}  WARN: push to ${STRATA_COMMIT_RENDERED_BRANCH} failed; continuing with deploy${NC}"
    fi
  fi
fi

# 3. Deploy.
echo -e "\n${YELLOW}→ schemachange deploy${NC}"
schemachange deploy --config-file "$CONF_FILE" --snowflake-account "$SNOWFLAKE_ACCOUNT"

# 4. Prune orphans — objects removed from the repo that schemachange can't drop.
#    ALWAYS project-scoped: runs only when STRATA_DEPLOY_PROJECT is set (which every
#    deploy should set — see the orchestrator). schemachange never drops, so this is
#    where deletions get applied. DRY-RUN by default (only LOGS); STRATA_PRUNE_APPLY=
#    true to actually drop. Non-fatal.
if [[ -n "${STRATA_DEPLOY_PROJECT:-}" ]]; then
  prune_mode="dry-run"
  prune_args=(--env "$ENV_NAME" --project "$STRATA_DEPLOY_PROJECT")
  if [[ "${STRATA_PRUNE_APPLY:-}" == "true" ]]; then
    prune_args+=(--apply)
    prune_mode="APPLY"
  fi
  echo -e "\n${YELLOW}→ prune orphans (project=$STRATA_DEPLOY_PROJECT, $prune_mode)${NC}"
  python3 scripts/prune_orphans.py "${prune_args[@]}" \
    || echo -e "${YELLOW}  WARN: prune step failed; continuing (objects already deployed)${NC}"
else
  echo -e "\n${YELLOW}→ prune skipped: STRATA_DEPLOY_PROJECT not set (deploy isn't project-scoped)${NC}"
fi

echo -e "\n${GREEN}${BOLD}==== deploy for env=$ENV_NAME completed ====${NC}\n"
