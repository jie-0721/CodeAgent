"""测试包。

在这里把项目根目录加入 ``sys.path``，保证 ``python -m unittest discover -s tests``
与 ``pytest tests`` 两种方式都能正确导入 ``codeagent``。
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
