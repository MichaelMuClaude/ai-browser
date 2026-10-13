#!/bin/bash
# Run ai-browser server
cd "$(dirname "$0")"
source venv/bin/activate
exec python -m src.server
