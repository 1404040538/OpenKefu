# -*- coding: utf-8 -*-
"""兼容 shim：实现已迁移至 openkefu.services.knowledge。"""
from openkefu.services.knowledge import (  # noqa: F401
    KnowledgeService,
    NoteSetService,
)
from openkefu.services.knowledge import *  # noqa: F401,F403
