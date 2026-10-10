import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from store_operator.gateway import Gateway

KEY = "admin-key"


def fake_gateway():
    servers, calls = {}, []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, status, data):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _handle(self):
            calls.append((self.command, self.path))
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                return self._reply(401, {"error": "no"})
            if self.command == "GET" and self.path == "/api/servers":
                return self._reply(200, [{"id": i, "name": s["name"]} for i, s in servers.items()])
            if self.command == "POST" and self.path == "/api/servers":
                sid = len(servers) + 1
                servers[sid] = self._body()
                return self._reply(201, {"id": sid})
            sid = int(self.path.rsplit("/", 1)[1])
            if self.command == "PATCH":
                servers[sid].update(self._body())
                return self._reply(200, {"id": sid})
            if self.command == "DELETE":
                servers.pop(sid)
                return self._reply(200, {"ok": True})

        do_GET = do_POST = do_PATCH = do_DELETE = _handle

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, servers, calls


def test_upsert_and_delete_are_idempotent():
    httpd, servers, calls = fake_gateway()
    gw = Gateway(f"http://127.0.0.1:{httpd.server_port}", KEY)
    try:
        gw.upsert_server("shop1", "http://a/mcp", "t1")
        gw.upsert_server("shop1", "http://b/mcp", "t2")  # retry: updates, no duplicate
        assert len(servers) == 1
        s = servers[1]
        assert (s["name"], s["url"], s["auth"], s["enabled"]) == ("shop1", "http://b/mcp", {"type": "bearer", "token": "t2"}, True)
        assert ("PATCH", "/api/servers/1") in calls

        gw.delete_server("shop1")
        gw.delete_server("shop1")  # already gone: fine
        assert servers == {}

        bad = Gateway(f"http://127.0.0.1:{httpd.server_port}", "wrong")
        try:
            bad.delete_server("x")
            raise AssertionError("expected an error")
        except RuntimeError as e:
            assert "401" in str(e)
    finally:
        httpd.shutdown()
