"""デモ用の合成カメラ画像と config/cameras.json を生成するスクリプト。

各カメラについて:
- 晴天時の基準画像 (data/reference/<id>.jpg)
- 現在画像 (data/current/<id>.jpg) — カメラごとに異なる気象条件

ランドマークの霞み方は Koschmieder の法則 t = exp(-3.912 d / V) に従って
正確に合成するため、解析側の推定値の妥当性検証にも使える。

実行: python3 scripts/generate_demo.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

W, H = 800, 450
HORIZON = 320
PX_PER_RAD = 2200.0  # 垂直方向の描画スケール (望遠気味の演出)
FT2M = 0.3048
K = 3.912

REF_VIS_KM = 150.0  # 基準画像の視程 (快晴の冬の日を想定)

SKY_TOP = np.array([190, 130, 60], dtype=np.float64)  # BGR
SKY_HORIZON = np.array([225, 210, 185], dtype=np.float64)
HAZE = np.array([215, 212, 205], dtype=np.float64)
FOG = np.array([228, 228, 226], dtype=np.float64)
CLOUD = np.array([185, 183, 180], dtype=np.float64)
GROUND = np.array([70, 95, 75], dtype=np.float64)


@dataclass
class Landmark:
    name: str
    kind: str  # "mountain" | "building" | "tower"
    distance_km: float
    top_elevation_ft: float  # 頂部標高 (MSL)
    x_center: int
    half_width: int
    color: tuple[float, float, float]  # BGR
    use_summit_bbox: bool = False  # シーリング用に頂部のみを ROI にする
    is_elevation_target: bool = False


@dataclass
class Weather:
    visibility_km: float
    cloud_base_ft_msl: Optional[float] = None  # None = 雲層なし
    cloud_cover: float = 0.1  # 0..1


@dataclass
class CameraSpec:
    id: str
    name: str
    lat: float
    lon: float
    heading_deg: float
    fov_deg: float
    elevation_ft: float
    description: str
    landmarks: list[Landmark]
    current_weather: Weather
    reference_weather: Weather = field(
        default_factory=lambda: Weather(visibility_km=REF_VIS_KM, cloud_cover=0.05)
    )


def apparent_top_row(spec: CameraSpec, lm: Landmark) -> int:
    """ランドマーク頂部の画面上の行 (仰角ベース)。"""
    dh_m = (lm.top_elevation_ft - spec.elevation_ft) * FT2M
    angle = np.arctan2(max(dh_m, 1.0), lm.distance_km * 1000.0)
    return int(round(HORIZON - angle * PX_PER_RAD))


def attenuation(distance_km: float, visibility_km: float) -> float:
    return float(np.exp(-K * distance_km / visibility_km))


def mix(c1: np.ndarray, c2: np.ndarray, a: float) -> np.ndarray:
    return c1 * (1.0 - a) + c2 * a


def horizon_sky_color(img: np.ndarray) -> np.ndarray:
    """地平線付近の空の実際の色。遠方の物体はこの色に向かって霞む。"""
    return img[HORIZON - 25 : HORIZON - 3, :].reshape(-1, 3).mean(axis=0)


def draw_sky(img: np.ndarray, weather: Weather, rng: np.random.Generator) -> None:
    for y in range(HORIZON):
        a = y / HORIZON
        img[y, :] = mix(SKY_TOP, SKY_HORIZON, a)
    # 視程が悪いと空も白っぽくなる
    milk = float(np.clip(8.0 / max(weather.visibility_km, 0.1), 0, 1)) * 0.85
    img[:HORIZON] = mix(img[:HORIZON], FOG[None, None, :], milk)

    cover = weather.cloud_cover
    if weather.cloud_base_ft_msl is not None or cover >= 0.9:
        # 全天曇り: 空全体を雲色 + なだらかなムラ
        noise = rng.normal(0, 12, (HORIZON, W)).astype(np.float64)
        noise = cv2.GaussianBlur(noise, (0, 0), 9)
        img[:HORIZON] = np.clip(CLOUD[None, None, :] + noise[:, :, None], 0, 255)
    elif cover > 0.02:
        # 部分的な雲: 楕円ブロブを被覆率に応じて描く
        area = 0.0
        tries = 0
        while area < cover and tries < 300:
            cx = int(rng.uniform(0, W))
            cy = int(rng.uniform(10, HORIZON * 0.6))
            ax = int(rng.uniform(40, 110))
            ay = int(rng.uniform(10, 25))
            cv2.ellipse(img, (cx, cy), (ax, ay), 0, 0, 360, (235, 233, 230), -1)
            area += (np.pi * ax * ay) / (W * HORIZON)
            tries += 1


def draw_ground(
    img: np.ndarray, weather: Weather, hz: np.ndarray, rng: np.random.Generator
) -> None:
    for y in range(HORIZON, H):
        frac = (H - y) / (H - HORIZON)  # 1=地平線際(遠い), 0=手前
        d_km = 0.05 + 10.0 * frac * frac
        a = 1.0 - attenuation(d_km, weather.visibility_km)
        base = GROUND * (0.75 + 0.25 * (1 - frac))
        img[y, :] = mix(base, hz, a)
    tex = rng.normal(0, 5, (H - HORIZON, W, 1))
    img[HORIZON:] = np.clip(img[HORIZON:] + tex, 0, 255)


def landmark_polygon(spec: CameraSpec, lm: Landmark) -> np.ndarray:
    top = apparent_top_row(spec, lm)
    x0, x1 = lm.x_center - lm.half_width, lm.x_center + lm.half_width
    if lm.kind == "mountain":
        return np.array(
            [
                [x0, HORIZON],
                [lm.x_center - lm.half_width // 3, top + (HORIZON - top) // 4],
                [lm.x_center, top],
                [lm.x_center + lm.half_width // 4, top + (HORIZON - top) // 5],
                [x1, HORIZON],
            ],
            dtype=np.int32,
        )
    if lm.kind == "building":
        return np.array(
            [[x0, HORIZON], [x0, top], [x1, top], [x1, HORIZON]], dtype=np.int32
        )
    # tower: 細い台形
    tw = max(lm.half_width // 4, 3)
    return np.array(
        [
            [lm.x_center - lm.half_width, HORIZON],
            [lm.x_center - tw, top],
            [lm.x_center + tw, top],
            [lm.x_center + lm.half_width, HORIZON],
        ],
        dtype=np.int32,
    )


def draw_landmark(
    img: np.ndarray,
    spec: CameraSpec,
    lm: Landmark,
    weather: Weather,
    hz: np.ndarray,
    rng: np.random.Generator,
) -> None:
    poly = landmark_polygon(spec, lm)
    top = int(poly[:, 1].min())
    t = attenuation(lm.distance_km, weather.visibility_km)
    color = mix(hz, np.array(lm.color, dtype=np.float64), t)

    layer = img.copy()
    cv2.fillPoly(layer, [poly], color.tolist())

    # 内部テクスチャ (尾根線・窓・骨組) — 減衰も同じ係数で
    mask = np.zeros((H, W), dtype=np.uint8)
    cv2.fillPoly(mask, [poly], 255)
    detail = layer.copy()
    # 岩肌/壁面の細かい起伏: 振幅は大気減衰と同じ係数で低下する
    speckle = rng.normal(0, 22, (H, W)).astype(np.float64)
    speckle = cv2.GaussianBlur(speckle, (0, 0), 1.2) * t
    detail += speckle[:, :, None]
    n_lines = 14 if lm.kind == "mountain" else 20
    for _ in range(n_lines):
        px = int(rng.uniform(poly[:, 0].min(), poly[:, 0].max()))
        py = int(rng.uniform(top, HORIZON))
        qx = px + int(rng.uniform(-18, 18))
        qy = py + int(rng.uniform(6, 30))
        shade = mix(hz, np.array(lm.color) * rng.uniform(0.45, 1.5), t)
        cv2.line(detail, (px, py), (qx, qy), np.clip(shade, 0, 255).tolist(), 2)
    if lm.kind == "mountain" and lm.top_elevation_ft > 9000:
        # 冠雪
        snow_poly = np.array(
            [
                [lm.x_center - lm.half_width // 4, top + (HORIZON - top) // 5],
                [lm.x_center, top],
                [lm.x_center + lm.half_width // 6, top + (HORIZON - top) // 6],
            ],
            dtype=np.int32,
        )
        snow = mix(hz, np.array([240, 240, 240], dtype=np.float64), t)
        cv2.fillPoly(detail, [snow_poly], snow.tolist())
    layer[mask > 0] = detail[mask > 0]

    # 雲層による頂部の遮蔽
    if weather.cloud_base_ft_msl is not None and (
        lm.top_elevation_ft > weather.cloud_base_ft_msl
    ):
        dh_m = (weather.cloud_base_ft_msl - spec.elevation_ft) * FT2M
        angle = np.arctan2(max(dh_m, 1.0), lm.distance_km * 1000.0)
        cloud_row = int(round(HORIZON - angle * PX_PER_RAD))
        rows = max(cloud_row, 1)
        noise = rng.normal(0, 12, (rows, W)).astype(np.float64)
        noise = cv2.GaussianBlur(noise, (0, 0), 9)
        layer[:rows, :] = np.clip(CLOUD[None, None, :] + noise[:, :, None], 0, 255)
        # 雲底の毛羽立ち
        for x in range(0, W, 4):
            drop = int(abs(rng.normal(0, 4)))
            cv2.line(
                layer,
                (x, cloud_row),
                (x, cloud_row + drop),
                CLOUD.tolist(),
                4,
            )

    img[mask > 0] = layer[mask > 0]


def bbox_for(spec: CameraSpec, lm: Landmark) -> tuple[int, int, int, int]:
    poly = landmark_polygon(spec, lm)
    x0 = int(poly[:, 0].min())
    y0 = int(poly[:, 1].min())
    w = int(poly[:, 0].max()) - x0
    h = HORIZON - y0
    if lm.use_summit_bbox:
        # 頂部 35% のみ: 雲に入ると真っ先に見えなくなる領域
        sh = max(int(h * 0.35), 14)
        sw = max(int(w * 0.5), 20)
        return (lm.x_center - sw // 2, y0, sw, sh)
    return (x0, y0, w, h)


def sky_bbox_for(spec: CameraSpec) -> tuple[int, int, int, int]:
    min_top = min(apparent_top_row(spec, lm) for lm in spec.landmarks)
    return (10, 6, W - 20, max(min_top - 14, 40))


def render(spec: CameraSpec, weather: Weather, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros((H, W, 3), dtype=np.float64)
    draw_sky(img, weather, rng)
    hz = horizon_sky_color(img)
    draw_ground(img, weather, hz, rng)
    for lm in sorted(spec.landmarks, key=lambda l: -l.distance_km):
        draw_landmark(img, spec, lm, weather, hz, rng)
    # センサーノイズ
    img = np.clip(img + rng.normal(0, 1.2, img.shape), 0, 255)
    return img.astype(np.uint8)


# ---------------------------------------------------------------- カメラ定義

CAMERAS: list[CameraSpec] = [
    CameraSpec(
        id="tokyo-heliport-west",
        name="東京ヘリポート 西向きカメラ",
        lat=35.6367,
        lon=139.8395,
        heading_deg=270,
        fov_deg=60,
        elevation_ft=20,
        description="新木場・東京ヘリポートから都心〜富士山方向。デモ気象: 快晴。",
        landmarks=[
            Landmark("富士山", "mountain", 95, 12388, 150, 70, (110, 90, 80),
                     use_summit_bbox=False, is_elevation_target=True),
            Landmark("丹沢山地", "mountain", 55, 4100, 400, 90, (120, 105, 70),
                     is_elevation_target=True),
            Landmark("新宿ビル群", "building", 12, 820, 600, 55, (110, 95, 90),
                     is_elevation_target=True),
            Landmark("運河対岸クレーン", "tower", 2, 270, 730, 22, (60, 60, 130)),
        ],
        current_weather=Weather(visibility_km=100, cloud_cover=0.1),
    ),
    CameraSpec(
        id="yokohama-mm-southwest",
        name="横浜みなとみらい 南西向きカメラ",
        lat=35.4553,
        lon=139.6350,
        heading_deg=225,
        fov_deg=60,
        elevation_ft=100,
        description="みなとみらいから大山・富士山方向。デモ気象: 靄 (視程約6km)。",
        landmarks=[
            Landmark("富士山", "mountain", 80, 12388, 130, 65, (110, 90, 80),
                     is_elevation_target=True),
            Landmark("大山", "mountain", 40, 4100, 350, 75, (115, 100, 70),
                     is_elevation_target=True),
            Landmark("ベイブリッジ主塔", "tower", 4, 610, 560, 30, (140, 140, 145),
                     is_elevation_target=True),
            Landmark("港湾クレーン", "tower", 1, 400, 700, 26, (60, 60, 140)),
        ],
        current_weather=Weather(visibility_km=6, cloud_cover=0.45),
    ),
    CameraSpec(
        id="kawasaki-coast-east",
        name="川崎臨海部 東向きカメラ",
        lat=35.5065,
        lon=139.7530,
        heading_deg=90,
        fov_deg=60,
        elevation_ft=30,
        description="川崎臨海部から東京湾方向。デモ気象: 濃霧 (視程約1.2km)。",
        landmarks=[
            Landmark("風の塔", "tower", 10, 330, 170, 30, (150, 120, 100),
                     is_elevation_target=True),
            Landmark("製鉄所クレーン群", "building", 3, 430, 380, 70, (90, 85, 95),
                     is_elevation_target=True),
            Landmark("プラント煙突", "tower", 1.5, 680, 600, 24, (100, 100, 110),
                     is_elevation_target=True),
            Landmark("隣接倉庫", "building", 0.4, 130, 740, 48, (80, 105, 120)),
        ],
        current_weather=Weather(visibility_km=1.2, cloud_cover=1.0),
    ),
    CameraSpec(
        id="atsugi-north",
        name="厚木 北西向きカメラ",
        lat=35.4520,
        lon=139.3600,
        heading_deg=315,
        fov_deg=60,
        elevation_ft=200,
        description="厚木から丹沢・富士山方向。デモ気象: 曇天・雲底約2,600ft AGL。",
        landmarks=[
            Landmark("富士山", "mountain", 45, 12388, 140, 85, (110, 90, 80),
                     use_summit_bbox=True, is_elevation_target=True),
            Landmark("大山", "mountain", 18, 4100, 360, 85, (115, 100, 70),
                     use_summit_bbox=True, is_elevation_target=True),
            Landmark("送電鉄塔 (尾根上)", "tower", 6, 1900, 545, 24, (90, 90, 100),
                     use_summit_bbox=True, is_elevation_target=True),
            Landmark("丘陵地", "mountain", 8, 950, 640, 70, (100, 120, 80),
                     is_elevation_target=True),
            Landmark("市街ビル", "building", 3, 450, 745, 40, (110, 95, 90),
                     is_elevation_target=True),
        ],
        current_weather=Weather(
            visibility_km=30, cloud_base_ft_msl=2800, cloud_cover=1.0
        ),
    ),
]


def main() -> None:
    ref_dir = ROOT / "data" / "reference"
    cur_dir = ROOT / "data" / "current"
    ref_dir.mkdir(parents=True, exist_ok=True)
    cur_dir.mkdir(parents=True, exist_ok=True)

    # 既存設定を読み込み、デモカメラのみ差し替える (手動登録した実カメラは保持)
    cfg_path = ROOT / "config" / "cameras.json"
    existing: list[dict] = []
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            existing = json.load(f).get("cameras", [])
    demo_ids = {spec.id for spec in CAMERAS}
    config = {"cameras": [c for c in existing if c["id"] not in demo_ids]}

    for i, spec in enumerate(CAMERAS):
        ref = render(spec, spec.reference_weather, seed=1000 + i)
        cur = render(spec, spec.current_weather, seed=1000 + i)  # 同一シード=同一シーン
        cv2.imwrite(str(ref_dir / f"{spec.id}.jpg"), ref, [cv2.IMWRITE_JPEG_QUALITY, 92])
        cv2.imwrite(str(cur_dir / f"{spec.id}.jpg"), cur, [cv2.IMWRITE_JPEG_QUALITY, 92])

        targets = []
        for lm in spec.landmarks:
            t = {
                "name": lm.name,
                "distance_km": lm.distance_km,
                "bbox": list(bbox_for(spec, lm)),
            }
            if lm.is_elevation_target:
                t["elevation_ft_msl"] = lm.top_elevation_ft
            targets.append(t)

        config["cameras"].append(
            {
                "id": spec.id,
                "name": f"【デモ】{spec.name}",
                "lat": spec.lat,
                "lon": spec.lon,
                "heading_deg": spec.heading_deg,
                "fov_deg": spec.fov_deg,
                "elevation_ft": spec.elevation_ft,
                "description": spec.description,
                "reference_image": f"data/reference/{spec.id}.jpg",
                "source": {"type": "local", "path": f"data/current/{spec.id}.jpg"},
                "sky_bbox": list(sky_bbox_for(spec)),
                "targets": targets,
            }
        )
        print(f"generated: {spec.id} (現況視程 {spec.current_weather.visibility_km}km)")

    cfg_path.parent.mkdir(exist_ok=True)
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"wrote: {cfg_path}")


if __name__ == "__main__":
    main()
