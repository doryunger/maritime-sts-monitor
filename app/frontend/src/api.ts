export type Counts = Record<string, number>;

export interface RunSummary {
  run: number;
  created: string;
  area: string | null;
  status: string;
  window?: [string, string] | null;
  sources?: string | null;
  captured?: [string | null, string | null];
  finished?: string | null;
  scenes?: number;
  scenes_failed?: number;
  run_verdicts?: Counts;
  verdicts?: Counts;
  reviewed?: number;
  listed?: number;
}

export interface AisParty {
  slot: string;
  name: string | null;
  flag: string | null;
  imo: string | null;
  mmsi: string | null;
  distance_m: number | null;
  age_min: number | null;
}

export interface Hull {
  id: string;
  scene_id: string;
  site: string;
  hull_id: string;
  source: string;
  time: string;
  lat: number;
  lon: number;
  verdict: string;
  final_verdict: string;
  role: string | null;
  image: boolean;
  ai2: Record<string, number | null>;
  measures: Record<string, number | string | null>;
  ais: AisParty[];
}

export interface Flag {
  hull_id: string;
  status: string;
  correct_verdict: string | null;
  note: string | null;
  reviewer: string | null;
  updated_utc: string | null;
  final_verdict?: string;
}

export interface Scene {
  scene_id: string;
  site: string;
  source: string;
  time: string;
  status: string;
  detector_s: number | null;
  raw: number | null;
  kept: number | null;
  checked: number | null;
  error: string | null;
  [verdict: string]: unknown;
}

export interface FixtureItem {
  key: string | number;
  label_user?: string | null;
  fixture_verdict: string;
  run_verdict: string | null;
  status: string;
  reason?: string;
  distance_m: number | null;
}

export interface FixturePass {
  name: string;
  source: string;
  date: string | null;
  covered: boolean;
  items: FixtureItem[];
  totals: { objects: number; agree: number; differ: number; missing: number; outside_area?: number } | null;
}

export interface RunDetail {
  number: number;
  summary: RunSummary;
  run: {
    hot_spot: string;
    window: [string, string];
    sources: string;
    rules_sha1: string;
    commit: string;
    runs: { run_id: number; start: string; end: string | null; status: string | null }[];
    singles_sample: { n: number; of: number; seed: number };
  };
  scenes: Scene[];
  hulls: Hull[];
  flags: Flag[];
  fixtures: { passes: FixturePass[]; totals: { objects: number; agree: number; differ: number; missing: number } | null };
}

async function json<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || r.statusText);
  }
  return r.json();
}

export const api = {
  runs: () => fetch("/api/runs").then((r) => json<RunSummary[]>(r)),
  run: (n: number) => fetch(`/api/runs/${n}`).then((r) => json<RunDetail>(r)),
  image: (n: number, id: string, kind: "raw" | "overlay") => `/api/runs/${n}/hulls/${encodeURIComponent(id)}/${kind}.png`,
  saveFlag: (n: number, id: string, body: { status: string; correct_verdict: string | null; note: string }) =>
    fetch(`/api/runs/${n}/flags/${encodeURIComponent(id)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => json<Flag>(r)),
};

export const when = (t?: string | null) => (t ? t.replace("T", " ").slice(0, 16) + "Z" : "–");
export const label = (v?: string | null) => (v ? v.replace(/_/g, " ") : "–");
export const num = (v: unknown, d = 2) =>
  v === null || v === undefined ? "n/a" : typeof v === "number" ? (Number.isInteger(v) ? String(v) : v.toFixed(d)) : String(v);
