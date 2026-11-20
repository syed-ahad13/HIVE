# ===========================================================================
# HIVE STUDIO BFF — THE REGISTRY
# ===========================================================================
# stdlib sqlite3, at studio/backend/studio.db. Two tables: what was asked for, and
# how the pipeline went.
#
# ---------------------------------------------------------------------------
# INVARIANT (b) — STATUS TRUTH IS ALWAYS READ LIVE FROM THE CLUSTER
# ---------------------------------------------------------------------------
# This file is the one most able to break CLAUDE.md Studio invariant (b), so the
# rule is repeated here in full:
#
#   The registry stores INTENT AND HISTORY ONLY — who asked for what, when, and how
#   the pipeline went. It never caches readiness, replica counts, URLs or expiry
#   state. The cluster is the single source of truth for what IS; SQLite records
#   what was ASKED FOR. A cached "Ready" that has since scaled to zero, been
#   evicted, or been deleted is worse than no status at all.
#
# Concretely, for anyone adding a column later:
#
#   ALLOWED    the parameters a PI submitted; when they submitted them; which
#              template; which pipeline steps ran, in what order, and whether each
#              one succeeded, together with its log.
#   FORBIDDEN  is it Ready right now; how many replicas; is it awake or asleep;
#              how long until it expires; what its URL is TODAY.
#
# `deployments.state` is the state of THE PROVISIONING RUN, not of the workload. A
# run that `succeeded` says the pipeline finished; it says nothing about whether
# anything is running now. A T-SZ project is expected to be asleep most of the time
# and that is not a change in this table.
#
# ---------------------------------------------------------------------------
# THE ONE UNCOMFORTABLE COLUMN: outputs_json
# ---------------------------------------------------------------------------
# `outputs` includes `endpointUrl`, and a URL is exactly the kind of thing invariant
# (b) forbids caching. It is stored anyway, and the distinction is real but narrow:
#
#   THIS IS A HISTORICAL RECORD OF WHAT WAS HANDED OVER when the run completed, in
#   the same sense as a step log. It is NOT a status cache and MUST NOT be served
#   as current state.
#
# So: the project page reads `.status.url` live from the InferenceService, every
# time, and never reads this column. What this column is for is history and audit —
# "what did we tell this PI on the day we provisioned it?" — which is a question
# about the past and therefore genuinely ours to answer. If that distinction ever
# starts feeling thin in practice, the honest fix is to drop endpointUrl from the
# stored outputs and keep only the things that are not cluster state (the kubeconfig
# location, the dashboard path), rather than to relax the invariant.
# ===========================================================================

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# ---------------------------------------------------------------------------
# STATES
# ---------------------------------------------------------------------------
# Deployment states describe the PROVISIONING RUN.
#
#   requested  a PI submitted the form; nothing has been created
#   rejected   the `approve` step refused it (reserved name, bad parameter,
#              namespace already exists). Recorded deliberately: a rejection must
#              appear in history AS a rejection, not as a deployment that never
#              started (CLAUDE.md §6, on why `approve` is a real step)
#   provisioning  the pipeline is working through its steps
#   ready      every step completed, including ready-check
#   failed     a step failed
#   deleted    torn down, by expiry or by request. The row stays — history does
#              not disappear because the thing it describes did.
#
# READ `ready` CAREFULLY — it is the state of THE RUN, not of the workload. It
# means the pipeline reached the end of ready-check successfully on the day it
# ran. It does NOT mean anything is serving right now, and nothing may render it
# as though it did (invariant (b)). An on-demand project is asleep most of its
# life and its run is still `ready`; a project deleted out from under Studio is
# still `ready` here until something notices. Live readiness comes from the
# cluster, every time, without exception.
DEPLOYMENT_STATES = (
    "requested",
    "rejected",
    # Accepted, but waiting for the machine. One real run at a time (see
    # pipeline.MAX_CONCURRENT_RUNS) — the project exists and starts by itself.
    "queued",
    "provisioning",
    # THE TWO TERMINAL SUCCESSES, and they are different things.
    #
    #   ready      a SERVICE is up and serving. It will stay in this state until
    #              something changes it — the endpoint exists, holds replicas,
    #              can sleep and wake.
    #   completed  a RUN finished its work and stopped. Nothing is running, and
    #              nothing is supposed to be. Its artifacts are what survive.
    #
    # Collapsing them would make the console lie in one direction or the other:
    # a finished batch run shown as "ready" invites a PI to look for an endpoint
    # that was never created, and a served model shown as "completed" reads as
    # though it had stopped.
    #
    # Which one a template reaches is decided by `lifecycle.kind`, not by the
    # executor — see pipeline.terminal_success_state.
    "ready",
    "completed",
    "failed",
    "deleted",
)

