#!/usr/bin/env python3
# ===========================================================================
# HIVE STUDIO — THE BATCH DRIVER
# ===========================================================================
# Streams a PI's dataset through a curated serving runtime and writes the
# results. Runs as the MAIN container of the batch Job, beside the runtime,
# which is a native sidecar in the same pod — so "the model" is always
# http://127.0.0.1:8080 and there is no gateway, no Service and no Host header
# anywhere in this file.
#
# ---------------------------------------------------------------------------
# THIS PROCESS'S EXIT IS WHAT ENDS THE RUN
# ---------------------------------------------------------------------------
# A native sidecar is terminated by the kubelet when the pod's main container
# exits. That makes this script the run's clock: when it returns, the runtime is
# torn down, the GPU is released, and the Job reports Complete. So it must
# ALWAYS exit, and always write its summary first — including on the failure
# paths, where the summary is the only explanation anyone gets.
#
# ---------------------------------------------------------------------------
# TWO MACHINE-READABLE LINES, AND WHY THEY ARE A CONTRACT
# ---------------------------------------------------------------------------
# The BFF parses this process's stdout — there is no callback, no shared
# database and no sidecar API. Two line shapes are load-bearing:
#
#   BATCH_PROGRESS {"done":N,"total":T,"failed":F,"rowsPerSec":R,"etaSeconds":E}
#   BATCH_RESULT   {"rows":N,"failed":F,"durationSeconds":D,...}
#
# One line each, one JSON object each, flushed immediately. Anything else this
# script prints is for a human reading the step log and is ignored by the
# parser, which takes the LAST line of each shape — so a progress line printed
# while a later one is in flight can never be read as final.
#
# Everything else here is ordinary Python and deliberately dependency-light:
# httpx for HTTP, and the standard library for the rest.
# ===========================================================================

from __future__ import annotations

import csv
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# CONFIGURATION — every knob arrives as an environment variable
# ---------------------------------------------------------------------------
# Read once, at start, and echoed into the log. A run that behaved oddly should
# be explicable from its own first ten lines.


def _int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


INPUT_PATH = os.environ.get("INPUT_PATH", "/mnt/scratch/input/dataset.csv")
INPUT_FORMAT = (os.environ.get("INPUT_FORMAT") or "csv").strip().lower()
# ONE OR MORE COLUMNS, IN ORDER. Comma-separated on the way in because an
# environment variable is a string; the ORDER is part of the answer, not
# incidental — for a classic model these columns ARE the feature vector, so
# swapping two of them silently changes what every row means.
#
# It replaced a single INPUT_FIELD, which could not express the commonest case
# there is: the bundled iris sample has four feature columns, and a classifier
# needs all four in one instance. Asking for one column meant sending `5.1`
# where the model expected `[5.1, 3.5, 1.4, 0.2]`.
INPUT_FIELDS = [
    f.strip() for f in (os.environ.get("INPUT_FIELDS") or "").split(",") if f.strip()
]
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "/mnt/scratch/output"))
RUNTIME_URL = (os.environ.get("RUNTIME_URL") or "http://127.0.0.1:8080").rstrip("/")
MODEL_INTERFACE = (os.environ.get("MODEL_INTERFACE") or "chat").strip()
MODEL_ID = os.environ.get("MODEL_ID", "model")
MODEL_INVOKE_PATH = os.environ.get("MODEL_INVOKE_PATH", "/v1/chat/completions")
# WHAT THE RUNTIME CALLS THE MODEL, which is not what the CATALOGUE calls it.
#
# KServe V1 embeds the served name in the path, and a serving runtime is started
# with `--model_name={{.Name}}` — rendered by KServe from the InferenceService's
# name, and by Studio (there being no InferenceService in a batch run) from the
# PROJECT name. `MODEL_ID` is the catalogue's id, which is a different string,
# and using it here made every predict request 404 on a runtime that was working
# perfectly.
#
# Falls back to MODEL_ID so the behaviour is defined if the variable is absent.
MODEL_SERVING_NAME = (os.environ.get("MODEL_SERVING_NAME") or "").strip() or MODEL_ID

# The prompt arrives JSON-ENCODED. A prompt is multi-line free text written by a
# PI; passing it raw through a YAML `value:` and then a shell environment is
# three quoting layers deep, and the failure mode is a template that silently
# loses its newlines. One json.loads here costs nothing and removes the whole
# class of problem.
_raw_prompt = os.environ.get("PROMPT_TEMPLATE", "")
try:
    PROMPT_TEMPLATE = json.loads(_raw_prompt) if _raw_prompt.startswith('"') else _raw_prompt
