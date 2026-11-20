// ===========================================================================
// HIVE STUDIO — CATALOGUE
// ===========================================================================
// The first screen a PI sees: what can I build?
//
// Grouped by the template's own `category`. The order below is the order a
// research project actually moves through — you serve a model, you build
// something on top of it, you train your own — rather than alphabetical.
//
// ANY CATEGORY NOT LISTED HERE IS STILL RENDERED, in a trailing group. The
// catalogue is data the platform team commits (CLAUDE.md §6); adding a category
// must not require a frontend change, and a template silently vanishing from the
// console because nobody updated this array would be the worst possible failure
// mode for a governance boundary.
// ===========================================================================

import { Link } from "react-router-dom";

import { EmptyState, Fetched, MaturityBadge, useFetch } from "../components.jsx";

const CATEGORY_ORDER = ["Serve", "Build", "Train"];

const CATEGORY_BLURB = {
  Serve: "Run a model and get a web address you can send requests to.",
  Build: "Put something on top of a model — search over your own documents, for example.",
  Train: "Adapt a model to your own data, then serve the result.",
};

function groupByCategory(templates) {
  const groups = new Map();
  for (const t of templates) {
    if (!groups.has(t.category)) groups.set(t.category, []);
    groups.get(t.category).push(t);
  }
  const known = CATEGORY_ORDER.filter((c) => groups.has(c));
  const extra = [...groups.keys()].filter((c) => !CATEGORY_ORDER.includes(c)).sort();
  return [...known, ...extra].map((name) => ({ name, items: groups.get(name) }));
}

function TemplateCard({ template }) {
  const available = template.maturity === "available";
  return (
    <article className={`card card--interactive${available ? "" : " card--muted"}`}>
      <div className="card__title">
        <h3>{template.name}</h3>
        <MaturityBadge maturity={template.maturity} />
      </div>

      <p className="small muted">{template.description}</p>

      <div className="card__foot">
        {available ? (
          <>
            <Link className="btn btn--primary btn--sm" to={`/templates/${template.id}`}>
              Deploy
            </Link>
            <Link className="btn btn--ghost btn--sm" to={`/templates/${template.id}`}>
              Details
            </Link>
          </>
        ) : (
          <>
            {/* No Deploy button at all, rather than a disabled one. A greyed-out
                button invites people to hunt for the thing that would enable it;
                the honest answer is that there is nothing to enable yet. */}
            <Link className="btn btn--ghost btn--sm" to={`/templates/${template.id}`}>
              Read more
            </Link>
            <span className="small muted">On the roadmap</span>
          </>
        )}
      </div>
    </article>
  );
}

export default function Catalog() {
  const state = useFetch("/api/templates", "Loading catalog…");

  return (
    <div className="page">
      <div className="page__head">
        <h1>What would you like to build?</h1>
        <p>
          Pick a starting point. You will be asked a few questions, and then Studio sets
          everything up for you — you do not need to know Kubernetes.
        </p>
      </div>

      <Fetched state={state}>
        {(templates) => {
          if (!templates || templates.length === 0) {
            return (
              <EmptyState title="The catalog is empty">
                <p className="small muted">
                  No templates were found in <code>templates/catalog/</code>. That is
                  unusual — the backend refuses to start with an empty catalog, so this
                  most likely means it is serving a different directory.
                </p>
              </EmptyState>
            );
          }

          return groupByCategory(templates).map((group) => (
            <section className="section" key={group.name}>
              <div className="section__head">
                <h2>{group.name}</h2>
                <span className="section__count">
                  {CATEGORY_BLURB[group.name] ||
                    `${group.items.length} template${group.items.length === 1 ? "" : "s"}`}
                </span>
              </div>
              <div className="grid">
                {group.items.map((t) => (
                  <TemplateCard key={t.id} template={t} />
                ))}
              </div>
            </section>
          ));
        }}
      </Fetched>
    </div>
  );
}
