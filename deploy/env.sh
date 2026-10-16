# Sourced by the other deploy scripts: finds the repository root and the OpenBench conda env's bin directory.
# Override the env with OPENBENCH_CONDA_ENV=<name> or point straight at it with OPENBENCH_BIN=/path/to/env/bin.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "${OPENBENCH_BIN:-}" ]; then
    if ! command -v conda >/dev/null 2>&1; then
        echo "conda not found on PATH; set OPENBENCH_BIN to the env's bin directory" >&2
        exit 1
    fi
    OPENBENCH_BIN="$(conda info --base)/envs/${OPENBENCH_CONDA_ENV:-openbench}/bin"
fi

if [ ! -x "$OPENBENCH_BIN/python" ]; then
    echo "no python in $OPENBENCH_BIN; create the env first (see docs/deploy.md)" >&2
    exit 1
fi
