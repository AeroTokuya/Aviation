"""HeliWX — ライブカメラ視程・シーリング推定 API サーバ。

起動: uvicorn app.main:app --reload
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analysis, history, metar, quicklook, refresher, sources
from .config import (
    CameraConfig,
    ROOT,
    add_camera_entry,
    delete_camera_entry,
    load_cameras,
    save_camera_setup,
)

app = FastAPI(title="HeliWX", description="ヘリコプター運航向け 視程・シーリング推定")

cameras = load_cameras()

# 動作状態の意味:
#   estimate        … 視程・シーリング推定が可能 (基準画像 + ターゲットあり)
#   needs_targets   … 画像はあるがターゲット未設定 → 画像比較 + 簡易評価
#   needs_reference … 晴天時基準画像が未取得 → 現在画像 + 簡易評価
#   view            … 映像視聴のみ (フレーム取得不可)
#   page            … 提供元ページへのリンクのみ
_status = refresher.camera_status


@app.on_event("startup")
def _start_refresher() -> None:
    # テスト等では HELIWX_BACKGROUND_REFRESH=0 で巡回を止められる
    if os.environ.get("HELIWX_BACKGROUND_REFRESH", "1") != "0":
        refresher.start(lambda: list(cameras.values()))


def _try_analyze(cam: CameraConfig) -> Optional[analysis.Estimate]:
    """推定可能なカメラなら推定を実行。不可能/失敗なら None。"""
    if _status(cam) != "estimate":
        return None
    ref = sources.reference_image(cam)
    cur = sources.current_image(cam)
    if ref is None or cur is None:
        return None
    est = analysis.analyze(
        ref, cur, cam.targets, camera_elevation_ft=cam.elevation_ft, sky_bbox=cam.sky_bbox
    )
    try:
        history.record(cam.id, est)
    except Exception:
        pass  # 履歴は副次機能: 記録の失敗で正常な推定結果を潰さない
    return est


def _camera_info(cam: CameraConfig) -> dict:
    return {
        "id": cam.id,
        "name": cam.name,
        "lat": cam.lat,
        "lon": cam.lon,
        "heading_deg": cam.heading_deg,
        "fov_deg": cam.fov_deg,
        "elevation_ft": cam.elevation_ft,
        "description": cam.description,
        "source_type": cam.source_type,
        "youtube_video_id": cam.source if cam.source_type == "youtube" else "",
        "youtube_channel_id": cam.youtube_channel_id,
        "attribution": cam.attribution,
        "page_url": cam.page_url,
        "image_capable": sources.image_capable(cam),
    }


@app.get("/api/cameras")
def list_cameras() -> list[dict]:
    """全カメラの位置・方角と最新評価 (地図アイコン用)。

    評価はバックグラウンド巡回のスナップショットを返す。まだ巡回が
    済んでいないカメラは、ネットワーク取得を伴わない範囲で即時評価する
    (ローカルのデモカメラは常に即時評価できる)。
    """
    out = []
    for cam in cameras.values():
        item = _camera_info(cam)
        snap = refresher.get(cam.id)
        if snap is None:
            try:
                snap = refresher.evaluate_camera(cam, fetch_frames=False)
            except Exception:
                snap = {"status": "error", "summary": None, "quick": None,
                        "last_error": "", "updated": time.time()}
        item.update(
            status=snap["status"],
            summary=snap["summary"],
            quick=snap["quick"],
            last_error=snap.get("last_error", ""),
        )
        out.append(item)
    return out


@app.get("/api/cameras/{cam_id}/estimate")
def camera_estimate(cam_id: str) -> dict:
    cam = cameras.get(cam_id)
    if cam is None:
        raise HTTPException(404, f"カメラ {cam_id} は存在しません")
    status = _status(cam)
    est = None
    if status == "estimate":
        try:
            est = _try_analyze(cam)
        except Exception:
            status = "error"
    # 簡易評価 (フレームは 60 秒キャッシュされるので二重取得にはならない)
    quick = None
    if status not in ("page", "view"):
        cur = sources.current_image(cam)
        if cur is not None:
            try:
                quicklook.consider_as_auto_reference(cam, cur)
                quick = quicklook.assess(cam, cur)
            except Exception:
                quick = None
        elif status != "error":
            status = "error"
    return {
        "camera": _camera_info(cam),
        "status": status,
        "has_reference": sources.reference_image(cam) is not None,
        "last_error": sources.last_error(cam_id),
        "metar": metar.get_metar(cam.metar_station),
        "quick": quick,
        "estimate": asdict(est) if est is not None else None,
        "setup": {
            "targets": [
                {
                    "name": t.name,
                    "distance_km": t.distance_km,
                    "bbox": list(t.bbox),
                    "elevation_ft_msl": t.elevation_ft_msl,
                }
                for t in cam.targets
            ],
            "sky_bbox": list(cam.sky_bbox) if cam.sky_bbox else None,
        },
    }


@app.get("/api/cameras/{cam_id}/history")
def camera_history(cam_id: str, hours: float = 12.0) -> dict:
    """視程・シーリングの推移 (トレンド表示用)。"""
    if cam_id not in cameras:
        raise HTTPException(404, f"カメラ {cam_id} は存在しません")
    hours = max(1.0, min(hours, 48.0))
    return {"points": history.load(cam_id, hours)}


@app.get("/api/cameras/{cam_id}/image/{kind}")
def camera_image(cam_id: str, kind: str) -> Response:
    cam = cameras.get(cam_id)
    if cam is None:
        raise HTTPException(404, f"カメラ {cam_id} は存在しません")
    if kind == "current":
        img = sources.current_image(cam)
    elif kind == "reference":
        img = sources.reference_image(cam)
    else:
        raise HTTPException(404, "kind は current または reference")
    if img is None:
        raise HTTPException(502, "画像を取得できません")
    return Response(
        content=sources.encode_jpeg(img),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


SOURCE_TYPES = ("url", "mjpeg", "stream", "youtube", "page_image", "page")


class CameraIn(BaseModel):
    """ブラウザからのカメラ追加。"""

    name: str = Field(min_length=1, max_length=80)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    heading_deg: float = Field(ge=0, le=360)
    fov_deg: float = Field(default=60, ge=10, le=180)
    elevation_ft: float = Field(default=0, ge=-100, le=15000)
    source_type: str
    source_value: str = Field(min_length=1, max_length=500)  # URL または video/channel ID
    description: str = Field(default="", max_length=300)
    attribution: str = Field(default="", max_length=120)
    page_url: str = Field(default="", max_length=500)
    metar_station: str = Field(default="", max_length=4)


@app.post("/api/cameras")
def add_camera(body: CameraIn) -> dict:
    """カメラを追加する (地図の「カメラ追加」フォームから使用)。"""
    if body.source_type not in SOURCE_TYPES:
        raise HTTPException(422, f"source_type は {SOURCE_TYPES} のいずれか")
    if body.source_type == "youtube":
        val = body.source_value.strip()
        # チャンネル ID (UC...24桁) なら channel 埋め込み、それ以外は video_id
        if val.startswith("UC") and len(val) == 24:
            source = {"type": "youtube", "video_id": "", "channel_id": val}
        else:
            source = {"type": "youtube", "video_id": val}
    else:
        source = {"type": body.source_type, "url": body.source_value.strip()}

    cam_id = f"user-{uuid.uuid4().hex[:8]}"
    entry = {
        "id": cam_id,
        "name": body.name,
        "lat": body.lat,
        "lon": body.lon,
        "heading_deg": body.heading_deg,
        "fov_deg": body.fov_deg,
        "elevation_ft": body.elevation_ft,
        "description": body.description,
        "source": source,
        "attribution": body.attribution,
        "page_url": body.page_url,
        "metar_station": body.metar_station.upper(),
    }
    try:
        cam = add_camera_entry(entry)
    except ValueError as e:
        raise HTTPException(409, str(e))
    cameras[cam_id] = cam
    return {"id": cam_id, "status": _status(cam)}


@app.delete("/api/cameras/{cam_id}")
def delete_camera(cam_id: str) -> dict:
    """カメラを削除する。"""
    if cam_id not in cameras:
        raise HTTPException(404, f"カメラ {cam_id} は存在しません")
    try:
        delete_camera_entry(cam_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    cameras.pop(cam_id, None)
    return {"deleted": cam_id}


class TargetIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    distance_km: float = Field(gt=0, le=500)
    bbox: list[int] = Field(min_length=4, max_length=4)
    elevation_ft_msl: Optional[float] = Field(default=None, ge=-100, le=30000)


class SetupIn(BaseModel):
    targets: list[TargetIn] = Field(max_length=30)
    sky_bbox: Optional[list[int]] = Field(default=None, min_length=4, max_length=4)


def _validate_bbox(bbox: list[int], label: str) -> None:
    x, y, w, h = bbox
    if x < 0 or y < 0 or w < 4 or h < 4:
        raise HTTPException(422, f"{label} の bbox が不正です: {bbox}")


@app.post("/api/cameras/{cam_id}/setup")
def save_setup(cam_id: str, setup: SetupIn) -> dict:
    """ターゲットと空領域の設定を保存する (ブラウザ上のエディタから使用)。"""
    cam = cameras.get(cam_id)
    if cam is None:
        raise HTTPException(404, f"カメラ {cam_id} は存在しません")
    for t in setup.targets:
        _validate_bbox(t.bbox, t.name)
    if setup.sky_bbox:
        _validate_bbox(setup.sky_bbox, "空領域")
    targets = [t.model_dump(exclude_none=True) for t in setup.targets]
    try:
        updated = save_camera_setup(cam_id, targets, setup.sky_bbox)
    except KeyError as e:
        raise HTTPException(404, str(e))
    cameras[cam_id] = updated
    return {"saved": True, "targets": len(targets), "status": _status(updated)}


@app.post("/api/cameras/{cam_id}/capture_reference")
def capture_reference(cam_id: str) -> dict:
    """現在の画像を晴天時の基準画像として保存する (url カメラの初期設定用)。

    快晴で遠方までよく見える日に実行すること。
    """
    cam = cameras.get(cam_id)
    if cam is None:
        raise HTTPException(404, f"カメラ {cam_id} は存在しません")
    if cam.source_type == "local" or not sources.image_capable(cam):
        raise HTTPException(400, "このカメラは基準画像の取得に対応していません")
    img = sources.current_image(cam)
    if img is None:
        raise HTTPException(502, f"現在画像を取得できません: {sources.last_error(cam_id)}")
    path = sources.save_reference(cam, img)
    return {"saved": path}


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
