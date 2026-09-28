"""Backward-compatible shim: the implementation moved to ``openkefu.web.runtime``."""

from openkefu.web.runtime import PasswordVerificationRequired, ShopRunner, ShopRuntimeManager

__all__ = ["PasswordVerificationRequired", "ShopRunner", "ShopRuntimeManager"]
