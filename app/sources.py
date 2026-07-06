"""カメラ画像の取得。多様な配信形式に対応する。

対応する source.type:

- ``local``      … リポジトリ内の画像ファイル (デモ用)
- ``url``        … 静止画 URL。時刻テンプレート ``{now:%Y%m%d%H%M}`` と
                    カスタム HTTP ヘッダ (Referer 等) に対応
- ``mjpeg``      … Motion JPEG ストリームから最初の 1 フレームを取得
- ``stream``     … HLS (.m3u8) / RTSP / MP4 等の動画から 1 フレームを取得
- ``youtube``    … YouTube ライブ。yt-dlp が入っていればフレーム取得も可能
- ``page_image`` … HTML ページから画像 URL を抽出して取得
- ``page``       … 提供元ページへのリンクのみ (画像取得なし)

フレームが取得できる形式はすべて視程・シーリング推定に使える。
"""

from __future__ import annotations

import concurrent.futures
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin

import cv2
import httpx
import numpy as np

from .config import ROOT, CameraConfig

# 取得間隔 (秒)。この間は同じカメラへの再取得をせずキャッシュを返す
FETCH_INTERVAL_SEC = 60
# 動画ストリームからのフレーム取得タイムアウト (秒)
STREAM_TIMEOUT_SEC = 30
# YouTube ストリーム URL の解決結果を保持する時間 (秒)
YOUTUBE_RESOLVE_TTL_SEC = 600

# カメラサーバの多くはブラウザ以外の UA を弾くため、ブラウザ相当の UA を既定にする
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}

JST = timezone(timedelta(hours=9))

_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

# cam.id -> (取得時刻 monotonic, JPEG バイト列)
_frame_cache: dict[str, tuple[float, bytes]] = {}
# cam.id -> 直近の取得エラー内容 (成功時は削除)
_last_errors: dict[str, str] = {}
# video/channel key -> (解決時刻, ストリーム URL)
_yt_cache: dict[str, tuple[float, str]] = {}


class SourceError(Exception):
    """画像取得失敗。message はそのまま UI に表示される。"""


# ---------------------------------------------------------------- 共通処理


def _decode(data: bytes) -> Optional[np.ndarray]:
    if not data:
        return None
    return cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)


def _headers(cam: CameraConfig) -> dict:
    h = dict(DEFAULT_HEADERS)
    h.update(cam.source_raw.get("headers", {}))
    return h


def _get_bytes(url: str, headers: dict, timeout: float = 15) -> bytes:
    resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def expand_time_template(
    url: str,
    now: Optional[datetime] = None,
    floor_min: int = 0,
    offset_min: int = 0,
    max_backtrack: int = 3,
) -> list[str]:
    """時刻テンプレート入り URL を候補リストに展開する。

    ``{now:%Y%m%d%H%M}`` を JST 現在時刻で置換する。floor_min で
    更新間隔 (分) に切り捨て、offset_min で配信遅延分を引く。
    取得失敗に備え、1 間隔ずつ遡った候補も返す (最大 max_backtrack 個)。
    テンプレートがない URL はそのまま 1 件返す。
    """
    if "{now:" not in url:
        return [url]
    base = now.astimezone(JST) if now else datetime.now(JST)
    base -= timedelta(minutes=offset_min)
    if floor_min > 0:
        base = base.replace(
            minute=base.minute - base.minute % floor_min, second=0, microsecond=0
        )
    step = max(floor_min, 1)
    candidates = []
    for k in range(max_backtrack):
        dt = base - timedelta(minutes=step * k)
        candidates.append(
            re.sub(r"\{now:([^}]+)\}", lambda m: dt.strftime(m.group(1)), url)
        )
    return candidates


# ---------------------------------------------------------------- 各形式


def read_local(rel_path: str) -> Optional[np.ndarray]:
    path = (ROOT / rel_path).resolve()
    if not path.is_relative_to(ROOT) or not path.exists():
        return None
    return cv2.imread(str(path), cv2.IMREAD_COLOR)


def fetch_still(cam: CameraConfig) -> np.ndarray:
    src = cam.source_raw
    candidates = expand_time_template(
        cam.source,
        floor_min=int(src.get("time_floor_min", 0)),
        offset_min=int(src.get("time_offset_min", 0)),
    )
    last_exc: Optional[Exception] = None
    for url in candidates:
        try:
            img = _decode(_get_bytes(url, _headers(cam)))
            if img is not None:
                return img
            last_exc = SourceError("画像としてデコードできませんでした")
        except httpx.HTTPError as e:
            last_exc = e
    raise SourceError(f"静止画を取得できません: {last_exc}")


