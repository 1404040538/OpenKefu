"""Shop runtime: per-shop runner and fleet manager.

Historical import path ``openkefu.web.shop_runtime`` remains available as a shim.
"""

from openkefu.web.runtime.manager import ShopRuntimeManager
from openkefu.web.runtime.shop_runner import PasswordVerificationRequired, ShopRunner

__all__ = ["PasswordVerificationRequired", "ShopRunner", "ShopRuntimeManager"]
