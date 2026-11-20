// ===========================================================================
// HIVE STUDIO — MOCK IDENTITY
// ===========================================================================
// THERE IS NO IDENTITY LAYER LOCALLY. This is one of the seeded rows in
// DEVIATIONS.md: production authenticates against KAUST AD through Keycloak and
// issues tokens; here a person types a name into a box and it is kept in
// localStorage.
//
// This module exists so that ALL of that pretence lives in one file. Nothing
// else in the console reads localStorage or reasons about who is signed in, so
// when Keycloak arrives there is exactly one place to replace — and one place to
// check that nothing has quietly started depending on the mock.
//
// Two things this deliberately does NOT do, because doing them would make a
// pretend control look like a real one:
//
//   * It grants nothing. The `role` is a label on a chip. It gates no route and
//     no button. A control that appears to authorise but does not is worse than
//     no control, because people plan around it.
//   * It never reaches the backend. The BFF has no concept of a user and no
//     endpoint here sends one. Every request is anonymous, exactly as it was
//     before this screen existed.
//
// The amber banner in the app chrome says so on every screen, permanently.
// ===========================================================================

const KEY = "hive.studio.identity";

export const ROLES = [
  { value: "pi", label: "Principal Investigator" },
  { value: "member", label: "Team member" },
];

export function roleLabel(value) {
  const found = ROLES.find((r) => r.value === value);
  return found ? found.label : value;
}

/** The signed-in person, or null. */
export function loadIdentity() {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    // Storage is user-writable and survives across versions of this code, so a
    // shape check is not paranoia — a malformed value would otherwise crash the
    // whole app on load, with no way to recover except clearing site data.
    if (!parsed || typeof parsed.name !== "string" || !parsed.name.trim()) return null;
    return { name: parsed.name, role: parsed.role === "member" ? "member" : "pi" };
  } catch {
    // Private windows and blocked site data throw on access rather than
    // returning null. Signed out is the correct answer in that case.
    return null;
  }
}

export function saveIdentity(identity) {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(identity));
  } catch {
    // Nothing to do and nothing worth interrupting the person for: they stay
    // signed in for this tab, and are asked again next time.
  }
}

export function clearIdentity() {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* see above */
  }
}
