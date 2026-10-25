import { useCallback, useEffect, useState } from "react";
import { api, SyncRequest, SyncState, when } from "../api";

const FAST_MS = 5000;
const IDLE_MS = 60000;
const KINDS: [string, string][] = [
  ["added", "added"],
  ["rejected", "rejected"],
  ["restored", "restored"],
  ["status_updated", "status updated"],
  ["unchanged", "unchanged"],
];

function elapsed(from: string | null, now: number): string {
  if (!from) return "";
  const s = Math.max(0, Math.round((now - Date.parse(from)) / 1000));
  return s >= 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`;
}

function Finished({ row }: { row: SyncRequest }) {
  const totals = row.summary?.totals;
  return (
    <>
      <span>
        Last sync: <b>{row.status === "done" ? "finished" : "failed"}</b> {when(row.finished_utc)}, requested by {row.requested_by ?? "unknown"}
        {row.dry_run ? " (dry run)" : ""}
      </span>
      {row.status === "done" && totals && (
        <span className="counts">
          {KINDS.map(([k, name]) => `${name} ${totals[k] ?? 0}`).join(" · ")}
          {row.summary?.duplicates ? ` · duplicates skipped ${row.summary.duplicates}` : ""}
        </span>
      )}
      {row.status === "failed" && <span className="sync-error">Error: {row.error ?? "unknown"}</span>}
    </>
  );
}

export default function SyncPanel() {
  const [state, setState] = useState<SyncState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const active = state?.active ?? null;

  const load = useCallback(async () => {
    try {
      setState(await api.sync());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, active ? FAST_MS : IDLE_MS);
    return () => window.clearInterval(timer);
  }, [load, active?.sync_id, active?.status]);

  useEffect(() => {
    if (!active) return;
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(tick);
  }, [active?.sync_id]);

  const confirm = async () => {
    setBusy(true);
    try {
      setState(await api.requestSync());
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <div className="sync-panel">
      <button className="sync" onClick={() => setConfirming(true)} disabled={!!active || busy || !state}>Sync results</button>
      <div className="sync-status" aria-live="polite">
        {!state && !error && <span className="muted">Loading sync status…</span>}
        {state && !active && <span>Idle{state.last_finished ? "" : ": no sync has run yet"}</span>}
        {active?.status === "pending" && <span>Pending: requested by {active.requested_by ?? "unknown"} {elapsed(active.requested_utc, now)} ago, waiting for the host to start it</span>}
        {active?.status === "running" && <span>Running for {elapsed(active.started_utc, now)} (requested by {active.requested_by ?? "unknown"})</span>}
        {state?.last_finished && <Finished row={state.last_finished} />}
        {error && <span className="sync-error">Sync status: {error}</span>}
      </div>
      {confirming && (
        <div className="modal-back" onClick={() => !busy && setConfirming(false)}>
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="sync-title" onClick={(e) => e.stopPropagation()}>
            <h2 id="sync-title">Sync results to S3?</h2>
            <p>This applies your reviews to the published Parquets in S3 and modifies existing ones.</p>
            <ul>
              <li>Pairs you rejected stay in the file with status rejected.</li>
              <li>Newly confirmed or corrected pairs are added or relabelled.</li>
              <li>Rows you did not review are never touched.</li>
            </ul>
            <div className="actions">
              <button onClick={() => setConfirming(false)} disabled={busy}>Cancel</button>
              <button className="save" onClick={() => void confirm()} disabled={busy}>Confirm</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
