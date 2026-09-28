import { useState } from "react";
import { AlertCircle, Loader2, Store } from "lucide-react";
import type { Shop } from "../types/types";
import { usePendingActions } from "../utils/usePendingActions";

export type VerifyInfo = {
  verify_type: "mobile" | "captcha";
  mask_mobile: string;
};

export function PasswordLoginModal({ shop, initialVerifyInfo = null, onLogin, onSendSms, onVerify, onClose }: {
  shop: Shop | null;
  initialVerifyInfo?: VerifyInfo | null;
  onLogin: (username: string, password: string, remember: boolean) => Promise<any>;
  onSendSms: () => Promise<void>;
  onVerify: (code: string) => Promise<void>;
  onClose: () => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [verifyCode, setVerifyCode] = useState("");
  const [remember, setRemember] = useState(true);
  const [loading, setLoading] = useState(false);
  const [sendingSms, setSendingSms] = useState(false);
  const [smsCountdown, setSmsCountdown] = useState(0);
  const [error, setError] = useState("");
  const [verifyInfo, setVerifyInfo] = useState<VerifyInfo | null>(initialVerifyInfo);
  const [dismissedError, setDismissedError] = useState(false);
  const { runAction } = usePendingActions();
  const status = shop?.status || "login_pending";

  const isCaptcha = verifyInfo?.verify_type === "captcha";

  // error 状态必须优先于其他分支展示，且提供"重新登录"入口，
  // 避免后台登录失败（身份冲突/连接失败等）时弹窗无任何提示。
  const isFailed = status === "error" && !dismissedError && !verifyInfo;
  const isSuccess = status === "online" || status === "logged_in";
  const isRunning = !verifyInfo && ["login_pending", "qr_pending", "qr_scanned", "login_success", "logged_in", "connecting"].includes(status);
  const showForm = !isFailed && !isSuccess && !(isRunning || loading);

  function handleRetry() {
    setDismissedError(true);
    setVerifyInfo(null);
    setVerifyCode("");
    setError("");
  }

  async function handleSendSms() {
    await runAction(`password-login-sms-${shop?.id || "none"}`, async () => {
      setSendingSms(true);
      setError("");
      try {
        await onSendSms();
        setSmsCountdown(60);
        const timer = window.setInterval(() => {
          setSmsCountdown((current) => {
            if (current <= 1) { window.clearInterval(timer); return 0; }
            return current - 1;
          });
        }, 1000);
      } catch (err: any) {
        setError(err.message || "发送验证码失败");
      } finally {
        setSendingSms(false);
      }
    });
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (verifyInfo) {
      if (!verifyCode.trim()) return;
      await runAction(`password-login-verify-${shop?.id || "none"}`, async () => {
        setError("");
        setLoading(true);
        try {
          await onVerify(verifyCode.trim());
        } catch (err: any) {
          setError(err.message || "验证失败");
        } finally {
          setLoading(false);
        }
      });
      return;
    }

    if (!username.trim() || !password.trim()) return;
    await runAction(`password-login-submit-${shop?.id || "none"}`, async () => {
      setError("");
      setLoading(true);
      try {
        const result = await onLogin(username.trim(), password, remember);
        if (result && result.need_verify) {
          setVerifyInfo({
            verify_type: result.verify_type,
            mask_mobile: result.mask_mobile || "",
          });
        }
      } catch (err: any) {
        setError(err.message || "登录失败");
      } finally {
        setLoading(false);
      }
    });
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h2 className="modal-title">{shop?.name || "账密登录"}</h2>
            <p className="modal-subtitle">
              {isSuccess
                ? "登录成功，正在建立客服连接"
                : isFailed
                ? "登录失败"
                : isRunning || loading
                ? verifyInfo
                  ? "正在验证短信验证码"
                  : "正在登录中，请稍候"
                : verifyInfo
                ? isCaptcha
                  ? "登录需要图形验证码"
                  : `请输入发送到 ${verifyInfo.mask_mobile || "绑定手机"} 的短信验证码`
                : "输入拼多多商家账号和密码登录"}
            </p>
          </div>
          <button className="btn btn-ghost" onClick={onClose} disabled={loading}>关闭</button>
        </div>

        {isFailed ? (
          <div className="qr-stage">
            <div className="qr-state-icon error">
              <AlertCircle size={40} />
            </div>
            <div className="qr-status qr-status-error">
              <strong>登录失败</strong>
              <span>{shop?.last_error || error || "请关闭弹窗后重试"}</span>
            </div>
            <div className="modal-actions">
              <button className="btn btn-primary btn-full" onClick={handleRetry}>重新登录</button>
            </div>
          </div>
        ) : isSuccess || isRunning || loading ? (
          <div className="qr-stage">
            <div className={`qr-state-icon ${isSuccess ? "success" : "pending"}`}>
              {isSuccess ? <Store size={40} /> : <Loader2 className="spin" size={40} />}
            </div>
            <div className={`qr-status qr-status-${isSuccess ? "success" : "pending"}`}>
              <strong>{isSuccess ? "登录成功" : verifyInfo ? "正在验证短信验证码" : "正在登录"}</strong>
              <span>{isSuccess ? "即将建立客服连接" : verifyInfo ? "正在验证短信验证码" : "正在验证账号密码"}</span>
            </div>
          </div>
        ) : (
          <form className="modal-body" onSubmit={handleSubmit}>
            {!verifyInfo ? (
              <>
                <div className="form-group">
                  <label className="form-label">手机号</label>
                  <input
                    className="input"
                    value={username}
                    onChange={(e) => setUsername(e.target.value)}
                    placeholder="输入拼多多商家手机号"
                    autoFocus
                  />
                </div>
                <div className="form-group">
                  <label className="form-label">密码</label>
                  <input
                    className="input"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    type="password"
                    placeholder="输入密码"
                  />
                </div>
                <label className="checkbox-row" style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: "var(--text-secondary)", cursor: "pointer" }}>
                  <input
                    type="checkbox"
                    checked={remember}
                    onChange={(e) => setRemember(e.target.checked)}
                    style={{ width: 14, height: 14, cursor: "pointer" }}
                  />
                  记住账号密码，登录过期后自动重新登录
                </label>
              </>
            ) : isCaptcha ? (
              <div className="form-group">
                <div className="form-error" style={{ marginTop: 0 }}>
                  需要完成图形验证码才能继续登录。当前版本暂不支持自动通过图形验证码，请稍后重试，或关闭弹窗后重新登录。
                </div>
              </div>
            ) : (
              <div className="form-group">
                <label className="form-label">
                  短信验证码
                  {verifyInfo.mask_mobile && (
                    <span style={{ fontWeight: 400, color: "var(--text-secondary)", marginLeft: 8 }}>
                      已发送至 {verifyInfo.mask_mobile}
                    </span>
                  )}
                </label>
                <div className="inline-field">
                  <input
                    className="input"
                    value={verifyCode}
                    onChange={(e) => setVerifyCode(e.target.value.replace(/\D/g, "").slice(0, 6))}
                    placeholder="输入 6 位验证码"
                    autoFocus
                    maxLength={6}
                  />
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={handleSendSms}
                    disabled={sendingSms || smsCountdown > 0}
                  >
                    {sendingSms ? <Loader2 className="spin" size={14} /> : null}
                    {smsCountdown > 0 ? `${smsCountdown}s` : "发送验证码"}
                  </button>
                </div>
              </div>
            )}

            {error && <div className="form-error">{error}</div>}

            <button
              className="btn btn-primary btn-full"
              disabled={loading || isCaptcha || (!verifyInfo && (!username.trim() || !password.trim())) || (!!verifyInfo && !verifyCode.trim())}
            >
              {loading ? <Loader2 className="spin" size={16} /> : null}
              {isCaptcha ? "请稍后重试" : verifyInfo ? "验证并登录" : "登录"}
            </button>
          </form>
        )}
      </div>
    </div>
  );
}
