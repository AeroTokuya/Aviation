"""METAR 取得 (app/metar.py) のテスト。ローカル HTTP サーバでモックする。"""

import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import metar  # noqa: E402

RAW = "RJTT 030700Z 18008KT 9999 FEW030 SCT120 28/22 Q1012 NOSIG"

request_count = {"n": 0}
fail_mode = {"on": False}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        request_count["n"] += 1
        if fail_mode["on"]:
            self.send_response(503)
            self.end_headers()
            return
        body = (RAW + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/metar"
    srv.shutdown()


@pytest.fixture(autouse=True)
def isolated(server, monkeypatch):
    monkeypatch.setattr(metar, "API_URL", server)
    monkeypatch.setattr(metar, "_cache", {})
    request_count["n"] = 0
    fail_mode["on"] = False


class TestGetMetar:
    def test_fetch_raw(self):
        out = metar.get_metar("RJTT")
        assert out == {"station": "RJTT", "raw": RAW, "stale": False}

    def test_cached_within_ttl(self):
        metar.get_metar("RJTT")
        metar.get_metar("RJTT")
        assert request_count["n"] == 1

    def test_station_normalized(self):
        out = metar.get_metar(" rjtt ")
        assert out["station"] == "RJTT"

    def test_empty_station_returns_none(self):
        assert metar.get_metar("") is None
        assert metar.get_metar(None) is None
        assert request_count["n"] == 0

    def test_error_returns_none_without_cache(self):
        fail_mode["on"] = True
        assert metar.get_metar("RJTT") is None

    def test_error_returns_stale_cache(self):
        metar.get_metar("RJTT")  # キャッシュを作る
        fail_mode["on"] = True
        expired = time.monotonic() - metar.CACHE_TTL_SEC - 1
        metar._cache["RJTT"] = (expired, RAW)  # TTL 切れに偽装
        out = metar.get_metar("RJTT")
        assert out["stale"] is True
        assert out["raw"] == RAW
