"""Streamlit 页面：上传文件、查看结果、人工标注。"""
from __future__ import annotations

import sys
import time
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

# ===== 状态检测 =====
def check_cdp():
    try:
        import requests
        r = requests.get("http://127.0.0.1:9222/json/version", timeout=2)
        if r.status_code == 200:
            return True, r.json().get("Browser", "Chrome")
    except Exception:
        pass
    return False, None

def check_douyin_session() -> tuple[bool, str]:
    """检测抖音登录态是否有效"""
    try:
        import browser_cookie3
        from pathlib import Path
        chrome = Path.home() / "Library/Application Support/Google/Chrome"
        for db in sorted(chrome.glob("*/Cookies"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                cookies = list(browser_cookie3.chrome(cookie_file=str(db)))
                douyin = [c for c in cookies if 'douyin' in c.domain]
                sessionid = [c for c in douyin if c.name == 'sessionid']
                if sessionid:
                    return True, f"Profile {db.parent.name} ({len(douyin)} cookies)"
            except Exception:
                continue
        return False, "未找到登录态"
    except Exception:
        return False, "检测失败"

def get_run_stats() -> dict:
    """读取历史运行统计"""
    import sqlite3
    db_path = DATA_DIR / "database" / "creator_finder.db"
    if not db_path.exists():
        return {"today_runs": 0, "today_creators": 0, "total_creators": 0, "last_run": "从未"}
    try:
        conn = sqlite3.connect(str(db_path))
        today = datetime.now().strftime("%Y-%m-%d")
        today_runs = conn.execute(
            "SELECT COUNT(DISTINCT run_date) FROM daily_results WHERE run_date = ?", (today,)
        ).fetchone()[0]
        today_creators = conn.execute(
            "SELECT COUNT(DISTINCT creator_key) FROM daily_results WHERE run_date = ?", (today,)
        ).fetchone()[0]
        total_creators = conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0]
        last = conn.execute("SELECT MAX(run_date) FROM daily_results").fetchone()[0]
        if last and last != "从未":
            # 取这个日期的最后写入时间
            last_time = conn.execute(
                "SELECT MAX(rowid) FROM daily_results WHERE run_date = ?", (last,)
            ).fetchone()[0]
            # 用 SQLite 文件修改时间来推断时间
            last_run = f"{last}"
        else:
            last_run = "从未"
        # 用数据库文件的修改时间来推算最近一次运行时间
        import os
        mtime = os.path.getmtime(str(db_path))
        last_dt = datetime.fromtimestamp(mtime)
        if last and last != "从未":
            last_run = f"{last} {last_dt:%H:%M}"
        conn.close()
        return {"today_runs": today_runs, "today_creators": today_creators,
                "total_creators": total_creators, "last_run": last_run}
    except Exception:
        return {"today_runs": 0, "today_creators": 0, "total_creators": 0, "last_run": "?"}

cdp_ok, cdp_browser = check_cdp()
session_ok, session_info = check_douyin_session()
stats = get_run_stats()

# 状态面板
stcols = st.columns(4)
with stcols[0]:
    if cdp_ok:
        st.success(f"🟢 CDP 在线")
    else:
        st.error("🔴 CDP 离线")
with stcols[1]:
    if session_ok:
        st.success(f"🟢 已登录抖音")
    else:
        st.error("🔴 需重新登录")
with stcols[2]:
    st.info(f"📊 今日 {stats['today_creators']} 达人")
with stcols[3]:
    st.info(f"⏱ 上次 {stats['last_run']}")

# CDP 离线时显示启动命令
if not cdp_ok:
    with st.expander("🔧 CDP Chrome 启动命令（点击展开）"):
        st.code(
            '"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \\\n'
            '  --remote-debugging-port=9222 \\\n'
            '  "--remote-allow-origins=*" \\\n'
            '  --user-data-dir="/tmp/cdp-chrome-profile" \\\n'
            '  "https://www.douyin.com/" &'
        )
        st.caption("在终端粘贴运行后，刷新本页面。CDP Chrome 窗口不要关，每天开着就行。")

if not session_ok:
    st.warning("⚠️ 抖音登录态丢失。请在 Chrome 中打开 douyin.com 重新登录，然后刷新本页面。")

# 运行限制提示
st.caption("💡 抖音搜索 API 无固定次数限制，但避免短时间内连续跑超过 3 次。如果触发验证，等待 10-15 分钟即可恢复。CDP Chrome 窗口需要一直开着。")

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

        start_time = time.time()
        # 估算：每个关键词约 6 秒（搜索+补全简介），20 个关键词约 120 秒
        kw_count = 20 if use_ai_kw else (len(keywords_final) if keywords_final else 20)
        est_total = max(kw_count * 7, 20)  # 每词 7 秒，最少 20 秒

        progress_bar = st.progress(0, "⏳ 准备中...")
        status_text = st.empty()
        eta_text = st.empty()

        def _eta_text(phase: str, pct: int) -> str:
            elapsed = time.time() - start_time
            remaining = max(est_total - elapsed, 0)
            rm, rs = int(remaining // 60), int(remaining % 60)
            em, es = int(elapsed // 60), int(elapsed % 60)
            if remaining > 60:
                return f"⏱ {phase} | 预计还需 {rm} 分 {rs} 秒 | 已耗时 {em} 分 {es} 秒"
            elif remaining > 0:
                return f"⏱ {phase} | 预计还需 {int(remaining)} 秒 | 已耗时 {em} 分 {es} 秒"
            else:
                return f"⏱ {phase} | 已耗时 {em} 分 {es} 秒"

        try:
            progress_bar.progress(5, "🤖 生成关键词...")
            status_text.text(f"共 {kw_count} 个关键词，预计 {int(est_total // 60)} 分 {int(est_total % 60)} 秒")
            eta_text.text(_eta_text("准备中", 5))

            progress_bar.progress(15, "🔍 搜索中 (1/3)...")
            status_text.text(f"CDP 连接中，逐关键词搜索，每个约 6-8 秒")
            eta_text.text(_eta_text("搜索中", 15))

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

            progress_bar.progress(85, "📊 评分分类中...")
            status_text.text(f"去重后 {summary['unique']} 条，正在评分分类")
            eta_text.text(_eta_text("评分中", 85))

            progress_bar.progress(95, "💾 保存...")
            eta_text.text(_eta_text("保存中", 95))

            elapsed = time.time() - start_time
            em, es = int(elapsed // 60), int(elapsed % 60)
            progress_bar.progress(100, "✅ 完成")
            status_text.text("")
            eta_text.text(f"✅ 总耗时 {em} 分 {es} 秒 | {summary['unique']} 条达人已就绪")
            st.session_state["last_summary"] = summary
            st.success(f"完成！{summary['unique']} 条达人，耗时 {em} 分 {es} 秒")
        except Exception as e:
            elapsed = time.time() - start_time
            progress_bar.progress(100, "❌ 失败")
            status_text.text("")
            eta_text.text(f"❌ 运行 {int(elapsed)} 秒后失败: {e}")
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
                "排名", "是否新达人", "推荐等级", "AI评分",
                "评分明细", "评分理由", "达人昵称", "抖音达人类型", "粉丝数",
                "推荐产品", "合作建议",
                "公开联系方式", "联系方式类型",
                "达人主页链接", "代表视频链接",
                "风险点", "搜索关键词", "数据来源", "提取状态",
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
            all_col_config = {}
            if "达人主页链接" in df_all.columns:
                all_col_config["达人主页链接"] = st.column_config.LinkColumn("达人主页链接", display_text="🔗 打开")
            if "代表视频链接" in df_all.columns:
                all_col_config["代表视频链接"] = st.column_config.LinkColumn("代表视频链接", display_text="▶ 打开")
            st.dataframe(df_all, width="stretch", height=500, column_config=all_col_config or None)
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
            status_col_config = {}
            if "主页链接" in df_status.columns:
                status_col_config["主页链接"] = st.column_config.LinkColumn("主页链接", display_text="🔗 打开")
            st.dataframe(df_status, width="stretch", height=400, column_config=status_col_config or None)

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

st.caption(f"🕐 页面刷新时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