def first_jpeg_in_buffer(buf: bytes) -> Optional[bytes]:
    """バイト列から最初の完全な JPEG (SOI..EOI) を切り出す。"""
    start = buf.find(b"\xff\xd8\xff")
    if start == -1:
        return None
    end = buf.find(b"\xff\xd9", start + 3)
    if end == -1:
        return None
    return buf[start : end + 2]


def fetch_mjpeg(cam: CameraConfig) -> np.ndarray:
    """MJPEG ストリームを読み、最初の 1 フレームを返す。"""
    buf = b""
    try:
        with httpx.stream(
            "GET", cam.source, headers=_headers(cam), timeout=20, follow_redirects=True
        ) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_bytes():
                buf += chunk
                jpg = first_jpeg_in_buffer(buf)
                if jpg is not None:
                    img = _decode(jpg)
                    if img is not None:
                        return img
                    buf = buf[buf.find(b"\xff\xd9") + 2 :]
                if len(buf) > 8 * 1024 * 1024:
                    break
    except httpx.HTTPError as e:
        raise SourceError(f"MJPEG ストリームに接続できません: {e}")
    raise SourceError("MJPEG ストリームからフレームを取得できませんでした")


def _grab_frame(url: str) -> Optional[np.ndarray]:
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    try:
        ok, frame = cap.read()
        return frame if ok else None
    finally:
        cap.release()


def fetch_stream(url: str, timeout: float = STREAM_TIMEOUT_SEC) -> np.ndarray:
    """HLS / RTSP / MP4 等の動画ストリームから 1 フレーム取得する。"""
    future = _executor.submit(_grab_frame, url)
    try:
        frame = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        raise SourceError(f"動画ストリームの読み込みが {timeout:.0f} 秒でタイムアウトしました")
    if frame is None:
        raise SourceError("動画ストリームからフレームを取得できませんでした")
    return frame


def resolve_youtube_stream_url(cam: CameraConfig) -> str:
    """yt-dlp で YouTube ライブの実ストリーム URL を解決する (10 分キャッシュ)。"""
    key = cam.youtube_channel_id or cam.source
    cached = _yt_cache.get(key)
    if cached and time.monotonic() - cached[0] < YOUTUBE_RESOLVE_TTL_SEC:
        return cached[1]
    try:
        import yt_dlp  # 任意依存: 無ければ視聴のみ
    except ImportError:
        raise SourceError(
            "yt-dlp が未インストールのためフレーム取得できません "
            "(pip install yt-dlp で推定が有効になります)"
        )
    watch = (
        f"https://www.youtube.com/channel/{cam.youtube_channel_id}/live"
        if cam.youtube_channel_id
        else f"https://www.youtube.com/watch?v={cam.source}"
    )
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "best[height<=720][protocol^=m3u8]/best[height<=720]/best",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(watch, download=False)
    except Exception as e:  # yt-dlp は多様な例外を投げる
        raise SourceError(f"YouTube ライブの解決に失敗しました: {e}")
    url = info.get("url")
    if not url:
        raise SourceError("YouTube ライブのストリーム URL が見つかりません")
    _yt_cache[key] = (time.monotonic(), url)
    return url


def fetch_youtube(cam: CameraConfig) -> np.ndarray:
    return fetch_stream(resolve_youtube_stream_url(cam))


