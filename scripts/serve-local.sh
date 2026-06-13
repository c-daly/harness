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
# Tuned for a 12 GB Blackwell card (RTX 5070) + 32 GB RAM with Qwen3-Coder-30B-A3B
# (MoE, 3B active). ~16 GB at IQ4_XS: expert tensors spill to RAM while attention
# stays on the GPU — interactive because only 3B params fire per token.
# First launch downloads the GGUF (~16 GB) into the mounted cache volume.
set -euo pipefail

MODEL="${HARNESS_LOCAL_MODEL:-unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:IQ4_XS}"
PORT="${HARNESS_LOCAL_PORT:-8080}"
CTX="${HARNESS_LOCAL_CTX:-16384}"
# Expert layers parked on the CPU. Lower = more GPU residency/speed; raise if it
# OOMs on load. Confirm --n-cpu-moe/--jinja on the image: `... server-cuda --help`.
NCPUMOE="${HARNESS_LOCAL_NCPUMOE:-24}"
CACHE="${HARNESS_LOCAL_CACHE:-$HOME/.cache/huggingface}"   # llama.cpp -hf uses the HF hub cache

mkdir -p "$CACHE"
exec docker run --rm --gpus all -p "${PORT}:8080" \
  -v "${CACHE}:/root/.cache/huggingface" \
  ghcr.io/ggml-org/llama.cpp:server-cuda \
  -hf "$MODEL" \
  --host 0.0.0.0 --port 8080 \
  -ngl 99 --n-cpu-moe "$NCPUMOE" \
  -c "$CTX" -fa on --jinja
