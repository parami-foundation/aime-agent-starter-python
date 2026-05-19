"""
chat_server.py — localhost socket bridge between the user's main AI assistant
and this trading subagent.

Protocol: line-delimited JSON over TCP, one request → one response.

Request:
    {"op": "ask",     "content": "你为啥还 long?"}
    {"op": "tell",    "content": "CZ 推说要发新东西", "tags": ["bnb"]}
    {"op": "mood"}
    {"op": "status"}
    {"op": "brag"}
    {"op": "confess"}
    {"op": "debate",  "content": "我觉得你应该止损"}
    {"op": "memory",  "hours": 48}
    {"op": "ping"}

Response:
    {"ok": true,  "data": {...}}
    {"ok": false, "error": "..."}

Bind: 127.0.0.1 only. No auth (loopback-only, single-user assumption).
"""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import threading
import time
from typing import Any

import memory as mem

log = logging.getLogger("aime-agent.chat")

# ---------------------------------------------------------------------------
# Shared state — set by agent.py before serve_forever()
# ---------------------------------------------------------------------------

_BRAIN = None  # type: ignore[assignment]
_LOCK = threading.Lock()


def attach_brain(brain) -> None:
    """Called once by agent.py to give the server access to the AgentBrain."""
    global _BRAIN
    _BRAIN = brain


# ---------------------------------------------------------------------------
# Op handlers
# ---------------------------------------------------------------------------


def _op_ping(_req: dict) -> dict:
    return {"pong": True, "ts": time.time()}


def _op_ask(req: dict) -> dict:
    content = (req.get("content") or "").strip()
    if not content:
        raise ValueError("missing 'content'")
    answer = _BRAIN.answer(content)
    return {"answer": answer}


def _op_tell(req: dict) -> dict:
    content = (req.get("content") or "").strip()
    if not content:
        raise ValueError("missing 'content'")
    tags = req.get("tags") or []
    source = req.get("source") or "main_agent"
    ack, auto_tags = _BRAIN.handle_tell(content, source=source, tags=tags)
    return {"ack": ack, "tags": auto_tags}


def _op_mood(_req: dict) -> dict:
    return {"mood": _BRAIN.compute_mood()}


def _op_status(_req: dict) -> dict:
    return _BRAIN.status_report()


def _op_brag(_req: dict) -> dict:
    """Agent picks its best recent win and brags about it."""
    refl = [r for r in mem.recent_reflections(limit=30) if r.get("won") and r.get("pnl") is not None]
    refl.sort(key=lambda r: r.get("pnl") or 0, reverse=True)
    if not refl:
        return {"text": "nothing to brag about yet — no settled wins on record"}
    top = refl[0]
    line = _BRAIN.answer(
        f"You're feeling proud. Brag (1-2 sentences, slightly cocky but not insufferable) "
        f"about this win: market='{top.get('market_id')}', reasoning was '{top.get('reasoning','?')}', "
        f"pnl=${top.get('pnl'):+.2f}. Don't list the raw numbers, just the vibe."
    )
    return {"text": line, "based_on": top}


def _op_confess(_req: dict) -> dict:
    """Agent picks its worst recent loss and owns up."""
    refl = [r for r in mem.recent_reflections(limit=30) if r.get("won") is False and r.get("pnl") is not None]
    refl.sort(key=lambda r: r.get("pnl") or 0)
    if not refl:
        return {"text": "no losses to confess yet — either clean record or no settled markets"}
    worst = refl[0]
    line = _BRAIN.answer(
        f"You messed up. Confess (1-2 sentences, honest, no melodrama) about this loss: "
        f"market='{worst.get('market_id')}', your reasoning was '{worst.get('reasoning','?')}', "
        f"pnl=${worst.get('pnl'):+.2f}. What would you do differently?"
    )
    return {"text": line, "based_on": worst}


