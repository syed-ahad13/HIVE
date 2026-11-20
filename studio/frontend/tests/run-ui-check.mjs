// ===========================================================================
// HIVE STUDIO — UI CHECK RUNNER
// ===========================================================================
// Sets up a document, then loads the compiled checks.
//
// THE ORDER IS THE WHOLE REASON THIS IS A SEPARATE FILE. react-dom reads
// `document` and `window` as it initialises, so the globals have to exist BEFORE
// it is imported. Static `import` statements are hoisted and would run first, so
// the bundle is brought in with a dynamic `import()` after the DOM is in place.
// ===========================================================================

import { JSDOM } from "jsdom";

const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  url: "http://localhost/",
  pretendToBeVisual: true,
});

globalThis.window = dom.window;
globalThis.document = dom.window.document;
// `navigator` is a getter-only property on modern Node's globalThis, so it
// cannot be assigned the way the others can — it has to be redefined. Node
// supplies its own `navigator`, and react-dom reads `navigator.userAgent`
// during hydration checks; leaving Node's in place works, but jsdom's is the
// one that matches the document, so the two cannot disagree about what
// environment this is.
Object.defineProperty(globalThis, "navigator", {
  value: dom.window.navigator,
  configurable: true,
  writable: true,
});
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.Node = dom.window.Node;
globalThis.MouseEvent = dom.window.MouseEvent;
// jsdom implements no layout, so it has no scrollIntoView — the chat and RAG
// playgrounds both call it to keep the newest turn in view. A no-op is the right
// stub: scrolling is not behaviour these checks make any claim about, and
// leaving it undefined would fail the mount for a reason that has nothing to do
// with the component.
dom.window.Element.prototype.scrollIntoView = function scrollIntoView() {};

globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

// React 19 asks for this before it will let `act` drive updates. It is a
// development-only flag and this is a development-only runner.
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

await import("../.ui-check/ui-check.js");
