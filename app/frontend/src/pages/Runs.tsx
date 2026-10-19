import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, label, SceneRow, sceneTag, when } from "../api";

type Key = "run" | "area" | "source" | "captured" | "created" | "status" | "review";

const COLUMNS: { key: Key; title: string }[] = [
  { key: "run", title: "Run" },
  { key: "area", title: "Area" },
  { key: "source", title: "Source" },
  { key: "captured", title: "Captured" },
  { key: "created", title: "Created" },
  { key: "status", title: "Run status" },
  { key: "review", title: "Review status" },
];

const sortValue = (r: SceneRow, k: Key): string | number => (k === "run" ? r.run : r[k] ?? "");

export default function Runs() {
  const [rows, setRows] = useState<SceneRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sort, setSort] = useState<{ key: Key; dir: 1 | -1 }>({ key: "captured", dir: -1 });
  const go = useNavigate();

  useEffect(() => {
    api.scenes().then(setRows).catch((e) => setError(e.message));
  }, []);

  const sorted = useMemo(() => {
    if (!rows) return [];
    return [...rows].sort((a, b) => {
      const x = sortValue(a, sort.key), y = sortValue(b, sort.key);
      const tie = a.run - b.run || a.captured.localeCompare(b.captured) || a.scene_id.localeCompare(b.scene_id);
      return (x < y ? -1 : x > y ? 1 : tie) * sort.dir;
    });
  }, [rows, sort]);

  const toggle = (key: Key) => setSort((s) => ({ key, dir: s.key === key ? (s.dir === 1 ? -1 : 1) : 1 }));
  const open = (r: SceneRow) => go(`/runs/${r.run}?scene=${encodeURIComponent(r.scene_id)}`);

  if (error) return <p className="error">Could not load runs: {error}</p>;
  if (!rows) return <p className="muted">Loading runs…</p>;

  return (
    <section>
      <h1>Runs</h1>
      <p className="muted">One row per scene. Click a column title to sort, a row to open its QA.</p>
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
            {sorted.map((r) => (
              <tr key={`${r.run}-${r.scene_id}`} onClick={() => open(r)} tabIndex={0} onKeyDown={(e) => e.key === "Enter" && open(r)}>
                <td className="mono">{r.run}</td>
                <td>{label(r.area)}</td>
                <td>
                  {r.source.toUpperCase()} <span className="muted mono small" title={r.scene_id}>{sceneTag(r.scene_id)}</span>
                </td>
                <td className="mono">{when(r.captured)}</td>
                <td className="mono">{when(r.created)}</td>
                <td><span className={`status s-${r.status}`}>{r.status}</span></td>
                <td><span className={`status r-${r.review.replace(/ /g, "-")}`}>{r.review}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="muted small">Captured: satellite capture time. Created: when processing of that scene finished.</p>
    </section>
  );
}
