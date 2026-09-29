# webui.py — local web UI for the governance assistant.
# Stdlib only (no third-party web deps); binds to 127.0.0.1. Answers stream to the
# browser over Server-Sent Events. Swap the transport for FastAPI/uvicorn when the
# app moves to a multi-user server — the page and Session logic carry over.

import json
import queue
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import chat
import config
import llm
from session import Session

HOST = "127.0.0.1"
PORT = int(__import__("os").getenv("WEBUI_PORT", "8080"))
_STATIC = Path(__file__).parent / "static"

_session = Session()
_lock = threading.Lock()   # one generation at a time; the model can't batch anyway


def _run(question: str, q: "queue.Queue"):
    """Runs one turn, pushing SSE events onto the queue."""
    try:
        with _lock:
            answer, topic, route, sql, intent = chat.ask(
                question,
                _session.history,
                topic_table=_session.topic_table,
                last_sql=_session.last_sql,
                last_intent=_session.last_intent,
                last_route=_session.last_route,
                clearance=config.APP_CLEARANCE,
                on_token=lambda t: q.put({"type": "token", "text": t}),
            )
            _session.topic_table, _session.last_route = topic, route
            _session.last_sql, _session.last_intent = sql, intent
        q.put({"type": "done", "answer": answer, "route": route,
               "sql": sql if route == "sql" else None})
    except Exception as e:
        traceback.print_exc()
        q.put({"type": "error", "message": f"{type(e).__name__}: {e}"})
    finally:
        q.put(None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ── helpers ──────────────────────────────────────────────────────────────
    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    # ── routes ───────────────────────────────────────────────────────────────
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            page = (_STATIC / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")
        if self.path == "/state":
            ok, detail = llm.health()
            return self._json({
                "model": config.REASON_MODEL,
                "effort": llm.get_reasoning_effort(),
                "efforts": list(config.REASONING_EFFORTS),
                "online": ok,
                "detail": detail,
            })
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/effort":
            try:
                return self._json({"effort": llm.set_reasoning_effort(self._body().get("effort", ""))})
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        if self.path == "/reset":
            with _lock:
                _session.reset()
            return self._json({"ok": True})

        if self.path == "/ask":
            question = (self._body().get("question") or "").strip()
            if not question:
                return self._json({"error": "empty question"}, 400)
            return self._stream(question)

        return self._json({"error": "not found"}, 404)

    def _stream(self, question: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        q: queue.Queue = queue.Queue()
        threading.Thread(target=_run, args=(question, q), daemon=True).start()
        while True:
            try:
                ev = q.get(timeout=180)
            except queue.Empty:
                ev = {"type": "ping"}          # keep the connection alive on long turns
            if ev is None:
                break
            try:
                self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break
        self.close_connection = True


def main():
    ok, detail = llm.health()
    print(f"  model   : {config.REASON_MODEL} ({'online' if ok else 'OFFLINE — ' + detail})")
    print(f"  effort  : {llm.get_reasoning_effort()}")
    print(f"  serving : http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
