#!/usr/bin/env bash
# Pull the instruct (non-thinking) Qwen model into the isolated Ollama container.
# The default qwen3:4b is a thinking model: even with think:false its reasoning
# spills into the response field, breaking the translate endpoint.
set -e
echo ">> pulling ${1:-qwen3:4b-instruct} ..."
docker exec lt-ollama ollama pull "${1:-qwen3:4b-instruct}"
echo ">> models in container:"
docker exec lt-ollama ollama list
