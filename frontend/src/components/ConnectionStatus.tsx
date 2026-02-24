interface ConnectionStatusProps {
  status: "disconnected" | "connecting" | "connected" | "error";
}

export function ConnectionStatus({ status }: ConnectionStatusProps) {
  const colors = {
    disconnected: "bg-gray-400",
    connecting: "bg-yellow-400 animate-pulse",
    connected: "bg-green-500",
    error: "bg-red-500",
  };

  const labels = {
    disconnected: "Disconnected",
    connecting: "Connecting...",
    connected: "Connected",
    error: "Connection Error",
  };

  return (
    <div className="flex items-center gap-2 text-xs text-[var(--muted-foreground)]">
      <span className={`w-1.5 h-1.5 rounded-full ${colors[status]} transition-colors duration-300`} />
      <span>{labels[status]}</span>
    </div>
  );
}
