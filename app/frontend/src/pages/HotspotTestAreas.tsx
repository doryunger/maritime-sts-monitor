import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, HotspotTestArea, HotspotTestParquet, when } from "../api";

function size(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export default function HotspotTestAreas() {
  const [areas, setAreas] = useState<HotspotTestArea[] | null>(null);
  const [parquets, setParquets] = useState<HotspotTestParquet[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([api.hotspotTestAreas(), api.hotspotTestParquets()])
      .then(([nextAreas, nextParquets]) => {
        if (!active) return;
        setAreas(nextAreas);
        setParquets(nextParquets.sort((a, b) => b.last_modified.localeCompare(a.last_modified)));
      })
      .catch((e) => active && setError((e as Error).message));
    return () => {
      active = false;
    };
  }, []);

  if (!areas || !parquets) {
    return <section><p className="crumbs"><Link to="/hotspot-tests">Hotspot tests</Link> / Areas</p>{error ? <p className="error">Could not load: {error}</p> : <p className="muted">Loading…</p>}</section>;
  }

  return (
    <section>
      <p className="crumbs"><Link to="/hotspot-tests">Hotspot tests</Link> / Areas</p>
      <h1>Scan areas and findings files</h1>
      <h2>Scan areas</h2>
      <div className="wrap">
        <table>
          <thead><tr><th>Area</th><th>Version</th><th>Look-back</th><th>Scan bounds W, S, E, N</th><th>Reference bounds</th><th>Why it is included</th></tr></thead>
          <tbody>
            {areas.map((area) => (
              <tr key={area.name}>
                <td>{area.label}</td>
                <td className="n">{area.area_version}</td>
                <td className="n">{area.lookback_days} d</td>
                <td className="mono">{area.bbox.join(", ")}</td>
                <td className="mono">{area.reference_bbox.join(", ")}</td>
                <td>{area.rationale}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h2>Findings Parquets</h2>
      {parquets.length ? (
        <div className="wrap">
          <table>
            <thead><tr><th>Object key</th><th>Size</th><th>Updated</th></tr></thead>
            <tbody>
              {parquets.map((item) => (
                <tr key={item.key}>
                  <td className="mono"><a href={api.hotspotTestParquet(item.key)}>{item.key}</a></td>
                  <td className="n">{size(item.size)}</td>
                  <td className="mono">{when(item.last_modified)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="muted">No findings Parquets have been written yet.</p>}
    </section>
  );
}
