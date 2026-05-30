"""统一日志：loguru，按天滚动到 logs/ 目录。"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

_INITIALIZED = False


def get_logger(project_root: Path | None = None):
    global _INITIALIZED
    if _INITIALIZED:
        return logger

    if project_root is None:
        project_root = Path(__file__).resolve().parents[2]
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | "
               "<cyan>{name}</cyan>:<cyan>{line}</cyan> - {message}",
    )
    logger.add(
        log_dir / "creator_finder_{time:YYYYMMDD}.log",
        level="DEBUG",
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
    )
    _INITIALIZED = True
    return logger
