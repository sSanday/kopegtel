/* ============================================================
   NMS Kopegtel — shell bersama halaman Services/SLA.
   Tema, sidebar, menu pengguna, toast, dan util format.
   ============================================================ */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  var ICON_MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" style="width:19px;height:19px"><path d="M20 13.5A8 8 0 0 1 10.5 4 8 8 0 1 0 20 13.5z"/></svg>';
  var ICON_SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" style="width:19px;height:19px"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';

  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("nms_theme", t); } catch (_) {}
    var b = $("themeBtn");
    if (b) {
      b.innerHTML = t === "dark" ? ICON_SUN : ICON_MOON;
      b.title = t === "dark" ? "Ganti ke mode terang" : "Ganti ke mode gelap";
    }
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", t === "dark" ? "#0b0f14" : "#eef1f6");
    try {
      document.dispatchEvent(new CustomEvent("nms-theme", { detail: t }));
    } catch (_) {}
  }

  try {
    var t = localStorage.getItem("nms_theme");
    if (t !== "light" && t !== "dark") {
      t = (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) ? "light" : "dark";
    }
    setTheme(t);
  } catch (_) {}

  var tb = $("themeBtn");
  if (tb) tb.addEventListener("click", function () {
    setTheme(document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light");
  });

  try {
    if (localStorage.getItem("nms_nav") === "collapsed" && window.innerWidth > 1100) {
      document.body.classList.add("nav-collapsed");
    }
  } catch (_) {}

  var st = $("sidebarToggle");
  if (st) st.addEventListener("click", function () { document.body.classList.toggle("nav-open"); });
  var sc = $("scrim");
  if (sc) sc.addEventListener("click", function () { document.body.classList.remove("nav-open"); });
  var cb = $("collapseBtn");
  if (cb) cb.addEventListener("click", function () {
    document.body.classList.toggle("nav-collapsed");
    try {
      localStorage.setItem("nms_nav", document.body.classList.contains("nav-collapsed") ? "collapsed" : "open");
    } catch (_) {}
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") document.body.classList.remove("nav-open");
  });

  var ub = $("userBtn"), um = $("userMenu");
  if (ub && um) {
    ub.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = um.style.display === "none";
      um.style.display = open ? "block" : "none";
      ub.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", function () {
      um.style.display = "none";
      ub.setAttribute("aria-expanded", "false");
    });
    um.addEventListener("click", function (e) { e.stopPropagation(); });
  }

  window.svcEsc = function (value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };

  window.svcToast = function (msg, type) {
    var w = $("toasts");
    if (!w) return;
    var el = document.createElement("div");
    el.className = "svc-toast " + (type === "error" ? "error" : (type === "info" ? "info" : "success"));
    el.setAttribute("role", "status");
    el.textContent = msg;
    w.appendChild(el);
    setTimeout(function () {
      el.style.opacity = "0";
      el.style.transition = "opacity .25s";
      setTimeout(function () { el.remove(); }, 260);
    }, 3800);
  };

  window.svcFetch = function (url, opts) {
    opts = opts || {};
    var headers = {};
    var key;
    for (key in (opts.headers || {})) {
      if (Object.prototype.hasOwnProperty.call(opts.headers, key)) headers[key] = opts.headers[key];
    }
    if (!headers["X-Requested-With"]) headers["X-Requested-With"] = "XMLHttpRequest";
    opts.headers = headers;
    return fetch(url, opts).then(function (r) {
      if (r.status === 401 && location.pathname.indexOf("/login") !== 0) {
        location.href = "/login?next=" + encodeURIComponent(location.pathname);
        throw new Error("Sesi berakhir. Silakan login kembali.");
      }
      return r;
    });
  };

  window.svcJson = function (url, opts) {
    return window.svcFetch(url, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) throw new Error(data && data.error ? data.error : ("HTTP " + r.status));
        return data;
      });
    });
  };

  window.svcStatusBadge = function (status) {
    var st = String(status || "PENDING").toUpperCase();
    var cls = st === "ONLINE" ? "online" : (st === "OFFLINE" ? "offline" : "pending");
    var label = st === "ONLINE" ? "ONLINE" : (st === "OFFLINE" ? "OFFLINE" : "PENDING");
    return '<span class="svc-badge ' + cls + '">' + label + '</span>';
  };

  window.svcAvailBar = function (value) {
    if (value === null || value === undefined || isNaN(value)) {
      return '<div class="svc-avail"><span class="num svc-muted">—</span><span class="svc-bar"><i class="none" style="width:0"></i></span></div>';
    }
    var v = Math.max(0, Math.min(100, Number(value)));
    var cls = v >= 99 ? "" : (v >= 95 ? "warn" : "bad");
    return '<div class="svc-avail"><span class="num">' + v.toFixed(2) + '%</span>' +
      '<span class="svc-bar" title="' + v.toFixed(2) + '%"><i class="' + cls + '" style="width:' + v + '%"></i></span></div>';
  };

  window.svcFmtLatency = function (value, status) {
    if (status && String(status).toUpperCase() !== "ONLINE") return "—";
    if (value === null || value === undefined || value === "" || isNaN(value)) return "—";
    var v = Number(value);
    if (v >= 1000) return (v / 1000).toFixed(2) + " s";
    if (v >= 100) return Math.round(v) + " ms";
    return v.toFixed(1) + " ms";
  };

  window.svcFmtPct = function (value) {
    if (value === null || value === undefined || isNaN(value)) return "—";
    return Number(value).toFixed(2) + "%";
  };

  window.svcFmtDateTime = function (value) {
    if (!value) return "—";
    return String(value);
  };

  window.svcFmtAge = function (seconds) {
    if (seconds === null || seconds === undefined || isNaN(seconds)) return "—";
    var s = Math.max(0, Math.round(Number(seconds)));
    if (s < 60) return s + "s ago";
    var m = Math.floor(s / 60);
    if (m < 60) return m + "m ago";
    var h = Math.floor(m / 60);
    if (h < 24) return h + "h ago";
    return Math.floor(h / 24) + "d ago";
  };

  window.svcTypeLabel = function (type) {
    var map = { tcp: "TCP", http: "HTTP", icmp: "ICMP", dns: "DNS" };
    return map[String(type || "").toLowerCase()] || String(type || "—").toUpperCase();
  };

  window.svcSslHtml = function (svc) {
    var type = String(svc.type || "").toLowerCase();
    var url = String(svc.url || "").toLowerCase();
    if (type !== "http" || url.indexOf("https://") !== 0) {
      return '<span class="svc-muted">—</span>';
    }
    var days = svc.ssl_days_left;
    if (days === null || days === undefined || days === "" || isNaN(days)) {
      return '<span class="svc-muted">—</span>';
    }
    var d = Number(days);
    if (d < 0) return '<span class="svc-ssl bad">Expired</span>';
    var cls = d <= 7 ? "bad" : (d <= 30 ? "warn" : "good");
    return '<span class="svc-ssl ' + cls + '" title="' + window.svcEsc(svc.ssl_expires_at || "") + '">' + d + ' d</span>';
  };

  window.svcEmptyRow = function (colspan, title, hint) {
    return '<tr><td colspan="' + colspan + '" class="svc-empty">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
      '<rect x="3" y="4" width="18" height="5" rx="1.5"/><rect x="3" y="11" width="18" height="5" rx="1.5"/>' +
      '<path d="M7 6.5h.01M7 13.5h.01"/></svg>' +
      '<div class="big">' + window.svcEsc(title || "No data found") + '</div>' +
      '<div>' + window.svcEsc(hint || "") + '</div></td></tr>';
  };

  window.svcChartColors = function () {
    var css = getComputedStyle(document.documentElement);
    function v(name, fallback) { return (css.getPropertyValue(name) || fallback).trim(); }
    return {
      text: v("--text-2", "#a3afbf"),
      text3: v("--text-3", "#8593a6"),
      grid: v("--chart-grid", "rgba(148,163,184,.12)"),
      accent: "#2e77ce",
      warn: v("--warn", "#fbbf24"),
      ok: v("--ok", "#4ade80"),
      down: v("--down", "#f87171")
    };
  };
})();
