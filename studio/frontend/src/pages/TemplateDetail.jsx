// ===========================================================================
// HIVE STUDIO — TEMPLATE DETAIL
// ===========================================================================
// The page between "that looks interesting" and "fill in the form". Its job is
// pre-disclosure: everything a PI should know BEFORE they start, so nothing that
// happens during provisioning is a surprise.
//
// Four parts, in the order a person asks the questions:
//   1. What you get
//   2. What we will ask you           (the wizard's questions, explained)
//   3. What happens when you deploy   (this template's steps, by their human
//                                       names — the list comes from the template,
//                                       so a RAG project discloses eleven and an
//                                       inference project six)
//   4. Continue
//
// ALL COPY IS USER-SIDE per invariant (e). "Your private cluster (vCluster)" —
// the parenthetical keeps the real term discoverable for a PI who wants it,
// without making it the price of entry.
// ===========================================================================

import { Link, useParams } from "react-router-dom";

import {
  AmberBanner,
  Fetched,
  MaturityBadge,
  useExecutorMode, useFetch,
} from "../components.jsx";
// The steps are described in ONE place (src/steps.js) so the wording a PI
// reads here, before deciding, is identical to the wording on the progress view
// while it happens.
import { STEPS as STEP_COPY } from "../steps.js";
import { TIERS, TIER_ROWS } from "../tiers.js";



// ---------------------------------------------------------------------------

function ParameterRow({ param }) {
  const options = param.values || [];
  return (
    <div className="params__row">
      <b>{param.label}</b>
      {param.help && <span>{param.help}</span>}

      {options.length > 0 && (
        <div className="params__opts">
          {options.map((o) => (
            <span className="badge badge--navy" key={o.value}>
              {o.label}
            </span>
          ))}
        </div>
      )}

      {param.type === "integer" && (
        <span>
          Between {param.minimum} and {param.maximum}
          {param.default != null ? `, usually ${param.default}` : ""}.
        </span>
      )}
    </div>
  );
}