# Step states. `skipped` exists for the steps after a failure, so a failed run's
# history shows which steps never got their turn rather than leaving them pending
# forever.
STEP_STATES = ("pending", "running", "succeeded", "failed", "skipped")


def db_path() -> Path:
    """Where the registry lives. Overridable for tests and smoke runs."""
    env = os.environ.get("STUDIO_DB")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent / "studio.db"


def utcnow() -> str:
    """RFC3339 UTC, to the second.

    Same convention as `hive.ksl/expiry` (CLAUDE.md §2.6), so every timestamp this
    system writes — annotation or database row — reads the same way.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
    -- The project name IS the primary key. It is already unique across the
    -- cluster (it becomes the namespace p-<id> and the vCluster name), so a
    -- separate surrogate id would be a second name for the same thing and an
    -- opportunity for the two to disagree.
    id           TEXT PRIMARY KEY,
    template_id  TEXT NOT NULL,
    -- The PI's answers, exactly as submitted, as JSON. Kept whole rather than
    -- split into columns: this is the INTENT, and a template's parameter set is
    -- data that changes by commit. Columns would mean a migration every time a
    -- template gained a question.
    params_json  TEXT NOT NULL,
    state        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    deleted_at   TEXT,
    -- See the header. Historical record of what was handed over, never a status
    -- cache, never read to display current state.
    outputs_json TEXT,
    -- A human label, editable, REGISTRY-ONLY. The project id is the namespace,
    -- the vCluster and the endpoint hostname and can never change; this exists so
    -- a PI can call it "Coral survey, summer run" without any of that moving.
    display_name TEXT
);

CREATE TABLE IF NOT EXISTS renewal_requests (
    -- An ASK, recorded. Studio grants nothing: extending a project's lifetime is
    -- a decision about scarce shared hardware, and in production it is routed to
    -- the Research Committee. Recording it here means the request exists and is
    -- auditable rather than being a mail nobody can find.
    deployment_id TEXT NOT NULL,
    requested_at  TEXT NOT NULL,
    note          TEXT,
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS steps (
    deployment_id TEXT NOT NULL,
    -- WHICH PIPELINE this row belongs to: 'provision' or 'teardown'.
    --
    -- Both pipelines speak the SAME six canonical step ids — CLAUDE.md §6 is
    -- explicit that those strings must not be respelled, so teardown does not get
    -- a private vocabulary like 'teardown-vcluster'. But a project's
    -- inference-service is created once and removed once, which is two rows with
    -- the same step_id. Without this column the second would overwrite the first,
    -- and the history of how a project was built would be erased by the record of
    -- it being taken down.
    phase         TEXT NOT NULL DEFAULT 'provision',
    step_id       TEXT NOT NULL,
    -- Position in the run. Explicit rather than inferred from step_id, because
    -- the UI's progress list must render in pipeline order before any step has
    -- started and therefore before there is a timestamp to sort by.
    ord           INTEGER NOT NULL,
    state         TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    -- LIVE PROGRESS FOR A LONG STEP, as a JSON object or NULL.
    --
    -- Every step before batch was either running or not, and a log line was
    -- enough to show what it was doing. `batch-run` is different in kind: it is
    -- one step that can last an hour, and "running" for forty minutes with no
    -- number attached is indistinguishable from "hung".
    --
    -- So the step carries a structured snapshot the UI can draw a bar from —
    -- {percent, done, total, failed, rowsPerSec, etaSeconds, detail} — replaced
    -- on each poll rather than appended. It is NOT status truth about the
    -- cluster (invariant (b)); it is the last thing the driver said about its
    -- own work, which is history, the same as the log beside it.
    --
    -- `detail` carries the state a number cannot express: "waiting for GPU —
    -- your project's GPU is currently in use". Rendered in amber, never as an
    -- error, because queueing behind a quota is the system working.
    progress_json TEXT,

    -- What the step printed, APPENDED to as the step runs (see append_step_log).
    -- The failure path is a FEATURE (CLAUDE.md §6): a PI must be able to see which
    -- step failed and why, so the log is part of the history rather than something
    -- only in the server's stdout.
    log           TEXT,
    PRIMARY KEY (deployment_id, phase, step_id),
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_steps_deployment ON steps(deployment_id, phase, ord);
CREATE INDEX IF NOT EXISTS idx_deployments_created ON deployments(created_at DESC);
"""

