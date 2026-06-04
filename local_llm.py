"""
Local LLM client for WhatsApp agent (no Ollama).

Providers (WHATSAPP_LLM_PROVIDER):
  auto       — llama_cpp if GGUF exists, else openai_compat, else hf_local
  llama_cpp  — in-process GGUF via llama-cpp-python (chat + policy)
  hf_local   — transformers model (chat + policy; mode auto-detected per request)
  openai_compat — LM Studio, llama.cpp --server, LocalAI, text-gen-webui, etc.
  cloud      — remote OpenAI-compatible API (needs WHATSAPP_LLM_API_KEY)

WHATSAPP_LLM_HF_MODE=auto|chat|policy — force HF path (default auto).
"""

from __future__ import annotations

import logging
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Literal

LlmMode = Literal["auto", "chat", "policy"]
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DEFAULT_LOCAL_BASE = "http://127.0.0.1:1234/v1"  # LM Studio default
DEFAULT_LOCAL_MODEL = "local-model"
MAX_CONTEXT_CHARS = int(os.environ.get("WHATSAPP_LLM_MAX_CONTEXT_CHARS", "14000"))
MAX_CHAT_CONTEXT_CHARS = int(os.environ.get("WHATSAPP_LLM_CHAT_CONTEXT_CHARS", "7500"))
MAX_POLICY_CONTEXT_CHARS = int(os.environ.get("WHATSAPP_LLM_POLICY_CONTEXT_CHARS", "3200"))
RETRY_ATTEMPTS = 3
RETRY_BASE_SEC = 1.0

_LlamaHandle: Any = None
_LLAMA_BROKEN_UNTIL = 0.0
_HF_TOKENIZER: Any = None
_HF_MODEL: Any = None


def _llama_cpp_available() -> bool:
    try:
        import llama_cpp  # noqa: F401

        return True
    except ImportError:
        return False


def _hf_local_available() -> bool:
    try:
        import transformers  # noqa: F401
        import torch  # noqa: F401

        return True
    except ImportError:
        return False


def _gguf_quality_score(path: Path) -> int:
    """Higher = smarter local model (used when multiple GGUF files exist)."""
    name = path.name.lower()
    score = 0
    for token, pts in (
        ("14b", 140),
        ("13b", 130),
        ("8b", 95),
        ("7b", 90),
        ("3b", 72),
        ("1.5b", 55),
        ("0.5b", 20),
    ):
        if token in name.replace("-", ""):
            score += pts
            break
    if "instruct" in name or "chat" in name:
        score += 12
    if "qwen" in name or "llama" in name or "mistral" in name:
        score += 8
    for q, pts in (("q6_k", 18), ("q5_k", 14), ("q4_k", 10), ("q3_k", 6), ("q2_k", 2)):
        if q in name:
            score += pts
            break
    return score


def _gguf_speed_score(path: Path) -> int:
    """Higher = faster inference (smaller quant / smaller param count)."""
    name = path.name.lower()
    score = 100
    for token, pts in (("0.5b", 95), ("1.5b", 88), ("3b", 82), ("7b", 55), ("8b", 50), ("14b", 30)):
        if token in name.replace("-", ""):
            score = pts
            break
    for q, pts in (("q2_k", 20), ("q3_k", 14), ("q4_k", 10), ("q5_k", 6), ("q6_k", 2)):
        if q in name:
            score += pts
            break
    return score


