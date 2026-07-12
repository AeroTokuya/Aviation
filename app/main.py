"""HeliWX — ライブカメラ視程・シーリング推定 API サーバ。

起動: uvicorn app.main:app --reload
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles

from . import analysis, sources
from .config import CameraConfig, ROOT, load_cameras

app = FastAPI(title="HeliWX", description="ヘリコプター運航向け 視程・シーリング推定")

cameras = load_cameras()


def _status(cam: CameraConfig) -> str:
    """カメラの動作状態を返す。

    estimate        … 推定可能 (基準画像 + ターゲットあり)
    needs_targets   … 画像はあるがターゲット未設定 → 画像比較のみ
    needs_reference … 晴天時基準画像が未取得 → 現在画像のみ
    view            … YouTube 等の映像視聴のみ
    page            … 提供元ページへのリンクのみ
    """
    if cam.source_type == "youtube":
        return "view"
    if cam.source_type == "page":
        return "page"
    if sources.reference_image(cam) is None:
        return "needs_reference"
    if not cam.targets:
        return "needs_targets"
    return "estimate"


def _get_camera(cam_id: str) -> CameraConfig:
    cam = cameras.get(cam_id)
    if cam is None:
        raise HTTPException(404, f"カメラ {cam_id} は存在しません")
    return cam


def _try_analyze(cam: CameraConfig) -> Optional[analysis.Estimate]:
    """推定可能なカメラなら推定を実行。不可能/失敗なら None。"""
    if _status(cam) != "estimate":
        return None
    ref = sources.reference_image(cam)
    cur = sources.current_image(cam)
    if ref is None or cur is None:
        return None
    return analysis.analyze(
        ref, cur, cam.targets, camera_elevation_ft=cam.elevation_ft, sky_bbox=cam.sky_bbox
    )


def _analyze_with_status(cam: CameraConfig) -> tuple[str, Optional[analysis.Estimate]]:
    """カメラの状態と推定結果を返す。推定が例外を出した場合は status="error"。"""
    status = _status(cam)
    if status != "estimate":
        return status, None
    try:
        return status, _try_analyze(cam)
    except Exception:
        return "error", None


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
    }


@app.get("/api/cameras")
def list_cameras() -> list[dict]:
    """全カメラの位置・方角と最新推定サマリ (地図アイコン用)。"""
    out = []
    for cam in cameras.values():
        item = _camera_info(cam)
        status, est = _analyze_with_status(cam)
        item["status"] = status
        item["summary"] = None
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
    cam = _get_camera(cam_id)
    status, est = _analyze_with_status(cam)
    return {
        "camera": _camera_info(cam),
        "status": status,
        "has_reference": sources.reference_image(cam) is not None,
        "estimate": asdict(est) if est is not None else None,
    }


@app.get("/api/cameras/{cam_id}/image/{kind}")
def camera_image(cam_id: str, kind: str) -> Response:
    cam = _get_camera(cam_id)
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


@app.post("/api/cameras/{cam_id}/capture_reference")
def capture_reference(cam_id: str) -> dict:
    """現在の画像を晴天時の基準画像として保存する (url カメラの初期設定用)。

    快晴で遠方までよく見える日に実行すること。
    """
    cam = _get_camera(cam_id)
    if cam.source_type != "url":
        raise HTTPException(400, "基準画像の取得は url 型カメラのみ対応です")
    img = sources.current_image(cam)
    if img is None:
        raise HTTPException(502, "現在画像を取得できません")
    path = sources.save_reference(cam, img)
    return {"saved": path}


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
