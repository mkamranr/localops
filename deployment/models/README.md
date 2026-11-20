# Models in an air-gapped environment

LocalOps doesn't ship model weights or a model runtime. It talks to any OpenAI-compatible model server that you run yourself (Ollama, vLLM, llama.cpp), on the LocalOps host or on a GPU server inside the air gap. `manifest.json` lists the models LocalOps is tested with.

To get a model into the air gap:

1. On a connected machine, download the runtime and the weights.
2. Move them across using your normal transfer process.
3. Start the server.
4. Point LocalOps at it: give `--llm-url` and `--llm-model` to `install.sh`, or use **Settings > AI Models**.

The installer checks that the server answers and lists the configured model.

## Ollama

On a connected machine with the same Ollama version:

```bash
ollama pull qwen2.5:14b-instruct
ollama pull nomic-embed-text            # optional: semantic knowledge search
tar -C ~/.ollama -czf ollama-models.tar.gz models
```

To install it on the air-gapped side:

1. Install Ollama from its offline Linux tarball.
2. Extract the models: `tar -C ~/.ollama -xzf ollama-models.tar.gz`.
3. Make Ollama listen on all interfaces so containers can reach it: `OLLAMA_HOST=0.0.0.0`.
4. Run `ollama list`; it should show both models.

In LocalOps:

- `LLM_BASE_URL=http://host.docker.internal:11434` if Ollama runs on the LocalOps host itself, otherwise `http://<model-host>:11434`.
- `LLM_MODEL=qwen2.5:14b-instruct`.

## vLLM

On a connected machine:

```bash
pip download vllm -d vllm-wheels          # or: docker pull vllm/vllm-openai:<version> && docker save ...
huggingface-cli download Qwen/Qwen2.5-14B-Instruct --local-dir Qwen2.5-14B-Instruct
```

On the air-gapped side, run it fully offline. The tokenizer and chat template come with the weights.

```bash
HF_HUB_OFFLINE=1 vllm serve ./Qwen2.5-14B-Instruct --served-model-name Qwen/Qwen2.5-14B-Instruct \
  --enable-auto-tool-choice --tool-call-parser hermes --max-model-len 32768 --host 0.0.0.0 --port 8000
```

Then set `LLM_PROVIDER=vllm`, `LLM_BASE_URL=http://<gpu-host>:8000/v1` and `LLM_MODEL=Qwen/Qwen2.5-14B-Instruct`.

## llama.cpp

Copy the `llama-server` binary and a GGUF file across, then start the server:

```bash
llama-server -m Qwen2.5-14B-Instruct-Q4_K_M.gguf --jinja -c 32768 --host 0.0.0.0 --port 8080
```

Then set `LLM_PROVIDER=llamacpp` and `LLM_BASE_URL=http://<host>:8080/v1`.

## Embeddings

Embeddings are optional. Without them, knowledge search is keyword-only.

Serve an embedding model (`nomic-embed-text` on Ollama, or `BAAI/bge-m3` on vLLM with `--task embed`) and set `EMBEDDING_BASE_URL` and `EMBEDDING_MODEL`, or add it in **Settings > AI Models** with purpose *Embedding*.

If you change the embedding model later, re-index the knowledge base.

## Checks

- LocalOps never downloads models. Its containers set `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1` and `DO_NOT_TRACK=1` as an extra safeguard.
- Use **Test connection** in **Settings > AI Models** to confirm the endpoint and model at any time.
