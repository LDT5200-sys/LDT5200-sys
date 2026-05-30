"""Streamlit 页面：上传文件、查看结果、人工标注。"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

from src.main import run
from src.models.schemas import CREATOR_STATUS
from src.storage.sqlite_store import list_creators_with_status, update_creator_status
from src.utils.config_loader import DATA_DIR, seed_keywords_config

st.set_page_config(page_title="龙牙达人发现", layout="wide")
st.title("🎯 龙牙外部达人自动发现")

# CDP 状态检测
def check_cdp():
    try:
        import requests
        r = requests.get("http://127.0.0.1:9222/json/version", timeout=2)
        if r.status_code == 200:
            return True, r.json().get("Browser", "Chrome")
    except Exception:
        pass
    return False, None

cdp_ok, cdp_browser = check_cdp()
if cdp_ok:
    st.success(f"🟢 CDP Chrome 在线 ({cdp_browser}) — 可自动搜索抖音")
else:
    st.error("🔴 CDP Chrome 离线 — 请在终端运行以下命令后刷新页面：")
    st.code(
        '"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\\n'
        '  --remote-debugging-port=9222 \\\n'
        '  "--remote-allow-origins=*" \\\n'
        '  --user-data-dir="/tmp/cdp-chrome-profile" \\\n'
        '  "https://www.douyin.com/" &'
    )

input_dir = DATA_DIR / "input"
output_dir = DATA_DIR / "output"
input_dir.mkdir(parents=True, exist_ok=True)
output_dir.mkdir(parents=True, exist_ok=True)

# ===== 侧边栏：上传 + 运行 =====
with st.sidebar:
    st.header("📤 上传数据文件")
    uploads = st.file_uploader(
        "支持 Excel / CSV / JSON（抖音导出/社媒助手/星图等）",
        type=["xlsx", "xls", "csv", "json"],
        accept_multiple_files=True,
        key="file_uploader",
    )
    if uploads:
        for up in uploads:
            (input_dir / up.name).write_bytes(up.getbuffer())
        st.success(f"已上传 {len(uploads)} 个文件")

    st.header("🔑 关键词")
    use_ai_kw = st.checkbox("🤖 AI 自动优化关键词", value=False,
        help="AI 根据品牌需求 + 历史反馈自动生成搜索关键词，无需手动选择。需配置 OPENAI_API_KEY。")

    if use_ai_kw:
        # AI 模式：显示反馈输入
        feedback_notes = st.text_area(
            "📝 给 AI 的需求提示（可选）",
            value="",
            placeholder="例如：多找微胖男装测评博主、优先女穿男装方向、排除纯娱乐号...",
            height=80,
        )
        st.caption("AI 会结合品牌定位 + 你的需求 + 历史学习记录，自动生成最优搜索词")
    else:
        # 手动模式
        seeds = seed_keywords_config().get("seed_keywords", [])
        chosen = st.multiselect("选择关键词", seeds, default=seeds[:8])
        custom_kw = st.text_input("追加关键词（英文逗号分隔）", value="", placeholder="例如：战术裤测评,通勤穿搭")
        feedback_notes = None

    st.header("⚡ 运行")
    skip_ai = st.checkbox("跳过 AI 评分（仅规则评分）", value=True)

    if st.button("开始筛选", type="primary", use_container_width=True):
        keywords_final = None
        fb = None
        if not use_ai_kw:
            kw_list = list(chosen) if chosen else []
            if custom_kw.strip():
                kw_list.extend([k.strip() for k in custom_kw.split(",") if k.strip()])
            keywords_final = kw_list if kw_list else None
        else:
            fb = [feedback_notes] if feedback_notes.strip() else None

        with st.spinner("正在搜索抖音 → 清洗 → 评分 → 输出报表..."):
            try:
                summary = run(
                    skip_keyword_expand=True,
                    skip_ai=skip_ai,
                    discover=False,
                    douyin_import=False,
                    enrich_remote=False,
                    keywords_override=keywords_final,
                    use_ai_keywords=use_ai_kw,
                    feedback_notes=fb,
                )
                st.session_state["last_summary"] = summary
                st.success(f"完成！{summary['unique']} 条达人")
            except Exception as e:
                st.error(f"运行失败：{e}")

# ===== 主区域 =====
summary = st.session_state.get("last_summary")
if summary:
    c1, c2, c3 = st.columns(3)
    c1.metric("有效候选", summary["unique"])
    c2.metric("原始数据", summary["raw_rows"])
    c3.metric("疑似重复", summary["suspects"])

# ===== Top 推荐 =====
st.subheader("🏆 今日 Top 推荐")
xlsx_files = sorted(output_dir.glob("daily_creator_result_*.xlsx"), reverse=True)

if not xlsx_files:
    st.info("还没有结果。左侧上传文件后点击「开始筛选」，或在本地运行 python src/main.py --skip-ai --skip-keyword-expand")
else:
    latest = xlsx_files[0]
    st.caption(f"最新结果：{latest.name}")

    tab1, tab2, tab3 = st.tabs(["Top 推荐", "全部候选", "人工标注"])

    with tab1:
        try:
            df_top = pd.read_excel(latest, sheet_name="今日Top推荐")
            cols = [c for c in [
                "排名", "推荐等级", "AI评分", "达人昵称", "粉丝数",
                "内容类型", "推荐产品", "合作建议",
                "公开联系方式", "联系方式类型",
                "达人主页链接", "代表视频链接",
                "提取状态", "缺失原因", "搜索关键词",
            ] if c in df_top.columns]

            column_config = {}
            if "达人主页链接" in df_top.columns:
                column_config["达人主页链接"] = st.column_config.LinkColumn("达人主页链接", display_text="🔗 打开")
            if "代表视频链接" in df_top.columns:
                column_config["代表视频链接"] = st.column_config.LinkColumn("代表视频链接", display_text="▶ 打开")

            st.dataframe(df_top[cols], width="stretch", height=500, column_config=column_config or None)
        except Exception as e:
            st.error(f"读取失败：{e}")

        with open(latest, "rb") as f:
            st.download_button("📥 下载完整 Excel", data=f.read(), file_name=latest.name,
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    with tab2:
        try:
            df_all = pd.read_excel(latest, sheet_name="全部候选")
            st.dataframe(df_all, width="stretch", height=500)
        except Exception as e:
            st.error(f"读取失败：{e}")

    with tab3:
        st.subheader("人工状态管理")
        status_rows = list_creators_with_status()
        if not status_rows:
            st.info("SQLite 里还没有达人记录，先跑一次筛选。")
        else:
            df_status = pd.DataFrame(status_rows)
            df_status = df_status.rename(columns={
                "creator_name": "达人昵称", "platform": "平台",
                "latest_follower_count": "粉丝数", "latest_score": "AI评分",
                "priority_level": "推荐等级", "status": "当前状态",
                "creator_profile_url": "主页链接",
                "contact_text": "公开联系方式", "extraction_status": "提取状态",
            })
            st.dataframe(df_status, width="stretch", height=400)

            col_a, col_b, col_c = st.columns(3)
            with col_a:
                target = st.selectbox(
                    "选择达人",
                    [r["creator_key"] for r in status_rows],
                    format_func=lambda k: next((r['creator_name'] for r in status_rows if r['creator_key'] == k), k),
                )
            with col_b:
                new_status = st.selectbox("新状态", CREATOR_STATUS)
            with col_c:
                note = st.text_input("备注（可选）", value="")
            if st.button("💾 保存状态"):
                update_creator_status(target, new_status, note)
                st.success("已保存")

st.caption(f"🕐 {datetime.now():%Y-%m-%d %H:%M:%S}")
