// ===========================================================================
// HIVE STUDIO — APP SHELL AND ROUTES
// ===========================================================================
// The chrome every page sits inside: top bar, left nav, and the permanent amber
// banner. Also the sign-in gate, which is a pretence and says so.
// ===========================================================================

import { useCallback, useState } from "react";
import {
  BrowserRouter,
  Link,
  NavLink,
  Navigate,
  Route,
  Routes,
} from "react-router-dom";

import { AmberBanner, useExecutorMode } from "./components.jsx";
import { DEVIATIONS } from "./deviations.js";
import { ROLES, clearIdentity, loadIdentity, roleLabel, saveIdentity } from "./identity.js";
import Catalog from "./pages/Catalog.jsx";
import DeploymentDetail from "./pages/DeploymentDetail.jsx";
import DeploymentsList from "./pages/DeploymentsList.jsx";
import TemplateDetail from "./pages/TemplateDetail.jsx";
import Wizard from "./pages/Wizard.jsx";

// ---------------------------------------------------------------------------
// SIGN IN
// ---------------------------------------------------------------------------

function SignIn({ onSignIn }) {
  const [name, setName] = useState("");
  const [role, setRole] = useState("pi");

  function submit(event) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    onSignIn({ name: trimmed, role });
  }

  return (
    <div className="signin">
      <form className="signin__box" onSubmit={submit}>
        <div className="signin__brand">
          HIVE <span>Studio</span>
        </div>

        <AmberBanner>
          <strong>Sign-in is mocked.</strong> Type any name — nothing is checked and
          nothing is granted. On the real platform you sign in with your KAUST
          account through Keycloak, and your role decides what you may do.
        </AmberBanner>

        <div className="card">
          <div className="field">
            <label htmlFor="signin-name">Your name</label>
            <input
              id="signin-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Layla Haddad"
              autoFocus
            />
            <small>Shown in the corner so you know whose view you are looking at.</small>
          </div>

          <div className="field">
            <label htmlFor="signin-role">Your role</label>
            <select id="signin-role" value={role} onChange={(e) => setRole(e.target.value)}>
              {ROLES.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
            <small>
              A label only, for now. It does not change what you can see or do in this
              local build.
            </small>
          </div>

          <button type="submit" className="btn btn--primary" disabled={!name.trim()}>
            Continue
          </button>
        </div>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// CHROME
// ---------------------------------------------------------------------------

function IdentityChip({ identity, onSignOut }) {
  return (
    <div className="identity">
      <div className="identity__chip">
        <span>{identity.name}</span>
        <span className="identity__role">{roleLabel(identity.role)}</span>
      </div>
      <button type="button" className="identity__signout" onClick={onSignOut}>
        Sign out
      </button>
    </div>
  );
}

/**
 * The honesty panel. Every local shortcut, in one list.
 *
 * Reachable from the top bar on every screen rather than buried in a footer,
 * because it is the answer to "wait, is that real?" — the question an architect
 * asks halfway through a demo, and the one moment when hunting for the answer
 * costs the most.
 */
function DeviationsModal({ onClose }) {
  return (
    <div className="modal" role="dialog" aria-modal="true" aria-labelledby="dev-title">
      <div className="modal__box modal__box--wide">
        <div className="devhead">
          <h2 id="dev-title">What this local build fakes</h2>
          <button type="button" className="btn btn--ghost btn--sm" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="small muted">
          Everything below works differently on the real platform. The architecture
          is the same in both — these are the substitutions that let it run on one
          laptop.
        </p>

        <div className="devlist">
          {DEVIATIONS.map((d) => (
            <div className="dev" key={d.id}>
              <div className="dev__area">
                {d.area}
                {d.partial && <span className="badge badge--green">partly real</span>}
              </div>
              <div className="dev__cols">
                <div>
                  <span className="dev__lbl">Here</span>
                  <p className="small">{d.local}</p>
                </div>
                <div>
                  <span className="dev__lbl">On the platform</span>
                  <p className="small">{d.production}</p>
                </div>
              </div>
              <p className="small muted dev__why">{d.why}</p>
            </div>
          ))}
        </div>

        <p className="small muted">
          The same list, written for whoever does the migration, is in{" "}
          <code>DEVIATIONS.md</code>.
        </p>
      </div>
    </div>
  );
}

function Shell({ identity, onSignOut }) {
  const [showDeviations, setShowDeviations] = useState(false);
  // `null` until the backend answers; neither mode-specific line renders until
  // then, so the bar never flashes the wrong claim.
  const executor = useExecutorMode();
  return (
    <div className="app">
      <header className="topbar">
        <Link to="/catalog" className="wordmark">
          HIVE <span>Studio</span>
        </Link>
        <div className="topbar__right">
          <button
            type="button"
            className="topbar__notes"
            onClick={() => setShowDeviations(true)}
          >
            ▲ Local build notes
          </button>
          <IdentityChip identity={identity} onSignOut={onSignOut} />
        </div>
      </header>

      {showDeviations && <DeviationsModal onClose={() => setShowDeviations(false)} />}

      {/* PERMANENT, on every screen, never dismissible. Invariant (e): a demo
          that hides its scaffolding teaches the audience something false. This
          is the identity row of DEVIATIONS.md, stated where it is relevant. */}
      <AmberBanner bar>
        <strong>Local build</strong> — sign-in is mocked; production authenticates via
        KAUST AD (Keycloak).{" "}
        <button type="button" className="linkbtn" onClick={() => setShowDeviations(true)}>
          See everything this build fakes
        </button>
        {/* WHICH MODE, ON EVERY SCREEN. The single most consequential fact about
            this console is whether pressing Deploy creates a real namespace, a
            real vCluster and a real served model — and until now the only way to
            find out was to read the terminal the backend was started in.

            It rides the existing amber bar rather than adding a second one:
            `real` is NOT itself a deviation and must not be dressed as one, so
            it is stated as plain emphasis while the amber around it keeps
            meaning what it meant. In mock the wording is stronger, because that
            IS the deviation. */}
        {executor === "real" && (
          <>
            {" · "}
            <strong>Deploy creates real infrastructure on the cluster.</strong>
          </>
        )}
        {executor === "mock" && (
          <>
            {" · "}
            <strong>Rehearsal mode — Deploy creates nothing.</strong>
          </>
        )}
      </AmberBanner>

      <div className="shell">
        <nav className="nav">
          <div className="nav__label">Build</div>
          <NavLink
            to="/catalog"
            className={({ isActive }) =>
              `nav__link${isActive ? " nav__link--active" : ""}`
            }
          >
            Catalog
          </NavLink>
          <NavLink
            to="/deployments"
            className={({ isActive }) =>
              `nav__link${isActive ? " nav__link--active" : ""}`
            }
          >
            My projects
          </NavLink>
        </nav>

        <main className="main">
          <Routes>
            <Route path="/" element={<Navigate to="/catalog" replace />} />
            <Route path="/catalog" element={<Catalog />} />
            <Route path="/templates/:id" element={<TemplateDetail />} />
            <Route path="/deployments" element={<DeploymentsList />} />
            <Route path="/deployments/:id" element={<DeploymentDetail />} />
            <Route path="/deploy/:id" element={<Wizard />} />
            {/* Anything unrecognised goes to the catalogue rather than to a
                dead end. There is nothing a PI can usefully do on a 404 page. */}
            <Route path="*" element={<Navigate to="/catalog" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}

export default function App() {
  // Read once at mount. localStorage is not reactive, and this is the only place
  // that holds the answer, so a single useState is the whole state model.
  const [identity, setIdentity] = useState(() => loadIdentity());

  const signIn = useCallback((value) => {
    saveIdentity(value);
    setIdentity(value);
  }, []);

  const signOut = useCallback(() => {
    clearIdentity();
    setIdentity(null);
  }, []);

  if (!identity) return <SignIn onSignIn={signIn} />;

  return (
    <BrowserRouter>
      <Shell identity={identity} onSignOut={signOut} />
    </BrowserRouter>
  );
}
