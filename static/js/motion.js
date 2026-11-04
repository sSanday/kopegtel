/* NMS Kopegtel - bootstrap gerak.
   Menandai halaman sedang "masuk" agar animasi entrance hanya
   berjalan sekali dan tidak diputar ulang ketika data polling
   mengganti isi kartu/tabel. */
(function () {
  "use strict";

  var root = document.documentElement;
  if (!root) return;

  var reduce = null;
  try {
    reduce = window.matchMedia
      ? window.matchMedia("(prefers-reduced-motion: reduce)")
      : null;
  } catch (_) {
    reduce = null;
  }

  if (reduce && reduce.matches) {
    root.classList.add("motion-reduced");
    return;
  }

  root.classList.add("motion-enter");

  var timer = 0;
  var settled = false;

  function settle() {
    if (settled) return;
    settled = true;
    window.clearTimeout(timer);
    root.classList.remove("motion-enter");
    root.classList.add("motion-settled");
  }

  function indexChildren(selector) {
    var groups = document.querySelectorAll(selector);
    for (var g = 0; g < groups.length; g++) {
      var children = groups[g].children;
      for (var i = 0; i < children.length; i++) {
        // Batasi stagger agar item ke-20+ tidak terlambat melewati
        // masa html.motion-enter dan muncul mendadak.
        children[i].style.setProperty("--motion-i", String(Math.min(i, 8)));
      }
    }
  }

  function prepare() {
    indexChildren(
      ".kpi-grid, .tiles, .angka, .sev-strip, .summary, .svc-kpis, " +
        ".host-grid, .odp-grid, .ont-grid, .chart-panels, .summary-bar"
    );
    window.clearTimeout(timer);
    timer = window.setTimeout(settle, 1700);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", prepare, { once: true });
  } else {
    prepare();
  }

  window.addEventListener(
    "load",
    function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(settle, 900);
    },
    { once: true }
  );

  if (reduce) {
    var onChange = function () {
      if (reduce.matches) settle();
    };
    if (reduce.addEventListener) reduce.addEventListener("change", onChange);
    else if (reduce.addListener) reduce.addListener(onChange);
  }
})();
