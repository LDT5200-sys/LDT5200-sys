"""命令行主入口：关键词扩展 → 多源导入 → 清洗去重 → AI 分类与评分 → 写库 → 出报表。"""
from __future__ import annotations

# 注意：必须在 import argparse 等标准库之前修复 sys.path，
# 否则 sys.path[0] 是 src/，会让 `from keyword import iskeyword`（collections 内部用到）
# 命中项目里的 src/keyword/ 包。
import os as _os
import sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_ROOT = _os.path.dirname(_HERE)
if _sys.path and _sys.path[0] == _HERE:
    _sys.path[0] = _ROOT
elif _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import argparse
import json
import sys
from pathlib import Path

from src.ai.creator_classifier import classify_records
from src.ai.creator_scorer import score_records
from src.cleaner.deduplicator import deduplicate
from src.cleaner.normalizer import normalize_records
from src.data_sources.registry import fetch_all
from src.keyword.keyword_expander import expand_keywords
from src.reports.daily_report import write_daily_report
from src.storage.excel_writer import write_daily_excel
from src.storage.sqlite_store import get_eliminated_creator_keys, upsert_records
from src.utils.config_loader import DATA_DIR, scoring_rules
from src.utils.logger import get_logger
from src.utils.time_utils import today_str

logger = get_logger()


