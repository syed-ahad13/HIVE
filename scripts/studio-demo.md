# HIVE Studio — the seven-minute walkthrough

For showing an architect what the console does and, just as importantly, what it
does not. Exact clicks, and what to say while clicking.

**Before you start**

```bash
scripts/fake-gpus.sh                      # if the machine restarted since last time
STUDIO_EXECUTOR=real scripts/studio-up.sh
```

Wait for the box that prints `executor  real`, then open **http://localhost:5173**.

You need one project already running — the walkthrough deploys a second one live
and needs something to compare it against. `jdi` (always-on) and `demo-cv`
(on-demand) both exist at time of writing; any always-on project will do.

> **One thing this script cannot show you.** There is no chat model to demonstrate.
> The catalogue lists Qwen 2.5, but the runtime it needs (`hive-llama-cpp`) has
> never been built — Step 9b does not exist in this repository. Selecting it fails
> at the first step with a message saying exactly that, which is itself worth
> showing if the audience is technical. Everything else below is real.

---

## 0:00 — The catalogue  ·  *what a PI sees first*

**Click** nothing. You land on `/catalog`.

> "This is what a research group sees when they log in. Not a cluster, not a YAML
> file — three things they might want to build."

**Point at** the three sections: **Serve**, **Build**, **Train**.

> "One of these works today. The other two are on the roadmap, and they are here
> deliberately: the console shows the whole shape of the product, so nobody has to
> imagine what comes next. They have no Deploy button — a greyed-out button just
> makes people hunt for what would enable it."

**Click** *Read more* on **Ask questions about your own documents**.

> "The RAG template already has its questions written. When it becomes real, the
> wizard for it is generated from this file — no new frontend code."

**Click** *Back to catalog*.

---

## 0:50 — Template detail  ·  *pre-disclosure*

**Click** *Deploy* on **Serve a model behind an API endpoint**.

> "Before anyone fills in a form, they are told exactly what they get and exactly
> what will happen."

**Scroll** through the three cards, then stop at **What happens when you deploy**.

> "Six steps, named in advance. This matters more than it looks: when step four
> sits on screen for forty seconds, they already know it is building their private
> cluster. And if something fails, they recognise the step that failed."

**Point at** the amber note.

> "Amber means one thing in this console: *this is not how production works*. The
> setup is a rehearsal here, so it takes twenty-five seconds instead of three
> minutes. Nothing else about it is different."

---

## 1:40 — Deploy, for real  ·  *the C-path*

**Click** *Continue — configure*.

- **Project name** → `reef-demo`. **Pause here.**

  > "Watch what appears underneath. That name becomes their cluster and their web
  > address, and it cannot be changed later — so we show both, while it is still
  > changeable."

- **How long** → `7`. **Click** *Continue*.
- **Model** → *Iris classifier*. **Availability** → **On demand**. **Size** → Small.

  > "This is the whole cost conversation, in one radio button. Always on holds a
  > GPU permanently. On demand gives it back when nobody is using it, and pays about
  > fifteen seconds the next time someone is."

  **Point at** the ceiling panel updating.

**Click** *Continue*, then expand **Exact resources to be created**.

> "This is the part I would want to see if I were you. These are the actual
> manifests — same renderer that applies them, so this is not an illustration.
> They get written into the repository under `tenants/reef-demo/rendered/` before
> anything is applied. A project built by clicking is as reviewable as one built
> by hand."

**Click** *Deploy*.

> "Now it is real. Namespace, quota, storage, a private Kubernetes control plane,
> and the model registered with the one shared serving platform."

**While it runs** (~3 minutes), talk over it:

> "Six steps, and every one is a real command — the same ones an engineer would
> type. The logs are live: that is `vcluster create` running right now. If any step
> fails, it stops there, tells them which one, and offers to resume from it. Nothing
> after a failure is left looking pending."

---

## 4:00 — While it builds  ·  *the existing project*

**Click** *My projects* in the sidebar.

> "Everything this group has, with its state and how long it has left."

**Click** *Open* on **jdi** (or your always-on project).

**Overview**:

