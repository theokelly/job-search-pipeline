#!/bin/bash
set -e
cd /workspace

# Create venv if not present
if [ ! -d ".venv" ]; then
    echo "[setup] Creating virtual environment..."
    python3 -m venv .venv
fi

# Install project if not already installed (with dev extras for pytest-asyncio)
if ! .venv/bin/python -c "import job_agent" 2>/dev/null; then
    echo "[setup] Installing project dependencies..."
    .venv/bin/pip install -e ".[dev]" -q
fi

# Activate venv for the interactive session
export VIRTUAL_ENV="/workspace/.venv"
export PATH="$VIRTUAL_ENV/bin:$PATH"
unset PYTHONHOME

# Start Streamlit UI in the background
if [ -f "app/streamlit_app.py" ]; then
    echo "[setup] Starting Streamlit UI on port 8501..."
    nohup .venv/bin/streamlit run app/streamlit_app.py \
        --server.port 8501 --server.headless true \
        > /tmp/streamlit.log 2>&1 &
    echo "[setup] UI available at http://localhost:8501"
fi

exec "$@"
