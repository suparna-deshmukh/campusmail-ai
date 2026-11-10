#!/bin/bash
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then echo "Python 3.9+ is required. Install from python.org"; exit 1; fi
if [ ! -d .venv ]; then echo "Creating virtual environment..."; "$PY" -m venv .venv; fi
if [ -f .venv/Scripts/activate ]; then . .venv/Scripts/activate; else . .venv/bin/activate; fi
pip install -q -r requirements.txt
python app.py
