# 龙牙外部达人自动发现与AI评分系统（本地 MVP）

面向抖音男装直播 / 短视频投放团队的本地工具。每天自动从本地表格、JSON、API
预留接口中收集候选达人，统一清洗后用大模型分类、打分，输出本地 Excel 报表，
并把结果写入本地 SQLite 用于历史去重和复盘。

> 本工具仅做**已合规导出**的数据汇总与评分，不做自动私信、不抓取非公开个人信息、
> 不绕过登录/验证码/平台风控。

## 1. 安装依赖

```bash
cd creator_finder
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. 配置 .env

```bash
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY、OPENAI_BASE_URL、OPENAI_MODEL
# base_url 可换成硅基流动 / 云雾 / OpenRouter 等任意 OpenAI 兼容地址
```

如果暂时不接 AI，把 `ENABLE_AI=false`，系统会跳过 AI，只跑规则评分。

## 3. 放入输入文件

把任意来源导出的表格放到 `data/input/`，支持：

- `*.xlsx` / `*.xls`
- `*.csv`
- `*.json`（数组或包含 `data` / `list` 字段）

字段无需提前对齐，`config/field_mapping.yaml` 里登记了常见列名（中英都行），
程序会自动匹配。匹配不上的列会原样保留到 `raw_data`。

也可以直接用示例文件：

```bash
python scripts/generate_sample_input.py
# 会在 data/input/ 下生成 sample_input.xlsx
```

## 4. 运行命令行版本

```bash
# 1) 只处理本地表格（Excel/CSV/JSON）
python src/main.py --skip-ai --skip-keyword-expand

# 2) 抖音数据源导入（data/input/douyin/ 下的导出文件）
python src/main.py --douyin-import --skip-ai

# 3) 公开搜索发现（site:douyin.com 关键词搜索）
python src/main.py --discover

# 4) 搜索发现 + 抖音数据源 + AI评分
python src/main.py --discover --douyin-import

# 5) 加上 og:meta 抓取（仅访问公开页面）
python src/main.py --discover --enrich-remote

# 也可以用包装脚本（适合 cron）
python run_daily.py
```

## 4a. 抖音数据源导入说明

将抖音搜索页手动导出的 CSV/Excel/HTML 文件放入 `data/input/douyin/`：

- 文件名任意，程序自动读取最新文件。
- 字段映射优先走 `config/douyin_source.yaml` 中的 `field_mapping_overrides`，
  没命中的列再走 `config/field_mapping.yaml` 通用映射。
- 如果一条记录既没有主页链接也没有视频链接，会被标记为「缺少关键链接」，
  评分时合作可行性大幅降分，且无法进入 S 级。

如果要启用 API 模式（调用抖音开放平台 / 星图 / 第三方达人搜索 API），
在 `config/douyin_source.yaml` 中把 `api_mode.enabled` 改为 `true`，
并在 `.env` 中填入对应的凭证。

## 5. 运行 Streamlit 页面

```bash
streamlit run app.py
```

页面功能：上传文件、选择关键词、一键筛选、查看 Top 推荐、下载 Excel、
人工修改达人状态（合适/不合适/待联系/已联系/已报价/已合作/淘汰），
状态写回本地 SQLite。

## 6. 输出位置

- `data/processed/expanded_keywords_YYYYMMDD.xlsx`：扩展关键词
- `data/output/daily_creator_result_YYYYMMDD.xlsx`：4 个 Sheet（Top 推荐 / 全部候选 / 关键词效果 / 疑似重复）
- `data/output/daily_report_YYYYMMDD.md`：日报
- `data/database/creator_finder.db`：SQLite 数据库（creators / videos / daily_results / status_changes）
- `data/raw/`：原始合并文件
- `logs/`：运行日志（loguru 自动滚动）

## 7. 后续接飞书多维表

`src/feishu/bitable_placeholder.py` 已留好接口：

```python
from src.feishu.bitable_placeholder import push_to_bitable
push_to_bitable(df, app_token=..., table_id=...)
```

第一版默认只打日志、不真正写入。需要接入时填 `.env` 的 `FEISHU_*`
变量，并在 `bitable_placeholder.py` 内补全 `_call_feishu_api`。

## 8. 目录结构

```
creator_finder/
├── README.md
├── requirements.txt
├── .env.example
├── config/                  # 配置：品牌、关键词、评分、数据源、字段映射
├── data/
│   ├── input/               # 放原始导出文件
│   ├── raw/                 # 合并后的原始备份
│   ├── processed/           # 关键词扩展、清洗中间产物
│   ├── output/              # 每日 Excel + Markdown 日报
│   └── database/            # SQLite
├── src/
│   ├── main.py              # 命令行主入口
│   ├── models/schemas.py    # pydantic 标准字段
│   ├── keyword/             # 关键词扩展
│   ├── data_sources/        # 可插拔数据源
│   ├── cleaner/             # 字段归一化 + 去重
│   ├── ai/                  # LLM 客户端 + 分类 + 评分
│   ├── storage/             # SQLite + Excel 输出
│   ├── reports/             # Markdown 日报
│   ├── feishu/              # 飞书多维表占位
│   └── utils/               # logger / 时间工具
├── app.py                   # Streamlit 页面
├── run_daily.py             # cron 友好入口
└── scripts/generate_sample_input.py
```

## 8. 接入真实数据源需要哪些账号权限

| 数据源 | 用途 | 所需权限 |
|---|---|---|
| **搜索 API** (Serper / SerpAPI / Bing / **博查**) | site:douyin.com 关键词公开搜索 | 在对应服务商注册获取 API Key。**博查国内可用**，微信扫码注册 https://open.bochaai.com，免费额度。配置 `SEARCH_API_PROVIDER=bocha` + `SEARCH_API_KEY` |
| **抖音开放平台** | 获取公开视频/账号信息（昵称、主页、粉丝、视频标题等） | 企业认证 → 创建应用 → 审核通过 → 获取 access_token。配置 `DOUYIN_DATA_PROVIDER=douyin_open` + `DOUYIN_API_KEY` / `DOUYIN_ACCESS_TOKEN` |
| **星图** | 获取达人报价、粉丝画像、公开商务联系方式 | 星图账号 + 广告主认证。API 模式需 `XINGTU_DATA_PROVIDER` + `XINGTU_API_KEY`；导出表模式直接将星图 Excel 放到 `data/input/xingtu/` |
| **第三方达人工具** (新榜/蝉妈妈/果集等) | 补全达人数据 | `THIRD_PARTY_PROVIDER` + `THIRD_PARTY_API_KEY`（API 模式）；或导出 Excel/CSV 放 `data/input/third_party/`（导出表模式） |
| **手动导出** | 直接使用抖音搜索页/工具导出文件 | 无需账号。将文件放到 `data/input/douyin/` 即可，程序自动解析 |

> 注意：所有 API 调用都走 `.env` 读取凭证，不做自动私信、不做绕过登录、不做非公开数据抓取。

## 9. 设计要点

- **AI 调不通不会让程序崩**：`creator_scorer` 始终保留规则评分，AI 只是叠加。
- **字段缺失走兜底**：粉丝/点赞缺失 → 中性分；联系方式缺失 → 不淘汰只降可行性。
- **历史去重在 SQLite**：通过 `creator_key`（profile_url 或 platform+name 的 hash）。
- **历史已淘汰达人**：默认不进入 Top 名单，除非新评分 ≥ `revive_threshold`（默认 80）。
- **配置驱动**：换品牌、换关键词、换评分逻辑都改 yaml，不动代码。