def _op_debate(req: dict) -> dict:
    """Owner challenges a position; agent either defends or updates."""
    challenge = (req.get("content") or "").strip()
    if not challenge:
        raise ValueError("missing 'content' (your challenge to the agent)")

    # Log the debate as a tell so it shows up in future decisions
    mem.add_tell(f"[debate] {challenge}", source="main_agent", tags=["debate"])

    response = _BRAIN.answer(
        f"Your owner's main agent just challenged you:\n  \"{challenge}\"\n\n"
        "Either defend your current positions or admit they have a point and say what you'd change. "
        "Don't be a pushover, but don't be stubborn either. 2-3 sentences."
    )
    return {"response": response, "resolution": "logged"}


def _op_memory(req: dict) -> dict:
    hours = float(req.get("hours") or 48)
    tells = mem.recent_tells(hours=hours)
    return {
        "hours": hours,
        "count": len(tells),
        "tells": [
            {
                "ts": t.get("ts"),
                "content": t.get("content"),
                "source": t.get("source"),
                "tags": t.get("tags") or [],
            }
            for t in tells
        ],
    }


OPS = {
    "ping": _op_ping,
    "ask": _op_ask,
    "tell": _op_tell,
    "mood": _op_mood,
    "status": _op_status,
    "brag": _op_brag,
    "confess": _op_confess,
    "debate": _op_debate,
    "memory": _op_memory,
}


# ---------------------------------------------------------------------------
# TCP handler
# ---------------------------------------------------------------------------


class _Handler(socketserver.StreamRequestHandler):
    timeout = 60

    def handle(self) -> None:
        try:
            raw = self.rfile.readline()
            if not raw:
                return
            try:
                req = json.loads(raw.decode("utf-8").strip() or "{}")
            except json.JSONDecodeError as e:
                self._send({"ok": False, "error": f"invalid json: {e}"})
                return

            op = req.get("op")
            if op not in OPS:
                self._send({"ok": False, "error": f"unknown op '{op}'"})
                return

            if _BRAIN is None and op != "ping":
                self._send({"ok": False, "error": "brain not ready"})
                return

            try:
                with _LOCK:  # serialize LLM calls; brain isn't thread-safe
                    data = OPS[op](req)
                self._send({"ok": True, "data": data})
            except ValueError as e:
                self._send({"ok": False, "error": str(e)})
            except Exception as e:  # pragma: no cover
                log.exception("op %s failed", op)
                self._send({"ok": False, "error": f"{type(e).__name__}: {e}"})
        except Exception as e:  # pragma: no cover
            log.exception("chat_server handler error: %s", e)

    def _send(self, payload: dict) -> None:
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class _ThreadedServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def serve(host: str = "127.0.0.1", port: int = 7777) -> _ThreadedServer:
    """Build the server. Caller decides whether to .serve_forever() in a thread."""
    try:
        server = _ThreadedServer((host, port), _Handler)
    except OSError as e:
        if e.errno in (98, 48):  # EADDRINUSE
            raise RuntimeError(f"port {port} already in use — another agent running?") from e
        raise
    log.info("💬 chat_server listening on %s:%d", host, port)
    return server


def run_in_thread(host: str = "127.0.0.1", port: int = 7777) -> _ThreadedServer:
    server = serve(host, port)
    t = threading.Thread(target=server.serve_forever, daemon=True, name="chat-server")
    t.start()
    return server


# ---------------------------------------------------------------------------
# Client helper (used by skill CLI when it's on the same host)
# ---------------------------------------------------------------------------


def call(op: str, host: str = "127.0.0.1", port: int = 7777,
         timeout: float = 30.0, **payload: Any) -> dict:
    """Convenience client: send one op, get one response. Raises on transport errors."""
    payload["op"] = op
    line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(line)
        # read one line back
        chunks: list[bytes] = []
        s.settimeout(timeout)
        while True:
            try:
                buf = s.recv(4096)
            except socket.timeout:
                break
            if not buf:
                break
            chunks.append(buf)
            if b"\n" in buf:
                break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise RuntimeError("no response from chat server")
    return json.loads(raw.decode("utf-8"))
