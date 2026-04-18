# VWAPバックテスト比較Webアプリ

Flask + Jinja2 + Plotly で J-Quants V2 API の日足データを使い、
VWAP / 出来高加重 / 移動平均クロス の3戦略を比較するバックテストツールです。

## 機能

- 最大10銘柄 × 3戦略を同時にバックテスト（4桁英数字コード対応: `7203`, `285A` 等）
- 結果をヒートマップで一覧、セルクリックで銘柄×戦略の詳細ページへ
- 詳細ページ: ローソク足 + インジケータ重ね描画、損益曲線、トレード履歴
- 損切り / 利確オプション（エントリ価格からの% 指定、ギャップ時は寄付き約定）
- J-Quants Free プランのレート制限対策（プロセス内キャッシュ 1h / 呼び出し間 2秒待機 / 指数バックオフ）
- スマホ対応（タップ領域 44px、ヒートマップ・ローソク足のモバイル最適化）

## セットアップ

### 1. J-Quants API キーを取得

[J-Quants ダッシュボード](https://jpx-jquants.com/ja/spec/quickstart) にログインし、
「APIキーを発行・取得」から V2 API キーを発行してください。

### 2. `.env` を作成

`.env.example` をコピーして自分のキーを設定します。

```bash
cp .env.example .env
# エディタで .env を開いて JQUANTS_API_KEY=... を書き換える
```

- `JQUANTS_API_KEY` : J-Quants ダッシュボードで発行した V2 API キー
- `FLASK_SECRET_KEY` : セッション暗号化用。任意のランダム文字列

### 3. 起動

```bash
docker compose up -d
```

ブラウザで http://localhost:5000 を開きます。

`.env` を更新した後に反映するには `restart` ではなく `--force-recreate` が必要です:

```bash
docker compose up -d --force-recreate web
```

## J-Quants 利用規約について

このツールは **個人の私的利用** 前提で設計されています。J-Quants 利用規約により、
以下の行為は **禁止** されているので注意してください。

- API キーを第三者に共有・使わせる（キーは各自で取得して `.env` に設定）
- 取得した株価データを第三者に配信・再配布する（CSV/PNG/スクショ含む）
- このアプリをホスティングして他人にアクセスさせる（データ提供に該当するため NG）

ソースコードの共有自体は問題ありません。このリポジトリには生データや API キーは含まれて
いません（`.gitignore` で `.env` / `*.csv` / `screenshots/` 等を除外済み）。

詳しくは J-Quants 公式の利用規約・ヘルプを参照してください:
https://jpx-jquants.com/ja/help/usage

## ファイル構成

```
├── app.py                    Flask メイン（ルーティング / バックテスト実行）
├── fetch_data.py             J-Quants V2 API クライアント（キャッシュ・リトライ含む）
├── strategies/
│   ├── _engine.py            共通バックテストエンジン（SL/TP intra-bar 約定）
│   ├── vwap.py               VWAP 戦略
│   ├── volume_price.py       出来高加重価格戦略
│   └── ma_cross.py           移動平均クロス戦略
├── templates/                Jinja2 テンプレート
├── static/style.css          スタイル（モバイル対応）
├── Dockerfile / docker-compose.yml
└── .env.example              環境変数テンプレート（ダミー値）
```

## レート制限について

Free プランは **5 リクエスト/分** が上限で、超過すると約5分ブロックされます。本アプリでは:

1. 同一銘柄の日足を 1時間プロセス内キャッシュ（再取得は API 不要）
2. 銘柄ごとに 2秒の間隔を空けて呼び出し
3. 429 発生時は指数バックオフで最大3回リトライ
4. それでも駄目なら専用エラー画面でキャッシュ済み銘柄と再試行可能時刻を案内

## ライセンス

個人学習目的のサンプルプロジェクトです。
