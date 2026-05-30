"""Streamlit 本地页面：上传文件、数据发现、AI 评分、人工状态管理。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

from src.utils.config_loader import DATA_DIR, load_env, seed_keywords_config
# 清除可能因导入时调用而缓存的空配置，强制从 st.secrets 重新读
load_env.cache_clear()

from src.main import run
from src.models.schemas import CREATOR_STATUS
from src.storage.sqlite_store import (
    list_creators_with_status,
    update_creator_status,
)

st.set_page_config(page_title="龙牙外部达人发现", layout="wide")
st.title("龙牙外部达人自动发现与AI评分系统")

input_dir = DATA_DIR / "input"
output_dir = DATA_DIR / "output"
input_dir.mkdir(parents=True, exist_ok=True)
output_dir.mkdir(parents=True, exist_ok=True)

# ===== 顶部 Tabs =====
tab_run, tab_status = st.tabs(["数据发现 / 评分", "人工状态管理"])

with tab_run:
    with st.sidebar:
        st.header("1. 上传本地数据文件（可选）")
        uploads = st.file_uploader(
            "支持 Excel / CSV / JSON，可多选",
            type=["xlsx", "xls", "csv", "json"],
            accept_multiple_files=True,
        )
        if uploads:
            for up in uploads:
                (input_dir / up.name).write_bytes(up.getbuffer())
            st.success(f"已上传 {len(uploads)} 个文件到 {input_dir}")

        st.header("2. 关键词")
        seeds = seed_keywords_config().get("seed_keywords", [])
        chosen = st.multiselect(
            "本次使用的关键词（默认全部种子词）",
            seeds,
            default=seeds,
        )
        custom_kw = st.text_input("追加自定义关键词（用英文逗号分隔）", value="")

        st.header("3. 运行模式")
        mode = st.radio(
            "选择数据来源",
            options=[
                "只处理本地表格",
                "抖音数据源导入",
                "公开搜索发现",
                "本地表格 + 公开搜索",
            ],
            index=0,
        )
        skip_kw = st.checkbox("跳过关键词扩展", value=True)
        skip_ai = st.checkbox("跳过 AI（仅规则评分）", value=True)
        enrich_remote = st.checkbox(
            "对抖音链接抓 og:meta（合规模式）",
            value=False,
            help="仅访问公开页面读取 og:title/description；遇登录、验证码、风控立即放弃。",
        )

        douyin_api_mode = False
        if mode == "抖音数据源导入":
            douyin_api_mode = st.checkbox(
                "包含 API 模式",
                value=False,
                help="启用 douyin_source.yaml 中的 api_mode，调用抖音/星图/第三方 API。需在 .env 配置 DOUYIN_DATA_PROVIDER 等。",
            )

        # 提示当前 API 配置情况
        env = load_env()
        has_search_key = bool(env.get("SEARCH_API_KEY"))
        has_douyin_key = bool(env.get("DOUYIN_API_KEY") or env.get("DOUYIN_ACCESS_TOKEN"))
        if mode in ("公开搜索发现", "本地表格 + 公开搜索"):
            if has_search_key:
                st.success(f"已检测到搜索 API: {env.get('SEARCH_API_PROVIDER') or env.get('SEARCH_PROVIDER') or '未指定'}")
            else:
                st.warning("当前未配置搜索API，无法自动发现公开网页结果。请配置 SEARCH_API_KEY。")
        if mode in ("抖音数据源导入",):
            if not has_douyin_key:
                st.info("未配置 DOUYIN_API_KEY，仅使用本地导入模式（data/input/douyin/ 下的文件）。若需 API 模式请勾选上方的「包含 API 模式」并在 .env 填入凭证。")
            elif douyin_api_mode:
                st.success(f"已检测到抖音数据源: {env.get('DOUYIN_DATA_PROVIDER') or 'generic'}")

        if st.button("开始筛选", type="primary", use_container_width=True):
            keywords = list(chosen)
            if custom_kw.strip():
                keywords.extend([k.strip() for k in custom_kw.split(",") if k.strip()])
            kw_override = keywords if keywords else None

            discover = mode in ("公开搜索发现", "本地表格 + 公开搜索")
            douyin_import = mode == "抖音数据源导入"
            with st.spinner("正在跑数据发现 → 清洗 → AI 评分 → 出报表..."):
                try:
                    summary = run(
                        skip_keyword_expand=skip_kw,
                        skip_ai=skip_ai,
                        discover=discover,
                        douyin_import=douyin_import,
                        enrich_remote=enrich_remote,
                        keywords_override=kw_override,
                    )
                    st.session_state["last_summary"] = summary
                    st.success("运行完成")
                except Exception as e:
                    st.error(f"运行失败：{e}")

    summary = st.session_state.get("last_summary")
    if summary:
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("原始行数", summary["raw_rows"])
        c2.metric("有效候选", summary["normalized"])
        c3.metric("去重后", summary["unique"])
        c4.metric("疑似重复", summary["suspects"])
        c5.metric("扩展关键词", summary["expanded_keywords"])

    st.subheader("今日 Top 推荐")
    xlsx_files = sorted(output_dir.glob("daily_creator_result_*.xlsx"), reverse=True)
    if not xlsx_files:
        st.info("还没有结果，左侧上传文件 / 选模式后点击「开始筛选」。")
    else:
        latest = xlsx_files[0]
        st.caption(f"最新结果文件：{latest.name}")
        try:
            df_top = pd.read_excel(latest, sheet_name="今日Top推荐")
            preferred = [
                "排名", "推荐等级", "AI评分", "达人昵称", "平台", "粉丝数",
                "内容类型", "推荐产品", "公开联系方式", "联系方式类型", "联系方式位置",
                "达人主页链接", "代表视频链接", "链接类型",
                "提取状态", "缺失原因",
                "推荐理由", "下一步动作",
                "搜索关键词", "数据来源", "数据来源链接",
            ]
            cols = [c for c in preferred if c in df_top.columns]
            # 让链接列可点击
            column_config = {}
            if "达人主页链接" in df_top.columns:
                column_config["达人主页链接"] = st.column_config.LinkColumn(
                    "达人主页链接", display_text="打开主页"
                )
            if "代表视频链接" in df_top.columns:
                column_config["代表视频链接"] = st.column_config.LinkColumn(
                    "代表视频链接", display_text="打开视频"
                )
            if "数据来源链接" in df_top.columns:
                column_config["数据来源链接"] = st.column_config.LinkColumn(
                    "数据来源链接", display_text="来源"
                )
            st.dataframe(
                df_top[cols], use_container_width=True, height=460,
                column_config=column_config if column_config else None,
            )
        except Exception as e:
            st.error(f"读取 Top 推荐失败：{e}")

        with open(latest, "rb") as f:
            st.download_button(
                "下载完整结果 Excel",
                data=f.read(),
                file_name=latest.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

with tab_status:
    st.subheader("人工状态管理")
    status_rows = list_creators_with_status()
    if not status_rows:
        st.info("SQLite 里还没有达人记录。先在「数据发现 / 评分」页跑一次。")
    else:
        df_status = pd.DataFrame(status_rows)
        df_status = df_status.rename(columns={
            "creator_key": "达人Key", "creator_name": "达人昵称", "platform": "平台",
            "latest_follower_count": "粉丝数", "latest_score": "AI评分",
            "priority_level": "推荐等级", "status": "当前状态", "last_seen_date": "最近出现",
            "contact_visible": "有公开联系方式", "contact_text": "公开联系方式",
            "extraction_status": "提取状态", "missing_reason": "缺失原因",
            "creator_profile_url": "主页链接", "url_type": "链接类型",
        })
        st.dataframe(df_status, use_container_width=True, height=380)

        st.markdown("**修改单个达人状态**")
        keys = [r["creator_key"] for r in status_rows]
        label_map = {
            r["creator_key"]: f"{r['creator_name']}（{r['platform']}/{r['priority_level']}）"
            for r in status_rows
        }
        target = st.selectbox("选择达人", keys, format_func=lambda k: label_map.get(k, k))
        new_status = st.selectbox("新状态", CREATOR_STATUS)
        note = st.text_input("备注（可选）", value="")
        if st.button("保存状态"):
            try:
                update_creator_status(target, new_status, note)
                st.success(f"已更新：{label_map.get(target, target)} → {new_status}")
            except Exception as e:
                st.error(f"更新失败：{e}")

st.caption(f"运行环境时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
