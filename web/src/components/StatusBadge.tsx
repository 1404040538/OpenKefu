import { statusText } from "../utils/helpers";

export function StatusBadge({ status }: { status: string }) {
  return <span className={`status-badge ${status}`}>{statusText(status)}</span>;
}
