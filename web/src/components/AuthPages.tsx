import { useEffect, useRef, useState } from "react";
import { Bot, Copy, Loader2, MessageSquareText, ShieldCheck, Store, UserPlus, Workflow } from "lucide-react";

const AUTH_VIDEO_SRC = "https://d8j0ntlcm91z4.cloudfront.net/user_38xzZboKViGWJOttwIXH07lWA1P/hf_20260530_042513_df96a13b-6155-4f6e-8b93-c9dee66fba08.mp4";
const SCRUB_SENSITIVITY = 0.8;

function useTypewriter(text: string, speed = 38, startDelay = 600) {
  const [displayed, setDisplayed] = useState("");
  const [done, setDone] = useState(false);

  useEffect(() => {
    setDisplayed("");
    setDone(false);
    let index = 0;
    let intervalId: number | undefined;
    const timeoutId = window.setTimeout(() => {
      intervalId = window.setInterval(() => {
        index += 1;
        setDisplayed(text.slice(0, index));
        if (index >= text.length) {
          if (intervalId) window.clearInterval(intervalId);
          setDone(true);
        }
      }, speed);
    }, startDelay);

    return () => {
      window.clearTimeout(timeoutId);
      if (intervalId) window.clearInterval(intervalId);
    };
  }, [speed, startDelay, text]);

  return { displayed, done };
}

function AuthVideoBackdrop() {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const prevXRef = useRef<number | null>(null);
  const targetTimeRef = useRef(0);
  const seekingRef = useRef(false);

  useEffect(() => {
    const seekToTarget = () => {
      const video = videoRef.current;
      if (!video || seekingRef.current || !Number.isFinite(video.duration) || video.duration <= 0) return;
      const nextTime = Math.max(0, Math.min(video.duration, targetTimeRef.current));
      if (Math.abs(video.currentTime - nextTime) < 0.015) return;
      seekingRef.current = true;
      video.currentTime = nextTime;
    };

    const handleMouseMove = (event: MouseEvent) => {
      const video = videoRef.current;
      if (!video || !Number.isFinite(video.duration) || video.duration <= 0) {
        prevXRef.current = event.clientX;
        return;
      }
      if (prevXRef.current == null) {
        prevXRef.current = event.clientX;
        return;
      }
      const delta = event.clientX - prevXRef.current;
      prevXRef.current = event.clientX;
      targetTimeRef.current = Math.max(
        0,
        Math.min(video.duration, targetTimeRef.current + (delta / window.innerWidth) * SCRUB_SENSITIVITY * video.duration),
      );
      seekToTarget();
    };

    const handleSeeked = () => {
      seekingRef.current = false;
      seekToTarget();
    };

    const video = videoRef.current;
    window.addEventListener("mousemove", handleMouseMove);
    video?.addEventListener("seeked", handleSeeked);
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      video?.removeEventListener("seeked", handleSeeked);
    };
  }, []);

  return (
    <video
      ref={videoRef}
      className="auth-video"
      src={AUTH_VIDEO_SRC}
      muted
      playsInline
      preload="auto"
    />
  );
}

function AuthShell({ children, mode }: { children: React.ReactNode; mode: "login" | "setup" | "boot" }) {
  return (
    <div className="auth-page auth-page-pdd-flow" data-auth-mode={mode}>
      <AuthVideoBackdrop />
      <div className="auth-video-wash" />
      <nav className="auth-topbar">
        <div className="auth-logo">
          <span>OpenKefu</span>
          <span className="auth-logo-mark">✳︎</span>
        </div>
        <div className="auth-topbar-links">
          <span>店铺登录</span>
          <span>客服会话</span>
          <span>知识约束</span>
          <span>售后记录</span>
        </div>
      </nav>
      {children}
    </div>
  );
}

function AuthHero({ title, typewriterText, setup = false }: { title: string; typewriterText: string; setup?: boolean }) {
  const { displayed, done } = useTypewriter(typewriterText);

  return (
    <section className="auth-ambient-panel auth-pdd-flow-copy">
      <div className="auth-blurred-label">
        <span>运营连接已就绪</span>
        <span>{setup ? "初始化后接入店铺与客服流程" : "多店铺智能客服控制台"}</span>
      </div>
      <h1 className="auth-hero-title">{title}</h1>
      <p className="auth-typewriter">
        {displayed}
        {!done && <span className="auth-typewriter-cursor" />}
      </p>
      <div className="auth-signal-grid auth-pdd-flow-pills">
        <div><Bot size={18} /><strong>AI 回复</strong><span>按知识库约束输出</span></div>
        <div><MessageSquareText size={18} /><strong>实时客服</strong><span>会话异常可接管</span></div>
        <div><Workflow size={18} /><strong>自动转接</strong><span>按店铺策略流转</span></div>
      </div>
    </section>
  );
}

export function BootPage({ message }: { message: string }) {
  return (
    <AuthShell mode="boot">
      <AuthHero
        title="正在进入 PDD 运营中枢"
        typewriterText="正在检查系统状态、权限与店铺连接，请稍候。"
      />
      <div className="auth-card auth-card-compact">
        <div className="auth-icon"><Store size={30} /></div>
        <h1 className="auth-title">加载控制台</h1>
        <p className="auth-desc">{message}</p>
        <Loader2 className="spin" size={22} />
      </div>
    </AuthShell>
  );
}