export default function TemplateDetail() {
  const { id } = useParams();
  const state = useFetch(`/api/templates/${id}`, "Loading template…");
  // `null` until the backend answers. Nothing mode-specific renders until then —
  // see useExecutorMode for why guessing would be worse than waiting.
  const mode = useExecutorMode();

  return (
    <div className="page">
      <Fetched state={state}>
        {(tpl) => {
          const available = tpl.metadata.maturity === "available";
          const params = tpl.spec.parameters || [];
          // THE STEPS COME FROM THE TEMPLATE, computed by the backend from this
          // template's own `resources` (GET /api/templates/{id} -> `steps`).
          //
          // This used to read a hard-coded six-entry array, which was right for
          // the one template that existed and silently wrong the moment a second
          // one had eleven steps in a different order. A page that pre-discloses
          // "what happens when you deploy" and then discloses somebody else's
          // pipeline is worse than not disclosing at all.
          const steps = (tpl.steps || []).filter((s) => STEP_COPY[s]);

          return (
            <>
              <div className="page__head">
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <h1>{tpl.metadata.displayName}</h1>
                  <MaturityBadge maturity={tpl.metadata.maturity} />
                </div>
                <p>{tpl.metadata.description}</p>
              </div>

              {!available && (
                <AmberBanner>
                  <strong>Not available yet.</strong> This is on the roadmap, so you can
                  read what it will do but you cannot start it. Nothing below is
                  final — the questions and the setup steps will change before it
                  ships.
                </AmberBanner>
              )}

              {/* ---------------- 1. WHAT YOU GET ---------------- */}
              {available && (
                <section className="section">
                  <div className="section__head">
                    <h2>What you get</h2>
                  </div>

                  <div className="grid">
                    <div className="card">
                      <h3>A web address for your model</h3>
                      <p className="small muted">
                        Send it requests from a script, a notebook, or your own
                        application. You can also try it here in the browser.
                      </p>
                    </div>
                    <div className="card">
                      <h3>Your private cluster (vCluster)</h3>
                      <p className="small muted">
                        A real Kubernetes control plane that is yours. You get admin
                        access, so you can inspect and change everything inside your
                        own project.
                      </p>
                    </div>
                    <div className="card">
                      <h3>A fixed end date</h3>
                      <p className="small muted">
                        You choose how long you need it. Your project is removed
                        automatically when this runs out, so nothing is left holding
                        hardware after you have finished.
                      </p>
                    </div>
                  </div>

                  <h3>How much you can use at once</h3>
                  <div className="tablewrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Size</th>
                          {TIER_ROWS.map(([label]) => (
                            <th key={label}>{label}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(TIERS).map(([id, t]) => (
                          <tr key={id}>
                            <td>
                              <b>{t.label}</b>
                            </td>
                            {TIER_ROWS.map(([label, key]) => (
                              <td className="num" key={key}>
                                {t[key]}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>

                  <AmberBanner>
                    <strong>Both sizes are the same on this laptop.</strong> The GPUs are
                    simulated, not real hardware — enough to prove that the sharing and
                    the limits work, but nothing is actually computed on a GPU. On the
                    real cluster the two sizes differ and the GPUs are real.
                  </AmberBanner>
                </section>
              )}

              {/* ---------------- 2. WHAT WE WILL ASK ---------------- */}
              <section className="section">
                <div className="section__head">
                  <h2>What we will ask you</h2>
                  <span className="section__count">
                    {params.length} question{params.length === 1 ? "" : "s"}
                  </span>
                </div>
                <div className="params">
                  {params.map((p) => (
                    <ParameterRow key={p.name} param={p} />
                  ))}
                </div>
              </section>

              {/* ---------------- 3. WHAT HAPPENS ---------------- */}
              {available && (
                <section className="section">
                  <div className="section__head">
                    <h2>What happens when you deploy</h2>
                    <span className="section__count">
                      {mode === "mock"
                        ? "about a minute in this rehearsal"
                        : mode === "real"
                          ? "several minutes — most of it fetching the model"
                          : `${steps.length} steps`}
                    </span>
                  </div>

                  <div className="steps">
                    {steps.map((stepId, i) => (
                      <div className="steps__row" key={stepId}>
                        <div className="steps__n">{i + 1}</div>
                        <div className="steps__body">
                          <b>{STEP_COPY[stepId].label}</b>
                          <span>{STEP_COPY[stepId].detail}</span>
                        </div>
                      </div>
                    ))}
                  </div>

                  {/* AMBER ONLY WHEN IT IS TRUE. The banner said "nothing is
                      really created yet" unconditionally, which was right for
                      the whole life of the build and became a lie the day the
                      real executor started provisioning. Amber means "this is
                      not how production works" (invariant (e)); spending it on
                      a claim that is false is worse than not spending it, because
                      it teaches a reader to discount the colour.

                      In real mode the honest note is the opposite one: this
                      DOES create infrastructure, and it will still be there when
                      you close the tab. */}
                  {mode === "mock" && (
                    <AmberBanner>
                      <strong>Nothing is really created.</strong> This console is in
                      rehearsal mode, so the setup runs in about a minute and hands
                      back a pretend web address. On the real cluster these same{" "}
                      {steps.length} steps take several minutes, most of it fetching
                      your model.
                    </AmberBanner>
                  )}
                  {mode === "real" && (
                    <p className="small muted">
                      These {steps.length} steps create real infrastructure on the
                      cluster — a namespace with a quota you cannot exceed, your own
                      Kubernetes control plane, and a served model. It takes several
                      minutes, most of it fetching your model, and everything it
                      creates stays until the project is removed or its time runs out.
                    </p>
                  )}
                </section>
              )}

              {/* ---------------- 4. CONTINUE ---------------- */}
              <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                {available ? (
                  <Link className="btn btn--primary" to={`/deploy/${tpl.metadata.id}`}>
                    Continue — configure
                  </Link>
                ) : (
                  <span className="small muted">
                    This template is on the roadmap and cannot be started yet.
                  </span>
                )}
                <Link className="btn btn--ghost" to="/catalog">
                  Back to catalog
                </Link>
              </div>
            </>
          );
        }}
      </Fetched>
    </div>
  );
}
