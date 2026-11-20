# ===========================================================================
# HIVE STUDIO BFF — THE DATA PATH
# ===========================================================================
# One `kubectl port-forward` to Kourier, owned by the backend, so the playground
# can reach a model.
#
# ---------------------------------------------------------------------------
# WHY 18080, AND WHY THE BROWSER NEVER SEES IT
# ---------------------------------------------------------------------------
# CLAUDE.md §6 fixes this port and explains both halves. It is deliberately NOT
# 8080: a PI running their own `kubectl port-forward` while using the console must
# not collide with Studio's. And the playground calls the model THROUGH the BFF
# over this port, never from the browser, because a local endpoint is only
# reachable with a port-forward plus a `Host:` header — a local shortcut standing
# in for MetalLB + Kong. Keeping it server-side means the browser never learns a
# URL shape that will not exist in production.
#
# ---------------------------------------------------------------------------
# WHY THIS IS A SUPERVISED CHILD PROCESS AND NOT A ONE-OFF
# ---------------------------------------------------------------------------
# `kubectl port-forward` dies. It dies when the Kourier pod is replaced, when the
# API server connection drops, when the laptop sleeps. A playground that worked
# this morning and returns "connection refused" this afternoon, with no
# explanation, is the failure this supervisor exists to prevent: one process, a
# health check that actually sends a request, and a restart when it goes away.
#
# It runs ONLY in real mode. In mock mode there is nothing to forward to and
# nothing to invoke, so starting it would be a pointless child process holding a
# port on every developer's laptop.
# ===========================================================================

from __future__ import annotations

import asyncio
import os
import sys

import httpx

PORT = 18080
SERVICE = "svc/kourier"
NAMESPACE = "kourier-system"

# How often the supervisor looks at the child. Short enough that a dead forward is
# noticed before a PI clicks Invoke, long enough to be invisible.
CHECK_SECONDS = 10
# Back-off after a failed start, so a permanently broken cluster does not spin.
RETRY_SECONDS = 15