export function LoginPage({ onLogin, error, registrationEnabled, onShowRegister }: {
  onLogin: (username: string, password: string) => Promise<void>;
  error: string;
  registrationEnabled?: boolean;
  onShowRegister?: () => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);

  return (
    <AuthShell mode="login">
      <AuthHero
        title="登录 PDD 多店铺运营台"
        typewriterText="把登录状态、实时会话、知识命中与售后动作收束到同一条运营流里。"
      />
      <form
        className="auth-card auth-pdd-flow-form"
        onSubmit={async (e) => {
          e.preventDefault();
          setLoading(true);
          try { await onLogin(username, password); } catch { return; } finally { setLoading(false); }
        }}
      >
        <div className="auth-icon"><Store size={30} /></div>
        <h1 className="auth-title">登录后台</h1>
        <p className="auth-desc">使用原系统账号进入店铺客服工作区</p>
        <div className="form-group">
          <input
            className="input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="用户名"
            autoFocus
          />
        </div>
        <div className="form-group">
          <input
            className="input"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            type="password"
            placeholder="密码"
          />
        </div>
        <div className="auth-action-row">
          <button className="btn btn-primary" disabled={loading || !username.trim() || !password}>
            {loading ? <Loader2 className="spin" size={16} /> : null}
            登录
          </button>
          {registrationEnabled && onShowRegister && (
            <button className="btn btn-ghost" type="button" disabled={loading} onClick={onShowRegister}>
              注册账号
            </button>
          )}
        </div>
        {error && <div className="form-error">{error}</div>}
        <div className="auth-security-note">
          <Copy size={13} />
          <span>仅展示业务模块与能力，不暴露外部品牌、邮箱或无关样板导航。</span>
        </div>
      </form>
    </AuthShell>
  );
}

export function RegisterPage({ onRegister, onBackToLogin, error }: {
  onRegister: (username: string, password: string, displayName: string) => Promise<void>;
  onBackToLogin: () => void;
  error: string;
}) {
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [formError, setFormError] = useState("");
  const [loading, setLoading] = useState(false);
  const shownError = formError || error;

  return (
    <AuthShell mode="login">
      <AuthHero
        title="注册 PDD 运营账号"
        typewriterText="创建账号后默认拥有 100 次大模型额度、5 个知识库额度与 10 个店铺额度。"
      />
      <form
        className="auth-card auth-pdd-flow-form"
        onSubmit={async (e) => {
          e.preventDefault();
          setFormError("");
          if (password !== confirmPassword) { setFormError("两次输入的密码不一致"); return; }
          setLoading(true);
          try { await onRegister(username, password, displayName); } catch { return; } finally { setLoading(false); }
        }}
      >
        <div className="auth-icon"><UserPlus size={30} /></div>
        <h1 className="auth-title">注册账号</h1>
        <p className="auth-desc">注册后即可进入店铺客服工作区</p>
        <div className="form-group">
          <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="用户名" autoFocus />
        </div>
        <div className="form-group">
          <input className="input" value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="显示名称" />
        </div>
        <div className="form-group">
          <input className="input" value={password} onChange={(e) => setPassword(e.target.value)} type="password" placeholder="密码" />
        </div>
        <div className="form-group">
          <input className="input" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} type="password" placeholder="确认密码" />
        </div>
        <div className="auth-action-row">
          <button className="btn btn-primary" disabled={loading || !username.trim() || !password}>
            {loading ? <Loader2 className="spin" size={16} /> : null}
            {loading ? "注册中" : "注册并登录"}
          </button>
          <button className="btn btn-ghost" type="button" disabled={loading} onClick={onBackToLogin}>
            返回登录
          </button>
        </div>
        {shownError && <div className="form-error">{shownError}</div>}
      </form>
    </AuthShell>
  );
}

export function SetupAdminPage({ onSetup, error }: {
  onSetup: (username: string, password: string, displayName: string) => Promise<void>;
  error: string;
}) {
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [formError, setFormError] = useState("");
  const [loading, setLoading] = useState(false);
  const shownError = formError || error;

  return (
    <AuthShell mode="setup">
      <AuthHero
        setup
        title="先建立管理员，再接入店铺"
        typewriterText="初始化账号后，可以继续配置店铺登录、客服连接、AI 回复额度与知识库权限。"
      />
      <form
        className="auth-card auth-pdd-flow-form"
        onSubmit={async (e) => {
          e.preventDefault();
          setFormError("");
          if (password !== confirmPassword) { setFormError("两次输入的密码不一致"); return; }
          setLoading(true);
          try { await onSetup(username, password, displayName); } catch { return; } finally { setLoading(false); }
        }}
      >
        <div className="auth-icon"><ShieldCheck size={30} /></div>
        <h1 className="auth-title">首次设置管理员</h1>
        <p className="auth-desc">创建第一个后台账号后即可进入管理台</p>
        <div className="form-group">
          <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="用户名" autoFocus />
        </div>
        <div className="form-group">
          <input className="input" value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="显示名称" />
        </div>
        <div className="form-group">
          <input className="input" value={password} onChange={(e) => setPassword(e.target.value)} type="password" placeholder="密码" />
        </div>
        <div className="form-group">
          <input className="input" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} type="password" placeholder="确认密码" />
        </div>
        <button className="btn btn-primary btn-full" disabled={loading || !username.trim() || !password}>
          {loading ? <Loader2 className="spin" size={16} /> : null}
          创建管理员
        </button>
        {shownError && <div className="form-error">{shownError}</div>}
      </form>
    </AuthShell>
  );
}