IMG_TAG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)
OG_IMAGE_RE = re.compile(
    r"<meta[^>]+property=[\"']og:image[\"'][^>]+content=[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


def extract_image_urls(html: str, base_url: str, image_regex: str = "") -> list[str]:
    """HTML からカメラ画像らしい URL 候補を抽出する。

    image_regex が指定されていればそれを最優先 (グループ 1 または全体)。
    無指定なら <img> タグと og:image から jpg/png を集め、
    ライブカメラらしいキーワードを含むものを優先する。
    """
    if image_regex:
        m = re.search(image_regex, html)
        if not m:
            return []
        url = m.group(1) if m.groups() else m.group(0)
        return [urljoin(base_url, url)]

    candidates = OG_IMAGE_RE.findall(html) + IMG_TAG_RE.findall(html)
    candidates = [
        urljoin(base_url, u)
        for u in candidates
        if re.search(r"\.(jpe?g|png)(\?|$)", u, re.IGNORECASE)
    ]
    keywords = ("cam", "live", "capture", "latest", "current", "now", "img")
    candidates.sort(key=lambda u: -sum(k in u.lower() for k in keywords))
    # 重複除去 (順序維持)
    seen: set[str] = set()
    return [u for u in candidates if not (u in seen or seen.add(u))]


def fetch_page_image(cam: CameraConfig) -> np.ndarray:
    headers = _headers(cam)
    try:
        html = _get_bytes(cam.source, headers).decode("utf-8", errors="replace")
    except httpx.HTTPError as e:
        raise SourceError(f"ページを取得できません: {e}")
    urls = extract_image_urls(html, cam.source, cam.source_raw.get("image_regex", ""))
    if not urls:
        raise SourceError(
            "ページから画像 URL を抽出できませんでした "
            "(source.image_regex の指定を検討してください)"
        )
    page_headers = dict(headers)
    page_headers.setdefault("Referer", cam.source)
    for url in urls[:5]:
        try:
            img = _decode(_get_bytes(url, page_headers))
            # 小さすぎる画像 (アイコン等) は除外
            if img is not None and img.shape[0] >= 120 and img.shape[1] >= 160:
                return img
        except httpx.HTTPError:
            continue
    raise SourceError("抽出した画像 URL からカメラ画像を取得できませんでした")


# ---------------------------------------------------------------- 入口


def image_capable(cam: CameraConfig) -> bool:
    """フレーム取得 (=推定) が可能なソースか。"""
    if cam.source_type in ("local", "url", "mjpeg", "stream", "page_image"):
        return True
    if cam.source_type == "youtube":
        try:
            import yt_dlp  # noqa: F401

            return True
        except ImportError:
            return False
    return False


def last_error(cam_id: str) -> str:
    return _last_errors.get(cam_id, "")


def has_cached_frame(cam_id: str) -> bool:
    return cam_id in _frame_cache


def _fetch_fresh(cam: CameraConfig) -> Optional[np.ndarray]:
    if cam.source_type == "local":
        return read_local(cam.source)
    if cam.source_type == "url":
        return fetch_still(cam)
    if cam.source_type == "mjpeg":
        return fetch_mjpeg(cam)
    if cam.source_type == "stream":
        return fetch_stream(cam.source)
    if cam.source_type == "youtube":
        return fetch_youtube(cam)
    if cam.source_type == "page_image":
        return fetch_page_image(cam)
    return None


def current_image(cam: CameraConfig) -> Optional[np.ndarray]:
    """現在画像を取得する (60 秒キャッシュ・エラー記録つき)。"""
    if cam.source_type == "page":
        return None
    if cam.source_type != "local":
        cached = _frame_cache.get(cam.id)
        if cached and time.monotonic() - cached[0] < FETCH_INTERVAL_SEC:
            return _decode(cached[1])
    try:
        img = _fetch_fresh(cam)
    except SourceError as e:
        _last_errors[cam.id] = str(e)
        cached = _frame_cache.get(cam.id)
        return _decode(cached[1]) if cached else None
    except Exception as e:
        _last_errors[cam.id] = f"取得エラー: {e}"
        cached = _frame_cache.get(cam.id)
        return _decode(cached[1]) if cached else None
    if img is None:
        _last_errors.setdefault(cam.id, "画像を取得できませんでした")
        return None
    _last_errors.pop(cam.id, None)
    if cam.source_type != "local":
        _frame_cache[cam.id] = (time.monotonic(), encode_jpeg(img))
    return img


def reference_image(cam: CameraConfig) -> Optional[np.ndarray]:
    if not cam.reference_image:
        return None
    return read_local(cam.reference_image)


def save_reference(cam: CameraConfig, img: np.ndarray) -> str:
    """現在画像を晴天時基準画像として保存する。"""
    if not cam.reference_image:
        raise ValueError("このカメラには基準画像パスが設定されていません")
    path = (ROOT / cam.reference_image).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("不正な保存先パスです")
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return cam.reference_image


def encode_jpeg(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise ValueError("JPEG エンコードに失敗しました")
    return buf.tobytes()
