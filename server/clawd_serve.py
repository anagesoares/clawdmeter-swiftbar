#!/usr/bin/env python3
# Servidor local que publica o % de limite (5h/7d) pro Claude Buddy buscar por WiFi.
# Reusa a lógica de ping do clawd (duplicada de propósito p/ não tocar o plugin).
import getpass
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEYCHAIN_SERVICE = "Claude Code-credentials"
API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {"model": "claude-haiku-4-5-20251001", "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}]}
PORT = int(os.environ.get("CLAWD_PORT", "8787"))


def _extract_token(blob):
    blob = (blob or "").strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _read_token():
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", getpass.getuser(), "-w"],
            capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return _extract_token(out.stdout)


def poll_limit():
    """Return {s, sr, w, wr, st} from rate-limit headers, or None."""
    if os.environ.get("CLAWD_NO_PING") == "1":
        return None
    token = _read_token()
    if not token:
        return None
    headers = dict(API_HEADERS)
    headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(API_URL, data=json.dumps(API_BODY).encode(),
                                 headers=headers, method="POST")
    try:
        h = urllib.request.urlopen(req, timeout=20).headers
    except urllib.error.HTTPError as e:
        h = e.headers
    except (urllib.error.URLError, OSError):
        return None
    if h is None:
        return None

    def pct(name):
        try:
            return int(round(float(h.get(name, "")) * 100))
        except (TypeError, ValueError):
            return None

    now = time.time()

    def mins(name):
        try:
            m = (float(h.get(name, "")) - now) / 60.0
        except (TypeError, ValueError):
            return None
        return int(round(m)) if m > 0 else 0

    s = pct("anthropic-ratelimit-unified-5h-utilization")
    w = pct("anthropic-ratelimit-unified-7d-utilization")
    if s is None and w is None:
        return None
    return {"s": s, "sr": mins("anthropic-ratelimit-unified-5h-reset"),
            "w": w, "wr": mins("anthropic-ratelimit-unified-7d-reset"),
            "st": h.get("anthropic-ratelimit-unified-5h-status", "unknown")}


class Cache:
    def __init__(self, poller, ttl=60, clock=time.time):
        self._poller = poller
        self._ttl = ttl
        self._clock = clock
        self._last = None          # último resultado bom
        self._last_ok_t = None     # quando pingou com sucesso

    def payload(self):
        now = self._clock()
        stale = self._last_ok_t is None or (now - self._last_ok_t) >= self._ttl
        if stale:
            res = self._poller()
            if res is not None:
                self._last = res
                self._last_ok_t = now
        if self._last is None:
            return {"s": None, "w": None, "st": "unknown", "age": -1}
        age = int(now - self._last_ok_t)
        return {"s": self._last["s"], "w": self._last["w"],
                "st": self._last["st"], "age": age}


def make_handler(cache):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):     # silencioso
            pass

        def do_GET(self):
            if self.path.rstrip("/") != "/tokens":
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(cache.payload()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Handler


def main():
    cache = Cache(poll_limit, ttl=60)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), make_handler(cache))
    print(f"clawd-serve on :{PORT}", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    main()
