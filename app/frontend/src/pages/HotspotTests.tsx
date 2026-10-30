import { Link } from "react-router-dom";
import GridMap from "../components/GridMap";
import RunTable from "../components/RunTable";
import WorkerStatus from "../components/WorkerStatus";

export default function HotspotTests() {
  return (
    <section>
      <h1>Hotspot tests</h1>
      <nav className="subnav"><Link to="/hotspot-tests/areas">Areas</Link><Link to="/hotspot-tests/scenes">Earlier scans</Link></nav>
      <GridMap />
      <WorkerStatus />
      <RunTable />
    </section>
  );
}
