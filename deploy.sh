#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Jarvis 一键部署脚本
#
# 用法:
#   首次部署:  ./deploy.sh setup
#   启动:      ./deploy.sh start
#   停止:      ./deploy.sh stop
#   重启:      ./deploy.sh restart
#   查看日志:  ./deploy.sh logs
#   更新:      ./deploy.sh update
#   状态:      ./deploy.sh status
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[Jarvis]${NC} $*"; }
warn() { echo -e "${YELLOW}[Jarvis]${NC} $*"; }
err()  { echo -e "${RED}[Jarvis]${NC} $*" >&2; }

# ---- Check prerequisites ----
check_deps() {
    local missing=()
    command -v docker >/dev/null 2>&1 || missing+=("docker")
    command -v docker compose >/dev/null 2>&1 || {
        command -v docker-compose >/dev/null 2>&1 || missing+=("docker-compose")
    }
    if [ ${#missing[@]} -ne 0 ]; then
        err "Missing: ${missing[*]}"
        err "Please install Docker: https://docs.docker.com/get-docker/"
        exit 1
    fi
}

# ---- Docker compose wrapper (supports v1 and v2) ----
dc() {
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    else
        docker-compose "$@"
    fi
}

# ---- Setup (first time) ----
cmd_setup() {
    log "Setting up Jarvis..."

    # Create .env if missing
    if [ ! -f .env ]; then
        log "Creating .env from template..."
        cp .env.example .env
        warn ""
        warn "=========================================="
        warn "  Please edit .env with your credentials:"
        warn "=========================================="
        warn ""
        warn "  Required:"
        warn "    FEISHU_APP_ID=cli_xxx"
        warn "    FEISHU_APP_SECRET=xxx"
        warn ""
        warn "  Optional (for full features):"
        warn "    ZENDESK_EMAIL=xxx"
        warn "    ZENDESK_API_TOKEN=xxx"
        warn "    OPENAI_API_KEY=sk-xxx"
        warn ""
        warn "  Edit with: nano .env"
        warn ""
        read -p "Press Enter after editing .env (or Ctrl+C to abort)..."
    fi

    # Validate required env vars
    source .env 2>/dev/null || true
    if [ -z "${FEISHU_APP_ID:-}" ] || [ -z "${FEISHU_APP_SECRET:-}" ]; then
        err "FEISHU_APP_ID and FEISHU_APP_SECRET are required in .env"
        exit 1
    fi

    log "Building Docker images..."
    dc build

    log "Starting services..."
    dc up -d

    log "Waiting for services to start..."
    sleep 5

    # Health check
    if curl -sf http://localhost:${BACKEND_PORT:-8000}/api/health >/dev/null 2>&1; then
        log "✅ Backend is healthy"
    else
        warn "⚠ Backend may still be starting..."
    fi

    log ""
    log "=========================================="
    log "  Jarvis is running!"
    log "=========================================="
    log ""
    log "  Frontend:  http://localhost:${FRONTEND_PORT:-3000}"
    log "  Backend:   http://localhost:${BACKEND_PORT:-8000}"
    log "  API Docs:  http://localhost:${BACKEND_PORT:-8000}/docs"
    log ""
    log "  View logs: ./deploy.sh logs"
    log "  Stop:      ./deploy.sh stop"
    log ""
}

# ---- Start ----
cmd_start() {
    log "Starting Jarvis..."
    dc up -d
    log "✅ Started. Frontend: http://localhost:${FRONTEND_PORT:-3000}"
}

# ---- Stop ----
cmd_stop() {
    log "Stopping Jarvis..."
    dc down
    log "✅ Stopped."
}

# ---- Restart ----
cmd_restart() {
    log "Restarting Jarvis..."
    dc down
    dc up -d
    log "✅ Restarted."
}

# ---- Logs ----
cmd_logs() {
    dc logs -f --tail=100 "${@:-}"
}

# ---- Update (pull + rebuild + restart) ----
cmd_update() {
    log "Updating Jarvis..."

    if [ -d .git ]; then
        log "Pulling latest code..."
        git pull
    fi

    log "Rebuilding images..."
    dc build

    log "Restarting with new images..."
    dc down
    dc up -d

    log "✅ Updated and restarted."
}

# ---- Status ----
cmd_status() {
    log "Service status:"
    dc ps
    echo ""

    log "Health check:"
    if curl -sf http://localhost:${BACKEND_PORT:-8000}/api/health 2>/dev/null | python3 -m json.tool 2>/dev/null; then
        true
    else
        warn "Backend not responding (may be starting...)"
    fi
}

# ---- Local dev (no Docker) ----
cmd_dev() {
    log "Starting in dev mode (no Docker)..."

    # Backend
    if [ ! -d backend/.venv ]; then
        log "Creating Python venv..."
        cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && cd ..
    fi

    log "Starting backend..."
    cd backend && source .venv/bin/activate && python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload &
    BACKEND_PID=$!
    cd ..

    # Frontend
    if [ ! -d frontend/node_modules ]; then
        log "Installing frontend deps..."
        cd frontend && npm install && cd ..
    fi

    log "Starting frontend..."
    cd frontend && npm run dev &
    FRONTEND_PID=$!
    cd ..

    log ""
    log "Frontend: http://localhost:3000"
    log "Backend:  http://localhost:8000"
    log "Press Ctrl+C to stop"

    trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0" INT TERM
    wait
}

# ---- Main ----
check_deps

case "${1:-help}" in
    setup)   cmd_setup ;;
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    logs)    shift; cmd_logs "$@" ;;
    update)  cmd_update ;;
    status)  cmd_status ;;
    dev)     cmd_dev ;;
    *)
        echo "Jarvis Deploy Script"
        echo ""
        echo "Usage: $0 <command>"
        echo ""
        echo "Commands:"
        echo "  setup     First-time setup (create .env, build, start)"
        echo "  start     Start all services"
        echo "  stop      Stop all services"
        echo "  restart   Restart all services"
        echo "  logs      View logs (add 'backend' or 'frontend' to filter)"
        echo "  update    Pull code + rebuild + restart"
        echo "  status    Check service status"
        echo "  dev       Start in local dev mode (no Docker)"
        ;;
esac
