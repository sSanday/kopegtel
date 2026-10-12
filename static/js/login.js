/* ══════════════════════════════════════════════════════════════════
   login.js — NMS Kopegtel, halaman Login v3.

   Seluruh perilaku ditulis ulang dari inventaris logika v1. Tidak ada
   dependensi, tidak ada fetch, tidak ada endpoint. Form tetap POST ke
   URL dokumen supaya parameter ?next= ikut terbawa.

   Perilaku yang dipertahankan dari v1:
     - tampilkan/sembunyikan kata sandi (ikon + aria-label)
     - validasi sebelum submit, cegah submit kalau ada isian kosong
     - fokus otomatis ke kolom kata sandi bila halaman menampilkan error
   Perilaku yang ditambahkan / diperbaiki:
     - keadaan loading menampilkan TEKS "Memproses…", tanpa spinner
     - tombol otomatis normal kembali: saat submit gagal, saat tombol
       Back / bcacheRestore (pageshow), dan pengaman waktu 15 detik
     - peringatan Caps Lock inline
     - pilihan tema: Otomatis / Terang / Gelap pada kunci nms_theme
   ══════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var STORAGE_KEY = "nms_theme";
  var RESET_MS = 15000;

  var ICON_EYE =
    '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" ' +
    'stroke-width="1.5" aria-hidden="true"><path d="M1.6 10S4.3 5 10 5s8.4 5 8.4 5-2.7 5-8.4 5S1.6 10 1.6 10Z"/>' +
    '<circle cx="10" cy="10" r="2.1"/></svg>';
  var ICON_EYE_OFF =
    '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" ' +
    'stroke-width="1.5" aria-hidden="true"><path d="M1.6 10S4.3 5 10 5c1.4 0 2.6.4 3.6.9M18.4 10s-2.7 5-8.4 5c-1.2 0-2.2-.3-3.2-.7"/>' +
    '<path d="M2.8 2.8l14.4 14.4"/></svg>';

  var $ = function (id) { return document.getElementById(id); };

  var form, user, pass, submitBtn, submitLabel, revealBtn, capsNote;

  /* ── Keadaan loading ─────────────────────────────────────────────
     Satu sumber kebenaran. Semua jalan keluar memanggil endLoading(),
     sehingga tombol tidak pernah tertinggal nonaktif tanpa teks. */
  var resetTimer = null;

  function startLoading() {
    if (resetTimer) { clearTimeout(resetTimer); resetTimer = null; }
    submitBtn.disabled = true;
    submitLabel.textContent = "Memproses…";
    submitBtn.setAttribute("aria-busy", "true");
    // Pengaman: kalau navigasi dibatalkan atau macet, tombol pulih sendiri.
    resetTimer = setTimeout(endLoading, RESET_MS);
  }

  function endLoading() {
    if (resetTimer) { clearTimeout(resetTimer); resetTimer = null; }
    submitBtn.disabled = false;
    submitLabel.textContent = "Masuk";
    submitBtn.removeAttribute("aria-busy");
  }

  /* ── Tampilkan / sembunyikan kata sandi ─────────────────────────── */
  function togglePassword() {
    var showing = pass.type === "text";
    pass.type = showing ? "password" : "text";
    revealBtn.innerHTML = showing ? ICON_EYE : ICON_EYE_OFF;
    revealBtn.setAttribute("aria-label", showing ? "Tampilkan kata sandi" : "Sembunyikan kata sandi");
    revealBtn.setAttribute("aria-pressed", showing ? "false" : "true");
  }

  /* ── Peringatan Caps Lock ──────────────────────────────────────── */
  function updateCaps(on) {
    capsNote.classList.toggle("on", !!on);
  }

  /* ── Tema ──────────────────────────────────────────────────────── */
  function readStored() {
    try {
      var t = localStorage.getItem(STORAGE_KEY);
      return (t === "light" || t === "dark") ? t : "auto";
    } catch (_) { return "auto"; }
  }

  function effectiveTheme(pref) {
    if (pref === "light" || pref === "dark") return pref;
    return (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches)
      ? "light" : "dark";
  }

  function applyTheme(pref) {
    var eff = effectiveTheme(pref);
    document.documentElement.setAttribute("data-theme", eff);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", eff === "dark" ? "#1B1A18" : "#F6F4EE");
    var opts = document.querySelectorAll(".theme-opt");
    for (var i = 0; i < opts.length; i++) {
      opts[i].setAttribute("aria-pressed",
        opts[i].getAttribute("data-theme") === pref ? "true" : "false");
    }
  }

  function setTheme(pref) {
    try {
      if (pref === "auto") localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, pref);
    } catch (_) { /* mode privat: tema tetap berlaku di halaman ini */ }
    applyTheme(pref);
  }

  /* ── Init ──────────────────────────────────────────────────────── */
  function init() {
    form = $("loginForm");
    user = $("username");
    pass = $("password");
    submitBtn = $("submitBtn");
    submitLabel = $("submitLabel");
    revealBtn = $("revealBtn");
    capsNote = $("capsNote");

    if (!(form && user && pass && submitBtn && submitLabel)) return;

    applyTheme(readStored());

    revealBtn.addEventListener("click", togglePassword);

    /* Submit: validasi dulu, baru keadaan loading. */
    form.addEventListener("submit", function (e) {
      if (!user.value.trim() || !pass.value.trim()) {
        e.preventDefault();
        (user.value.trim() ? pass : user).focus();
        return;
      }
      startLoading();
    });

    /* Jaring pengaman: validate dipanggil browser, submit dipanggil
       kode. Bila halaman dimuat dari cache Back, tombol ikut dipulihkan. */
    window.addEventListener("pageshow", function (e) {
      if (e.persisted) endLoading();
    });
    window.addEventListener("pagehide", endLoading);

    /* Galat validasi bawaan (required) = submit dibatalkan, tombol
       harus kembali bisa diklik. */
    form.addEventListener("invalid", endLoading, true);

    /* Caps Lock */
    pass.addEventListener("keyup", function (e) {
      if (typeof e.getModifierState === "function") updateCaps(e.getModifierState("CapsLock"));
    });
    pass.addEventListener("blur", function () { updateCaps(false); });
    pass.addEventListener("focus", function (e) {
      if (typeof e.getModifierState === "function") updateCaps(e.getModifierState("CapsLock"));
    });

    /* Pilihan tema */
    var opts = document.querySelectorAll(".theme-opt");
    for (var i = 0; i < opts.length; i++) {
      opts[i].addEventListener("click", function (e) {
        setTheme(e.currentTarget.getAttribute("data-theme"));
      });
    }

    /* Mode "Otomatis" ikut berubah bila preferensi sistem berubah. */
    var mq = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)");
    if (mq && mq.addEventListener) {
      mq.addEventListener("change", function () {
        if (readStored() === "auto") applyTheme("auto");
      });
    }

    /* Bila halaman menampilkan pesan error, fokus ke kata sandi. */
    if ($("loginError") && !$("loginError").hidden) {
      pass.focus();
      if (pass.select) pass.select();
    }

    endLoading();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
