"""画像取得レイヤー (app/sources.py) のテスト。

ローカル HTTP サーバを立てて、静止画 / 時刻テンプレート / MJPEG /
ページ抽出 / 動画ストリームの各形式を実際に取得して検証する。
"""

import sys
import threading
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import sources  # noqa: E402
from app.config import CameraConfig  # noqa: E402

JST = timezone(timedelta(hours=9))


def make_camera(cam_id: str, source: dict) -> CameraConfig:
    return CameraConfig.from_dict(
        {
            "id": cam_id,
            "name": cam_id,
            "lat": 35.0,
            "lon": 139.0,
            "heading_deg": 0,
            "source": source,
        }
    )


def sample_jpeg() -> bytes:
    rng = np.random.default_rng(1)
    img = rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


JPEG = sample_jpeg()


class Handler(BaseHTTPRequestHandler):
    """テスト用エンドポイント群。"""

    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/latest.jpg":
            self._send(200, JPEG, "image/jpeg")
        elif self.path.startswith("/timed/"):
            # /timed/<YYYYmmddHHMM>.jpg — 10 分刻みの「1 つ前」だけ存在する
            now = datetime.now(JST)
            floored = now.replace(minute=now.minute - now.minute % 10, second=0, microsecond=0)
            prev = floored - timedelta(minutes=10)
            if self.path == f"/timed/{prev.strftime('%Y%m%d%H%M')}.jpg":
                self._send(200, JPEG, "image/jpeg")
            else:
                self._send(404, b"not found", "text/plain")
        elif self.path == "/needs-referer.jpg":
            if self.headers.get("Referer"):
                self._send(200, JPEG, "image/jpeg")
            else:
                self._send(403, b"forbidden", "text/plain")
        elif self.path == "/mjpeg":
            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame"
            )
            self.end_headers()
            for _ in range(3):
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
                self.wfile.write(JPEG)
                self.wfile.write(b"\r\n")
        elif self.path == "/campage.html":
            html = (
                "<html><head>"
                '<meta property="og:image" content="/ogp_banner.png">'
                "</head><body>"
                '<img src="/icons/tiny.jpg">'
                '<img src="/livecam/current.jpg?t=123">'
                "</body></html>"
            )
            self._send(200, html.encode(), "text/html")
        elif self.path.startswith("/livecam/current.jpg"):
            self._send(200, JPEG, "image/jpeg")
        elif self.path.startswith(("/icons/tiny.jpg", "/ogp_banner.png")):
            tiny = np.zeros((32, 32, 3), dtype=np.uint8)
            ok, buf = cv2.imencode(".jpg", tiny)
            self._send(200, buf.tobytes(), "image/jpeg")
        else:
            self._send(404, b"not found", "text/plain")


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


class TestTimeTemplate:
    def test_no_template_passthrough(self):
        assert sources.expand_time_template("http://x/a.jpg") == ["http://x/a.jpg"]

    def test_floor_and_backtrack(self):
        now = datetime(2026, 7, 3, 12, 34, tzinfo=JST)
        urls = sources.expand_time_template(
            "http://x/{now:%Y%m%d%H%M}.jpg", now=now, floor_min=10
        )
        assert urls[0] == "http://x/202607031230.jpg"
        assert urls[1] == "http://x/202607031220.jpg"
        assert urls[2] == "http://x/202607031210.jpg"

    def test_offset(self):
        now = datetime(2026, 7, 3, 12, 5, tzinfo=JST)
        urls = sources.expand_time_template(
            "http://x/{now:%H%M}.jpg", now=now, floor_min=10, offset_min=5
        )
        assert urls[0] == "http://x/1200.jpg"


class TestJpegBuffer:
    def test_extract_jpeg(self):
        buf = b"junkjunk" + JPEG + b"trailing"
        out = sources.first_jpeg_in_buffer(buf)
        assert out is not None
        assert sources._decode(out) is not None

    def test_incomplete_returns_none(self):
        assert sources.first_jpeg_in_buffer(JPEG[:100]) is None
        assert sources.first_jpeg_in_buffer(b"nothing here") is None


