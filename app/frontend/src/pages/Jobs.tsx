import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, label, QueueJob, QueueSnapshot } from "../api";

type Key = "area" | "source" | "status" | "scenes" | "captured" | "updated";

const COLUMNS: { key: Key; title: string }[] = [
  { key: "area", title: "Area" },
  { key: "source", title: "Sensor" },
  { key: "status", title: "Status" },
  { key: "scenes", title: "Scenes" },
  { key: "captured", title: "Captured" },
  { key: "updated", title: "Last update" },
];
const KEYS = COLUMNS.map((c) => c.key);
const STORE = "jobs.sort";
const CLOSED = ["succeeded", "superseded"];
const ORDER = ["claimed", "ready", "retry_wait", "failed", "succeeded", "superseded"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

const clock = (t?: string | null) => {
  if (!t) return "–";
  const d = new Date(t);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
};

const ago = (t?: string | null) => {
  if (!t) return "";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(t).getTime()) / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} h ago` : `${Math.floor(hours / 24)} d ago`;
};

const earliest = (job: QueueJob) => job.scene_details.map((s) => s.captured).sort()[0] ?? "";

const sortValue = (job: QueueJob, key: Key): string | number => {
  switch (key) {
    case "area": return job.site;
    case "source": return job.source;
    case "status": return ORDER.indexOf(job.status);
    case "scenes": return job.scene_details.length;
    case "captured": return earliest(job);
    case "updated": return job.last_update_utc;
  }
};

function remembered(): { key: Key; dir: 1 | -1 } | null {
  try {
    const v = JSON.parse(localStorage.getItem(STORE) || "null");
    return v && KEYS.includes(v.key) && (v.dir === 1 || v.dir === -1) ? v : null;
  } catch {
    return null;
  }
}

const Time = ({ t }: { t?: string | null }) => <span title={t ?? ""}>{clock(t)} <span className="muted small">{ago(t)}</span></span>;

export default function Jobs() {
  const [data, setData] = useState<QueueSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [showDone, setShowDone] = useState(false);
  const [params, setParams] = useSearchParams();
  const fromUrl = params.get("sort") as Key | null;
  const sort: { key: Key; dir: 1 | -1 } = fromUrl && KEYS.includes(fromUrl)
    ? { key: fromUrl, dir: params.get("dir") === "desc" ? -1 : 1 }
    : remembered() ?? { key: "status", dir: 1 };
  const setSort = (next: { key: Key; dir: 1 | -1 }) => {
    setParams({ sort: next.key, dir: next.dir === 1 ? "asc" : "desc" }, { replace: true });
    try {
      localStorage.setItem(STORE, JSON.stringify(next));
    } catch {
      return;
    }
  };
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
  const jobs = useMemo(() => {
    const rows = (data?.jobs ?? []).filter((job) => showDone || !CLOSED.includes(job.status));
    return [...rows].sort((a, b) => {
      const x = sortValue(a, sort.key), y = sortValue(b, sort.key);
      const primary = x < y ? -1 : x > y ? 1 : 0;
      return primary * sort.dir || a.site.localeCompare(b.site) || earliest(a).localeCompare(earliest(b)) || a.job_id.localeCompare(b.job_id);
    });
  }, [data, showDone, sort.key, sort.dir]);
  const count = (status: string) => data?.jobs?.filter((job) => job.status === status).length ?? 0;
  return <section>
    <h1>Jobs</h1>
    <p className="muted">The job ledger on the control host. Nothing on this machine is used to decide what is done. Times are UTC.</p>
    <div className="table-options">
      <button onClick={() => void refresh()} disabled={loading}>{loading ? "Refreshing..." : "Refresh"}</button>
      <label><input type="checkbox" checked={showDone} onChange={(e) => setShowDone(e.target.checked)} /> Show succeeded and superseded jobs</label>
      {data && <span className="muted small">Checked <Time t={data.checked_utc} /></span>}
    </div>
    {error && <p className="error">Could not refresh: {error}. The previous view, if present, is unchanged.</p>}
    {data?.error && <p className="error">{data.error}</p>}
    {data?.jobs != null && <>
      <p>{count("claimed")} running, {count("ready")} ready, {count("retry_wait")} waiting to retry, {count("failed")} failed, {count("succeeded")} succeeded, {count("superseded")} superseded.</p>
      {jobs.length === 0 ? <p className="muted">No {showDone ? "" : "open "}jobs in the ledger.</p> : <div className="wrap"><table>
        <thead><tr>
          {COLUMNS.map((c) => <th key={c.key} aria-sort={sort.key === c.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
            <button className="sort" onClick={() => setSort({ key: c.key, dir: sort.key === c.key ? (sort.dir === 1 ? -1 : 1) : 1 })}>
              {c.title}<span className="arrow">{sort.key === c.key ? (sort.dir === 1 ? "▲" : "▼") : ""}</span>
            </button>
          </th>)}
          <th>Attempts</th><th>Job</th>
        </tr></thead>
        <tbody>{jobs.map((job) => <tr key={job.job_id}>
          <td>{label(job.site)}</td>
          <td>{job.source.toUpperCase()}</td>
          <td><span className="status">{job.status}</span>{job.superseded_by && <div className="muted small" title={job.superseded_by}>by {job.superseded_by.slice(0, 8)}</div>}</td>
          <td>{job.scene_details.map((s) => <div key={s.scene_id} className="mono small" title={s.scene_id}>{s.scene_id.slice(0, 36)}…</div>)}</td>
          <td><Time t={earliest(job)} /></td>
          <td><Time t={job.last_update_utc} /></td>
          <td>{job.attempts.length ? job.attempts.map((a) => <div key={a.attempt_no} className="small" title={a.error ?? ""}>
            {a.attempt_no}: {a.verdict}{a.worker ? ` on ${a.worker}` : ""}{a.before_requeue ? " (before requeue)" : ""}
            {a.error && <div className="muted">{a.error.slice(0, 80)}</div>}
          </div>) : <span className="muted">None</span>}</td>
          <td className="mono small" title={job.job_id}>{job.job_id.slice(0, 8)}</td>
        </tr>)}</tbody>
      </table></div>}
    </>}
  </section>;
}
