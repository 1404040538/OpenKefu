# -*- coding: utf-8 -*-
"""千牛客服聊天平台层（登录 + impaas 长连接）。"""

from openkefu.platforms.qianniu.auth.login import Login
from openkefu.platforms.qianniu.chat.impaas_client import ImpaasClient, ImpaasClientConfig
from openkefu.platforms.qianniu.chat.models import ImpaasConversation, ImpaasMessage

__all__ = [
    "Login",
    "ImpaasClient",
    "ImpaasClientConfig",
    "ImpaasConversation",
    "ImpaasMessage",
]
