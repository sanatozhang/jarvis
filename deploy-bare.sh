#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Jarvis 裸机部署脚本（无 Docker）
#
# 用法:
#   首次部署:  ./deploy-bare.sh setup
#   启动:      ./deploy-bare.sh start
#   停止:      ./deploy-bare.sh stop
#   重启:      ./deploy-bare.sh restart
#   更新:      ./deploy-bare.sh update
#   状态:      ./deploy-bare.sh status
#   日志:      ./deploy-bare.sh logs
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

BACKEND_PORT=${BACKEND_PORT:-8000}
FRONTEND_PORT=${FRONTEND_PORT:-3000}
PID_DIR="$SCRIPT_DIR/.pids"
LOG_DIR="$SCRIPT_DIR/logs"

mkdir -p "$PID_DIR" "$LOG_DIR"

# ---- Check prerequisites ----
check_deps() {
    local missing=()
    command -v python3 >/dev/null 2>&1 || missing+=("python3")
    command -v node >/dev/null 2>&1 || missing+=("node")
    command -v npm >/dev/null 2>&1 || missing+=("npm")
    if [ ${#missing[@]} -ne 0 ]; then
        err "Missing: ${missing[*]}"
        err "Please install: Python 3.11+, Node.js 18+"
        exit 1
    fi
    log "python3: $(python3 --version 2>&1)"
    log "node: $(node --version 2>&1)"
}

# ---- Setup (first time) ----
cmd_setup() {
    log "Setting up Jarvis (bare metal)..."
    check_deps

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
        warn "  Optional:"
        warn "    OPENAI_API_KEY=sk-xxx"
        warn "    ZENDESK_EMAIL / ZENDESK_API_TOKEN"
        warn ""
        warn "  Edit with: nano .env"
        warn ""
        read -p "Press Enter after editing .env (or Ctrl+C to abort)..."
    fi

    # Backend setup
    log "Setting up backend..."
    cd backend
    if [ ! -d .venv ]; then
        python3 -m venv .venv
    fi
    source .venv/bin/activate
    pip install -q -r requirements.txt
    deactivate
    cd ..
    log "✅ Backend dependencies installed"

    # Frontend setup
    log "Setting up frontend..."
    cd frontend
    npm install --silent
    npm run build 2>&1 | tail -3
    cd ..
    log "✅ Frontend built"

    # Create data dirs
    mkdir -p data workspaces

    log ""
    log "=========================================="
    log "  Setup complete!"
    log "=========================================="
    log ""
    log "  Start:  ./deploy-bare.sh start"
    log ""
}

# ---- Start ----
cmd_start() {
    log "Starting Jarvis..."

    # Load .env
    if [ -f .env ]; then
        set -a
        source .env
        set +a
    fi

    # Start backend
    if [ -f "$PID_DIR/backend.pid" ] && kill -0 "$(cat "$PID_DIR/backend.pid")" 2>/dev/null; then
        warn "Backend already running (PID $(cat "$PID_DIR/backend.pid"))"
    else
        log "Starting backend on port $BACKEND_PORT..."
        cd backend
        source .venv/bin/activate
        nohup python -m uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" \
            > "$LOG_DIR/backend.log" 2>&1 &
        echo $! > "$PID_DIR/backend.pid"
        deactivate
        cd ..
        log "✅ Backend started (PID $(cat "$PID_DIR/backend.pid"))"
    fi

    # Start frontend
    if [ -f "$PID_DIR/frontend.pid" ] && kill -0 "$(cat "$PID_DIR/frontend.pid")" 2>/dev/null; then
        warn "Frontend already running (PID $(cat "$PID_DIR/frontend.pid"))"
    else
        log "Starting frontend on port $FRONTEND_PORT..."
        cd frontend
        nohup npx next start -p "$FRONTEND_PORT" \
            > "$LOG_DIR/frontend.log" 2>&1 &
        echo $! > "$PID_DIR/frontend.pid"
        cd ..
        log "✅ Frontend started (PID $(cat "$PID_DIR/frontend.pid"))"
    fi

    sleep 2

    log ""
    log "=========================================="
    log "  Jarvis is running!"
    log "=========================================="
    log ""
    log "  Frontend:  http://localhost:$FRONTEND_PORT"
    log "  Backend:   http://localhost:$BACKEND_PORT"
    log "  API Docs:  http://localhost:$BACKEND_PORT/docs"
    log ""
    log "  Logs:      ./deploy-bare.sh logs"
    log "  Stop:      ./deploy-bare.sh stop"
    log ""
}

# ---- Stop ----
cmd_stop() {
    log "Stopping Jarvis..."

    for svc in backend frontend; do
        if [ -f "$PID_DIR/$svc.pid" ]; then
            pid=$(cat "$PID_DIR/$svc.pid")
            if kill -0 "$pid" 2>/dev/null; then
                kill "$pid" 2>/dev/null
                log "Stopped $svc (PID $pid)"
            fi
            rm -f "$PID_DIR/$svc.pid"
        fi
    done

    # Also kill by port as fallback
    lsof -ti:"$BACKEND_PORT" | xargs kill -9 2>/dev/null || true
    lsof -ti:"$FRONTEND_PORT" | xargs kill -9 2>/dev/null || true

    log "✅ Stopped."
}

# ---- Restart ----
cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

# ---- Update (pull + rebuild + restart) ----
cmd_update() {
    log "Updating Jarvis..."

    if [ -d .git ]; then
        log "Pulling latest code..."
        git pull
    fi

    # Update backend deps
    log "Updating backend dependencies..."
    cd backend
    source .venv/bin/activate
    pip install -q -r requirements.txt
    deactivate
    cd ..

    # Rebuild frontend
    log "Rebuilding frontend..."
    cd frontend
    npm install --silent
    npm run build 2>&1 | tail -3
    cd ..

    # Restart
    cmd_stop
    sleep 1
    cmd_start

    log "✅ Updated and restarted."
}

# ---- Logs ----
cmd_logs() {
    local svc="${1:-all}"
    if [ "$svc" = "backend" ]; then
        tail -f "$LOG_DIR/backend.log"
    elif [ "$svc" = "frontend" ]; then
        tail -f "$LOG_DIR/frontend.log"
    else
        tail -f "$LOG_DIR/backend.log" "$LOG_DIR/frontend.log"
    fi
}

# ---- Status ----
cmd_status() {
    log "Service status:"
    for svc in backend frontend; do
        if [ -f "$PID_DIR/$svc.pid" ] && kill -0 "$(cat "$PID_DIR/$svc.pid")" 2>/dev/null; then
            echo -e "  ${GREEN}●${NC} $svc  PID $(cat "$PID_DIR/$svc.pid")"
        else
            echo -e "  ${RED}●${NC} $svc  stopped"
        fi
    done
    echo ""

    log "Health check:"
    if curl -sf "http://localhost:$BACKEND_PORT/api/health" 2>/dev/null | python3 -m json.tool 2>/dev/null; then
        true
    else
        warn "Backend not responding"
    fi
}

# ---- Main ----
case "${1:-help}" in
    setup)   cmd_setup ;;
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    update)  cmd_update ;;
    logs)    shift; cmd_logs "${1:-all}" ;;
    status)  cmd_status ;;
    *)
        echo "Jarvis Bare Metal Deploy Script (no Docker)"
        echo ""
        echo "Usage: $0 <command>"
        echo ""
        echo "Commands:"
        echo "  setup     First-time setup (install deps, build, config)"
        echo "  start     Start backend + frontend"
        echo "  stop      Stop all services"
        echo "  restart   Stop + start"
        echo "  update    Pull code + rebuild + restart"
        echo "  status    Check service status + health"
        echo "  logs      View logs (add 'backend' or 'frontend' to filter)"
        ;;
esac
