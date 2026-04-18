"""移動平均クロス戦略。

シグナル:
  - 短期MA(5日)が長期MA(25日)を上抜け（ゴールデンクロス） → 翌日買い
  - 短期MA(5日)が長期MA(25日)を下抜け（デッドクロス） → 翌日売り（手仕舞い）
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ._engine import run_backtest

SHORT_WINDOW = 5
LONG_WINDOW = 25


def run_ma_cross_strategy(
    df: pd.DataFrame,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
) -> dict:
    close = df["Close"].astype(float)
    short_ma = close.rolling(SHORT_WINDOW).mean()
    long_ma = close.rolling(LONG_WINDOW).mean()

    diff = short_ma - long_ma
    signal = np.zeros(len(df), dtype=int)
    for i in range(1, len(df)):
        if pd.isna(diff.iloc[i]) or pd.isna(diff.iloc[i - 1]):
            continue
        prev, curr = diff.iloc[i - 1], diff.iloc[i]
        if prev <= 0 and curr > 0:
            signal[i] = 1
        elif prev >= 0 and curr < 0:
            signal[i] = -1

    result = run_backtest(df, signal, stop_loss_pct, take_profit_pct)
    result["indicator"] = {
        "name": f"MA({SHORT_WINDOW})/MA({LONG_WINDOW})",
        "series_short": [None if pd.isna(v) else round(float(v), 2) for v in short_ma.to_numpy()],
        "series_long": [None if pd.isna(v) else round(float(v), 2) for v in long_ma.to_numpy()],
    }
    result["signals"] = signal.tolist()
    return result
