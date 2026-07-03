"""カメラ設定 (config/cameras.json) の読み込み。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .analysis import Target

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cameras.json"


@dataclass
class CameraConfig:
    id: str
    name: str
    lat: float
    lon: float
    heading_deg: float  # 撮影方位 (真北基準・時計回り)
    fov_deg: float  # 水平画角
    elevation_ft: float  # カメラ設置標高 (MSL)
    reference_image: str  # 晴天時基準画像のパス (リポジトリ相対)
    source_type: str  # "local" | "url"
    source: str  # ローカルパス or ライブカメラ画像 URL
    targets: list[Target] = field(default_factory=list)
    sky_bbox: Optional[tuple[int, int, int, int]] = None
    description: str = ""

    @staticmethod
    def from_dict(d: dict) -> "CameraConfig":
        return CameraConfig(
            id=d["id"],
            name=d["name"],
            lat=float(d["lat"]),
            lon=float(d["lon"]),
            heading_deg=float(d["heading_deg"]),
            fov_deg=float(d.get("fov_deg", 60)),
            elevation_ft=float(d.get("elevation_ft", 0)),
            reference_image=d["reference_image"],
            source_type=d["source"]["type"],
            source=d["source"].get("path") or d["source"].get("url") or "",
            targets=[Target.from_dict(t) for t in d.get("targets", [])],
            sky_bbox=(
                tuple(int(v) for v in d["sky_bbox"]) if d.get("sky_bbox") else None
            ),
            description=d.get("description", ""),
        )


def load_cameras(path: Path = CONFIG_PATH) -> dict[str, CameraConfig]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    cameras = [CameraConfig.from_dict(c) for c in raw["cameras"]]
    return {c.id: c for c in cameras}
