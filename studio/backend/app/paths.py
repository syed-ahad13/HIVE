# ===========================================================================
# HIVE STUDIO BFF — WHERE THINGS LIVE ON DISK
# ===========================================================================
# Two directories, defined once, because two modules need them and they must
# never disagree.
#
# ---------------------------------------------------------------------------
# WHY THIS FILE EXISTS RATHER THAN A SECOND COPY
# ---------------------------------------------------------------------------
# `uploads_root()` is read by BOTH ends of the corpus path: app/main.py writes a
# PI's documents into it, and app/executor_real.py copies them out of it into the
# project's volume. It is also overridable, so that a smoke run stages test
# documents into a temp directory instead of littering the repository.
#
# An override honoured by one end and not the other is the worst possible
# outcome: uploads would succeed, deployments would succeed, and the corpus-upload
# step would silently fall back to the demo corpus because it looked in the
# wrong place. Nothing would report an error.
#
# main.py cannot import executor_real at module scope — that module shells out to
# kubectl and is loaded lazily so a mock-mode process never pulls it in — and
# executor_real importing main would be circular. So the shared definition lives
# here, in a module that imports nothing.
# ===========================================================================

from __future__ import annotations

import os
from pathlib import Path

# Staged corpora, one directory per upload id.
#
# The leading underscore is load-bearing: `_uploads` cannot collide with a
# project directory, because projectName is constrained to ^[a-z0-9-]{3,20}$ and
# cannot begin with one.
UPLOAD_ROOT_NAME = "_uploads"


def repo_root() -> Path:
    """The repository root.

    Overridable so tests and a future container image do not have to sit at a
    fixed depth. Default walks up from studio/backend/app/ to hive-local/.
    """
    env = os.environ.get("HIVE_REPO_ROOT")
    return Path(env).expanduser().resolve() if env else Path(__file__).resolve().parents[3]


def uploads_root() -> Path:
    """Where staged corpora live. Overridable, exactly as STUDIO_DB is.

    The default is `tenants/_uploads/` in the repository, because that is where
    per-project material belongs.

    IT IS OVERRIDABLE FOR THE SAME REASON THE REGISTRY IS:
    scripts/studio-smoke.sh promises that a run leaves nothing behind, and
    without this it left a directory of test documents in the repository on every
    run — gitignored, so invisible in `git status`, and therefore exactly the
    kind of litter nobody notices until it is a hundred directories deep.
    """
    env = os.environ.get("STUDIO_UPLOADS")
    if env:
        return Path(env).expanduser()
    return repo_root() / "tenants" / UPLOAD_ROOT_NAME
