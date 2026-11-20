// ===========================================================================
// HIVE STUDIO — API CLIENT
// ===========================================================================
// A thin wrapper over fetch. Deliberately small: no caching, no retries, no
// request dedupe, no state library. The BFF is on localhost and the console is
// a rehearsal console; anything cleverer would be machinery to maintain in
// exchange for latency that does not exist.
//
// ---------------------------------------------------------------------------
// RELATIVE PATHS ONLY
// ---------------------------------------------------------------------------
// Every call goes to `/api/...`, never to `http://localhost:8000/...`. The Vite
// dev server proxies it (vite.config.js), so the browser sees one origin. Nothing
// in src/ contains a hostname or a port, which is what lets this same code work
// when the console is served from the BFF itself later.
//
// ---------------------------------------------------------------------------
// ERRORS ARE OBJECTS, NOT STRINGS
// ---------------------------------------------------------------------------
// A failed call throws an ApiError carrying { status, body }, because the caller
// usually needs to branch on both. The wizard in particular needs the 400 body:
// the BFF answers a rejected submission with the FULL list of field errors, so
// each message can be shown beside the input it belongs to rather than as one
// line of text at the top of the form.
// ===========================================================================

export class ApiError extends Error {
  constructor(status, body, url) {
    super(ApiError.messageFrom(status, body, url));
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.url = url;
  }

  /** The most useful sentence available, in decreasing order of specificity. */
  static messageFrom(status, body, url) {
    if (body && typeof body === "object") {
      // FastAPI puts our own message in `detail`, which is either a string or,
      // for a validation rejection, an object holding the error list.
      if (typeof body.detail === "string") return body.detail;
      if (body.detail && Array.isArray(body.detail.errors)) {
        return body.detail.errors.map((e) => `${e.field}: ${e.message}`).join("; ");
      }
      // POST /api/uploads rejects with {message, files?} — one sentence naming
      // the rule and, where it helps, the files that broke it. Surfaced as-is
      // rather than reformatted: the backend already wrote it for a person.
      if (body.detail && typeof body.detail.message === "string") {
        return body.detail.message;
      }
    }
    if (status === 0) {
      // Nothing answered. Almost always the BFF is not running, so say that
      // instead of "Failed to fetch", which tells a PI nothing actionable.
      return "Could not reach the Studio backend. Is it running on port 8000?";
    }
    return `Request failed (HTTP ${status}) for ${url}`;
  }

  /** Field errors from a 400, or [] — so callers need not dig through `body`. */
  get fieldErrors() {
    const detail = this.body && this.body.detail;
    if (detail && Array.isArray(detail.errors)) return detail.errors;
    return [];
  }
}

async function request(method, path, payload) {
  let response;
  try {
    response = await fetch(path, {
      method,
      headers: payload === undefined ? undefined : { "Content-Type": "application/json" },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
  } catch (cause) {
    // fetch only rejects when the request never completed — the server is down,
    // DNS failed, the network vanished. Status 0 marks "no answer at all", which
    // is a different problem from any HTTP status and deserves its own message.
    throw new ApiError(0, null, path, { cause });
  }

  // 204 and friends have no body; asking for JSON would throw on empty text.
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      // A non-JSON body from a JSON API means something upstream answered
      // instead of the BFF — a proxy error page, usually. Keep the raw text so
      // the error state can show what actually arrived.
      body = { raw: text };
    }
  }

  if (!response.ok) throw new ApiError(response.status, body, path);
  return body;
}

/**
 * Multipart POST — the one call that does not send JSON.
 *
 * `Content-Type` is DELIBERATELY NOT SET. The browser has to write it itself
 * because it must include the multipart boundary it generated; setting
 * "multipart/form-data" by hand omits the boundary and the server rejects the
 * body as malformed. That is why this cannot go through `request()` above,
 * which sets a JSON content type for anything with a payload.
 *
 * Everything else is the same: relative path, ApiError on failure, parsed body
 * on success — so a caller handles upload errors exactly like any other.
 */
export async function upload(path, formData) {
  let response;
  try {
    response = await fetch(path, { method: "POST", body: formData });
  } catch (cause) {
    throw new ApiError(0, null, path, { cause });
  }
  const text = await response.text();
  let body = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = { raw: text };
    }
  }
  if (!response.ok) throw new ApiError(response.status, body, path);
  return body;
}

export const get = (path) => request("GET", path);
export const post = (path, payload) => request("POST", path, payload ?? {});
export const patch = (path, payload) => request("PATCH", path, payload ?? {});
export const del = (path) => request("DELETE", path);
