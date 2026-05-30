"""去重：按 profile_url / creator_id / video_url 严格去重，platform+name 标记疑似重复。"""
from __future__ import annotations

from dataclasses import dataclass

from src.models.schemas import CreatorRecord
from src.utils.logger import get_logger

logger = get_logger()


@dataclass
class DedupResult:
    unique: list[CreatorRecord]
    suspect_duplicates: list[CreatorRecord]   # 疑似重复（platform+name 同名异 url）


def _score_for_keep(r: CreatorRecord) -> tuple:
    """同一达人多条记录时保留最高分；评分相同时保留点赞最高的。"""
    return (r.ai_score or 0.0, r.rule_score or 0.0, r.like_count or 0)


def deduplicate(records: list[CreatorRecord]) -> DedupResult:
    by_creator: dict[str, CreatorRecord] = {}
    by_video: dict[str, CreatorRecord] = {}
    by_id: dict[str, CreatorRecord] = {}
    by_name_platform: dict[tuple[str, str], list[CreatorRecord]] = {}

    for r in records:
        ck = r.creator_key()
        prev = by_creator.get(ck)
        if prev is None or _score_for_keep(r) > _score_for_keep(prev):
            by_creator[ck] = r

        vk = r.video_key()
        if vk:
            prevv = by_video.get(vk)
            if prevv is None or _score_for_keep(r) > _score_for_keep(prevv):
                by_video[vk] = r

        if r.creator_id:
            key = f"{r.platform}::{r.creator_id}"
            prevu = by_id.get(key)
            if prevu is None or _score_for_keep(r) > _score_for_keep(prevu):
                by_id[key] = r

        if r.creator_name:
            by_name_platform.setdefault((r.platform, r.creator_name), []).append(r)

    # 取并集（以 by_creator 为主，by_video 兜底视频不同的同人）
    unique: list[CreatorRecord] = list(by_creator.values())
    seen_keys = {r.creator_key() for r in unique}
    for v in by_video.values():
        if v.creator_key() not in seen_keys:
            unique.append(v)
            seen_keys.add(v.creator_key())

    # 疑似重复：同 platform+name 但 creator_key 不同
    suspects: list[CreatorRecord] = []
    for (_, _), rs in by_name_platform.items():
        if len({r.creator_key() for r in rs}) > 1:
            suspects.extend(rs)

    logger.info(f"去重：输入 {len(records)} → 唯一 {len(unique)}，疑似重复 {len(suspects)} 条")
    return DedupResult(unique=unique, suspect_duplicates=suspects)
