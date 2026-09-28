import { useEffect, useMemo, useState } from "react";
import { Download, FileText, ListFilter, Loader2, PackageSearch, Plus, RefreshCw, Save, Search, Trash2, X } from "lucide-react";
import type { ReturnRecord, ReturnRecordResponse, Shop } from "../types/types";
import { usePendingActions } from "../utils/usePendingActions";

type Filters = Record<string, string>;
type RecordForm = {
  shop_id: string;
  user_uid: string;
  username: string;
  order_no: string;
  order_status: string;
  record_type: string;
  new_address: string;
  remark: string;
  source_message: string;
};

const TYPE_LABELS: Record<string, string> = {
  return: "退货",
  exchange: "换货",
  address_change: "修改地址",
  refund_only: "仅退款",
  logistics_intercept: "物流拦截",
  other: "其他",
};

const TYPE_OPTIONS = [
  ["", "全部类型"],
  ["return", "退货"],
  ["exchange", "换货"],
  ["address_change", "修改地址"],
  ["refund_only", "仅退款"],
  ["logistics_intercept", "物流拦截"],
  ["other", "其他"],
];

const EDIT_TYPE_OPTIONS = TYPE_OPTIONS.filter(([value]) => value);

const EMPTY_FORM: RecordForm = {
  shop_id: "",
  user_uid: "",
  username: "",
  order_no: "",
  order_status: "待核实",
  record_type: "return",
  new_address: "",
  remark: "",
  source_message: "",
};

