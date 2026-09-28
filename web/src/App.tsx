import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, BookOpen, Bot, CircleUserRound, Gauge, LogOut, MessageSquareText, PackageSearch, Radio, Server, Store, Users } from "lucide-react";
import type { User, Shop, Conversation, Message, LogResponse, KnowledgeBase, NoteSet, Quota, ViewName, AuthMode, ShopFilter, CustomerContext, ReturnRecordResponse, ServerStatusHistory, ServerStatusLatest } from "./types/types";
import { NavButton } from "./components/NavButton";
import { Toast } from "./components/Toast";
import { BootPage, LoginPage, RegisterPage, SetupAdminPage } from "./components/AuthPages";
import { ShopPage } from "./components/ShopPage";
import { ChatPage } from "./components/ChatPage";
import { KnowledgePage } from "./components/KnowledgePage";
import { ReturnRecordsPage } from "./components/ReturnRecordsPage";
import { UsersPage } from "./components/UsersPage";
import { LogsPage } from "./components/LogsPage";
import { ProfilePage } from "./components/ProfilePage";
import { ServerStatusPage } from "./components/ServerStatusPage";
import { TransferSettingsModal } from "./components/TransferSettingsModal";
import { QrLoginModal } from "./components/QrLoginModal";
import { PasswordLoginModal, type VerifyInfo } from "./components/PasswordLoginModal";
import { upsert, upsertAppend, readError } from "./utils/helpers";
import { QR_LOGIN_AUTO_CLOSE_STATUSES } from "./utils/constants";

const API_BASE = "";

function sortConversations(items: Conversation[]): Conversation[] {
  return [...items].sort((a, b) => {
    const attention = Number(Boolean(b.human_attention_required)) - Number(Boolean(a.human_attention_required));
    if (attention) return attention;
    const attentionTime = Date.parse(b.human_attention_at || "") - Date.parse(a.human_attention_at || "");
    if (Number.isFinite(attentionTime) && attentionTime) return attentionTime;
    const updatedTime = Date.parse(b.updated_at || "") - Date.parse(a.updated_at || "");
    if (Number.isFinite(updatedTime) && updatedTime) return updatedTime;
    return b.id - a.id;
  });
}

function playAttentionSound() {
  try {
    const AudioContextCtor = window.AudioContext || (window as any).webkitAudioContext;
    if (!AudioContextCtor) return;
    const context = new AudioContextCtor();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    oscillator.type = "sine";
    oscillator.frequency.value = 880;
    gain.gain.setValueAtTime(0.001, context.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.18, context.currentTime + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.45);
    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start();
    oscillator.stop(context.currentTime + 0.48);
    window.setTimeout(() => context.close().catch(() => undefined), 700);
  } catch {
    // Browser autoplay policy can block sound; visual attention remains active.
  }
}

