#!/usr/bin/env python3
"""启动 FDL Frank UI 占位页。

用法：python scripts/launch_ui.py
出口门验证：启动后浏览器访问 http://localhost:8080 应显示 Frank UI 主题占位。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fdl.ui.app import main  # noqa: E402

if __name__ == "__main__":
    main()
