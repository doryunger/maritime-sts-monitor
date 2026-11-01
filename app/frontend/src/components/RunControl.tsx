import { useCallback, useEffect, useState } from "react";
import { api, GridProcess } from "../api";

const STATES: Record<string, string> = {
  idle: "Idle",
  starting: "Starting",
  running: "Running",
  interrupted: "Interrupted",
};

export default function RunControl() {
  const [process, setProcess] = useState<GridProcess | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = process?.action === null;

  const load = useCallback(async () => {
    try {
      setProcess(await api.hotspotTestGridProcess());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, active ? 5000 : 30000);
    return () => window.clearInterval(timer);
  }, [load, active]);

  const press = async () => {
    if (!process?.action) return;
    setBusy(true);
    try {
      setProcess(await api.startHotspotTestGridProcess(process.action));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!process) return error ? <p className="sync-error">{error}</p> : null;
  return (
    <>
      <h2>Run management</h2>
      <dl className="worker-status">
        <div><dt>Run ID</dt><dd>{process.run_no}</dd></div>
        <div><dt>Process</dt><dd>{STATES[process.status] ?? process.status}</dd></div>
        {process.tiles !== null && <div><dt>Decided</dt><dd>{process.decided}/{process.tiles}</dd></div>}
        {process.requested_by && <div><dt>Started by</dt><dd>{process.requested_by}</dd></div>}
        {process.heartbeat_utc && <div><dt>Updated</dt><dd>{process.heartbeat_utc.slice(11, 16)}Z</dd></div>}
        {(process.error || error) && <div><dt>Error</dt><dd>{process.error || error}</dd></div>}
      </dl>
      <button className="run" onClick={() => void press()} disabled={busy || !process.action}>
        {process.action === "continue" ? `Continue run ${process.run_no}` : "Start a new run"}
      </button>
    </>
  );
}
