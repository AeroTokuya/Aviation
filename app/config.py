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
    # ソース種別 (詳細は app/sources.py 冒頭のコメント参照):
    #   local / url / mjpeg / stream / youtube / page_image / page
    source_type: str
    source: str  # local: パス / youtube: video_id / それ以外: URL
    youtube_channel_id: str = ""  # youtube で channel 埋め込みを使う場合
    source_raw: dict = field(default_factory=dict)  # source 設定の生辞書 (headers 等)
    reference_image: str = ""  # 晴天時基準画像のパス (image 系のみ)
    targets: list[Target] = field(default_factory=list)
    sky_bbox: Optional[tuple[int, int, int, int]] = None
    description: str = ""
    attribution: str = ""  # 映像提供元の表記
    page_url: str = ""  # 提供元ページ (出典リンク)

    @staticmethod
    def from_dict(d: dict) -> "CameraConfig":
        src = d["source"]
        stype = src["type"]
        source = (
            src.get("path")
            or src.get("url")
            or src.get("video_id")
            or ""
        )
        ref = d.get("reference_image", "")
        if not ref and stype != "page":
            ref = f"data/reference/{d['id']}.jpg"
        return CameraConfig(
            id=d["id"],
            name=d["name"],
            lat=float(d["lat"]),
            lon=float(d["lon"]),
            heading_deg=float(d["heading_deg"]),
            fov_deg=float(d.get("fov_deg", 60)),
            elevation_ft=float(d.get("elevation_ft", 0)),
            source_type=stype,
            source=source,
            youtube_channel_id=src.get("channel_id", ""),
            source_raw=src,
            reference_image=ref,
            targets=[Target.from_dict(t) for t in d.get("targets", [])],
            sky_bbox=(
                tuple(int(v) for v in d["sky_bbox"]) if d.get("sky_bbox") else None
            ),
            description=d.get("description", ""),
            attribution=d.get("attribution", ""),
            page_url=d.get("page_url", ""),
        )


def load_cameras(path: Path = CONFIG_PATH) -> dict[str, CameraConfig]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    cameras = [CameraConfig.from_dict(c) for c in raw["cameras"]]
    return {c.id: c for c in cameras}
