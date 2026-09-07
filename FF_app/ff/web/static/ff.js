/* FF_app client script — plain fetch, no frameworks, no CDN.
 *
 * Live bits:
 *  - snapshot freshness age ticker (OR-5 banner honesty);
 *  - login role -> scope dropdown toggling (disabled inputs don't submit,
 *    so exactly one scope field reaches the server);
 *  - POST /api/v1/actuals from the mechanic rows (single write-path OR-6);
 *  - POST /api/v1/replan from the banner button (single-flight; 409 shown);
 *  - POST /api/v1/excusals from the lead capture control (cause + notes;
 *    server enforces role/team scope and the notes-required causes).
 */
(function () {
  "use strict";

  function $(sel, el) { return (el || document).querySelector(sel); }
  function $all(sel, el) { return Array.prototype.slice.call((el || document).querySelectorAll(sel)); }

  async function postJSON(url, body) {
    var resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
    var data = null;
    try { data = await resp.json(); } catch (e) { data = { error: "non-JSON response", code: "bad_response" }; }
    return { status: resp.status, ok: resp.ok, data: data };
  }

  /* ---- freshness age ticker ------------------------------------------- */
  function ageText(iso) {
    var t = Date.parse(iso);
    if (isNaN(t)) { return "age n/a"; }
    var s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 60) { return s + "s ago"; }
    var m = Math.floor(s / 60);
    if (m < 60) { return m + "m " + (s % 60) + "s ago"; }
    var h = Math.floor(m / 60);
    return h + "h " + (m % 60) + "m ago";
  }

  function tickAges() {
    var banner = $("#mock-banner");
    if (banner && banner.dataset.builtAt) {
      var el = $("#snap-age");
      if (el) { el.textContent = ageText(banner.dataset.builtAt); }
    }
    var trust = $("#trust-age");
    if (trust && trust.dataset.builtAt) {
      trust.textContent = ageText(trust.dataset.builtAt);
    }
  }
  tickAges();
  window.setInterval(tickAges, 15000);

  /* ---- login: show exactly one scope field per role -------------------- */
  var roleSel = $("#login-role");
  if (roleSel) {
    var syncScopes = function () {
      var role = roleSel.value;
      $all(".scope-field").forEach(function (field) {
        var roles = (field.dataset.roles || "").split(/\s+/);
        var active = roles.indexOf(role) !== -1;
        field.hidden = !active;
        $all("select", field).forEach(function (sel) { sel.disabled = !active; });
      });
    };
    roleSel.addEventListener("change", syncScopes);
    syncScopes();
  }

  /* ---- replan button (single-flight; 409 replan_in_flight surfaced) ---- */
  var replanBtn = $("#replan-btn");
  if (replanBtn) {
    replanBtn.addEventListener("click", async function () {
      var msg = $("#replan-msg");
      replanBtn.disabled = true;
      if (msg) { msg.textContent = "replanning…"; msg.className = "msg"; }
      var r = await postJSON("/api/v1/replan", {});
      if (r.ok) {
        if (msg) {
          msg.textContent = "new snapshot " + r.data.snapshot_id +
            " (scheduled " + r.data.stats.scheduled + ", unscheduled " +
            r.data.stats.unscheduled + ") — reloading";
          msg.className = "msg ok";
        }
        window.setTimeout(function () { window.location.reload(); }, 700);
      } else {
        if (msg) {
          msg.textContent = (r.data.code || "error") + ": " + (r.data.error || r.status);
          msg.className = "msg bad";
        }
        replanBtn.disabled = false;
      }
    });
  }

  /* ---- mechanic actuals (OR-6 single write-path) ------------------------ */
  $all(".act-apply").forEach(function (btn) {
    btn.addEventListener("click", async function () {
      var row = btn.closest("tr");
      if (!row) { return; }
      var msg = $(".act-msg", row);
      var state = $(".act-state", row).value;
      var remRaw = $(".act-rem", row).value;
      var force = $(".act-force", row).checked;
      var body = { task_id: row.dataset.task, state: state, force: force };
      if (state === "in_progress" && remRaw !== "") {
        body.remaining_minutes = parseInt(remRaw, 10);
      }
      btn.disabled = true;
      if (msg) { msg.textContent = "saving…"; msg.className = "msg act-msg"; }
      var r = await postJSON("/api/v1/actuals", body);
      btn.disabled = false;
      if (!msg) { return; }
      if (r.ok) {
        msg.textContent = "saved (" + r.data.previous_state + " → " + r.data.state +
          "); stale until Replan";
        msg.className = "msg act-msg ok";
      } else {
        msg.textContent = (r.data.code || "error") + ": " + (r.data.error || r.status);
        msg.className = "msg act-msg bad";
      }
    });
  });

  /* ---- lead excusal capture (POST /api/v1/excusals) --------------------- */
  $all(".exc-apply").forEach(function (btn) {
    btn.addEventListener("click", async function () {
      var ctl = btn.closest(".exc-ctl");
      if (!ctl) { return; }
      var msg = $(".exc-msg", ctl);
      var notes = $(".exc-notes", ctl).value.trim();
      var body = { task_id: ctl.dataset.task, cause: $(".exc-cause", ctl).value };
      if (notes !== "") { body.notes = notes; }
      btn.disabled = true;
      if (msg) { msg.textContent = "capturing…"; msg.className = "msg exc-msg"; }
      var r = await postJSON("/api/v1/excusals", body);
      btn.disabled = false;
      if (!msg) { return; }
      if (r.ok) {
        msg.textContent = "captured " + r.data.excusal.cause +
          (r.data.excusal.excusable ? " (excusable)" : " (not excusable)") +
          " — reload to refresh chips";
        msg.className = "msg exc-msg ok";
      } else {
        msg.textContent = (r.data.code || "error") + ": " + (r.data.error || r.status);
        msg.className = "msg exc-msg bad";
      }
    });
  });
})();
