// ===========================================================================
// HIVE STUDIO — MY PROJECTS
// ===========================================================================
// The table of everything a PI has, and the one place a project can be removed.
//
// ---------------------------------------------------------------------------
// DELETION ASKS YOU TO TYPE THE NAME
// ---------------------------------------------------------------------------
// Not an "are you sure?" — those are clicked through without reading. Typing the
// project name is the standard pattern for an irreversible destructive action
// because it forces the person to look at WHICH project they are about to
// destroy, which is the mistake actually worth preventing: deleting the right
// thing by accident is rare, deleting the wrong thing is not.
//
// It matters more here than in most consoles. Removing a project takes its
// namespace, its quota, its private cluster and its storage with it, and there
// is no undo — DEVIATIONS.md gives the local grace period as 10 minutes against
// production's 14 days, so locally there is even less room to change your mind.
// ===========================================================================

import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { del, get } from "../api.js";
import { EmptyState, ErrorState, Loading, StatusPill } from "../components.jsx";

// The registry's run state is not the same vocabulary as the status pill, which
// describes what a project is DOING. Mapped in one place rather than at each use.
//
// `ready` here means the RUN finished (invariant (b) — the registry never caches
// live readiness). The pill says "Ready" because that is what a PI understands,
// and the project page is where a live reading from the cluster will appear.
function pillFor(row) {
  if (row.deletedAt) return "deleted";
  if (row.state === "provisioning" || row.state === "requested") return "provisioning";
  if (row.state === "failed") return "failed";
  if (row.state === "ready") return "ready";
  if (row.state === "deleted") return "deleted";
  return "sleeping";
}

function ConfirmDelete({ project, onCancel, onConfirm, busy, error }) {
  const [typed, setTyped] = useState("");
  const matches = typed.trim() === project;

  return (
    <div className="modal" role="dialog" aria-modal="true" aria-labelledby="del-title">
      <div className="modal__box">
        <h2 id="del-title">Remove {project}?</h2>
        <p className="small">
          This takes down the model, your private cluster and everything stored in the
          project. It cannot be undone.
        </p>
        <div className="field">
          <label htmlFor="confirm">
            Type <code>{project}</code> to confirm
          </label>
          <input
            id="confirm"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoFocus
            autoComplete="off"
          />
        </div>
        {error && (
          <p className="small" style={{ color: "var(--red)" }}>
            {error.message}
          </p>
        )}
        <div className="modal__acts">
          <button type="button" className="btn btn--ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--danger"
            onClick={onConfirm}
            disabled={!matches || busy}
          >
            {busy ? "Removing…" : "Remove project"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function DeploymentsList() {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  const [showAll, setShowAll] = useState(false);
  const [target, setTarget] = useState(null);
  const [busy, setBusy] = useState(false);
  const [deleteError, setDeleteError] = useState(null);

  const load = useCallback(async () => {
    try {
      setRows(await get(`/api/deployments${showAll ? "?all=1" : ""}`));
      setError(null);
    } catch (err) {
      setError(err);
    }
  }, [showAll]);

  useEffect(() => {
    load();
  }, [load]);

  async function confirmDelete() {
    setBusy(true);
    setDeleteError(null);
    try {
      await del(`/api/deployments/${target}`);
      setTarget(null);
      await load();
    } catch (err) {
      setDeleteError(err);
    } finally {
      setBusy(false);
    }
  }

  if (error && !rows) {
    return (
      <div className="page">
        <ErrorState error={error} onRetry={load} />
      </div>
    );
  }
  if (!rows) {
    return (
      <div className="page">
        <Loading verb="Loading your projects…" />
      </div>
    );
  }

  return (
    <div className="page">
      <div className="page__head">
        <h1>My projects</h1>
        <p>Everything you have set up, and how long each one has left.</p>
      </div>

      {rows.length === 0 ? (
        <EmptyState title={showAll ? "Nothing here yet" : "You have no projects yet"}>
          <p className="small muted">
            Pick a starting point from the catalog and Studio will set it up for you.
          </p>
          <Link className="btn btn--primary btn--sm" to="/catalog">
            Browse the catalog
          </Link>
        </EmptyState>
      ) : (
        <div className="tablewrap">
          <table>
            <thead>
              <tr>
                <th>Project</th>
                <th>Template</th>
                <th>Status</th>
                <th>Endpoint</th>
                <th>Days left</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>
                    <b>{row.id}</b>
                  </td>
                  <td className="small muted">{row.templateId}</td>
                  <td>
                    <StatusPill status={pillFor(row)} />
                  </td>
                  <td className="small">
                    {/* The list carries no endpoint — the registry holds intent and
                        history, not live state. The address lives on the project
                        page, which is where a live reading belongs. */}
                    <Link to={`/deployments/${row.id}`}>See project</Link>
                  </td>
                  <td className="num">
                    {row.deletedAt ? "—" : row.daysLeft != null ? row.daysLeft : "—"}
                  </td>
                  <td>
                    <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                      <Link className="btn btn--ghost btn--sm" to={`/deployments/${row.id}`}>
                        Open
                      </Link>
                      {!row.deletedAt && (
                        <button
                          type="button"
                          className="btn btn--danger btn--sm"
                          onClick={() => {
                            setDeleteError(null);
                            setTarget(row.id);
                          }}
                        >
                          Delete
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <label className="small muted" style={{ display: "flex", gap: 7, alignItems: "center" }}>
        <input
          type="checkbox"
          checked={showAll}
          onChange={(e) => setShowAll(e.target.checked)}
        />
        Show removed projects
      </label>

      {target && (
        <ConfirmDelete
          project={target}
          busy={busy}
          error={deleteError}
          onCancel={() => setTarget(null)}
          onConfirm={confirmDelete}
        />
      )}
    </div>
  );
}
