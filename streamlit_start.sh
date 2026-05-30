#!/bin/bash
set -e

# 硬编码默认配置，保证 Streamlit 能读到
SEARCH_API_PROVIDER="${SEARCH_API_PROVIDER:-bocha}"
SEARCH_API_KEY="${SEARCH_API_KEY:-sk-d6d73e45bfd748b0ba75f5111d44c803}"
ENABLE_AI="${ENABLE_AI:-false}"

# 写入 .streamlit/secrets.toml（Streamlit 启动时优先读这个）
mkdir -p .streamlit
cat > .streamlit/secrets.toml << EOF
SEARCH_API_PROVIDER = "${SEARCH_API_PROVIDER}"
SEARCH_API_KEY = "${SEARCH_API_KEY}"
ENABLE_AI = ${ENABLE_AI}
EOF

export SEARCH_API_PROVIDER SEARCH_API_KEY ENABLE_AI

echo "✅ SEARCH_API_PROVIDER=${SEARCH_API_PROVIDER}"
echo "✅ SEARCH_API_KEY=${SEARCH_API_KEY:0:12}..."
echo "✅ ENABLE_AI=${ENABLE_AI}"

streamlit run app.py --server.enableCORS false --server.enableXsrfProtection false
