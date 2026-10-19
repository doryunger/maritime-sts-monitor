import { useEffect, useState } from "react";
import { api, Flag, Hull, label, num, when } from "../api";

const FLAGS: [string, string][] = [
  ["ok", "OK"],
  ["wrong_verdict", "Wrong verdict"],
  ["bad_geometry", "Bad geometry"],
  ["other", "Other issue"],
];
const LABELS: [string, string][] = [
  ["is_single", "Single"],
  ["is_moving", "Single (moving)"],
  ["is_pair", "Pair"],
];
const NAMES: Record<string, string> = { pair: "Pair", single: "Single", moving: "Single (moving)" };

interface Props {
  run: number;
  hull: Hull;
  focused?: boolean;
  flag?: Flag;
  onSaved: (f: Flag) => void;
}

export default function HullCard({ run, hull, flag, onSaved, focused }: Props) {
  const [status, setStatus] = useState(flag?.status ?? "");
  const [fix, setFix] = useState(flag?.correct_verdict ?? "");
  const [note, setNote] = useState(flag?.note ?? "");
  const [state, setState] = useState<string>("");
  const [zoom, setZoom] = useState<string | null>(null);

  useEffect(() => {
    setStatus(flag?.status ?? "");
    setFix(flag?.correct_verdict ?? "");
    setNote(flag?.note ?? "");
  }, [flag]);

  const options = hull.verdict === "unmeasured" ? LABELS : FLAGS;
  const alternatives = Object.keys(NAMES).filter((v) => v !== hull.verdict);
  const ready = !!status && (status !== "wrong_verdict" || !!fix);
  const dirty = status !== (flag?.status ?? "") || (fix || "") !== (flag?.correct_verdict ?? "") || note.trim() !== (flag?.note ?? "");
  const name = `q-${hull.id}`;

  const save = async () => {
    setState("Saving…");
    try {
      const f = await api.saveFlag(run, hull.id, { status, correct_verdict: status === "wrong_verdict" ? fix : null, note });
      onSaved(f);
      setState("");
    } catch (e) {
      setState(`Not saved: ${(e as Error).message}`);
    }
  };

  const a = hull.ai2 || {};
  const changed = hull.final_verdict !== hull.verdict;

  return (
    <article id={`hull-${hull.hull_id}`} className={`card ${flag ? `flag-${flag.status}` : ""} ${focused ? "focused" : ""}`}>
      <div className="hid">
        <span className="mono" title={hull.id}>ID {hull.hull_id}</span>
        <button
          className="copy"
          title="Copy a link to this card"
          onClick={() => navigator.clipboard?.writeText(`${location.origin}/runs/${run}?scene=${encodeURIComponent(hull.scene_id)}&hull=${encodeURIComponent(hull.hull_id)}`)}
        >
          link
        </button>
      </div>
      <div className="pics">
        {hull.image ? (
          <>
            <figure onClick={() => setZoom("raw")}>
              <img loading="lazy" src={api.image(run, hull.id, "raw")} alt={`Raw capture of ${label(hull.verdict)} hull`} />
              <figcaption>Raw</figcaption>
            </figure>
            <figure onClick={() => setZoom("overlay")}>
              <img loading="lazy" src={api.image(run, hull.id, "overlay")} alt="Same crop with hull outlines" />
              <figcaption>Outlines</figcaption>
            </figure>
          </>
        ) : (
          <div className="nopic">No pixels stored for this hull</div>
        )}
      </div>
      <div className="cap">
        <span className={`badge v-${hull.verdict}`}>{label(hull.verdict)}</span>
        {changed && <span className={`badge after v-${hull.final_verdict}`}>after QA: {label(hull.final_verdict)}</span>}
        <span className="mono">{hull.source.toUpperCase()} {when(hull.time)}</span>
      </div>
      <div className="mono">{num(hull.lat, 5)} N, {num(hull.lon, 5)} E</div>
      <div className="muted small">
        Ai2 {num(a.length_m, 0)} × {num(a.width_m, 0)} m, heading {num(a.heading, 0)}°
        {a.speed_kn !== undefined && a.speed_kn !== null ? `, ${num(a.speed_kn, 1)} kn` : ""}
      </div>
      {hull.ais?.length ? (
        <div className="ais">
          AIS:{" "}
          {hull.ais.map((p, i) => (
            <span key={i}>
              {i ? " + " : ""}
              <b>{p.name || p.mmsi || "unnamed"}</b>
              {p.flag ? ` (${p.flag})` : ""} {num(p.distance_m, 0)} m, {num(p.age_min, 0)} min
            </span>
          ))}
        </div>
      ) : (
        <div className="ais muted">
          AIS:{" "}
          {hull.ais_status === "no_data" ? "no archive data at capture time" : hull.ais_status === "not_checked" ? "not matched yet" : "no ship matched"}
        </div>
      )}
      <dl className="m">
        {Object.entries(hull.measures || {}).map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{num(v)}</dd>
          </div>
        ))}
      </dl>
      <div className="qa">
        <div className="opts" role="radiogroup" aria-label="QA flag">
          {options.map(([v, t]) => (
            <label key={v}>
              <input type="radio" name={name} value={v} checked={status === v} onChange={() => setStatus(v)} />
              {t}
            </label>
          ))}
        </div>
        {status === "wrong_verdict" && (
          <div className="opts fix" role="radiogroup" aria-label="Correct verdict">
            <span className="muted small">Correct verdict:</span>
            {alternatives.map((v) => (
              <label key={v}>
                <input type="radio" name={`c-${hull.id}`} value={v} checked={fix === v} onChange={() => setFix(v)} />
                {NAMES[v]}
              </label>
            ))}
          </div>
        )}
        <textarea rows={2} placeholder="What looks wrong? (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
        <div className="row">
          <button className="save" disabled={!ready || !dirty || state === "Saving…"} onClick={save}>Save flag</button>
          <span className="muted small">{state || (flag ? `Saved ${when(flag.updated_utc)}` : "")}</span>
        </div>
      </div>
      {zoom && (
        <div className="zoom" onClick={() => setZoom(null)} role="dialog" aria-label="Enlarged image">
          <img src={api.image(run, hull.id, zoom as "raw" | "overlay")} alt="" />
        </div>
      )}
    </article>
  );
}
