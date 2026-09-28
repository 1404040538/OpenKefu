import { useState } from "react";
import { Bot, Loader2, Play, Plus, Settings, Trash2, Wifi, WifiOff } from "lucide-react";
import type { Quota, Shop } from "../types/types";
import { StatusBadge } from "./StatusBadge";
import { LOGIN_FINISHED_STATUSES } from "../utils/constants";
import { usePendingActions } from "../utils/usePendingActions";

export function ShopPage({
  shops,
  quota,
  selectedShopId,
  onSelect,
  onCreate,
  onLogin,
  onPasswordLogin,
  onOnline,
  onOffline,
  onDelete,
  onToggleAutoReply,
  onOpenTransferSettings,
}: {
  shops: Shop[];
  quota: Quota | null;
  selectedShopId: number | null;
  onSelect: (id: number) => void;
  onCreate: (body: any) => Promise<void>;
  onLogin: (id: number) => Promise<void>;
  onPasswordLogin: (id: number) => void;
  onOnline: (id: number) => Promise<void>;
  onOffline: (id: number) => Promise<void>;
  onDelete: (id: number) => Promise<void>;
  onToggleAutoReply: (shop: Shop) => Promise<void>;
  onOpenTransferSettings: (shop: Shop) => void;
}) {
  const [name, setName] = useState("");
  const [remark, setRemark] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Shop | null>(null);
  const { isPending, runAction } = usePendingActions();

  const maxShops = quota?.max_shops ?? null;
  const canCreate = maxShops == null || shops.length < maxShops;
  const createPending = isPending("shop-create");
  const deletePending = deleteTarget ? isPending(`shop-delete-${deleteTarget.id}`) : false;
  return (
    <div className="page shop-page">
      <div className="page-header">
        {canCreate && (
          <button className="btn btn-primary" onClick={() => setShowCreate(true)}>
            <Plus size={16} /> 新建店铺
          </button>
        )}
      </div>

      <div className="shop-grid">
        {!canCreate && (
          <div className="shop-card shop-card-disabled">
            <div className="shop-card-icon"><Plus size={22} /></div>
            <div className="shop-card-body">
              <strong>店铺数量已达上限</strong>
              <span>当前 {shops.length}/{maxShops ?? 0}，暂不可新建店铺</span>
            </div>
          </div>
        )}
        {shops.map((shop) => (
          <article
            key={shop.id}
            className={`shop-card ${selectedShopId === shop.id ? "shop-card-active" : ""}`}
            onClick={() => onSelect(shop.id)}
          >
            <div className="shop-card-header">
              <div className="shop-card-title">
                <strong>{shop.name}</strong>
                <span className="shop-card-mall">{shop.nickname || shop.mall_id || "未绑定店铺信息"}</span>
              </div>
              <div className="shop-card-header-actions">
                <StatusBadge status={shop.status} />
                <button
                  className="icon-btn shop-card-delete"
                  title="删除店铺"
                  onClick={(e) => { e.stopPropagation(); setDeleteTarget(shop); }}
                >
                  <Trash2 size={15} />
                </button>
              </div>
            </div>
            <p className="shop-card-remark">{shop.remark || "暂无备注"}</p>
            <div className="shop-card-actions">
              <button className="btn btn-sm" disabled={isPending(`shop-login-${shop.id}`)} onClick={(e) => {
                e.stopPropagation();
                runAction(`shop-login-${shop.id}`, () => onLogin(shop.id)).catch(() => undefined);
              }}>
                {isPending(`shop-login-${shop.id}`) ? <Loader2 className="spin" size={14} /> : <Play size={14} />}
                {isPending(`shop-login-${shop.id}`) ? "登录中" : shop.has_login_cache || LOGIN_FINISHED_STATUSES.has(shop.status) ? "重新扫码" : "扫码登录"}
              </button>
              <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); onPasswordLogin(shop.id); }}>
                <Play size={14} /> 账密登录
              </button>
              {shop.status === "online" ? (
                <button className="btn btn-sm" disabled={isPending(`shop-offline-${shop.id}`)} onClick={(e) => {
                  e.stopPropagation();
                  runAction(`shop-offline-${shop.id}`, () => onOffline(shop.id)).catch(() => undefined);
                }}>
                  {isPending(`shop-offline-${shop.id}`) ? <Loader2 className="spin" size={14} /> : <WifiOff size={14} />}
                  {isPending(`shop-offline-${shop.id}`) ? "下线中" : "下线"}
                </button>
              ) : (
                <button className="btn btn-sm" disabled={!shop.has_login_cache || isPending(`shop-online-${shop.id}`)} onClick={(e) => {
                  e.stopPropagation();
                  runAction(`shop-online-${shop.id}`, () => onOnline(shop.id)).catch(() => undefined);
                }}>
                  {isPending(`shop-online-${shop.id}`) ? <Loader2 className="spin" size={14} /> : <Wifi size={14} />}
                  {isPending(`shop-online-${shop.id}`) ? "上线中" : "上线"}
                </button>
              )}
              <button className="btn btn-sm" disabled={isPending(`shop-auto-reply-${shop.id}`)} onClick={(e) => {
                e.stopPropagation();
                runAction(`shop-auto-reply-${shop.id}`, () => onToggleAutoReply(shop)).catch(() => undefined);
              }}>
                {isPending(`shop-auto-reply-${shop.id}`) ? <Loader2 className="spin" size={14} /> : <Bot size={14} />}
                {isPending(`shop-auto-reply-${shop.id}`) ? "切换中" : shop.auto_reply_enabled ? "AI 开" : "AI 关"}
              </button>
              <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); onOpenTransferSettings(shop); }}>
                <Settings size={14} /> 设置
              </button>
            </div>
            {shop.last_error && <span className="shop-card-error">{shop.last_error}</span>}
          </article>
        ))}
      </div>

      {showCreate && (
        <div className="modal-backdrop" onClick={() => { if (!createPending) setShowCreate(false); }}>
          <form className="modal modal-sm" onClick={(e) => e.stopPropagation()} onSubmit={async (e) => {
            e.preventDefault();
            await runAction("shop-create", async () => {
              const body: any = { name, remark, auto_reply_enabled: true };
              await onCreate(body);
              setName("");
              setRemark("");
              setShowCreate(false);
            }).catch(() => undefined);
          }}>
            <div className="modal-header">
              <div>
                <h2 className="modal-title">新建店铺</h2>
                <p className="modal-subtitle">创建后可在列表中登录并上线客服连接</p>
              </div>
              <button className="btn btn-ghost" type="button" disabled={createPending} onClick={() => setShowCreate(false)}>关闭</button>
            </div>
            <div className="modal-body">
              <div className="form-group">
                <input className="input" disabled={createPending} value={name} onChange={(e) => setName(e.target.value)} placeholder="店铺名称" autoFocus />
              </div>
              <div className="form-group">
                <textarea className="input input-textarea" disabled={createPending} value={remark} onChange={(e) => setRemark(e.target.value)} placeholder="备注，例如负责人、品类或运营说明" />
              </div>
              <button className="btn btn-primary btn-full" disabled={!name.trim() || createPending}>
                {createPending ? <Loader2 className="spin" size={16} /> : null}
                {createPending ? "创建中" : "创建店铺"}
              </button>
            </div>
          </form>
        </div>
      )}

      {deleteTarget && (
        <div className="modal-backdrop" onClick={() => { if (!deletePending) setDeleteTarget(null); }}>
          <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <div>
                <h2 className="modal-title">删除店铺</h2>
                <p className="modal-subtitle">确定删除“{deleteTarget.name}”吗？此操作不可撤销。</p>
              </div>
              <button className="btn btn-ghost" disabled={deletePending} onClick={() => setDeleteTarget(null)}>关闭</button>
            </div>
            <div className="modal-body modal-actions">
              <button className="btn btn-danger btn-full" disabled={deletePending} onClick={async () => {
                await runAction(`shop-delete-${deleteTarget.id}`, async () => {
                  await onDelete(deleteTarget.id);
                  setDeleteTarget(null);
                }).catch(() => undefined);
              }}>
                {deletePending ? <Loader2 className="spin" size={16} /> : null}
                {deletePending ? "删除中" : "确认删除"}
              </button>
              <button className="btn btn-ghost btn-full" disabled={deletePending} onClick={() => setDeleteTarget(null)}>取消</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
