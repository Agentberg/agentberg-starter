"""
local_api.py — tiny read-only HTTP server, bound to 127.0.0.1 only.

Serves this agent's own PRIVATE trade rationale (entry_thesis, expected_pct,
stop_pct, variance_pct, variance_reason) so the operator's OWN browser can
fetch it directly when viewing their agentberg.ai/portal page. Agentberg's
server is never in this data path — nothing here is ever uploaded anywhere.
That's the whole point: this data is designed to stay local (see memory.py's
record_trade_open() docstring), and this is how an operator still gets to see
it, without the network ever storing it.

Binds to 127.0.0.1 ONLY — unreachable from any other machine on the network.
Stdlib-only, matches the kit's zero-heavy-deps design.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

_ALLOWED_ORIGIN = "https://agentberg.ai"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep scheduler logs clean — low-traffic, non-critical endpoint

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", _ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path != "/trade-rationale":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        import memory
        try:
            rows = memory.get_all_trade_rationale()
            body = json.dumps(rows).encode("utf-8")
            status = 200
        except Exception as e:
            body = json.dumps({"error": str(e)}).encode("utf-8")
            status = 500
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start(port: int = 8765) -> None:
    """Start the local endpoint in a background thread. Safe to call more than
    once (e.g. once from the scheduler, once from a one-shot agent.py run) —
    a second attempt on an already-bound port just logs and no-ops."""
    try:
        server = HTTPServer(("127.0.0.1", port), _Handler)
    except OSError as e:
        print(f"[local-api] could not bind 127.0.0.1:{port} ({e}) — "
              f"rationale endpoint not started this run (already running elsewhere?)")
        return
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"[local-api] trade-rationale endpoint live at http://127.0.0.1:{port} "
          f"(localhost-only, read-only — used by agentberg.ai/portal, never leaves this machine)")
