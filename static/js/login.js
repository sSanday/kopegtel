/* ============================================================
   NMS Kopegtel — Login (hanya lapisan tampilan).
   ============================================================ */
(function () {
  "use strict";

  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (REDUCED) document.body.classList.add("reduced-motion");

  var ICON_EYE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/></svg>';
  var ICON_EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12S6 5.5 12 5.5c2 0 3.7.7 5.1 1.6M21.5 12S18 18.5 12 18.5c-2 0-3.7-.7-5.1-1.6"/><path d="M4 4l16 16"/></svg>';
  var ICON_MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 13.5A8 8 0 0 1 10.5 4 8 8 0 1 0 20 13.5z"/></svg>';
  var ICON_SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';

  function $(id) { return document.getElementById(id); }

  /* ---------- Tema ---------- */
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("nms_theme", theme); } catch (_) {}
    var btn = $("themeBtn");
    if (btn) {
      btn.innerHTML = theme === "dark" ? ICON_SUN : ICON_MOON;
      btn.title = theme === "dark" ? "Ganti ke mode terang" : "Ganti ke mode gelap";
      btn.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    }
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#0b0f14" : "#eef1f6");
  }
  function toggleTheme() {
    var cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    applyTheme(cur === "dark" ? "light" : "dark");
  }

  /* ---------- Tampil/sembunyikan password ---------- */
  function togglePassword() {
    var pw = $("password");
    var btn = $("toggleBtn");
    if (!pw || !btn) return;
    if (pw.type === "password") {
      pw.type = "text";
      btn.innerHTML = ICON_EYE_OFF;
      btn.setAttribute("aria-label", "Sembunyikan password");
      btn.setAttribute("aria-pressed", "true");
    } else {
      pw.type = "password";
      btn.innerHTML = ICON_EYE;
      btn.setAttribute("aria-label", "Tampilkan password");
      btn.setAttribute("aria-pressed", "false");
    }
  }

  /* ---------- Caps Lock ---------- */
  function bindCapsHint() {
    var pw = $("password");
    var hint = $("capsHint");
    if (!pw || !hint) return;
    var update = function (e) {
      var on = false;
      try {
        if (e.getModifierState) on = e.getModifierState("CapsLock");
      } catch (_) { on = false; }
      hint.classList.toggle("on", !!on);
    };
    pw.addEventListener("keydown", update);
    pw.addEventListener("keyup", update);
    pw.addEventListener("blur", function () { hint.classList.remove("on"); });
  }

  /* ---------- Loading state saat submit ---------- */
  function bindSubmit() {
    var form = $("loginForm");
    if (!form) return;
    form.addEventListener("submit", function (e) {
      var u = $("username").value.trim();
      var p = $("password").value;
      if (!u || !p) {
        e.preventDefault();
        var target = !u ? $("username") : $("password");
        target.focus();
        return;
      }
      var btn = $("submitBtn");
      var text = $("btnText");
      btn.disabled = true;
      btn.classList.add("loading");
      btn.setAttribute("aria-busy", "true");
      text.textContent = "Memproses…";
    });
  }

  /* ---------- Hitung mundur blokir (rate limit) ---------- */
  function initCountdown() {
    var box = $("errorBox");
    if (!box) return;
    var sec = parseInt(box.getAttribute("data-seconds") || "0", 10);
    if (!Number.isFinite(sec) || sec <= 0) return;
    var cd = $("errCountdown");
    var btn = $("submitBtn");
    var u = $("username"), p = $("password");
    if (btn) btn.disabled = true;
    if (u) u.disabled = true;
    if (p) p.disabled = true;
    var left = sec;
    var timer = setInterval(tick, 1000);
    function tick() {
      if (left <= 0) {
        clearInterval(timer);
        if (cd) cd.textContent = "Silakan coba lagi sekarang.";
        if (btn) btn.disabled = false;
        if (u) { u.disabled = false; u.focus(); }
        if (p) p.disabled = false;
        return;
      }
      if (cd) cd.textContent = "Bisa mencoba lagi dalam " + left + " detik…";
      left -= 1;
    }
    tick();
  }

  /* ---------- Status sistem asli ---------- */
  function checkHealth() {
    var el = $("sysStatus");
    if (!el) return;
    fetch("/health", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) {
        if (r.ok) el.classList.add("on");
      })
      .catch(function () {});
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = $("themeBtn");
    if (btn) {
      applyTheme(document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark");
      btn.addEventListener("click", toggleTheme);
    }
    var toggle = $("toggleBtn");
    if (toggle) {
      toggle.innerHTML = ICON_EYE;
      toggle.addEventListener("click", togglePassword);
    }
    bindCapsHint();
    bindSubmit();
    initCountdown();
    checkHealth();

    // Fokus ke password bila ada pesan error (percobaan sebelumnya gagal),
    // selain itu autofokus tetap di username.
    if ($("errorBox")) {
      var pw = $("password");
      if (pw) pw.focus();
      var box = document.querySelector(".form-box");
      if (box && !REDUCED) {
        box.classList.add("shake");
        box.addEventListener("animationend", function () { box.classList.remove("shake"); }, { once: true });
      }
    }
  });
})();
