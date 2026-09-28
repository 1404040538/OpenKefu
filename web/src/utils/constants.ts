export const API_BASE = "";
export const LOGIN_FINISHED_STATUSES = new Set(["logged_in", "offline", "online"]);
export const QR_LOGIN_AUTO_CLOSE_STATUSES = new Set(["online"]);

export const VIEW_TITLES: Record<string, string> = {
  shops: "店铺管理",
  chat: "客服聊天",
  knowledge: "知识库",
  notes: "特别注意事项",
  users: "账号管理",
  logs: "运行日志",
  profile: "个人中心",
};

export const STATUS_TEXT: Record<string, string> = {
  idle: "未启动",
  login_pending: "登入中",
  qr_pending: "待扫码",
  qr_scanned: "已扫码",
  login_success: "登录成功",
  logged_in: "已登入",
  connecting: "上线中",
  online: "在线",
  offline: "已下线",
  stopped: "已停止",
  error: "异常",
};

export const MESSAGE_STATUS_TEXT: Record<string, string> = {
  sending: "发送中",
  sent: "已发送",
  received: "已接收",
  failed: "失败",
};
