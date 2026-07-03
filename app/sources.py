"""カメラ画像の取得。ローカルファイルまたはライブカメラ URL に対応。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import cv2
import httpx
import numpy as np

from .config import ROOT, CameraConfig

# ライブカメラ URL のフェッチ間隔 (秒)。この間はキャッシュを返す。
FETCH_INTERVAL_SEC = 60

_cache: dict[str, tuple[float, bytes]] = {}


def _decode(data: bytes) -> Optional[np.ndarray]:
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    return img


def read_local(rel_path: str) -> Optional[np.ndarray]:
    path = (ROOT / rel_path).resolve()
    if not path.is_relative_to(ROOT) or not path.exists():
        return None
    return cv2.imread(str(path), cv2.IMREAD_COLOR)


def fetch_url(url: str) -> Optional[np.ndarray]:
    now = time.monotonic()
    cached = _cache.get(url)
    if cached and now - cached[0] < FETCH_INTERVAL_SEC:
        return _decode(cached[1])
    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
        _cache[url] = (now, resp.content)
        return _decode(resp.content)
    except httpx.HTTPError:
        # 失敗時は古いキャッシュがあればそれを返す
        if cached:
            return _decode(cached[1])
        return None


def current_image(cam: CameraConfig) -> Optional[np.ndarray]:
    if cam.source_type == "url":
        return fetch_url(cam.source)
    return read_local(cam.source)


def reference_image(cam: CameraConfig) -> Optional[np.ndarray]:
    return read_local(cam.reference_image)


def encode_jpeg(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise ValueError("JPEG エンコードに失敗しました")
    return buf.tobytes()
