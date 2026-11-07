import { useCallback, useEffect, useRef, useState } from "react";
import { api, duration, GridProcess, RunMode } from "../api";

const STATES: Record<string, string> = {
  idle: "Idle",
  starting: "Starting",
  running: "Running",
  interrupted: "Interrupted",
};

const MODES: { value: RunMode; label: string }[] = [
  { value: "regular", label: "Regular run" },
  { value: "pairs", label: "Known pairs" },
];

export default function RunControl() {
  const [process, setProcess] = useState<GridProcess | null>(null);
  const [mode, setMode] = useState<RunMode>("regular");
  const [menu, setMenu] = useState(false);
  const holder = useRef<HTMLDivElement>(null);
  const [loaded, setLoaded] = useState(Date.now());
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = process?.action === null;
  const counting = process?.status === "running";
  const accept = (next: GridProcess) => {
    setProcess(next);
    setLoaded(Date.now());
  };

  const load = useCallback(async () => {
    try {
      accept(await api.hotspotTestGridProcess(mode));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [mode]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, active ? 5000 : 30000);
    return () => window.clearInterval(timer);
  }, [load, active]);

  useEffect(() => {
    if (!menu) return;
    const close = (e: Event) => {
      if (e.type === "keydown" ? (e as KeyboardEvent).key === "Escape" : !holder.current?.contains(e.target as Node)) setMenu(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [menu]);

  useEffect(() => {
    if (!counting) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [counting]);

  const press = async () => {
    if (!process?.action) return;
    setBusy(true);
    try {
      accept(await api.startHotspotTestGridProcess(process.action, mode));
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
      <div className="split" ref={holder}>
        <button className="run" onClick={() => void press()} disabled={busy || !process.action}>
          {process.action === "continue" ? `Continue run ${process.label}` : mode === "pairs" ? "Start a known pairs run" : "Start a new run"}
        </button>
        <button className="run toggle" onClick={() => setMenu((open) => !open)} disabled={busy || process.action !== "start"} aria-haspopup="menu" aria-expanded={menu} aria-label="Run mode">▾</button>
        {menu && (
          <ul className="split-menu" role="menu">
            {MODES.map((m) => (
              <li key={m.value}>
                <button role="menuitemradio" aria-checked={m.value === mode} className={m.value === mode ? "on" : ""} onClick={() => { setMode(m.value); setMenu(false); }}>
                  {m.label}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <dl className="worker-status">
        <div><dt>Run ID</dt><dd>{process.label}</dd></div>
        <div><dt>Process</dt><dd>{STATES[process.status] ?? process.status}</dd></div>
        {process.elapsed_seconds > 0 && <div><dt>Elapsed</dt><dd>{duration(process.elapsed_seconds + (counting ? Math.max(0, now - loaded) / 1000 : 0))}</dd></div>}
        {process.tiles !== null && <div><dt>Scanned</dt><dd>{process.decided}/{process.tiles}</dd></div>}
        {process.requested_by && <div><dt>Started by</dt><dd>{process.requested_by}</dd></div>}
        {process.heartbeat_utc && <div><dt>Updated</dt><dd>{process.heartbeat_utc.slice(11, 16)}Z</dd></div>}
        {(process.error || error) && <div><dt>Error</dt><dd>{process.error || error}</dd></div>}
      </dl>
    </>
  );
}
