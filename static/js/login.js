/* ============================================================
   NMS Kopegtel — Login (hanya lapisan tampilan).
   ============================================================ */
(function () {
  "use strict";

  var ICON_EYE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/></svg>';
  var ICON_EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12S6 5.5 12 5.5c2 0 3.7.7 5.1 1.6M21.5 12S18 18.5 12 18.5c-2 0-3.7-.7-5.1-1.6"/><path d="M4 4l16 16"/></svg>';

  function $(id) { return document.getElementById(id); }

  function prefersLight() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
  }
  function storedTheme() {
    try {
      var t = localStorage.getItem("nms_theme");
      if (t === "light" || t === "dark" || t === "auto") return t;
    } catch (_) {}
    return "auto";
  }
  function effectiveTheme(choice) {
    if (choice === "light") return "light";
    if (choice === "dark") return "dark";
    return prefersLight() ? "light" : "dark";
  }
  function paintTheme(choice) {
    var eff = effectiveTheme(choice);
    document.documentElement.setAttribute("data-theme", eff);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = eff === "light" ? "#F6F4EE" : "#1B1A18";
    var btns = document.querySelectorAll("[data-theme-choice]");
    for (var i = 0; i < btns.length; i++) {
      btns[i].setAttribute("aria-pressed", btns[i].getAttribute("data-theme-choice") === choice ? "true" : "false");
    }
  }
  function applyTheme(choice) {
    try { localStorage.setItem("nms_theme", choice); } catch (_) {}
    paintTheme(choice);
  }

  function togglePassword() {
    var pw = $("password");
    var btn = $("toggleBtn");
    if (!pw || !btn) return;
    if (pw.type === "password") {
      pw.type = "text";
      btn.innerHTML = ICON_EYE_OFF;
      btn.setAttribute("aria-label", "Sembunyikan kata sandi");
      btn.setAttribute("aria-pressed", "true");
    } else {
      pw.type = "password";
      btn.innerHTML = ICON_EYE;
      btn.setAttribute("aria-label", "Tampilkan kata sandi");
      btn.setAttribute("aria-pressed", "false");
    }
  }

  function bindCapsHint() {
    var pw = $("password");
    var hint = $("capsHint");
    if (!pw || !hint) return;
    var update = function (e) {
      var on = false;
      try { if (e.getModifierState) on = e.getModifierState("CapsLock"); } catch (_) { on = false; }
      hint.classList.toggle("visible", !!on);
    };
    pw.addEventListener("keydown", update);
    pw.addEventListener("keyup", update);
    pw.addEventListener("blur", function () { hint.classList.remove("visible"); });
  }

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
      var text = form.querySelector(".btn-submit .btn-text");
      btn.disabled = true;
      btn.classList.add("loading");
      btn.setAttribute("aria-busy", "true");
      if (text) text.textContent = "Memproses…";
    });
  }

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

  document.addEventListener("DOMContentLoaded", function () {
    paintTheme(storedTheme());

    var themeBtns = document.querySelectorAll("[data-theme-choice]");
    for (var i = 0; i < themeBtns.length; i++) {
      themeBtns[i].addEventListener("click", function () {
        applyTheme(this.getAttribute("data-theme-choice"));
      });
    }

    var toggle = $("toggleBtn");
    if (toggle) toggle.addEventListener("click", togglePassword);

    bindCapsHint();
    bindSubmit();
    initCountdown();

    if ($("errorBox")) {
      var pw = $("password");
      if (pw) pw.focus();
    }
  });
})();
