#!/usr/bin/env python
"""便捷启动脚本：``python run.py ask "审查 examples/buggy_code.py"``。

等价于 ``python -m codeagent ...``，方便不熟悉模块运行方式的同学使用。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codeagent.cli import main  # noqa: E402  (需先补充 sys.path)

if __name__ == "__main__":
    raise SystemExit(main())