export function App() {
  const [token, setToken] = useState(() => localStorage.getItem("pdd_web_token") || "");
  const [user, setUser] = useState<User | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [authMode, setAuthMode] = useState<AuthMode>("checking");
  const [authError, setAuthError] = useState("");
  const [registrationEnabled, setRegistrationEnabled] = useState(true);
  const [view, setView] = useState<ViewName>("shops");
  const [shops, setShops] = useState<Shop[]>([]);
  const [users, setUsers] = useState<User[]>([]);
  const [adminSettings, setAdminSettings] = useState({ registration_enabled: true });
  const [logData, setLogData] = useState<LogResponse | null>(null);
  const [logFilters, setLogFilters] = useState<Record<string, string>>({});
  const [serverStatusLatest, setServerStatusLatest] = useState<ServerStatusLatest | null>(null);
  const [serverStatusHistory, setServerStatusHistory] = useState<ServerStatusHistory | null>(null);
  const [serverStatusRange, setServerStatusRange] = useState<ServerStatusHistory["range"]>("1h");
  const [returnRecordData, setReturnRecordData] = useState<ReturnRecordResponse | null>(null);
  const [returnRecordFilters, setReturnRecordFilters] = useState<Record<string, string>>({});
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [noteSets, setNoteSets] = useState<NoteSet[]>([]);
  const [selectedShopId, setSelectedShopId] = useState<number | null>(null);
  const [chatShopFilter, setChatShopFilter] = useState<ShopFilter>("all");
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedConversationId, setSelectedConversationId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [customerContext, setCustomerContext] = useState<CustomerContext | null>(null);
  const [qrcodeUrls, setQrcodeUrls] = useState<Record<number, string>>({});
  const [qrModalShopId, setQrModalShopId] = useState<number | null>(null);
  const [pwdModalShopId, setPwdModalShopId] = useState<number | null>(null);
  const [pwdVerifyPrompt, setPwdVerifyPrompt] = useState<{ shopId: number; verifyInfo: VerifyInfo } | null>(null);
  const [transferSettingsShopId, setTransferSettingsShopId] = useState<number | null>(null);
  const [transferServices, setTransferServices] = useState<any[]>([]);
  const [quota, setQuota] = useState<Quota | null>(null);
  const [toast, setToast] = useState("");
  const wsRef = useRef<WebSocket | null>(null);
  const selectedConversationIdRef = useRef<number | null>(null);
  const chatShopFilterRef = useRef<ShopFilter>("all");
  const notifiedAttentionIdsRef = useRef<Set<number>>(new Set());
  const wsReconnectTimerRef = useRef<number | null>(null);
  const conversationRefreshTimerRef = useRef<number | null>(null);
  const contextRefreshTimerRef = useRef<number | null>(null);
  const conversationsRequestRef = useRef(0);
  const messagesRequestRef = useRef(0);
  const contextRequestRef = useRef(0);

  const apiFetch = useCallback(async (path: string, init: RequestInit = {}) => {
    const headers = new Headers(init.headers || {});
    if (!(init.body instanceof FormData)) headers.set("Content-Type", headers.get("Content-Type") || "application/json");
    if (token) headers.set("Authorization", `Bearer ${token}`);
    const response = await fetch(`${API_BASE}${path}`, { ...init, headers });
    if (!response.ok) { const text = await response.text(); throw new Error(readError(text) || response.statusText); }
    return response;
  }, [token]);

  useEffect(() => {
    if (token) { setAuthReady(true); setAuthMode("login"); setAuthError(""); return; }
    let cancelled = false;
    setUser(null); setAuthReady(false); setAuthMode("checking");
    (async () => {
      try {
        const response = await fetch("/api/auth/status");
        if (!response.ok) throw new Error(readError(await response.text()) || "读取初始化状态失败");
        const data = await response.json();
        if (cancelled) return;
        setRegistrationEnabled(Boolean(data.registration_enabled));
        setAuthMode(data.initialized ? "login" : "setup"); setAuthError("");
      } catch (error: any) {
        if (!cancelled) { setAuthMode("login"); setAuthError(error.message || String(error)); }
      } finally { if (!cancelled) setAuthReady(true); }
    })();
    return () => { cancelled = true; };
  }, [token]);

  const applyShopRows = useCallback((shopRows: Shop[]) => {
    setShops(shopRows);
    setSelectedShopId((current) => current && shopRows.some((s) => s.id === current) ? current : shopRows[0]?.id || null);
  }, []);

  const loadShops = useCallback(async () => {
    if (!token) return [];
    const response = await apiFetch("/api/shops");
    const rows = await response.json();
    applyShopRows(rows);
    return rows;
  }, [apiFetch, applyShopRows, token]);

  const loadQuota = useCallback(async () => {
    if (!token) return null;
    const response = await apiFetch("/api/me/quota");
    const data = await response.json();
    setQuota(data);
    return data;
  }, [apiFetch, token]);

  const loadAll = useCallback(async () => {
    if (!token) return;
    const [meRes, shopRes, kbRes, nsRes, quotaRes] = await Promise.all([
      apiFetch("/api/me"), apiFetch("/api/shops"),
      apiFetch("/api/knowledge-bases"), apiFetch("/api/note-sets"), apiFetch("/api/me/quota"),
    ]);
    const me = await meRes.json();
    const shopRows = await shopRes.json();
    setUser(me); applyShopRows(shopRows);
    setKnowledgeBases(await kbRes.json());
    setNoteSets(await nsRes.json());
    setQuota(await quotaRes.json());
    if (me.role === "admin") {
      const [userRes, logRes, settingsRes] = await Promise.all([apiFetch("/api/users"), apiFetch("/api/logs"), apiFetch("/api/admin/settings")]);
      setUsers(await userRes.json());
      setLogData(await logRes.json());
      const settings = await settingsRes.json();
      setAdminSettings(settings);
      setRegistrationEnabled(Boolean(settings.registration_enabled));
    } else {
      setLogData(null);
    }
  }, [apiFetch, applyShopRows, token]);

  useEffect(() => { loadAll().catch((error) => {
    setToast(error.message);
    if (String(error.message).includes("token")) { localStorage.removeItem("pdd_web_token"); setToken(""); }
  }); }, [loadAll]);

  useEffect(() => { selectedConversationIdRef.current = selectedConversationId; }, [selectedConversationId]);
  useEffect(() => { chatShopFilterRef.current = chatShopFilter; }, [chatShopFilter]);

  useEffect(() => {
    if (!token) return;
    let disposed = false;
    let reconnectAttempt = 0;
    let fallbackRefreshTimer: number | null = null;
    const scheduleConversationRefresh = (filter: ShopFilter) => {
      if (conversationRefreshTimerRef.current) window.clearTimeout(conversationRefreshTimerRef.current);
      conversationRefreshTimerRef.current = window.setTimeout(() => {
        conversationRefreshTimerRef.current = null;
        loadConversations(filter).catch(() => undefined);
      }, 300);
    };
    const scheduleContextRefresh = (conversationId: number) => {
      if (contextRefreshTimerRef.current) window.clearTimeout(contextRefreshTimerRef.current);
      contextRefreshTimerRef.current = window.setTimeout(() => {
        contextRefreshTimerRef.current = null;
        loadConversationContext(conversationId).catch(() => undefined);
      }, 500);
    };
    const refreshWhileDisconnected = () => {
      const socket = wsRef.current;
      if (socket?.readyState === WebSocket.OPEN) return;
      const filter = chatShopFilterRef.current;
      loadConversations(filter).catch(() => undefined);
      const conversationId = selectedConversationIdRef.current;
      if (conversationId) {
        loadMessages(conversationId).catch(() => undefined);
        loadConversationContext(conversationId).catch(() => undefined);
      }
    };
    const connect = () => {
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      // 令牌经 WebSocket subprotocol 传输，避免出现在 URL（历史记录/访问日志）
      const ws = new WebSocket(`${protocol}://${location.host}/ws`, [`openkefu-auth.${token}`]);
      wsRef.current = ws;
      ws.onopen = () => { reconnectAttempt = 0; };
      ws.onmessage = (event) => {
        let payload: any;
        try {
          payload = JSON.parse(event.data);
        } catch {
          return;
        }
        if (!payload || typeof payload !== "object") return;
        if (payload.type === "shop_status") {
          setShops((items) => upsert(items, payload.data));
        }
        if (payload.type === "qrcode") { fetchQrcode(payload.data.shop_id, payload.data.qrcode_url).catch((error) => setToast(error.message)); }
        if (payload.type === "message") {
          const msg = payload.data as Message;
          if (msg.conversation_id === selectedConversationIdRef.current) {
            setMessages((items) => upsertAppend(items, msg));
            scheduleContextRefresh(msg.conversation_id);
          }
          const filter = chatShopFilterRef.current;
          if (filter === "all" || msg.shop_id === filter) scheduleConversationRefresh(filter);
        }
        if (payload.type === "conversation" || payload.type === "conversation_attention") {
          const conversation = payload.data as Conversation;
          const filter = chatShopFilterRef.current;
          if (filter === "all" || conversation.shop_id === filter) {
            setConversations((items) => sortConversations(upsert(items, conversation)));
          }
          if (conversation.human_attention_required) {
            if (!notifiedAttentionIdsRef.current.has(conversation.id)) {
              notifiedAttentionIdsRef.current.add(conversation.id);
              playAttentionSound();
            }
          } else {
            notifiedAttentionIdsRef.current.delete(conversation.id);
          }
        }
        if (payload.type === "runtime_log") {
          if (view === "logs") loadLogs(logFilters).catch(() => undefined);
        }
        if (payload.type === "server_status") {
          setServerStatusLatest(payload.data as ServerStatusLatest);
        }
        if (payload.type === "return_record") {
          if (view === "returnRecords") loadReturnRecords(returnRecordFilters).catch(() => undefined);
          setToast("已新增退换记录");
        }
        if (payload.type === "return_record_deleted") {
          if (view === "returnRecords") loadReturnRecords(returnRecordFilters).catch(() => undefined);
          setToast("已删除退换记录");
        }
        if (payload.type === "reply_result" || payload.type === "transfer_result") setToast(payload.data.status === "success" ? "操作成功" : payload.data.error || "操作失败");
        if (payload.type === "action_request") setToast(payload.data?.action_type === "transfer_to_human" ? "已预留转人工处理" : "已记录后续动作");
        if (payload.type === "llm_quota_exceeded") loadQuota().catch(() => undefined);
      };
      ws.onclose = () => {
        if (disposed) return;
        const delay = Math.min(15000, 1000 * 2 ** reconnectAttempt);
        reconnectAttempt += 1;
        wsReconnectTimerRef.current = window.setTimeout(connect, delay);
      };
    };
    fallbackRefreshTimer = window.setInterval(refreshWhileDisconnected, 5000);
    connect();
    return () => {
      disposed = true;
      if (fallbackRefreshTimer) window.clearInterval(fallbackRefreshTimer);
      if (wsReconnectTimerRef.current) window.clearTimeout(wsReconnectTimerRef.current);
      if (conversationRefreshTimerRef.current) window.clearTimeout(conversationRefreshTimerRef.current);
      if (contextRefreshTimerRef.current) window.clearTimeout(contextRefreshTimerRef.current);
      wsRef.current?.close();
    };
  }, [token]);


  const selectedConversation = useMemo(() => conversations.find((c) => c.id === selectedConversationId) || null, [conversations, selectedConversationId]);
  const activeChatShop = useMemo(() => {
    const filtered = chatShopFilter === "all" ? null : shops.find((s) => s.id === chatShopFilter) || null;
    const convShop = shops.find((s) => s.id === selectedConversation?.shop_id) || null;
    return filtered || convShop;
  }, [chatShopFilter, shops, selectedConversation]);
  const qrModalShop = useMemo(
    () => qrModalShopId ? shops.find((s) => s.id === qrModalShopId) || null : null,
    [qrModalShopId, shops],
  );
  const pwdModalShop = useMemo(
    () => pwdModalShopId ? shops.find((s) => s.id === pwdModalShopId) || null : null,
    [pwdModalShopId, shops],
  );

  useEffect(() => {
    if (!qrModalShop || !QR_LOGIN_AUTO_CLOSE_STATUSES.has(qrModalShop.status)) return;
    const timer = window.setTimeout(() => {
      setQrModalShopId((currentShopId) => currentShopId === qrModalShop.id ? null : currentShopId);
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [qrModalShop]);

  useEffect(() => {
    if (!pwdModalShop || !QR_LOGIN_AUTO_CLOSE_STATUSES.has(pwdModalShop.status)) return;
    const timer = window.setTimeout(() => {
      setPwdModalShopId((currentShopId) => currentShopId === pwdModalShop.id ? null : currentShopId);
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [pwdModalShop]);

  const showShops = useCallback(() => { setView("shops"); loadShops().catch((error) => setToast(error.message)); }, [loadShops]);

  function focusChatShop(shopId: number) {
    setSelectedShopId(shopId);
    setChatShopFilter(shopId);
    setSelectedConversationId(null);
    setMessages([]);
    setCustomerContext(null);
    setTransferServices([]);
  }

  async function login(username: string, password: string) {
    setAuthError("");
    const response = await fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
    if (!response.ok) { const msg = readError(await response.text()) || "登录失败"; setAuthError(msg); throw new Error(msg); }
    const data = await response.json();
    localStorage.setItem("pdd_web_token", data.token);
    setToken(data.token); setUser(data.user);
  }

  async function register(username: string, password: string, displayName: string) {
    setAuthError("");
    const response = await fetch("/api/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, display_name: displayName }),
    });
    if (!response.ok) { const msg = readError(await response.text()) || "注册失败"; setAuthError(msg); throw new Error(msg); }
    const data = await response.json();
    localStorage.setItem("pdd_web_token", data.token);
    setToken(data.token); setUser(data.user); setAuthMode("login");
  }

  async function passwordShopLogin(shopId: number, username: string, password: string, remember: boolean) {
    if (remember) {
      await apiFetch(`/api/shops/${shopId}/login-credentials`, {
        method: "POST",
        body: JSON.stringify({ username, password }),
      }).catch(() => undefined);
    }
    const response = await apiFetch(`/api/shops/${shopId}/password-login`, {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    const data = await response.json();
    if (!data.need_verify) {
      await loadAll();
      focusChatShop(shopId);
    }
    return data;
  }

  async function passwordShopSendSms(shopId: number) {
    await apiFetch(`/api/shops/${shopId}/password-login/send-sms`, { method: "POST" });
  }

  async function passwordShopVerify(shopId: number, code: string) {
    await apiFetch(`/api/shops/${shopId}/password-login/verify`, {
      method: "POST",
      body: JSON.stringify({ verify_code: code }),
    });
    await loadAll();
    focusChatShop(shopId);
  }

  async function onlineShop(shopId: number) {
    try {
      const response = await apiFetch(`/api/shops/${shopId}/online`, { method: "POST" });
      const result = await response.json();
      await loadAll();
      if (result?.need_verify) {
        setPwdVerifyPrompt({
          shopId,
          verifyInfo: {
            verify_type: result.verify_type === "captcha" ? "captcha" : "mobile",
            mask_mobile: result.mask_mobile || "",
          },
        });
        setPwdModalShopId(shopId);
        setToast(result.message || "需要完成登录验证");
        return;
      }
      focusChatShop(shopId);
      setToast("店铺已上线");
    } catch (error: any) {
      setToast(error.message || String(error));
      throw error;
    }
  }

  async function setupAdmin(username: string, password: string, displayName: string) {
    setAuthError("");
    const response = await fetch("/api/auth/setup-admin", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password, display_name: displayName }) });
    if (!response.ok) { const msg = readError(await response.text()) || "初始化管理员失败"; setAuthError(msg); throw new Error(msg); }
    const data = await response.json();
    localStorage.setItem("pdd_web_token", data.token);
    setToken(data.token); setUser(data.user); setAuthMode("login");
  }

  async function loadConversations(filter: ShopFilter = chatShopFilter) {
    if (filter !== chatShopFilterRef.current) return;
    const requestId = ++conversationsRequestRef.current;
    const path = filter === "all" ? "/api/conversations" : `/api/conversations?shop_id=${filter}`;
    const response = await apiFetch(path);
    const rows = await response.json();
    if (requestId !== conversationsRequestRef.current || filter !== chatShopFilterRef.current) return;
    setConversations(sortConversations(rows));
    setSelectedConversationId((current) => rows.some((row: Conversation) => row.id === current) ? current : rows[0]?.id || null);
    if (!rows[0]) { setMessages([]); setCustomerContext(null); }
  }

  async function loadMessages(conversationId: number) {
    if (conversationId !== selectedConversationIdRef.current) return;
    const requestId = ++messagesRequestRef.current;
    const response = await apiFetch(`/api/conversations/${conversationId}/messages`);
    const rows = await response.json();
    if (requestId !== messagesRequestRef.current || conversationId !== selectedConversationIdRef.current) return;
    setMessages(rows);
  }
  async function loadConversationContext(conversationId: number) {
    if (conversationId !== selectedConversationIdRef.current) return;
    const requestId = ++contextRequestRef.current;
    const response = await apiFetch(`/api/conversations/${conversationId}/context`);
    const context = await response.json();
    if (requestId !== contextRequestRef.current || conversationId !== selectedConversationIdRef.current) return;
    setCustomerContext(context);
  }

  async function fetchQrcode(shopId: number, url?: string | null) {
    const response = await apiFetch(url || `/api/shops/${shopId}/qrcode`, { headers: { Accept: "image/png" } });
    const blob = await response.blob();
    setQrcodeUrls((items) => ({ ...items, [shopId]: URL.createObjectURL(blob) }));
  }

  async function loadLogs(filters: Record<string, string> = logFilters) {
    if (user?.role !== "admin") return;
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
    const query = params.toString() ? `?${params.toString()}` : "";
    const response = await apiFetch(`/api/logs${query}`);
    setLogData(await response.json());
  }

  async function loadServerStatusLatest() {
    const response = await apiFetch("/api/server-status/latest");
    setServerStatusLatest(await response.json());
  }

  async function loadServerStatusHistory(range: ServerStatusHistory["range"] = serverStatusRange) {
    const response = await apiFetch(`/api/server-status/history?range=${range}`);
    setServerStatusHistory(await response.json());
  }

  async function loadServerStatus(range: ServerStatusHistory["range"] = serverStatusRange) {
    await Promise.all([loadServerStatusLatest(), loadServerStatusHistory(range)]);
  }

  async function loadReturnRecords(filters: Record<string, string> = returnRecordFilters) {
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
    const query = params.toString() ? `?${params.toString()}` : "";
    const response = await apiFetch(`/api/return-records${query}`);
    setReturnRecordData(await response.json());
  }

  async function exportReturnRecords(filters: Record<string, string> = returnRecordFilters) {
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value && !["limit", "offset"].includes(key)) params.set(key, value);
    });
    const query = params.toString() ? `?${params.toString()}` : "";
    const response = await apiFetch(`/api/return-records/export${query}`, {
      headers: { Accept: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
    });
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "return-records.xlsx";
    link.click();
    URL.revokeObjectURL(url);
  }

  async function loadKnowledgeContent() {
    const [kbRes, nsRes] = await Promise.all([
      apiFetch("/api/knowledge-bases"),
      apiFetch("/api/note-sets"),
    ]);
    setKnowledgeBases(await kbRes.json());
    setNoteSets(await nsRes.json());
  }

  useEffect(() => { if (token) loadConversations(chatShopFilter).catch((error) => setToast(error.message)); }, [chatShopFilter, token]);
  useEffect(() => {
    setMessages([]);
    setCustomerContext(null);
  }, [selectedConversationId]);
  useEffect(() => {
    if (selectedConversationId) {
      loadMessages(selectedConversationId).catch((error) => setToast(error.message));
      loadConversationContext(selectedConversationId).catch((error) => setToast(error.message));
      if (selectedConversation?.human_attention_required) {
        apiFetch(`/api/conversations/${selectedConversationId}/clear-attention`, { method: "POST" })
          .then((response) => response.json())
          .then((updated) => {
            notifiedAttentionIdsRef.current.delete(selectedConversationId);
            setConversations((items) => sortConversations(upsert(items, updated)));
          })
          .catch((error) => setToast(error.message));
      }
    } else {
      setCustomerContext(null);
    }
  }, [selectedConversationId, selectedConversation?.human_attention_required]);
  useEffect(() => {
    if (!token || view !== "logs" || user?.role !== "admin") return;
    loadLogs(logFilters).catch((error) => setToast(error.message));
    const timer = window.setInterval(() => {
      loadLogs(logFilters).catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [token, view, user?.role, logFilters]);
  useEffect(() => {
    if (!user || user.role === "admin" || view !== "logs") return;
    setView("shops");
  }, [user, view]);
  useEffect(() => {
    if (!token || view !== "serverStatus" || user?.role !== "admin") return;
    loadServerStatus(serverStatusRange).catch((error) => setToast(error.message));
    const latestTimer = window.setInterval(() => {
      loadServerStatusLatest().catch(() => undefined);
    }, 5000);
    const historyTimer = window.setInterval(() => {
      loadServerStatusHistory(serverStatusRange).catch(() => undefined);
    }, 30000);
    return () => {
      window.clearInterval(latestTimer);
      window.clearInterval(historyTimer);
    };
  }, [token, view, user?.role, serverStatusRange]);
  useEffect(() => {
    if (!token || view !== "returnRecords") return;
    loadReturnRecords(returnRecordFilters).catch((error) => setToast(error.message));
    const timer = window.setInterval(() => {
      loadReturnRecords(returnRecordFilters).catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [token, view, returnRecordFilters]);

  function handleLogout() { localStorage.removeItem("pdd_web_token"); setToken(""); setUser(null); }

  async function withToast<T>(task: () => Promise<T>, successMessage?: string): Promise<T> {
    try {
      const result = await task();
      if (successMessage) setToast(successMessage);
      return result;
    } catch (error: any) {
      setToast(error.message || String(error));
      throw error;
    }
  }

  if (!authReady || authMode === "checking") return <BootPage message="正在检查系统状态" />;
  if (!token || !user) {
    if (authMode === "setup") return <SetupAdminPage onSetup={setupAdmin} error={authError} />;
    if (authMode === "register" && registrationEnabled) {
      return <RegisterPage onRegister={register} onBackToLogin={() => { setAuthError(""); setAuthMode("login"); }} error={authError} />;
    }
    return <LoginPage
      onLogin={login}
      error={authError}
      registrationEnabled={registrationEnabled}
      onShowRegister={() => { setAuthError(""); setAuthMode("register"); }}
    />;
  }

  const activeViewMeta: Record<ViewName, { title: string; subtitle: string }> = {
    shops: { title: "店铺运行中枢", subtitle: "集中管理登录、上线、AI 回复和转接策略" },
    chat: { title: "实时客服指挥台", subtitle: "处理买家会话、上下文和人工转接" },
    knowledge: { title: "知识与约束中心", subtitle: "维护知识库、结构化问答和回复注意事项" },
    returnRecords: { title: "售后记录流", subtitle: "沉淀退换、退款、改址和物流拦截记录" },
    users: { title: "账号管理", subtitle: "管理账号、角色、注册开关和资源额度" },
    logs: { title: "运行观测", subtitle: "追踪任务批次、请求定位和异常堆栈" },
    serverStatus: { title: "服务器状态", subtitle: "查看 Linux 主机资源、网络和进程趋势" },
    profile: { title: "个人工作区", subtitle: "查看个人账号、店铺范围和共享额度" },
  };
  const onlineShopCount = shops.filter((shop) => shop.status === "online").length;
  const attentionCount = conversations.filter((conversation) => conversation.human_attention_required).length;
  const quotaRemaining = quota?.remaining_llm_replies ?? 0;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand">
          <div className="sidebar-brand-icon"><Store size={22} /></div>
          <div className="sidebar-brand-text">
            <strong>OpenKefu</strong>
            <span>店铺后台管理</span>
          </div>
        </div>
        <nav className="sidebar-nav">
          <NavButton active={view === "shops"} icon={<Store size={18} />} label="店铺" onClick={showShops} />
          <NavButton active={view === "chat"} icon={<MessageSquareText size={18} />} label="客服聊天" onClick={() => setView("chat")} />
          <NavButton active={view === "knowledge"} icon={<BookOpen size={18} />} label="知识库" onClick={() => setView("knowledge")} />
          <NavButton active={view === "returnRecords"} icon={<PackageSearch size={18} />} label="退换记录" onClick={() => setView("returnRecords")} />
          <NavButton active={view === "profile"} icon={<CircleUserRound size={18} />} label="个人中心" onClick={() => { setView("profile"); loadQuota().catch((error) => setToast(error.message)); }} />
          {user.role === "admin" && <NavButton active={view === "users"} icon={<Users size={18} />} label="账号管理" onClick={() => setView("users")} />}
          {user.role === "admin" && <NavButton active={view === "serverStatus"} icon={<Server size={18} />} label="服务器状态" onClick={() => setView("serverStatus")} />}
          {user.role === "admin" && <NavButton active={view === "logs"} icon={<Activity size={18} />} label="日志" onClick={() => setView("logs")} />}
        </nav>
        <div className="sidebar-footer">
          <div className="sidebar-user">
            <div className="sidebar-user-avatar">{user.display_name[0]}</div>
            <div>
              <strong>{user.display_name}</strong>
              <span>{user.role === "admin" ? "管理员" : "客服"}</span>
            </div>
          </div>
          <button className="btn btn-ghost btn-sm" onClick={handleLogout} title="退出登录"><LogOut size={14} /></button>
        </div>
      </aside>

      <main className="main">
        <header className="ops-topbar">
          <div className="ops-topbar-main">
            <div className="ops-live-dot"><Radio size={15} /></div>
            <div>
              <strong>{activeViewMeta[view].title}</strong>
              <span>{activeViewMeta[view].subtitle}</span>
            </div>
          </div>
          <div className="ops-topbar-metrics">
            <div className="ops-metric"><Store size={15} /><span>在线店铺</span><strong>{onlineShopCount}/{shops.length}</strong></div>
            <div className="ops-metric"><Bot size={15} /><span>待人工</span><strong>{attentionCount}</strong></div>
            <div className="ops-metric"><Gauge size={15} /><span>LLM 剩余</span><strong>{quotaRemaining}</strong></div>
          </div>
        </header>
        <div className="main-content">
          {view === "shops" && <ShopPage shops={shops} quota={quota} selectedShopId={selectedShopId}
            onSelect={setSelectedShopId}
            onCreate={async (body: any) => withToast(async () => { await apiFetch("/api/shops", { method: "POST", body: JSON.stringify(body) }); await loadAll(); }, "店铺已创建")}
            onLogin={async (shopId: number) => withToast(async () => { setQrModalShopId(shopId); const result = await (await apiFetch(`/api/shops/${shopId}/login`, { method: "POST" })).json(); if (result.qrcode_url) await fetchQrcode(shopId, result.qrcode_url); await loadAll(); }, "登录流程已启动")}
            onPasswordLogin={(shopId: number) => { setPwdVerifyPrompt(null); setPwdModalShopId(shopId); }}
            onOnline={onlineShop}
            onOffline={async (shopId: number) => withToast(async () => { await apiFetch(`/api/shops/${shopId}/offline`, { method: "POST" }); await loadAll(); }, "店铺已下线")}
            onDelete={async (shopId: number) => withToast(async () => { await apiFetch(`/api/shops/${shopId}`, { method: "DELETE" }); await loadAll(); }, "店铺已删除")}
            onToggleAutoReply={async (shop: Shop) => withToast(async () => { await apiFetch(`/api/shops/${shop.id}`, { method: "PATCH", body: JSON.stringify({ auto_reply_enabled: !Boolean(shop.auto_reply_enabled) }) }); await loadAll(); }, "大模型回复设置已更新")}
            onOpenTransferSettings={(shop: Shop) => { setTransferSettingsShopId(shop.id); }}
          />}
          {view === "chat" && <ChatPage shops={shops} activeShop={activeChatShop} shopFilter={chatShopFilter}
            conversations={conversations} selectedConversation={selectedConversation} messages={messages} customerContext={customerContext} services={transferServices}
            onShopFilterChange={(value) => { setChatShopFilter(value); setSelectedConversationId(null); setMessages([]); setCustomerContext(null); setTransferServices([]); }}
            onSelectConversation={setSelectedConversationId}
            onReply={async (content: string) => withToast(async () => { if (!selectedConversationId) return; await apiFetch(`/api/conversations/${selectedConversationId}/reply`, { method: "POST", body: JSON.stringify({ content }) }); }, "消息已发送")}
            onImageReply={async (imageBase64: string, imageName?: string) => withToast(async () => { if (!selectedConversationId) return; await apiFetch(`/api/conversations/${selectedConversationId}/reply-image`, { method: "POST", body: JSON.stringify({ image_base64: imageBase64, image_name: imageName || "" }) }); }, "图片已发送")}
            onLoadServices={async () => withToast(async () => { const shopId = selectedConversation?.shop_id || activeChatShop?.id; if (!shopId) return; const response = await apiFetch(`/api/shops/${shopId}/transfer-services`); setTransferServices(await response.json()); }, "客服列表已更新")}
            onToggleConversationBotReply={async (conversation: Conversation) => {
              await withToast(async () => {
                const response = await apiFetch(`/api/conversations/${conversation.id}/bot-reply`, {
                  method: "PATCH",
                  body: JSON.stringify({ enabled: !Boolean(conversation.bot_reply_enabled) }),
                });
                const updated = await response.json();
                setConversations((items) => upsert(items, updated));
              }, "机器人回复设置已更新");
            }}
            onTransfer={async (csid: string, remark: string) => withToast(async () => { if (!selectedConversationId) return; await apiFetch(`/api/conversations/${selectedConversationId}/transfer`, { method: "POST", body: JSON.stringify({ csid, remark }) }); }, "转接请求已提交")}
          />}
          {view === "knowledge" && <KnowledgePage user={user} shops={shops} knowledgeBases={knowledgeBases} noteSets={noteSets}
            onRefresh={loadKnowledgeContent}
            onLoad={async (id: number) => { const response = await apiFetch(`/api/knowledge-bases/${id}`); return response.json(); }}
            onLoadNoteSet={async (id: number) => { const response = await apiFetch(`/api/note-sets/${id}`); return response.json(); }}
            onCreate={async (body: any) => withToast(async () => { await apiFetch("/api/knowledge-bases", { method: "POST", body: JSON.stringify(body) }); await loadKnowledgeContent(); }, "知识库已创建")}
            onUpdate={async (id: number, body: any) => withToast(async () => { await apiFetch(`/api/knowledge-bases/${id}`, { method: "PATCH", body: JSON.stringify(body) }); await loadKnowledgeContent(); }, "知识库已更新")}
            onDelete={async (id: number) => withToast(async () => { await apiFetch(`/api/knowledge-bases/${id}`, { method: "DELETE" }); await loadKnowledgeContent(); }, "知识库已删除")}
            onUpload={async (id: number, file: File) => withToast(async () => { const form = new FormData(); form.append("file", file); await apiFetch(`/api/knowledge-bases/${id}/files`, { method: "POST", body: form }); await loadKnowledgeContent(); }, "文件已上传")}
            onSaveQaItems={async (id: number, items: any[]) => withToast(async () => { await apiFetch(`/api/knowledge-bases/${id}/qa-items`, { method: "PATCH", body: JSON.stringify({ items }) }); await loadKnowledgeContent(); }, "结构化问答已保存")}
            onCreateNoteSet={async (body: any) => withToast(async () => { await apiFetch("/api/note-sets", { method: "POST", body: JSON.stringify(body) }); await loadKnowledgeContent(); }, "注意事项已创建")}
            onUpdateNoteSet={async (id: number, body: any) => withToast(async () => { await apiFetch(`/api/note-sets/${id}`, { method: "PATCH", body: JSON.stringify(body) }); await loadKnowledgeContent(); }, "注意事项已更新")}
            onDeleteNoteSet={async (id: number) => withToast(async () => { await apiFetch(`/api/note-sets/${id}`, { method: "DELETE" }); await loadKnowledgeContent(); }, "注意事项已删除")}
            onAddNoteItem={async (id: number, content: string) => withToast(async () => { await apiFetch(`/api/note-sets/${id}/items`, { method: "POST", body: JSON.stringify({ content }) }); await loadKnowledgeContent(); }, "注意事项已添加")}
            onUpdateNoteItem={async (id: number, itemId: number, content: string) => withToast(async () => { await apiFetch(`/api/note-sets/${id}/items/${itemId}`, { method: "PATCH", body: JSON.stringify({ content }) }); await loadKnowledgeContent(); }, "注意事项已保存")}
            onDeleteNoteItem={async (id: number, itemId: number) => withToast(async () => { await apiFetch(`/api/note-sets/${id}/items/${itemId}`, { method: "DELETE" }); await loadKnowledgeContent(); }, "注意事项已删除")}
          />}
          {view === "returnRecords" && <ReturnRecordsPage data={returnRecordData}
            filters={returnRecordFilters}
            shops={shops}
            onFiltersChange={(next) => { setReturnRecordFilters(next); loadReturnRecords(next).catch((error) => setToast(error.message)); }}
            onRefresh={() => loadReturnRecords(returnRecordFilters)}
            onExport={async (filters) => withToast(() => exportReturnRecords(filters), "退换记录已导出")}
            onCreate={async (body: any) => withToast(async () => { await apiFetch("/api/return-records", { method: "POST", body: JSON.stringify(body) }); await loadReturnRecords(returnRecordFilters); }, "退换记录已创建")}
            onUpdate={async (id: number, body: any) => withToast(async () => { await apiFetch(`/api/return-records/${id}`, { method: "PATCH", body: JSON.stringify(body) }); await loadReturnRecords(returnRecordFilters); }, "退换记录已保存")}
            onDelete={async (id: number) => withToast(async () => { await apiFetch(`/api/return-records/${id}`, { method: "DELETE" }); await loadReturnRecords(returnRecordFilters); }, "退换记录已删除")}
          />}
          {view === "users" && user.role === "admin" && <UsersPage users={users} shops={shops} settings={adminSettings}
            onCreate={async (body: any) => withToast(async () => { await apiFetch("/api/users", { method: "POST", body: JSON.stringify(body) }); const response = await apiFetch("/api/users"); setUsers(await response.json()); }, "账号已创建")}
            onUpdate={async (id: number, body: any) => withToast(async () => { await apiFetch(`/api/users/${id}`, { method: "PATCH", body: JSON.stringify(body) }); const response = await apiFetch("/api/users"); setUsers(await response.json()); }, "账号已保存")}
            onSettingsUpdate={async (body: any) => withToast(async () => { const response = await apiFetch("/api/admin/settings", { method: "PATCH", body: JSON.stringify(body) }); const data = await response.json(); setAdminSettings(data); setRegistrationEnabled(Boolean(data.registration_enabled)); }, "注册设置已保存")}
          />}
          {view === "profile" && <ProfilePage user={user} quota={quota} shops={shops} />}
          {view === "serverStatus" && user.role === "admin" && <ServerStatusPage
            latest={serverStatusLatest}
            history={serverStatusHistory}
            range={serverStatusRange}
            onRangeChange={(nextRange) => {
              setServerStatusRange(nextRange);
              loadServerStatusHistory(nextRange).catch((error) => setToast(error.message));
            }}
            onRefresh={() => loadServerStatus(serverStatusRange)}
          />}
          {view === "logs" && user.role === "admin" && <LogsPage data={logData}
            filters={logFilters}
            onFiltersChange={(next) => { setLogFilters(next); loadLogs(next).catch((error) => setToast(error.message)); }}
            onRefresh={() => loadLogs(logFilters)}
          />}
        </div>
      </main>

      {qrModalShopId && <QrLoginModal shop={qrModalShop} qrcodeUrl={qrcodeUrls[qrModalShopId]} onClose={() => setQrModalShopId(null)} />}
      {pwdModalShopId && (
            <PasswordLoginModal
              shop={pwdModalShop}
              initialVerifyInfo={pwdVerifyPrompt?.shopId === pwdModalShopId ? pwdVerifyPrompt.verifyInfo : null}
              onLogin={async (username: string, password: string, remember: boolean) => {
                return await passwordShopLogin(pwdModalShopId!, username, password, remember);
              }}
              onSendSms={async () => {
                await passwordShopSendSms(pwdModalShopId!);
              }}
              onVerify={async (code: string) => {
                await passwordShopVerify(pwdModalShopId!, code);
              }}
              onClose={() => { setPwdModalShopId(null); setPwdVerifyPrompt(null); }}
            />
          )}
      {transferSettingsShopId && <TransferSettingsModal shop={shops.find((s) => s.id === transferSettingsShopId) || null}
        onClose={() => setTransferSettingsShopId(null)}
        onSave={async (shopId: number, csids: string[]) => withToast(async () => { await apiFetch(`/api/shops/${shopId}`, { method: "PATCH", body: JSON.stringify({ transfer_csids: csids }) }); await loadAll(); }, "转接设置已保存")}
        onShopSettingSave={async (shopId: number, body: any) => withToast(async () => { await apiFetch(`/api/shops/${shopId}`, { method: "PATCH", body: JSON.stringify(body) }); await loadAll(); }, "店铺设置已保存")}
      />}
      <Toast message={toast} onDismiss={() => setToast("")} />
    </div>
  );
}
