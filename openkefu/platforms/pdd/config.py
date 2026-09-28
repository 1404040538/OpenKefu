from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent.parent

LOGIN_JS_DIR = PACKAGE_ROOT / "auth" / "login_js"
QRCODE_API_JS = LOGIN_JS_DIR / "qrcode_api.js"
PASSWORD_LOGIN_JS = LOGIN_JS_DIR / "password_login.js"
SEC_WEBSOCKET_KEY_JS = LOGIN_JS_DIR / "sec_websocket_key.js"
PFB_TEMPLATE_FILE = PACKAGE_ROOT / "auth" / "pfb_template.json"
QRCODE_DIR = PROJECT_ROOT / "qrcodes"

BASE_URL = "https://mms.pinduoduo.com"
XG_BASE_URL = "https://xg.pinduoduo.com"
LOGIN_URL = f"{BASE_URL}/login/?redirectUrl=https%3A%2F%2Fmms.pinduoduo.com%2Fhome%2F"
HOME_URL = f"{BASE_URL}/home/"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
WS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
)

WS_BASE_URL = "wss://m-ws.pinduoduo.com/"
TITAN_URL = "wss://titan-ws.pinduoduo.com/"
TITAN_HOST = "mms.pinduoduo.com"
TITAN_SUB_SYSTEM_ID = 17

MMS_APP_JS_URL = "https://mms-static.pddpic.com/main/_next/static/4lKYNnheBX02UcX58Iq2d/pages/_app.js"
MMS_COOKIE_NAME = "mms_b84d1838"
MMS_LEON_SIDEBAR_TYPE = "f467c3caa379512dde18fde6f054b"

# 可以填写字体文件路径、base64 或 data:font/...；留空时使用内置映射。
SPIDER_FONT_SOURCE = ""
