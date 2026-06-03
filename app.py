"""Streamlit 页面：上传文件、查看结果、人工标注。"""
from __future__ import annotations

import sys
import time
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

from src.main import run
from src.models.schemas import CREATOR_STATUS
from src.storage.excel_writer import _fmt_contact, _fmt_follower_tier, _fmt_followers, _fmt_creator_tier, _fmt_like_ratio_and_flag
from src.storage.sqlite_store import list_creators_with_status, update_creator_status
from src.utils.config_loader import DATA_DIR, seed_keywords_config

st.set_page_config(page_title="龙牙达人发现", layout="wide")
st.title("🎯 龙牙外部达人自动发现")


def _parse_followers(s: str) -> int:
    """将格式化粉丝数（如 10.5w, 1234）转回整数用于筛选"""
    if not s or s == "":
        return 0
    s = str(s).strip().lower()
    if s.endswith("w") or s.endswith("万"):
        try:
            return int(float(s[:-1]) * 10000)
        except ValueError:
            return 0
    try:
        return int(float(s.replace(",", "")))
    except ValueError:
        return 0


def _apply_follower_filter(df, col: str, selected_tiers: list[str]) -> "pd.DataFrame":
    """根据粉丝量筛选 DataFrame，col 可以是格式化字符串或原始整数"""
    if not selected_tiers:
        return df
    ranges = {
        "不足1万": (0, 10000),
        "1-5万": (10000, 50000),
        "5-10万": (50000, 100000),
        "10-50万": (100000, 500000),
        "50-100万": (500000, 1000000),
        "100万+": (1000000, 999999999),
    }
    # 判断列是格式化字符串还是整数
    sample = df[col].dropna()
    if len(sample) > 0:
        first = sample.iloc[0]
        if isinstance(first, str) and ("w" in first or "万" in first):
            df["_follower_raw"] = df[col].apply(_parse_followers)
        else:
            df["_follower_raw"] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    else:
        return df
    masks = []
    for t in selected_tiers:
        lo, hi = ranges[t]
        masks.append((df["_follower_raw"] >= lo) & (df["_follower_raw"] < hi))
    if masks:
        combined = masks[0]
        for m in masks[1:]:
            combined = combined | m
        df = df[combined]
    df = df.drop(columns=["_follower_raw"])
    return df


def _tier_from_grading(grading: str) -> str:
    """从粉丝量分级推导头腰尾量级。"""
    if not grading or grading == "-":
        return "-"
    if "100万" in str(grading):
        return "头部"
    if "10万" in str(grading) or "50万" in str(grading):
        return "腰部"
    return "尾部"


def _enrich_new_fields(df: pd.DataFrame) -> pd.DataFrame:
    """给旧 Excel 数据补充量级/赞粉比/流量质量字段（如果缺失）。"""
    # 量级：从粉丝量分级推导
    if "量级" not in df.columns:
        if "粉丝量分级" in df.columns:
            df["量级"] = df["粉丝量分级"].apply(_tier_from_grading)
        else:
            df["量级"] = "-"
    # 赞粉比 & 流量质量
    if "赞粉比" not in df.columns:
        has_likes = "点赞数" in df.columns
        has_followers = "粉丝数" in df.columns
        if has_likes and has_followers:
            # 粉丝数可能是 "4.0w" 格式，先转整数；点赞数可能是空字符串
            ratios = []
            flags = []
            for _, row in df.iterrows():
                lc = row["点赞数"]
                fc = row["粉丝数"]
                # 空点赞 → 数据不足
                if not lc or (isinstance(lc, str) and lc.strip() == ""):
                    ratios.append("-")
                    flags.append("数据不足")
                    continue
                # 粉丝数可能已格式化
                try:
                    likes = int(float(lc))
                except (ValueError, TypeError):
                    ratios.append("-")
                    flags.append("数据不足")
                    continue
                # 解析粉丝数（处理 "4.0w" 和数字两种格式）
                if isinstance(fc, str):
                    followers = _parse_followers(fc)
                else:
                    try:
                        followers = int(float(fc))
                    except (ValueError, TypeError):
                        followers = 0
                if not followers:
                    ratios.append("-")
                    flags.append("数据不足")
                    continue
                r_text, flag = _fmt_like_ratio_and_flag(likes, followers)
                ratios.append(r_text)
                flags.append(flag)
            df["赞粉比"] = ratios
            df["流量质量"] = flags
        else:
            df["赞粉比"] = "-"
            df["流量质量"] = "-"
    return df


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
    import sqlite3, os
    db_path = DATA_DIR / "database" / "creator_finder.db"
    result = {"today_runs": 0, "today_creators": 0, "total_creators": 0, "last_run": "从未"}
    if not db_path.exists():
        return result
    try:
        conn = sqlite3.connect(str(db_path))
        today = datetime.now().strftime("%Y-%m-%d")
        result["today_runs"] = conn.execute(
            "SELECT COUNT(DISTINCT run_date) FROM daily_results WHERE run_date = ?", (today,)
        ).fetchone()[0] or 0
        result["today_creators"] = conn.execute(
            "SELECT COUNT(DISTINCT creator_key) FROM daily_results WHERE run_date = ?", (today,)
        ).fetchone()[0] or 0
        result["total_creators"] = conn.execute("SELECT COUNT(*) FROM creators").fetchone()[0] or 0
        conn.close()
        mtime = os.path.getmtime(str(db_path))
        last_dt = datetime.fromtimestamp(mtime)
        result["last_run"] = f"{last_dt:%Y-%m-%d %H:%M}"
        return result
    except Exception:
        return result

