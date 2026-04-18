"""戦略共通のバックテスト実行エンジン。

各戦略は日次シグナル（+1=買いエントリ, -1=手仕舞い, 0=維持）を生成し、
当モジュールが翌日寄付き価格で約定させてポジション・損益・トレード履歴を算出する。

オプションで損切り（ストップロス）・利確（テイクプロフィット）をサポートする。
いずれも「エントリ価格からの下落率/上昇率」で指定し、寄付き後のザラ場で
Low/High が閾値に到達した時点で約定させる（ギャップ時は寄付き価格で約定）。
"""

from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

INITIAL_CASH = 1_000_000  # 100万円（銘柄ごとに独立）


def run_backtest(
    df: pd.DataFrame,
    signal: Iterable[int],
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
) -> dict:
    """シグナル列から実際の約定・損益を計算する。

    Args:
      df: OHLCV を含む DataFrame
      signal: 日次シグナル列（+1=買い, -1=手仕舞い, 0=維持）
      stop_loss_pct: 有効化するとエントリ価格から -X% 下落時に強制手仕舞い（例: 3.0 で -3%）
      take_profit_pct: 有効化するとエントリ価格から +X% 上昇時に強制手仕舞い（例: 5.0 で +5%）

    仕様:
      - シグナルは当日終値時点で判定し、翌営業日の Open で執行する
      - ポジションは0か1の全力ロング単位（単純化のため買越しのみ、保有中の買いシグナルは無視）
      - 買える最大株数（1株単位）で購入し、余った現金は保持する
      - 手仕舞い時は翌日Openで全株売却
      - 損切り/利確はエントリ翌日以降のザラ場で判定（同日内の再エントリは不可）
      - ギャップダウンで Open < stop 価格の場合は Open で約定（現実的な最悪ケース）
      - ギャップアップで Open > take profit 価格の場合も Open で約定（ギャップ益を反映）
      - 同日内に損切りと利確両方が理論上トリガー可能なとき、保守的に損切りを優先する
    """
    sig = np.asarray(list(signal), dtype=int)
    if len(df) != len(sig):
        raise ValueError("シグナル長がDataFrame長と一致しません")

    cash = INITIAL_CASH
    shares = 0
    entry_price: float | None = None
    equity_curve: list[float] = []
    trades: list[dict] = []
    cumulative_pl = 0.0

    open_prices = df["Open"].to_numpy(dtype=float)
    high_prices = df["High"].to_numpy(dtype=float)
    low_prices = df["Low"].to_numpy(dtype=float)
    close_prices = df["Close"].to_numpy(dtype=float)
    dates = df["Date"].tolist()
    n = len(df)

    def _sell(i: int, price: float, reason: str) -> None:
        nonlocal cash, shares, entry_price, cumulative_pl
        proceeds = shares * price
        trade_pl = (price - (entry_price or price)) * shares
        cumulative_pl += trade_pl
        cash += proceeds
        trades.append({
            "date": dates[i].strftime("%Y-%m-%d"),
            "side": "SELL",
            "price": round(price, 2),
            "shares": shares,
            "pl": round(trade_pl, 2),
            "cumulative_pl": round(cumulative_pl, 2),
            "reason": reason,
        })
        shares = 0
        entry_price = None

    for i in range(n):
        entered_this_bar = False
        # i日目の始値で前日シグナルを執行する（i-1のシグナルをここで処理）
        if i > 0 and shares == 0 and sig[i - 1] == 1:
            price = open_prices[i]
            buy_shares = int(cash // price)
            if buy_shares > 0:
                cash -= buy_shares * price
                shares = buy_shares
                entry_price = price
                entered_this_bar = True
                trades.append({
                    "date": dates[i].strftime("%Y-%m-%d"),
                    "side": "BUY",
                    "price": round(price, 2),
                    "shares": buy_shares,
                    "pl": 0.0,
                    "cumulative_pl": round(cumulative_pl, 2),
                    "reason": "SIGNAL",
                })
        elif i > 0 and shares > 0 and sig[i - 1] == -1:
            _sell(i, open_prices[i], "SIGNAL")

        # 損切り/利確（エントリ当日はスキップして翌日以降に判定）
        if shares > 0 and not entered_this_bar and entry_price is not None:
            stop_price = entry_price * (1 - stop_loss_pct / 100) if stop_loss_pct else None
            tp_price = entry_price * (1 + take_profit_pct / 100) if take_profit_pct else None

            # 損切り優先（保守的）
            if stop_price is not None and low_prices[i] <= stop_price:
                # ギャップダウンは寄付きで約定、そうでなければ閾値で約定
                exit_price = min(open_prices[i], stop_price)
                _sell(i, exit_price, "STOP")
            elif tp_price is not None and high_prices[i] >= tp_price:
                # ギャップアップは寄付きで約定、そうでなければ閾値で約定
                exit_price = max(open_prices[i], tp_price)
                _sell(i, exit_price, "TP")

        # 当日終値ベースの時価評価（チャート描画用）
        equity = cash + shares * close_prices[i]
        equity_curve.append(round(float(equity), 2))

    # 期末に保有中なら最終終値で時価決済した想定で集計（トレードとしては未確定扱い）
    final_equity = equity_curve[-1] if equity_curve else INITIAL_CASH
    final_pl = final_equity - INITIAL_CASH
    wins = [t for t in trades if t["side"] == "SELL" and t["pl"] > 0]
    losses = [t for t in trades if t["side"] == "SELL" and t["pl"] <= 0]
    total_closed = len(wins) + len(losses)
    win_rate = (len(wins) / total_closed * 100) if total_closed else 0.0

    return {
        "equity_curve": equity_curve,
        "dates": [d.strftime("%Y-%m-%d") for d in dates],
        "trades": trades,
        "final_equity": round(float(final_equity), 2),
        "final_pl": round(float(final_pl), 2),
        "return_pct": round(final_pl / INITIAL_CASH * 100, 2),
        "win_rate": round(win_rate, 2),
        "trade_count": total_closed,
        "open_position": shares > 0,
    }
