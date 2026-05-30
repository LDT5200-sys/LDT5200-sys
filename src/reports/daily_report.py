"""每日 Markdown 日报。"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from src.models.schemas import CreatorRecord
from src.utils.config_loader import DATA_DIR
from src.utils.logger import get_logger
from src.utils.time_utils import now_str, today_str

logger = get_logger()


def write_daily_report(
    records: list[CreatorRecord],
    suspects: list[CreatorRecord] | None = None,
    out_dir: Path | None = None,
) -> Path:
    out_dir = out_dir or (DATA_DIR / "output")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"daily_report_{today_str()}.md"

    levels = Counter(r.priority_level for r in records)
    s_count = levels.get("S", 0)
    a_count = levels.get("A", 0)
    b_count = levels.get("B", 0)
    rejected = levels.get("淘汰", 0)
    candidates_total = len(records)
    creator_keys = {r.creator_key() for r in records}

    contact_yes = sum(1 for r in records if r.contact_visible == "是")
    extract_fail = sum(1 for r in records if r.extraction_status == "失败")
    extract_partial = sum(1 for r in records if r.extraction_status == "部分成功")

    top_records = sorted(records, key=lambda r: r.ai_score, reverse=True)[:20]
    top_records = [r for r in top_records if r.priority_level != "淘汰"]

    kw_counter: Counter = Counter()
    kw_top: Counter = Counter()
    for r in records:
        kw = r.search_keyword or "(空)"
        kw_counter[kw] += 1
        if r.priority_level in ("S", "A"):
            kw_top[kw] += 1

    review_notes: list[str] = []
    for r in records:
        if r.is_fit_longya == "不确定":
            review_notes.append(f"{r.creator_name}（{r.platform}）分类不确定，建议人工复核")
        if r.priority_level in ("S", "A") and not r.creator_profile_url:
            review_notes.append(f"{r.creator_name} 评分高但缺主页链接，需补全后再联系")
        if len(review_notes) >= 10:
            break

    lines: list[str] = []
    lines.append(f"# 龙牙外部达人日报 {today_str('%Y-%m-%d')}")
    lines.append("")
    lines.append(f"- 运行时间：{now_str()}")
    lines.append(f"- 今日采集候选数：{candidates_total}")
    lines.append(f"- 去重后达人数量：{len(creator_keys)}")
    lines.append(f"- S 级：{s_count}")
    lines.append(f"- A 级：{a_count}")
    lines.append(f"- B 级：{b_count}")
    lines.append(f"- 淘汰：{rejected}")
    lines.append(f"- 疑似重复：{len(suspects or [])}")
    lines.append(f"- 有公开联系方式：{contact_yes}")
    lines.append(f"- 提取失败：{extract_fail}（部分成功 {extract_partial}）")
    lines.append("")

    lines.append("## Top20 达人")
    if top_records:
        lines.append("| 排名 | 等级 | AI评分 | 达人 | 平台 | 粉丝 | 内容类型 | 推荐产品 | 推荐理由 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(top_records, 1):
            lines.append(
                f"| {i} | {r.priority_level} | {r.ai_score:.1f} | {r.creator_name or '-'} | "
                f"{r.platform or '-'} | {r.follower_count if r.follower_count is not None else '-'} | "
                f"{r.content_type or '-'} | {r.recommended_product or '-'} | "
                f"{(r.recommend_reason or '').replace('|', '/')} |"
            )
    else:
        lines.append("（今日无 Top 候选）")
    lines.append("")

    lines.append("## 表现最好的关键词")
    if kw_top:
        for kw, n in kw_top.most_common(10):
            lines.append(f"- {kw}：S/A 数 {n}，候选总数 {kw_counter.get(kw, 0)}")
    else:
        lines.append("（无 S/A 级候选）")
    lines.append("")

    lines.append("## 需要人工复核")
    if review_notes:
        for note in review_notes:
            lines.append(f"- {note}")
    else:
        lines.append("- 无明显问题")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"Markdown 日报写出：{path}")
    return path
