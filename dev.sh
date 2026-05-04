#!/usr/bin/env bash
# SmartQueue dev launcher.
#
# Starts the FastAPI backend (port 8000) and the React/Vite frontend
# (port 8080) together. The Vite dev server proxies /api to the backend,
# so you only need to open http://localhost:8080 — staff dashboard pulls
# live data, edits flow through to the Telegram bot's department queue.
#
# Press Ctrl-C once to stop both.
set -e
cd "$(dirname "$0")"

# Kill the entire process group on exit so uvicorn and bun both die together.
trap 'echo; echo "Stopping SmartQueue dev stack..."; kill 0 2>/dev/null; exit' INT TERM

# ─── Pre-flight checks ─────────────────────────────────────────────────────

if [ ! -d ".venv" ]; then
  echo "ERROR: .venv missing. First-time setup:"
  echo "  python3 -m venv .venv"
  echo "  .venv/bin/pip install -r requirements.txt"
  exit 1
fi

if ! command -v bun >/dev/null 2>&1; then
  if [ -x "$HOME/.bun/bin/bun" ]; then
    export PATH="$HOME/.bun/bin:$PATH"
  else
    echo "ERROR: bun is not installed."
    echo "Install with: curl -fsSL https://bun.sh/install | bash"
    exit 1
  fi
fi

if [ ! -d "frontend/frontend/node_modules" ]; then
  echo "Installing frontend deps (one-time)..."
  (cd frontend/frontend && bun install)
fi

# Warn but don't block if Ollama is missing — fallback heuristics still run.
if ! curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "[WARN] Ollama not running on :11434 — LLM calls will fall back to heuristics."
  echo "       Start it with: ollama serve   (or open the Ollama.app)"
  echo
fi

# Quick port-in-use guard.
for port in 8000 8080; do
  if lsof -i :$port >/dev/null 2>&1; then
    echo "ERROR: Port $port is already in use. Free it and retry."
    echo "  lsof -i :$port      # see what's using it"
    echo "  kill \$(lsof -ti :$port)"
    exit 1
  fi
done

cat <<'EOF'
============================================================
  SmartQueue dev stack starting...

    Backend:  http://localhost:8000
              http://localhost:8000/staff   (Jinja staff page)
              http://localhost:8000/admin   (?password=admin)
    Frontend: http://localhost:8080         (React control center)

    Press Ctrl-C to stop both.
============================================================

EOF

# ─── Launch ────────────────────────────────────────────────────────────────

.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload &
(cd frontend/frontend && bun run dev) &

wait