def _gguf_fast_mode() -> bool:
    return (os.environ.get("WHATSAPP_LLM_FAST") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _skip_llama() -> bool:
    return (os.environ.get("WHATSAPP_LLM_SKIP_LLAMA") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def gguf_path() -> Path | None:
    raw = (os.environ.get("WHATSAPP_LLM_GGUF_PATH") or "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = ROOT / p
        if p.is_file():
            return p
    models_dir = ROOT / "models"
    if models_dir.is_dir():
        candidates = list(models_dir.glob("*.gguf"))
        if candidates:
            if _gguf_fast_mode():
                return max(
                    candidates,
                    key=lambda x: (_gguf_speed_score(x), _gguf_quality_score(x), x.stat().st_mtime),
                )
            return max(
                candidates,
                key=lambda x: (_gguf_quality_score(x), x.stat().st_mtime),
            )
    return None


def _base_url() -> str:
    return (
        os.environ.get("WHATSAPP_LLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or DEFAULT_LOCAL_BASE
    ).rstrip("/")


def _is_local_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def resolve_provider() -> str:
    explicit = (os.environ.get("WHATSAPP_LLM_PROVIDER") or "auto").strip().lower()
    if explicit == "hf_local":
        return "hf_local" if _hf_local_available() else "none"
    def _llama_usable() -> bool:
        return (
            not _skip_llama()
            and _llama_cpp_available()
            and _LLAMA_BROKEN_UNTIL <= time.time()
            and gguf_path() is not None
        )

    if explicit == "llama_cpp":
        if _llama_usable():
            return "llama_cpp"
        if openai_compat_healthy():
            return "openai_compat"
        if _hf_local_available():
            return "hf_local"
        return "none"
    if explicit != "auto":
        return explicit

    # LM Studio / llama.cpp server: model stays loaded — fast + typically larger than HF 0.5B.
    if openai_compat_healthy():
        return "openai_compat"
    if _llama_usable():
        return "llama_cpp"
    if _hf_local_available():
        return "hf_local"
    key = (os.environ.get("WHATSAPP_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if key and not _is_local_url(_base_url()):
        return "cloud"
    return "none"


def _auth_headers() -> dict[str, str]:
    key = (
        os.environ.get("WHATSAPP_LLM_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or "local"
    ).strip()
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def openai_compat_healthy(base: str | None = None) -> bool:
    base = (base or _base_url()).rstrip("/")
    try:
        r = requests.get(f"{base}/models", headers=_auth_headers(), timeout=4)
        return r.status_code == 200
    except Exception:
        return False


def _score_openai_model_id(model_id: str) -> int:
    mid = model_id.lower()
    score = 0
    if any(x in mid for x in ("embed", "vision", "rerank", "nomic")):
        return -100
    for token, pts in (
        ("72b", 50),
        ("32b", 88),
        ("14b", 92),
        ("13b", 90),
        ("8b", 86),
        ("7b", 84),
        ("3b", 76),
        ("1.5b", 58),
        ("0.5b", 25),
    ):
        if token in mid.replace("-", ""):
            score += pts
            break
    if "instruct" in mid or "chat" in mid:
        score += 14
    if "qwen" in mid:
        score += 12
    if "llama" in mid or "mistral" in mid or "gemma" in mid:
        score += 8
    if "gpt-4" in mid:
        score += 95
    if "gpt-3.5" in mid:
        score += 70
    return score


def discover_openai_model(base: str | None = None) -> str | None:
    """Pick the strongest chat model exposed by the local OpenAI-compatible server."""
    base = (base or _base_url()).rstrip("/")
    configured = (os.environ.get("WHATSAPP_LLM_MODEL") or "").strip()
    if configured:
        return configured
    try:
        r = requests.get(f"{base}/models", headers=_auth_headers(), timeout=6)
        r.raise_for_status()
        data = r.json().get("data") or []
        ids = [str(row.get("id") or "") for row in data if row.get("id")]
        if ids:
            return max(ids, key=_score_openai_model_id)
    except Exception as exc:
        log.debug("model discovery failed: %s", exc)
    return DEFAULT_LOCAL_MODEL


def _trim_messages(
    messages: list[dict[str, str]],
    *,
    max_chars: int | None = None,
) -> list[dict[str, str]]:
    cap = max_chars if max_chars is not None else MAX_CONTEXT_CHARS
    total = sum(len(m.get("content", "")) for m in messages)
    if total <= cap:
        return messages
    kept = [messages[0]]
    if len(messages) > 1 and messages[1].get("role") == "system":
        snap = messages[1]["content"]
        if len(snap) > 6000:
            kept.append({"role": "system", "content": snap[:6000] + "\n...(trimmed)"})
        else:
            kept.append(messages[1])
    tail = messages[2:] if len(messages) > 2 and messages[1].get("role") == "system" else messages[1:]
    budget = cap - sum(len(m.get("content", "")) for m in kept)
    rev: list[dict[str, str]] = []
    for m in reversed(tail):
        c = m.get("content", "")
        if len(c) <= budget:
            rev.insert(0, m)
            budget -= len(c)
        elif m.get("role") == "user" and budget > 200:
            rev.insert(0, {"role": "user", "content": c[-budget:]})
            break
    return kept + rev


def _get_llama():
    global _LLAMA_BROKEN_UNTIL
    if _LLAMA_BROKEN_UNTIL > time.time():
        raise RuntimeError("llama_cpp temporarily disabled after load failure")
    global _LlamaHandle
    if _LlamaHandle is not None:
        return _LlamaHandle
    path = gguf_path()
    if path is None:
        raise FileNotFoundError(
            "No GGUF model. Set WHATSAPP_LLM_GGUF_PATH or place a .gguf in ./models/"
        )
    from llama_cpp import Llama

    n_ctx = int(os.environ.get("WHATSAPP_LLM_N_CTX", "8192"))
    n_gpu = int(os.environ.get("WHATSAPP_LLM_N_GPU_LAYERS", "-1"))
    n_threads = int(os.environ.get("WHATSAPP_LLM_N_THREADS", "0")) or None
    n_batch = int(os.environ.get("WHATSAPP_LLM_N_BATCH", "512"))
    log.info("Loading local GGUF: %s (n_ctx=%s n_gpu_layers=%s)", path.name, n_ctx, n_gpu)
    try:
        kwargs: dict[str, Any] = {
            "model_path": str(path),
            "n_ctx": n_ctx,
            "n_gpu_layers": n_gpu,
            "n_batch": n_batch,
            "use_mmap": True,
            "use_mlock": False,
            "verbose": False,
        }
        if n_threads:
            kwargs["n_threads"] = n_threads
        _LlamaHandle = Llama(**kwargs)
        return _LlamaHandle
    except Exception as exc:
        # CPU wheel mismatch (WinError 0xc000001d) — fall back to HF / LM Studio for session.
        _LLAMA_BROKEN_UNTIL = time.time() + 86400
        log.error("llama_cpp load failed (will use fallback 24h): %s", exc)
        raise


def _chat_llama_cpp(
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    *,
    mode: LlmMode = "auto",
) -> str:
    llm = _get_llama()
    cap = max_tokens
    if mode == "policy":
        cap = min(cap, int(os.environ.get("WHATSAPP_LLM_POLICY_MAX_TOKENS", "128")))
    out = llm.create_chat_completion(
        messages=messages,
        temperature=temperature,
        max_tokens=cap,
        top_p=float(os.environ.get("WHATSAPP_LLM_TOP_P", "0.9")),
        repeat_penalty=float(os.environ.get("WHATSAPP_LLM_REPEAT_PENALTY", "1.12")),
    )
    return str(out["choices"][0]["message"]["content"]).strip()


def _chat_openai_compat(
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    *,
    base: str | None = None,
    timeout_sec: int | None = None,
) -> str:
    base = (base or _base_url()).rstrip("/")
    model = discover_openai_model(base) or DEFAULT_LOCAL_MODEL
    url = f"{base}/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    default_timeout = int(os.environ.get("WHATSAPP_LLM_OPENAI_TIMEOUT_SEC", "45"))
    last_err: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            r = requests.post(
                url,
                headers=_auth_headers(),
                json=payload,
                timeout=timeout_sec if timeout_sec is not None else default_timeout,
            )
            r.raise_for_status()
            data = r.json()
            return str(data["choices"][0]["message"]["content"]).strip()
        except Exception as exc:
            last_err = exc
            if attempt + 1 < RETRY_ATTEMPTS:
                time.sleep(RETRY_BASE_SEC * (2**attempt))
    raise last_err or RuntimeError("openai_compat chat failed")


def _get_hf_local():
    global _HF_MODEL, _HF_TOKENIZER
    if _HF_MODEL is not None and _HF_TOKENIZER is not None:
        return _HF_TOKENIZER, _HF_MODEL
    from transformers import AutoModelForCausalLM, AutoTokenizer

    model_id = (
        os.environ.get("WHATSAPP_LLM_HF_MODEL") or "Qwen/Qwen2.5-1.5B-Instruct"
    ).strip()
    log.info("Loading HF local model: %s", model_id)
    local_only = (os.environ.get("WHATSAPP_LLM_HF_LOCAL_ONLY") or "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.float16 if device == "cuda" else torch.float32
    _HF_TOKENIZER = AutoTokenizer.from_pretrained(model_id, local_files_only=local_only)
    load_kw: dict[str, Any] = {
        "local_files_only": local_only,
        "torch_dtype": dtype,
    }
    if device == "cuda":
        load_kw["device_map"] = "auto"
    _HF_MODEL = AutoModelForCausalLM.from_pretrained(model_id, **load_kw)
    if device == "cpu":
        _HF_MODEL = _HF_MODEL.to(device)
    _HF_MODEL.eval()
    return _HF_TOKENIZER, _HF_MODEL


def _infer_hf_mode(messages: list[dict[str, str]]) -> LlmMode:
    """Policy JSON for trading brain; natural language for dashboard/whatsapp chat."""
    forced = (os.environ.get("WHATSAPP_LLM_HF_MODE") or "auto").strip().lower()
    if forced in ("chat", "policy"):
        return forced  # type: ignore[return-value]
    for m in messages:
        if m.get("role") != "system":
            continue
        body = (m.get("content") or "").lower()
        if "trading policy" in body or "strict json only" in body:
            return "policy"
        if "copilot" in body or "conversational" in body or "do not place trades" in body:
            return "chat"
    for m in messages:
        if m.get("role") != "user":
            continue
        raw = (m.get("content") or "").strip()
        if raw.startswith("{") and '"baseline"' in raw:
            return "policy"
        if raw.startswith("{") and '"symbol"' in raw and '"close"' in raw:
            return "policy"
        return "chat"
    return "chat"


def _chat_hf_local_chat(messages: list[dict[str, str]], max_tokens: int, temperature: float) -> str:
    """Natural-language chat via HF instruct model (dashboard / whatsapp)."""
    import torch

    tokenizer, model = _get_hf_local()
    chat_msgs = [{"role": m.get("role", "user"), "content": m.get("content", "")} for m in messages]
    prompt = ""
    if hasattr(tokenizer, "apply_chat_template"):
        try:
            prompt = tokenizer.apply_chat_template(
                chat_msgs,
                tokenize=False,
                add_generation_prompt=True,
            )
        except Exception:
            prompt = ""
    if not prompt:
        for m in chat_msgs:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "system":
                prompt += f"<|im_start|>system\n{content}\n"
            elif role == "assistant":
                prompt += f"<|im_start|>assistant\n{content}\n"
            else:
                prompt += f"<|im_start|>user\n{content}\n"
        prompt += "<|im_start|>assistant\n"

    inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=8192)
    if hasattr(model, "device"):
        inputs = {k: v.to(model.device) for k, v in inputs.items()}
    cap = max(64, min(int(max_tokens), 1200))
    with torch.inference_mode():
        out = model.generate(
            **inputs,
            do_sample=True,
            temperature=max(0.1, float(temperature)),
            max_new_tokens=cap,
            pad_token_id=tokenizer.eos_token_id,
            top_p=float(os.environ.get("WHATSAPP_LLM_TOP_P", "0.9")),
        )
    text = tokenizer.decode(
        out[0][inputs["input_ids"].shape[1] :], skip_special_tokens=True
    ).strip()
    for stop in ("<|im_start|>", "\nuser:", "\nUSER:"):
        if stop in text:
            text = text.split(stop)[0].strip()
    return text or "(no reply — try a shorter question)"


def _chat_hf_local_policy(messages: list[dict[str, str]], max_tokens: int, temperature: float) -> str:
    import torch

    tokenizer, model = _get_hf_local()
    prompt = ""
    baseline_signal = "long"
    baseline_conf = 0.62
    baseline_score = 68
    try:
        for m in reversed(messages):
            if m.get("role") != "user":
                continue
            raw = m.get("content", "")
            blob = json.loads(raw) if raw.strip().startswith("{") else None
            if isinstance(blob, dict):
                b = blob.get("baseline") or {}
                sig = str(b.get("signal", "")).strip().lower()
                if sig in ("long", "short"):
                    baseline_signal = sig
                baseline_conf = float(b.get("confidence", baseline_conf) or baseline_conf)
                baseline_score = float(b.get("score", baseline_score) or baseline_score)
                break
    except Exception:
        pass
    for m in messages:
        role = m.get("role", "user").upper()
        content = m.get("content", "")
        prompt += f"{role}: {content}\n"
    prompt += (
        "ASSISTANT: Think briefly, then return only one JSON object with keys "
        'signal, confidence, score, stop_pct, take_pct, reason.\n'
    )
    inputs = tokenizer(prompt, return_tensors="pt")
    with torch.no_grad():
        out = model.generate(
            **inputs,
            do_sample=True,
            temperature=max(0.05, float(temperature)),
            max_new_tokens=max(32, min(int(max_tokens), 256)),
            pad_token_id=tokenizer.eos_token_id,
        )
    text = tokenizer.decode(out[0][inputs["input_ids"].shape[1] :], skip_special_tokens=True).strip()
    if not text:
        return '{"signal":"flat","confidence":0.0,"score":0,"stop_pct":0.01,"take_pct":0.01,"reason":"empty"}'
    # Enforce structured JSON so trading policy can consume local HF outputs safely.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return text
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            chunk = m.group(0)
            try:
                parsed = json.loads(chunk)
                if isinstance(parsed, dict):
                    return chunk
            except Exception:
                pass
    # Second pass: force conversion to strict JSON if first generation is chatty.
    extract_prompt = (
        "SYSTEM: Convert the following analysis into strict JSON only with keys "
        "signal(long|short|flat), confidence(0..1), score(0..100), stop_pct, take_pct, reason.\n"
        f"TEXT:\n{text}\nJSON:"
    )
    try:
        ex_inputs = tokenizer(extract_prompt, return_tensors="pt")
        with torch.no_grad():
            ex_out = model.generate(
                **ex_inputs,
                do_sample=False,
                max_new_tokens=120,
                pad_token_id=tokenizer.eos_token_id,
            )
        ex_text = tokenizer.decode(
            ex_out[0][ex_inputs["input_ids"].shape[1] :], skip_special_tokens=True
        ).strip()
        try:
            parsed = json.loads(ex_text)
            if isinstance(parsed, dict):
                return ex_text
        except Exception:
            m = re.search(r"\{.*\}", ex_text, re.DOTALL)
            if m:
                chunk = m.group(0)
                parsed = json.loads(chunk)
                if isinstance(parsed, dict):
                    return chunk
    except Exception:
        pass

    low = text.lower()
    signal = baseline_signal
    if "short" in low and "long" not in low:
        signal = "short"
    elif "long" in low and "short" not in low:
        signal = "long"
    conf = max(0.55, min(0.92, baseline_conf if baseline_conf > 0 else 0.62))
    score = max(60, min(100, int(baseline_score if baseline_score > 0 else 68)))
    return json.dumps(
        {
            "signal": signal,
            "confidence": conf,
            "score": score,
            "stop_pct": 0.01,
            "take_pct": 0.015,
            "reason": "hf_local_normalized",
        }
    )


def _chat_hf_local(
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    *,
    mode: LlmMode = "auto",
) -> str:
    resolved = _infer_hf_mode(messages) if mode == "auto" else mode
    if resolved == "policy":
        return _chat_hf_local_policy(messages, max_tokens, temperature)
    return _chat_hf_local_chat(messages, max_tokens, temperature)


def chat_completion(
    messages: list[dict[str, str]],
    *,
    max_tokens: int = 700,
    temperature: float = 0.35,
    timeout_sec: int | None = None,
    mode: LlmMode = "auto",
) -> tuple[str | None, str | None]:
    """
    Returns (reply_text, error_message). On success error is None.
    """
    provider = resolve_provider()
    if provider == "none":
        return None, "no_local_llm"

    if mode == "chat":
        trim_cap = MAX_CHAT_CONTEXT_CHARS
    elif mode == "policy":
        trim_cap = MAX_POLICY_CONTEXT_CHARS
    else:
        trim_cap = MAX_CONTEXT_CHARS
    trimmed = _trim_messages(messages, max_chars=trim_cap)
    policy_timeout = int(os.environ.get("WHATSAPP_LLM_POLICY_TIMEOUT_SEC", "12"))
    chat_timeout = int(os.environ.get("WHATSAPP_LLM_TIMEOUT_SEC", "60"))
    req_timeout = policy_timeout if mode == "policy" else chat_timeout

    try:
        if provider == "llama_cpp":
            text = _chat_llama_cpp(trimmed, max_tokens, temperature, mode=mode)
            return text, None
        if provider in ("openai_compat", "cloud"):
            text = _chat_openai_compat(
                trimmed,
                max_tokens,
                temperature,
                timeout_sec=timeout_sec if timeout_sec is not None else req_timeout,
            )
            return text, None
        if provider == "hf_local":
            text = _chat_hf_local(trimmed, max_tokens, temperature, mode=mode)
            return text, None
        return None, f"unknown_provider:{provider}"
    except Exception as exc:
        log.exception("local_llm chat failed provider=%s", provider)
        return None, str(exc)


def warmup_provider() -> str:
    """Load HF/llama backend once at bot startup so first scan is not multi-minute."""
    global _LLAMA_BROKEN_UNTIL
    provider = resolve_provider()
    if provider == "hf_local":
        _get_hf_local()
    elif provider == "llama_cpp":
        try:
            _get_llama()
        except Exception:
            _LLAMA_BROKEN_UNTIL = time.time() + 86400
            log.warning("llama_cpp warmup failed; falling back to hf_local for this session")
            if _hf_local_available():
                _get_hf_local()
    return status_line()


def status_line() -> str:
    provider = resolve_provider()
    if provider == "llama_cpp":
        if not _llama_cpp_available():
            return "llama_cpp:pip install llama-cpp-python"
        p = gguf_path()
        return f"llama_cpp:{p.name if p else 'missing GGUF in models/'}"
    if provider == "openai_compat":
        base = _base_url()
        model = discover_openai_model(base) or "?"
        return f"openai_compat:{base} model={model}"
    if provider == "hf_local":
        model_id = (os.environ.get("WHATSAPP_LLM_HF_MODEL") or "Qwen/Qwen2.5-1.5B-Instruct").strip()
        hf_mode = (os.environ.get("WHATSAPP_LLM_HF_MODE") or "auto").strip().lower()
        return f"hf_local:{model_id} (chat+policy, mode={hf_mode})"
    if provider == "cloud":
        return f"cloud:{_base_url()}"
    if _LLAMA_BROKEN_UNTIL > time.time():
        remain = int(_LLAMA_BROKEN_UNTIL - time.time())
        return f"none (llama_cpp cooldown {remain}s; set LM Studio or llama.cpp server)"
    return "none (set GGUF path, LM Studio, or llama.cpp server)"
