#!/usr/bin/env bash
set -euo pipefail

# Resolve the installed source, including when invoked through a project symlink.
script_dir=$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")
if [[ $# -eq 0 ]]; then
    set -- --help
elif [[ $1 != -* ]]; then
    task_base=${1%/}
    shift
    if [[ $(basename -- "$task_base") != .batch-flow ]]; then
        task_base="$task_base/.batch-flow"
    fi
    set -- --base "$task_base" "$@"
fi
exec python3 "$script_dir/bf/front.py" launch "$@"
