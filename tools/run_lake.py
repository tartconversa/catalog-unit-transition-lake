#!/usr/bin/env python
"""数据湖命令行引导脚本。

独立模式（python -I）下 sys.path 不含工作目录与 PYTHONPATH，
因此由本脚本显式加入项目根目录后转交 lake.cli 处理。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lake.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
