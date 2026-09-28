import { useEffect, useMemo, useState } from "react";
import { Bot, ChevronDown, ChevronUp, Loader2, Plus, RefreshCw, Workflow, X } from "lucide-react";
import type { Shop, TransferService } from "../types/types";
import { parseTransferCsids } from "../utils/helpers";
import { usePendingActions } from "../utils/usePendingActions";

export function TransferSettingsModal({ shop, onClose, onSave, onShopSettingSave }: {
  shop: Shop | null;
  onClose: () => void;
  onSave: (shopId: number, csids: string[]) => Promise<void>;
  onShopSettingSave: (shopId: number, body: any) => Promise<void>;
}) {
  const [services, setServices] = useState<TransferService[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [remark, setRemark] = useState("");
  const [greetingMessage, setGreetingMessage] = useState("");
  const [forceAiReply, setForceAiReply] = useState(false);
  const [optimizing, setOptimizing] = useState(false);
  const [llmSaving, setLlmSaving] = useState(false);
  const [llmSaved, setLlmSaved] = useState(false);
  const { runAction } = usePendingActions();

  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const normalizedServices = useMemo(() => services.map((service) => ({ ...service, csid: String(service.csid) })), [services]);
  const availableServices = useMemo(() => normalizedServices.filter((service) => !selectedSet.has(service.csid)), [normalizedServices, selectedSet]);
  const selectedServices = useMemo(
    () => selected.map((csid) => normalizedServices.find((service) => service.csid === csid) || { csid }),
    [selected, normalizedServices],
  );
  const shopOnline = shop?.status === "online";
  const closeLocked = saving || llmSaving || optimizing;

  function authHeaders() {
    return { Authorization: `Bearer ${localStorage.getItem("pdd_web_token") || ""}` };
  }

  function loadServices() {
    if (!shop) return;
    if (shop.status !== "online") {
      setServices([]);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError("");
    fetch(`/api/shops/${shop.id}/transfer-services`, { headers: authHeaders() })
      .then((response) => {
        if (!response.ok) throw new Error("获取客服列表失败");
        return response.json();
      })
      .then((data) => setServices(data || []))
      .catch((err) => setError(err.message || String(err)))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    if (!shop) return;
    setSelected(parseTransferCsids(shop.transfer_csids));
    setRemark(shop.remark || "");
    setGreetingMessage(shop.greeting_message || "");
    setForceAiReply(Boolean(shop.force_ai_reply));
    setLlmSaved(false);
    loadServices();
  }, [shop?.id]);

  function addService(csid: string) {
    setSelected((prev) => [...prev, String(csid)]);
  }

  function removeService(index: number) {
    setSelected((prev) => prev.filter((_, itemIndex) => itemIndex !== index));
  }

  function moveUp(index: number) {
    if (index <= 0) return;
    setSelected((prev) => {
      const next = [...prev];
      [next[index - 1], next[index]] = [next[index], next[index - 1]];
      return next;
    });
  }

  function moveDown(index: number) {
    if (index >= selected.length - 1) return;
    setSelected((prev) => {
      const next = [...prev];
      [next[index], next[index + 1]] = [next[index + 1], next[index]];
      return next;
    });
  }

  async function refreshServices() {
    await runAction(`transfer-refresh-${shop?.id || "none"}`, async () => {
      setRefreshing(true);
      setError("");
      try {
        if (!shop) return;
        if (shop.status !== "online") throw new Error("请先将店铺上线后再刷新客服列表");
        const response = await fetch(`/api/shops/${shop.id}/transfer-services`, { headers: authHeaders() });
        if (!response.ok) throw new Error("获取客服列表失败");
        setServices(await response.json());
      } catch (err: any) {
        setError(err.message || String(err));
      } finally {
        setRefreshing(false);
      }
    });
  }

  async function handleOptimizeGreeting() {
    if (!shop) return;
    const message = greetingMessage;
    if (!message.trim()) return;
    await runAction(`transfer-optimize-${shop.id}`, async () => {
      setOptimizing(true);
      setError("");
      try {
        const response = await fetch(`/api/shops/${shop.id}/optimize-message`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders() },
          body: JSON.stringify({ message: message.trim(), msg_type: "greeting" }),
        });
        if (!response.ok) throw new Error("优化开场白失败");
        const data = await response.json();
        setGreetingMessage(data.optimized || message);
      } catch (err: any) {
        setError(err.message || String(err));
      } finally {
        setOptimizing(false);
      }
    });
  }

  async function handleShopSettingSave() {
    if (!shop) return;
    await runAction(`transfer-shop-save-${shop.id}`, async () => {
      setLlmSaving(true);
      setError("");
      try {
        await onShopSettingSave(shop.id, {
          remark,
          greeting_message: greetingMessage || null,
          force_ai_reply: forceAiReply,
        });
        setLlmSaved(true);
        window.setTimeout(() => setLlmSaved(false), 2000);
      } catch (err: any) {
        setError(err.message || String(err));
      } finally {
        setLlmSaving(false);
      }
    });
  }

  if (!shop) return null;

  return (
    <div className="modal-backdrop" onClick={() => { if (!closeLocked) onClose(); }}>
      <div className="modal settings-modal" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h2 className="modal-title">{shop.name} · 店铺设置</h2>
            <p className="modal-subtitle">配置备注、开场白、AI 回复模式和自动转接客服</p>
          </div>
          <button className="btn btn-ghost" disabled={closeLocked} onClick={onClose}>关闭</button>
        </div>

        <div className="settings-sections">
          <div className="settings-section">
            <h3 className="settings-section-title"><Workflow size={18} /> 店铺信息</h3>
            <div className="form-group">
              <label className="form-label">备注</label>
              <textarea
                className="input input-textarea"
                value={remark}
                onChange={(event) => setRemark(event.target.value)}
                placeholder="记录这个店铺的用途、负责人或注意事项"
                rows={3}
              />
            </div>
          </div>

          <div className="settings-section">
            <h3 className="settings-section-title"><Bot size={18} /> AI 回复设置</h3>
            <div className="form-group">
              <label className="form-label">开场白</label>
              <div className="llm-input-group">
                <textarea
                  className="input input-textarea"
                  value={greetingMessage}
                  onChange={(event) => setGreetingMessage(event.target.value)}
                  placeholder="用户首次发消息时自动发送的开场白"
                  rows={3}
                />
                <button className="btn btn-accent btn-sm" disabled={optimizing || !greetingMessage.trim()} onClick={handleOptimizeGreeting}>
                  {optimizing ? <Loader2 className="spin" size={14} /> : <Bot size={14} />}
                  优化开场白
                </button>
              </div>
            </div>
            <label className="checkbox-label">
              <input type="checkbox" checked={forceAiReply} onChange={(event) => setForceAiReply(event.target.checked)} />
              <span>强制 AI 回复模式：开启后不自动转人工，让大模型尽可能理解并回复用户</span>
            </label>
            {!shop.auto_reply_enabled && (
              <p className="settings-section-desc">当前店铺的大模型回复已关闭，强制 AI 开启后也不会触发自动回复。</p>
            )}
            <button className="btn btn-primary" disabled={llmSaving} onClick={handleShopSettingSave}>
              {llmSaving ? <Loader2 className="spin" size={14} /> : null}
              {llmSaved ? "已保存" : "保存店铺设置"}
            </button>
          </div>

          <div className="settings-section">
            <h3 className="settings-section-title"><Workflow size={18} /> 自动转接设置</h3>
            <p className="settings-section-desc">选择客服并排序，大模型无法处理或报错时按优先级自动转接。</p>
            <button className="btn btn-ghost btn-sm" disabled={refreshing || loading || !shopOnline} onClick={refreshServices}>
              {refreshing ? <Loader2 className="spin" size={14} /> : <RefreshCw size={14} />} 刷新客服列表
            </button>
            {!shopOnline ? (
              <div className="transfer-empty">店铺上线后可刷新客服列表并调整转接客服。</div>
            ) : loading ? (
              <div className="transfer-loading"><Loader2 className="spin" size={20} /><span>正在获取可用客服列表</span></div>
            ) : error ? (
              <div className="transfer-error">{error}</div>
            ) : (
              <div className="transfer-layout">
                <div className="transfer-list">
                  <div className="transfer-list-header">可选客服</div>
                  {availableServices.length === 0 && <div className="transfer-empty">所有客服已添加</div>}
                  {availableServices.map((service) => (
                    <button key={service.csid} className="transfer-service-item" onClick={() => addService(service.csid)}>
                      <Plus size={14} /><span>{service.nickname || service.username || service.csid}</span>
                    </button>
                  ))}
                </div>
                <div className="transfer-list">
                  <div className="transfer-list-header">转接优先级</div>
                  {selectedServices.length === 0 && <div className="transfer-empty">请在左侧选择客服</div>}
                  {selectedServices.map((service, index) => (
                    <div key={service.csid} className="transfer-priority-item">
                      <span className="transfer-priority-rank">{index + 1}</span>
                      <span className="transfer-priority-name">{service.nickname || service.username || service.csid}</span>
                      <div className="transfer-priority-actions">
                        <button disabled={index === 0} onClick={() => moveUp(index)} title="上移"><ChevronUp size={14} /></button>
                        <button disabled={index === selectedServices.length - 1} onClick={() => moveDown(index)} title="下移"><ChevronDown size={14} /></button>
                        <button className="btn btn-danger btn-sm" onClick={() => removeService(index)} title="移除"><X size={14} /></button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            <div className="transfer-save-bar">
              <button className="btn btn-primary" disabled={saving || loading || !!error || !shopOnline} onClick={async () => {
                if (!shop) return;
                await runAction(`transfer-save-${shop.id}`, async () => {
                  setSaving(true);
                  try {
                    await onSave(shop.id, selected);
                    onClose();
                  } catch (err: any) {
                    setError(err.message || String(err));
                  } finally {
                    setSaving(false);
                  }
                });
              }}>
                {saving ? <Loader2 className="spin" size={16} /> : null}
                保存转接设置
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
