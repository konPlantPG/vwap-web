"""VWAP戦略。

シグナル:
  - 終値 > VWAP → 翌日買いエントリ
  - 終値 < VWAP → 翌日売り（手仕舞い）

VWAPは日足データの場合、日々の (高値+安値+終値)/3 を出来高で加重した累積平均として計算する。
ここでは区間全体の累積VWAPを用いる（期間内の平均水準との比較で十分とする）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ._engine import run_backtest


def compute_vwap(df: pd.DataFrame) -> np.ndarray:
    """出来高加重平均価格（累積VWAP）を返す。"""
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    volume = df["Volume"].astype(float)
    cum_pv = (typical * volume).cumsum()
    cum_vol = volume.cumsum().replace(0, np.nan)
    vwap = cum_pv / cum_vol
    return vwap.to_numpy()


def run_vwap_strategy(
    df: pd.DataFrame,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
) -> dict:
    """VWAP戦略をバックテストし、結果辞書に追加指標を含めて返す。"""
    vwap = compute_vwap(df)
    close = df["Close"].to_numpy(dtype=float)

    signal = np.zeros(len(df), dtype=int)
    for i in range(len(df)):
        if np.isnan(vwap[i]):
            continue
        if close[i] > vwap[i]:
            signal[i] = 1
        elif close[i] < vwap[i]:
            signal[i] = -1

    result = run_backtest(df, signal, stop_loss_pct, take_profit_pct)
    result["indicator"] = {
        "name": "VWAP",
        "series": [None if np.isnan(v) else round(float(v), 2) for v in vwap],
    }
    result["signals"] = signal.tolist()
    return result
