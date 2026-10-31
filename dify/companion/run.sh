#!/usr/bin/env bash
set -euo pipefail
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export SEEKDB_HOST=${SEEKDB_HOST:-127.0.0.1}
export SEEKDB_PORT=${SEEKDB_PORT:-2881}
exec uvicorn app:app --host 0.0.0.0 --port 7870
