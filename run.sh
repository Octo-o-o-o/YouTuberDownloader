#!/usr/bin/env bash
# 一键启动 YouTube 博主数据采集工具
set -e

cd "$(dirname "$0")"

# 创建/激活虚拟环境
if [ ! -d ".venv" ]; then
    echo "[setup] Creating virtualenv..."
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# 安装核心依赖（不含人脸识别，那部分是可选的）
if [ ! -f ".venv/.installed" ] || [ requirements.txt -nt .venv/.installed ]; then
    echo "[setup] Installing core requirements..."
    pip install --upgrade pip -q
    pip install -r requirements.txt
    touch .venv/.installed
fi

# 提示可选依赖
if ! .venv/bin/python -c "import insightface" 2>/dev/null; then
    echo ""
    echo "[note] InsightFace 未安装 — 博主照片会走'无人脸验证'降级模式"
    echo "       启用：.venv/bin/pip install -r requirements-extras.txt"
    echo ""
fi

# 加载 .env (如果存在)
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

echo ""
echo "===================================="
echo "  YouTube 博主数据采集工具"
echo "  访问: http://${HOST}:${PORT}"
echo "===================================="
echo ""

exec uvicorn backend.main:app --host "$HOST" --port "$PORT" --reload
