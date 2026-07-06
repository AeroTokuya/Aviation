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
    metar_station: str = ""  # 最寄り METAR 観測局 (ICAO 4 文字。例: RJTT)

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
            metar_station=d.get("metar_station", ""),
        )


def load_cameras(path: Path = CONFIG_PATH) -> dict[str, CameraConfig]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    cameras = [CameraConfig.from_dict(c) for c in raw["cameras"]]
    return {c.id: c for c in cameras}


def _write_config(raw: dict, path: Path) -> None:
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def add_camera_entry(entry: dict, path: Path = CONFIG_PATH) -> CameraConfig:
    """カメラを設定ファイルに追加する。id 重複はエラー。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if any(c["id"] == entry["id"] for c in raw["cameras"]):
        raise ValueError(f"カメラ ID {entry['id']} は既に存在します")
    raw["cameras"].append(entry)
    _write_config(raw, path)
    return CameraConfig.from_dict(entry)


def delete_camera_entry(cam_id: str, path: Path = CONFIG_PATH) -> None:
    """カメラを設定ファイルから削除する。存在しなければ KeyError。"""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    before = len(raw["cameras"])
    raw["cameras"] = [c for c in raw["cameras"] if c["id"] != cam_id]
    if len(raw["cameras"]) == before:
        raise KeyError(f"カメラ {cam_id} は設定に存在しません")
    _write_config(raw, path)


def save_camera_setup(
    cam_id: str,
    targets: list[dict],
    sky_bbox: Optional[list[int]],
    path: Path = CONFIG_PATH,
) -> CameraConfig:
    """カメラのターゲットと空領域を config ファイルに永続化する。

    他のフィールドは一切変更しない。更新後の CameraConfig を返す。
    """
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    for entry in raw["cameras"]:
        if entry["id"] == cam_id:
            entry["targets"] = targets
            if sky_bbox:
                entry["sky_bbox"] = sky_bbox
            else:
                entry.pop("sky_bbox", None)
            break
    else:
        raise KeyError(f"カメラ {cam_id} は設定に存在しません")
    _write_config(raw, path)
    return CameraConfig.from_dict(entry)
