"""バックテスト戦略モジュール群。"""

from .vwap import run_vwap_strategy
from .volume_price import run_volume_price_strategy
from .ma_cross import run_ma_cross_strategy

STRATEGIES = {
    "vwap": {"label": "VWAP戦略", "runner": run_vwap_strategy},
    "volume_price": {"label": "出来高加重価格戦略", "runner": run_volume_price_strategy},
    "ma_cross": {"label": "移動平均クロス戦略", "runner": run_ma_cross_strategy},
}