- **Status** → *Ready*, **Running copies 1**.

  > "Always-on. It holds its GPU whether or not anyone is using it — that is what
  > they chose and what they pay for."

- **Point at** the endpoint. > "Read live from the running service, never
  constructed. Local addresses have a segment production will not have, so nothing
  here is allowed to guess."
- **Point at** the quota bars and the countdown ring.

**Click** the **Playground** tab, press *Send request*.

> "The response comes back in milliseconds because it never went to sleep. The
> browser never talks to the cluster, by the way — the call goes through the
> backend, because reaching an endpoint locally needs a port-forward and a Host
> header, and that is a shortcut we refuse to teach the browser."

---

## 5:00 — The moment  ·  *sleep and wake*

**Click** *My projects*, then *Open* on **demo-cv** (the on-demand one).

If it says **Sleeping** — go straight on. If not, it has been used recently; leave
it a couple of minutes, or use `reef-demo` once it finishes.

**Point at** the status.

> "Sleeping. Zero running copies."

**Point at** the GPU bar: **0 / 2**.

> "And there it is — the GPU is back in the shared pool. Nobody is paying for this
> project right now. This is the whole argument for the on-demand archetype, and
> it is the number I would check."

**Click** **Playground** → *Send request*.

> "Watch the status."

*Sleeping → Waking up → Ready*, then the answer, with **`8.5s · cold start`** in
amber beside it.

> "Eight and a half seconds, and the console said so before they clicked. That is
> the trade, made visible: give the hardware back, wait once when you come back."

**Click** *Send request* again.

> "Milliseconds. It is awake now, and it will go back to sleep about a minute after
> we stop talking to it."

---

## 6:00 — What they actually own

Still on the project page, **Overview**:

**Point at** *Download kubeconfig*.

> "This is the promise the platform makes: it is a real Kubernetes cluster and it
> is theirs. Admin access, inside their own project, without being able to touch
> anyone else's."

**Point at** the countdown ring and read the amber note aloud.

> "The date is real — written on the project when it is created, read back live.
> What is missing is the service that acts on it. We are not pretending that part
> is done."

**Click** **Settings**.

> "They can rename it — the label, not the project id, because that id is the
> cluster name and the address. And they can ask for more time, which records the
> request. Studio grants nothing: extending a project holds shared hardware longer,
> and that is somebody else's decision."

**Scroll** to the danger zone, **type** the project name to arm the button.

> "Not an 'are you sure?' — those get clicked through. Typing the name makes you
> look at *which* project you are about to destroy."

*(Only actually delete `reef-demo`, at the very end.)*

---

## 6:40 — Closing: what this build fakes

**Click** **▲ Local build notes** in the top bar.

> "I want to finish here, because it is the most useful slide. Six things in this
> demo are not how production works, and this is all of them."

**Read two aloud** — the GPU row and the TTL row.

> "The GPUs are counters. The scheduler really does ration them and really does
> refuse to overcommit — that part is genuine, and it is what the rehearsal proves.
> But nothing computes on a GPU here, so any speed you saw means nothing.
>
> And the expiry: the countdown is real, the enforcement is not. It is marked
> *partly real* rather than fake, because the distinction matters — the clock
> works, nothing acts on it yet."

> "Every one of these is a row in `DEVIATIONS.md`, which is the migration
> checklist. The architecture does not change when we move to the real cluster —
> the substitutions do."

---

## Afterwards

```bash
# remove the project you created during the demo
# (through the UI: Settings → type reef-demo → Remove this project)
```

Leave `jdi` and `demo-cv` running — they are what the next walkthrough starts from.

---

## If something goes wrong mid-demo

| Symptom | Cause | Fix |
|---|---|---|
| Everything Pending, nothing schedules | the machine restarted and wiped the fake GPU capacity | `scripts/fake-gpus.sh` |
| Playground returns "data path is not ready" | the port-forward died | it restarts itself within ~10s; check the top of `/api/health` |
| Deploy button does nothing | a run is already in flight — the second is queued | wait, or point at it as a feature: one at a time on a laptop |
| A step fails | genuinely useful — show the log, press **Retry step** | it resumes from the failed step |
