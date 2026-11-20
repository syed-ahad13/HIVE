// ===========================================================================
// HIVE STUDIO — THE PARAMETER RENDERER
// ===========================================================================
// One component per parameter TYPE, chosen from the template rather than from
// the parameter's name.
//
// ---------------------------------------------------------------------------
// WHY THIS FILE EXISTS
// ---------------------------------------------------------------------------
// The wizard was schema-driven in intent and hard-coded in fact: it knew the
// five parameter NAMES `inference-endpoint` happens to declare — projectName,
// model, availability, tier, durationDays — and rendered an input for each. That
// worked exactly as long as there was one template. `rag-stack` asks nine
// questions, two of them of kinds the wizard had never seen, and the choice was
// between a second branch in React for every template or making the renderer
// actually generic.
//
// So the dispatch below is on `param.type`, and the only thing this file knows
// about the catalogue is the shape of a Parameter (studio/backend/app/schema.py).
// A template that adds a question gets an input without anyone editing the
// console; a template that adds a new KIND of question needs one renderer here,
// which is the honest cost and is where it belongs.
//
// ---------------------------------------------------------------------------
// WHAT IS STILL NOT GENERIC, AND WHY THAT IS RIGHT
// ---------------------------------------------------------------------------
// Three parameters get bespoke presentation, and each is bespoke for a reason
// that is about the DECISION rather than about the data type:
//
//   availability  rendered as two cards with consequences spelled out, because
//                 it is an archetype choice — a cost/latency trade a PI is
//                 making, not a dropdown.
//   tier          shows the ceiling it buys, live, as it changes.
//   model enums   name their interface in the option label — but that text
//                 comes from models.yaml's `displayName` ("… (chat)",
//                 "… (embeddings)"), not from this file, so a plain select is
//                 already informative and no hook is needed.
//
// Those live in Wizard.jsx as decorations AROUND the generic field, never as
// replacements for it. The rule this file holds to: presentation may be
// specialised, the input may not.
// ===========================================================================

import { useCallback, useRef, useState } from "react";

import { ApiError, upload } from "./api.js";

// ---------------------------------------------------------------------------
// SHARED SHELL
// ---------------------------------------------------------------------------

export function FieldError({ message }) {
  if (!message) return null;
  return (
    <span className="field__error" role="alert">
      {message}
    </span>
  );
}

/**
 * Label, control, error, help — in that order, for every parameter type.
 *
 * The order is fixed on purpose. An error that appears above its input is read
 * before the thing it is about; help text above the error competes with it. A
 * PI scanning a form should meet the question, the answer, what is wrong, then
 * why it is being asked.
 */
function Field({ param, id, error, children }) {
  return (
    <div className="field">
      <label htmlFor={id}>{param.label}</label>
      {children}
      <FieldError message={error} />
      {param.help && <small>{param.help}</small>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// STRING
// ---------------------------------------------------------------------------

function StringField({ param, value, onChange, onBlur, error, autoFocus }) {
  return (
    <Field param={param} id={param.name} error={error}>
      <input
        id={param.name}
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onBlur}
        autoFocus={autoFocus}
        autoComplete="off"
      />
    </Field>
  );
}

// ---------------------------------------------------------------------------
// INTEGER
// ---------------------------------------------------------------------------

/**
 * A bounded number. `min`/`max` come from the template, never from this file.
 *
 * The bounds are also stated in words underneath, because a browser's own
 * enforcement of `min`/`max` is silent until submit and invisible to anyone
 * using the keyboard — and because the template's bounds usually encode a real
 * constraint (topK's maximum is what the serving runtime's context window
 * allows) that is worth reading rather than bumping into.
 */
function IntegerField({ param, value, onChange, onBlur, error }) {
  const bounds =
    param.minimum != null && param.maximum != null
      ? `Between ${param.minimum} and ${param.maximum}.`
      : null;
  return (
    <Field param={param} id={param.name} error={error}>
      <input
        id={param.name}
        type="number"
        min={param.minimum}
        max={param.maximum}
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))}
        onBlur={onBlur}
      />
      {bounds && <small className="muted">{bounds}</small>}
    </Field>
  );
}

