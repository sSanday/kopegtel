/* Kerangka bersama: tanggal, jam (waktu server, zona Asia/Jakarta), banner backend mati.
 * Guard autentikasi ditangani oleh nav.js + apiFetch (401 → login).
 */
(function () {
  "use strict";

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  let clockTimer = null;

  function startClock() {
    const clockEl = document.getElementById("clock");
    const todayEl = document.getElementById("today");

    // Selisih waktu server terhadap waktu lokal (sekali saat memuat).
    const me = window.__nmsMe || {};
    const serverOffsetMs = me.server_time ? new Date(me.server_time).getTime() - Date.now() : 0;
    const timeZone = me.timezone || "Asia/Jakarta";

    function render() {
      const now = new Date(Date.now() + serverOffsetMs);
      if (clockEl) {
        clockEl.textContent = new Intl.DateTimeFormat("id-ID", {
          hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone,
        }).format(now);
        clockEl.title = `Waktu server (${timeZone})`;
      }
      if (todayEl) {
        todayEl.textContent = new Intl.DateTimeFormat("id-ID", {
          weekday: "long", day: "numeric", month: "long", year: "numeric", timeZone,
        }).format(now);
      }
    }

    if (clockTimer !== null) window.clearInterval(clockTimer);
    render();
    clockTimer = window.setInterval(render, 1000);
  }

  function buildBanner() {
    if (document.getElementById("backend-down-banner")) return;
    const banner = document.createElement("div");
    banner.id = "backend-down-banner";
    banner.className = "backend-banner";
    banner.setAttribute("role", "alert");
    banner.hidden = true;
    banner.innerHTML =
      '<span class="backend-banner__text">Server tidak dapat dihubungi. Periksa koneksi atau backend NMS.</span>' +
      '<button class="btn btn--ghost backend-banner__retry" type="button">Coba lagi</button>';
    banner.querySelector(".backend-banner__retry").addEventListener("click", () => {
      window.location.reload();
    });
    document.body.prepend(banner);

    window.addEventListener("nms:backend-down", () => {
      banner.hidden = false;
    });
    window.addEventListener("nms:backend-up", () => {
      banner.hidden = true;
    });
  }

  buildBanner();
  window.addEventListener("nms:me-ready", startClock);
  // Fallback bila nav.js selesai sebelum listener terpasang.
  if (window.__nmsMe) startClock();
})();
