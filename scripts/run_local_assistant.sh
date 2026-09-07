#!/usr/bin/env bash
# The provisioned Linux/WSL CUDA profile. No downloads or global configuration changes.
# Usage: scripts/run_local_assistant.sh PROJECT [--memory] [Harness CLI options...]
set -euo pipefail
if [[ $# -eq 0 || "$1" == --help ]]; then
  echo 'Usage: scripts/run_local_assistant.sh PROJECT [--memory] [--continue | --resume ID | other Harness options]'
  echo 'PROJECT needs PROJECT.md, or pass --context-profile with a file under PROJECT.'
  exit 0
fi
resident_repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
resident_project="$(cd -- "$1" && pwd)"
shift
resident_python_root="${HARNESS_PYTHON_ROOT:-$HOME/.local/share/uv/python}"
resident_state="${HARNESS_LOCAL_STATE:-$resident_project/.local-runtime/sessions}"
resident_weights="$resident_repo/.local-runtime/Qwen3-8B-Q4_K_M.gguf"
mkdir -p -- "$resident_state"
resident_state="$(cd -- "$resident_state" && pwd)"
resident_profile=project
resident_mounts=()
resident_mcp=(--no-mcp)
if [[ "${1:-}" == --memory ]]; then
  shift
  resident_profile=memory
  resident_memory="${HARNESS_MEMORY_ROOT:-$HOME/.claude/plugins/memory}"
  resident_vault="${HARNESS_MEMORY_VAULT:-$HOME/projects/vault}"
  resident_mounts+=(--mount "type=bind,src=$resident_memory,dst=/memory,readonly"
                   --mount "type=bind,src=$resident_vault,dst=/vault,readonly")
  resident_mcp=(--mcp-config "$resident_repo/docs/examples/local-resident/mcp.toml"
                --allow mcp__memory__memory_list)
fi
# An absent weight file leaves the interface available; readiness reports it.
if [[ -f "$resident_weights" ]]; then
  resident_mounts+=(--mount "type=bind,src=$resident_weights,dst=/models/8b.gguf,readonly")
fi
resident_context=(--context-profile "$resident_repo/docs/examples/local-resident/$resident_profile.toml")
for resident_arg in "$@"; do
  case "$resident_arg" in
    --continue|--resume|--resume=*) resident_context=() ;; # inherit the saved policy
  esac
done
exec docker run --rm --pull never -it --name "harness-local-$$" --user "$(id -u):$(id -g)" \
  --network none --memory 4g --memory-swap 4g --cpus 4 --gpus all \
  --env LITELLM_LOCAL_MODEL_COST_MAP=True --env MEMORY_VAULT_DIR=/vault \
  --mount "type=bind,src=$resident_repo,dst=$resident_repo,readonly" \
  --mount "type=bind,src=$resident_python_root,dst=$resident_python_root,readonly" \
  --mount "type=bind,src=$resident_project,dst=/project" \
  --mount "type=bind,src=$resident_state,dst=/state" \
  "${resident_mounts[@]}" --workdir /project \
  --entrypoint "$resident_repo/.venv/bin/python" \
  sha256:841b199aed2649a748875b043b32fed2e8c2d4d87e1d563556817fb7fa44b72b \
  -B "$resident_repo/.venv/bin/harness" --base-dir /state --workspace /project --no-plugins \
  --catalog "$resident_repo/docs/examples/local-resident/models-8b.toml" --model local-small \
  "${resident_context[@]}" --allow write_file "${resident_mcp[@]}" "$@"
