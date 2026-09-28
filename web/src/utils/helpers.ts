export function upsert<T extends { id: number }>(items: T[], item: T): T[] {
  const exists = items.some((current) => current.id === item.id);
  return exists ? items.map((current) => (current.id === item.id ? item : current)) : [item, ...items];
}

export function upsertAppend<T extends { id: number }>(items: T[], item: T): T[] {
  const exists = items.some((current) => current.id === item.id);
  return exists ? items.map((current) => (current.id === item.id ? item : current)) : [...items, item];
}

export function safeJson(text?: string): any {
  if (!text) return null;
  try { return JSON.parse(text); } catch { return null; }
}

export function trimRaw(text?: string): string {
  return (text || "未知消息").slice(0, 320);
}

export function readError(text: string): string {
  try {
    const data = JSON.parse(text);
    return data.detail || data.message || text;
  } catch {
    return text;
  }
}

export function formatBytes(value: number): string {
  if (!value) return "0 B";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

export function parseTransferCsids(raw?: string): string[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((s: any) => typeof s === "string" && s.trim()) : [];
  } catch {
    return [];
  }
}

export function statusText(status: string): string {
  const map: Record<string, string> = {
    idle: "未启动",
    login_pending: "登录中",
    qr_pending: "待扫码",
    qr_scanned: "已扫码",
    login_success: "登录成功",
    logged_in: "已登录",
    connecting: "连接中",
    online: "在线",
    offline: "已下线",
    stopped: "已停止",
    active: "启用",
    disabled: "停用",
    inactive: "停用",
    error: "异常",
  };
  return map[status] || status || "未知";
}

export function messageStatusText(status: string): string {
  const map: Record<string, string> = {
    sending: "发送中",
    sent: "已发送",
    received: "已接收",
    failed: "失败",
  };
  return map[status] || status || "";
}
