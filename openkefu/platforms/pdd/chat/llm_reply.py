# -*- coding: utf-8 -*-
"""兼容 shim：实现已迁移至 openkefu.services.llm_reply。"""
from openkefu.services.llm_reply import (  # noqa: F401
    analyze_customer_intent,
)
from openkefu.services.llm_reply import *  # noqa: F401,F403