class TestExtractImageUrls:
    HTML = (
        '<meta property="og:image" content="https://x.example/ogp.png">'
        '<img src="/img/logo.png"><img src="/cam/livecam_latest.jpg">'
    )

    def test_keyword_priority(self):
        urls = sources.extract_image_urls(self.HTML, "https://x.example/page")
        assert urls[0] == "https://x.example/cam/livecam_latest.jpg"

    def test_regex_override(self):
        urls = sources.extract_image_urls(
            self.HTML, "https://x.example/page", image_regex=r'src="(/img/[^"]+)"'
        )
        assert urls == ["https://x.example/img/logo.png"]

    def test_regex_no_match(self):
        assert (
            sources.extract_image_urls(self.HTML, "https://x", image_regex=r"zzz(9)?")
            == []
        )


class TestFetchStill:
    def test_plain_url(self, server):
        cam = make_camera("t1", {"type": "url", "url": f"{server}/latest.jpg"})
        img = sources.current_image(cam)
        assert img is not None and img.shape == (240, 320, 3)
        assert sources.last_error("t1") == ""

    def test_time_template_with_backtrack(self, server):
        # 最新時刻の画像は 404 で、1 間隔前が取得できるケース
        cam = make_camera(
            "t2",
            {
                "type": "url",
                "url": f"{server}/timed/{{now:%Y%m%d%H%M}}.jpg",
                "time_floor_min": 10,
            },
        )
        assert sources.current_image(cam) is not None

    def test_custom_headers(self, server):
        cam = make_camera(
            "t3",
            {
                "type": "url",
                "url": f"{server}/needs-referer.jpg",
                "headers": {"Referer": "https://provider.example/"},
            },
        )
        assert sources.current_image(cam) is not None

    def test_error_recorded(self, server):
        cam = make_camera("t4", {"type": "url", "url": f"{server}/missing.jpg"})
        assert sources.current_image(cam) is None
        assert sources.last_error("t4") != ""


class TestFetchMjpeg:
    def test_first_frame(self, server):
        cam = make_camera("m1", {"type": "mjpeg", "url": f"{server}/mjpeg"})
        img = sources.current_image(cam)
        assert img is not None and img.shape == (240, 320, 3)


class TestPageImage:
    def test_extracts_camera_image(self, server):
        # og:image とアイコンは小さすぎるので除外され、livecam 画像が選ばれる
        cam = make_camera("p1", {"type": "page_image", "url": f"{server}/campage.html"})
        img = sources.current_image(cam)
        assert img is not None and img.shape == (240, 320, 3)


class TestFetchStream:
    def test_frame_from_video_file(self, tmp_path):
        video = tmp_path / "cam.avi"
        w = cv2.VideoWriter(
            str(video), cv2.VideoWriter_fourcc(*"MJPG"), 5, (320, 240)
        )
        if not w.isOpened():
            pytest.skip("VideoWriter (MJPG) が利用できない環境")
        rng = np.random.default_rng(2)
        for _ in range(10):
            w.write(rng.integers(0, 255, (240, 320, 3), dtype=np.uint8))
        w.release()
        cam = make_camera("s1", {"type": "stream", "url": str(video)})
        img = sources.current_image(cam)
        assert img is not None and img.shape == (240, 320, 3)


class TestCapability:
    def test_frame_capable_types(self):
        for stype in ("url", "mjpeg", "stream", "page_image"):
            cam = make_camera(f"c-{stype}", {"type": stype, "url": "http://x/"})
            assert sources.image_capable(cam)

    def test_page_not_capable(self):
        cam = make_camera("c-page", {"type": "page", "url": "http://x/"})
        assert not sources.image_capable(cam)
        assert sources.current_image(cam) is None

    def test_cache_returns_same_frame(self, ):
        # current_image は取得成功後 60 秒間キャッシュを返す
        assert "t1" in sources._frame_cache
