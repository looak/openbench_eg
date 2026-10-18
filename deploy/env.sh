# Sourced by the other deploy scripts: finds the repository root and the OpenBench conda env's bin directory.
# In order: OPENBENCH_BIN=/path/to/env/bin, OPENBENCH_CONDA_ENV=<name>, the active conda env (unless it's base),
# then an env named openbench.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "${OPENBENCH_BIN:-}" ]; then
    if [ -z "${OPENBENCH_CONDA_ENV:-}" ] && [ -n "${CONDA_PREFIX:-}" ] && [ "${CONDA_DEFAULT_ENV:-}" != "base" ]; then
        OPENBENCH_BIN="$CONDA_PREFIX/bin"
    else
        if ! command -v conda >/dev/null 2>&1; then
            echo "conda not found on PATH; set OPENBENCH_BIN to the env's bin directory" >&2
            exit 1
        fi
        OPENBENCH_BIN="$(conda info --base)/envs/${OPENBENCH_CONDA_ENV:-openbench}/bin"
    fi
fi

if [ ! -x "$OPENBENCH_BIN/python" ]; then
    echo "no python in $OPENBENCH_BIN; activate OpenBench's conda env, or set OPENBENCH_CONDA_ENV=<name>" \
         "or OPENBENCH_BIN=/path/to/env/bin" >&2
    exit 1
fi

echo "using $OPENBENCH_BIN/python ($("$OPENBENCH_BIN/python" --version 2>&1))"
