# Python 3.11-slim ベースの軽量イメージ
# 依存インストール層とアプリ層を分離し、requirements.txt 変更時のみ再ビルドされるようにする
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FLASK_APP=app.py \
    FLASK_RUN_HOST=0.0.0.0 \
    FLASK_RUN_PORT=5000

WORKDIR /app

# 依存のみ先にコピーしてインストール（レイヤーキャッシュを効かせる）
COPY requirements.txt ./
RUN pip install -r requirements.txt

# アプリ本体
COPY . .

EXPOSE 5000

# 開発用途として flask run でホットリロードを有効化
CMD ["flask", "run", "--debug"]
