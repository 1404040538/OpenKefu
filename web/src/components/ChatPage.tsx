import { useEffect, useRef, useState } from "react";
import { AlertCircle, Bot, ExternalLink, Image as ImageIcon, Loader2, MessageSquareText, Package, ReceiptText, RefreshCw, Send, Workflow } from "lucide-react";
import type { Conversation, CustomerContext, CustomerGoodsCard, CustomerOrderCard, Message, Shop, ShopFilter } from "../types/types";
import { StatusBadge } from "./StatusBadge";
import { usePendingActions } from "../utils/usePendingActions";

export function ChatPage({ shops, activeShop, shopFilter, conversations, selectedConversation, messages, customerContext, services, onShopFilterChange, onSelectConversation, onReply, onImageReply, onLoadServices, onToggleConversationBotReply, onTransfer }: {
  shops: Shop[];
  activeShop: Shop | null;
  shopFilter: ShopFilter;
  conversations: Conversation[];
  selectedConversation: Conversation | null;
  messages: Message[];
  customerContext: CustomerContext | null;
  services: any[];
  onShopFilterChange: (value: ShopFilter) => void;
  onSelectConversation: (id: number) => void;
  onReply: (content: string) => Promise<void>;
  onImageReply: (imageBase64: string, imageName?: string) => Promise<void>;
  onLoadServices: () => Promise<void>;
  onToggleConversationBotReply: (conversation: Conversation) => Promise<void>;
  onTransfer: (csid: string, remark: string) => Promise<void>;
}) {
  const [drafts, setDrafts] = useState<Record<number, string>>({});
  const text = selectedConversation ? drafts[selectedConversation.id] || "" : "";
  function setText(value: string) {
    if (!selectedConversation) return;
    const conversationId = selectedConversation.id;
    setDrafts((current) => ({ ...current, [conversationId]: value }));
  }
  const [csid, setCsid] = useState("");
  const [remark, setRemark] = useState("无需原因，直接转接");
  const streamRef = useRef<HTMLDivElement | null>(null);
  const { isPending, runAction } = usePendingActions();
  const selectedShop = shops.find((s) => s.id === selectedConversation?.shop_id) || activeShop;
  const selectedCustomerName = conversationCustomerName(selectedConversation);
  const selectedConversationSubtitle = selectedConversation
    ? selectedConversation.shop_name || selectedShop?.name || "当前店铺"
    : selectedShop?.name || "全部店铺";
  const conversationMallId = selectedConversation?.mall_id ? String(selectedConversation.mall_id) : "";
  const currentMallId = selectedShop?.mall_id ? String(selectedShop.mall_id) : "";
  const isShopChangedHistory = Boolean(conversationMallId && currentMallId && conversationMallId !== currentMallId);
  const canSend = Boolean(selectedConversation && selectedShop?.status === "online" && !isShopChangedHistory);
  const replyKey = selectedConversation ? `chat-reply-${selectedConversation.id}` : "chat-reply-none";
  const imageReplyKey = selectedConversation ? `chat-image-reply-${selectedConversation.id}` : "chat-image-reply-none";
  const loadServicesKey = selectedConversation ? `chat-services-${selectedConversation.id}` : `chat-services-shop-${selectedShop?.id || "none"}`;
  const botReplyKey = selectedConversation ? `chat-bot-reply-${selectedConversation.id}` : "chat-bot-reply-none";
  const transferKey = selectedConversation ? `chat-transfer-${selectedConversation.id}` : "chat-transfer-none";
  const replyPending = isPending(replyKey);
  const imageReplyPending = isPending(imageReplyKey);
  const loadServicesPending = isPending(loadServicesKey);
  const botReplyPending = isPending(botReplyKey);
  const transferPending = isPending(transferKey);
  const attentionCount = conversations.filter((conv) => conv.human_attention_required).length;
  const sendPlaceholder = isShopChangedHistory
    ? "店铺已更换，该历史会话无法发送消息"
    : canSend ? "输入人工回复" : "店铺未在线，无法发送消息";

  useEffect(() => {
    if (streamRef.current) streamRef.current.scrollTop = streamRef.current.scrollHeight;
  }, [messages.length, selectedConversation?.id]);

  async function handleImageFile(file?: File) {
    if (!file || !selectedConversation || !canSend) return;
    if (!file.type.startsWith("image/")) {
      alert("请选择图片文件");
      return;
    }
    const imageBase64 = await readImageFile(file);
    await runAction(imageReplyKey, () => onImageReply(imageBase64, file.name)).catch(() => undefined);
  }

  return (
    <div className="page chat-page">
      <div className="page-header">
        <div className="page-header-left">
          <MessageSquareText size={22} />
          <div>
            <h2 className="page-title">客服聊天</h2>
            <p className="page-subtitle">{conversations.length} 个会话，实时处理买家咨询和人工转接</p>
          </div>
        </div>
      </div>

      <div className="chat-command-strip">
        <div>
          <span>会话流</span>
          <strong>{conversations.length}</strong>
        </div>
        <div>
          <span>待人工接管</span>
          <strong>{attentionCount}</strong>
        </div>
        <div>
          <span>当前店铺</span>
          <strong>{selectedShop?.name || "全部店铺"}</strong>
        </div>
        <div>
          <span>发送状态</span>
          <strong>{canSend ? "可回复" : "受限"}</strong>
        </div>
      </div>

      <div className="chat-layout">
        <section className="chat-sidebar">
          <div className="chat-sidebar-header">
            <MessageSquareText size={18} />
            <span>会话</span>
          </div>
          <select className="input" value={shopFilter === "all" ? "all" : shopFilter} onChange={(e) => onShopFilterChange(e.target.value === "all" ? "all" : Number(e.target.value))}>
            <option value="all">全部店铺</option>
            {shops.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
          <div className="conversation-list">
            {conversations.map((conv) => (
              <button key={conv.id} className={`conversation-item ${selectedConversation?.id === conv.id ? "active" : ""} ${conv.human_attention_required ? "needs-human" : ""}`} onClick={() => onSelectConversation(conv.id)}>
                <strong>{conversationCustomerName(conv)}</strong>
                <small>{conv.shop_name || `店铺 ${conv.shop_id}`}</small>
                <span>{conv.last_message_preview}</span>
              </button>
            ))}
            {!conversations.length && <div className="empty-note">暂无会话</div>}
          </div>
        </section>

        <section className="chat-main">
          <div className="chat-header">
            <div>
              <strong>{selectedConversation ? selectedCustomerName : "未选择会话"}</strong>
              <span>{selectedConversationSubtitle}</span>
            </div>
            <StatusBadge status={selectedShop?.status || "idle"} />
          </div>
          <div className="chat-messages" ref={streamRef}>
            {messages.map((msg) => <MessageBubble key={msg.id} message={msg} customerName={selectedCustomerName} />)}
            {!messages.length && <div className="empty-note">选择左侧会话后查看消息</div>}
          </div>
          <form className="chat-input-bar" onSubmit={async (e) => {
            e.preventDefault();
            const content = text.trim();
            if (!content || !selectedConversation) return;
            const conversationId = selectedConversation.id;
            const submittedDraft = text;
            await runAction(replyKey, async () => {
              await onReply(content);
              setDrafts((current) => {
                if (current[conversationId] !== submittedDraft) return current;
                const next = { ...current };
                delete next[conversationId];
                return next;
              });
            }).catch(() => undefined);
          }}>
            <input className="input" disabled={!canSend || replyPending || imageReplyPending} value={text} onChange={(e) => setText(e.target.value)} placeholder={sendPlaceholder} />
            <label className={`btn btn-ghost btn-icon chat-image-send ${!canSend || replyPending || imageReplyPending ? "disabled" : ""}`} title={imageReplyPending ? "图片发送中" : "发送图片"}>
              {imageReplyPending ? <Loader2 className="spin" size={16} /> : <ImageIcon size={16} />}
              <input type="file" accept="image/*" disabled={!canSend || replyPending || imageReplyPending} onChange={(event) => {
                const file = event.target.files?.[0];
                event.currentTarget.value = "";
                handleImageFile(file).catch((error) => alert(String(error)));
              }} />
            </label>
            <button className="btn btn-primary" disabled={!canSend || replyPending || imageReplyPending || !text.trim()}>
              {replyPending ? <Loader2 className="spin" size={16} /> : <Send size={16} />}
              {replyPending ? "发送中" : null}
            </button>
          </form>
        </section>

        <section className="chat-panel">
          <CustomerContextPanel context={customerContext} hasConversation={Boolean(selectedConversation)} />

          <h3 className="chat-panel-title"><Bot size={16} /> 机器人回复</h3>
          <button className="btn btn-ghost btn-full" disabled={!selectedConversation || botReplyPending} onClick={() => selectedConversation && runAction(botReplyKey, () => onToggleConversationBotReply(selectedConversation)).catch(() => undefined)}>
            {botReplyPending ? <Loader2 className="spin" size={16} /> : <Bot size={16} />}
            {botReplyPending ? "切换中" : selectedConversation?.bot_reply_enabled ? "机器人回复：开" : "机器人回复：关"}
          </button>
          <small className="chat-panel-note">
            {selectedConversation?.human_attention_required ? "该顾客需要人工处理，打开后提醒已清除；重新开启前会进入大模型。" : selectedShop ? `当前店铺：${selectedShop.name}` : "选择会话后可切换"}
          </small>

          {selectedShop?.platform !== "qianniu" && (
            <>
              <h3 className="chat-panel-title"><Workflow size={16} /> 转接客服</h3>
              <button className="btn btn-ghost btn-full" disabled={!canSend || loadServicesPending} onClick={() => runAction(loadServicesKey, onLoadServices).catch(() => undefined)}>
                {loadServicesPending ? <Loader2 className="spin" size={16} /> : <RefreshCw size={16} />}
                {loadServicesPending ? "获取中" : "获取客服列表"}
              </button>
              <select className="input" disabled={transferPending} value={csid} onChange={(e) => setCsid(e.target.value)}>
                <option value="">选择客服</option>
                {services.map((svc: any) => <option key={svc.csid} value={svc.csid}>{svc.username || svc.nickname || svc.csid}</option>)}
              </select>
              <input className="input" disabled={transferPending} value={remark} onChange={(e) => setRemark(e.target.value)} />
              <button className="btn btn-primary btn-full" disabled={!canSend || !csid || transferPending} onClick={() => csid && runAction(transferKey, () => onTransfer(csid, remark)).catch(() => undefined)}>
                {transferPending ? <Loader2 className="spin" size={16} /> : null}
                {transferPending ? "转接中" : "转接当前会话"}
              </button>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function conversationCustomerName(conversation?: Conversation | null) {
  const nickname = (conversation?.nickname || "").trim();
  if (nickname) return nickname;
  const uid = String(conversation?.user_uid || "").trim();
  if (!uid) return "顾客";
  return `顾客 ${uid.length > 4 ? uid.slice(-4) : uid}`;
}

function CustomerContextPanel({ context, hasConversation }: { context: CustomerContext | null; hasConversation: boolean }) {
  const goods = context?.recent_goods || [];
  const orders = context?.orders || [];
  return (
    <div className="customer-context">
      <h3 className="chat-panel-title"><Package size={16} /> 顾客上下文</h3>
      {!hasConversation && <div className="context-empty">未选择会话</div>}

      {hasConversation && (
        <>
          <div className="context-section">
            <div className="context-section-head">
              <span>近期浏览</span>
              <small>{goods.length}</small>
            </div>
            {goods.length ? goods.map((item) => <GoodsContextCard key={item.goods_id} item={item} />) : <div className="context-empty">暂无浏览商品</div>}
          </div>

          <div className="context-section">
            <div className="context-section-head">
              <span>订单记录</span>
              <small>{orders.length}</small>
            </div>
            {orders.length ? orders.map((item, index) => <OrderContextCard key={item.order_sn || index} item={item} />) : <div className="context-empty">{context?.orders_message || "暂无订单记录"}</div>}
          </div>
        </>
      )}
    </div>
  );
}

function GoodsContextCard({ item }: { item: CustomerGoodsCard }) {
  const body = (
    <div className="context-card goods-context-card">
      <div className="context-thumb">
        {item.goods_thumb_url ? <img src={item.goods_thumb_url} alt="" /> : <Package size={18} />}
      </div>
      <div className="context-card-body">
        <strong>{item.goods_name || item.title || "未命名商品"}</strong>
        <span>{item.display_price || item.goods_id}</span>
      </div>
      {item.mall_link_url && <ExternalLink size={14} className="context-link-icon" />}
    </div>
  );
  if (!item.mall_link_url) return body;
  return <a className="context-card-link" href={item.mall_link_url} target="_blank" rel="noreferrer">{body}</a>;
}

function OrderContextCard({ item }: { item: CustomerOrderCard }) {
  const firstGoods = item.goods?.[0];
  return (
    <div className="context-card order-context-card">
      <div className="context-card-line">
        <ReceiptText size={15} />
        <strong>{item.status || "订单"}</strong>
        {item.amount && <span>{item.amount}</span>}
      </div>
      {firstGoods && <div className="context-order-goods">{firstGoods.goods_name || "订单商品"}{firstGoods.quantity ? ` x${firstGoods.quantity}` : ""}</div>}
      {item.order_sn && <div className="context-muted">单号：{item.order_sn}</div>}
      {(item.shipping_status || item.after_sale_status) && <div className="context-muted">{item.shipping_status || item.after_sale_status}</div>}
    </div>
  );
}

function MessageBubble({ message, customerName }: { message: Message; customerName: string }) {
  if (message.direction === "system" || message.kind === "system") {
    return <div className="bubble-row system"><div className="system-bubble"><MessageContent message={message} /></div></div>;
  }
  const outbound = message.direction === "outbound";
  const failed = message.status === "failed";
  const errorText = failed ? formatMessageError(message.error) : "";
  return (
    <div className={`bubble-row ${outbound ? "outbound" : "inbound"}`}>
      <div className={`bubble ${failed ? "failed" : ""}`}>
        {!outbound && <strong className="bubble-name">{customerName}</strong>}
        <MessageContent message={message} />
        {failed && (
          <div className="bubble-error" title={errorText || "发送失败"}>
            <AlertCircle size={13} />
            <span>{errorText || "消息发送失败，请查看运行日志或稍后重试"}</span>
          </div>
        )}
        <span className="bubble-meta">
          {message.status === "sent" || message.status === "received" ? "✓" : null}
          {message.status === "failed" ? "!" : null}
          {messageStatusText(message.status)}
        </span>
      </div>
    </div>
  );
}

function messageStatusText(status: string) {
  return { sending: "发送中", sent: "已发送", received: "已接收", failed: "失败" }[status] || status || "";
}

function formatMessageError(error?: string) {
  const value = (error || "").trim();
  if (!value) return "";
  return value.length > 180 ? `${value.slice(0, 180)}...` : value;
}

async function readImageFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error || new Error("failed to read image"));
    reader.readAsDataURL(file);
  });
}

function MessageContent({ message }: { message: Message }) {
  if (message.kind === "image" && message.content) return <img className="chat-image" src={message.content} alt="聊天图片" />;
  if (message.kind === "goods") {
    let goods: any = null;
    try { goods = JSON.parse(message.goods_json || ""); } catch { }
    return (
      <div className="goods-card">
        <Package size={16} />
        <div><strong>{goods?.name || "商品/订单消息"}</strong><span>{goods?.id || message.content}</span></div>
      </div>
    );
  }
  if (message.kind === "link" && message.content) return <a href={message.content} target="_blank" rel="noreferrer">{message.content}</a>;
  if (!message.content) return <pre>{trimRaw(message.raw_json)}</pre>;
  return <span>{message.content}</span>;
}

function trimRaw(text?: string) {
  return (text || "未知消息").slice(0, 320);
}
