# -*- coding: utf-8 -*-
"""兼容 shim：实现已迁移至 openkefu.services.intent。"""
from openkefu.services.intent import (  # noqa: F401
    build_intent_messages,
    normalize_intent_result,
    clean_reply_text,
)
from openkefu.services.intent import *  # noqa: F401,F403
