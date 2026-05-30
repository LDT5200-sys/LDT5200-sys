#!/bin/bash
# Streamlit 启动脚本（Codespaces / 本地通用）
# 自动从 .env 读取配置，缺失时给出明确提示

if [ -f .env ]; then
  export $(grep -v '^#' .env | grep -v '^$' | xargs)
  echo "✅ 已加载 .env 配置"
else
  echo "⚠️  未找到 .env 文件，请从 .env.example 复制并填入 API 密钥"
  echo "   cp .env.example .env"
fi

echo "SEARCH_API_PROVIDER=${SEARCH_API_PROVIDER:-未配置}"
echo "SEARCH_API_KEY=${SEARCH_API_KEY:0:10}..."

streamlit run app.py --server.enableCORS false --server.enableXsrfProtection false
