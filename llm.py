"""Tiny chat client for any OpenAI- or Anthropic-compatible endpoint (MiniMax by default, or a local model).

    LLM_API       openai (default) | anthropic
    LLM_BASE_URL  default https://api.minimax.io/v1  (anthropic: https://api.minimax.io/anthropic/v1)
    LLM_MODEL     default MiniMax-M3
    LLM_API_KEY   or MINIMAX_API_KEY; also read from a .env file next to this script
Local model example: LLM_BASE_URL=http://localhost:11434/v1 LLM_MODEL=qwen3 (Ollama), no key needed.
"""
import json, os, re, threading, time

import requests


def _env():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k and not k.startswith("#"):
                    os.environ.setdefault(k, v.strip().strip("'\""))
    except OSError:
        pass


calls = {"n": 0}  # completions made by this process, for the run summary
_gate = threading.Lock()
_last_call = [0.0]


def _pace():
    """At most one LLM call per LLM_MIN_GAP seconds (default 3) in this process: a shared plan's quota
    is for everyone using it, not just this scraper."""
    with _gate:
        wait = _last_call[0] + float(os.environ.get("LLM_MIN_GAP", "3")) - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        calls["n"] += 1


def chat(prompt, system="", max_tokens=4000, retries=4):
    """One completion; rate limits, server errors and timeouts are retried with backoff (2, 4, 8, 16 s)."""
    for attempt in range(retries + 1):
        _pace()
        try:
            return _chat(prompt, system, max_tokens)
        except (requests.ConnectionError, requests.Timeout) as e:
            err = e
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code not in (408, 429, 500, 502, 503, 504, 529):
                raise
            err = e
        if attempt < retries:
            time.sleep(2 ** (attempt + 1))
    raise err


def _chat(prompt, system, max_tokens):
    _env()
    api = os.environ.get("LLM_API", "openai")
    key = os.environ.get("LLM_API_KEY") or os.environ.get("MINIMAX_API_KEY", "")
    model = os.environ.get("LLM_MODEL", "MiniMax-M3")
    if api == "anthropic":
        base = os.environ.get("LLM_BASE_URL", "https://api.minimax.io/anthropic/v1")
        r = requests.post(f"{base}/messages", timeout=180,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                          json={"model": model, "system": system, "max_tokens": max_tokens,
                                "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json()["content"])
    else:
        base = os.environ.get("LLM_BASE_URL", "https://api.minimax.io/v1")
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        r = requests.post(f"{base}/chat/completions", timeout=180,
                          headers={"Authorization": f"Bearer {key}"} if key else {},
                          json={"model": model, "messages": msgs, "max_tokens": max_tokens})
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # reasoning models think out loud


def chat_json(prompt, system="", attempts=3):
    """chat() that must return one JSON value; tolerates ```json fences and chatter around it.
    Reasoning models can spend the whole output budget thinking on a big prompt and answer nothing, so the
    budget is generous and an empty or unparseable answer is asked again."""
    for attempt in range(attempts):
        text = chat(prompt, system + "\nAnswer with a single JSON value only, no commentary.", max_tokens=16000)
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        if m:
            try:
                return json.loads(m.group())
            except ValueError:
                pass
    raise ValueError(f"LLM returned no JSON: {text[:300]}")