except json.JSONDecodeError:
    PROMPT_TEMPLATE = _raw_prompt

MAX_ROWS = _int("MAX_ROWS", 200)
BATCH_SIZE = max(1, _int("BATCH_SIZE", 64))
CONCURRENCY = max(1, _int("CONCURRENCY", 2))
MAX_FAILURE_RATE_PCT = _int("MAX_FAILURE_RATE_PCT", 10)
PROGRESS_EVERY = max(1, _int("PROGRESS_EVERY", 25))

# How long to wait for the sidecar. A first-ever model load can be slow — a GGUF
# is read off a local-path volume and mapped into memory — so this is generous.
HEALTH_TIMEOUT_SECONDS = 300
# Per-request ceiling. A chat completion on a CPU-only 0.5B model takes tens of
# seconds; a predict batch takes under one.
REQUEST_TIMEOUT_SECONDS = 180.0
# Before this many rows have been attempted, the failure RATE is not a
# meaningful number — 2 failures out of 2 is 100% and means nothing.
MIN_ROWS_BEFORE_ABORT = 20


def say(message: str) -> None:
    """A line for a human. Flushed, so `kubectl logs -f` is live."""
    print(message, flush=True)


def emit(kind: str, payload: dict) -> None:
    """One machine-readable line. THE contract with the BFF — see the header."""
    print(f"{kind} {json.dumps(payload, separators=(',', ':'))}", flush=True)


class RunAborted(RuntimeError):
    """The run stopped itself. The summary is still written."""


# ---------------------------------------------------------------------------
# WAITING FOR THE SIDECAR
# ---------------------------------------------------------------------------


# THE RUNTIMES DISAGREE ABOUT WHERE HEALTH LIVES, so the driver asks all of
# them rather than being told. llama.cpp serves `/health`; KServe's Python
# runtimes serve the V1 protocol's `/v1/models` and answer `/` as well.
#
# Trying a list and LOGGING WHICH ONE ANSWERED is better than picking one per
# interface: it means a runtime added later works without a code change here,
# and when something is wrong the log says exactly what was probed.
HEALTH_PATHS = ("/health", "/v1/models", "/healthz", "/")


def wait_for_runtime() -> str:
    """Block until the sidecar answers, or exit non-zero explaining what was tried."""
    say(f"waiting for the runtime at {RUNTIME_URL} (up to {HEALTH_TIMEOUT_SECONDS}s)")
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    last_error = "no attempt made"
    attempts = 0

    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            for path in HEALTH_PATHS:
                try:
                    response = client.get(f"{RUNTIME_URL}{path}")
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    continue
                attempts += 1
                # ANY answer below 500 counts. A 404 from `/health` on a runtime
                # that does not serve it still proves the process is up and
                # listening, which is the only question being asked.
                if response.status_code < 500:
                    waited = int(HEALTH_TIMEOUT_SECONDS - (deadline - time.monotonic()))
                    say(
                        f"runtime is up — {path} answered {response.status_code} "
                        f"after ~{waited}s"
                    )
                    return path
                last_error = f"{path} -> HTTP {response.status_code}"
            time.sleep(2)

    say(f"ERROR: the runtime never became healthy within {HEALTH_TIMEOUT_SECONDS}s.")
    say(f"       probed {' '.join(HEALTH_PATHS)} at {RUNTIME_URL}")
    say(f"       last error: {last_error}")
    say("       The model may be too large to load, or the runtime may have failed")
    say("       to start. Check the `runtime` container's own log:")
    say("           kubectl logs <pod> -c runtime")
    sys.exit(1)


# ---------------------------------------------------------------------------
# READING THE DATASET
# ---------------------------------------------------------------------------


