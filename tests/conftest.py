"""Pytest 根配置 —— 将项目根目录加入 sys.path，保证 ``src.*`` 可导入。"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
