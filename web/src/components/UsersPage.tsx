import { useState } from "react";
import { Loader2, Settings, UserPlus, Users } from "lucide-react";
import type { Shop, User } from "../types/types";
import { usePendingActions } from "../utils/usePendingActions";

export function UsersPage({ users, shops, settings, onCreate, onUpdate, onSettingsUpdate }: {
  users: User[];
  shops: Shop[];
  settings: { registration_enabled: boolean };
  onCreate: (body: any) => Promise<void>;
  onUpdate: (id: number, body: any) => Promise<void>;
  onSettingsUpdate: (body: any) => Promise<void>;
}) {
  const [form, setForm] = useState({ username: "", password: "", display_name: "", role: "service", shop_ids: [] as number[], max_llm_replies: "0", max_shops: "10", max_knowledge_bases: "5" });
  const [editTarget, setEditTarget] = useState<User | null>(null);
  const [editForm, setEditForm] = useState({ display_name: "", is_active: true, password: "", max_llm_replies: "" as string, llm_reply_count: "" as string, max_shops: "" as string, max_knowledge_bases: "" as string });
  const { isPending, runAction } = usePendingActions();
  const createPending = isPending("user-create");
  const editPending = editTarget ? isPending(`user-update-${editTarget.id}`) : false;
  const settingsPending = isPending("registration-settings");
  const createIsAdmin = form.role === "admin";
  const editIsAdmin = editTarget?.role === "admin";

  return (
    <div className="page users-page">
      <div className="page-header">
        <div className="page-header-left">
            <Users size={22} />
            <div>
            <h2 className="page-title">账号管理</h2>
            <p className="page-subtitle">{users.length} 个账号，分配角色、店铺、注册开关和资源额度</p>
            </div>
        </div>
        <button
          className="btn btn-sm"
          disabled={settingsPending}
          onClick={() => runAction("registration-settings", () => onSettingsUpdate({ registration_enabled: !settings.registration_enabled })).catch(() => undefined)}
        >
          {settingsPending ? <Loader2 className="spin" size={14} /> : <Settings size={14} />}
          {settings.registration_enabled ? "注册已开启" : "注册已关闭"}
        </button>
      </div>

      <div className="users-layout">
        <section className="users-table-wrap">
          <table className="table">
            <thead>
              <tr><th>用户名</th><th>名称</th><th>角色</th><th>状态</th><th>店铺</th><th>知识库上限</th><th>店铺上限</th><th>LLM 总额度</th><th>LLM 已用</th><th>LLM 剩余</th><th>操作</th></tr>
            </thead>
            <tbody>
              {users.map((item) => (
                <tr key={item.id}>
                  <td><strong>{item.username}</strong></td>
                  <td>{item.display_name}</td>
                  <td><span className={`role-tag ${item.role}`}>{item.role === "admin" ? "管理员" : "客服"}</span></td>
                  <td><span className={`state-tag ${item.is_active ? "active" : "inactive"}`}>{item.is_active ? "启用" : "停用"}</span></td>
                  <td>{item.shop_ids?.length || 0}</td>
                  <td>{item.role === "admin" ? "不限制" : item.max_knowledge_bases ?? 5}</td>
                  <td>{item.role === "admin" ? "不限制" : item.max_shops ?? 10}</td>
                  <td>{item.max_llm_replies ?? 0}</td>
                  <td>{item.llm_reply_count || 0}</td>
                  <td>{Math.max(0, (item.max_llm_replies || 0) - (item.llm_reply_count || 0))}</td>
                  <td>
                    <button className="btn btn-sm" onClick={() => {
                      setEditTarget(item);
                      setEditForm({
                        display_name: item.display_name, is_active: item.is_active, password: "",
                        max_llm_replies: String(item.max_llm_replies || 0),
                        llm_reply_count: String(item.llm_reply_count || 0),
                        max_shops: String(item.max_shops ?? 10),
                        max_knowledge_bases: String(item.max_knowledge_bases ?? 5),
                      });
                    }}>编辑</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="users-create-panel">
          <div className="panel-header"><UserPlus size={18} /> 新建账号</div>
          <form className="form-stack" onSubmit={async (e) => {
            e.preventDefault();
            await runAction("user-create", async () => {
              await onCreate({
                username: form.username, password: form.password, display_name: form.display_name, role: form.role,
                shop_ids: form.shop_ids,
                max_shops: createIsAdmin ? null : parseInt(form.max_shops) || 0,
                max_knowledge_bases: createIsAdmin ? null : parseInt(form.max_knowledge_bases) || 0,
                max_llm_replies: parseInt(form.max_llm_replies) || 0,
              });
              setForm({ username: "", password: "", display_name: "", role: "service", shop_ids: [], max_llm_replies: "0", max_shops: "10", max_knowledge_bases: "5" });
            }).catch(() => undefined);
          }}>
            <div className="form-group"><input className="input" disabled={createPending} placeholder="用户名" value={form.username} required onChange={(e) => setForm({ ...form, username: e.target.value })} /></div>
            <div className="form-group"><input className="input" disabled={createPending} placeholder="显示名" value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} /></div>
            <div className="form-group"><input className="input" disabled={createPending} placeholder="密码" type="password" value={form.password} required onChange={(e) => setForm({ ...form, password: e.target.value })} /></div>
            <div className="form-group">
              <select className="input" disabled={createPending} value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
                <option value="service">客服</option>
                <option value="admin">管理员</option>
              </select>
            </div>
            <div className="form-group"><input className="input" disabled={createPending || createIsAdmin} placeholder={createIsAdmin ? "管理员不限制知识库" : "知识库上限"} type="number" min="0" value={createIsAdmin ? "" : form.max_knowledge_bases} onChange={(e) => setForm({ ...form, max_knowledge_bases: e.target.value })} /></div>
            <div className="form-group"><input className="input" disabled={createPending || createIsAdmin} placeholder={createIsAdmin ? "管理员不限制店铺" : "店铺上限"} type="number" min="0" value={createIsAdmin ? "" : form.max_shops} onChange={(e) => setForm({ ...form, max_shops: e.target.value })} /></div>
            <div className="form-group"><input className="input" disabled={createPending} placeholder="LLM 总额度" type="number" min="0" value={form.max_llm_replies} onChange={(e) => setForm({ ...form, max_llm_replies: e.target.value })} /></div>
            <div className="check-list">
              {shops.map((shop) => (
                <label key={shop.id} className="kb-shop-check">
                  <input type="checkbox" disabled={createPending} checked={form.shop_ids.includes(shop.id)} onChange={(e) => setForm({ ...form, shop_ids: e.target.checked ? [...form.shop_ids, shop.id] : form.shop_ids.filter((id) => id !== shop.id) })} />
                  {shop.name}
                </label>
              ))}
            </div>
            <button className="btn btn-primary btn-full" disabled={!form.username.trim() || !form.password || createPending}>
              {createPending ? <Loader2 className="spin" size={16} /> : null}
              {createPending ? "创建中" : "创建账号"}
            </button>
          </form>
        </section>
      </div>

      {editTarget && (
        <div className="modal-backdrop" onClick={() => { if (!editPending) setEditTarget(null); }}>
          <div className="modal modal-sm" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <div><h2 className="modal-title">编辑账号 {editTarget.username}</h2></div>
              <button className="btn btn-ghost" disabled={editPending} onClick={() => setEditTarget(null)}>关闭</button>
            </div>
            <div className="modal-body form-stack">
              <div className="form-group"><input className="input" disabled={editPending} placeholder="显示名" value={editForm.display_name} onChange={(e) => setEditForm({ ...editForm, display_name: e.target.value })} /></div>
              <label className="checkbox-label"><input type="checkbox" disabled={editPending} checked={editForm.is_active} onChange={(e) => setEditForm({ ...editForm, is_active: e.target.checked })} /><span>启用</span></label>
              <div className="form-group"><input className="input" disabled={editPending} placeholder="新密码，留空则不修改" type="password" value={editForm.password} onChange={(e) => setEditForm({ ...editForm, password: e.target.value })} /></div>
              <div className="form-group"><input className="input" disabled={editPending || editIsAdmin} placeholder={editIsAdmin ? "管理员不限制知识库" : "知识库上限"} type="number" min="0" value={editIsAdmin ? "" : editForm.max_knowledge_bases} onChange={(e) => setEditForm({ ...editForm, max_knowledge_bases: e.target.value })} /></div>
              <div className="form-group"><input className="input" disabled={editPending || editIsAdmin} placeholder={editIsAdmin ? "管理员不限制店铺" : "店铺上限"} type="number" min="0" value={editIsAdmin ? "" : editForm.max_shops} onChange={(e) => setEditForm({ ...editForm, max_shops: e.target.value })} /></div>
              <div className="form-group"><input className="input" disabled={editPending} placeholder="LLM 总额度" type="number" min="0" value={editForm.max_llm_replies} onChange={(e) => setEditForm({ ...editForm, max_llm_replies: e.target.value })} /></div>
              <div className="form-group"><input className="input" disabled={editPending} placeholder="LLM 已用次数" type="number" min="0" value={editForm.llm_reply_count} onChange={(e) => setEditForm({ ...editForm, llm_reply_count: e.target.value })} /></div>
              <button className="btn btn-primary btn-full" disabled={editPending} onClick={async () => {
                await runAction(`user-update-${editTarget.id}`, async () => {
                  const body: any = { display_name: editForm.display_name, is_active: editForm.is_active,
                    max_llm_replies: parseInt(editForm.max_llm_replies) || 0,
                    llm_reply_count: parseInt(editForm.llm_reply_count) || 0 };
                  if (!editIsAdmin) {
                    body.max_shops = parseInt(editForm.max_shops) || 0;
                    body.max_knowledge_bases = parseInt(editForm.max_knowledge_bases) || 0;
                  }
                  if (editForm.password) body.password = editForm.password;
                  await onUpdate(editTarget.id, body); setEditTarget(null);
                }).catch(() => undefined);
              }}>
                {editPending ? <Loader2 className="spin" size={16} /> : null}
                {editPending ? "保存中" : "保存"}
              </button>
              <button className="btn btn-ghost btn-full" disabled={editPending} onClick={() => setEditTarget(null)}>取消</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
