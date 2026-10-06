// DOM helpers. Text from the server always goes in as text, never as HTML.
import { t } from "./i18n.js";
import { errorText } from "./api.js";

// h("p", { class: "muted", onclick: fn }, "text", child, [more]) → element.
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}

// replaceChildren() that, like h(), skips null and false instead of
// printing them.
export function fill(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

export function errorMessage(err) {
  const e = errorText(err);
  return e.text ?? t(e.key, e.vars);
}

export function loading() {
  return h("p", { class: "muted center" }, t("common.loading"));
}

// An error box with a retry button.
export function errorBox(err, retry) {
  return h(
    "div",
    { class: "notice notice-error", role: "alert" },
    h("p", null, errorMessage(err)),
    retry ? h("button", { class: "btn btn-secondary", onclick: retry }, t("common.retry")) : null,
  );
}

let toastTimer;
// A short line at the bottom that goes away on its own.
export function toast(text) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 4000);
}

let bannerTimer;
// The in-app banner for a notification that arrived in the foreground:
// title, body, and a tap that does what the system notification's tap does.
export function banner({ title, body, onTap }) {
  const el = document.getElementById("banner");
  if (!el) return;
  fill(
    el,
    h("strong", null, title || t("app.name")),
    body ? h("span", null, body) : null,
  );
  el.onclick = () => {
    el.hidden = true;
    onTap?.();
  };
  el.hidden = false;
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => (el.hidden = true), 8000);
}
