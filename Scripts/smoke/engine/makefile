# Builds the smoke test engine the way the worker builds every engine: `make EXE=<name>`.
# The output is engine.py behind a shebang for PYTHON, which must have python-chess.

EXE    ?= smoke
PYTHON ?= python3

all:
	printf '#!%s\n' "$$(command -v $(PYTHON))" > $(EXE)
	cat engine.py >> $(EXE)
	chmod +x $(EXE)
