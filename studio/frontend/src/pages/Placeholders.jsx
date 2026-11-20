// ===========================================================================
// HIVE STUDIO — PLACEHOLDER PAGES
// ===========================================================================
// The deploy wizard and the projects list are built in the next step. They are
// routed now so the navigation is complete and nothing in the console leads to a
// dead link.
//
// Each says plainly that it is not built yet AND what already works underneath,
// because both are true: the backend has run these flows end to end since S2
// (33 smoke assertions), there is simply no screen in front of them. A
// placeholder that implied the whole feature was missing would misrepresent
// where the build actually is.
// ===========================================================================

import { Link, useParams } from "react-router-dom";

import { EmptyState } from "../components.jsx";

export function Deployments() {
  return (
    <div className="page">
      <div className="page__head">
        <h1>My projects</h1>
        <p>Everything you have running, and what state it is in.</p>
      </div>

      <EmptyState title="This screen is built in the next step">
        <p className="small muted">
          The list, the live status and the teardown button all arrive with the
          project pages. The backend behind them already works — it can create,
          track, retry and remove a project — so this is a missing screen rather
          than a missing feature.
        </p>
        <p className="small muted">
          Until then: <code>curl localhost:8000/api/deployments</code>
        </p>
        <Link className="btn btn--ghost btn--sm" to="/catalog">
          Back to catalog
        </Link>
      </EmptyState>
    </div>
  );
}

export function Deploy() {
  const { id } = useParams();
  return (
    <div className="page">
      <div className="page__head">
        <h1>Configure your project</h1>
        <p>
          The questions from <code>{id}</code>, one at a time, then a live view of the
          setup as it runs.
        </p>
      </div>

      <EmptyState title="This screen is built in the next step">
        <p className="small muted">
          The wizard is generated from the template's questions — the same ones listed
          on the template page — and the progress view follows the six setup steps as
          they happen.
        </p>
        <Link className="btn btn--ghost btn--sm" to={`/templates/${id}`}>
          Back to the template
        </Link>
      </EmptyState>
    </div>
  );
}
