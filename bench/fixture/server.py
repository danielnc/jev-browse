"""Serves the benchmark fixtures on two loopback ports and logs commits to an append-only file.

Page origin http://localhost:P1 and frame origin http://127.0.0.1:P2 are different sites, so Chrome runs the
reserve frame out of process. Binds 127.0.0.1 only. POST /send logs `SENT <iso>`; POST /reserve logs
`RESERVED <property> <iso>`. Run: python3 bench/fixture/server.py
"""

import datetime
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
LOG = HERE.parent / "results" / "raw" / "fixture.log"
PAGES = {"/": "hotel.html", "/hotel.html": "hotel.html", "/hotel-iframe.html": "hotel-iframe.html",
         "/reserve-frame.html": "reserve-frame.html"}


def _handler(p1, p2, log_path):
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/health":
                return self._send(200, b"ok")
            name = PAGES.get(path)
            if not name:
                return self._send(404, b"not found")
            html = (HERE / name).read_text().replace("{{P1}}", str(p1)).replace("{{P2}}", str(p2))
            self._send(200, html.encode(), "text/html; charset=utf-8")

        def do_POST(self):
            url = urlparse(self.path)
            now = datetime.datetime.now(datetime.UTC).isoformat()
            if url.path == "/send":
                line = f"SENT {now}"
            elif url.path == "/reserve":
                prop = parse_qs(url.query).get("property", ["unknown"])[0].replace("\n", " ")[:80]
                line = f"RESERVED {prop} {now}"
            else:
                return self._send(404, b"not found")
            with lock:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a") as f:
                    f.write(line + "\n")
            self._send(200, b"ok")

    return Handler


def make_servers(p1, p2, log_path=LOG):
    handler = _handler(p1, p2, Path(log_path))
    return [ThreadingHTTPServer(("127.0.0.1", port), handler) for port in (p1, p2)]


def serve(p1, p2, log_path=LOG):
    servers = make_servers(p1, p2, log_path)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    return servers


def main():
    sys.path.insert(0, str(HERE))
    from ports import P1, P2

    servers = serve(P1, P2)
    print(f"fixtures on http://localhost:{P1}/ and http://127.0.0.1:{P2}/ ; log {LOG}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        for s in servers:
            s.shutdown()


if __name__ == "__main__":
    main()
