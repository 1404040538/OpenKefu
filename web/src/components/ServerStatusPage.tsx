import { Activity, Cpu, Database, Gauge, HardDrive, MemoryStick, Network, RefreshCw, Server, Timer } from "lucide-react";
import type { ReactNode } from "react";
import type { ServerStatusHistory, ServerStatusHistoryPoint, ServerStatusLatest } from "../types/types";

const RANGES: ServerStatusHistory["range"][] = ["1h", "6h", "24h", "7d", "30d"];

export function ServerStatusPage({ latest, history, range, onRangeChange, onRefresh }: {
  latest: ServerStatusLatest | null;
  history: ServerStatusHistory | null;
  range: ServerStatusHistory["range"];
  onRangeChange: (range: ServerStatusHistory["range"]) => void;
  onRefresh: () => Promise<void>;
}) {
  const sample = latest?.sample || null;
  const points = history?.items || [];

  return (
    <div className="page server-status-page">
      <div className="page-header">
        <div className="page-header-left">
          <Server size={22} />
          <div>
            <h2 className="page-title">服务器状态</h2>
            <p className="page-subtitle">CPU、内存、磁盘、网络和进程运行状态</p>
          </div>
        </div>
        <button className="btn btn-sm" onClick={() => onRefresh().catch(() => undefined)} title="刷新">
          <RefreshCw size={14} />
        </button>
      </div>

      {!sample ? (
        <div className="server-empty">
          <Server size={24} />
          <strong>暂无服务器采集数据</strong>
          <span>{latest?.last_error || "采集器启动后会自动写入最新状态"}</span>
        </div>
      ) : (
        <>
          <section className="server-hero">
            <div>
              <div className="server-node-title">
                <strong>{sample.hostname || latest?.hostname || "unknown"}</strong>
                <span className={`server-state ${latest?.stale ? "stale" : "online"}`}>{latest?.stale ? "延迟" : "在线"}</span>
              </div>
              <div className="server-node-meta">
                <span>PID {sample.pid}</span>
                <span>采集 {sample.collected_at}</span>
                <span>心跳 {latest?.heartbeat_at || "-"}</span>
              </div>
              {latest?.last_error && <div className="server-error">{latest.last_error}</div>}
            </div>
            <div className="server-hero-metrics">
              <Metric icon={<Cpu size={16} />} label="CPU" value={`${sample.cpu_percent}%`} hint={`${sample.cpu_count} 核`} percent={sample.cpu_percent} />
              <Metric icon={<MemoryStick size={16} />} label="内存" value={`${sample.memory_percent}%`} hint={`${formatBytes(sample.memory_used)} / ${formatBytes(sample.memory_total)}`} percent={sample.memory_percent} />
              <Metric icon={<HardDrive size={16} />} label="磁盘" value={`${sample.disk_percent}%`} hint={`${formatBytes(sample.disk_used)} / ${formatBytes(sample.disk_total)}`} percent={sample.disk_percent} />
              <Metric icon={<Gauge size={16} />} label="Load" value={sample.load1.toFixed(2)} hint={`${sample.load5.toFixed(2)} / ${sample.load15.toFixed(2)}`} />
            </div>
          </section>

          <section className="server-detail-grid">
            <div className="server-panel">
              <div className="server-panel-title"><Activity size={15} /> 历史趋势</div>
              <div className="server-range-tabs">
                {RANGES.map((item) => (
                  <button key={item} className={range === item ? "active" : ""} onClick={() => onRangeChange(item)}>{item}</button>
                ))}
              </div>
              <div className="server-chart-grid">
                <Chart title="CPU" unit="%" points={points} field="cpu_percent" />
                <Chart title="内存" unit="%" points={points} field="memory_percent" />
                <Chart title="磁盘" unit="%" points={points} field="disk_percent" />
                <Chart title="网络入站" unit="/s" points={points} field="net_recv_bps" formatter={formatBytes} />
              </div>
            </div>

            <div className="server-panel server-side-panel">
              <div className="server-panel-title"><Timer size={15} /> 运行摘要</div>
              <InfoRows rows={[
                ["运行时间", formatDuration(sample.uptime_seconds)],
                ["连接数", String(sample.connection_count)],
                ["Swap", `${sample.swap_percent}% (${formatBytes(sample.swap_used)} / ${formatBytes(sample.swap_total)})`],
                ["磁盘读", `${formatBytes(sample.disk_read_bps)}/s`],
                ["磁盘写", `${formatBytes(sample.disk_write_bps)}/s`],
                ["网络入站", `${formatBytes(sample.net_recv_bps)}/s`],
                ["网络出站", `${formatBytes(sample.net_sent_bps)}/s`],
              ]} />
            </div>
          </section>

          <section className="server-bottom-grid">
            <div className="server-panel">
              <div className="server-panel-title"><Database size={15} /> 分区</div>
              <div className="server-table-wrap">
                <table className="server-table">
                  <thead><tr><th>挂载点</th><th>设备</th><th>类型</th><th>使用率</th><th>容量</th></tr></thead>
                  <tbody>
                    {sample.partitions.map((part) => (
                      <tr key={`${part.device}-${part.mountpoint}`}>
                        <td>{part.mountpoint}</td>
                        <td>{part.device}</td>
                        <td>{part.fstype || "-"}</td>
                        <td><InlineBar value={part.percent} /></td>
                        <td>{formatBytes(part.used)} / {formatBytes(part.total)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
            <div className="server-panel">
              <div className="server-panel-title"><Network size={15} /> 进程</div>
              <div className="server-table-wrap">
                <table className="server-table">
                  <thead><tr><th>PID</th><th>名称</th><th>CPU</th><th>内存</th><th>用户</th></tr></thead>
                  <tbody>
                    {sample.top_processes.map((proc) => (
                      <tr key={proc.pid}>
                        <td>{proc.pid}</td>
                        <td>{proc.name}</td>
                        <td>{proc.cpu_percent}%</td>
                        <td>{proc.memory_percent}%</td>
                        <td>{proc.username || "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function Metric({ icon, label, value, hint, percent }: { icon: ReactNode; label: string; value: string; hint: string; percent?: number }) {
  return (
    <div className="server-metric-card">
      <div className="server-metric-head">{icon}<span>{label}</span></div>
      <strong>{value}</strong>
      <small>{hint}</small>
      {percent !== undefined && <InlineBar value={percent} />}
    </div>
  );
}

function Chart({ title, unit, points, field, formatter }: {
  title: string;
  unit: string;
  points: ServerStatusHistoryPoint[];
  field: keyof ServerStatusHistoryPoint;
  formatter?: (value: number) => string;
}) {
  const values = points.map((point) => Number(point[field]) || 0);
  const latest = values[values.length - 1] || 0;
  return (
    <div className="server-chart-card">
      <div className="server-chart-head">
        <span>{title}</span>
        <strong>{formatter ? formatter(latest) : latest.toFixed(1)}{formatter ? "" : unit}</strong>
      </div>
      <Sparkline values={values} />
    </div>
  );
}

function Sparkline({ values }: { values: number[] }) {
  if (!values.length) return <div className="server-chart-empty">暂无数据</div>;
  const max = Math.max(...values, 1);
  const width = 280;
  const height = 88;
  const points = values.map((value, index) => {
    const x = values.length === 1 ? 0 : (index / (values.length - 1)) * width;
    const y = height - (Math.max(0, value) / max) * (height - 8) - 4;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  return (
    <svg className="server-sparkline" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

function InlineBar({ value }: { value: number }) {
  const normalized = Math.max(0, Math.min(100, Number(value) || 0));
  return (
    <span className="server-inline-bar">
      <span style={{ width: `${normalized}%` }} />
      <strong>{normalized.toFixed(1)}%</strong>
    </span>
  );
}

function InfoRows({ rows }: { rows: [string, string][] }) {
  return (
    <div className="server-info-rows">
      {rows.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}
    </div>
  );
}

function formatBytes(value: number) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Math.max(0, Number(value) || 0);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`;
}

function formatDuration(seconds: number) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${days} 天 ${hours} 小时 ${minutes} 分钟`;
}
