import { useEffect, useMemo, useState } from "react";
import { Activity, ChevronLeft, ChevronRight, ListFilter, LocateFixed, RefreshCw, Search, X } from "lucide-react";
import type { LogResponse, RuntimeLog } from "../types/types";

type LogFilters = Record<string, string>;

const LEVELS = ["ERROR", "WARNING", "INFO", "DEBUG"];

export function LogsPage({ data, filters, onFiltersChange, onRefresh }: {
  data: LogResponse | null;
  filters: LogFilters;
  onFiltersChange: (filters: LogFilters) => void;
  onRefresh: () => Promise<void>;
}) {
  const [draft, setDraft] = useState<LogFilters>(filters);
  const [selectedId, setSelectedId] = useState<number | string | null>(null);
  const items = data?.items || [];
  const selected = useMemo(
    () => items.find((item) => String(item.id) === String(selectedId)) || items[0] || null,
    [items, selectedId],
  );
  const total = data?.total || 0;
  const limit = Number(filters.limit || data?.limit || 100);
  const offset = Number(filters.offset || data?.offset || 0);

  useEffect(() => { setDraft(filters); }, [filters]);
  useEffect(() => {
    if (items.length && !items.some((item) => String(item.id) === String(selectedId))) {
      setSelectedId(items[0].id);
    }
  }, [items, selectedId]);

  function updateDraft(key: string, value: string) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function apply(next: LogFilters = draft) {
    const cleaned = Object.fromEntries(Object.entries({ ...next, offset: "0", limit: String(limit) }).filter(([, value]) => value));
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

  function locate() {
    const id = draft.id?.trim();
    const requestId = draft.request_id?.trim();
    if (id || requestId) apply({ ...draft, id, request_id: requestId });
  }

  return (
    <div className="page logs-workbench">
      <div className="page-header">
        <div className="page-header-left">
          <Activity size={22} />
          <div>
            <h2 className="page-title">日志工作台</h2>
            <p className="page-subtitle">查看运行日志、定位请求，并追踪批次执行情况</p>
          </div>
        </div>
        <button className="btn btn-sm" onClick={() => onRefresh().catch(() => undefined)} title="刷新">
          <RefreshCw size={14} />
        </button>
      </div>

      <div className="logs-grid">
        <aside className="logs-filter-panel">
          <div className="logs-panel-title"><ListFilter size={15} /> 筛选</div>
          <div className="logs-filter-stack">
            <div className="logs-levels">
              {LEVELS.map((level) => (
                <button key={level} className={`log-level-filter ${draft.level === level ? "active" : ""}`} onClick={() => updateDraft("level", draft.level === level ? "" : level)}>
                  {level}
                </button>
              ))}
            </div>
            <label className="form-stack">
              <span className="form-label">关键词</span>
              <div className="logs-input-icon">
                <Search size={14} />
                <input className="input input-sm" value={draft.keyword || ""} onChange={(e) => updateDraft("keyword", e.target.value)} />
              </div>
            </label>
            <div className="logs-two-col">
              <label className="form-stack"><span className="form-label">模块</span><input className="input input-sm" value={draft.module || ""} onChange={(e) => updateDraft("module", e.target.value)} /></label>
              <label className="form-stack"><span className="form-label">事件</span><input className="input input-sm" value={draft.action || ""} onChange={(e) => updateDraft("action", e.target.value)} /></label>
            </div>
            <div className="logs-two-col">
              <label className="form-stack"><span className="form-label">店铺 ID</span><input className="input input-sm" value={draft.shop_id || ""} onChange={(e) => updateDraft("shop_id", e.target.value)} /></label>
              <label className="form-stack"><span className="form-label">会话 ID</span><input className="input input-sm" value={draft.conversation_id || ""} onChange={(e) => updateDraft("conversation_id", e.target.value)} /></label>
            </div>
            <label className="form-stack"><span className="form-label">用户 UID</span><input className="input input-sm" value={draft.user_uid || ""} onChange={(e) => updateDraft("user_uid", e.target.value)} /></label>
            <label className="form-stack"><span className="form-label">运行批次</span><input className="input input-sm" value={draft.run_id || ""} onChange={(e) => updateDraft("run_id", e.target.value)} /></label>
            <div className="logs-two-col">
              <label className="form-stack"><span className="form-label">开始时间</span><input className="input input-sm" type="datetime-local" value={toDateInput(draft.date_from)} onChange={(e) => updateDraft("date_from", e.target.value.replace("T", " "))} /></label>
              <label className="form-stack"><span className="form-label">结束时间</span><input className="input input-sm" type="datetime-local" value={toDateInput(draft.date_to)} onChange={(e) => updateDraft("date_to", e.target.value.replace("T", " "))} /></label>
            </div>
            <div className="logs-locate-box">
              <div className="logs-panel-title"><LocateFixed size={15} /> 定位</div>
              <input className="input input-sm" placeholder="日志 ID" value={draft.id || ""} onChange={(e) => updateDraft("id", e.target.value)} />
              <input className="input input-sm" placeholder="请求 ID" value={draft.request_id || ""} onChange={(e) => updateDraft("request_id", e.target.value)} />
              <button className="btn btn-sm btn-full" onClick={locate}>定位</button>
            </div>
            <div className="logs-filter-actions">
              <button className="btn btn-primary btn-sm" onClick={() => apply()}>查询</button>
              <button className="btn btn-ghost btn-sm" onClick={clearFilters}><X size={14} /> 清空</button>
            </div>
          </div>

          <div className="logs-panel-title">运行批次</div>
          <div className="logs-run-list">
            {(data?.facets.runs || []).map((run) => (
              <button key={run.run_id || "empty"} className={`logs-run-item ${filters.run_id === run.run_id ? "active" : ""}`} onClick={() => apply({ ...draft, run_id: run.run_id || "" })}>
                <strong>{run.run_id || "未记录"}</strong>
                <span>{run.started_at || "-"} 至 {run.ended_at || "-"}</span>
                <small>{run.count} 条</small>
              </button>
            ))}
          </div>
        </aside>

        <section className="logs-main-panel">
          <div className="logs-summary">
            <strong>{total}</strong>
            <span>条匹配日志</span>
            <span>第 {total ? offset + 1 : 0}-{Math.min(offset + limit, total)} 条</span>
          </div>
          <div className="logs-table-wrap">
            <table className="logs-table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>时间</th>
                  <th>级别</th>
                  <th>事件</th>
                  <th>模块</th>
                  <th>消息</th>
                  <th>定位</th>
                </tr>
              </thead>
              <tbody>
                {items.map((log) => (
                  <tr key={log.id} className={String(log.id) === String(selected?.id) ? "active" : ""} onClick={() => setSelectedId(log.id)}>
                    <td>{log.id}</td>
                    <td>{log.created_at}</td>
                    <td><span className={`log-level ${String(log.level || "info").toLowerCase()}`}>{log.level}</span></td>
                    <td>{log.action}</td>
                    <td>{log.module}</td>
                    <td title={log.message}>{log.message}</td>
                    <td>{[log.shop_id ? `店铺 ${log.shop_id}` : "", log.user_uid || "", log.request_id || ""].filter(Boolean).join(" / ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!items.length && <div className="empty-note">没有匹配的日志</div>}
          </div>
          <div className="logs-pager">
            <button className="btn btn-sm" disabled={offset <= 0} onClick={() => page(-1)}><ChevronLeft size={14} /> 上一页</button>
            <button className="btn btn-sm" disabled={offset + limit >= total} onClick={() => page(1)}>下一页 <ChevronRight size={14} /></button>
          </div>
        </section>

        <aside className="logs-detail-panel">
          <div className="logs-panel-title">日志详情</div>
          {selected ? <LogDetail log={selected} /> : <div className="empty-note">选择一条日志查看详情</div>}
        </aside>
      </div>
    </div>
  );
}

function toDateInput(value?: string) {
  return value ? value.replace(" ", "T").slice(0, 16) : "";
}

function LogDetail({ log }: { log: RuntimeLog }) {
  const context = log.context && Object.keys(log.context).length ? JSON.stringify(log.context, null, 2) : "";
  return (
    <div className="logs-detail">
      <div><span>ID</span><strong>{log.id}</strong></div>
      <div><span>时间</span><strong>{log.created_at || "-"}</strong></div>
      <div><span>级别</span><strong>{log.level}</strong></div>
      <div><span>事件</span><strong>{log.action || "-"}</strong></div>
      <div><span>模块</span><strong>{log.module || "-"}</strong></div>
      <div><span>运行批次</span><strong>{log.run_id || "-"}</strong></div>
      <div><span>进程</span><strong>{log.pid || "-"}</strong></div>
      <div><span>店铺</span><strong>{log.shop_id || "-"}</strong></div>
      <div><span>会话</span><strong>{log.conversation_id || "-"}</strong></div>
      <div><span>用户</span><strong>{log.user_uid || "-"}</strong></div>
      <div><span>请求</span><strong>{log.request_id || "-"}</strong></div>
      <section>
        <span>消息</span>
        <pre>{log.message}</pre>
      </section>
      {context && <section><span>上下文</span><pre>{context}</pre></section>}
      {log.error_trace && <section><span>异常堆栈</span><pre>{log.error_trace}</pre></section>}
    </div>
  );
}
