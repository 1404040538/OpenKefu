# -*- coding: utf-8 -*-
"""兼容 shim：实现已迁移至 openkefu.services.llm。"""
from openkefu.services.llm import (  # noqa: F401
    LLMClient,
    get_llm_client,
)
from openkefu.services.llm import *  # noqa: F401,F403
