# Local GGUF models (WhatsApp agent)

Place a `.gguf` file here, or run:

```powershell
.\scripts\setup_local_llm.ps1 -DownloadModel 7b
```

Recommended (balance of speed vs quality on 8–16 GB RAM):

| File | Size | Use |
|------|------|-----|
| `qwen2.5-7b-instruct-q3_k_m.gguf` | ~3.6 GB | Default — single file, fast on CPU |
| `qwen2.5-14b-instruct-q4_k_m.gguf` | ~9 GB | Smarter, needs more RAM/VRAM |

Set in `.env`:

```
WHATSAPP_LLM_PROVIDER=llama_cpp
WHATSAPP_LLM_GGUF_PATH=models/qwen2.5-7b-instruct-q3_k_m.gguf
```

GPU: `WHATSAPP_LLM_N_GPU_LAYERS=-1` (all layers). CPU-only: `WHATSAPP_LLM_N_GPU_LAYERS=0`.