// ---------------------------------------------------------------------------
// ENUM
// ---------------------------------------------------------------------------

/**
 * A select, or a read-only disclosure when the template marks it immutable.
 *
 * IMMUTABLE IS NOT DISABLED. A disabled `<select>` with one option looks like a
 * control that is broken or not yet loaded; it also drops out of the tab order,
 * so a keyboard user cannot even read it. This renders the decision as a
 * statement — the answer, plus why there is no choice — and the value still goes
 * to the server, which refuses anything else (validation.py). The visual
 * treatment is a courtesy; the control is server-side.
 */
function EnumField({ param, value, onChange, onBlur, error }) {
  const options = param.values || [];

  if (param.immutable) {
    const chosen = options.find((o) => o.value === (value ?? param.default));
    return (
      <Field param={param} id={param.name} error={error}>
        <div className="readonly" id={param.name}>
          <span className="readonly__value">{chosen?.label || value || param.default}</span>
          <span className="readonly__note">Chosen for you</span>
        </div>
      </Field>
    );
  }

  return (
    <Field param={param} id={param.name} error={error}>
      <select
        id={param.name}
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onBlur}
      >
        <option value="">Choose…</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </Field>
  );
}

/**
 * The same enum, as cards — used when every option carries a `description`.
 *
 * THE TRIGGER IS DATA, NOT A NAME. An option that explains its consequence is an
 * option worth reading before choosing, and that is exactly when a dropdown is
 * the wrong control: it hides every alternative behind a click and shows the
 * trade-off for none of them. So an enum whose options all describe themselves
 * renders as radio cards, and one whose labels say everything stays a select.
 *
 * `availability` is the case this exists for — a PI picking between always-on
 * and on-demand is making a cost/latency decision, not naming a value — and it
 * gets the treatment because the catalogue describes its options, not because
 * this file knows what "availability" means.
 */
