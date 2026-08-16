#!/usr/bin/env bash
# Serve a local model for the harness `local` alias (~/.config/harness/models.toml).
#
# Path: the official llama.cpp CUDA *container*. Chosen because llama.cpp ships NO
# Linux-CUDA prebuilt binary, and this WSL2 box has Docker but no CUDA toolkit and
# no NVIDIA Vulkan ICD. The image brings the CUDA runtime; your driver + the NVIDIA
# Container Toolkit provide GPU access — no host toolkit, no compiling.
#
# PREREQUISITE: GPU-in-Docker. On Docker Desktop (WSL2 backend) this is BUILT IN —
# NO NVIDIA Container Toolkit needed. Verify once (should list your GPU):
#   docker run --rm --gpus all ubuntu nvidia-smi
# (Only native dockerd-in-WSL needs `sudo apt install nvidia-container-toolkit`.)
#
# Tuned for a 12 GB Blackwell card (RTX 5070) with Qwen3.6-35B-A3B (MoE,
# ~17.7 GB at UD-IQ4_XS): expert tensors spill to RAM while attention stays
# on the GPU — interactive because only ~3.5B params fire per token.
#
# RAM PREREQUISITE: the CPU-side experts (~10 GB) must stay in page cache or
# every token re-reads them from the virtual disk (observed: 0.5-4 tok/s with
# 15x swings). WSL2's default VM cap (50% of host RAM = ~15.8 GB here) is not
# enough next to Docker + the agent stack; ~/.wslconfig on the Windows side
# raises it to 22GB (see C:\Users\<user>\.wslconfig, applied via wsl --shutdown).
#
# Old Qwen3-Coder remains reachable via
# HARNESS_LOCAL_MODEL=unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:IQ4_XS
# First launch downloads the GGUF into the mounted cache volume.
set -euo pipefail

MODEL="${HARNESS_LOCAL_MODEL:-unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ4_XS}"
PORT="${HARNESS_LOCAL_PORT:-8080}"
# 8192 ctx (down from 16384) halves the KV cache; the freed VRAM pays for two
# expert layers moved off the CPU (NCPUMOE 26, was 28). Revert with
# HARNESS_LOCAL_CTX=16384 HARNESS_LOCAL_NCPUMOE=28 if long sessions need it.
CTX="${HARNESS_LOCAL_CTX:-8192}"
# Expert layers parked on the CPU. Lower = more GPU residency/speed; raise if it
# OOMs on load. Confirm --n-cpu-moe/--jinja on the image: `... server-cuda --help`.
NCPUMOE="${HARNESS_LOCAL_NCPUMOE:-26}"
CACHE="${HARNESS_LOCAL_CACHE:-$HOME/.cache/huggingface}"   # llama.cpp -hf uses the HF hub cache

mkdir -p "$CACHE"
exec docker run --rm --gpus all -p "${PORT}:8080" \
  -v "${CACHE}:/root/.cache/huggingface" \
  ghcr.io/ggml-org/llama.cpp:server-cuda \
  -hf "$MODEL" \
  --host 0.0.0.0 --port 8080 \
  -ngl 99 --n-cpu-moe "$NCPUMOE" \
  -c "$CTX" -fa on --jinja