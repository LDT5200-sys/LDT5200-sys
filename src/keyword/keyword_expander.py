"""关键词扩展：从 seed_keywords.yaml 读取种子词，调用 LLM 扩展。AI 失败时回退到本地启发式。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from src.utils.config_loader import seed_keywords_config, DATA_DIR
from src.utils.logger import get_logger
from src.utils.time_utils import today_str

logger = get_logger()

_PROMPT_TEMPLATE = """你是抖音男装内容投放策略助手。品牌方向是男装、户外、战术、机能、通勤、硬朗。

请基于以下种子关键词，扩展 {n} 个抖音/小红书可直接搜索的相关搜索词，覆盖：
- 测评、改造、推荐、避雷、场景穿搭、大码/微胖、功能/速干 等意图
- 关键词需要是真人搜索口语，不要堆品牌词、不要含露骨/擦边内容

每条输出 JSON 对象：
{{"keyword": "扩展词", "intent": "搜索意图（必须从 {intents} 中选一个，不在表中用 其他）", "priority": "高/中/低", "product_direction": "适合的产品方向（从 {products} 中选一个，可填 综合）"}}

只输出一个 JSON 数组，不要任何额外解释、不要 Markdown 代码块。

种子关键词：{seed}
"""


def _heuristic_expand(seed: str, n: int) -> list[dict]:
    """AI 不可用时的兜底：基于固定模板生成几条扩展词，保证流程不中断。"""
    suffix = ["测评", "推荐", "避雷", "穿搭", "对比", "真实体验", "上身效果", "夏季"]
    out = []
    for s in suffix[:n]:
        out.append({
            "keyword": f"{seed}{s}" if not seed.endswith(s) else seed,
            "intent": "其他",
            "priority": "中",
            "product_direction": "综合",
        })
    return out


def _parse_llm_json(content: str) -> list[dict]:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    start = content.find("[")
    end = content.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(content[start: end + 1])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def expand_keywords(use_ai: bool = True) -> pd.DataFrame:
    cfg = seed_keywords_config()
    seeds: list[str] = cfg.get("seed_keywords", [])
    n = int(cfg.get("expand_per_seed", 8))
    intents = cfg.get("intent_categories", [])
    products = cfg.get("product_directions", [])

    if not seeds:
        logger.warning("seed_keywords.yaml 没有种子词，跳过关键词扩展。")
        return pd.DataFrame(columns=["原始关键词", "扩展关键词", "搜索意图", "优先级", "适合产品方向"])

    rows: list[dict] = []

    llm = None
    if use_ai:
        try:
            from src.ai.llm_client import LLMClient
            llm = LLMClient(role="keyword")
            if not llm.usable:
                logger.warning("LLM 不可用，关键词扩展回退到本地启发式。")
                llm = None
        except Exception as e:
            logger.warning(f"LLM 初始化失败：{e}，回退到本地启发式。")
            llm = None

    for seed in seeds:
        items: list[dict] = []
        if llm is not None:
            prompt = _PROMPT_TEMPLATE.format(
                n=n, seed=seed,
                intents=intents or ["测评", "其他"],
                products=products or ["综合"],
            )
            try:
                resp = llm.chat(prompt)
                items = _parse_llm_json(resp)
            except Exception as e:
                logger.warning(f"种子词 {seed} 调用 LLM 失败：{e}")
                items = []

        if not items:
            items = _heuristic_expand(seed, n)

        for it in items[:n]:
            rows.append({
                "原始关键词": seed,
                "扩展关键词": str(it.get("keyword", "")).strip(),
                "搜索意图": it.get("intent", "其他") or "其他",
                "优先级": it.get("priority", "中") or "中",
                "适合产品方向": it.get("product_direction", "综合") or "综合",
            })
        logger.info(f"关键词扩展：{seed} → {len(items)} 条")

    df = pd.DataFrame(rows).drop_duplicates(subset=["扩展关键词"]).reset_index(drop=True)

    out_dir = DATA_DIR / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"expanded_keywords_{today_str()}.xlsx"
    try:
        df.to_excel(out_path, index=False)
        logger.info(f"扩展关键词已写入：{out_path} 共 {len(df)} 条")
    except Exception as e:
        logger.error(f"写出扩展关键词失败：{e}")

    return df


if __name__ == "__main__":
    expand_keywords()
