"""解析ロジックの検証テスト。

デモ画像 (data/) は Koschmieder 則に従って正確に合成されているため、
エンドツーエンドの推定精度検証にも用いる。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import analysis, sources  # noqa: E402
from app.analysis import Target, TargetResult  # noqa: E402
from app.config import load_cameras  # noqa: E402


def make_scene(attenuations: list[float], rng_seed: int = 7) -> tuple:
    """テクスチャ付きターゲットを並べた合成画像ペアを作る。

    attenuations[i] はターゲット i のコントラスト透過率 t (1=快晴と同一)。
    """
    rng = np.random.default_rng(rng_seed)
    h, w = 200, 120 * len(attenuations)
    bg = 180.0
    ref = np.full((h, w), bg)
    cur = np.full((h, w), bg)
    bboxes = []
    for i, t in enumerate(attenuations):
        x = 10 + 120 * i
        tex = rng.normal(0, 40, (80, 100))
        ref[60:140, x : x + 100] = bg + tex
        cur[60:140, x : x + 100] = bg + tex * t
        bboxes.append((x, 60, 100, 80))
    ref3 = np.clip(ref, 0, 255).astype(np.uint8)
    cur3 = np.clip(cur, 0, 255).astype(np.uint8)
    return np.stack([ref3] * 3, -1), np.stack([cur3] * 3, -1), bboxes


class TestContrastRatio:
    def test_identical_images_ratio_is_one(self):
        ref, cur, boxes = make_scene([1.0])
        r = analysis.target_contrast_ratio(ref, ref, boxes[0])
        assert r == pytest.approx(1.0, abs=0.02)

    def test_attenuation_is_recovered(self):
        ref, cur, boxes = make_scene([0.5])
        r = analysis.target_contrast_ratio(ref, cur, boxes[0])
        assert r == pytest.approx(0.5, abs=0.08)

    def test_fully_obscured_target(self):
        ref, cur, boxes = make_scene([0.0])
        r = analysis.target_contrast_ratio(ref, cur, boxes[0])
        assert r < 0.05


class TestKoschmieder:
    def test_visibility_from_known_attenuation(self):
        # t = exp(-3.912*d/V) で V=10km, d=5km → t=0.1415
        v = analysis.koschmieder_visibility(5.0, np.exp(-3.912 * 5 / 10))
        assert v == pytest.approx(10.0, rel=0.01)

    def test_extreme_ratios_return_none(self):
        assert analysis.koschmieder_visibility(5.0, 1.0) is None
        assert analysis.koschmieder_visibility(5.0, 0.0) is None


class TestEstimateVisibility:
    def test_end_to_end_with_synthetic_targets(self):
        # V=8km を想定した減衰でターゲットを配置
        v_true = 8.0
        dists = [1.0, 3.0, 6.0, 15.0]
        atten = [np.exp(-3.912 * d / v_true) for d in dists]
        ref, cur, boxes = make_scene(atten)
        targets = [
            Target(name=f"t{i}", distance_km=d, bbox=boxes[i])
            for i, d in enumerate(dists)
        ]
        est = analysis.analyze(ref, cur, targets)
        assert est.visibility_km == pytest.approx(v_true, rel=0.35)

    def test_all_clear_reports_lower_bound(self):
        ref, cur, boxes = make_scene([1.0, 1.0])
        targets = [
            Target(name="a", distance_km=5, bbox=boxes[0]),
            Target(name="b", distance_km=20, bbox=boxes[1]),
        ]
        est = analysis.analyze(ref, cur, targets)
        assert est.visibility_is_lower_bound
        assert est.visibility_km >= 20


def _tr(name, dist, elev, ratio, visible):
    return TargetResult(
        name=name,
        distance_km=dist,
        elevation_ft_msl=elev,
        contrast_ratio=ratio,
        visible=visible,
        visibility_estimate_km=None,
    )


class TestCeiling:
    def test_cloud_between_visible_and_obscured(self):
        results = [
            _tr("hill", 5, 1000, 0.8, True),
            _tr("ridge", 8, 2000, 0.7, True),
            _tr("peak", 10, 4000, 0.01, False),  # 雲中: 予測比よりはるかに低い
        ]
        ceiling, unlimited, _ = analysis.estimate_ceiling(
            results, camera_elevation_ft=0, visibility_km=30, visibility_is_lower_bound=False
        )
        assert not unlimited
        assert 2000 <= ceiling <= 4000

    def test_haze_obscured_target_not_treated_as_cloud(self):
        # 視程 2km では 5km 先は霞で見えないだけ → シーリング判定に使わない
        results = [
            _tr("near", 0.5, 300, 0.6, True),
            _tr("far", 5, 4000, 0.0, False),
        ]
        ceiling, unlimited, _ = analysis.estimate_ceiling(
            results, camera_elevation_ft=0, visibility_km=2, visibility_is_lower_bound=False
        )
        assert unlimited

    def test_no_elevation_targets(self):
        results = [_tr("x", 1, None, 0.9, True)]
        ceiling, unlimited, notes = analysis.estimate_ceiling(
            results, camera_elevation_ft=0, visibility_km=10, visibility_is_lower_bound=False
        )
        assert ceiling is None and not unlimited
        assert notes


class TestFlightCategory:
    @pytest.mark.parametrize(
        "vis,ceil,unlimited,expected",
        [
            (10, None, True, "VFR"),
            (10, 3500, False, "VFR"),
            (6, None, True, "MVFR"),
            (10, 1500, False, "MVFR"),
            (3, None, True, "IFR"),
            (10, 700, False, "IFR"),
            (1.0, None, True, "LIFR"),
            (10, 300, False, "LIFR"),
            (None, None, False, "UNKNOWN"),
        ],
    )
    def test_categories(self, vis, ceil, unlimited, expected):
        assert analysis.flight_category(vis, ceil, unlimited) == expected


DEMO_TRUTH = {
    # id: (真の視程 km, 真の雲底 ft AGL または None)
    "tokyo-heliport-west": (100.0, None),
    "yokohama-mm-southwest": (6.0, None),
    "kawasaki-coast-east": (1.2, None),
    "atsugi-north": (30.0, 2600.0),
}


@pytest.mark.skipif(
    not (ROOT / "config" / "cameras.json").exists(), reason="デモデータ未生成"
)
class TestDemoEndToEnd:
    """合成デモ画像に対して推定値が真値と整合することを確認する。"""

    @pytest.fixture(scope="class")
    def estimates(self):
        cams = load_cameras()
        out = {}
        for cid, cam in cams.items():
            ref = sources.reference_image(cam)
            cur = sources.current_image(cam)
            assert ref is not None and cur is not None
            out[cid] = analysis.analyze(
                ref, cur, cam.targets, cam.elevation_ft, cam.sky_bbox
            )
        return out

    def test_visibility_within_factor_two(self, estimates):
        for cid, (v_true, _) in DEMO_TRUTH.items():
            est = estimates[cid]
            assert est.visibility_km is not None, cid
            if est.visibility_is_lower_bound:
                assert v_true >= est.visibility_km * 0.8, cid
            else:
                assert v_true / 2 <= est.visibility_km <= v_true * 2, cid

    def test_ceiling_detection(self, estimates):
        for cid, (_, ceil_true) in DEMO_TRUTH.items():
            est = estimates[cid]
            if ceil_true is None:
                assert est.ceiling_is_unlimited, cid
            else:
                assert not est.ceiling_is_unlimited, cid
                assert ceil_true - 1000 <= est.ceiling_ft_agl <= ceil_true + 1000, cid

    def test_flight_categories(self, estimates):
        assert estimates["tokyo-heliport-west"].flight_category == "VFR"
        assert estimates["yokohama-mm-southwest"].flight_category == "MVFR"
        assert estimates["kawasaki-coast-east"].flight_category == "LIFR"
        assert estimates["atsugi-north"].flight_category in ("MVFR", "IFR")
