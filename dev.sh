#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════
# Synodic — Dev Runner (thin wrapper over docker compose)
# ══════════════════════════════════════════════════════════════════════
#
# Everything runs in containers; backend + frontend source is bind-mounted
# so uvicorn --reload and vite HMR pick up edits live.
#
#   ./dev.sh up             Start the full stack (infra + apps)
#   ./dev.sh infra          Start only postgres + redis + falkordb
#                           (for running backend/frontend on the host)
#   ./dev.sh down           Stop everything (data preserved)
#   ./dev.sh restart <svc>  Recover one service
#   ./dev.sh rebuild [svc]  Rebuild image + recreate (picks up Dockerfile/deps changes)
#   ./dev.sh logs [svc]     Tail logs (all services or one)
#   ./dev.sh ps             Container status
#   ./dev.sh shell <svc>    Open a shell inside a running service
#   ./dev.sh reset          Wipe all data volumes (interactive confirm)
#   ./dev.sh doctor         Run preflight checks, no side effects
#
# Service names: viz-service, aggregation-controlplane, aggregation-worker,
#                stats-service, graph-service, frontend, postgres, redis,
#                falkordb
#
# For VM / self-host deployment, use ./deploy.sh instead.
# ══════════════════════════════════════════════════════════════════════
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env.dev"
COMPOSE=(docker compose --env-file "$ENV_FILE"
         -f docker-compose.yml
         -f docker-compose.dev.yml)

# ── Bootstrap .env.dev on first run ────────────────────────────────
if [ ! -f "$ENV_FILE" ]; then
    if [ -f .env.example ]; then
        echo "[dev] creating $ENV_FILE from .env.example"
        cp .env.example "$ENV_FILE"
    else
        echo "[dev] error: $ENV_FILE missing and .env.example not found" >&2
        exit 1
    fi
fi

usage() { sed -n '4,21p' "$0" | sed 's/^# //; s/^#//'; }

cmd="${1:-up}"
shift || true

case "$cmd" in
    up)
        "${COMPOSE[@]}" up -d "$@"
        echo ""
        echo "  Frontend     http://localhost:${FRONTEND_PORT:-5173}"
        echo "  Backend API  http://localhost:${VIZ_PORT:-8000}/docs"
        echo "  Logs:        ./dev.sh logs [service]"
        echo "  Status:      ./dev.sh ps"
        ;;
    infra)
        "${COMPOSE[@]}" up -d postgres redis falkordb
        set -a; source "$ENV_FILE"; set +a
        echo ""
        echo "  Postgres   localhost:${POSTGRES_PORT:-5432}  (${POSTGRES_USER}/${POSTGRES_PASSWORD})"
        echo "  Redis      localhost:${REDIS_PORT:-6380}"
        echo "  FalkorDB   localhost:${FALKORDB_PORT:-6379}  (browser http://localhost:${FALKORDB_UI_PORT:-3000})"
        echo ""
        echo "  Run apps on the host against this infra:"
        echo "    source .venv/bin/activate && set -a && source .env.dev && set +a"
        echo "    python -m uvicorn backend.app.main:app --reload --host 0.0.0.0 --port 8000"
        echo "    (cd frontend && npm run dev)"
        ;;
    down)
        "${COMPOSE[@]}" down "$@"
        ;;
    restart)
        [ "$#" -gt 0 ] || { echo "usage: ./dev.sh restart <service>" >&2; exit 1; }
        "${COMPOSE[@]}" restart "$@"
        ;;
    build)
        "${COMPOSE[@]}" build "$@"
        ;;
    rebuild)
        "${COMPOSE[@]}" up -d --build "$@"
        ;;
    logs)
        "${COMPOSE[@]}" logs -f --tail=100 "$@"
        ;;
    ps|status)
        "${COMPOSE[@]}" ps
        ;;
    shell|exec)
        svc="${1:-}"
        [ -n "$svc" ] || { echo "usage: ./dev.sh shell <service>" >&2; exit 1; }
        shift
        "${COMPOSE[@]}" exec "$svc" "${@:-/bin/sh}"
        ;;
    reset)
        echo "This WIPES all data volumes (postgres + redis + falkordb)."
        read -rp "Type 'yes' to confirm: " a
        [ "$a" = yes ] || { echo "Aborted."; exit 0; }
        "${COMPOSE[@]}" down -v
        echo "Data wiped. Run './dev.sh up' to start fresh."
        ;;
    doctor)
        # Source the preflight library for doctor-style checks that don't
        # require the stack to be up.
        # shellcheck source=scripts/preflight.sh
        source "$(dirname "$0")/scripts/preflight.sh"
        set -a; source "$ENV_FILE"; set +a
        run_doctor
        ;;
    help|-h|--help|"")
        usage
        ;;
    *)
        echo "Unknown command: $cmd" >&2
        usage
        exit 1
        ;;
esac
