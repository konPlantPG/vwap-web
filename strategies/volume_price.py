"""出来高加重価格戦略。

シグナル:
  - 出来高 > 過去5日平均の1.5倍 かつ 終値 > 前日終値 → 翌日買い
  - 出来高 > 過去5日平均の1.5倍 かつ 終値 < 前日終値 → 翌日売り（手仕舞い）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ._engine import run_backtest

VOLUME_MULTIPLIER = 1.5
VOLUME_WINDOW = 5


def run_volume_price_strategy(
    df: pd.DataFrame,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
) -> dict:
    close = df["Close"].astype(float)
    volume = df["Volume"].astype(float)
    # 直前5日（当日を含まない）の平均出来高と比較する
    vol_ma = volume.rolling(VOLUME_WINDOW).mean().shift(1)
    prev_close = close.shift(1)

    signal = np.zeros(len(df), dtype=int)
    for i in range(len(df)):
        vma = vol_ma.iloc[i]
        pc = prev_close.iloc[i]
        if pd.isna(vma) or pd.isna(pc):
            continue
        if volume.iloc[i] <= vma * VOLUME_MULTIPLIER:
            continue
        if close.iloc[i] > pc:
            signal[i] = 1
        elif close.iloc[i] < pc:
            signal[i] = -1

    result = run_backtest(df, signal, stop_loss_pct, take_profit_pct)
    result["indicator"] = {
        "name": "出来高5日MA",
        "series": [None if pd.isna(v) else round(float(v), 2) for v in vol_ma.to_numpy()],
    }
    result["signals"] = signal.tolist()
    return result
