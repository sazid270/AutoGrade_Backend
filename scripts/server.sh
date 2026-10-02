#!/bin/bash

set -euo pipefail 2>/dev/null || set -eu

# Resolve project root (one level up from the scripts directory) so this works regardless of CWD
script_dir="$(cd "$(dirname "$0")" >/dev/null 2>&1 && pwd)"
root_dir="$(cd "$script_dir/.." >/dev/null 2>&1 && pwd)"
cd "$root_dir"

# Configuration
PROJECT_NAME="autograde"
HOST="0.0.0.0"
PORT="8000"
WORKERS="3"
VENV_DIR="${VENV_DIR:-.venv}"
PYTHON_BIN="$VENV_DIR/bin/python"
GUNICORN_BIN="$VENV_DIR/bin/gunicorn"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Logging functions
log_info() { echo -e "[INFO] $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
log_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Ensure the project virtual environment exists before doing anything else
ensure_venv() {
    if [ ! -x "$PYTHON_BIN" ]; then
        log_error "Virtual environment not found at $VENV_DIR. Run scripts/setup.sh first."
        exit 1
    fi
}

# Load environment variables from .env file
load_environment() {
    if [ -f .env ]; then
        set -a; source .env; set +a
        log_info "Environment loaded from .env"
    fi
    
    # Set default Django settings module if not specified
    export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-autograde.settings}"
    log_info "Using Django settings: $DJANGO_SETTINGS_MODULE"
}

# Check if server is running
is_running() {
    [ -f logs/gunicorn.pid ] && kill -0 $(cat logs/gunicorn.pid) 2>/dev/null
}

# Stop the Django server
stop_server() {
    log_info "Stopping server..."
    
    if [ -f logs/gunicorn.pid ]; then
        local pid=$(cat logs/gunicorn.pid)
        if kill -0 $pid 2>/dev/null; then
            kill -TERM $pid
            sleep 2
            kill -0 $pid 2>/dev/null && kill -KILL $pid
        fi
        rm -f logs/gunicorn.pid
    fi
    
    pkill -f "gunicorn.*${PROJECT_NAME}" 2>/dev/null || true
    log_success "Server stopped"
}

# Start the Django server with Gunicorn
start_server() {
    log_info "Starting server..."
    
    mkdir -p logs
    
    # Check Django configuration; pipefail (set above) ensures a failing check aborts the script
    log_info "Checking Django configuration..."
    "$PYTHON_BIN" manage.py check --deploy 2>&1 | tee logs/check.log
    
    # Run migrations and collect static files
    log_info "Running migrations..."
    "$PYTHON_BIN" manage.py migrate 2>&1 | tee logs/migrate.log
    
    log_info "Collecting static files..."
    "$PYTHON_BIN" manage.py collectstatic --noinput 2>&1 | tee logs/collectstatic.log
    
    # Start Gunicorn daemon
    log_info "Starting Gunicorn server..."
    "$GUNICORN_BIN" ${PROJECT_NAME}.wsgi:application \
        --bind "$HOST:$PORT" \
        --workers $WORKERS \
        --timeout 120 \
        --max-requests 1000 \
        --preload \
        --log-file logs/gunicorn.log \
        --log-level info \
        --pid logs/gunicorn.pid \
        --daemon
    
    sleep 2
    if is_running; then
        log_success "Server started on $HOST:$PORT (PID: $(cat logs/gunicorn.pid))"
        log_info "Access your API at: http://$HOST:$PORT/"
        log_info "View logs: tail -f logs/gunicorn.log"
    else
        log_error "Failed to start server"
        [ -f logs/gunicorn.log ] && tail -10 logs/gunicorn.log
        exit 1
    fi
}

# Command handling
case "${1:-start}" in
    start|--start)
        ensure_venv
        load_environment
        if is_running; then
            log_warning "Server already running (PID: $(cat logs/gunicorn.pid))"
            exit 1
        fi
        start_server
        ;;
    stop|--stop)
        stop_server
        ;;
    restart|--restart)
        ensure_venv
        load_environment
        stop_server
        sleep 1
        start_server
        ;;
    status|--status)
        if is_running; then
            log_success "Server is running (PID: $(cat logs/gunicorn.pid))"
        else
            log_info "Server is not running"
        fi
        ;;
    logs|--logs)
        if [ -f logs/gunicorn.log ]; then
            tail -f logs/gunicorn.log
        else
            log_error "No log file found"
        fi
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        echo ""
        echo "Commands:"
        echo "  start    - Start the server"
        echo "  stop     - Stop the server"
        echo "  restart  - Restart the server"
        echo "  status   - Check server status"
        echo "  logs     - Follow server logs"
        exit 1
        ;;
esac
