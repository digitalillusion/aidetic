#!/bin/sh
# aidetic in one command, and again after any update: `install` in choice_gate.py says what it does.
exec python3 "$(dirname "$0")/src/choice_gate.py" install "$@"
