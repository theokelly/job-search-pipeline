#!/bin/bash
# Start the Job Agent Streamlit UI
cd "$(dirname "$0")"
export $(grep -v '^#' .env | xargs)
python3 -m streamlit run app/streamlit_app.py --server.port 8501 --server.headless true
