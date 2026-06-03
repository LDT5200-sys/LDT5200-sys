# 龙牙外部达人自动发现与AI评分系统

## 启动方式
- 页面：`streamlit run app.py` → http://localhost:8501
- 命令行：`python src/main.py --skip-ai --skip-keyword-expand`
- CDP Chrome 启动后（状态栏🟢），页面一键筛选即可

## CDP Chrome 启动命令（每次开机跑一次）
```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  "--remote-allow-origins=*" \
  --user-data-dir="/tmp/cdp-chrome-profile" \
  "https://www.douyin.com/" &
```

## 核心依赖
- .venv（Python 3.9+，依赖见 requirements.txt）
- DeepSeek API（关键词优化 + 评分理由生成）
- CDP Chrome（抖音搜索数据源）
- 社媒助手 Chrome 扩展（备用数据源）

## 常用命令
- 完整搜索+评分：`python src/main.py --skip-ai --skip-keyword-expand`
- 只处理本地文件：`python src/main.py --douyin-import --skip-ai`
- 页面模式：左侧勾选 AI 关键词 → 开始筛选

## 数据流
CDP Chrome → douyin_cdp_source → normalizer → deduplicator → classifier → scorer → Excel + SQLite

## 输出
- data/output/daily_creator_result_YYYYMMDD.xlsx
- data/output/daily_report_YYYYMMDD.md
- data/database/creator_finder.db
