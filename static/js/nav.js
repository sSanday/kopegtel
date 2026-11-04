/* Simpan status buka/tutup grup menu sidebar agar tidak terbuka lagi
   setiap kali berpindah halaman. */
(function () {
  "use strict";

  var STORAGE_KEY = "nms_nav_groups";

  function readState() {
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      var parsed = raw ? JSON.parse(raw) : null;
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
      return {};
    }
  }

  function groupKey(details, index) {
    var label = details.querySelector("summary > span");
    var text = label ? label.textContent.replace(/\s+/g, " ").trim() : "";
    return text || "group-" + index;
  }

  function save(groups) {
    var state = {};
    groups.forEach(function (details, index) {
      state[groupKey(details, index)] = !!details.open;
    });
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (_) {}
  }

  function init() {
    var groups = Array.prototype.slice.call(
      document.querySelectorAll("details.nav-sub-wrap")
    );
    if (!groups.length) return;

    var saved = readState();
    groups.forEach(function (details, index) {
      var key = groupKey(details, index);
      if (Object.prototype.hasOwnProperty.call(saved, key)) {
        details.open = !!saved[key];
      }
      details.addEventListener("toggle", function () {
        save(groups);
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
