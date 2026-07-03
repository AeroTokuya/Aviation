"""視程・シーリング推定モジュール。

晴天時の基準画像と現在画像を比較し、以下を推定する:

- 視程: 既知距離のランドマーク(ターゲット)ごとにコントラスト減衰比を測り、
  Koschmieder の法則 V = 3.912 * d / (-ln r) から気象学的視程を算出する。
- シーリング: 既知標高のターゲットのうち「雲に隠されている」ものと
  「見えている」ものの境界から雲底高度を推定する。
- 雲量: 空領域の色解析で雲被覆率を求め、FEW/SCT/BKN/OVC に分類する。

注意: 本推定は参考情報であり、公式な気象観測(METAR 等)に代わるものではない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

# Koschmieder 定数 (コントラスト閾値 0.02 に対応)
KOSCHMIEDER_K = 3.912

# ターゲットのコントラスト比がこの値以上なら「明瞭に視認できる」
RATIO_CLEAR = 0.85
# この値未満なら「完全に隠されている」
RATIO_OBSCURED = 0.15

# 視程の報告上限 (km)。これを超える推定は「上限以上」として報告する
VISIBILITY_CAP_KM = 50.0

# 雲量 (8分量) → 記号
OKTA_LABELS = [
    (0, "SKC"),
    (2, "FEW"),
    (4, "SCT"),
    (7, "BKN"),
    (8, "OVC"),
]


@dataclass
class Target:
    """基準画像内のランドマーク。bbox は (x, y, w, h) ピクセル。"""

    name: str
    distance_km: float
    bbox: tuple[int, int, int, int]
    elevation_ft_msl: Optional[float] = None  # 頂部の標高。None なら地上目標

    @staticmethod
    def from_dict(d: dict) -> "Target":
        return Target(
            name=d["name"],
            distance_km=float(d["distance_km"]),
            bbox=tuple(int(v) for v in d["bbox"]),
            elevation_ft_msl=(
                float(d["elevation_ft_msl"])
                if d.get("elevation_ft_msl") is not None
                else None
            ),
        )


@dataclass
class TargetResult:
    name: str
    distance_km: float
    elevation_ft_msl: Optional[float]
    contrast_ratio: float
    visible: bool
    visibility_estimate_km: Optional[float]  # このターゲット単独からの推定値


@dataclass
class Estimate:
    visibility_km: Optional[float]
    visibility_is_lower_bound: bool  # True なら「〜以上」
    ceiling_ft_agl: Optional[float]  # None = シーリングなし/不明
    ceiling_is_unlimited: bool
    cloud_cover_oktas: Optional[int]
    cloud_cover_label: Optional[str]
    flight_category: str  # VFR / MVFR / IFR / LIFR / UNKNOWN
    targets: list[TargetResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float64)
    return img.astype(np.float64)


def _roi(img: np.ndarray, bbox: tuple[int, int, int, int]) -> np.ndarray:
    x, y, w, h = bbox
    return img[y : y + h, x : x + w]


def _gradient_magnitude(gray_roi: np.ndarray) -> np.ndarray:
    blur = cv2.GaussianBlur(gray_roi, (3, 3), 0)
    gx = cv2.Sobel(blur, cv2.CV_64F, 1, 0, ksize=3)
    gy = cv2.Sobel(blur, cv2.CV_64F, 0, 1, ksize=3)
    return np.sqrt(gx * gx + gy * gy)


def contrast_metric(gray_roi: np.ndarray, exposure_norm: float) -> float:
    """露出正規化した勾配強度によるコントラスト指標。

    exposure_norm には画像全体の平均輝度を渡す。カメラの自動露出による
    明るさ変動の影響を打ち消し、大気減衰による勾配低下だけを拾う。
    """
    if gray_roi.size == 0:
        return 0.0
    grad = _gradient_magnitude(gray_roi)
    return float(np.sqrt(np.mean(grad * grad))) / max(exposure_norm, 1.0)


def noise_floor_metric(gray_roi: np.ndarray, exposure_norm: float) -> float:
    """空領域からノイズ床を推定する (中央値ベース)。

    空はほぼ一様なため、勾配の中央値はセンサ/圧縮ノイズを表す。
    雲の輪郭のような疎な強エッジには中央値なので影響されにくい。
    係数 1.2 はガウスノイズの勾配 (レイリー分布) の RMS/中央値比。
    """
    if gray_roi.size == 0:
        return 0.0
    grad = _gradient_magnitude(gray_roi)
    return 1.2 * float(np.median(grad)) / max(exposure_norm, 1.0)


def target_contrast_ratio(
    reference: np.ndarray,
    current: np.ndarray,
    bbox: tuple[int, int, int, int],
    sky_bbox: Optional[tuple[int, int, int, int]] = None,
) -> float:
    """基準画像に対する現在画像のコントラスト比 (0..1 に clip)。

    sky_bbox が与えられた場合、一様なはずの空領域の勾配をノイズ床として
    各 ROI の指標から二乗差で差し引く。完全に霞んだターゲットの ROI は
    空/霧と同質になるため、比がきちんと 0 に落ちる。
    """

    def adjusted_metric(gray: np.ndarray) -> float:
        norm = float(np.mean(gray))
        m = contrast_metric(_roi(gray, bbox), norm)
        if sky_bbox is None:
            return m
        noise = noise_floor_metric(_roi(gray, sky_bbox), norm)
        return float(np.sqrt(max(m * m - noise * noise, 0.0)))

    ref_m = adjusted_metric(_to_gray(reference))
    cur_m = adjusted_metric(_to_gray(current))
    if ref_m <= 1e-9:
        return 1.0  # 基準に構造がない → 判定不能なので減衰なし扱い
    return float(np.clip(cur_m / ref_m, 0.0, 1.0))


def koschmieder_visibility(distance_km: float, ratio: float) -> Optional[float]:
    """コントラスト比から視程を推定。比が極端なときは None。"""
    if ratio >= 0.999 or ratio <= 1e-4:
        return None
    return KOSCHMIEDER_K * distance_km / (-np.log(ratio))


def estimate_visibility(
    targets: list[Target], ratios: list[float]
) -> tuple[Optional[float], bool, list[TargetResult]]:
    """全ターゲットの結果から視程を統合推定する。

    戻り値: (視程 km, 下限値フラグ, ターゲット別結果)
    """
    results: list[TargetResult] = []
    per_target_estimates: list[float] = []
    max_clear_dist = 0.0
    min_obscured_dist: Optional[float] = None

    for t, r in zip(targets, ratios):
        visible = r >= RATIO_OBSCURED
        est = None
        if RATIO_OBSCURED <= r <= RATIO_CLEAR:
            est = koschmieder_visibility(t.distance_km, r)
            if est is not None:
                per_target_estimates.append(est)
        results.append(
            TargetResult(
                name=t.name,
                distance_km=t.distance_km,
                elevation_ft_msl=t.elevation_ft_msl,
                contrast_ratio=round(r, 3),
                visible=visible,
                visibility_estimate_km=(round(est, 1) if est else None),
            )
        )
        if r > RATIO_CLEAR:
            max_clear_dist = max(max_clear_dist, t.distance_km)
        if r < RATIO_OBSCURED:
            if min_obscured_dist is None or t.distance_km < min_obscured_dist:
                min_obscured_dist = t.distance_km

    if per_target_estimates:
        vis = float(np.median(per_target_estimates))
        # 明瞭に見えている最遠ターゲットより短くはならない
        vis = max(vis, max_clear_dist)
        if vis > VISIBILITY_CAP_KM:
            return VISIBILITY_CAP_KM, True, results
        return round(vis, 1), False, results

    if min_obscured_dist is None:
        # 全ターゲット明瞭 → 最遠ターゲット距離を下限として報告
        if max_clear_dist > 0:
            return round(max_clear_dist, 1), True, results
        return None, False, results

    if max_clear_dist > 0:
        # 中間の減衰ターゲットがない → 見えている最遠と隠れている最近の間
        vis = (max_clear_dist + min_obscured_dist) / 2.0
        return round(vis, 1), False, results

    # 全ターゲットが隠れている → 最近ターゲット距離の半分を上限的推定とする
    return round(min_obscured_dist / 2.0, 1), False, results


def estimate_ceiling(
    results: list[TargetResult],
    camera_elevation_ft: float,
    visibility_km: Optional[float],
    visibility_is_lower_bound: bool,
) -> tuple[Optional[float], bool, list[str]]:
    """標高付きターゲットの可視/遮蔽から雲底 (AGL ft) を推定する。

    霧・靄による遮蔽と雲による遮蔽を区別するため、推定視程から
    Koschmieder 則で予測されるコントラスト比 r_pred と実測比を比較し、
    「霞だけなら見えるはずなのに実測が大幅に低い」ターゲットのみ
    雲遮蔽とみなす。
    """
    notes: list[str] = []
    elev_targets = [r for r in results if r.elevation_ft_msl is not None]
    if not elev_targets:
        return None, False, ["標高付きターゲットが未設定のためシーリング推定不可"]

    def cloud_obscured(r: TargetResult) -> bool:
        if r.visible:
            return False
        if visibility_km is None:
            return True
        r_pred = float(np.exp(-KOSCHMIEDER_K * r.distance_km / visibility_km))
        # 霞だけでもほぼ見えない距離なら判定不能 → 雲遮蔽とはみなさない
        if r_pred <= 0.02:
            return False
        return r.contrast_ratio < 0.4 * r_pred

    visible_elevs = [r.elevation_ft_msl for r in elev_targets if r.visible]
    obscured_elevs = [r.elevation_ft_msl for r in elev_targets if cloud_obscured(r)]

    if not obscured_elevs:
        max_elev = max(r.elevation_ft_msl for r in elev_targets)
        notes.append(
            f"標高 {max_elev:.0f}ft MSL まで雲遮蔽なし (それ以上は判定不能)"
        )
        return None, True, notes

    lowest_obscured = min(obscured_elevs)
    visible_below = [e for e in visible_elevs if e < lowest_obscured]
    if visible_below:
        base_msl = (max(visible_below) + lowest_obscured) / 2.0
    else:
        base_msl = lowest_obscured * 0.75 + camera_elevation_ft * 0.25

    ceiling_agl = max(base_msl - camera_elevation_ft, 0.0)
    return round(ceiling_agl / 50) * 50, False, notes


def estimate_cloud_cover(
    current: np.ndarray, sky_bbox: Optional[tuple[int, int, int, int]]
) -> tuple[Optional[int], Optional[str]]:
    """空領域の色から雲量 (oktas) を推定する。

    彩度が低い(白/灰色)ピクセルを雲、青みが強いピクセルを晴天とみなす。
    """
    if sky_bbox is None:
        return None, None
    roi = _roi(current, sky_bbox)
    if roi.size == 0 or roi.ndim != 3:
        return None, None
    hsv = cv2.cvtColor(roi.astype(np.uint8), cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1].astype(np.float64) / 255.0
    val = hsv[:, :, 2].astype(np.float64) / 255.0
    cloud_mask = (sat < 0.25) & (val > 0.25)
    fraction = float(np.mean(cloud_mask))
    oktas = int(round(fraction * 8))
    label = "OVC"
    for upper, name in OKTA_LABELS:
        if oktas <= upper:
            label = name
            break
    return oktas, label


def flight_category(
    visibility_km: Optional[float],
    ceiling_ft_agl: Optional[float],
    ceiling_is_unlimited: bool,
) -> str:
    """視程とシーリングからフライトカテゴリを判定 (US 基準を km 換算)。

    VFR: 視程 > 8km かつ シーリング > 3000ft
    MVFR: 視程 5–8km または シーリング 1000–3000ft
    IFR: 視程 1.6–5km または シーリング 500–1000ft
    LIFR: 視程 < 1.6km または シーリング < 500ft
    """
    if visibility_km is None:
        return "UNKNOWN"
    ceil = None if ceiling_is_unlimited else ceiling_ft_agl

    def vis_cat(v: float) -> int:
        if v > 8.0:
            return 3
        if v >= 5.0:
            return 2
        if v >= 1.6:
            return 1
        return 0

    def ceil_cat(c: Optional[float]) -> int:
        if c is None:
            return 3
        if c > 3000:
            return 3
        if c >= 1000:
            return 2
        if c >= 500:
            return 1
        return 0

    return ["LIFR", "IFR", "MVFR", "VFR"][min(vis_cat(visibility_km), ceil_cat(ceil))]


def analyze(
    reference: np.ndarray,
    current: np.ndarray,
    targets: list[Target],
    camera_elevation_ft: float = 0.0,
    sky_bbox: Optional[tuple[int, int, int, int]] = None,
) -> Estimate:
    """基準画像・現在画像・ターゲット定義から総合推定を行う。"""
    if reference.shape[:2] != current.shape[:2]:
        current = cv2.resize(current, (reference.shape[1], reference.shape[0]))

    ordered = sorted(targets, key=lambda t: t.distance_km)
    ratios = [
        target_contrast_ratio(reference, current, t.bbox, sky_bbox) for t in ordered
    ]

    vis_km, vis_lb, results = estimate_visibility(ordered, ratios)
    ceiling, unlimited, notes = estimate_ceiling(
        results, camera_elevation_ft, vis_km, vis_lb
    )
    oktas, cover_label = estimate_cloud_cover(current, sky_bbox)

    # 雲量が BKN 未満なら定義上シーリングは存在しない
    if ceiling is not None and oktas is not None and oktas < 5:
        notes.append(
            f"雲量 {cover_label} (BKN 未満) のため正式なシーリングには該当しない可能性"
        )

    return Estimate(
        visibility_km=vis_km,
        visibility_is_lower_bound=vis_lb,
        ceiling_ft_agl=ceiling,
        ceiling_is_unlimited=unlimited,
        cloud_cover_oktas=oktas,
        cloud_cover_label=cover_label,
        flight_category=flight_category(vis_km, ceiling, unlimited),
        targets=results,
        notes=notes,
    )
