"""Flask エントリポイント。

ルート:
  - GET  /            銘柄選択フォーム
  - POST /run         複数銘柄×3戦略のバックテスト結果ページ
  - GET  /detail/<code>/<strategy>   個別の詳細ページ（セッションにキャッシュした結果を表示）

バックテスト結果は Flask session に JSON として保持し、
詳細ページではキャッシュを参照して Plotly 用データを組み立てる。
"""

from __future__ import annotations

import json
import os
import secrets
import time
import traceback
from typing import Any

import pandas as pd
import plotly
import plotly.graph_objects as go
from dotenv import load_dotenv
from flask import Flask, abort, redirect, render_template, request, session, url_for

from fetch_data import (
    JQuantsAuthError,
    JQuantsError,
    JQuantsRateLimitError,
    cached_codes,
    fetch_daily_quotes,
)

# Free プラン (5 req/分) で 429 を誘発しないよう、銘柄間に 2 秒のウェイトを入れる
_INTER_REQUEST_SLEEP_SEC = 2.0
from strategies import STRATEGIES

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 1 * 1024 * 1024

# セッションクッキーのサイズ制限を避けるため、バックテスト結果はプロセス内キャッシュに保持し、
# セッションにはキャッシュキー（ランダムID）のみを格納する。
_RESULT_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_MAX_ENTRIES = 32


def _store_result(payload: dict[str, Any]) -> str:
    token = secrets.token_urlsafe(12)
    _RESULT_CACHE[token] = payload
    # 古いキャッシュを先頭から溢れさせる（単純なLRU風）
    while len(_RESULT_CACHE) > _CACHE_MAX_ENTRIES:
        oldest = next(iter(_RESULT_CACHE))
        _RESULT_CACHE.pop(oldest, None)
    return token


def _load_result() -> dict[str, Any] | None:
    token = session.get("result_token")
    if not token:
        return None
    return _RESULT_CACHE.get(token)

# J-Quants Free プランは 5 req/分 で非常に厳しいため、デフォルトは1銘柄のみとする
DEFAULT_CODES = ["7203"]
MAX_CODES = 10


def _json_dump(obj: Any) -> str:
    """Plotly用にnumpy型を含むオブジェクトを安全にJSON化する。"""
    return json.dumps(obj, cls=plotly.utils.PlotlyJSONEncoder)


def _render_index(
    codes: list[str] | None = None,
    error: str | None = None,
    auth_error: dict | None = None,
    rate_limit_error: dict | None = None,
    errors: list[str] | None = None,
):
    """index.html を共通レンダリングする。エラー系を種別ごとに渡せるようにする。"""
    return render_template(
        "index.html",
        default_codes=codes or DEFAULT_CODES,
        max_codes=MAX_CODES,
        error=error,
        auth_error=auth_error,
        rate_limit_error=rate_limit_error,
        errors=errors or [],
    )


@app.route("/")
def index():
    return _render_index()


def _parse_optional_pct(form, enabled_key: str, value_key: str, default: float) -> float | None:
    """チェックボックス有効時のみ % 値を float で返す。未有効/不正値は None。"""
    if form.get(enabled_key) != "on":
        return None
    try:
        v = float(form.get(value_key, str(default)))
    except (TypeError, ValueError):
        return None
    if v <= 0 or v > 100:
        return None
    return v


