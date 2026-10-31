export interface Placed {
  col: number;
  row: number;
}

export interface Tiled {
  id: string;
  bbox: [number, number, number, number];
}

export const PITCH = 0.9;
const OFFSETS = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9];

const centre = (t: Tiled) => [(t.bbox[0] + t.bbox[2]) / 2, (t.bbox[1] + t.bbox[3]) / 2];

const clashes = (tiles: Tiled[], offset: number) => {
  const seen = new Set<string>();
  let n = 0;
  for (const t of tiles) {
    const [lon, lat] = centre(t);
    const key = `${Math.floor(lon / PITCH + offset)},${Math.floor(lat / PITCH + offset)}`;
    if (seen.has(key)) n += 1;
    seen.add(key);
  }
  return n;
};

export function layout(tiles: Tiled[]): { offset: number; cells: Map<string, Placed> } {
  const offset = OFFSETS.reduce((best, o) => (clashes(tiles, o) < clashes(tiles, best) ? o : best), OFFSETS[0]);
  const taken = new Set<string>();
  const cells = new Map<string, Placed>();
  for (const t of [...tiles].sort((a, b) => a.id.localeCompare(b.id))) {
    const [lon, lat] = centre(t);
    const x = lon / PITCH + offset;
    const y = lat / PITCH + offset;
    const home = { col: Math.floor(x), row: Math.floor(y) };
    let spot = home;
    if (taken.has(`${home.col},${home.row}`)) {
      const options: { col: number; row: number; d: number }[] = [];
      for (let dc = -3; dc <= 3; dc++) {
        for (let dr = -3; dr <= 3; dr++) {
          const col = home.col + dc;
          const row = home.row + dr;
          if (!taken.has(`${col},${row}`)) options.push({ col, row, d: (col + 0.5 - x) ** 2 + (row + 0.5 - y) ** 2 });
        }
      }
      options.sort((a, b) => a.d - b.d);
      spot = options[0];
    }
    taken.add(`${spot.col},${spot.row}`);
    cells.set(t.id, { col: spot.col, row: spot.row });
  }
  return { offset, cells };
}
