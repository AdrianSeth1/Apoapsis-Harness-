#!/usr/bin/env bash
# The locked Slice 2C argv, with ONE deliberate deviation for this smoke:
#   -n 16384  ->  -n 4096
# Enough reasoning headroom for a one-function task, well short of a Crisis
# Atlas budget. Every other flag is byte-identical to the locked configuration,
# so the topology under test is the pinned one.
exec /home/arya/llama.cpp/build/bin/llama-server \
  -m /home/arya/models/qwen3.6-27b/Qwen3.6-27B-Q4_K_M.gguf \
  --alias qwen3.6-27b \
  --parallel 1 \
  --ctx-size 65536 \
  -n 4096 \
  --flash-attn on \
  --cache-type-k q8_0 \
  --cache-type-v q8_0 \
  --n-gpu-layers 999 \
  --jinja \
  --threads 16 \
  --host 0.0.0.0 \
  --port 8080
