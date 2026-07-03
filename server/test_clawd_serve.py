import time
import threading
import json
import urllib.request
from http.server import ThreadingHTTPServer

import clawd_serve


def _run_all():
    """Runner mínimo (sem pytest): roda toda função test_*."""
    import sys
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    fails = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:
            fails += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - fails}/{len(fns)} passaram")
    sys.exit(1 if fails else 0)


def test_cache_polls_once_within_ttl():
    calls = {"n": 0}
    def fake_poller():
        calls["n"] += 1
        return {"s": 42, "sr": 120, "w": 18, "wr": 999, "st": "allowed"}
    t = {"now": 1000.0}
    c = clawd_serve.Cache(fake_poller, ttl=60, clock=lambda: t["now"])
    p1 = c.payload()
    assert p1["s"] == 42 and p1["w"] == 18 and p1["st"] == "allowed"
    assert p1["age"] == 0
    t["now"] = 1030.0            # 30s depois: ainda dentro do TTL
    p2 = c.payload()
    assert calls["n"] == 1        # não pingou de novo
    assert p2["age"] == 30
    t["now"] = 1100.0            # 100s depois: passou do TTL
    c.payload()
    assert calls["n"] == 2        # pingou de novo


def test_cache_serves_stale_on_poll_failure():
    seq = [{"s": 50, "sr": 1, "w": 10, "wr": 1, "st": "allowed"}, None]
    def flaky():
        return seq.pop(0)
    t = {"now": 0.0}
    c = clawd_serve.Cache(flaky, ttl=60, clock=lambda: t["now"])
    c.payload()                   # 1º ping ok
    t["now"] = 100.0
    p = c.payload()               # 2º ping falha → serve o último bom
    assert p["s"] == 50
    assert p["age"] == 100


def test_payload_null_when_never_succeeded():
    c = clawd_serve.Cache(lambda: None, ttl=60, clock=lambda: 0.0)
    p = c.payload()
    assert p["s"] is None and p["w"] is None and p["st"] == "unknown"


def test_http_get_tokens_returns_json():
    cache = clawd_serve.Cache(
        lambda: {"s": 42, "sr": 1, "w": 18, "wr": 1, "st": "allowed"},
        ttl=60, clock=lambda: 0.0)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), clawd_serve.make_handler(cache))
    threading.Thread(target=srv.handle_request, daemon=True).start()
    port = srv.server_address[1]
    body = urllib.request.urlopen(f"http://127.0.0.1:{port}/tokens", timeout=5).read()
    data = json.loads(body)
    assert data["s"] == 42 and data["w"] == 18 and data["st"] == "allowed"
    srv.server_close()


if __name__ == "__main__":
    _run_all()
