"""バックグラウンドでカメラを巡回し、評価スナップショットを保持する。

YouTube や動画ストリームからのフレーム取得は 1 カメラ数秒かかるため、
地図表示のたびに全カメラを取得すると使い物にならない。代わりに
デーモンスレッドが一定間隔で全カメラを巡回して評価し、API は
最新スナップショットを即座に返す。

巡回では同時に:
- 自動基準画像の学習 (quicklook.consider_as_auto_reference)
- 推定履歴の記録 (history.record)
も行う。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from . import analysis, history, quicklook, sources
from .config import CameraConfig

REFRESH_INTERVAL_SEC = 120
# 巡回中のカメラ間の小休止 (取得先への配慮とCPUスパイク回避)
PER_CAMERA_PAUSE_SEC = 1.0

_snapshot: dict[str, dict] = {}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def camera_status(cam: CameraConfig) -> str:
    """カメラの動作状態 (main.py と共有)。"""
    if cam.source_type == "page":
        return "page"
    if not sources.image_capable(cam):
        return "view"
    if sources.reference_image(cam) is None:
        return "needs_reference"
    if not cam.targets:
        return "needs_targets"
    return "estimate"


def summarize(est: analysis.Estimate) -> dict:
    return {
        "visibility_km": est.visibility_km,
        "visibility_is_lower_bound": est.visibility_is_lower_bound,
        "ceiling_ft_agl": est.ceiling_ft_agl,
        "ceiling_is_unlimited": est.ceiling_is_unlimited,
        "flight_category": est.flight_category,
    }


def evaluate_camera(cam: CameraConfig, fetch_frames: bool = True) -> dict:
    """1 カメラを評価してスナップショットエントリを作る。

    fetch_frames=False ならネットワーク取得を伴う処理をスキップし、
    キャッシュ済みフレームがある場合のみ簡易評価する (地図の初期表示用)。
    """
    status = camera_status(cam)
    entry: dict = {
        "status": status,
        "summary": None,
        "quick": None,
        "last_error": "",
        "updated": time.time(),
    }
    if status in ("page", "view"):
        return entry

    if not fetch_frames and cam.source_type != "local" and not sources.has_cached_frame(cam.id):
        return entry

    cur = sources.current_image(cam)
    if cur is None:
        entry["status"] = "error"
        entry["last_error"] = sources.last_error(cam.id)
        return entry

    try:
        quicklook.consider_as_auto_reference(cam, cur)
        entry["quick"] = quicklook.assess(cam, cur)
    except Exception:
        pass  # 簡易評価は参考情報: 失敗しても他を止めない

    if status == "estimate":
        ref = sources.reference_image(cam)
        if ref is not None:
            try:
                est = analysis.analyze(
                    ref,
                    cur,
                    cam.targets,
                    camera_elevation_ft=cam.elevation_ft,
                    sky_bbox=cam.sky_bbox,
                )
                entry["summary"] = summarize(est)
                history.record(cam.id, est)
            except Exception:
                entry["status"] = "error"
                entry["last_error"] = "解析に失敗しました"
    return entry


def get(cam_id: str) -> Optional[dict]:
    return _snapshot.get(cam_id)


def _loop(cameras_provider: Callable[[], list[CameraConfig]]) -> None:
    while not _stop.is_set():
        started = time.monotonic()
        for cam in cameras_provider():
            if _stop.is_set():
                return
            try:
                _snapshot[cam.id] = evaluate_camera(cam)
            except Exception:
                pass  # 個別カメラの失敗で巡回を止めない
            _stop.wait(PER_CAMERA_PAUSE_SEC)
        elapsed = time.monotonic() - started
        _stop.wait(max(REFRESH_INTERVAL_SEC - elapsed, 5.0))


def start(cameras_provider: Callable[[], list[CameraConfig]]) -> None:
    """巡回スレッドを開始する (多重起動は無視)。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(
        target=_loop, args=(cameras_provider,), daemon=True, name="heliwx-refresher"
    )
    _thread.start()


def stop() -> None:
    _stop.set()
