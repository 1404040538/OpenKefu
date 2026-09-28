import { AlertCircle, CheckCircle2, Loader2 } from "lucide-react";
import type { Shop } from "../types/types";

export function QrLoginModal({ shop, qrcodeUrl, onClose }: {
  shop: Shop | null;
  qrcodeUrl?: string;
  onClose: () => void;
}) {
  const status = shop?.status || "login_pending";
  const progress = loginProgressMeta(status, shop?.last_error);
  const showQrCode = Boolean(qrcodeUrl) && (status === "qr_pending" || status === "login_pending");

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h2 className="modal-title">{shop?.name || "店铺登录"}</h2>
            <p className="modal-subtitle">扫码、登录和客服连接进度会实时更新</p>
          </div>
          <button className="btn btn-ghost" onClick={onClose}>关闭</button>
        </div>
        <div className="qr-stage">
          {showQrCode ? (
            <img className="qr-large" src={qrcodeUrl} alt="登录二维码" />
          ) : (
            <div className={`qr-state-icon ${progress.tone}`}>
              {progress.icon === "success" ? <CheckCircle2 size={40} /> : progress.icon === "error" ? <AlertCircle size={40} /> : <Loader2 className="spin" size={40} />}
            </div>
          )}
          <div className={`qr-status qr-status-${progress.tone}`}>
            <strong>{progress.title}</strong>
            <span>{progress.detail}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function loginProgressMeta(status: string, error?: string) {
  const map: Record<string, { title: string; detail: string; tone: "pending" | "success" | "error"; icon: "loading" | "success" | "error" }> = {
    login_pending: { title: "正在准备登录", detail: "正在初始化登录环境并生成二维码", tone: "pending", icon: "loading" },
    qr_pending: { title: "等待扫码", detail: "请使用拼多多商家版扫码登录", tone: "pending", icon: "loading" },
    qr_scanned: { title: "已扫码", detail: "请在手机端确认登录", tone: "pending", icon: "loading" },
    login_success: { title: "登录成功", detail: "正在保存登录信息", tone: "success", icon: "success" },
    logged_in: { title: "登录成功", detail: "正在建立客服连接", tone: "success", icon: "success" },
    connecting: { title: "正在建立客服连接", detail: "即将同步客服会话", tone: "pending", icon: "loading" },
    online: { title: "客服连接已建立", detail: "店铺已在线，弹窗即将关闭", tone: "success", icon: "success" },
    error: { title: "登录失败", detail: error || "请关闭弹窗后重新扫码", tone: "error", icon: "error" },
  };
  return map[status] || { title: "正在处理登录", detail: "请稍候", tone: "pending", icon: "loading" };
}
