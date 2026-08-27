# llm.py — local model backends. No external services: chat goes to an
# OpenAI-compatible server on localhost (LM Studio / Ollama /v1 / vLLM),
# embeddings stay on Ollama+nomic so existing Qdrant vectors remain valid.

import json
import re

import httpx
import ollama

import config

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_OPEN, _CLOSE = "<think>", "</think>"

_effort = config.REASONING_EFFORT
_embed_client = ollama.Client(host=config.OLLAMA_HOST)
_http = httpx.Client(timeout=config.REASON_TIMEOUT)


# ── Session reasoning effort ──────────────────────────────────────────────────

def set_reasoning_effort(level: str) -> str:
    """Set thinking depth for the session (low | medium | xhigh)."""
    global _effort
    level = (level or "").strip().lower()
    if level not in config.REASONING_EFFORTS:
        raise ValueError(f"reasoning_effort must be one of {config.REASONING_EFFORTS}")
    _effort = level
    return _effort


def get_reasoning_effort() -> str:
    return _effort


# ── Chat ──────────────────────────────────────────────────────────────────────

def _body(messages, temperature, max_tokens, effort, stream):
    b = {
        "model": config.REASON_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": stream,
    }
    if max_tokens:
        # Thinking tokens are billed against max_tokens; without headroom a small
        # cap is spent entirely on reasoning and `content` comes back empty.
        b["max_tokens"] = max_tokens + config.REASONING_HEADROOM
    lvl = effort or _effort
    if lvl:
        b["reasoning_effort"] = lvl
    return b


def _url() -> str:
    return f"{config.REASON_BASE_URL.rstrip('/')}/chat/completions"


def strip_think(text: str) -> str:
    return _THINK_RE.sub("", text or "").strip()


def reason(messages, temperature: float = 0.1, max_tokens: int | None = None,
           effort: str | None = None) -> str:
    """Non-streaming completion; reasoning traces removed."""
    b = _body(messages, temperature, max_tokens, effort, False)
    r = _http.post(_url(), json=b)
    if r.status_code == 400 and "reasoning_effort" in b:
        b.pop("reasoning_effort")          # server doesn't accept the param
        r = _http.post(_url(), json=b)
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    return strip_think(msg.get("content", ""))


class _ThinkFilter:
    """Suppresses <think>…</think> spans across streamed chunks."""

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, tok: str) -> str:
        self.buf += tok
        out = ""
        while True:
            if self.inside:
                i = self.buf.find(_CLOSE)
                if i < 0:
                    self.buf = self.buf[-len(_CLOSE):]
                    return out
                self.buf = self.buf[i + len(_CLOSE):]
                self.inside = False
            else:
                i = self.buf.find(_OPEN)
                if i < 0:
                    keep = len(_OPEN) - 1
                    if len(self.buf) > keep:
                        out += self.buf[:-keep]
                        self.buf = self.buf[-keep:]
                    return out
                out += self.buf[:i]
                self.buf = self.buf[i + len(_OPEN):]
                self.inside = True

    def flush(self) -> str:
        out = "" if self.inside else self.buf
        self.buf = ""
        return out


def reason_stream(messages, temperature: float = 0.5, effort: str | None = None):
    """Yields visible tokens only (reasoning traces suppressed)."""
    b = _body(messages, temperature, None, effort, True)
    filt = _ThinkFilter()

    def _run(body):
        with _http.stream("POST", _url(), json=body) as resp:
            if resp.status_code == 400:
                resp.read()
                raise _BadParam()
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0].get("delta", {})
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                tok = delta.get("content")
                if tok:
                    vis = filt.feed(tok)
                    if vis:
                        yield vis
        tail = filt.flush()
        if tail:
            yield tail

    try:
        yield from _run(b)
    except _BadParam:
        b.pop("reasoning_effort", None)
        yield from _run(b)


class _BadParam(Exception):
    pass


# ── Embeddings (unchanged backend) ────────────────────────────────────────────

def embed(text: str) -> list[float]:
    return _embed_client.embeddings(model=config.EMBED_MODEL, prompt=text)["embedding"]


def health() -> tuple[bool, str]:
    """Check the chat server is reachable. Returns (ok, detail)."""
    try:
        r = _http.get(f"{config.REASON_BASE_URL.rstrip('/')}/models", timeout=5)
        r.raise_for_status()
        names = [m.get("id", "?") for m in r.json().get("data", [])]
        return True, ", ".join(names) or "no models listed"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"
