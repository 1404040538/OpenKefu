import { useCallback, useRef, useState } from "react";

type PendingTask<T> = () => Promise<T>;

export function usePendingActions() {
  const pendingRef = useRef<Set<string>>(new Set());
  const [pendingKeys, setPendingKeys] = useState<Set<string>>(() => new Set());

  const syncPending = useCallback(() => {
    setPendingKeys(new Set(pendingRef.current));
  }, []);

  const isPending = useCallback((key: string) => pendingKeys.has(key), [pendingKeys]);

  const runAction = useCallback(async <T,>(key: string, task: PendingTask<T>): Promise<T | undefined> => {
    if (pendingRef.current.has(key)) return undefined;
    pendingRef.current.add(key);
    syncPending();
    try {
      return await task();
    } finally {
      pendingRef.current.delete(key);
      syncPending();
    }
  }, [syncPending]);

  return { isPending, runAction };
}
