/* Halaman Login NMS Kopegtel.
   Form tetap POST ke URL dokumen supaya ?next= ikut terbawa;
   karena itu <form> sengaja tanpa atribut action. */

(function () {
  var KUNCI_TEMA = "nms_theme";
  var SELAMAT_MS = 15000;

  var form = document.getElementById("loginForm");
  var sandi = document.getElementById("password");
  var tombol = document.getElementById("tombolMasuk");
  var label = document.getElementById("tombolLabel");
  var lihat = document.getElementById("lihatSandi");
  var catatanCaps = document.getElementById("capsNote");
  var pengaman = null;

  var IKON_MATA =
    '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor"' +
    ' stroke-width="1.5" aria-hidden="true"><path d="M1.6 10S4.3 5 10 5s8.4 5 8.4 5-2.7 5-8.4 5S1.6 10 1.6 10Z"/>' +
    '<circle cx="10" cy="10" r="2.1"></circle></svg>';
  var IKON_MATA_TERTUTUP =
    '<svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor"' +
    ' stroke-width="1.5" aria-hidden="true"><path d="M1.6 10S4.3 5 10 5c1.4 0 2.6.4 3.6.9"/>' +
    '<path d="M18.4 10s-2.7 5-8.4 5c-1.2 0-2.2-.3-3.2-.7"/><path d="M2.8 2.8l14.4 14.4"/></svg>';

  function simpanPengaman() {
    if (pengaman) clearTimeout(pengaman);
    pengaman = setTimeout(siapkanTombol, SELAMAT_MS);
  }

  function siapkanTombol() {
    if (pengaman) { clearTimeout(pengaman); pengaman = null; }
    tombol.disabled = false;
    label.textContent = "Masuk";
    tombol.removeAttribute("aria-busy");
  }

  function kirimForm() {
    var nama = document.getElementById("username");
    if (!nama.value.trim() || !sandi.value.trim()) {
      (nama.value.trim() ? sandi : nama).focus();
      return;
    }
    tombol.disabled = true;
    label.textContent = "Memproses…";
    tombol.setAttribute("aria-busy", "true");
    simpanPengaman();
  }

  function tampilkanSandi() {
    var tampil = sandi.type === "text";
    sandi.type = tampil ? "password" : "text";
    lihat.innerHTML = tampil ? IKON_MATA : IKON_MATA_TERTUTUP;
    lihat.setAttribute("aria-label", tampil ? "Tampilkan kata sandi" : "Sembunyikan kata sandi");
    lihat.setAttribute("aria-pressed", tampil ? "false" : "true");
  }

  function capsLock(aktif) {
    catatanCaps.classList.toggle("on", !!aktif);
  }

  function terapkanTema(mode) {
    var efektif = mode;
    if (mode !== "light" && mode !== "dark") {
      efektif = (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches)
        ? "light" : "dark";
    }
    document.documentElement.setAttribute("data-theme", efektif);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", efektif === "dark" ? "#1B1A18" : "#F6F4EE");
    Array.prototype.forEach.call(document.querySelectorAll(".tema-pilih"), function (tombolTema) {
      tombolTema.setAttribute("aria-pressed",
        tombolTema.getAttribute("data-mode") === mode ? "true" : "false");
    });
  }

  function pilihTema(mode) {
    try {
      if (mode === "auto") localStorage.removeItem(KUNCI_TEMA);
      else localStorage.setItem(KUNCI_TEMA, mode);
    } catch (_) {}
    terapkanTema(mode);
  }

  function mulai() {
    var tersimpan = "auto";
    try {
      var nilai = localStorage.getItem(KUNCI_TEMA);
      if (nilai === "light" || nilai === "dark") tersimpan = nilai;
    } catch (_) {}
    terapkanTema(tersimpan);

    lihat.innerHTML = IKON_MATA;
    lihat.addEventListener("click", tampilkanSandi);
    form.addEventListener("submit", kirimForm);
    form.addEventListener("invalid", siapkanTombol, true);
    window.addEventListener("pageshow", function (e) { if (e.persisted) siapkanTombol(); });
    window.addEventListener("pagehide", siapkanTombol);

    ["keyup", "focus", "blur"].forEach(function (jenis) {
      sandi.addEventListener(jenis, function (e) {
        capsLock(jenis !== "blur" && typeof e.getModifierState === "function"
          && e.getModifierState("CapsLock"));
      });
    });

    Array.prototype.forEach.call(document.querySelectorAll(".tema-pilih"), function (tombolTema) {
      tombolTema.addEventListener("click", function () {
        pilihTema(tombolTema.getAttribute("data-mode"));
      });
    });

    var mq = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)");
    if (mq && mq.addEventListener) {
      mq.addEventListener("change", function () {
        var mode = "auto";
        try { mode = localStorage.getItem(KUNCI_TEMA) || "auto"; } catch (_) {}
        if (mode === "auto") terapkanTema("auto");
      });
    }

    var pesan = document.getElementById("pesanError");
    if (pesan && !pesan.hidden) {
      sandi.focus();
      sandi.select();
    }

    siapkanTombol();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mulai);
  else mulai();
})();