def _open_rows():
    """Yield (row_index, record, error) — the WHOLE record, not one value.

    Yielding the record rather than a single column is what lets a chat prompt
    reference several columns by name and a classic model build a feature
    vector from several at once. The callers below pick what they need.

    A GENERATOR, not a list: a 200MB JSONL file read into memory would be a
    container OOM on a driver sized for streaming work.
    """
    if INPUT_FORMAT == "jsonl":
        with open(INPUT_PATH, encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    yield index, None, f"line {index + 1} is not valid JSON: {exc}"
                    continue
                if not isinstance(record, dict):
                    yield index, None, f"line {index + 1} is not a JSON object"
                    continue
                missing = [f for f in INPUT_FIELDS if f not in record]
                if missing:
                    yield index, record, f"no key {', '.join(missing)} in this record"
                    continue
                yield index, record, None
    else:
        with open(INPUT_PATH, newline="", encoding="utf-8") as handle:
            for index, record in enumerate(csv.DictReader(handle)):
                missing = [f for f in INPUT_FIELDS if f not in record]
                if missing:
                    yield index, record, f"no column {', '.join(missing)} in this row"
                    continue
                yield index, record, None


def row_values(record: dict) -> list:
    """The chosen columns of one record, IN THE ORDER THE PI CHOSE THEM."""
    return [record.get(f) for f in INPUT_FIELDS]


def row_text(record: dict) -> str:
    """One record's chosen columns as text, for a chat prompt's {input}.

    Several columns are labelled rather than blindly concatenated: a model
    handed `Acme Corp 1998 quarterly` cannot tell where one field ends and the
    next begins, whereas `company: Acme Corp\nyear: 1998\n...` is unambiguous
    and costs a few tokens. A single column is passed through bare, because
    labelling one field adds noise and nothing else.
    """
    values = row_values(record)
    if len(values) == 1:
        return "" if values[0] is None else str(values[0])
    return "\n".join(f"{name}: {record.get(name)}" for name in INPUT_FIELDS)


def check_field_exists() -> list[str]:
    """FAIL FAST if the chosen field is not in the file. Returns the columns found.

    THE MOST COMMON MISCONFIGURATION, and the one with the worst failure mode if
    it is not caught here: every row errors, the run burns its whole allowance
    producing an empty results file, and the PI is handed 5,000 identical
    "no column" errors instead of one sentence.
    """
    if not INPUT_FIELDS:
        say("ERROR: INPUT_FIELDS is empty — nothing tells the driver which columns to read.")
        sys.exit(2)

    try:
        if INPUT_FORMAT == "jsonl":
            with open(INPUT_PATH, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        found = list(json.loads(line).keys())
                        break
                else:
                    found = []
        else:
            with open(INPUT_PATH, newline="", encoding="utf-8") as handle:
                found = list(csv.DictReader(handle).fieldnames or [])
    except FileNotFoundError:
        say(f"ERROR: no dataset at {INPUT_PATH}. The upload step did not leave a file here.")
        sys.exit(2)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        say(f"ERROR: {INPUT_PATH} could not be read as {INPUT_FORMAT}: {exc}")
        sys.exit(2)

    missing = [f for f in INPUT_FIELDS if f not in found]
    if missing:
        say(f"ERROR: {'this field is' if len(missing) == 1 else 'these fields are'} "
            f"not in this dataset: {', '.join(missing)}")
        say(f"       {INPUT_FORMAT} columns found: {', '.join(found) or '(none)'}")
        say("       Nothing was processed. Re-run and choose from the columns above.")
        sys.exit(2)

    return found


def count_rows() -> int:
    """A cheap first pass, so progress has a denominator.

    Worth the extra read of the file: without a total there is no percentage and
    no ETA, and a progress bar that only counts up is barely better than a
    spinner. Bounded by MAX_ROWS so capping a run also caps this.
    """
    total = 0
    for _ in _open_rows():
        total += 1
        if MAX_ROWS and total >= MAX_ROWS:
            break
    return total


# ---------------------------------------------------------------------------
# CALLING THE MODEL
# ---------------------------------------------------------------------------


def build_prompt(record: dict) -> str:
    """This row's prompt.

    TWO WAYS TO REFERENCE THE DATA, and both are useful:

      {input}       the chosen columns, as text (see row_text). The default
                    prompt uses this, so a PI who picks one column and changes
                    nothing gets exactly what they expect.
      {column_name} ANY column of the record, by name — including ones not in
                    INPUT_FIELDS. This is what makes a structured prompt
                    possible: "Given the abstract {abstract}, and knowing it was
                    published in {year}, ..."

    A `{}` naming something the row does not have is left ALONE rather than
    raising. A prompt is free text a PI wrote, and it may legitimately contain
    braces — JSON examples especially, and the default prompt asks for JSON
    output. Killing the run over a brace would be worse than passing it through.
    """
    fields = {"input": row_text(record)}
    fields.update({k: v for k, v in record.items() if isinstance(k, str)})
    try:
        return PROMPT_TEMPLATE.format(**fields)
    except (KeyError, IndexError, ValueError):
        # A placeholder we cannot fill, or a stray brace. Substitute only what
        # we certainly can, and leave the rest of the text untouched.
        out = PROMPT_TEMPLATE
        for key, value in fields.items():
            out = out.replace("{" + key + "}", "" if value is None else str(value))
        return out


def call_chat(client: httpx.Client, record: dict) -> str:
    """One row, one request. OpenAI-compatible chat completions."""
    prompt = build_prompt(record)
    response = client.post(
        f"{RUNTIME_URL}{MODEL_INVOKE_PATH}",
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": prompt}],
            # DETERMINISTIC. A batch run over a dataset is an experiment, and an
            # experiment that cannot be repeated is not one. Temperature 0 means
            # re-running the same rows gives the same answers.
            "temperature": 0,
            "stream": False,
        },
    )
    response.raise_for_status()
    body = response.json()
    return body["choices"][0]["message"]["content"]


def call_predict(client: httpx.Client, values: list) -> list:
    """N rows, one request. KServe V1 predict.

    `{"instances": [...]}` in, `{"predictions": [...]}` out, mapped back to rows
    POSITIONALLY — which is the whole reason a length mismatch has to fail the
    batch rather than be tolerated. If the runtime returned fewer predictions
    than instances, every row after the gap would be silently paired with
    another row's answer, and the results file would be wrong in a way nothing
    downstream could detect.
    """
    # `{name}` is the SERVED name, not the catalogue id — see MODEL_SERVING_NAME.
    path = MODEL_INVOKE_PATH.replace("{name}", MODEL_SERVING_NAME)
    response = client.post(f"{RUNTIME_URL}{path}", json={"instances": values})
    response.raise_for_status()
    predictions = response.json().get("predictions")
    if not isinstance(predictions, list):
        raise ValueError(f"runtime returned no `predictions` list: {response.text[:200]}")
    if len(predictions) != len(values):
        raise ValueError(
            f"runtime returned {len(predictions)} predictions for {len(values)} rows; "
            f"they cannot be matched up"
        )
    return predictions


def coerce_instance(record: dict):
    """One record -> one instance for a predict-v1 model.

    THE CHOSEN COLUMNS, IN ORDER, ARE THE FEATURE VECTOR. sklearn-iris wants
    `[5.1, 3.5, 1.4, 0.2]`, and those four numbers are four columns of the
    dataset — which is exactly why this takes a list of fields rather than one.

    A CSV reader gives strings, so anything that parses as a number becomes one;
    anything that does not is passed through as text, so a single-column text
    classifier still works. A single chosen column yields a SCALAR rather than a
    one-element list, because that is what a one-feature model expects and
    wrapping it would change the shape.
    """
    values = []
    for value in row_values(record):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(value)
            continue
        text = "" if value is None else str(value).strip()
        try:
            values.append(float(text))
        except ValueError:
            values.append(text)
    return values if len(values) > 1 else (values[0] if values else "")


# ---------------------------------------------------------------------------
# THE RUN
# ---------------------------------------------------------------------------


class Results:
    """The three output files, written as the run proceeds.

    APPENDED AS WE GO rather than collected and dumped at the end. A run killed
    by `activeDeadlineSeconds` — which is a normal outcome, not an exceptional
    one — must still leave behind everything it had finished. Buffering in
    memory would mean a timeout produces nothing at all, which is the least
    useful possible result.
    """

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.dir = directory
        self.results = open(directory / "results.jsonl", "w", encoding="utf-8")
        self.errors = open(directory / "errors.jsonl", "w", encoding="utf-8")
        self.ok = 0
        self.failed = 0

    def record(self, row_index: int, value, output, latency_ms: int) -> None:
        self.results.write(
            json.dumps(
                {"row_index": row_index, "input": value, "output": output,
                 "latency_ms": latency_ms},
                ensure_ascii=False,
            )
            + "\n"
        )
        self.results.flush()
        self.ok += 1

    def record_error(self, row_index: int, value, error: str) -> None:
        self.errors.write(
            json.dumps(
                {"row_index": row_index, "input": value, "error": error},
                ensure_ascii=False,
            )
            + "\n"
        )
        self.errors.flush()
        self.failed += 1

    @property
    def attempted(self) -> int:
        return self.ok + self.failed

    def check_failure_rate(self) -> None:
        """Stop a run that is clearly misconfigured, once the rate means anything."""
        if self.attempted < MIN_ROWS_BEFORE_ABORT:
            return
        rate = (self.failed / self.attempted) * 100
        if rate > MAX_FAILURE_RATE_PCT:
            raise RunAborted(
                f"{self.failed} of {self.attempted} rows failed ({rate:.0f}%), which is "
                f"over the {MAX_FAILURE_RATE_PCT}% limit. Stopping so the errors can be "
                f"looked at rather than repeated {MAX_ROWS or 'all'} times. See "
                f"errors.jsonl."
            )

    def close(self) -> None:
        self.results.close()
        self.errors.close()


def progress(done: int, total: int, failed: int, started: float) -> None:
    elapsed = max(1e-6, time.monotonic() - started)
    rate = done / elapsed
    remaining = max(0, total - done)
    emit(
        "BATCH_PROGRESS",
        {
            "done": done,
            "total": total,
            "failed": failed,
            "rowsPerSec": round(rate, 2),
            "etaSeconds": int(remaining / rate) if rate > 0 else None,
        },
    )


def run_chat(client: httpx.Client, out: Results, total: int, started: float) -> None:
    """One request per row, up to CONCURRENCY in flight.

    ORDER IS PRESERVED BY CARRYING THE ROW INDEX, not by waiting. Results are
    written as each row finishes, and every record names its `row_index`, so the
    file can be re-sorted by anyone who cares while the run itself never blocks
    on a slow row holding up faster ones behind it.

    A thread pool rather than asyncio: the work is entirely I/O-bound on one
    local socket, the concurrency ceiling is 8, and threads keep this readable
    next to the synchronous predict path below.
    """
    rows = _open_rows()

    def work(item):
        index, record, read_error = item
        # The INPUT recorded against each result is the columns the model
        # actually saw, not the whole row — a 40-column CSV would otherwise put
        # 39 irrelevant fields into every line of results.jsonl.
        shown = row_text(record) if record else None
        if read_error:
            return index, shown, None, read_error, 0
        began = time.monotonic()
        try:
            answer = call_chat(client, record)
            return index, shown, answer, None, int((time.monotonic() - began) * 1000)
        except Exception as exc:  # noqa: BLE001 — one bad row must not end the run
            return index, shown, None, f"{type(exc).__name__}: {exc}", int(
                (time.monotonic() - began) * 1000
            )

    limited = (row for i, row in enumerate(rows) if not MAX_ROWS or i < MAX_ROWS)
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for index, value, answer, error, latency in pool.map(work, limited):
            if error:
                out.record_error(index, value, error)
            else:
                out.record(index, value, answer, latency)
            if out.attempted % PROGRESS_EVERY == 0:
                progress(out.attempted, total, out.failed, started)
            out.check_failure_rate()


def run_predict(client: httpx.Client, out: Results, total: int, started: float) -> None:
    """BATCH_SIZE rows per request — where a classic model's throughput comes from.

    5,000 rows at 64 per request is 79 round trips rather than 5,000, and on a
    model that answers in single-digit milliseconds the round trips ARE the
    runtime.
    """
    batch: list[tuple[int, object]] = []

    def flush() -> None:
        if not batch:
            return
        began = time.monotonic()
        values = [coerce_instance(record) for _, record in batch]
        try:
            predictions = call_predict(client, values)
        except Exception as exc:  # noqa: BLE001
            # THE BATCH FAILS, NOT THE RUN. Every row in it is recorded as an
            # error with the same reason, so the count stays honest and the
            # failure-rate brake can still see it.
            reason = f"{type(exc).__name__}: {exc}"
            for index, record in batch:
                out.record_error(index, row_text(record), reason)
        else:
            latency = int(((time.monotonic() - began) * 1000) / max(1, len(batch)))
            for (index, record), prediction in zip(batch, predictions):
                out.record(index, row_text(record), prediction, latency)
        batch.clear()
        progress(out.attempted, total, out.failed, started)
        out.check_failure_rate()

    for count, (index, record, read_error) in enumerate(_open_rows()):
        if MAX_ROWS and count >= MAX_ROWS:
            break
        if read_error:
            out.record_error(index, row_text(record) if record else None, read_error)
            out.check_failure_rate()
            continue
        batch.append((index, record))
        if len(batch) >= BATCH_SIZE:
            flush()
    flush()


def write_summary(out: Results, total: int, duration: float, health_path: str,
                  status: str, note: str | None) -> dict:
    """run-summary.json — written on EVERY path, including the failures.

    It is the only artefact that explains a run rather than listing its output,
    so a run that aborted or timed out needs it more than a clean one does.
    """
    rate = round(out.ok / duration, 2) if duration > 0 else 0.0
    summary = {
        "status": status,
        "note": note,
        "model": {
            "id": MODEL_ID,
            # The name the RUNTIME answered to, which is not the id above and is
            # what a V1 predict path is built from. Recorded because a run that
            # 404'd every row is explained by exactly this pair disagreeing.
            "servedAs": MODEL_SERVING_NAME,
            "interface": MODEL_INTERFACE,
            "invokePath": MODEL_INVOKE_PATH,
            "healthPathThatAnswered": health_path,
        },
        "input": {
            "path": INPUT_PATH,
            "format": INPUT_FORMAT,
            "fields": INPUT_FIELDS,
            "rowsRequested": MAX_ROWS or "all",
            "rowsFound": total,
        },
        "parameters": {
            "maxRows": MAX_ROWS,
            "batchSize": BATCH_SIZE if MODEL_INTERFACE == "predict-v1" else None,
            "concurrency": CONCURRENCY if MODEL_INTERFACE == "chat" else None,
            "maxFailureRatePct": MAX_FAILURE_RATE_PCT,
            "promptTemplate": PROMPT_TEMPLATE if MODEL_INTERFACE == "chat" else None,
        },
        "totals": {
            "rowsProcessed": out.ok,
            "rowsFailed": out.failed,
            "rowsAttempted": out.attempted,
            "durationSeconds": round(duration, 2),
            "rowsPerSecond": rate,
        },
    }
    (out.dir / "run-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return summary


def main() -> int:
    say("=== hive studio batch driver ===")
    say(f"input     : {INPUT_PATH} ({INPUT_FORMAT})")
    say(f"columns   : {', '.join(INPUT_FIELDS)}")
    say(f"model     : {MODEL_ID} ({MODEL_INTERFACE}) via {MODEL_INVOKE_PATH}")
    say(f"served as : {MODEL_SERVING_NAME}   <- what the runtime answers to")
    say(f"limits    : maxRows={MAX_ROWS or 'all'} failureRate<={MAX_FAILURE_RATE_PCT}%")
    if MODEL_INTERFACE == "chat":
        say(f"concurrency: {CONCURRENCY}")
    else:
        say(f"batchSize : {BATCH_SIZE}")

    # BEFORE ANYTHING EXPENSIVE. Checking the field costs one read of one line
    # and saves an entire run when it is wrong.
    columns = check_field_exists()
    say(f"columns   : {', '.join(columns)}")

    health_path = wait_for_runtime()

    total = count_rows()
    if total == 0:
        say("ERROR: the dataset has no rows to process.")
        return 2
    say(f"rows      : {total} to process")

    out = Results(OUTPUT_DIR)
    started = time.monotonic()
    status, note, exit_code = "completed", None, 0

    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            if MODEL_INTERFACE == "chat":
                run_chat(client, out, total, started)
            else:
                run_predict(client, out, total, started)
    except RunAborted as exc:
        # A DELIBERATE STOP, not a crash. Non-zero so the Job fails and the UI
        # shows it as failed, but the summary and both files are still written —
        # which is the whole point of aborting rather than grinding on.
        status, note, exit_code = "aborted", str(exc), 3
        say(f"ABORTED: {exc}")
    except Exception as exc:  # noqa: BLE001
        status, note, exit_code = "error", f"{type(exc).__name__}: {exc}", 1
        say(f"ERROR: {note}")

    duration = time.monotonic() - started
    out.close()
    summary = write_summary(out, total, duration, health_path, status, note)

    # THE LAST LINE, ALWAYS — on success, on abort and on error. The BFF parses
    # it to fill the deployment's outputs, and a run that ended without one
    # would show a completed pipeline with no numbers in it.
    emit(
        "BATCH_RESULT",
        {
            "rows": summary["totals"]["rowsProcessed"],
            "failed": summary["totals"]["rowsFailed"],
            "durationSeconds": summary["totals"]["durationSeconds"],
            "rowsPerSecond": summary["totals"]["rowsPerSecond"],
            "artifacts": ["results.jsonl", "errors.jsonl", "run-summary.json"],
            "status": status,
        },
    )
    say(
        f"done: {summary['totals']['rowsProcessed']} ok, "
        f"{summary['totals']['rowsFailed']} failed, "
        f"{summary['totals']['durationSeconds']}s, "
        f"{summary['totals']['rowsPerSecond']} rows/s"
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
