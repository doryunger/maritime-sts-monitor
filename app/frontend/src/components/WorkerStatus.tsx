import { useEffect, useState } from "react";
import { api, WorkerStatus as Status } from "../api";

const STATES: Record<string, string> = {
  pending: "Starting",
  running: "Running",
  stopping: "Stopping",
  "shutting-down": "Stopping",
  stopped: "Stopped",
};

const ACTIVITY: Record<string, string> = {
  idle: "Idle",
  "preparing imagery": "Preparing imagery",
  detecting: "Detecting",
  unreachable: "Unreachable",
};

const percent = (value: number | null) => (value === null ? "—" : `${value}%`);

export default function WorkerStatus() {
  const [status, setStatus] = useState<Status | null>(null);

  useEffect(() => {
    let alive = true;
    const refresh = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const data = await api.hotspotTestWorker();
        if (alive) setStatus(data);
      } catch {
        if (alive) setStatus(null);
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 30000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  if (!status) return null;
  const running = status.state === "running";
  return (
    <dl className="worker-status">
      <div><dt>Worker</dt><dd>{STATES[status.state] ?? status.state}</dd></div>
      <div><dt>Doing</dt><dd>{(status.activity && ACTIVITY[status.activity]) || "—"}</dd></div>
      <div><dt>CPU</dt><dd>{running ? percent(status.cpu) : "—"}</dd></div>
      <div><dt>GPU</dt><dd>{running ? percent(status.gpu) : "—"}</dd></div>
      <div><dt>Updated</dt><dd>{status.measured_utc.slice(11, 16)}Z</dd></div>
    </dl>
  );
}
