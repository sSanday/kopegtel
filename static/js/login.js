// Form POST ke URL dokumen agar parameter ?next= tetap terbawa.

(function () {
  var SELAMAT_MS = 15000;

  var IKON_TEMA_GELAP =
    '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"></circle><path d="M12 3v2M12 19v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M3 12h2M19 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4"></path></svg>';
  var IKON_TEMA_TERANG =
    '<svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 13.5A8 8 0 0 1 10.5 4 8 8 0 1 0 20 13.5z"></path></svg>';

  var form = document.getElementById("loginForm");
  var sandi = document.getElementById("password");
  var tombol = document.getElementById("tombolMasuk");
  var label = document.getElementById("tombolLabel");
  var lihat = document.getElementById("lihatSandi");
  var catatanCaps = document.getElementById("capsNote");
  var pengaman = null;
  var kunci = null;
  var terkunci = false;

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
    if (terkunci) return;
    if (pengaman) { clearTimeout(pengaman); pengaman = null; }
    tombol.disabled = false;
    label.textContent = "Masuk";
    tombol.removeAttribute("aria-busy");
  }

  // Server mengunci IP setelah 5 kali gagal; hitung mundurnya ikut ditampilkan.
  function mulaiKunci(sekon) {
    var sisa = sekon;
    terkunci = true;
    tombol.disabled = true;
    function tik() {
      if (sisa <= 0) {
        clearInterval(kunci);
        terkunci = false;
        siapkanTombol();
        document.getElementById("username").focus();
        return;
      }
      label.textContent = "Terkunci " + sisa + " detik";
      sisa -= 1;
    }
    kunci = setInterval(tik, 1000);
    tik();
  }

  function kirimForm(e) {
    if (terkunci) {
      e.preventDefault();
      return;
    }
    var nama = document.getElementById("username");
    if (!nama.value.trim() || !sandi.value.trim()) {
      e.preventDefault();
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
    catatanCaps.textContent = aktif ? "Caps Lock sedang aktif." : "";
    catatanCaps.classList.toggle("err", !!aktif);
  }

  // Sama seperti setTheme()/toggleTheme() di app.js agar perilakunya konsisten.
  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("nms_theme", theme); } catch (_) {}
    var btn = document.getElementById("themeBtn");
    if (btn) {
      var dark = theme === "dark";
      btn.setAttribute("aria-pressed", dark ? "true" : "false");
      btn.title = dark ? "Ganti ke mode terang" : "Ganti ke mode gelap";
      btn.innerHTML = dark ? IKON_TEMA_GELAP : IKON_TEMA_TERANG;
    }
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#10131a" : "#f4f0e8");
  }

  function toggleTheme() {
    var cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    setTheme(cur === "dark" ? "light" : "dark");
  }

  function mulai() {
    setTheme(document.documentElement.getAttribute("data-theme") || "dark");

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

    document.getElementById("themeBtn").addEventListener("click", toggleTheme);

    var sekonKunci = parseInt(document.body.dataset.terkunci || "0", 10);
    if (sekonKunci > 0) {
      mulaiKunci(sekonKunci);
    } else {
      var pesan = document.getElementById("pesanError");
      if (pesan) {
        sandi.focus();
        sandi.select();
      }
    }

    siapkanTombol();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mulai);
  else mulai();
})();
