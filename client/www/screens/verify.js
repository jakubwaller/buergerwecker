// The one screen while the server waits for this device's setup push
// (push.js, "Verification"). It goes away by itself: app.js posts the code
// from the push and re-renders once the device is verified, and on resume it
// re-reads GET /device in case `verified` flipped while the app was away.
import { t } from "../i18n.js";
import { h, errorMessage } from "../ui.js";
import * as push from "../push.js";

// The server allows one resend a minute; a 429 says how long to wait, and
// the button stays off for that long. Kept across re-renders.
let blockedUntil = 0;
let lastProblem = null; // "invalid" after a wrong or expired code

export function noteInvalidCode() {
  lastProblem = "invalid";
}

export function mount(el) {
  const status = h("p", { class: "muted small", role: "status" });
  if (lastProblem === "invalid") status.textContent = t("verify.invalid");
  const resend = h("button", { class: "btn btn-secondary" }, t("verify.resend"));
  let timer = null;
  const tick = () => {
    const wait = Math.ceil((blockedUntil - Date.now()) / 1000);
    resend.disabled = wait > 0;
    if (wait > 0) {
      resend.textContent = `${t("verify.resend")} (${wait})`;
      timer = setTimeout(tick, 1000);
    } else {
      resend.textContent = t("verify.resend");
      timer = null;
    }
  };
  resend.onclick = async () => {
    resend.disabled = true;
    try {
      const r = await push.resendVerification();
      // Already verified: the device change re-renders the app past this screen.
      if (r.verified) return;
      lastProblem = null;
      status.textContent = t("verify.resent");
      blockedUntil = Date.now() + 60_000;
    } catch (e) {
      if (e?.error === "rate_limited") {
        blockedUntil = Date.now() + (Number(e.retryAfter) || 60) * 1000;
        status.textContent = t("verify.wait", { s: Math.ceil((blockedUntil - Date.now()) / 1000) });
      } else {
        status.textContent = errorMessage(e);
      }
    }
    if (resend.isConnected && !timer) tick();
  };
  tick();
  el.append(
    h(
      "div",
      { class: "onboarding" },
      h("div", { class: "glyph pulse", "aria-hidden": "true" }),
      h("h1", null, t("verify.title")),
      h("p", null, t("verify.body")),
      h("p", { class: "muted" }, t("verify.hint")),
      h("div", { class: "stack" }, resend, status),
    ),
  );
}
