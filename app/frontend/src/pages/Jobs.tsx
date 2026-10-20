import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, label, QueueSnapshot, when } from "../api";

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
  const jobs = data?.jobs?.filter((job) => showDone || job.status !== "succeeded") ?? [];
  const active = data?.local.filter((scene) => scene.status === "running") ?? [];
  return <section>
    <h1>Jobs</h1>
    <p className="muted">Shared processing queue, with matching scenes from this machine.</p>
    <div className="table-options">
      <button onClick={() => void refresh()} disabled={loading}>{loading ? "Refreshing..." : "Refresh"}</button>
      <label><input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} /> Show completed jobs</label>
      {data && <span className="muted small">Checked {when(data.checked_utc)}</span>}
    </div>
    {error && <p className="error">Could not refresh: {error}. The previous view, if present, is unchanged.</p>}
    {data?.error && <p className="error">{data.error}</p>}
    {data?.jobs !== null && data && <>
      <p>{data.jobs.filter((job) => job.status === "ready").length} ready, {data.jobs.filter((job) => job.status === "claimed").length} claimed, {data.jobs.filter((job) => job.status === "failed").length} failed.</p>
      {jobs.length === 0 ? <p className="muted">No {showDone ? "" : "pending "}jobs in the shared queue.</p> : <div className="wrap"><table>
        <thead><tr><th>Area</th><th>Job status</th><th>Scene</th><th>Captured</th><th>Queued</th><th>On this machine</th></tr></thead>
        <tbody>{jobs.flatMap((job) => job.scene_details.map((scene) => <tr key={`${job.job_id}-${scene.scene_id}`}>
          <td>{label(job.site)}</td>
          <td title={job.job_id}><span className="status">{job.status}</span></td>
          <td className="mono">{scene.scene_id}</td><td className="mono">{when(scene.captured)}</td><td className="mono">{when(job.created_utc)}</td>
          <td>{scene.local_runs.length ? scene.local_runs.map((run) => <div key={run.run}><Link to={`/runs/${run.run}?scene=${encodeURIComponent(scene.scene_id)}`}>Run {run.run}</Link>: {run.status}</div>) : "No local run"}</td>
        </tr>))}</tbody>
      </table></div>}
    </>}
    <p className="muted small">Local matches use the same area, sensor and scene ID. Boundaries or rules may differ; a local match does not complete or remove a shared job.</p>
    <h2>Processing on this machine</h2>
    {active.length === 0 ? <p className="muted">No scenes currently marked as running locally.</p> : <div className="wrap"><table>
      <thead><tr><th>Run</th><th>Area</th><th>Scene</th><th>Captured</th><th>In shared queue</th></tr></thead>
      <tbody>{active.map((scene) => <tr key={`${scene.run}-${scene.site}-${scene.scene_id}`}>
        <td><Link to={`/runs/${scene.run}?scene=${encodeURIComponent(scene.scene_id)}`}>{scene.run}</Link></td><td>{label(scene.site)}</td>
        <td className="mono">{scene.scene_id}</td><td className="mono">{when(scene.captured)}</td>
        <td>{data?.jobs === null ? "Unknown" : data?.jobs?.some((job) => job.status !== "succeeded" && job.site === scene.site && job.scene_details.some((item) => item.source === scene.source && item.scene_id === scene.scene_id)) ? "Yes" : "No"}</td>
      </tr>)}</tbody>
    </table></div>}
  </section>;
}