class DataPath:
    """Owns the single port-forward and reports whether it is usable."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._ready = False
        self._detail = "not started"
        self._restarts = 0

    # --- status ------------------------------------------------------------

    @property
    def status(self) -> dict:
        return {
            "ready": self._ready,
            "port": PORT,
            "detail": self._detail,
            "restarts": self._restarts,
        }

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._kill_child()

    async def _kill_child(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None

    async def _spawn(self) -> None:
        await self._kill_child()
        self._proc = await asyncio.create_subprocess_exec(
            "kubectl", "port-forward", SERVICE, "-n", NAMESPACE, f"{PORT}:80",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def _probe(self) -> bool:
        """Is something actually answering on the port?

        A LIVE REQUEST, not a check that the child process exists. `kubectl
        port-forward` stays alive with a broken tunnel underneath it more often
        than it exits cleanly, so "the process is running" is not evidence.

        ANY HTTP response counts as success, including 404. Kourier answers 404 to
        a request carrying no recognised `Host:` header, and that 404 is proof the
        gateway is reachable — which is the only thing being asked here.
        """
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.get(f"http://127.0.0.1:{PORT}/", headers={"Host": "studio-probe.invalid"})
            return True
        except httpx.HTTPError:
            return False

    async def _supervise(self) -> None:
        while True:
            try:
                if self._proc is None or self._proc.returncode is not None:
                    if self._proc is not None:
                        self._restarts += 1
                        print(
                            f"data path: port-forward exited, restarting "
                            f"(restart #{self._restarts})",
                            file=sys.stderr,
                        )
                    await self._spawn()
                    # port-forward needs a moment before it accepts connections.
                    await asyncio.sleep(2)

                healthy = await self._probe()
                if healthy and not self._ready:
                    print(f"data path: ready on 127.0.0.1:{PORT}", file=sys.stderr)
                if not healthy and self._ready:
                    print("data path: probe failed, will restart", file=sys.stderr)

                self._ready = healthy
                self._detail = (
                    f"port-forward {SERVICE} -n {NAMESPACE} {PORT}:80"
                    if healthy
                    else "the port-forward is not answering; restarting"
                )
                if not healthy:
                    await self._kill_child()
                    await asyncio.sleep(RETRY_SECONDS)
                    continue

                await asyncio.sleep(CHECK_SECONDS)

            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the supervisor must not die
                self._ready = False
                self._detail = f"supervisor error: {exc}"
                await asyncio.sleep(RETRY_SECONDS)


# One per process.
data_path = DataPath()


def enabled() -> bool:
    """Real mode only — see the module header."""
    return os.environ.get("STUDIO_EXECUTOR", "mock").strip().lower() == "real"


# ===========================================================================
# PER-SERVICE FORWARDS — THE RAG DATA PATH
# ===========================================================================
# The Kourier forward above is ONE forward to ONE gateway, shared by every
# project, because Knative routes by Host header: one tunnel reaches every model
# in the cluster.
#
# A RAG project's endpoint is not behind that gateway. It is an ordinary
# ClusterIP Service in the tenant's host namespace (a plain Deployment, not a
# Knative Service — see templates/catalog/rag-stack/resources/rag-api.yaml.tmpl
# for why), so reaching it needs a forward PER SERVICE, and therefore per
# project.
#
# ---------------------------------------------------------------------------
# WHY LAZY, CACHED, AND HEALTH-CHECKED — ALL THREE
# ---------------------------------------------------------------------------
#   LAZY     a forward is opened the first time someone actually calls a
#            project's endpoint. Opening one per project at startup would mean N
#            child processes for N projects, most of which nobody is using, and
#            would fail noisily for every project that is torn down.
#   CACHED   a forward survives between requests. Opening one per request adds a
#            second of setup to every question and leaks processes under any
#            concurrency.
#   CHECKED  `kubectl port-forward` stays alive with a broken tunnel underneath
#            it more often than it exits cleanly — the same finding the Kourier
#            supervisor above was built around. "The process is running" is not
#            evidence, so every use probes with a real request and respawns if
#            the probe fails.
#
# The forwards are NOT supervised by a background task, unlike the Kourier one.
# That is deliberate: a Kourier forward must be up before anyone clicks, so it
# is worth a supervisor; a project forward is only interesting while somebody is
# asking that project questions, and a permanent supervisor per project would be
# a background task that outlives its usefulness and keeps a dead project's
# tunnel retrying forever.
# ===========================================================================

import contextlib
import socket


def _free_port() -> int:
    """A local port nothing is listening on.

    Bind to 0, read what the kernel chose, release it. There is a race between
    releasing and `kubectl port-forward` binding, and it is accepted rather than
    engineered away: the alternative is a fixed port per project, which collides
    with whatever a PI is forwarding by hand — the exact problem CLAUDE.md §6
    chose 18080 to avoid. A lost race produces a failed bind and a retry.
    """
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class ServiceForward:
    """One `kubectl port-forward` to one Service, kept usable.

    `ensure()` returns a local port that is answering RIGHT NOW, respawning the
    child if it is not. Callers do not track the port themselves.
    """

    def __init__(self, namespace: str, service: str, remote_port: int, health_path: str):
        self.namespace = namespace
        self.service = service
        self.remote_port = remote_port
        self.health_path = health_path
        self._proc: asyncio.subprocess.Process | None = None
        self._port: int | None = None
        # Serialises ensure(): two concurrent requests to a project whose
        # forward has just died would otherwise both spawn one, and the second
        # would fail to bind and look like an unreachable service.
        self._lock = asyncio.Lock()

    @property
    def port(self) -> int | None:
        return self._port

    async def _probe(self) -> bool:
        if self._port is None:
            return False
        try:
            async with httpx.AsyncClient(timeout=4.0) as client:
                response = await client.get(
                    f"http://127.0.0.1:{self._port}{self.health_path}"
                )
            # ANY answer counts, including a 503. A RAG service whose Qdrant is
            # still starting answers 503 on /healthz, and that 503 is proof the
            # tunnel works — which is the only thing being asked here. Treating
            # it as a broken forward would tear down a perfectly good tunnel and
            # retry, turning a starting dependency into a restart loop.
            return response.status_code > 0
        except httpx.HTTPError:
            return False

    async def _kill(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None
        self._port = None

    async def _spawn(self) -> None:
        await self._kill()
        port = _free_port()
        self._proc = await asyncio.create_subprocess_exec(
            "kubectl", "port-forward",
            f"svc/{self.service}", "-n", self.namespace,
            f"{port}:{self.remote_port}",
            "--address", "127.0.0.1",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._port = port

    async def ensure(
        self,
        attempts: int = 3,
        poll_seconds: float = 0.4,
        ready_timeout: float = 15.0,
    ) -> int:
        """A local port that is answering. Raises RuntimeError if it cannot be.

        POLLS FOR READINESS RATHER THAN SLEEPING A FIXED TIME, and that is a
        correction rather than a refinement. The first version spawned the child,
        slept 1.5 seconds and probed ONCE; three attempts of that is 4.5 seconds
        of waiting spread across three processes.

        `kubectl port-forward` routinely takes longer than that to establish — it
        has to reach the API server, resolve the Service to a pod and open a
        SPDY stream — so the failure was intermittent and load-dependent: fine on
        an idle cluster, and a 503 on the playground's first question when the
        same cluster was busy. Which is exactly when a demo would hit it.

        Now each attempt spawns once and polls for up to `ready_timeout`, so a
        forward that needs four seconds gets four seconds instead of being
        killed at 1.5 and retried from scratch. A child that dies is detected
        and respawned immediately rather than polled at for the full window.
        """
        async with self._lock:
            if self._proc is not None and self._proc.returncode is None:
                if await self._probe():
                    return self._port  # type: ignore[return-value]

            loop = asyncio.get_running_loop()
            last = "not started"
            for attempt in range(attempts):
                await self._spawn()
                deadline = loop.time() + ready_timeout
                while loop.time() < deadline:
                    await asyncio.sleep(poll_seconds)
                    # A child that has exited will never answer; respawning now
                    # beats waiting out the window to discover that.
                    if self._proc is None or self._proc.returncode is not None:
                        last = (
                            f"the forward to svc/{self.service} in {self.namespace} "
                            f"exited immediately (attempt {attempt + 1})"
                        )
                        break
                    if await self._probe():
                        return self._port  # type: ignore[return-value]
                else:
                    last = (
                        f"the forward to svc/{self.service} in {self.namespace} did "
                        f"not answer on 127.0.0.1:{self._port} within "
                        f"{ready_timeout:.0f}s (attempt {attempt + 1})"
                    )
            await self._kill()
            raise RuntimeError(last)

    async def stop(self) -> None:
        async with self._lock:
            await self._kill()


class ForwardPool:
    """The open per-service forwards, one per key.

    Keyed by (namespace, service, port) rather than by project, so a project
    with two services in future gets two forwards rather than one that
    mysteriously points at the wrong one.
    """

    def __init__(self) -> None:
        self._forwards: dict[tuple[str, str, int], ServiceForward] = {}

    def get(
        self, namespace: str, service: str, remote_port: int, health_path: str
    ) -> ServiceForward:
        key = (namespace, service, remote_port)
        forward = self._forwards.get(key)
        if forward is None:
            forward = ServiceForward(namespace, service, remote_port, health_path)
            self._forwards[key] = forward
        return forward

    async def drop(self, namespace: str, service: str, remote_port: int) -> None:
        """Close and forget one forward. Called when a project is torn down —
        a tunnel to a Service that no longer exists is only a process."""
        forward = self._forwards.pop((namespace, service, remote_port), None)
        if forward is not None:
            await forward.stop()

    async def stop_all(self) -> None:
        for forward in list(self._forwards.values()):
            await forward.stop()
        self._forwards.clear()

    def status(self) -> list[dict]:
        return [
            {
                "namespace": f.namespace,
                "service": f.service,
                "localPort": f.port,
                "open": f.port is not None,
            }
            for f in self._forwards.values()
        ]


# One per process, like `data_path` above.
forwards = ForwardPool()


@contextlib.asynccontextmanager
async def temporary_forward(namespace: str, service: str, remote_port: int, health_path: str):
    """A forward that exists only for the duration of a `with` block.

    Used by the pipeline's rag-ready-check, which needs to reach a Service once,
    during provisioning, and must not leave a tunnel behind — the pool above is
    for the playground, where a forward SHOULD outlive one request.
    """
    forward = ServiceForward(namespace, service, remote_port, health_path)
    try:
        yield forward
    finally:
        await forward.stop()
