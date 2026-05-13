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

# ---- Check docker daemon reachable, auto-start colima on macOS ----
check_docker_daemon() {
    if docker info >/dev/null 2>&1; then
        return 0
    fi
    warn "Docker daemon not reachable."
    if [[ "$(uname -s)" == "Darwin" ]] && command -v colima >/dev/null 2>&1; then
        warn "Detected colima on macOS — starting it..."
        if colima start; then
            # wait up to 30s for daemon
            for i in $(seq 1 30); do
                if docker info >/dev/null 2>&1; then
                    log "✅ Docker daemon is up (colima)"
                    return 0
                fi
                sleep 1
            done
            err "colima started but docker daemon still not reachable after 30s"
            exit 1
        else
            err "Failed to start colima. Run 'colima start' manually."
            exit 1
        fi
    fi
    err "Docker daemon not running. Start Docker Desktop / colima / dockerd first."
    exit 1
}

# ---- Docker compose wrapper (supports v1 and v2) ----
dc() {
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    else
        docker-compose "$@"
    fi
}

# ---- Prune dangling images (避免每次 rebuild 后旧 image 堆磁盘) ----
prune_dangling() {
    local before after freed
    before=$(docker images -f dangling=true -q | wc -l | tr -d ' ')
    if [ "$before" -gt 0 ]; then
        log "Pruning $before dangling image(s)..."
        # 仅 prune dangling，不动 named image（防误删）
        docker image prune -f >/dev/null 2>&1 || true
        after=$(docker images -f dangling=true -q | wc -l | tr -d ' ')
        freed=$((before - after))
        log "✅ Pruned $freed dangling image(s)"
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

    # Ensure data directories exist (for bind mounts)
    mkdir -p data workspaces
    
    # Stop bare-metal services if running (avoid port conflicts)
    if [ -f .pids/backend.pid ] && kill -0 "$(cat .pids/backend.pid 2>/dev/null)" 2>/dev/null; then
        warn "Stopping bare-metal services first..."
        ./deploy-bare.sh stop 2>/dev/null || true
    fi

    # Migrate: if data exists from bare-metal deploy, it will be used directly
    if [ -f data/jarvis.db ]; then
        log "✅ Found existing database ($(du -sh data/jarvis.db | cut -f1)) — data will be preserved"
    fi
    if [ -d workspaces ] && [ "$(ls workspaces/ 2>/dev/null | wc -l)" -gt 0 ]; then
        log "✅ Found existing workspaces ($(du -sh workspaces/ | cut -f1)) — data will be preserved"
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

    # Auto-install boot persistence (idempotent — safe to re-run)
    log ""
    log "Configuring boot-time auto-start..."
    setup_autostart || warn "⚠ Auto-start setup encountered an issue; services still run for this session"

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

# ---- Update (pull + rebuild + restart + prune) ----
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

    # 治本闭环：rebuild 后旧 image 变 dangling，长期堆磁盘
    prune_dangling

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

# ---- Boot-time auto-start (cross-platform, idempotent) ----
# Called automatically by cmd_setup. Safe to re-run on existing installs.
setup_autostart() {
    local os
    os="$(uname -s)"
    case "$os" in
        Linux)   _autostart_linux ;;
        Darwin)  _autostart_macos ;;
        *)       warn "Unsupported OS '$os' for auto-start — skipping"; return 0 ;;
    esac
}

_autostart_linux() {
    if ! command -v systemctl >/dev/null 2>&1; then
        warn "systemctl not found — skipping auto-start (manual init system not supported)"
        return 0
    fi
    if ! systemctl list-unit-files docker.service >/dev/null 2>&1; then
        warn "docker.service unit not found — skipping auto-start"
        return 0
    fi
    if [ "$EUID" -ne 0 ] && ! command -v sudo >/dev/null 2>&1; then
        warn "Not root and no sudo — skipping systemd auto-start install"
        return 0
    fi

    local SUDO=""
    [ "$EUID" -ne 0 ] && SUDO="sudo"

    # 1. enable docker.service
    if ! systemctl is-enabled docker.service >/dev/null 2>&1; then
        log "Enabling docker.service..."
        $SUDO systemctl enable --now docker.service
    fi

    # 2. install/refresh jarvis.service unit (idempotent)
    local unit_path=/etc/systemd/system/jarvis.service
    local workdir="$SCRIPT_DIR"
    local docker_bin
    docker_bin="$(command -v docker)"
    local invoking_user="${SUDO_USER:-$USER}"

    log "Writing $unit_path (WorkingDirectory=$workdir, User=$invoking_user)"
    $SUDO tee "$unit_path" >/dev/null <<EOF
[Unit]
Description=Jarvis Docker Compose Stack
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$workdir
User=$invoking_user
Group=$invoking_user
ExecStart=$docker_bin compose up -d
ExecStop=$docker_bin compose down
TimeoutStartSec=0
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

    $SUDO systemctl daemon-reload
    $SUDO systemctl enable jarvis.service
    log "✅ jarvis.service installed and enabled (will auto-start on boot)"
}

_autostart_macos() {
    if ! command -v colima >/dev/null 2>&1 || ! command -v brew >/dev/null 2>&1; then
        warn "colima or brew not found — skipping macOS auto-start"
        return 0
    fi

    # Idempotent: brew services start is no-op if already started
    log "Registering colima with brew services (launchd)..."
    brew services start colima >/dev/null 2>&1 || true
    brew services list | grep -E "^colima" | sed 's/^/    /'

    # Check macOS auto-login (LaunchAgent only fires on login)
    local autologin
    autologin="$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null || true)"
    if [ -z "$autologin" ]; then
        warn "⚠ macOS auto-login NOT configured — LaunchAgent fires at LOGIN, not boot."
        warn "  Enable: System Settings → Users & Groups → Automatic login"
    else
        log "✅ Auto-login user: $autologin (LaunchAgent fires after boot)"
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

# Daemon check only for commands that actually need Docker running
case "${1:-help}" in
    setup|start|restart|update|status|logs|stop) check_docker_daemon ;;
esac

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
        echo "  setup     First-time setup (.env, build, start, install boot auto-start)"
        echo "  start     Start all services"
        echo "  stop      Stop all services"
        echo "  restart   Restart all services"
        echo "  logs      View logs (add 'backend' or 'frontend' to filter)"
        echo "  update    Pull code + rebuild + restart"
        echo "  status    Check service status"
        echo "  dev       Start in local dev mode (no Docker)"
        ;;
esac