function ChoiceCards({ param, value, onChange, error }) {
  const options = param.values || [];
  return (
    <div className="field">
      <label>{param.label}</label>
      <FieldError message={error} />
      <div className="choices">
        {options.map((o) => (
          <label
            key={o.value}
            className={`choice${value === o.value ? " choice--on" : ""}`}
          >
            <input
              type="radio"
              name={param.name}
              value={o.value}
              checked={value === o.value}
              onChange={(e) => onChange(e.target.value)}
            />
            <span>
              <b>{o.label}</b>
              <small>{o.description}</small>
            </span>
          </label>
        ))}
      </div>
      {param.help && <small>{param.help}</small>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// MULTISELECT
// ---------------------------------------------------------------------------

/**
 * Several of a set, IN THE ORDER THEY WERE PICKED.
 *
 * Not a `<select multiple>`, deliberately. That control returns its options in
 * DOM order, not click order — and for this parameter the order is the answer:
 * batch-inference feeds the chosen columns to a classic model as a feature
 * vector, so `[petal, sepal]` is a different question from `[sepal, petal]`.
 * A native multi-select cannot express that, and worse, it looks as though it
 * can.
 *
 * So: click to add (appending), click again to remove, with the position shown
 * on each chosen chip. Nothing is pre-selected — a PI confirming four columns
 * they did not choose is how the wrong feature order ships.
 *
 * `options` normally come from the template's `values`. When it declares none —
 * as batch-inference does, because the columns come from the PI's own uploaded
 * file — the caller passes them in.
 */
function MultiSelectField({ param, value, onChange, error, options }) {
  const chosen = Array.isArray(value) ? value : [];
  const available = options && options.length ? options : (param.values || []).map((v) => v.value);

  function toggle(name) {
    onChange(chosen.includes(name) ? chosen.filter((c) => c !== name) : [...chosen, name]);
  }

  // NOTHING TO OFFER YET. Reached when the catalogue declared no `values` — the
  // choices come from the PI's own file — and no file has been read. The
  // message names the screen rather than saying "above", because the file is
  // asked for on an EARLIER screen than the columns are, and a PI told to look
  // up found nothing there.
  if (!available.length) {
    return (
      <Field param={param} id={param.name} error={error}>
        <p className="small muted" id={param.name}>
          We could not read any columns yet. Go back and add your file, or choose
          a model — we use a sample file to stand in, and which one depends on
          the model.
        </p>
      </Field>
    );
  }

  return (
    <Field param={param} id={param.name} error={error}>
      <div className="multi" id={param.name} role="group" aria-label={param.label}>
        {available.map((name) => {
          const at = chosen.indexOf(name);
          return (
            <button
              type="button"
              key={name}
              className={`multi__opt${at >= 0 ? " multi__opt--on" : ""}`}
              onClick={() => toggle(name)}
              aria-pressed={at >= 0}
            >
              {at >= 0 && <span className="multi__n">{at + 1}</span>}
              {name}
            </button>
          );
        })}
      </div>
      {chosen.length > 1 && (
        <small className="muted">
          Used in this order: {chosen.join(", ")}. Click a chosen column to remove it.
        </small>
      )}
    </Field>
  );
}

// ---------------------------------------------------------------------------
// BOOLEAN
// ---------------------------------------------------------------------------

function BooleanField({ param, value, onChange, error }) {
  return (
    <Field param={param} id={param.name} error={error}>
      <label className="checkline">
        <input
          id={param.name}
          type="checkbox"
          checked={Boolean(value ?? param.default)}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span>{param.label}</span>
      </label>
    </Field>
  );
}

// ---------------------------------------------------------------------------
// FIXED
// ---------------------------------------------------------------------------

/** Not an input. A decision the platform already made, shown so it is not a surprise. */
function FixedField({ param }) {
  return (
    <Field param={param} id={param.name}>
      <div className="readonly" id={param.name}>
        <span className="readonly__value">{String(param.value)}</span>
        <span className="readonly__note">Chosen for you</span>
      </div>
    </Field>
  );
}

// ---------------------------------------------------------------------------
// UPLOAD
// ---------------------------------------------------------------------------

// FALLBACKS ONLY. Every `upload` parameter must declare `accept`, `maxFiles`
// and `maxBytes` — the catalogue loader refuses a template that does not — so
// these are reached only if a parameter somehow arrives without them. They are
// the SAFE direction: the smallest ceiling and the narrowest type list, so a
// missing declaration refuses too much rather than accepting too much.
//
// These used to be the real values, hard-coded, and that was the bug: the first
// template to exist set the rules for every later one. A batch dataset field
// advertised itself as "Markdown or plain text", and the file picker would not
// show a .csv at all.
const FALLBACK_ACCEPT = [".txt"];
const FALLBACK_MAX_FILES = 1;
const FALLBACK_MAX_BYTES = 5 * 1024 * 1024;

/** Human list: [".csv", ".jsonl"] -> ".csv or .jsonl" */
function humanExtensions(accept) {
  if (accept.length === 1) return accept[0];
  return `${accept.slice(0, -1).join(", ")} or ${accept[accept.length - 1]}`;
}

export function humanBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * A drop zone that uploads on selection and keeps the returned id in form state.
 *
 * ---------------------------------------------------------------------------
 * THE VALUE IN THE FORM IS AN ID, NOT THE FILES
 * ---------------------------------------------------------------------------
 * Files go to POST /api/uploads the moment they are chosen, and what lands in
 * `values.corpusUploadId` is the id that came back. So the deploy request stays
 * small JSON that can be logged, stored as the record of intent, and shown back
 * on the review screen — while the PI only ever sees their own filenames.
 *
 * The manifest displayed below the zone is THE SERVER'S, not the browser's list
 * of selected files. Those can differ — a file can be rejected, or renamed by
 * sanitisation — and showing the local list would tell a PI their corpus
 * contains something it does not.
 *
 * ---------------------------------------------------------------------------
 * REMOVING A FILE RE-UPLOADS THE REST
 * ---------------------------------------------------------------------------
 * There is no DELETE on an upload, deliberately: an id names a set of documents
 * that a deployment may already point at, so mutating one in place would change
 * what an existing project indexes. Removing a file here therefore mints a NEW
 * id from the files that remain, and the old staging directory is simply never
 * referenced. That costs one more upload of a few megabytes and keeps every id
 * meaning exactly one immutable set.
 */
function UploadField({ param, value, onChange, error }) {
  // FROM THE TEMPLATE, not from this file. See FALLBACK_* above for why.
  const accept = param.accept && param.accept.length ? param.accept : FALLBACK_ACCEPT;
  const maxFiles = param.maxFiles ?? FALLBACK_MAX_FILES;
  const maxBytes = param.maxBytes ?? FALLBACK_MAX_BYTES;
  const [manifest, setManifest] = useState(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState(null);
  const [dragging, setDragging] = useState(false);
  // The browser File objects behind the current manifest, kept only so a
  // removal can re-upload what remains without asking the PI to pick again.
  const held = useRef([]);
  const input = useRef(null);

  const send = useCallback(
    async (files) => {
      setFailure(null);

      if (files.length === 0) {
        held.current = [];
        setManifest(null);
        onChange("");
        return;
      }
      // Checked here so the obvious mistakes are caught before a 5MB round trip.
      // The server checks all of this again and its answer is the one that
      // counts — these messages exist to be faster, not to be the control.
      if (files.length > maxFiles) {
        setFailure(
          maxFiles === 1
            ? `This takes one file, and you added ${files.length}.`
            : `That is ${files.length} files. Add at most ${maxFiles}.`,
        );
        return;
      }
      // CHECKED HERE TOO, not only by the picker's `accept` filter. That filter
      // is a convenience the browser applies to the dialog; it does nothing for
      // a file dropped onto the zone, which is the path most people use.
      const wrongType = files.filter(
        (f) => !accept.some((ext) => f.name.toLowerCase().endsWith(ext)),
      );
      if (wrongType.length) {
        setFailure(
          `This takes ${humanExtensions(accept)}. ` +
            `${wrongType.map((f) => f.name).join(", ")} ` +
            `${wrongType.length === 1 ? "is not" : "are not"}.`,
        );
        return;
      }
      const total = files.reduce((sum, f) => sum + f.size, 0);
      if (total > maxBytes) {
        setFailure(
          `Those come to ${humanBytes(total)}. Add at most ${humanBytes(maxBytes)}.`,
        );
        return;
      }

      setBusy(true);
      try {
        const form = new FormData();
        for (const f of files) form.append("files", f);
        const body = await upload("/api/uploads", form);
        held.current = files;
        setManifest(body);
        onChange(body.uploadId);
      } catch (err) {
        // The upload failed, so the form must not keep pointing at whatever it
        // pointed at before — a stale id would deploy a corpus the PI believes
        // they replaced.
        held.current = [];
        setManifest(null);
        onChange("");
        setFailure(err instanceof ApiError ? err.message : String(err));
      } finally {
        setBusy(false);
      }
    },
    [onChange],
  );

  function onDrop(e) {
    e.preventDefault();
    setDragging(false);
    if (busy) return;
    send([...(e.dataTransfer?.files || [])]);
  }

  function removeAt(index) {
    send(held.current.filter((_, i) => i !== index));
  }

  const files = manifest?.files || [];

  return (
    <Field param={param} id={param.name} error={error}>
      <div
        className={`drop${dragging ? " drop--over" : ""}${busy ? " drop--busy" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => !busy && input.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            if (!busy) input.current?.click();
          }
        }}
        aria-busy={busy}
        aria-describedby={`${param.name}-fallback`}
      >
        <input
          ref={input}
          id={param.name}
          type="file"
          multiple={maxFiles > 1}
          accept={accept.join(",")}
          hidden
          onChange={(e) => send([...(e.target.files || [])])}
        />
        {busy ? (
          <>
            <b>Adding your {maxFiles === 1 ? "file" : "documents"}…</b>
            <span className="small muted">Copying to Studio. This is quick.</span>
          </>
        ) : (
          <>
            <b>
              {maxFiles === 1
                ? "Drop your file here, or choose one"
                : "Drop your documents here, or choose files"}
            </b>
            {/* THE SENTENCE COMES FROM THE TEMPLATE TOO. It used to promise
                "Markdown or plain text" to everyone, including the person
                uploading a CSV that the endpoint would have accepted. */}
            <span className="small muted">
              {humanExtensions(accept)}
              {maxFiles === 1
                ? `, up to ${humanBytes(maxBytes)}.`
                : `. Up to ${maxFiles} files, ${humanBytes(maxBytes)} in total.`}
            </span>
          </>
        )}
      </div>

      {failure && (
        <div className="state state--error" role="alert">
          <div className="state__title">Those documents were not added</div>
          <p className="small">{failure}</p>
          <p className="small muted">
            Nothing was kept. Fix the files and add them again.
          </p>
        </div>
      )}

      {files.length > 0 && (
        <div className="filelist">
          <div className="filelist__head">
            <span>
              <b>{files.length}</b> document{files.length === 1 ? "" : "s"} ready to index
            </span>
            <span className="small muted">{humanBytes(manifest.totalBytes)}</span>
          </div>
          {files.map((f, i) => (
            <div className="filerow" key={f.name}>
              <span className="filerow__name">{f.name}</span>
              <span className="filerow__size small muted">{humanBytes(f.bytes)}</span>
              <button
                type="button"
                className="filerow__x"
                onClick={() => removeAt(i)}
                disabled={busy}
                aria-label={`Remove ${f.name}`}
                title={`Remove ${f.name}`}
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}

      {/* THE FALLBACK, STATED UP FRONT rather than discovered afterwards. A PI
          who has nothing to hand can still deploy and see the thing work — and
          they are told exactly what will be indexed instead, so nothing about
          the result is a surprise. */}
      {files.length === 0 && !busy && (
        <p className="small muted" id={`${param.name}-fallback`}>
          No files? We&rsquo;ll index the bundled HIVE documents so you can try it
          immediately.
        </p>
      )}
    </Field>
  );
}

// ---------------------------------------------------------------------------
// THE DISPATCH
// ---------------------------------------------------------------------------

const RENDERERS = {
  string: StringField,
  integer: IntegerField,
  enum: EnumField,
  boolean: BooleanField,
  fixed: FixedField,
  upload: UploadField,
  multiselect: MultiSelectField,
};

/**
 * Render one parameter. Unknown types are shown as a refusal, not skipped.
 *
 * A type this console cannot render is a template asking for something the
 * console does not support, and quietly omitting the field would produce a form
 * that submits without an answer the template requires — a deployment that
 * fails at the server for a reason the PI never saw a question about.
 */
export function ParameterField({ param, value, onChange, onBlur, error, autoFocus, options }) {
  let Renderer = RENDERERS[param.type];

  // See ChoiceCards: an enum whose every option explains its consequence is one
  // a PI should compare rather than pick blind. Immutable enums are excluded —
  // there is nothing to compare, and EnumField already renders those as a plain
  // statement of the decision.
  if (
    param.type === "enum" &&
    !param.immutable &&
    (param.values || []).length > 0 &&
    (param.values || []).every((o) => o.description)
  ) {
    Renderer = ChoiceCards;
  }

  if (!Renderer) {
    return (
      <div className="field">
        <label>{param.label}</label>
        <div className="state state--error">
          <div className="state__title">This console cannot ask this question yet</div>
          <p className="small">
            The template wants a <code>{param.type}</code> answer, which Studio does
            not know how to collect.
          </p>
        </div>
      </div>
    );
  }
  return (
    <Renderer
      param={param}
      value={value}
      onChange={onChange}
      onBlur={onBlur}
      error={error}
      autoFocus={autoFocus}
      options={options}
    />
  );
}

/** The parameters belonging to one wizard screen, in the template's own order. */
export function paramsInGroup(params, group) {
  return params.filter((p) => (p.group || "resources") === group);
}