def run(
    skip_keyword_expand: bool = False,
    skip_ai: bool = False,
    discover: bool = False,
    douyin_import: bool = False,
    enrich_remote: bool = False,
    keywords_override: list[str] | None = None,
) -> dict:
    logger.info("=" * 60)
    logger.info(
        f"龙牙达人发现系统启动 run_date={today_str('%Y-%m-%d')} "
        f"discover={discover} douyin_import={douyin_import} skip_ai={skip_ai} skip_kw={skip_keyword_expand}"
    )
    logger.info("=" * 60)

    expanded_keywords: list[str] = []
    if keywords_override:
        expanded_keywords = list(keywords_override)
        logger.info(f"使用外部传入关键词 {len(expanded_keywords)} 条，跳过扩展")
    elif not skip_keyword_expand:
        try:
            kw_df = expand_keywords(use_ai=not skip_ai)
            expanded_keywords = kw_df["扩展关键词"].dropna().astype(str).tolist()
        except Exception as e:
            logger.error(f"关键词扩展失败：{e}")

    # --discover / --douyin-import: 临时开启相应数据源
    overrides: dict[str, bool] = {}
    if douyin_import:
        overrides["douyin_search"] = True
        # 检查是否完全无数据可读
        from src.utils.config_loader import load_env
        from pathlib import Path
        env = load_env()
        has_api = bool(env.get("DOUYIN_API_KEY") or env.get("DOUYIN_ACCESS_TOKEN"))
        dy_dir = DATA_DIR / "input" / "douyin"
        has_files = False
        if dy_dir.exists():
            has_files = any(
                any(dy_dir.glob(p)) for p in ("*.xlsx", "*.xls", "*.csv", "*.html")
            )
        if not has_api and not has_files:
            logger.warning(
                "抖音数据源导入模式已启用，但 data/input/douyin/ 下无文件，"
                "且未配置 DOUYIN_API_KEY / DOUYIN_ACCESS_TOKEN。"
                "请先放入抖音搜索导出文件，或配置 API 凭证。"
            )
            print(
                "\n[douyin_import] 当前 data/input/douyin/ 下无文件，且未配置 DOUYIN_API_KEY。\n"
                "请先将抖音搜索导出文件（CSV/Excel/HTML）放入 data/input/douyin/，"
                "或在 .env 中配置 DOUYIN_DATA_PROVIDER / DOUYIN_API_KEY 等。\n"
                "本次抖音数据源将返回 0 条候选。\n"
            )
    if discover:
        from src.utils.config_loader import load_env
        env = load_env()
        if not env.get("SEARCH_API_KEY"):
            logger.warning(
                "当前未配置搜索API（SEARCH_API_KEY 为空），无法自动发现公开网页结果。"
                "请配置 SEARCH_API_KEY，或先把导出的达人表放入 data/input/。"
            )
            print(
                "\n[discover] 当前未配置搜索API，无法自动发现公开网页结果。\n"
                "请在 .env 中配置 SEARCH_PROVIDER / SEARCH_API_KEY / SEARCH_API_BASE_URL，"
                "或先把导出的达人表放入 data/input/。\n"
                "本次将仅处理 data/input/ 中的本地文件。\n"
            )
        else:
            overrides["public_search"] = True

    raw_rows = fetch_all(keywords=expanded_keywords, enable_overrides=overrides)
    logger.info(f"多源导入合计 {len(raw_rows)} 行")

    raw_dir = DATA_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    try:
        (raw_dir / f"merged_{today_str()}.json").write_text(
            json.dumps(raw_rows, ensure_ascii=False, default=str)[:5_000_000],
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"原始合并文件保存失败：{e}")

    records = normalize_records(raw_rows, enrich_remote=enrich_remote)
    if not records:
        logger.warning("无有效候选记录，仍会写出空报表")

    if not skip_ai:
        try:
            records = classify_records(records)
        except Exception as e:
            logger.error(f"分类阶段异常但继续：{e}")
    else:
        # 即便用户跳过 AI，也跑启发式分类（不联网），保证内容类型不为空
        try:
            from src.ai.creator_classifier import _heuristic_classify
            for rec in records:
                h = _heuristic_classify(rec)
                rec.content_type = h["content_type"]
                rec.is_fit_longya = h["is_fit_longya"]
                rec.is_guozi_like = h["is_guozi_like"]
        except Exception as e:
            logger.error(f"启发式分类异常但继续：{e}")
    try:
        records = score_records(records)
    except Exception as e:
        logger.error(f"评分阶段异常但继续：{e}")

    dedup = deduplicate(records)
    unique = dedup.unique

    revive_th = float(scoring_rules().get("revive_threshold", 80))
    eliminated = get_eliminated_creator_keys()
    if eliminated:
        before = len(unique)
        unique = [r for r in unique if r.creator_key() not in eliminated or r.ai_score >= revive_th]
        logger.info(f"历史淘汰过滤：{before} → {len(unique)}（回捞阈值 {revive_th}）")

    excel_path = write_daily_excel(unique, suspects=dedup.suspect_duplicates)
    md_path = write_daily_report(unique, suspects=dedup.suspect_duplicates)

    try:
        upsert_records(unique)
    except Exception as e:
        logger.error(f"SQLite 写入失败：{e}")

    summary = {
        "raw_rows": len(raw_rows),
        "normalized": len(records),
        "unique": len(unique),
        "suspects": len(dedup.suspect_duplicates),
        "expanded_keywords": len(expanded_keywords),
        "excel": str(excel_path),
        "report": str(md_path),
    }
    logger.info(f"运行完成：{summary}")
    return summary


def _parse_args():
    p = argparse.ArgumentParser(description="龙牙外部达人自动发现与AI评分系统")
    p.add_argument("--skip-keyword-expand", action="store_true", help="跳过关键词扩展")
    p.add_argument("--skip-ai", action="store_true", help="跳过 AI，仅规则评分")
    p.add_argument(
        "--discover",
        action="store_true",
        help="开启公开搜索发现模式（需在 .env 配置 SEARCH_API_KEY）",
    )
    p.add_argument(
        "--douyin-import",
        action="store_true",
        help="开启抖音数据源导入模式（读取 data/input/douyin/ + 可选 API）",
    )
    p.add_argument(
        "--enrich-remote",
        action="store_true",
        help="对识别出的 douyin 主页/视频链接抓取一次 og:meta（合规模式，遇风控/登录立即放弃）",
    )
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(
        skip_keyword_expand=args.skip_keyword_expand,
        skip_ai=args.skip_ai,
        discover=args.discover,
        douyin_import=args.douyin_import,
        enrich_remote=args.enrich_remote,
    )
