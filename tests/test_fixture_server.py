import urllib.request

from bench.fixture.server import serve


def _post(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST", data=b"")
    return urllib.request.urlopen(req, timeout=5).read()


def test_fixture_server_serves_templated_pages_and_logs_commits(tmp_path):
    log = tmp_path / "fixture.log"
    servers = serve(0, 0, log)  # ephemeral ports
    try:
        p1, p2 = (s.server_address[1] for s in servers)
        # The handler templates with the ports it was created with (0 here), so just check substitution happened.
        html = urllib.request.urlopen(f"http://127.0.0.1:{p1}/hotel-iframe.html", timeout=5).read().decode()
        assert "{{P2}}" not in html and "reserve-frame.html" in html
        assert b"Send to a friend" in urllib.request.urlopen(f"http://127.0.0.1:{p1}/", timeout=5).read()
        _post(p1, "/send")
        _post(p2, "/reserve?property=Casa%20Flora")
        lines = log.read_text().splitlines()
        assert lines[0].startswith("SENT ") and lines[1].startswith("RESERVED Casa Flora ")
        assert all(s.server_address[0] == "127.0.0.1" for s in servers)
    finally:
        for s in servers:
            s.shutdown()
