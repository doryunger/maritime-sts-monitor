import { useCallback, useEffect, useState } from "react";
import { api, label, QueueSnapshot, when } from "../api";

const CLOSED = ["succeeded", "superseded"];

export default function Jobs() {
  const [data, setData] = useState<QueueSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [showDone, setShowDone] = useState(false);
  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.jobs());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const jobs = data?.jobs?.filter((job) => showDone || !CLOSED.includes(job.status)) ?? [];
  const count = (status: string) => data?.jobs?.filter((job) => job.status === status).length ?? 0;
  return <section>
    <h1>Jobs</h1>
    <p className="muted">The job ledger on the control host. Nothing on this machine is used to decide what is done.</p>
    <div className="table-options">
      <button onClick={() => void refresh()} disabled={loading}>{loading ? "Refreshing..." : "Refresh"}</button>
      <label><input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} /> Show succeeded and superseded jobs</label>
      {data && <span className="muted small">Checked {when(data.checked_utc)}</span>}
    </div>
    {error && <p className="error">Could not refresh: {error}. The previous view, if present, is unchanged.</p>}
    {data?.error && <p className="error">{data.error}</p>}
    {data?.jobs != null && <>
      <p>{count("ready")} ready, {count("claimed")} running, {count("retry_wait")} waiting to retry, {count("failed")} failed, {count("succeeded")} succeeded, {count("superseded")} superseded.</p>
      {jobs.length === 0 ? <p className="muted">No {showDone ? "" : "open "}jobs in the ledger.</p> : <div className="wrap"><table>
        <thead><tr><th>Area</th><th>Job status</th><th>Scene</th><th>Captured</th><th>Created</th><th>Attempts</th></tr></thead>
        <tbody>{jobs.flatMap((job) => job.scene_details.map((scene) => <tr key={`${job.job_id}-${scene.scene_id}`}>
          <td>{label(job.site)}</td>
          <td title={job.job_id}><span className="status">{job.status}</span>{job.superseded_by && <div className="muted small" title={job.superseded_by}>by {job.superseded_by.slice(0, 8)}</div>}</td>
          <td className="mono">{scene.scene_id}</td><td className="mono">{when(scene.captured)}</td><td className="mono">{when(job.created_utc)}</td>
          <td>{job.attempts.length ? job.attempts.map((a) => <div key={a.attempt_no} title={a.error ?? ""}>{a.attempt_no}: {a.verdict}</div>) : "None"}</td>
        </tr>))}</tbody>
      </table></div>}
    </>}
  </section>;
}
