#!/bin/bash
# Manual launcher: runs the same app as halodashboard.service, from wherever
# the repository is cloned. Uses the venv at the repository root.
set -e
cd "$(dirname "$(readlink -f "$0")")"
source ../venv/bin/activate
exec python app.py
