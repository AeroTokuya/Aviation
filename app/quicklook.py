"""セットアップ不要の簡易天候評価。

ターゲット未登録のカメラでも「今の見え方」を即座に評価するための 2 段構え:

1. 単一画像評価 — エッジ密度・彩度・明るさから霧・霞の兆候スコア (0..1)
   を計算し、定性クラスに分類する。基準画像もターゲットも不要。
2. 自動基準比較 — 運用中に「これまでで最も澄んだフレーム」を自動保存し
   (自動基準画像)、画像全体をグリッド分割したコントラスト比から
   鮮明度 (晴天比 %) を計算する。時間が経つほど精度が上がる。

これらは距離既知ターゲットによる視程・シーリング推定 (analysis.py) の
代替ではなく、セットアップ前でも地図が機能するための参考評価。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Optional

import cv2
import numpy as np

from .analysis import (
    _gradient_magnitude,
    _to_gray,
    is_low_light,
    noise_floor_metric,
)
from .config import ROOT, CameraConfig

AUTO_REF_DIR = ROOT / "data" / "auto_reference"

# 自動基準画像の更新間隔 (秒)。頻繁なディスク書き込みを避ける
AUTO_REF_CHECK_INTERVAL_SEC = 600
# 既存の自動基準よりこの倍率以上澄んでいるときだけ置き換える
AUTO_REF_IMPROVE_FACTOR = 1.05
# 自動基準自体の霧指数がこの値以上なら、まだ「晴天基準」として信用せず
# グリッド比較を行わない (霧の日に始めた直後の誤評価を防ぐ)
AUTO_REF_TRUST_FOG_MAX = 0.50

# 定性クラス: (しきい値上限, level, 表示ラベル)
FOG_CLASSES = [
    (0.30, "good", "視界良好"),
    (0.50, "slight", "おおむね良好"),
    (0.70, "haze", "視界低下の兆候 (霞・靄・曇天)"),
    (9.99, "fog", "視界不良の可能性 (霧など)"),
]

# 鮮明度 (晴天比 %) → 定性クラス
CLARITY_CLASSES = [
    (70.0, "good", "視界良好 (晴天比 {pct:.0f}%)"),
    (45.0, "slight", "やや低下 (晴天比 {pct:.0f}%)"),
    (20.0, "haze", "視界低下 (晴天比 {pct:.0f}%)"),
    (-1.0, "fog", "視界不良 (晴天比 {pct:.0f}%)"),
]

_last_ref_check: dict[str, float] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- 指標


def _image_features(img: np.ndarray) -> dict:
    gray = _to_gray(img)
    mean_lum = max(float(np.mean(gray)), 1.0)
    grad = _gradient_magnitude(gray)
    edge = float(np.sqrt(np.mean(grad * grad))) / mean_lum
    if img.ndim == 3:
        hsv = cv2.cvtColor(img.astype(np.uint8), cv2.COLOR_BGR2HSV)
        sat = float(np.mean(hsv[:, :, 1])) / 255.0
    else:
        sat = 0.0
    return {"edge": edge, "saturation": sat, "brightness": mean_lum / 255.0}


def fog_score(img: np.ndarray) -> float:
    """霧・霞の兆候スコア (0=澄んでいる .. 1=濃霧的)。

    霧の画像は「エッジが消え、彩度が落ち、白っぽく明るい」性質を使う。
    曇天でもある程度上がるため、判定クラスのラベルは控えめにしてある。
    """
    f = _image_features(img)
    edge_term = float(np.clip(1.0 - f["edge"] / 0.20, 0.0, 1.0))
    sat_term = float(np.clip(1.0 - f["saturation"] / 0.30, 0.0, 1.0))
    bright_term = float(np.clip((f["brightness"] - 0.45) / 0.40, 0.0, 1.0))
    return round(0.5 * edge_term + 0.3 * sat_term + 0.2 * bright_term, 3)


def clarity_score(img: np.ndarray) -> float:
    """「澄んだ晴天フレームらしさ」。自動基準画像の選別に使う。"""
    f = _image_features(img)
    if not (0.20 <= f["brightness"] <= 0.90):
        return 0.0  # 夜間・露出異常は基準にしない
    return f["edge"] * (0.5 + f["saturation"])


# ---------------------------------------------------------------- 自動基準


def _ref_paths(cam_id: str):
    safe = "".join(c for c in cam_id if c.isalnum() or c in "-_")
    return AUTO_REF_DIR / f"{safe}.jpg", AUTO_REF_DIR / f"{safe}.json"


def consider_as_auto_reference(cam: CameraConfig, img: np.ndarray) -> bool:
    """現在フレームがこれまでで最も澄んでいれば自動基準として保存する。"""
    with _lock:
        now = time.monotonic()
        last = _last_ref_check.get(cam.id)
        if last is not None and now - last < AUTO_REF_CHECK_INTERVAL_SEC:
            return False
        _last_ref_check[cam.id] = now

    if is_low_light(img):
        return False
    score = clarity_score(img)
    if score <= 0.0:
        return False

    img_path, meta_path = _ref_paths(cam.id)
    prev_score = 0.0
    if meta_path.exists():
        try:
            with open(meta_path, encoding="utf-8") as f:
                prev_score = float(json.load(f).get("score", 0.0))
        except (OSError, ValueError, json.JSONDecodeError):
            prev_score = 0.0
    if prev_score > 0 and score < prev_score * AUTO_REF_IMPROVE_FACTOR:
        return False

    try:
        AUTO_REF_DIR.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(img_path), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(
                {"score": score, "fog_score": fog_score(img), "saved_at": time.time()},
                f,
            )
    except OSError:
        return False
    return True


def _auto_reference_meta(cam: CameraConfig) -> dict:
    _, meta_path = _ref_paths(cam.id)
    if not meta_path.exists():
        return {}
    try:
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def auto_reference(cam: CameraConfig) -> Optional[np.ndarray]:
    img_path, _ = _ref_paths(cam.id)
    if not img_path.exists():
        return None
    return cv2.imread(str(img_path), cv2.IMREAD_COLOR)


# ---------------------------------------------------------------- 比較評価


def grid_clarity_pct(
    reference: np.ndarray, current: np.ndarray, grid: tuple[int, int] = (6, 4)
) -> Optional[float]:
    """画像全体をグリッド分割し、基準に対するコントラスト比の中央値 (%) を返す。

    ターゲット定義が無くても「晴天時と比べてどれだけ霞んでいるか」を測る。
    基準側に構造が乏しいブロック (空など) は除外する。
    """
    if reference.shape[:2] != current.shape[:2]:
        current = cv2.resize(current, (reference.shape[1], reference.shape[0]))
    ref_gray = _to_gray(reference)
    cur_gray = _to_gray(current)
    ref_norm = max(float(np.mean(ref_gray)), 1.0)
    cur_norm = max(float(np.mean(cur_gray)), 1.0)
    h, w = ref_gray.shape
    cols, rows = grid
    bw, bh = w // cols, h // rows

    def block_metric(gray: np.ndarray, x: int, y: int, norm: float) -> float:
        roi = gray[y : y + bh, x : x + bw]
        grad = _gradient_magnitude(roi)
        return float(np.sqrt(np.mean(grad * grad))) / norm

    ref_noise = noise_floor_metric(ref_gray, ref_norm)
    cur_noise = noise_floor_metric(cur_gray, cur_norm)

    ratios = []
    for r in range(rows):
        for c in range(cols):
            x, y = c * bw, r * bh
            rm = block_metric(ref_gray, x, y, ref_norm)
            rm = float(np.sqrt(max(rm * rm - ref_noise * ref_noise, 0.0)))
            if rm < ref_noise * 1.5 or rm < 1e-6:
                continue  # 基準に構造がない (空・一様面)
            cm = block_metric(cur_gray, x, y, cur_norm)
            cm = float(np.sqrt(max(cm * cm - cur_noise * cur_noise, 0.0)))
            ratios.append(float(np.clip(cm / rm, 0.0, 1.2)))
    if len(ratios) < 3:
        return None
    return round(float(np.median(ratios)) * 100.0, 1)


def assess(cam: CameraConfig, current: np.ndarray) -> dict:
    """簡易評価のエントリポイント。

    戻り値: {level, label, fog_score, clarity_pct, has_auto_reference}
    """
    if is_low_light(current):
        return {
            "level": "night",
            "label": "夜間・低照度 (評価不可)",
            "fog_score": None,
            "clarity_pct": None,
            "has_auto_reference": False,
        }

    score = fog_score(current)
    ref = auto_reference(cam)
    # 自動基準そのものが霧っぽい間 (運用開始直後など) は比較を信用しない。
    # 霧の日に始めると「霧 vs 霧 = 鮮明度100%」と誤評価してしまうため。
    ref_trusted = (
        ref is not None
        and _auto_reference_meta(cam).get("fog_score", 1.0) < AUTO_REF_TRUST_FOG_MAX
    )
    clarity = grid_clarity_pct(ref, current) if ref_trusted else None

    if clarity is not None:
        for threshold, level, label in CLARITY_CLASSES:
            if clarity >= threshold:
                return {
                    "level": level,
                    "label": label.format(pct=clarity),
                    "fog_score": score,
                    "clarity_pct": clarity,
                    "has_auto_reference": True,
                }

    for threshold, level, label in FOG_CLASSES:
        if score < threshold:
            return {
                "level": level,
                "label": label,
                "fog_score": score,
                "clarity_pct": None,
                "has_auto_reference": ref is not None,
            }
    # ここには到達しない (最後のクラスが必ず受ける)
    return {
        "level": "unknown",
        "label": "評価不能",
        "fog_score": score,
        "clarity_pct": None,
        "has_auto_reference": ref is not None,
    }
