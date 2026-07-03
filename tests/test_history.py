"""推定履歴 (app/history.py) のテスト。"""

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import history  # noqa: E402
from app.analysis import Estimate  # noqa: E402


def make_estimate(vis=10.0, ceiling=2000.0) -> Estimate:
    return Estimate(
        visibility_km=vis,
        visibility_is_lower_bound=False,
        ceiling_ft_agl=ceiling,
        ceiling_is_unlimited=False,
        cloud_cover_oktas=4,
        cloud_cover_label="SCT",
        flight_category="MVFR",
    )


@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    monkeypatch.setattr(history, "HISTORY_DIR", tmp_path)
    monkeypatch.setattr(history, "_last_recorded", {})
    yield tmp_path


class TestRecord:
    def test_record_and_load(self):
        assert history.record("cam1", make_estimate(vis=7.5))
        points = history.load("cam1")
        assert len(points) == 1
        assert points[0]["visibility_km"] == 7.5
        assert points[0]["flight_category"] == "MVFR"

    def test_throttled_within_interval(self):
        assert history.record("cam1", make_estimate())
        assert not history.record("cam1", make_estimate())  # 間引かれる
        assert len(history.load("cam1")) == 1

    def test_unknown_estimate_not_recorded(self):
        est = make_estimate()
        est.visibility_km = None
        assert not history.record("cam1", est)
        assert history.load("cam1") == []

    def test_cameras_are_isolated(self):
        history.record("cam1", make_estimate(vis=1.0))
        history.record("cam2", make_estimate(vis=2.0))
        assert history.load("cam1")[0]["visibility_km"] == 1.0
        assert history.load("cam2")[0]["visibility_km"] == 2.0

    def test_path_is_sanitized(self):
        history.record("../evil", make_estimate())
        files = list(Path(history.HISTORY_DIR).glob("*.jsonl"))
        assert len(files) == 1
        assert ".." not in files[0].name


class TestLoad:
    def test_old_entries_filtered(self, isolated_history):
        path = isolated_history / "cam1.jsonl"
        old = {"ts": time.time() - 24 * 3600, "visibility_km": 3.0}
        new = {"ts": time.time() - 60, "visibility_km": 9.0}
        path.write_text(
            json.dumps(old) + "\n" + json.dumps(new) + "\n", encoding="utf-8"
        )
        points = history.load("cam1", hours=12)
        assert [p["visibility_km"] for p in points] == [9.0]

    def test_corrupt_lines_skipped(self, isolated_history):
        path = isolated_history / "cam1.jsonl"
        good = {"ts": time.time(), "visibility_km": 5.0}
        path.write_text("not json\n" + json.dumps(good) + "\n", encoding="utf-8")
        assert len(history.load("cam1")) == 1

    def test_missing_file(self):
        assert history.load("nothing") == []


class TestPrune:
    def test_prune_removes_old_entries(self, isolated_history, monkeypatch):
        monkeypatch.setattr(history, "PRUNE_THRESHOLD", 5)
        path = isolated_history / "cam1.jsonl"
        lines = []
        for i in range(8):
            lines.append(json.dumps({"ts": time.time() - 100 * 3600, "visibility_km": 1.0}))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        history._prune_if_needed(path)
        remaining = path.read_text(encoding="utf-8").strip()
        assert remaining == ""  # 全て保持期間外