cdp_ok, cdp_browser = check_cdp()
session_ok, session_info = check_douyin_session()
stats = get_run_stats()
last_run_info = st.session_state.get("last_run_stats", {})
if last_run_info:
    stats["last_run"] = last_run_info.get("runtime", stats["last_run"])
    stats["today_creators"] = max(stats["today_creators"], last_run_info.get("creators", 0))

# 状态面板 - 用 st.info 保留背景色
stcols = st.columns(5)
with stcols[0]:
    if cdp_ok: st.success("🟢 CDP")
    else: st.error("🔴 CDP")
with stcols[1]:
    if session_ok: st.success("🟢 登录")
    else: st.error("🔴 登录")
with stcols[2]:
    r = last_run_info.get("creators", 0)
    st.info(f"📊 本次\n{r}人")
with stcols[3]:
    st.info(f"📅 今日\n{stats['today_creators']}人")
with stcols[4]:
    st.info(f"📦 总计\n{stats['total_creators']}人")

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

    if st.button("开始筛选", type="primary", width="stretch"):
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
        kw_count = 20 if use_ai_kw else (len(keywords_final) if keywords_final else 20)
        # 更准确的预估：每词约 10-12 秒（搜索+补全简介），加评分和写库约 30 秒
        est_total = max(kw_count * 12 + 30, 30)

        progress_bar = st.progress(0, "⏳ 准备中...")
        status_text = st.empty()
        eta_text = st.empty()

        result_holder = {"summary": None, "error": None, "done": False}
        first_result_time = {"t": None}

        def _run_pipeline():
            try:
                result_holder["summary"] = run(
                    skip_keyword_expand=True, skip_ai=skip_ai,
                    discover=False, douyin_import=False, enrich_remote=False,
                    keywords_override=keywords_final,
                    use_ai_keywords=use_ai_kw, feedback_notes=fb,
                )
            except Exception as e:
                result_holder["error"] = str(e)
            result_holder["done"] = True

        thread = threading.Thread(target=_run_pipeline, daemon=True)
        thread.start()

        # 动态进度：前 80% 基于时间估算，后 20% 等实际完成
        while not result_holder["done"]:
            elapsed = time.time() - start_time

            # 动态调整预估：如果超过原始预估，按比例扩展
            adjusted_est = est_total
            if elapsed > est_total * 0.8:
                adjusted_est = elapsed * 1.3  # 实际时间的 1.3 倍

            progress_pct = min(elapsed / adjusted_est * 100, 92)
            remaining = max(adjusted_est - elapsed, 0)
            em, es = int(elapsed // 60), int(elapsed % 60)
            rm, rs = int(remaining // 60), int(remaining % 60)

            # 阶段文字
            if progress_pct < 10:
                phase = "🤖 准备中..."
            elif progress_pct < 80:
                phase = "🔍 搜索中..."
            else:
                phase = "📊 评分/保存中..."

            progress_bar.progress(int(progress_pct), phase)
            status_text.text(f"{kw_count} 个关键词 | 已耗时 {em} 分 {es} 秒")

            if remaining > 60:
                eta_text.text(f"⏱ 预计还需 {rm} 分 {rs} 秒")
            elif remaining > 0:
                eta_text.text(f"⏱ 预计还需 {int(remaining)} 秒")
            else:
                eta_text.text(f"⏱ 即将完成...")

            time.sleep(1)

        # 完成
        thread.join(timeout=5)
        elapsed = time.time() - start_time
        em, es = int(elapsed // 60), int(elapsed % 60)

        if result_holder["error"]:
            progress_bar.progress(100, "❌ 失败")
            eta_text.text(f"❌ {em} 分 {es} 秒后失败")
            st.error(f"运行失败：{result_holder['error']}")
        else:
            summary = result_holder["summary"]
            progress_bar.progress(100, "✅ 完成")
            status_text.text("")
            eta_text.text(f"✅ 总耗时 {em} 分 {es} 秒 | {summary['unique']} 条达人已就绪")
            st.session_state["last_summary"] = summary
            st.session_state["last_run_stats"] = {
                "runtime": f"{datetime.now():%H:%M}",
                "creators": summary["unique"],
            }
            st.success(f"完成！{summary['unique']} 条达人，耗时 {em} 分 {es} 秒")

    st.header("🎚 粉丝量筛选")
    follower_ranges = {
        "不足1万": (0, 10000),
        "1-5万": (10000, 50000),
        "5-10万": (50000, 100000),
        "10-50万": (100000, 500000),
        "50-100万": (500000, 1000000),
        "100万+": (1000000, 999999999),
    }
    selected_tiers = st.multiselect(
        "选择粉丝量级（留空=全部）",
        list(follower_ranges.keys()),
        default=[],
        placeholder="全部",
    )
    st.session_state["follower_filter"] = selected_tiers

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

    tab1, tab2, tab3, tab4 = st.tabs(["Top 推荐", "全部候选", "人工标注", "反向评估"])

    with tab1:
        try:
            df_top = pd.read_excel(latest, sheet_name="今日Top推荐").fillna("")
            df_top = _enrich_new_fields(df_top)
            # 粉丝量筛选
            selected_tiers = st.session_state.get("follower_filter", [])
            if selected_tiers and "粉丝数" in df_top.columns:
                df_top = _apply_follower_filter(df_top, "粉丝数", selected_tiers)
            # 列筛选控件
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                level_filter = st.multiselect("推荐等级", ["S", "A", "B", "C"], key="tab1_level")
            with fc2:
                new_filter = st.multiselect("是否新达人", ["🆕新", "历史"], key="tab1_new")
            with fc3:
                product_filter = st.multiselect("是否挂车", ["是", "否", "未知"], key="tab1_product")
            if level_filter:
                df_top = df_top[df_top["推荐等级"].isin(level_filter)]
            if new_filter:
                df_top = df_top[df_top["是否新达人"].isin(new_filter)]
            if product_filter:
                df_top = df_top[df_top["是否挂车"].isin(product_filter)]
            cols = [c for c in [
                "排名", "是否新达人", "推荐等级", "AI评分",
                "评分明细", "达人昵称", "抖音号", "抖音达人类型",
                "粉丝数", "粉丝量分级", "量级",
                "推荐产品", "合作建议",
                "公开联系方式", "联系方式类型",
                "达人主页链接", "代表视频链接",
                "是否挂车", "赞粉比", "流量质量",
                "风险点", "搜索关键词", "数据来源", "提取状态",
            ] if c in df_top.columns]

            column_config = {}
            if "达人主页链接" in df_top.columns:
                column_config["达人主页链接"] = st.column_config.LinkColumn("达人主页链接", display_text="🔗 打开")
            if "代表视频链接" in df_top.columns:
                column_config["代表视频链接"] = st.column_config.LinkColumn("代表视频链接", display_text="▶ 打开")
            if "AI评分" in df_top.columns:
                column_config["AI评分"] = st.column_config.NumberColumn("AI评分", format="%.1f")
            if "排名" in df_top.columns:
                column_config["排名"] = st.column_config.NumberColumn("排名")

            st.caption(f"共 {len(df_top)} 条")
            st.dataframe(df_top[cols], width="stretch", height=500, column_config=column_config or None)
        except Exception as e:
            st.error(f"读取失败：{e}")

    with tab2:
        try:
            df_all = pd.read_excel(latest, sheet_name="全部候选").fillna("")
            df_all = _enrich_new_fields(df_all)
            # 粉丝量筛选
            selected_tiers = st.session_state.get("follower_filter", [])
            if selected_tiers and "粉丝数" in df_all.columns:
                df_all = _apply_follower_filter(df_all, "粉丝数", selected_tiers)
            # 列筛选控件
            afc1, afc2, afc3 = st.columns(3)
            with afc1:
                a_level_filter = st.multiselect("推荐等级", ["S", "A", "B", "C"], key="tab2_level")
            with afc2:
                a_product_filter = st.multiselect("是否挂车", ["是", "否", "未知"], key="tab2_product")
            with afc3:
                a_contact_filter = st.multiselect("公开联系方式", ["有", "无"], key="tab2_contact")
            if a_level_filter:
                df_all = df_all[df_all["推荐等级"].isin(a_level_filter)]
            if a_product_filter:
                df_all = df_all[df_all["是否挂车"].isin(a_product_filter)]
            if a_contact_filter:
                df_all = df_all[df_all["是否有公开联系方式"].isin(a_contact_filter)]
            # 只显示有用的列
            show_cols = [c for c in [
                "推荐等级", "AI评分", "推荐理由",
                "达人昵称", "抖音号", "抖音达人类型", "粉丝数", "粉丝量分级", "量级",
                "内容类型", "是否接近果子模式",
                "推荐产品", "合作建议",
                "是否有公开联系方式", "公开联系方式",
                "达人主页链接", "代表视频链接",
                "是否挂车", "赞粉比", "流量质量",
                "风险点", "下一步动作",
                "搜索关键词", "提取状态",
                "采集日期",
            ] if c in df_all.columns]
            all_col_config = {}
            if "达人主页链接" in df_all.columns:
                all_col_config["达人主页链接"] = st.column_config.LinkColumn("达人主页链接", display_text="🔗 打开")
            if "代表视频链接" in df_all.columns:
                all_col_config["代表视频链接"] = st.column_config.LinkColumn("代表视频链接", display_text="▶ 打开")
            if "AI评分" in df_all.columns:
                all_col_config["AI评分"] = st.column_config.NumberColumn("AI评分", format="%.1f")
            if "粉丝数" in df_all.columns:
                all_col_config["粉丝数"] = st.column_config.TextColumn("粉丝数")
            st.caption(f"共 {len(df_all)} 条")
            st.dataframe(df_all[show_cols], width="stretch", height=500, column_config=all_col_config or None)
        except Exception as e:
            st.error(f"读取失败：{e}")

    with tab3:
        st.subheader("人工状态管理")
        status_rows = list_creators_with_status()
        if not status_rows:
            st.info("SQLite 里还没有达人记录，先跑一次筛选。")
        else:
            df_status = pd.DataFrame(status_rows).fillna("")

            # 粉丝量筛选（在格式化前，基于原始整数）
            selected_tiers = st.session_state.get("follower_filter", [])
            if selected_tiers and "latest_follower_count" in df_status.columns:
                df_status = _apply_follower_filter(df_status, "latest_follower_count", selected_tiers)

            # 粉丝量分级（在格式化前）
            if "latest_follower_count" in df_status.columns:
                df_status["粉丝量分级"] = df_status["latest_follower_count"].apply(_fmt_follower_tier)

            # 格式化粉丝数
            if "latest_follower_count" in df_status.columns:
                df_status["latest_follower_count"] = df_status["latest_follower_count"].apply(_fmt_followers)
            if "contact_text" in df_status.columns:
                df_status["contact_text"] = df_status["contact_text"].apply(_fmt_contact)
            if "contact_visible" in df_status.columns:
                df_status["contact_visible"] = df_status["contact_visible"].apply(
                    lambda v: "有" if str(v) == "是" else ("无" if str(v) == "否" else "")
                )
            if "first_seen_date" in df_status.columns:
                df_status["是否新达人"] = df_status.apply(
                    lambda row: "🆕新" if str(row.get("first_seen_date", "")) == str(row.get("last_seen_date", "")) else "历史",
                    axis=1,
                )

            df_status = df_status.rename(columns={
                "creator_name": "达人昵称",
                "douyin_id": "抖音号",
                "creator_id": "达人ID",
                "platform": "平台",
                "latest_follower_count": "粉丝数",
                "main_content_type": "内容类型",
                "latest_score": "AI评分",
                "priority_level": "推荐等级",
                "status": "当前状态",
                "creator_profile_url": "达人主页链接",
                "video_url": "代表视频链接",
                "contact_visible": "是否有公开联系方式",
                "contact_text": "公开联系方式",
                "has_product_link": "是否挂车",
                "contact_type": "联系方式类型",
                "extraction_status": "提取状态",
                "missing_reason": "缺失原因",
                "search_keyword": "搜索关键词",
                "recommend_reason": "推荐理由",
                "risk_reason": "风险点",
                "next_action": "下一步动作",
                "last_seen_date": "上次出现",
                "url_type": "链接类型",
                "creator_tier": "量级",
                "like_follower_ratio": "赞粉比",
                "traffic_quality_flag": "流量质量",
            })

            show_cols = [c for c in [
                "是否新达人", "推荐等级", "AI评分", "当前状态",
                "达人昵称", "抖音号", "粉丝数", "粉丝量分级", "量级", "内容类型",
                "推荐理由", "风险点", "下一步动作",
                "是否有公开联系方式", "公开联系方式",
                "达人主页链接", "代表视频链接",
                "是否挂车", "赞粉比", "流量质量",
                "搜索关键词", "提取状态",
                "上次出现",
            ] if c in df_status.columns]

            # 列筛选控件
            sfc1, sfc2, sfc3, sfc4 = st.columns(4)
            with sfc1:
                s_level_filter = st.multiselect("推荐等级", ["S", "A", "B", "C", "淘汰"], key="tab3_level")
            with sfc2:
                s_new_filter = st.multiselect("是否新达人", ["🆕新", "历史"], key="tab3_new")
            with sfc3:
                s_product_filter = st.multiselect("是否挂车", ["是", "否", "未知"], key="tab3_product")
            with sfc4:
                s_status_filter = st.multiselect("当前状态", CREATOR_STATUS, key="tab3_status")
            if s_level_filter:
                df_status = df_status[df_status["推荐等级"].isin(s_level_filter)]
            if s_new_filter:
                df_status = df_status[df_status["是否新达人"].isin(s_new_filter)]
            if s_product_filter:
                df_status = df_status[df_status["是否挂车"].isin(s_product_filter)]
            if s_status_filter:
                df_status = df_status[df_status["当前状态"].isin(s_status_filter)]

            status_col_config = {}
            if "达人主页链接" in df_status.columns:
                status_col_config["达人主页链接"] = st.column_config.LinkColumn("达人主页链接", display_text="🔗 打开")
            if "代表视频链接" in df_status.columns:
                status_col_config["代表视频链接"] = st.column_config.LinkColumn("代表视频链接", display_text="▶ 打开")
            if "AI评分" in df_status.columns:
                status_col_config["AI评分"] = st.column_config.NumberColumn("AI评分", format="%.1f")
            if "粉丝数" in df_status.columns:
                status_col_config["粉丝数"] = st.column_config.TextColumn("粉丝数")
            st.caption(f"共 {len(df_status)} 条")
            st.dataframe(df_status[show_cols], width="stretch", height=400, column_config=status_col_config or None)

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

    with tab4:
        st.subheader("🔍 反向评估达人")
        st.caption("输入达人主页链接、视频链接或抖音 sec_uid，AI 自动拉取数据并深度分析")

        eval_input = st.text_area(
            "达人链接/ID（每行一个，支持批量）",
            placeholder="https://www.douyin.com/user/MS4wLjABAAAAxxx...\nhttps://www.douyin.com/user/MS4wLjABAAAAyyy...",
            height=100,
        )

        eval_mode = st.radio("评估模式", ["标准评估", "竞品分析"], horizontal=True)

        if st.button("🔍 开始评估", key="eval_btn"):
            inputs = [l.strip() for l in eval_input.split("\n") if l.strip()]
            if not inputs:
                st.warning("请输入至少一个达人链接或ID")
            else:
                with st.spinner(f"正在分析 {len(inputs)} 个达人..."):
                    from src.ai.creator_reverse_evaluator import batch_analyze, analyze_creator

                    if len(inputs) == 1:
                        result = analyze_creator(inputs[0])
                        results = [result]
                    else:
                        results = batch_analyze(inputs)

                for i, r in enumerate(results):
                    if "error" in r:
                        st.error(f"❌ {r['error']}")
                        continue

                    ai = r.get("ai_analysis", {})
                    with st.expander(f"{'🥇' if i==0 else '📋'} {r.get('nickname','?')} — {ai.get('overall_score',0)}分 | {ai.get('priority_level','?')}级", expanded=(i == 0)):
                        c1, c2, c3, c4 = st.columns(4)
                        c1.metric("综合评分", f"{ai.get('overall_score',0)}/100")
                        c2.metric("推荐等级", ai.get('priority_level', '?'))
                        c3.metric("粉丝数", f"{r.get('follower_count',0):,}")
                        c4.metric("合作建议", ai.get('cooperation_suggestion', '?'))

                        st.markdown(f"**推荐理由**：{ai.get('recommend_reason', '?')}")
                        st.markdown(f"**推荐产品**：{ai.get('recommended_product', '?')}")

                        col_a, col_b = st.columns(2)
                        with col_a:
                            st.markdown(f"📊 内容匹配({ai.get('content_fit_score',0)}/30)：{ai.get('content_fit_reason','?')}")
                            st.markdown(f"📊 数据表现({ai.get('data_performance_score',0)}/20)：{ai.get('data_performance_reason','?')}")
                            st.markdown(f"📊 达人量级({ai.get('creator_scale_score',0)}/15)：{ai.get('creator_scale_reason','?')}")
                        with col_b:
                            st.markdown(f"📊 合作可行性({ai.get('cooperation_score',0)}/15)：{ai.get('cooperation_reason','?')}")
                            st.markdown(f"📊 素材复用({ai.get('reuse_score',0)}/10)：{ai.get('reuse_reason','?')}")
                            st.markdown(f"⚠️ 风险({ai.get('risk_score',0)}/10)：{ai.get('risk_reason','?')}")

                        if eval_mode == "竞品分析":
                            st.info(f"**竞品分析**：{ai.get('competitor_analysis','?')}")
                        st.caption(f"**下一步**：{ai.get('next_action','?')}")
                        st.caption(f"主页：{r.get('profile_url','')}")

                        # 展示最近视频
                        videos = r.get("recent_videos", [])
                        if videos:
                            st.markdown("**最近视频**：")
                            for v in videos[:5]:
                                st.markdown(f"- [{v['desc'][:60]}]({v['video_url']}) | 👍{v['likes']} 💬{v['comments']} ↗{v['shares']}")

    new_only = st.checkbox("🆕 仅导出新增达人", value=False,
        help="勾选后仅导出首次发现的达人，方便增量处理")
    if new_only:
        # 从 SQLite 获取新达人集合（首次出现日期 == 最近出现日期）
        try:
            import sqlite3
            db = DATA_DIR / "database" / "creator_finder.db"
            conn = sqlite3.connect(str(db))
            new_rows = conn.execute(
                "SELECT creator_profile_url FROM creators WHERE first_seen_date = last_seen_date"
            ).fetchall()
            conn.close()
            new_urls = {r[0] for r in new_rows if r[0]}
            # 读取全部候选，过滤出新达人
            df_all = pd.read_excel(latest, sheet_name="全部候选").fillna("")
            if "达人主页链接" in df_all.columns:
                df_filtered = df_all[df_all["达人主页链接"].isin(new_urls)]
            else:
                df_filtered = df_all.head(0)
            from io import BytesIO
            buf = BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as w:
                df_filtered.to_excel(w, sheet_name="新增达人", index=False)
            buf.seek(0)
            label = f"📥 下载新增达人（{len(df_filtered)}条）"
            st.download_button(label, data=buf.getvalue(),
                file_name=f"new_creators_{latest.name}",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception as e:
            st.warning(f"无法生成增量导出：{e}")
    else:
        with open(latest, "rb") as f:
            st.download_button("📥 下载完整 Excel", data=f.read(), file_name=latest.name,
                               mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

st.caption(f"🕐 页面刷新时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
