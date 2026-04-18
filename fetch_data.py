"""J-Quants V2 APIから日足株価データを取得するモジュール。

認証方針（V2 API）:
  - 2025-12-22 以降の新規登録ユーザーは V2 API のみ利用可能
  - ダッシュボードで発行したAPIキーを `x-api-key` ヘッダに指定するだけ（トークン交換は不要）
  - 取得に必要な環境変数は JQUANTS_API_KEY（旧名 JQUANTS_REFRESH_TOKEN もフォールバック対応）

参考:
  - https://jpx-jquants.com/ja/spec/migration-v1-v2
  - https://jpx-jquants.com/ja/spec/quickstart
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests

JQUANTS_BASE_URL = "https://api.jquants.com/v2"
DAILY_ENDPOINT = f"{JQUANTS_BASE_URL}/equities/bars/daily"

# 購読期間外の400エラーから「2024-01-24 ~ 2026-01-24」の範囲を抽出するための正規表現
_SUBSCRIPTION_RANGE_RE = re.compile(
    r"subscription covers the following dates:\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)

# プロセス内キャッシュ（一度判明した購読終了日を再利用してリトライを減らす）
_subscription_end_cache: date | None = None

# 銘柄ごとの日足データをプロセス内キャッシュする（Free プランは 5 req/分で厳しいため必須）
# 構造: { code: (timestamp, DataFrame) }
_DAILY_CACHE_TTL_SEC = 60 * 60  # 1時間
_daily_quotes_cache: dict[str, tuple[float, "pd.DataFrame"]] = {}


class JQuantsError(RuntimeError):
    """J-Quants APIアクセスで発生したエラーを表す例外。"""


class _SubscriptionRangeError(Exception):
    """購読期間外を示す 400 レスポンスの内部伝播用例外。"""

    def __init__(self, body: str) -> None:
        super().__init__(body)
        self.body = body


class JQuantsRateLimitError(JQuantsError):
    """短時間の連続アクセスで 429 が返ったことを表す例外。

    画面側ではこれを特別扱いし、ユーザーに「しばらく待って再実行」を促す。
    """

    def __init__(self, message: str, *, detail: str = "", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.detail = detail
        self.retry_after = retry_after


class JQuantsAuthError(JQuantsError):
    """認証（APIキー）関連のエラー。

    画面側ではこの例外を特別扱いし、ユーザーに APIキー再発行/再設定 を促す。
    """

    def __init__(self, message: str, *, detail: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.detail = detail
        self.status = status


def get_api_key() -> str:
    """環境変数から V2 API キーを取得する。

    新名 JQUANTS_API_KEY を優先し、旧名 JQUANTS_REFRESH_TOKEN にもフォールバックする。
    """
    key = (
        os.environ.get("JQUANTS_API_KEY")
        or os.environ.get("JQUANTS_REFRESH_TOKEN")
        or ""
    ).strip()
    if not key:
        raise JQuantsAuthError(
            "JQUANTS_API_KEY が未設定です",
            detail=".env ファイルに JQUANTS_API_KEY を設定してください",
        )
    if key.lower() in {"your_api_key_here", "your_refresh_token_here", "change-me"}:
        raise JQuantsAuthError(
            "JQUANTS_API_KEY がテンプレートのダミー値のままです",
            detail=".env に J-Quants ダッシュボードで発行した APIキー を貼り付けてください",
        )
    return key


def _parse_subscription_end(body_text: str) -> date | None:
    """400エラー本文から購読期間終了日をパースする。"""
    m = _SUBSCRIPTION_RANGE_RE.search(body_text or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(2), "%Y-%m-%d").date()
    except ValueError:
        return None


def _retry_after_seconds(header_value: str | None) -> float | None:
    """HTTP Retry-After ヘッダ（秒数 or HTTP-date）を秒数に変換する。"""
    if not header_value:
        return None
    header_value = header_value.strip()
    # まずは秒数指定を試す
    try:
        return max(0.0, float(header_value))
    except ValueError:
        pass
    # HTTP-date 形式の場合
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(header_value)
        delta = (dt - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, delta)
    except Exception:
        return None


def fetch_daily_quotes(code: str, weeks: int = 12) -> pd.DataFrame:
    """指定銘柄コード(4桁)の直近 weeks 週間分の日足データをDataFrameで返す。

    戻り値の列:
      Date(datetime), Open, High, Low, Close, Volume

    注意:
      Free プランはデータが約12週間遅延しており、APIの購読期間も過去範囲に限られる。
      購読期間外の日付を指定すると 400 が返るため、エラー本文から終了日をパースして自動リトライする。
    """
    global _subscription_end_cache

    # J-Quants の銘柄コードは 4 桁英数字（近年は 285A/296A のように末尾に英字が入る新規上場銘柄あり）
    if not (isinstance(code, str) and len(code) == 4 and code.isalnum()):
        raise ValueError(f"銘柄コードは4桁英数字で指定してください: {code!r}")
    code = code.upper()

    # キャッシュヒット時は API を叩かずに即返す（レート制限対策の要）
    cached = _daily_quotes_cache.get(code)
    if cached is not None:
        cached_at, cached_df = cached
        if time.time() - cached_at < _DAILY_CACHE_TTL_SEC:
            return cached_df.copy()

    api_key = get_api_key()
    today = datetime.now(timezone.utc).date()
    # 購読終端が判明していればそれを使い、未知なら今日をまず試す
    end = _subscription_end_cache or today
    # 12週分のデータを取るため終点から weeks+2 週さかのぼる（余裕を持って取得）
    start = end - timedelta(weeks=weeks + 2)

    headers = {"x-api-key": api_key}

    def _fetch_with(from_dt: date, to_dt: date) -> list[dict]:
        """指定された範囲でページングしながら全件取得する。"""
        params: dict = {
            "code": code,
            "from": from_dt.strftime("%Y-%m-%d"),
            "to": to_dt.strftime("%Y-%m-%d"),
        }
        collected: list[dict] = []
        pagination_key: Optional[str] = None
        # 429 発生時の指数バックオフ用カウンタ（pagination とは独立）
        _attempts: dict[str, int] = {"n": 0}
        for _ in range(10):
            if pagination_key:
                params["pagination_key"] = pagination_key
            try:
                resp = requests.get(DAILY_ENDPOINT, headers=headers, params=params, timeout=30)
            except requests.RequestException as e:
                raise JQuantsError(f"J-Quants APIへの接続に失敗しました: {e}") from e

            if resp.status_code in (401, 403):
                raise JQuantsAuthError(
                    "J-Quants APIの認証が拒否されました（APIキーが無効の可能性）",
                    detail=resp.text[:400],
                    status=resp.status_code,
                )
            if resp.status_code == 429:
                # レート制限は指数バックオフで最大3回リトライし、それでも駄目なら専用例外を投げる
                retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
                if _attempts["n"] < 3:
                    wait = retry_after if retry_after is not None else (2 ** _attempts["n"])
                    _attempts["n"] += 1
                    time.sleep(wait)
                    continue
                raise JQuantsRateLimitError(
                    "J-Quants APIのレート制限に達しました（短時間のアクセス過多）",
                    detail=resp.text[:400],
                    retry_after=retry_after,
                )
            if resp.status_code == 400:
                # 上位で購読期間外判定＋リトライするため、そのまま例外化
                raise _SubscriptionRangeError(resp.text[:400])
            if resp.status_code != 200:
                raise JQuantsError(
                    f"日足取得失敗: status={resp.status_code} body={resp.text[:200]}"
                )
            body = resp.json()
            collected.extend(body.get("data", []))
            pagination_key = body.get("pagination_key")
            if not pagination_key:
                break
        return collected

    try:
        rows = _fetch_with(start, end)
    except _SubscriptionRangeError as exc:
        # 購読終端を抽出して一度だけリトライする
        sub_end = _parse_subscription_end(exc.body)
        if sub_end is None:
            raise JQuantsError(f"{code}: リクエスト拒否 (400) {exc.body[:200]}") from exc
        _subscription_end_cache = sub_end
        new_end = sub_end
        new_start = new_end - timedelta(weeks=weeks + 2)
        rows = _fetch_with(new_start, new_end)

    if not rows:
        raise JQuantsError(f"銘柄 {code} のデータが取得できませんでした（上場廃止/未対応/購読期間外の可能性）")

    df = pd.DataFrame(rows)
    # V2 は調整後カラムが Adj{O/H/L/C} / AdjVo 名称。フォールバックで素の O/H/L/C/Vo も許容
    def pick(row_cols: list[str], fallbacks: list[str]) -> str:
        for name in row_cols + fallbacks:
            if name in df.columns:
                return name
        raise JQuantsError(f"想定カラムがレスポンスに見つかりません: {row_cols + fallbacks}")

    col_open = pick(["AdjO"], ["O"])
    col_high = pick(["AdjH"], ["H"])
    col_low = pick(["AdjL"], ["L"])
    col_close = pick(["AdjC"], ["C"])
    col_volume = pick(["AdjVo"], ["Vo", "Volume"])

    df = df[["Date", col_open, col_high, col_low, col_close, col_volume]].copy()
    df.columns = ["Date", "Open", "High", "Low", "Close", "Volume"]
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    df = df.sort_values("Date").reset_index(drop=True)

    # 取得データの末尾から直近 weeks 週分のみ採用する（実データの末日を起点に）
    if not df.empty:
        cutoff = df["Date"].iloc[-1] - pd.Timedelta(weeks=weeks)
        df = df[df["Date"] >= cutoff].reset_index(drop=True)

    # 成功時はキャッシュに保存（後続の同銘柄リクエストを API 不要化）
    _daily_quotes_cache[code] = (time.time(), df.copy())
    return df


def cached_codes() -> list[str]:
    """現在 TTL 内の有効なキャッシュを持つ銘柄コード一覧を返す。

    レート制限エラー時に「この銘柄はキャッシュから即取り出せる」旨を案内するために使用する。
    """
    now = time.time()
    return [
        code
        for code, (ts, _df) in _daily_quotes_cache.items()
        if now - ts < _DAILY_CACHE_TTL_SEC
    ]