@app.route("/run", methods=["POST"])
def run_backtest():
    raw_codes = request.form.getlist("codes")
    # 4桁英数字の銘柄コード（例: 7203 / 285A）を想定し、小文字は大文字に正規化する
    codes = [c.strip().upper() for c in raw_codes if c and c.strip()]
    invalid = [c for c in codes if not (len(c) == 4 and c.isalnum())]
    if not codes:
        return _render_index(error="銘柄コードを1件以上入力してください")
    if invalid:
        return _render_index(
            codes=codes,
            error=f"4桁英数字で入力してください: {', '.join(invalid)}",
        )
    if len(codes) > MAX_CODES:
        return _render_index(
            codes=codes[:MAX_CODES],
            error=f"銘柄は最大{MAX_CODES}件までです",
        )

    # 損切り/利確オプション（チェックボックスで有効化、未有効なら None でエンジンに渡らない）
    stop_loss_pct = _parse_optional_pct(request.form, "use_stop", "stop_pct", 3.0)
    take_profit_pct = _parse_optional_pct(request.form, "use_tp", "tp_pct", 5.0)

    # バックテスト実行（銘柄×戦略の2次元）
    results: dict[str, dict[str, dict]] = {}
    price_data: dict[str, dict] = {}
    errors: list[str] = []

    for idx, code in enumerate(codes):
        if idx > 0:
            # レート制限回避のための軽いウェイト
            time.sleep(_INTER_REQUEST_SLEEP_SEC)
        try:
            df = fetch_daily_quotes(code)
        except JQuantsAuthError as e:
            # 認証エラーは全銘柄共通で致命的。即座に打ち切って専用表示する
            traceback.print_exc()
            return _render_index(
                codes=codes,
                auth_error={
                    "message": str(e),
                    "detail": e.detail,
                    "status": e.status,
                },
            )
        except JQuantsRateLimitError as e:
            # レート制限も全銘柄共通で一時的に致命的。即座に打ち切って待機を促す
            traceback.print_exc()
            # Free プランは 429 後に 5 分程度遮断されるため、目安時刻も伝える
            import datetime as _dt
            resume_at = _dt.datetime.now().astimezone() + _dt.timedelta(minutes=5)
            return _render_index(
                codes=codes,
                rate_limit_error={
                    "message": str(e),
                    "detail": e.detail,
                    "retry_after": e.retry_after,
                    "resume_at": resume_at.strftime("%H:%M:%S"),
                    "cached_codes": [c for c in codes if c in cached_codes()],
                },
            )
        except (JQuantsError, ValueError) as e:
            errors.append(f"{code}: {e}")
            continue
        except Exception as e:
            errors.append(f"{code}: 予期しないエラー ({e})")
            traceback.print_exc()
            continue
        if df.empty or len(df) < 30:
            errors.append(f"{code}: データ不足（{len(df)}件）")
            continue

        price_data[code] = {
            "dates": [d.strftime("%Y-%m-%d") for d in df["Date"]],
            "open": df["Open"].astype(float).round(2).tolist(),
            "high": df["High"].astype(float).round(2).tolist(),
            "low": df["Low"].astype(float).round(2).tolist(),
            "close": df["Close"].astype(float).round(2).tolist(),
            "volume": df["Volume"].astype(float).tolist(),
        }
        results[code] = {}
        for key, spec in STRATEGIES.items():
            results[code][key] = spec["runner"](
                df,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
            )

    if not results:
        return _render_index(
            codes=codes,
            error="バックテスト可能な銘柄がありませんでした",
            errors=errors,
        )

    # 結果はプロセス内キャッシュに格納し、セッションにはトークンのみ保持する
    # POST/Redirect/GET パターンで「フォーム再送信の確認」ダイアログを回避する
    token = _store_result({
        "results": results,
        "price_data": price_data,
        "codes": list(results.keys()),
        "errors": errors,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
    })
    session["result_token"] = token
    return redirect(url_for("show_result"))


@app.route("/result")
def show_result():
    """バックテスト結果ページ（GET）。セッションのトークン経由でキャッシュを参照する。

    /run から Redirect で遷移してくるため、ブラウザ履歴からの戻る操作で
    「フォーム再送信の確認」が出ないようにする役割を持つ。
    """
    cached = _load_result()
    if not cached:
        return redirect(url_for("index"))
    results = cached["results"]
    errors = cached.get("errors", [])
    stop_loss_pct = cached.get("stop_loss_pct")
    take_profit_pct = cached.get("take_profit_pct")

    # ヒートマップ（銘柄×戦略の最終損益）
    strategy_keys = list(STRATEGIES.keys())
    strategy_labels = [STRATEGIES[k]["label"] for k in strategy_keys]
    heatmap_rows: list[list[float]] = []
    cell_texts: list[list[str]] = []
    for code in results.keys():
        row_pl = [results[code][s]["final_pl"] for s in strategy_keys]
        row_txt = [
            f"{results[code][s]['final_pl']:+,.0f}円<br>勝率{results[code][s]['win_rate']}%<br>取引{results[code][s]['trade_count']}回"
            for s in strategy_keys
        ]
        heatmap_rows.append(row_pl)
        cell_texts.append(row_txt)

    heatmap = go.Heatmap(
        z=heatmap_rows,
        x=strategy_labels,
        y=list(results.keys()),
        text=cell_texts,
        texttemplate="%{text}",
        hovertemplate="%{y} × %{x}<br>損益: %{z:+,.0f}円<extra></extra>",
        colorscale=[
            [0.0, "#d62728"],
            [0.5, "#ffffff"],
            [1.0, "#2ca02c"],
        ],
        zmid=0,
    )
    heatmap_fig = go.Figure(heatmap)
    heatmap_fig.update_layout(
        title="最終損益ヒートマップ（銘柄 × 戦略）",
        xaxis_title="戦略",
        yaxis_title="銘柄",
        height=max(260, 60 * len(results) + 120),
        margin=dict(l=60, r=30, t=60, b=60),
    )

    return render_template(
        "result.html",
        codes=list(results.keys()),
        strategies=[{"key": k, "label": STRATEGIES[k]["label"]} for k in strategy_keys],
        results=results,
        heatmap_json=_json_dump(heatmap_fig),
        errors=errors,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
    )


