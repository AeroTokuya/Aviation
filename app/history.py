"""推定履歴の記録と読み出し。

カメラごとに data/history/<cam_id>.jsonl へ追記する。
操縦士の天候判断で重要な「良くなっているか悪くなっているか」の
トレンド表示に使う。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from .analysis import Estimate
from .config import ROOT

HISTORY_DIR = ROOT / "data" / "history"

# 同一カメラの記録間隔 (秒)。自動更新のたびに肥大化しないよう間引く
RECORD_INTERVAL_SEC = 300
# 保持期間 (時間)。これより古い行は書き込み時に間引く
RETENTION_HOURS = 48
# この行数を超えたら保持期間で刈り込む
# (300 秒間隔 × 48 時間 = 576 行なので、それを少し超えたら刈り込む)
PRUNE_THRESHOLD = 600

_last_recorded: dict[str, float] = {}
# FastAPI は同期エンドポイントをスレッドプールで並行実行するため、
# 同一ファイルへの追記・刈り込みと間引き判定を直列化する
_lock = threading.Lock()


def _path(cam_id: str) -> Path:
    safe = "".join(c for c in cam_id if c.isalnum() or c in "-_")
    return HISTORY_DIR / f"{safe}.jsonl"


def record(cam_id: str, est: Estimate) -> bool:
    """推定結果を履歴に追記する。間引き間隔内なら何もしない。

    履歴はあくまで副次機能なので、呼び出し元を壊さないよう
    書き込み失敗は False を返すだけで例外は外に出さない。
    """
    if est.visibility_km is None:
        return False  # 夜間等の UNKNOWN はトレンドに乗せない
    entry = {
        "ts": time.time(),
        "visibility_km": est.visibility_km,
        "visibility_is_lower_bound": est.visibility_is_lower_bound,
        "ceiling_ft_agl": est.ceiling_ft_agl,
        "ceiling_is_unlimited": est.ceiling_is_unlimited,
        "flight_category": est.flight_category,
    }
    with _lock:
        now = time.monotonic()
        last = _last_recorded.get(cam_id)
        if last is not None and now - last < RECORD_INTERVAL_SEC:
            return False
        try:
            HISTORY_DIR.mkdir(parents=True, exist_ok=True)
            path = _path(cam_id)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            return False
        # 書き込みが成功したときだけ間引きタイマーを進める
        _last_recorded[cam_id] = now
        _prune_if_needed(path)
    return True


def _prune_if_needed(path: Path) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    if len(lines) <= PRUNE_THRESHOLD:
        return
    cutoff = time.time() - RETENTION_HOURS * 3600
    kept = []
    for line in lines:
        try:
            if json.loads(line).get("ts", 0) >= cutoff:
                kept.append(line)
        except json.JSONDecodeError:
            continue
    try:
        tmp = path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(kept)
        tmp.replace(path)
    except OSError:
        pass  # 刈り込み失敗は次回に持ち越す


def load(cam_id: str, hours: float = 12.0) -> list[dict]:
    """直近 hours 時間の履歴を古い順で返す。"""
    path = _path(cam_id)
    if not path.exists():
        return []
    cutoff = time.time() - hours * 3600
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("ts", 0) >= cutoff:
                    out.append(entry)
    except OSError:
        return []
    return out
