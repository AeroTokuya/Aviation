"""API エンドポイントのテスト (FastAPI TestClient)。"""

import json
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config as config_mod  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)

DEMO_ID = "tokyo-heliport-west"


class TestListCameras:
    def test_returns_all_cameras_with_status(self):
        res = client.get("/api/cameras")
        assert res.status_code == 200
        cams = res.json()
        assert len(cams) >= 4
        for c in cams:
            assert c["status"] in (
                "estimate", "needs_reference", "needs_targets", "view", "page", "error"
            )
            assert "image_capable" in c

    def test_demo_camera_has_summary(self):
        cams = {c["id"]: c for c in client.get("/api/cameras").json()}
        assert cams[DEMO_ID]["summary"] is not None
        assert cams[DEMO_ID]["summary"]["flight_category"] == "VFR"


class TestEstimate:
    def test_demo_estimate(self):
        res = client.get(f"/api/cameras/{DEMO_ID}/estimate")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "estimate"
        assert body["estimate"]["visibility_km"] is not None

    def test_unknown_camera_404(self):
        assert client.get("/api/cameras/nope/estimate").status_code == 404


class TestImages:
    def test_current_and_reference(self):
        for kind in ("current", "reference"):
            res = client.get(f"/api/cameras/{DEMO_ID}/image/{kind}")
            assert res.status_code == 200
            assert res.headers["content-type"] == "image/jpeg"

    def test_bad_kind_404(self):
        assert client.get(f"/api/cameras/{DEMO_ID}/image/zzz").status_code == 404


class TestSaveSetup:
    @pytest.fixture()
    def config_backup(self):
        backup = config_mod.CONFIG_PATH.with_suffix(".json.bak")
        shutil.copy(config_mod.CONFIG_PATH, backup)
        yield
        shutil.move(backup, config_mod.CONFIG_PATH)

    def test_save_and_reload(self, config_backup):
        before = client.get(f"/api/cameras/{DEMO_ID}/estimate").json()
        targets = before["estimate"]["targets"]
        payload = {
            "targets": [
                {
                    "name": "テスト目標",
                    "distance_km": 5.0,
                    "bbox": [10, 10, 50, 40],
                    "elevation_ft_msl": 1200,
                }
            ],
            "sky_bbox": [0, 0, 100, 50],
        }
        res = client.post(f"/api/cameras/{DEMO_ID}/setup", json=payload)
        assert res.status_code == 200, res.text
        assert res.json()["targets"] == 1

        # ファイルにも反映されている
        with open(config_mod.CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        entry = next(c for c in raw["cameras"] if c["id"] == DEMO_ID)
        assert entry["targets"][0]["name"] == "テスト目標"
        assert entry["sky_bbox"] == [0, 0, 100, 50]

        # メモリ上の設定もリロードされ、推定に反映される
        after = client.get(f"/api/cameras/{DEMO_ID}/estimate").json()
        assert len(after["estimate"]["targets"]) == 1
        assert len(targets) != 1  # 元は複数ターゲットだった

    def test_invalid_bbox_rejected(self, config_backup):
        payload = {
            "targets": [
                {"name": "bad", "distance_km": 5.0, "bbox": [-1, 0, 50, 40]}
            ]
        }
        res = client.post(f"/api/cameras/{DEMO_ID}/setup", json=payload)
        assert res.status_code == 422

    def test_invalid_distance_rejected(self, config_backup):
        payload = {
            "targets": [{"name": "bad", "distance_km": 0, "bbox": [0, 0, 50, 40]}]
        }
        assert client.post(f"/api/cameras/{DEMO_ID}/setup", json=payload).status_code == 422

    def test_unknown_camera_404(self):
        assert client.post("/api/cameras/nope/setup", json={"targets": []}).status_code == 404
