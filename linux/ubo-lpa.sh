#!/usr/bin/env bash
# Convenience launcher. All logic lives in ../ubo-lpa.py.
exec python3 "$(dirname "$(readlink -f "$0")")/../ubo-lpa.py" "$@"
