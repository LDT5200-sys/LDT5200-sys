"""cron 友好的入口：直接调用 src.main.run，便于排程。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.main import run

if __name__ == "__main__":
    run()