# Phases. A deployment has one provisioning run and, if it is torn down, one
# teardown run. Both are recorded; neither replaces the other.
PHASES = ("provision", "teardown")


def _migrate_display_name(conn: sqlite3.Connection) -> None:
    """Add `display_name` to a registry created before S6. Cheap and additive."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(deployments)")}
    if "display_name" not in cols:
        conn.execute("ALTER TABLE deployments ADD COLUMN display_name TEXT")


def _migrate_progress_json(conn: sqlite3.Connection) -> None:
    """Add steps.progress_json to a registry that predates batch runs.

    ADDITIVE AND IDEMPOTENT: one ALTER TABLE adding a nullable column. No table
    rewrite, no primary-key change, nothing to copy — so it cannot lose a row,
    and an existing studio.db full of inference and RAG history keeps every one
    of them with the new column reading NULL, which is exactly what "this step
    reported no structured progress" should look like.

    That is a deliberate contrast with _migrate below, which HAD to do the
    rename-copy-drop dance because it changed a primary key. When a migration
    can be an ALTER, it should be.
    """
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(steps)")}
    if "progress_json" in columns:
        return
    conn.execute("ALTER TABLE steps ADD COLUMN progress_json TEXT")


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a pre-S2 registry up to the current schema.

    S1 shipped `steps` without a `phase` column and with PRIMARY KEY
    (deployment_id, step_id). SQLite cannot alter a primary key in place, so the
    standard rename-copy-drop dance is used. Existing rows are all provisioning
    rows by definition — teardown did not exist before S2.

    Runs inside init_db on every startup and is a no-op once applied.
    """
    tables = {
        r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "steps" not in tables:
        return  # fresh database; SCHEMA creates it correctly

    columns = {r["name"] for r in conn.execute("PRAGMA table_info(steps)")}
    if "phase" in columns:
        return  # already migrated

    conn.executescript(
        """
        ALTER TABLE steps RENAME TO steps_pre_s2;
        """
    )
    conn.executescript(SCHEMA)
    conn.execute(
        """
        INSERT INTO steps (deployment_id, phase, step_id, ord, state,
                           started_at, finished_at, log)
        SELECT deployment_id, 'provision', step_id, ord, state,
               started_at, finished_at, log
        FROM steps_pre_s2
        """
    )
    conn.execute("DROP TABLE steps_pre_s2")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open a connection for one unit of work, committing on clean exit.

    A NEW CONNECTION PER CALL, deliberately. FastAPI runs synchronous endpoints in
    a thread pool, and a sqlite3 connection is bound to the thread that made it —
    a shared module-level connection would work in development and then raise
    "SQLite objects created in a thread can only be used in that same thread"
    under concurrency. Connections are cheap against a local file; thread-affinity
    bugs are not.
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        # ON DELETE CASCADE on steps is not enforced unless this is on — sqlite
        # has foreign keys off by default, per connection.
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL lets the smoke script read while the pipeline writes. The -wal and
        # -shm sidecar files are covered by .gitignore.
        conn.execute("PRAGMA journal_mode = WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create the tables if they are not there. Safe to call on every startup."""
    with connect() as conn:
        # MIGRATE FIRST. SCHEMA creates an index over steps(deployment_id, phase,
        # ord); against a pre-S2 table that column does not exist yet and the
        # index fails, so the old table has to be rebuilt before SCHEMA runs.
        # _migrate returns immediately on a fresh or already-migrated database.
        _migrate(conn)
        conn.executescript(SCHEMA)
        _migrate_display_name(conn)
        # After _migrate, which may have recreated `steps` from SCHEMA — in which
        # case the column is already there and this is a no-op.
        _migrate_progress_json(conn)


# ---------------------------------------------------------------------------
# DAO
# ---------------------------------------------------------------------------


def create_deployment(
    deployment_id: str,
    template_id: str,
    params: dict[str, Any],
    steps: list[str],
    state: str = "requested",
) -> None:
    """Record a submitted deploy, and lay out its steps as `pending`.

    The steps are written UP FRONT, in order, so the provisioning UI can render the
    full progress list from the first render — the PI sees the six things that are
    going to happen, rather than watching them appear one at a time with no idea
    how many are left.

    Raises sqlite3.IntegrityError if the project name is taken. That is a real
    guard, not just a constraint: it is the registry's half of "the project does
    not already exist", which the `approve` step also checks against the cluster.
    """
    if state not in DEPLOYMENT_STATES:
        raise ValueError(f"unknown deployment state {state!r}")

    now = utcnow()
    with connect() as conn:
        conn.execute(
            "INSERT INTO deployments (id, template_id, params_json, state, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (deployment_id, template_id, json.dumps(params, sort_keys=True), state, now),
        )
        conn.executemany(
            "INSERT INTO steps (deployment_id, phase, step_id, ord, state) "
            "VALUES (?, 'provision', ?, ?, ?)",
            [(deployment_id, step, i, "pending") for i, step in enumerate(steps)],
        )


def ensure_steps(deployment_id: str, steps: list[str], phase: str) -> None:
    """Lay out a phase's steps as `pending`, without disturbing any that exist.

    Used by teardown, which builds its step rows when it starts rather than when
    the deployment was created — at creation time nobody knows whether the project
    will ever be torn down. `INSERT OR IGNORE` makes it safe to call twice, which
    matters because a teardown can be retried.
    """
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    with connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO steps (deployment_id, phase, step_id, ord, state) "
            "VALUES (?, ?, ?, ?, 'pending')",
            [(deployment_id, phase, step, i) for i, step in enumerate(steps)],
        )


def set_deployment_state(
    deployment_id: str,
    state: str,
    outputs: dict[str, Any] | None = None,
) -> None:
    """Move a run to a new state, optionally recording what it handed over."""
    if state not in DEPLOYMENT_STATES:
        raise ValueError(f"unknown deployment state {state!r}")

    sets = ["state = ?"]
    args: list[Any] = [state]
    if outputs is not None:
        sets.append("outputs_json = ?")
        args.append(json.dumps(outputs, sort_keys=True))
    if state == "deleted":
        sets.append("deleted_at = ?")
        args.append(utcnow())
    args.append(deployment_id)

    with connect() as conn:
        conn.execute(f"UPDATE deployments SET {', '.join(sets)} WHERE id = ?", args)


def _decode_progress(raw: str | None) -> dict[str, Any] | None:
    """steps.progress_json -> dict, tolerating anything unparseable.

    A malformed value must NOT break the project page. Progress is a nicety on
    top of the step list; the step list is what a PI needs when something has
    gone wrong, and losing it to a bad JSON blob would be the worst possible
    trade.
    """
    if not raw:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def set_step_progress(
    deployment_id: str,
    step_id: str,
    progress: dict[str, Any] | None,
    phase: str = "provision",
) -> None:
    """Replace one step's progress snapshot.

    REPLACED, NOT APPENDED — the opposite of append_step_log beside it, and the
    difference is the point. A log is a history of what happened; this is one
    answer to "where is it now?", and keeping the previous answers would mean the
    UI had to work out which was current.

    Silently does nothing for a step that does not exist. The caller is a polling
    loop, and a poll that arrives after the deployment was deleted should not
    raise — it should stop mattering.
    """
    encoded = json.dumps(progress, separators=(",", ":")) if progress else None
    with connect() as conn:
        conn.execute(
            "UPDATE steps SET progress_json = ? "
            "WHERE deployment_id = ? AND phase = ? AND step_id = ?",
            (encoded, deployment_id, phase, step_id),
        )


def set_step(
    deployment_id: str,
    step_id: str,
    state: str,
    log: str | None = None,
    phase: str = "provision",
) -> None:
    """Update one step of one run.

    Timestamps are set by the transition rather than by the caller: `running`
    stamps started_at, and any terminal state stamps finished_at. That keeps the
    duration shown in the UI honest even if a caller forgets to pass one.

    UPSERT rather than UPDATE so a step can be recorded even if it was not laid out
    in advance — a pipeline that gains a step should not lose its history to a
    missing row.
    """
    if state not in STEP_STATES:
        raise ValueError(f"unknown step state {state!r}")

    now = utcnow()
    started = now if state == "running" else None
    finished = now if state in ("succeeded", "failed", "skipped") else None

    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")

    with connect() as conn:
        # ord for a step that was never laid out: put it after everything present
        # IN THE SAME PHASE, so teardown numbers from 0 rather than continuing the
        # provisioning run's sequence.
        row = conn.execute(
            "SELECT COALESCE(MAX(ord) + 1, 0) AS next FROM steps "
            "WHERE deployment_id = ? AND phase = ?",
            (deployment_id, phase),
        ).fetchone()
        next_ord = row["next"] if row else 0

        conn.execute(
            """
            INSERT INTO steps (deployment_id, phase, step_id, ord, state,
                               started_at, finished_at, log)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(deployment_id, phase, step_id) DO UPDATE SET
                state       = excluded.state,
                -- A step ENTERING `running` from any other state is a NEW ATTEMPT,
                -- and gets a fresh clock. Otherwise COALESCE keeps the ORIGINAL
                -- started_at, so a step updated twice WHILE running is not reset.
                --
                -- The distinction used to be missing. Provisioning retry resets
                -- its steps first, but a repeated DELETE does not, so a teardown
                -- step that failed and was run again kept its first start time:
                -- a one-second step was shown as taking 1548s.
                --
                -- `steps.state` here is the row BEFORE this update — SQLite
                -- evaluates every SET expression against the original row.
                started_at  = CASE
                    WHEN excluded.state = 'running' AND steps.state != 'running'
                        THEN excluded.started_at
                    ELSE COALESCE(steps.started_at, excluded.started_at)
                END,
                finished_at = CASE
                    WHEN excluded.state = 'running' AND steps.state != 'running'
                        THEN NULL
                    ELSE COALESCE(excluded.finished_at, steps.finished_at)
                END,
                -- A later call with log=None must not erase a log already written.
                log         = COALESCE(excluded.log, steps.log)
            """,
            (deployment_id, phase, step_id, next_ord, state, started, finished, log),
        )


def append_step_log(
    deployment_id: str,
    step_id: str,
    line: str,
    phase: str = "provision",
) -> None:
    """Add a line to a step's log, keeping what is already there.

    SEPARATE FROM set_step ON PURPOSE. `set_step` REPLACES the log; this one
    ACCUMULATES. A running step emits several lines before it finishes, and the
    pipeline writes each as it happens rather than assembling the whole log and
    saving it at the end — a run that recorded its history only on completion
    would show a PI nothing during the twenty-odd seconds they are watching, and
    would lose everything if the process died mid-step.

    Creates the row if it is missing, so a log line is never dropped because a
    step was not laid out in advance.
    """
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")

    with connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(ord) + 1, 0) AS next FROM steps "
            "WHERE deployment_id = ? AND phase = ?",
            (deployment_id, phase),
        ).fetchone()
        next_ord = row["next"] if row else 0

        conn.execute(
            """
            INSERT INTO steps (deployment_id, phase, step_id, ord, state, log)
            VALUES (?, ?, ?, ?, 'running', ?)
            ON CONFLICT(deployment_id, phase, step_id) DO UPDATE SET
                log = CASE
                        WHEN steps.log IS NULL OR steps.log = '' THEN excluded.log
                        ELSE steps.log || char(10) || excluded.log
                      END
            """,
            (deployment_id, phase, step_id, next_ord, line),
        )