@app.route("/detail/<code>/<strategy>")
def detail(code: str, strategy: str):
    cached = _load_result()
    if not cached:
        return redirect(url_for("index"))
    results = cached["results"]
    price_data = cached["price_data"]
    if code not in results or strategy not in results[code]:
        abort(404)

    price = price_data[code]
    stg = results[code][strategy]
    dates = price["dates"]

    # --- メインチャート（ローソク足 + インジケータ + シグナル） ---
    main_fig = go.Figure()
    main_fig.add_trace(go.Candlestick(
        x=dates,
        open=price["open"], high=price["high"],
        low=price["low"], close=price["close"],
        name="価格", increasing_line_color="#2ca02c", decreasing_line_color="#d62728",
    ))

    indicator = stg.get("indicator", {})
    if strategy == "vwap" and "series" in indicator:
        main_fig.add_trace(go.Scatter(
            x=dates, y=indicator["series"], mode="lines",
            name="VWAP", line=dict(color="#1f77b4", width=2),
        ))
    elif strategy == "ma_cross":
        main_fig.add_trace(go.Scatter(
            x=dates, y=indicator["series_short"], mode="lines",
            name="短期MA(5)", line=dict(color="#1f77b4", width=2),
        ))
        main_fig.add_trace(go.Scatter(
            x=dates, y=indicator["series_long"], mode="lines",
            name="長期MA(25)", line=dict(color="#ff7f0e", width=2),
        ))

    # 実際に約定したトレードのみマーカー表示する
    # （生のシグナル列を描くと毎日 ▲▼ が付き、トレード履歴と一致せず混乱するため）
    # 売り約定は reason（SIGNAL/STOP/TP）ごとに色と形を分けて視覚的に区別する
    trades = stg.get("trades", [])
    buy_x, buy_y, buy_text = [], [], []
    sell_groups: dict[str, dict[str, list]] = {
        "SIGNAL": {"x": [], "y": [], "text": []},
        "STOP": {"x": [], "y": [], "text": []},
        "TP": {"x": [], "y": [], "text": []},
    }
    for t in trades:
        if t["side"] == "BUY":
            buy_x.append(t["date"])
            buy_y.append(t["price"])
            buy_text.append(f"BUY {t['shares']}株 @ {t['price']:,.0f}円")
        elif t["side"] == "SELL":
            reason = t.get("reason", "SIGNAL")
            g = sell_groups.get(reason, sell_groups["SIGNAL"])
            g["x"].append(t["date"])
            g["y"].append(t["price"])
            g["text"].append(
                f"SELL({reason}) {t['shares']}株 @ {t['price']:,.0f}円<br>損益 {t['pl']:+,.0f}円"
            )
    main_fig.add_trace(go.Scatter(
        x=buy_x, y=buy_y, mode="markers", name="買い約定",
        marker=dict(symbol="triangle-up", size=14, color="#2ca02c",
                    line=dict(color="#ffffff", width=1.5)),
        text=buy_text, hovertemplate="%{x}<br>%{text}<extra></extra>",
    ))
    sell_styles = [
        ("SIGNAL", "売り約定(シグナル)", "triangle-down", "#d62728"),
        ("STOP", "損切り約定", "x", "#7c2d12"),
        ("TP", "利確約定", "star", "#1d4ed8"),
    ]
    for key, label, symbol, color in sell_styles:
        g = sell_groups[key]
        if not g["x"]:
            continue
        main_fig.add_trace(go.Scatter(
            x=g["x"], y=g["y"], mode="markers", name=label,
            marker=dict(symbol=symbol, size=14, color=color,
                        line=dict(color="#ffffff", width=1.5)),
            text=g["text"], hovertemplate="%{x}<br>%{text}<extra></extra>",
        ))
    main_fig.update_layout(
        title=f"{code} / {STRATEGIES[strategy]['label']} - 価格チャート",
        xaxis_rangeslider_visible=False,
        height=520, margin=dict(l=50, r=30, t=60, b=40),
    )

    # --- 損益曲線 ---
    pnl_fig = go.Figure(go.Scatter(
        x=stg["dates"], y=stg["equity_curve"], mode="lines",
        name="資産推移", line=dict(color="#9467bd", width=2),
        hovertemplate="%{x}<br>資産: %{y:,.0f}円<extra></extra>",
    ))
    pnl_fig.add_hline(y=1_000_000, line_dash="dash", line_color="#aaaaaa",
                      annotation_text="初期資金", annotation_position="top left")
    pnl_fig.update_layout(
        title="損益曲線", height=320, margin=dict(l=50, r=30, t=50, b=40),
        yaxis_title="資産評価額(円)",
    )

    return render_template(
        "detail.html",
        code=code,
        strategy_key=strategy,
        strategy_label=STRATEGIES[strategy]["label"],
        stats={
            "final_pl": stg["final_pl"],
            "return_pct": stg["return_pct"],
            "win_rate": stg["win_rate"],
            "trade_count": stg["trade_count"],
            "open_position": stg["open_position"],
        },
        trades=stg["trades"],
        main_json=_json_dump(main_fig),
        pnl_json=_json_dump(pnl_fig),
        stop_loss_pct=cached.get("stop_loss_pct"),
        take_profit_pct=cached.get("take_profit_pct"),
    )


@app.errorhandler(413)
def payload_too_large(_e):
    return _render_index(error="リクエストサイズが大きすぎます"), 413


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
