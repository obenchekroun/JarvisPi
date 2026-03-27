#!/usr/bin/env sh

cd "$(dirname "$0")"
set -a; . ./.env; set +a;
python3 launcher.py

