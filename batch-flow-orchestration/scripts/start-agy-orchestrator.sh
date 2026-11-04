#!/usr/bin/env bash
set -euo pipefail
launcher_file="$(readlink -f -- "${BASH_SOURCE[0]}")"
launcher_dir="$(dirname -- "$launcher_file")"
if [[ -L "${BASH_SOURCE[0]}" ]]; then
    workspace_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
else
    workspace_root="$PWD"
fi
exec python3 "$launcher_dir/start_agy_orchestrator.py" --workspace "$workspace_root" "$@"