def reset_step(deployment_id: str, step_id: str, phase: str = "provision") -> None:
    """Put a step back to `pending`, clearing its timings and log.

    Used by retry on the step that failed. The log is cleared deliberately: a
    retried step's log should describe THIS attempt, not be a growing transcript
    of every attempt with no marker between them. The fact that an earlier attempt
    failed remains visible in the deployment's own history.
    """
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    with connect() as conn:
        conn.execute(
            "UPDATE steps SET state = 'pending', started_at = NULL, "
            "finished_at = NULL, log = NULL "
            "WHERE deployment_id = ? AND phase = ? AND step_id = ?",
            (deployment_id, phase, step_id),
        )


def get_deployment_full(deployment_id: str) -> dict[str, Any] | None:
    """One run, with its steps in pipeline order. None if there is no such row.

    NOTE FOR THE CALLER: this is history. It carries no live status, by design —
    whatever renders it must read readiness, replicas and URL from the cluster
    (invariant (b)).
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM deployments WHERE id = ?", (deployment_id,)
        ).fetchone()
        if row is None:
            return None

        # Provisioning first, then teardown — the order things happened.
        steps = conn.execute(
            "SELECT phase, step_id, ord, state, started_at, finished_at, log, "
            "       progress_json "
            "FROM steps WHERE deployment_id = ? "
            "ORDER BY CASE phase WHEN 'provision' THEN 0 ELSE 1 END, ord",
            (deployment_id,),
        ).fetchall()

    return {
        "id": row["id"],
        "templateId": row["template_id"],
        "params": json.loads(row["params_json"]),
        "state": row["state"],
        "createdAt": row["created_at"],
        "deletedAt": row["deleted_at"],
        "outputs": json.loads(row["outputs_json"]) if row["outputs_json"] else None,
        "displayName": (row["display_name"] if "display_name" in row.keys() else None)
        or row["id"],
        "steps": [
            {
                "phase": s["phase"],
                "stepId": s["step_id"],
                "ord": s["ord"],
                "state": s["state"],
                "startedAt": s["started_at"],
                "finishedAt": s["finished_at"],
                "durationSeconds": _duration(s["started_at"], s["finished_at"]),
                "log": s["log"],
                # Decoded here rather than handed to the UI as a string, so the
                # console never parses JSON out of a field. NULL for every step
                # that reports no structured progress, which is all of them
                # except `batch-run`.
                "progress": _decode_progress(s["progress_json"]),
            }
            for s in steps
        ],
    }


def _duration(started: str | None, finished: str | None) -> float | None:
    """Seconds a step took, or None if it has not finished.

    Computed on read rather than stored, so it cannot drift from the two
    timestamps it is derived from.
    """
    if not started or not finished:
        return None
    try:
        a = datetime.fromisoformat(started.replace("Z", "+00:00"))
        b = datetime.fromisoformat(finished.replace("Z", "+00:00"))
    except ValueError:  # pragma: no cover — only if a timestamp was hand-edited
        return None
    return round((b - a).total_seconds(), 1)


def list_deployments(include_deleted: bool = False) -> list[dict[str, Any]]:
    """Runs, newest first. Summaries only — no steps, no logs.

    Deleted projects are HIDDEN by default, because the common question is "what
    do I have?" and a list dominated by torn-down projects answers a different
    one. They are never removed, only filtered: pass include_deleted=True (the
    API's `?all=1`) to see the full history, which is the whole reason a soft
    delete is used instead of a real one.
    """
    sql = (
        "SELECT id, template_id, params_json, state, created_at, deleted_at "
        "FROM deployments"
    )
    if not include_deleted:
        sql += " WHERE deleted_at IS NULL"
    sql += " ORDER BY created_at DESC, id"

    with connect() as conn:
        rows = conn.execute(sql).fetchall()

    return [
        {
            "id": r["id"],
            "templateId": r["template_id"],
            # Carried so the API can derive the lifetime countdown from the PI's
            # own answer. Intent, not cluster state.
            "params": json.loads(r["params_json"]),
            "state": r["state"],
            "createdAt": r["created_at"],
            "deletedAt": r["deleted_at"],
        }
        for r in rows
    ]


def set_display_name(deployment_id: str, name: str) -> None:
    """Rename the label only. The id is load-bearing everywhere and never moves."""
    with connect() as conn:
        conn.execute(
            "UPDATE deployments SET display_name = ? WHERE id = ?",
            (name.strip() or None, deployment_id),
        )


def record_renewal_request(deployment_id: str, note: str | None = None) -> str:
    """Record that more time was asked for. Grants nothing — see the table comment."""
    when = utcnow()
    with connect() as conn:
        conn.execute(
            "INSERT INTO renewal_requests (deployment_id, requested_at, note) VALUES (?, ?, ?)",
            (deployment_id, when, note),
        )
    return when


def renewal_requests(deployment_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT requested_at, note FROM renewal_requests "
            "WHERE deployment_id = ? ORDER BY requested_at DESC",
            (deployment_id,),
        ).fetchall()
    return [{"requestedAt": r["requested_at"], "note": r["note"]} for r in rows]


# ---------------------------------------------------------------------------
# ARCHIVING A REMOVED PROJECT SO ITS NAME CAN BE USED AGAIN
# ---------------------------------------------------------------------------


def archive_deleted(deployment_id: str) -> str | None:
    """Move a soft-deleted project aside so its name is free. Returns the new id.

    THE PROBLEM THIS SOLVES. `id` is the primary key and a delete is SOFT — the
    row stays, stamped with `deleted_at`, because history does not disappear
    because the thing it describes did. Correct, and it had a consequence nobody
    stated: the NAME was reserved for ever. Redeploying `my-rag` after removing
    `my-rag` was refused with "a project called 'my-rag' already exists", which
    reads as "your deletion did not work" — and sends someone to look at a
    cluster that is, in fact, perfectly clean.

    That is hostile in a rehearsal console, where deploy / tear down / redeploy
    under the same name is the normal working loop, and where the cluster has no
    conflicting object left at all.

    WHY RENAME RATHER THAN DELETE THE ROW. The history is the point: "what did we
    run last term, and what happened to it?" is a question the registry should
    still answer. So the old project keeps every step, every log and every
    timestamp, under `<id>@<deleted_at>` — which is unambiguous, sorts sensibly
    beside its siblings, and cannot itself collide, because a project name cannot
    contain '@' (the pattern is ^[a-z0-9-]{3,20}$).

    Refuses to touch a LIVE project. Archiving something that is still running
    would orphan its history from the namespace it describes, and the caller has
    no business asking.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT deleted_at FROM deployments WHERE id = ?", (deployment_id,)
        ).fetchone()
        if row is None:
            return None
        if not row["deleted_at"]:
            raise ValueError(
                f"{deployment_id} is not deleted; refusing to archive a live project"
            )

        archived = f"{deployment_id}@{row['deleted_at']}"
        # Both child tables are ON DELETE CASCADE but there is no ON UPDATE, so
        # the rename has to be carried to them explicitly. Foreign keys are OFF
        # for the duration: with them on, the parent UPDATE is rejected before
        # the children can be pointed at the new id, and doing the children
        # first would point them at a row that does not exist yet.
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute(
                "UPDATE deployments SET id = ? WHERE id = ?", (archived, deployment_id)
            )
            conn.execute(
                "UPDATE steps SET deployment_id = ? WHERE deployment_id = ?",
                (archived, deployment_id),
            )
            conn.execute(
                "UPDATE renewal_requests SET deployment_id = ? WHERE deployment_id = ?",
                (archived, deployment_id),
            )
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        return archived
