# -*- coding: utf-8 -*-
"""千牛（淘宝商家）平台常量。

登录链路（2026-09 逆向，协议细节见 auth/login.py 模块注释）：
  login.taobao.com/member/qrcode.htm (from=site)
  → qrlogin.taobao.com/qrcodelogin/generateQRCode4Login.do
  → qrlogin.taobao.com/qrcodelogin/qrcodeLoginCheck.do 轮询
  → login.taobao.com/newlogin/token/loginByIm.do 直连换会话
"""

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent.parent

QRCODE_DIR = PROJECT_ROOT / "qrcodes"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# 千牛 PC 客户端（AliWorkbench 9.x）登录 H5 使用的 appkey。
QN_APPKEY = "24585574"

# 扫码登录页（from=site 时轮询成功直接返回 loginByIm.do 换会话 URL）。
QR_PAGE_URL = (
    "https://login.taobao.com/member/qrcode.htm"
    "?from=site&qrversion=2017&appkey={appkey}&lang=zh_CN&size=150"
).format(appkey=QN_APPKEY)

# 二维码生成 / 轮询（JSONP）。
QR_GENERATE_URL = (
    "https://qrlogin.taobao.com/qrcodelogin/generateQRCode4Login.do"
    "?from=site&qrversion=2017&appkey={appkey}"
).format(appkey=QN_APPKEY)
QR_CHECK_URL = "https://qrlogin.taobao.com/qrcodelogin/qrcodeLoginCheck.do"

# 轮询成功后直连换 web 会话（Set-Cookie 全套登录态；无需 baxia 参数）。
LOGIN_COMPLETE_URL = "https://login.taobao.com/newlogin/token/loginByIm.do?_bx-v=2.5.37"

# 会话有效性验证目标。
QN_HOME_URL = "https://qn.taobao.com/home.html"
MTOP_H5_API = "https://h5api.m.taobao.com/h5/{api}/{version}/"
# 旺旺/工作台 H5 实际使用的 mtop appKey（_m_h5_tk 与之绑定，错配会报 PARAMINVALID）。
MTOP_H5_APPKEY = "12574478"
