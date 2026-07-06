"""テスト共通設定。バックグラウンド巡回はテストでは起動しない。"""

import os

os.environ.setdefault("HELIWX_BACKGROUND_REFRESH", "0")
