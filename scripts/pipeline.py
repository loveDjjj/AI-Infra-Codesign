#!/usr/bin/env python3
"""兼容旧调用入口；主控逻辑由项目包维护。"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from codesign_lab.search.pipeline import Pipeline, main, select_jobs

if __name__ == '__main__':
    main()
