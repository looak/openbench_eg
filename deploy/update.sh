#!/usr/bin/env bash
# Pulls the latest OpenBench, installs Python deps, migrates the database and restarts the service.
# Wrapped in a function so bash has read the whole script before git pull can change it.
set -euo pipefail

main() {
    source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
    cd "$REPO_DIR"

    git pull --ff-only
    "$OPENBENCH_BIN/python" -m pip install --quiet -r requirements.txt
    "$OPENBENCH_BIN/python" manage.py migrate
    "$OPENBENCH_BIN/python" manage.py collectstatic --noinput --verbosity 0

    SERVICE_NAME="${OPENBENCH_SERVICE:-openbench}"
    sudo systemctl restart "$SERVICE_NAME"
    systemctl status "$SERVICE_NAME" --no-pager
}

main "$@"
