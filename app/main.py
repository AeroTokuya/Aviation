"""HeliWX — ライブカメラ視程・シーリング推定 API サーバ。

起動: uvicorn app.main:app --reload
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analysis, history, metar, sources
from .config import CameraConfig, ROOT, load_cameras, save_camera_setup

app = FastAPI(title="HeliWX", description="ヘリコプター運航向け 視程・シーリング推定")

cameras = load_cameras()


def _status(cam: CameraConfig) -> str:
    """カメラの動作状態を返す。

    estimate        … 推定可能 (基準画像 + ターゲットあり)
    needs_targets   … 画像はあるがターゲット未設定 → 画像比較のみ
    needs_reference … 晴天時基準画像が未取得 → 現在画像のみ
    view            … 映像視聴のみ (フレーム取得不可)
    page            … 提供元ページへのリンクのみ
    """
    if cam.source_type == "page":
        return "page"
    if not sources.image_capable(cam):
        return "view"
    if sources.reference_image(cam) is None:
        return "needs_reference"
    if not cam.targets:
        return "needs_targets"
    return "estimate"


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
    """全カメラの位置・方角と最新推定サマリ (地図アイコン用)。"""
    out = []
    for cam in cameras.values():
        item = _camera_info(cam)
        item["status"] = _status(cam)
        item["summary"] = None
        try:
            est = _try_analyze(cam)
        except Exception:
            est = None
            item["status"] = "error"
        if est is not None:
            item["summary"] = {
                "visibility_km": est.visibility_km,
                "visibility_is_lower_bound": est.visibility_is_lower_bound,
                "ceiling_ft_agl": est.ceiling_ft_agl,
                "ceiling_is_unlimited": est.ceiling_is_unlimited,
                "flight_category": est.flight_category,
            }
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
    return {
        "camera": _camera_info(cam),
        "status": status,
        "has_reference": sources.reference_image(cam) is not None,
        "last_error": sources.last_error(cam_id),
        "metar": metar.get_metar(cam.metar_station),
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
