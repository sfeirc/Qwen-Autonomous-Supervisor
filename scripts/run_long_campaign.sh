#!/usr/bin/env bash
# Resilient outer wrapper for `qas campaign`, for genuinely multi-hour/
# multi-day autonomous operation: if the qas process itself dies (host
# reboot, OOM kill, unhandled exception -- not just the qwen child process it
# manages, which the supervisor already recovers from internally), this
# restarts it automatically. Correctness across restarts depends entirely on
# `qas campaign`'s own durable, checkpoint-based resumability (see
# qas/campaign.py's active_campaign()/run_campaign()): the SAME total
# duration is passed on every attempt, and a resumed campaign keeps its
# original campaign_id, event-count baseline, and deadline, so the final
# report genuinely covers the full requested wall-clock span rather than
# resetting every time this script restarts it.
#
# Usage: run_long_campaign.sh <total-duration> [chaos-every] [min-successful-ticks]
#   e.g. run_long_campaign.sh 24h 30m 5
#
# Env overrides: QAS_CAMPAIGN_MAX_RESTARTS (default 1000),
#                QAS_CAMPAIGN_RESTART_DELAY_SECONDS (default 10)

set -euo pipefail

DURATION="${1:?usage: run_long_campaign.sh <total-duration> [chaos-every] [min-successful-ticks]}"
CHAOS_EVERY="${2:-}"
MIN_TICKS="${3:-1}"
MAX_RESTARTS="${QAS_CAMPAIGN_MAX_RESTARTS:-1000}"
RESTART_DELAY_SECONDS="${QAS_CAMPAIGN_RESTART_DELAY_SECONDS:-10}"

attempt=0
while true; do
  attempt=$((attempt + 1))
  args=(campaign --duration "$DURATION" --minimum-successful-ticks "$MIN_TICKS")
  if [[ -n "$CHAOS_EVERY" ]]; then
    args+=(--chaos-every "$CHAOS_EVERY")
  fi

  echo "[run_long_campaign] attempt #${attempt}: qas ${args[*]}" >&2
  if qas "${args[@]}"; then
    echo "[run_long_campaign] campaign finished successfully" >&2
    exit 0
  fi
  status=$?

  if [[ "$attempt" -ge "$MAX_RESTARTS" ]]; then
    echo "[run_long_campaign] giving up after ${attempt} attempts (last exit ${status})" >&2
    exit "$status"
  fi
  echo "[run_long_campaign] qas campaign exited ${status}; retrying in ${RESTART_DELAY_SECONDS}s" >&2
  sleep "$RESTART_DELAY_SECONDS"
done
