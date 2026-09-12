"""上位机入口：启动 Tk 界面和内嵌 TCP 路由服务器。

代码分层见 ``host/cable_tester/``；本文件只保留文档里记录的启动命令：

    python cable_tester_gui.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "host"))

from cable_tester.ui.gui import main

if __name__ == "__main__":
    main()
