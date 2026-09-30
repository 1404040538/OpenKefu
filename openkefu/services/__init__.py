# -*- coding: utf-8 -*-
"""平台无关的共享服务层：LLM、知识库、回复缓存、意图分析等。

原先位于 platforms/pdd/chat/ 下，因千牛平台接入抽取为通用层；
pdd 侧模块保留为兼容 re-export。
"""

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent
