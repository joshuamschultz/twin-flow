export function Spinner({ label }: { label?: string }) {
  return (
    <div className="status-line">
      <span className="spinner" />
      <span>{label ?? "Running…"}</span>
    </div>
  );
}
