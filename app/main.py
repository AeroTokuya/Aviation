"""HeliWX — ライブカメラ視程・シーリング推定 API サーバ。

起動: uvicorn app.main:app --reload
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles

from . import analysis, sources
from .config import ROOT, load_cameras

app = FastAPI(title="HeliWX", description="ヘリコプター運航向け 視程・シーリング推定")

cameras = load_cameras()


def _analyze(cam_id: str) -> analysis.Estimate:
    cam = cameras.get(cam_id)
    if cam is None:
        raise HTTPException(404, f"カメラ {cam_id} は存在しません")
    ref = sources.reference_image(cam)
    cur = sources.current_image(cam)
    if ref is None:
        raise HTTPException(500, "基準画像を読み込めません")
    if cur is None:
        raise HTTPException(502, "現在画像を取得できません")
    return analysis.analyze(
        ref, cur, cam.targets, camera_elevation_ft=cam.elevation_ft, sky_bbox=cam.sky_bbox
    )


@app.get("/api/cameras")
def list_cameras() -> list[dict]:
    """全カメラの位置・方角と最新推定サマリ (地図アイコン用)。"""
    out = []
    for cam in cameras.values():
        item = {
            "id": cam.id,
            "name": cam.name,
            "lat": cam.lat,
            "lon": cam.lon,
            "heading_deg": cam.heading_deg,
            "fov_deg": cam.fov_deg,
            "description": cam.description,
        }
        try:
            est = _analyze(cam.id)
            item["summary"] = {
                "visibility_km": est.visibility_km,
                "visibility_is_lower_bound": est.visibility_is_lower_bound,
                "ceiling_ft_agl": est.ceiling_ft_agl,
                "ceiling_is_unlimited": est.ceiling_is_unlimited,
                "flight_category": est.flight_category,
            }
        except HTTPException:
            item["summary"] = None
        out.append(item)
    return out


@app.get("/api/cameras/{cam_id}/estimate")
def camera_estimate(cam_id: str) -> dict:
    cam = cameras.get(cam_id)
    if cam is None:
        raise HTTPException(404, f"カメラ {cam_id} は存在しません")
    est = _analyze(cam_id)
    return {
        "camera": {
            "id": cam.id,
            "name": cam.name,
            "lat": cam.lat,
            "lon": cam.lon,
            "heading_deg": cam.heading_deg,
            "fov_deg": cam.fov_deg,
            "elevation_ft": cam.elevation_ft,
            "description": cam.description,
        },
        "estimate": asdict(est),
    }


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


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
