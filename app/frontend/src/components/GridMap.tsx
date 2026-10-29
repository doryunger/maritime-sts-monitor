import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, GridScan, GridSummary, GridTile, label } from "../api";

const WEST = 53;
const NORTH = 28;
const SCALE = 10;
const WIDTH = (109 - WEST) * SCALE;
const HEIGHT = (NORTH + 2) * SCALE;
const INERT = ["unscanned", "no_imagery"];
const LEGEND: { swatch: string; title: string; statuses: string[] }[] = [
  { swatch: "scanned_pairs", title: "Scanned, pairs", statuses: ["scanned_pairs"] },
  { swatch: "scanned_no_pairs", title: "Scanned, no pairs", statuses: ["scanned_no_pairs"] },
  { swatch: "waiting", title: "Waiting", statuses: ["waiting"] },
  { swatch: "processing", title: "Processing", statuses: ["processing"] },
  { swatch: "failed", title: "Failed", statuses: ["failed"] },
];
const NAMES: Record<string, string> = {
  scanned_pairs: "Processed, pairs",
  scanned_no_pairs: "Processed, no pairs",
  processing: "Processing",
  waiting: "Waiting",
  no_imagery: "No imagery",
  failed: "Failed",
  unscanned: "Unscanned",
};
const name = (status: string) => NAMES[status] ?? label(status);
const LIVE: [string, [number, number, number, number]][] = [
  ["Fujairah", [56.3, 24.8, 57.2, 25.8]],
  ["Sohar", [56.6, 24.2, 57.6, 24.9]],
  ["EOPL", [103.0, 0.3, 106.5, 1.8]],
  ["Johor", [103.5, 1.8, 106.5, 3.3]],
];

const px = (lon: number) => (lon - WEST) * SCALE;
const py = (lat: number) => (NORTH - lat) * SCALE;

export default function GridMap() {
  const [tiles, setTiles] = useState<GridTile[] | null>(null);
  const [summary, setSummary] = useState<GridSummary | null>(null);
  const [land, setLand] = useState<[number, number][][]>([]);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const go = useNavigate();

  useEffect(() => {
    api.hotspotTestGridLand().then((d) => setLand(d.rings)).catch(() => setLand([]));
  }, []);

  useEffect(() => {
    let alive = true;
    const refresh = async () => {
      if (document.visibilityState !== "visible") return;
      try {
        const data = await api.hotspotTestGrid();
        if (!alive) return;
        setTiles(data.tiles);
        setSummary(data.summary);
        setError(null);
      } catch (e) {
        if (alive) setError((e as Error).message);
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 30000);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  const chosen = tiles?.find((t) => t.id === selected) ?? null;
  const open = (s: GridScan) => s.pairs && s.run_no && go(`/hotspot-tests/runs/${s.run_no}?scene=${encodeURIComponent(s.scene)}`);
  const result = (s: GridScan) => (s.status === "failed" ? "Failed" : s.status !== "done" ? "Pending" : s.pairs ? (s.pairs === 1 ? "1 pair" : `${s.pairs} pairs`) : "No pairs");

  if (error && !tiles) return <p className="error">Could not load the grid scan: {error}</p>;
  if (!tiles || !summary) return <p className="muted">Loading the grid scan…</p>;

  return (
    <div className="grid-body">
      <svg className="grid-map" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label="Grid scan status map">
        {land.map((ring, i) => (
          <polygon key={i} className="land" points={ring.map(([lon, lat]) => `${px(lon)},${py(lat)}`).join(" ")} />
        ))}
        {tiles.map((t) => (
          <rect
            key={t.id}
            className={`tile ${t.status}${INERT.includes(t.status) ? " inert" : ""}${t.id === selected ? " selected" : ""}`}
            onClick={INERT.includes(t.status) ? undefined : () => setSelected(t.id === selected ? null : t.id)}
            x={px(t.bbox[0])}
            y={py(t.bbox[3])}
            width={(t.bbox[2] - t.bbox[0]) * SCALE}
            height={(t.bbox[3] - t.bbox[1]) * SCALE}
          >
            {!INERT.includes(t.status) && <title>{`${t.id}: ${name(t.status)}`}</title>}
          </rect>
        ))}
        {LIVE.map(([name, b]) => (
          <g key={name}>
            <rect className="live" x={px(b[0])} y={py(b[3])} width={(b[2] - b[0]) * SCALE} height={(b[3] - b[1]) * SCALE} />
            <text className="live-label" x={px(b[0])} y={py(b[3]) - 2}>{name}</text>
          </g>
        ))}
      </svg>
      <div className="grid-side">
      <div className="grid-legend">
        {LEGEND.map((entry) => ({ ...entry, count: entry.statuses.reduce((n, k) => n + (summary.by_status[k] ?? 0), 0) }))
          .filter((entry) => entry.count > 0)
          .map((entry) => (
            <span key={entry.swatch}><i className={`swatch ${entry.swatch}`} /> {entry.title} ({entry.count})</span>
          ))}
      </div>
      {chosen && chosen.scans.length > 0 && (
        <div className="grid-detail">
          <strong>{chosen.id}</strong>
          <table>
            <tbody>
              {chosen.scans.map((s) => (
                <tr key={s.date_no} className={s.pairs ? "has-pairs" : ""} onClick={() => open(s)} tabIndex={s.pairs ? 0 : undefined} onKeyDown={(e) => e.key === "Enter" && open(s)}>
                  <td>{s.scene_time.slice(0, 10)}</td>
                  <td>{s.source.toUpperCase()}</td>
                  <td>{result(s)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      </div>
    </div>
  );
}