export function ReturnRecordsPage({
  data,
  filters,
  shops,
  onFiltersChange,
  onRefresh,
  onExport,
  onCreate,
  onUpdate,
  onDelete,
}: {
  data: ReturnRecordResponse | null;
  filters: Filters;
  shops: Shop[];
  onFiltersChange: (filters: Filters) => void;
  onRefresh: () => Promise<void>;
  onExport: (filters: Filters) => Promise<void>;
  onCreate: (body: any) => Promise<void>;
  onUpdate: (id: number, body: any) => Promise<void>;
  onDelete: (id: number) => Promise<void>;
}) {
  const [draft, setDraft] = useState<Filters>(filters);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const { isPending, runAction } = usePendingActions();
  const items = data?.items || [];
  const selected = useMemo(
    () => !creating ? items.find((item) => item.id === selectedId) || items[0] || null : null,
    [items, selectedId, creating],
  );
  const total = data?.total || 0;
  const limit = Number(filters.limit || data?.limit || 100);
  const offset = Number(filters.offset || data?.offset || 0);
  const exportPending = isPending("return-export");

  useEffect(() => { setDraft(filters); }, [filters]);
  useEffect(() => {
    if (!creating && items.length && !items.some((item) => item.id === selectedId)) {
      setSelectedId(items[0].id);
    }
  }, [items, selectedId, creating]);

  function updateDraft(key: string, value: string) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function apply(next: Filters = draft) {
    const cleaned = Object.fromEntries(
      Object.entries({ ...next, offset: "0", limit: String(limit) }).filter(([, value]) => value),
    );
    onFiltersChange(cleaned);
  }

  function clearFilters() {
    setDraft({});
    onFiltersChange({ limit: String(limit), offset: "0" });
  }

  function page(delta: number) {
    const nextOffset = Math.max(0, offset + delta * limit);
    onFiltersChange({ ...filters, offset: String(nextOffset), limit: String(limit) });
  }

  function startCreate() {
    setCreating(true);
    setSelectedId(null);
  }

  return (
    <div className="page return-records-page">
      <div className="page-header">
        <div className="page-header-left">
          <PackageSearch size={22} />
          <div>
            <h2 className="page-title">退换记录</h2>
            <p className="page-subtitle">同一用户同一订单仅保留一条，可手动维护售后处理记录</p>
          </div>
        </div>
        <div className="return-record-actions">
          <button className="btn btn-sm" onClick={() => onRefresh().catch(() => undefined)} title="刷新">
            <RefreshCw size={14} />
          </button>
          <button className="btn btn-sm" onClick={startCreate}>
            <Plus size={14} /> 新增
          </button>
          <button className="btn btn-primary btn-sm" disabled={exportPending} onClick={() => runAction("return-export", () => onExport(filters)).catch(() => undefined)}>
            {exportPending ? <Loader2 className="spin" size={14} /> : <Download size={14} />}
            {exportPending ? "导出中" : "导出"}
          </button>
        </div>
      </div>

      <div className="return-records-grid">
        <aside className="return-filter-panel">
          <div className="return-panel-title"><ListFilter size={15} /> 筛选</div>
          <div className="return-filter-stack">
            <label className="form-stack">
              <span className="form-label">店铺</span>
              <select className="input input-sm" value={draft.shop_id || ""} onChange={(event) => updateDraft("shop_id", event.target.value)}>
                <option value="">全部店铺</option>
                {shops.map((shop) => <option key={shop.id} value={shop.id}>{shop.name}</option>)}
              </select>
            </label>
            <label className="form-stack">
              <span className="form-label">类型</span>
              <select className="input input-sm" value={draft.record_type || ""} onChange={(event) => updateDraft("record_type", event.target.value)}>
                {TYPE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <label className="form-stack">
              <span className="form-label">关键词</span>
              <div className="return-input-icon">
                <Search size={14} />
                <input className="input input-sm" value={draft.keyword || ""} onChange={(event) => updateDraft("keyword", event.target.value)} />
              </div>
            </label>
            <label className="form-stack">
              <span className="form-label">开始时间</span>
              <input className="input input-sm" type="datetime-local" value={toDateInput(draft.date_from)} onChange={(event) => updateDraft("date_from", event.target.value.replace("T", " "))} />
            </label>
            <label className="form-stack">
              <span className="form-label">结束时间</span>
              <input className="input input-sm" type="datetime-local" value={toDateInput(draft.date_to)} onChange={(event) => updateDraft("date_to", event.target.value.replace("T", " "))} />
            </label>
            <div className="return-filter-actions">
              <button className="btn btn-primary btn-sm" onClick={() => apply()}>查询</button>
              <button className="btn btn-ghost btn-sm" onClick={clearFilters}><X size={14} /> 清空</button>
            </div>
          </div>
        </aside>

        <section className="return-main-panel">
          <div className="return-summary">
            <strong>{total}</strong>
            <span>条退换记录</span>
            <span>第 {total ? offset + 1 : 0}-{Math.min(offset + limit, total)} 条</span>
          </div>
          <div className="return-table-wrap">
            <table className="return-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>时间</th>
                  <th>店铺</th>
                  <th>类型</th>
                  <th>用户</th>
                  <th>订单号</th>
                  <th>状态</th>
                  <th>备注</th>
                </tr>
              </thead>
              <tbody>
                {items.map((record) => (
                  <tr key={record.id} className={!creating && record.id === selected?.id ? "active" : ""} onClick={() => { setCreating(false); setSelectedId(record.id); }}>
                    <td>{record.id}</td>
                    <td>{record.created_at}</td>
                    <td>{record.shop_name || record.shop_id}</td>
                    <td><span className={`return-type-badge ${record.record_type}`}>{TYPE_LABELS[record.record_type] || record.record_type}</span></td>
                    <td>{record.username || record.user_uid}</td>
                    <td>{record.order_no}</td>
                    <td>{record.order_status || "待核实"}</td>
                    <td title={record.remark || record.new_address || ""}>{record.remark || record.new_address || "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!items.length && <div className="empty-note">暂无退换记录</div>}
          </div>
          <div className="return-pager">
            <button className="btn btn-sm" disabled={offset <= 0} onClick={() => page(-1)}>上一页</button>
            <button className="btn btn-sm" disabled={offset + limit >= total} onClick={() => page(1)}>下一页</button>
          </div>
        </section>

        <aside className="return-detail-panel">
          <div className="return-panel-title"><FileText size={15} /> {creating ? "新增记录" : "详情编辑"}</div>
          {(selected || creating) ? (
            <ReturnRecordEditor
              record={selected}
              creating={creating}
              shops={shops}
              onCancel={() => setCreating(false)}
              onCreate={async (body) => { await onCreate(body); setCreating(false); }}
              onUpdate={onUpdate}
              onDelete={async (id) => { await onDelete(id); setSelectedId(null); }}
            />
          ) : <div className="empty-note">选择一条记录查看详情</div>}
        </aside>
      </div>
    </div>
  );
}

function toDateInput(value?: string) {
  return value ? value.replace(" ", "T").slice(0, 16) : "";
}

function formFromRecord(record: ReturnRecord | null, shops: Shop[]): RecordForm {
  if (!record) return { ...EMPTY_FORM, shop_id: shops[0]?.id ? String(shops[0].id) : "" };
  return {
    shop_id: String(record.shop_id || ""),
    user_uid: record.user_uid || "",
    username: record.username || "",
    order_no: record.order_no || "",
    order_status: record.order_status || "待核实",
    record_type: record.record_type || "return",
    new_address: record.new_address || "",
    remark: record.remark || "",
    source_message: record.source_message || "",
  };
}

function bodyFromForm(form: RecordForm) {
  return {
    shop_id: Number(form.shop_id),
    user_uid: form.user_uid.trim(),
    username: form.username.trim(),
    order_no: form.order_no.trim(),
    order_status: form.order_status.trim() || "待核实",
    record_type: form.record_type,
    new_address: form.new_address.trim() || null,
    remark: form.remark.trim() || null,
    source_message: form.source_message.trim() || null,
  };
}

function ReturnRecordEditor({
  record,
  creating,
  shops,
  onCancel,
  onCreate,
  onUpdate,
  onDelete,
}: {
  record: ReturnRecord | null;
  creating: boolean;
  shops: Shop[];
  onCancel: () => void;
  onCreate: (body: any) => Promise<void>;
  onUpdate: (id: number, body: any) => Promise<void>;
  onDelete: (id: number) => Promise<void>;
}) {
  const [form, setForm] = useState<RecordForm>(() => formFromRecord(record, shops));
  const { isPending, runAction } = usePendingActions();
  const saveKey = creating ? "return-create" : `return-update-${record?.id || "none"}`;
  const deleteKey = `return-delete-${record?.id || "none"}`;
  const savePending = isPending(saveKey);
  const deletePending = isPending(deleteKey);
  const busy = savePending || deletePending;

  useEffect(() => { setForm(formFromRecord(record, shops)); }, [record, creating, shops]);

  function update(key: keyof RecordForm, value: string) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  async function submit() {
    const body = bodyFromForm(form);
    if (!body.shop_id || !body.user_uid || !body.order_no || !body.record_type) return;
    await runAction(saveKey, async () => {
      if (creating) await onCreate(body);
      else if (record) await onUpdate(record.id, body);
    }).catch(() => undefined);
  }

  async function remove() {
    if (!record) return;
    if (!window.confirm(`确认删除订单 ${record.order_no} 的退换记录吗？`)) return;
    await runAction(deleteKey, async () => {
      await onDelete(record.id);
    }).catch(() => undefined);
  }

  const canSave = Boolean(form.shop_id && form.user_uid.trim() && form.order_no.trim() && form.record_type);

  return (
    <div className="return-edit-form">
      {!creating && record && <div className="return-edit-id">ID {record.id} · {record.created_at}</div>}
      <label className="form-stack">
        <span className="form-label">店铺</span>
        <select className="input input-sm" disabled={busy} value={form.shop_id} onChange={(event) => update("shop_id", event.target.value)}>
          <option value="">选择店铺</option>
          {shops.map((shop) => <option key={shop.id} value={shop.id}>{shop.name}</option>)}
        </select>
      </label>
      <label className="form-stack">
        <span className="form-label">类型</span>
        <select className="input input-sm" disabled={busy} value={form.record_type} onChange={(event) => update("record_type", event.target.value)}>
          {EDIT_TYPE_OPTIONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label className="form-stack">
        <span className="form-label">用户 UID</span>
        <input className="input input-sm" disabled={busy} value={form.user_uid} onChange={(event) => update("user_uid", event.target.value)} />
      </label>
      <label className="form-stack">
        <span className="form-label">用户名</span>
        <input className="input input-sm" disabled={busy} value={form.username} onChange={(event) => update("username", event.target.value)} />
      </label>
      <label className="form-stack">
        <span className="form-label">订单号</span>
        <input className="input input-sm" disabled={busy} value={form.order_no} onChange={(event) => update("order_no", event.target.value)} />
      </label>
      <label className="form-stack">
        <span className="form-label">订单状态</span>
        <input className="input input-sm" disabled={busy} value={form.order_status} onChange={(event) => update("order_status", event.target.value)} />
      </label>
      <label className="form-stack">
        <span className="form-label">新地址</span>
        <textarea className="input return-textarea" disabled={busy} value={form.new_address} onChange={(event) => update("new_address", event.target.value)} />
      </label>
      <label className="form-stack">
        <span className="form-label">备注</span>
        <textarea className="input return-textarea" disabled={busy} value={form.remark} onChange={(event) => update("remark", event.target.value)} />
      </label>
      <label className="form-stack">
        <span className="form-label">来源消息</span>
        <textarea className="input return-textarea" disabled={busy} value={form.source_message} onChange={(event) => update("source_message", event.target.value)} />
      </label>
      <div className="return-edit-actions">
        <button className="btn btn-primary btn-sm" disabled={!canSave || busy} onClick={submit}>
          {savePending ? <Loader2 className="spin" size={14} /> : <Save size={14} />}
          {savePending ? "保存中" : "保存"}
        </button>
        {creating && <button className="btn btn-ghost btn-sm" disabled={busy} onClick={onCancel}><X size={14} /> 取消</button>}
        {!creating && record && <button className="btn btn-danger btn-sm" disabled={busy} onClick={remove}>
          {deletePending ? <Loader2 className="spin" size={14} /> : <Trash2 size={14} />}
          {deletePending ? "删除中" : "删除"}
        </button>}
      </div>
    </div>
  );
}
