"""最寄り空港の METAR 取得。

本アプリの推定は参考情報であり、公式気象情報と併用するのが前提。
その公式情報 (METAR) をパネル内に併記できるよう、
aviationweather.gov の公開 API から生電文を取得する。
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import httpx

API_URL = "https://aviationweather.gov/api/data/metar"
CACHE_TTL_SEC = 300
FETCH_TIMEOUT_SEC = 10

# station -> (取得時刻 monotonic, 生電文)
_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def get_metar(station: str) -> Optional[dict]:
    """指定局の最新 METAR 生電文を返す (5 分キャッシュ)。

    取得できない場合は古いキャッシュがあればそれを、なければ None を返す。
    例外は外に出さない。
    """
    station = (station or "").strip().upper()
    if not station:
        return None

    with _lock:
        cached = _cache.get(station)
        if cached and time.monotonic() - cached[0] < CACHE_TTL_SEC:
            return {"station": station, "raw": cached[1], "stale": False}

    try:
        resp = httpx.get(
            API_URL,
            params={"ids": station, "format": "raw", "taf": "false"},
            timeout=FETCH_TIMEOUT_SEC,
        )
        resp.raise_for_status()
        lines = [ln.strip() for ln in resp.text.strip().splitlines() if ln.strip()]
        raw = lines[0] if lines else ""
    except httpx.HTTPError:
        raw = ""

    if raw:
        with _lock:
            _cache[station] = (time.monotonic(), raw)
        return {"station": station, "raw": raw, "stale": False}

    # 取得失敗: 古いキャッシュがあれば「古い」と明示して返す
    with _lock:
        cached = _cache.get(station)
    if cached:
        return {"station": station, "raw": cached[1], "stale": True}
    return None
