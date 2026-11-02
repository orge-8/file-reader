# -*- coding: utf-8 -*-
"""pytest 公共夹具：把插件根目录放进 sys.path，让测试能直接 import 插件模块。

pytest 只把「测试文件所在目录」插入 sys.path（rootdir 下没有 __init__.py），
插件模块（chunker / file_parser / vector_store / lexical_index / plugin）都在
tests/ 的上一级，不补这一行所有新测试都会 ModuleNotFoundError。
"""

import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent

for _path in (str(PLUGIN_DIR), str(TESTS_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
