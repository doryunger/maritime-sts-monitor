import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, label, SceneRow, sceneTag, when } from "../api";

type Key = "run" | "area" | "source" | "captured" | "created" | "status" | "candidates" | "review";

const COLUMNS: { key: Key; title: string }[] = [
  { key: "run", title: "Run" },
  { key: "area", title: "Area" },
  { key: "source", title: "Source" },
  { key: "captured", title: "Captured" },
  { key: "created", title: "Created" },
  { key: "status", title: "Run status" },
  { key: "candidates", title: "Pair candidates" },
  { key: "review", title: "Review status" },
];

const KEYS = COLUMNS.map((c) => c.key);
const PAGE_SIZE = 30;

const sortValue = (r: SceneRow, k: Key): string | number => (k === "run" ? r.run : k === "candidates" ? r.candidates ?? 0 : r[k] ?? "");

export default function HotspotTestScenes() {
  const [rows, setRows] = useState<SceneRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(true);
  const [checked, setChecked] = useState<string | null>(null);
  const [showEmpty, setShowEmpty] = useState(false);
  const [params, setParams] = useSearchParams();
  const go = useNavigate();
  const fromUrl = params.get("sort") as Key | null;
  const sort: { key: Key; dir: 1 | -1 } =
    fromUrl && KEYS.includes(fromUrl) ? { key: fromUrl, dir: params.get("dir") === "asc" ? 1 : -1 } : { key: "captured", dir: -1 };
  const area = params.get("area") || "";

  const update = (next: Record<string, string>) => {
    const copy = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) {
      if (v) copy.set(k, v);
      else copy.delete(k);
    }
    setParams(copy, { replace: true });
  };

  useEffect(() => {
    let etag: string | null = null;
    let pending = false;
    const controller = new AbortController();
    const refresh = async () => {
      if (pending || controller.signal.aborted) return;
      pending = true;
      setRefreshing(true);
      try {
        const result = await api.hotspotTestScenes(etag, controller.signal);
        if (controller.signal.aborted) return;
        etag = result.etag;
        if (result.rows) setRows(result.rows);
        setChecked(new Date().toISOString());
        setError(null);
      } catch (e) {
        if (!controller.signal.aborted) setError((e as Error).message);
      } finally {
        pending = false;
        if (!controller.signal.aborted) setRefreshing(false);
      }
    };
    const visibleRefresh = () => { if (document.visibilityState === "visible") void refresh(); };
    void refresh();
    const timer = window.setInterval(visibleRefresh, 15000);
    window.addEventListener("focus", visibleRefresh);
    document.addEventListener("visibilitychange", visibleRefresh);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      window.removeEventListener("focus", visibleRefresh);
      document.removeEventListener("visibilitychange", visibleRefresh);
    };
  }, []);

  const areas = useMemo(() => [...new Set((rows ?? []).map((r) => r.area))].sort(), [rows]);
  const sorted = useMemo(() => {
    if (!rows) return [];
    return rows
      .filter((r) => !area || r.area === area)
      .sort((a, b) => {
        const x = sortValue(a, sort.key), y = sortValue(b, sort.key);
        const tie = a.run - b.run || a.captured.localeCompare(b.captured) || a.scene_id.localeCompare(b.scene_id);
        return (x < y ? -1 : x > y ? 1 : tie) * sort.dir;
      });
  }, [rows, area, sort.key, sort.dir]);

  const matching = showEmpty ? sorted : sorted.filter((r) => (r.candidates ?? 0) > 0 || r.status === "failed");
  const requested = Number(params.get("page"));
  const pages = Math.max(1, Math.ceil(matching.length / PAGE_SIZE));
  const page = Number.isInteger(requested) && requested >= 1 ? Math.min(requested, pages) : 1;
  const visible = matching.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const toggle = (key: Key) => update({ sort: key, dir: sort.key === key && sort.dir === -1 ? "asc" : sort.key === key ? "desc" : "asc", page: "1" });
  const open = (r: SceneRow) => go(`/hotspot-tests/runs/${r.run}?scene=${encodeURIComponent(r.scene_id)}`);

  if (error && !rows) return <p className="error">Could not load test runs: {error}</p>;
  if (!rows) return <p className="muted">Loading test runs…</p>;

  return (
    <section>
      <h1>Earlier area scans</h1>
      {error && <p className="error">Could not refresh: {error}. The previous list is unchanged.</p>}
      <p className="muted">
        One row per scanned scene in the exploratory areas. Open a row to review its pair candidates and mark each as a real pair or not.
        Scan bounds and the findings Parquets are on the <Link to="/hotspot-tests/areas">areas page</Link>.
      </p>
      <div className="table-options">
        <label>
          Area{" "}
          <select value={area} onChange={(e) => update({ area: e.target.value, page: "1" })}>
            <option value="">All areas</option>
            {areas.map((a) => <option key={a} value={a}>{label(a.replace(/^test_/, ""))}</option>)}
          </select>
        </label>
        <label><input type="checkbox" checked={showEmpty} onChange={(e) => { setShowEmpty(e.target.checked); update({ page: "1" }); }} /> Show scenes with no pair candidates</label>
        <span className="muted small">Showing {matching.length} of {sorted.length} scenes</span>
        <span className="muted small">{refreshing ? "Checking for updates..." : checked ? `Updated ${when(checked)}` : ""}</span>
      </div>
      {matching.length ? (
        <div className="wrap">
          <table className="runs">
            <thead>
              <tr>
                {COLUMNS.map((c) => (
                  <th key={c.key} aria-sort={sort.key === c.key ? (sort.dir === 1 ? "ascending" : "descending") : "none"}>
                    <button className="sort" onClick={() => toggle(c.key)}>
                      {c.title}
                      <span className="arrow">{sort.key === c.key ? (sort.dir === 1 ? "▲" : "▼") : ""}</span>
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {visible.map((r) => (
                <tr key={`${r.run}-${r.scene_id}`} onClick={() => open(r)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && open(r)}>
                  <td className="mono">{r.run}</td>
                  <td>{label(r.area.replace(/^test_/, ""))}</td>
                  <td>{r.source.toUpperCase()} <span className="muted mono small" title={r.scene_id}>{sceneTag(r.scene_id)}</span></td>
                  <td className="mono">{when(r.captured)}</td>
                  <td className="mono">{when(r.created)}</td>
                  <td><span className={`status s-${r.status}`}>{r.status}</span></td>
                  <td className="n">{r.candidates ?? 0}</td>
                  <td><span className={`status r-${r.review.replace(/ /g, "-")}`}>{r.review}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">{sorted.length ? "No scenes with pair candidates yet. Tick the box to see every scanned scene." : "No hotspot-test scenes have been processed yet."}</p>
      )}
      {matching.length > PAGE_SIZE && <div className="table-options">
        <button onClick={() => update({ page: String(page - 1) })} disabled={page <= 1}>Previous</button>
        <span className="small">Page {page} of {pages}</span>
        <button onClick={() => update({ page: String(page + 1) })} disabled={page >= pages}>Next</button>
        <span className="muted small">Showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, matching.length)} of {matching.length}</span>
      </div>}
      <p className="muted small">Captured: satellite capture time. Created: when processing of that scene finished. Pair candidates: pair and possible-pair detections only.</p>
    </section>
  );
}
