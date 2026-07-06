"""簡易天候評価 (app/quicklook.py) のテスト。

デモ画像は Koschmieder 則に従い正確に霞ませてあるので、
視程真値と評価の整合を確認する。
"""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import quicklook  # noqa: E402
from app.config import CameraConfig  # noqa: E402


def load(path: str) -> np.ndarray:
    img = cv2.imread(str(ROOT / path))
    assert img is not None, path
    return img


def make_camera(cam_id: str) -> CameraConfig:
    return CameraConfig.from_dict(
        {
            "id": cam_id,
            "name": cam_id,
            "lat": 35.0,
            "lon": 139.0,
            "heading_deg": 0,
            "source": {"type": "url", "url": "http://example/cam.jpg"},
        }
    )


@pytest.fixture(autouse=True)
def isolated_auto_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(quicklook, "AUTO_REF_DIR", tmp_path / "auto_ref")
    monkeypatch.setattr(quicklook, "_last_ref_check", {})


CLEAR = "data/current/tokyo-heliport-west.jpg"  # 真値 V=100km
HAZY = "data/current/yokohama-mm-southwest.jpg"  # 真値 V=6km
FOGGY = "data/current/kawasaki-coast-east.jpg"  # 真値 V=1.2km


class TestFogScore:
    def test_monotonic_with_visibility(self):
        s_clear = quicklook.fog_score(load(CLEAR))
        s_hazy = quicklook.fog_score(load(HAZY))
        s_foggy = quicklook.fog_score(load(FOGGY))
        assert s_clear < s_hazy <= s_foggy + 0.1  # 濃霧と靄は近くてよい
        assert s_clear < 0.30  # 快晴は good 域
        assert s_foggy > 0.55  # 濃霧は明確に高い

    def test_assess_without_reference_classifies(self):
        cam = make_camera("q1")
        good = quicklook.assess(cam, load(CLEAR))
        bad = quicklook.assess(cam, load(FOGGY))
        assert good["level"] == "good"
        assert bad["level"] in ("haze", "fog")
        assert not good["has_auto_reference"]

    def test_night_detected(self):
        cam = make_camera("q2")
        night = (load(CLEAR).astype(float) * 0.1).astype(np.uint8)
        out = quicklook.assess(cam, night)
        assert out["level"] == "night"


class TestAutoReference:
    def test_clear_frame_becomes_reference(self):
        cam = make_camera("a1")
        assert quicklook.consider_as_auto_reference(cam, load(CLEAR))
        assert quicklook.auto_reference(cam) is not None

    def test_foggy_frame_does_not_replace_clear(self, monkeypatch):
        cam = make_camera("a2")
        quicklook.consider_as_auto_reference(cam, load(CLEAR))
        monkeypatch.setattr(quicklook, "_last_ref_check", {})  # 間引き解除
        assert not quicklook.consider_as_auto_reference(cam, load(FOGGY))

    def test_throttled(self):
        cam = make_camera("a3")
        quicklook.consider_as_auto_reference(cam, load(CLEAR))
        # 間引き間隔内の 2 回目は保存処理に入らない
        assert not quicklook.consider_as_auto_reference(cam, load(CLEAR))

    def test_night_not_saved(self):
        cam = make_camera("a4")
        night = (load(CLEAR).astype(float) * 0.05).astype(np.uint8)
        assert not quicklook.consider_as_auto_reference(cam, night)


class TestGridClarity:
    def test_clear_vs_itself_near_100(self):
        img = load(CLEAR)
        pct = quicklook.grid_clarity_pct(img, img)
        assert pct is not None and pct > 85

    def test_foggy_vs_clear_low(self):
        # 同一シーン (川崎) の基準画像と濃霧画像
        ref = load("data/reference/kawasaki-coast-east.jpg")
        cur = load(FOGGY)
        pct = quicklook.grid_clarity_pct(ref, cur)
        assert pct is not None and pct < 40

    def test_foggy_first_frame_not_trusted_as_reference(self):
        # 霧の日に運用開始 → 最初の1枚 (霧) が自動基準になっても、
        # 「霧 vs 霧 = 鮮明度高」と誤評価してはならない
        cam = make_camera("g0")
        foggy = load(FOGGY)
        quicklook.consider_as_auto_reference(cam, foggy)
        out = quicklook.assess(cam, foggy)
        assert out["clarity_pct"] is None  # 比較は信用しない
        assert out["level"] in ("haze", "fog")  # 単一画像評価にフォールバック

    def test_assess_uses_auto_reference(self):
        cam = make_camera("g1")
        quicklook.consider_as_auto_reference(
            cam, load("data/reference/kawasaki-coast-east.jpg")
        )
        out = quicklook.assess(cam, load(FOGGY))
        assert out["has_auto_reference"]
        assert out["clarity_pct"] is not None
        assert out["level"] in ("haze", "fog")
