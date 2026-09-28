export function Toast({ message, onDismiss }: { message: string; onDismiss: () => void }) {
  if (!message) return null;
  return (
    <button className="toast" onClick={onDismiss}>
      {message}
    </button>
  );
}
