/* ============================================================
   NMS Kopegtel — Dashboard (hanya lapisan tampilan).
   Membaca API yang sudah ada, tidak mengubah backend.
   ============================================================ */
(function () {
  "use strict";

  /* ---------- Konfigurasi ---------- */
  var PALETTE = ["#14b8a6", "#0ea5e9", "#a78bfa", "#f59e0b", "#f43f5e"];
  var REFRESH_MS = 30000;
  var EVENTS_PAGE = 8;

  var METRIC_CONFIG = {
    latency:   { title: "Latensi Jaringan", sub: "Aliran data ping waktu nyata — ms", unit: "ms", color: null },
    cpu:       { title: "Penggunaan CPU",   sub: "Utilisasi CPU dari agen — %",        unit: "%",  color: "#f43f5e" },
    ram:       { title: "Penggunaan RAM",   sub: "Utilisasi memori dari agen — %",     unit: "%",  color: "#f59e0b" },
    disk:      { title: "Penggunaan Disk",  sub: "Utilisasi disk dari agen — %",       unit: "%",  color: "#a78bfa" },
    bandwidth: { title: "Bandwidth",        sub: "Throughput jaringan — Mbps",         unit: "Mbps", color: "#14b8a6" }
  };

  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (REDUCED) document.body.classList.add("reduced-motion");

  var myChart = null;
  var currentRange = 6;
  var currentMetric = "latency";
  var hosts = [];
  var hostStateFilter = "all";
  var eventsShown = EVENTS_PAGE;
  var countdownLeft = REFRESH_MS / 1000;

  var ICON_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>';
  var ICON_ALERT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 17H2L12 3z"/><path d="M12 10v4M12 17.5v.5"/></svg>';
  var ICON_EYE = '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" style="vertical-align:-2px"><path d="M1.5 8S4 4.2 8 4.2 14.5 8 14.5 8 12 11.8 8 11.8 1.5 8 1.5 8Z"/><circle cx="8" cy="8" r="1.8"/></svg>';
  var ICON_EYE_OFF = '<svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" style="vertical-align:-2px"><path d="M1.5 8S4 4.2 8 4.2c1.4 0 2.7.4 3.8 1M14.5 8S12 11.8 8 11.8c-1.4 0-2.7-.4-3.8-1"/><path d="M2.5 2.5l11 11"/></svg>';
  var NOTIF_ICON = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" rx="22" fill="%230b0f14"/><circle cx="50" cy="50" r="26" fill="none" stroke="%232dd4bf" stroke-width="7"/><circle cx="50" cy="50" r="9" fill="%234ade80"/></svg>';

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function themeVars() { return getComputedStyle(document.documentElement); }

  /* ---------- Toast ---------- */
  function toast(msg, type) {
    var wrap = $("toasts");
    if (!wrap) return;
    var el = document.createElement("div");
    el.className = "toast " + (type === "error" ? "error" : "success");
    el.setAttribute("role", "status");
    el.innerHTML = (type === "error" ? ICON_ALERT : ICON_CHECK) + "<span>" + esc(msg) + "</span>";
    wrap.appendChild(el);
    setTimeout(function () {
      el.style.opacity = "0";
      setTimeout(function () { el.remove(); }, 250);
    }, 3500);
  }

  /* ---------- Tema ---------- */
  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("nms_theme", theme); } catch (_) {}
    var btn = $("themeBtn");
    if (btn) {
      var dark = theme === "dark";
      btn.setAttribute("aria-pressed", dark ? "true" : "false");
      btn.title = dark ? "Ganti ke mode terang" : "Ganti ke mode gelap";
    }
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#0b0f14" : "#eef1f6");
  }
  function toggleTheme() {
    var cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    setTheme(cur === "dark" ? "light" : "dark");
    renderKpiTrends();
    fetchChart();
  }

  /* ---------- Sidebar / menu ---------- */
  function closeAllMenus() {
    document.querySelectorAll(".menu-pop.open").forEach(function (m) { m.classList.remove("open"); });
  }
  function toggleSidebar() { document.body.classList.toggle("nav-open"); }
  function closeSidebar() { document.body.classList.remove("nav-open"); }
  function toggleCollapse() {
    var collapsed = document.body.classList.toggle("nav-collapsed");
    try { localStorage.setItem("nms_nav", collapsed ? "collapsed" : "full"); } catch (_) {}
  }

  function setActiveNav() {
    var path = window.location.pathname || "/";
    var map = { "/": "dashboard", "/triggers": "monitoring", "/services": "services", "/inventory": "inventory", "/logs": "logs", "/reports": "reports", "/fiber": "fiber", "/mikrotik": "mikrotik", "/power-analytics": "power", "/discovery": "discovery" };
    var key = map[path] || "dashboard";
    var params = new URLSearchParams(window.location.search || "");
    var sev = (params.get("severity") || "").toLowerCase();
    if (path === "/triggers" && (sev.indexOf("disaster") !== -1 || sev.indexOf("high") !== -1)) key = "alerts";
    document.querySelectorAll("[data-nav]").forEach(function (el) {
      el.classList.toggle("active", el.getAttribute("data-nav") === key);
      if (el.getAttribute("data-nav") === key) el.removeAttribute("aria-current");
      if (el.getAttribute("data-nav") === key) el.setAttribute("aria-current", "page");
    });
  }

  /* ---------- Error banner ---------- */
  function showErr(msg) {
    $("errMsg").textContent = msg;
    $("errBanner").classList.add("on");
  }
  function hideErr() { $("errBanner").classList.remove("on"); }

  /* ---------- Animasi angka KPI ---------- */
  function animNum(el, to, decimals, suffix) {
    if (!el) return;
    decimals = decimals || 0;
    suffix = suffix || "";
    if (REDUCED) { el.textContent = Number(to).toFixed(decimals) + suffix; return; }
    var from = parseFloat(el.getAttribute("data-v") || "0") || 0;
    var start = null;
    var dur = 450;
    function frame(t) {
      if (!start) start = t;
      var p = Math.min(1, (t - start) / dur);
      var v = from + (to - from) * (1 - Math.pow(1 - p, 3));
      el.textContent = v.toFixed(decimals) + suffix;
      if (p < 1) requestAnimationFrame(frame);
      else el.setAttribute("data-v", String(to));
    }
    requestAnimationFrame(frame);
  }

  function setBar(id, pct) {
    var el = $(id);
    if (el) el.style.width = Math.max(0, Math.min(100, Number(pct) || 0)) + "%";
  }

  /* ---------- Tren KPI (riwayat lokal) ---------- */
  var TREND_KEY = "nms_kpi_hist";
  var TREND_MAX = 288;           // 288 titik x 5 menit = 24 jam
  var TREND_GAP = 5 * 60 * 1000; // simpan maksimal 1 titik per 5 menit
  var TREND_SPAN = 20 * 3600 * 1000;

  function loadTrendHist() {
    try {
      var a = JSON.parse(localStorage.getItem(TREND_KEY) || "[]");
      return Array.isArray(a) ? a : [];
    } catch (_) { return []; }
  }
  function recordKpiSnapshot(uptime) {
    try {
      var h = loadTrendHist();
      var now = Date.now();
      if (h.length && now - h[h.length - 1].t < TREND_GAP) { renderKpiTrends(); return; }
      h.push({
        t: now,
        total: window._kpiTotal || 0,
        up: window._kpiUp || 0,
        down: window._kpiDown || 0,
        uptime: uptime == null ? null : Number(uptime)
      });
      while (h.length > TREND_MAX) h.shift();
      localStorage.setItem(TREND_KEY, JSON.stringify(h));
    } catch (_) {}
    renderKpiTrends();
  }
  function drawSpark(canvasId, pts, color) {
    var cv = $(canvasId);
    if (!cv || !cv.getContext) return;
    var ctx = cv.getContext("2d");
    var W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    if (!pts || pts.length < 2) return;
    var vs = pts.map(function (p) { return p.v; });
    var lo = Math.min.apply(null, vs), hi = Math.max.apply(null, vs);
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    var px = function (i) { return 4 + (i * (W - 8)) / (pts.length - 1); };
    var py = function (v) { return H - 5 - ((v - lo) * (H - 10)) / (hi - lo); };
    ctx.beginPath();
    pts.forEach(function (p, i) { if (i === 0) ctx.moveTo(px(i), py(p.v)); else ctx.lineTo(px(i), py(p.v)); });
    ctx.strokeStyle = color;
    ctx.lineWidth = 3;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke();
    ctx.lineTo(px(pts.length - 1), H);
    ctx.lineTo(px(0), H);
    ctx.closePath();
    var g = ctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, color + "33");
    g.addColorStop(1, color + "00");
    ctx.fillStyle = g;
    ctx.fill();
  }
  function setDelta(id, d, fmt, polarity) {
    var el = $(id);
    if (!el) return;
    if (d === null || d === undefined || isNaN(d)) { el.textContent = "—"; el.className = "delta flat"; return; }
    el.textContent = (d > 0 ? "+" : "") + fmt(d);
    var cls = "flat";
    if (Math.abs(d) > 1e-9) {
      if (polarity === "neutral") cls = "flat";
      else if (polarity === "invert") cls = d > 0 ? "down" : "up";
      else cls = d > 0 ? "up" : "down";
    }
    el.className = "delta " + cls;
  }
  function renderKpiTrends() {
    var h = loadTrendHist();
    var vars = themeVars();
    var css = function (n, fb) { return (vars.getPropertyValue(n) || fb).trim(); };
    var cfgs = [
      { key: "total", cv: "sparkTotal", dl: "deltaTotal", color: css("--text-3", "#8593a6"), fmt: function (d) { return String(Math.round(d)); }, pol: "neutral" },
      { key: "up", cv: "sparkUp", dl: "deltaUp", color: css("--ok-text", "#4ade80"), fmt: function (d) { return String(Math.round(d)); }, pol: "normal" },
      { key: "down", cv: "sparkDown", dl: "deltaDown", color: css("--down-text", "#f87171"), fmt: function (d) { return String(Math.round(d)); }, pol: "invert" },
      { key: "uptime", cv: "sparkUptime", dl: "deltaUptime", color: css("--accent-text", "#5eead4"), fmt: function (d) { return d.toFixed(1) + "%"; }, pol: "normal", eps: 0.05 }
    ];
    if (h.length < 2) {
      cfgs.forEach(function (c) {
        drawSpark(c.cv, [], c.color);
        var el = $(c.dl);
        if (el) { el.textContent = "—"; el.className = "delta flat"; el.title = "Riwayat lokal belum cukup (perlu 2 titik)"; }
      });
      return;
    }
    var now = Date.now();
    cfgs.forEach(function (c) {
      var pts = h.filter(function (p) { return p[c.key] !== null && p[c.key] !== undefined; })
        .map(function (p) { return { t: p.t, v: Number(p[c.key]) }; });
      drawSpark(c.cv, pts, c.color);
      var base = null;
      for (var i = pts.length - 1; i >= 0; i--) {
        if (now - pts[i].t >= TREND_SPAN) { base = pts[i].v; break; }
      }
      if (base === null) base = pts[0].v;
      var d = pts[pts.length - 1].v - base;
      if (c.eps && Math.abs(d) < c.eps) d = 0;
      setDelta(c.dl, d, c.fmt, c.pol);
      var el = $(c.dl);
      if (el) el.title = "Perubahan vs " + (now - pts[0].t >= TREND_SPAN ? "24 jam lalu" : "awal riwayat") + " (riwayat lokal)";
    });
  }

  /* ---------- Sorot kartu dari tautan (?sorot=ip) ---------- */
  function sorotFromQuery() {
    try {
      var p = new URLSearchParams(location.search || "");
      var ip = p.get("sorot") || p.get("host");
      if (!ip) return;
      var card = document.querySelector('.host-card[data-host="' + (window.CSS && CSS.escape ? CSS.escape(ip) : ip) + '"]');
      if (!card) return;
      card.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "center" });
      card.classList.add("flash");
      setTimeout(function () { card.classList.remove("flash"); }, 3200);
    } catch (_) {}
  }

  /* ---------- Ringkasan ---------- */
  function updateSummary(total, up, down, avgUptime, pending, catCount) {
    window._kpiTotal = total || 0;
    window._kpiUp = up || 0;
    window._kpiDown = down || 0;
    animNum($("sumTotal"), total || 0, 0, "");
    animNum($("sumUp"), up || 0, 0, "");
    animNum($("sumDown"), down || 0, 0, "");
    var upEl = $("sumUptime");
    if (upEl) upEl.textContent = avgUptime != null ? Number(avgUptime).toFixed(1) + "%" : "—";
    var set = function (id, v) { var el = $(id); if (el) el.textContent = v; };
    set("sumTotalSub", (catCount || 0) + " kategori" + (pending ? " · " + pending + " menunggu" : ""));
    set("sumUpSub", total ? Math.round(((up || 0) / total) * 100) + "% dari total host" : "Belum ada data");
    var downCard = $("kpiDown");
    if (downCard) downCard.classList.toggle("is-alert", (down || 0) > 0);
    set("sumDownSub", (down || 0) > 0 ? "Butuh perhatian segera" : "Semua host normal");
    var downEl = $("sumDown");
    if (downEl && pending != null && pending > 0) downEl.title = pending + " host belum ada data ping";
    else if (downEl) downEl.removeAttribute("title");
    var lanHint = $("lanHint");
    if (lanHint) {
      var known = (up || 0) + (down || 0) + (pending || 0);
      var show = total > 0 && (up || 0) === 0 && known >= total;
      lanHint.style.display = show ? "" : "none";
      if (!show) {
        var acc = $("troubleshoot");
        if (acc) acc.classList.remove("open");
        var tg = $("alertToggle");
        if (tg) { tg.setAttribute("aria-expanded", "false"); tg.textContent = "Lihat langkah"; }
      }
    }
  }

  /* ---------- Daftar host ---------- */
  function pillFor(item, isMaint) {
    if (item.pending) return '<span class="pill pill-pending">Menunggu</span>';
    if (item.up) return '<span class="pill pill-up">' + (isMaint ? "Up · Rawat" : "Up") + "</span>";
    return '<span class="pill pill-down">' + (isMaint ? "Rawat" : "Down") + "</span>";
  }

  function renderStatus(pData, aData) {
    var grid = $("statusGrid");
    var allHosts = hosts && hosts.length ? hosts : Object.keys(pData || {});
    var total = allHosts.length;
    var upCount = 0, downCount = 0, pendingCount = 0;

    if (!pData || Object.keys(pData).length === 0) {
      if (total > 0) {
        pData = {};
        allHosts.forEach(function (h) { pData[h] = []; });
      } else {
        grid.innerHTML = '<div class="empty-box" role="status"><b>Belum ada host</b>Tambahkan host pertama Anda untuk mulai memantau.</div>';
        updateSummary(0, 0, 0, null, 0, 0);
        updateHostsCount(0, 0);
        return;
      }
    }

    var groups = {};
    var hostSet = {};
    Object.keys(pData || {}).concat(allHosts).forEach(function (h) { hostSet[h] = 1; });
    Object.keys(hostSet).forEach(function (host) {
      var latArr = (pData && pData[host]) || [];
      var hasData = latArr.length > 0;
      var lat = hasData ? latArr[latArr.length - 1] : null;
      var isPending = !hasData || lat === null;
      var up = !isPending && lat !== -1;
      if (up) upCount++;
      else if (isPending) pendingCount++;
      else downCount++;
      var agent = aData && aData[host];
      var displayName = window.aliasMap && window.aliasMap[host] ? window.aliasMap[host] : host;
      var category = window.categoryMap && window.categoryMap[host] ? window.categoryMap[host] : "Uncategorized";
      category = String(category || "").trim() || "Uncategorized";
      if (!groups[category]) groups[category] = [];
      groups[category].push({ host: host, lat: lat, up: up, pending: isPending, agent: agent, displayName: displayName, category: category });
    });

    updateSummary(total, upCount, downCount, null, pendingCount, Object.keys(groups).length);

    var html = "";
    Object.keys(groups).sort().forEach(function (cat, gi) {
      var items = groups[cat];
      var downs = items.filter(function (i) { return !i.pending && !i.up; }).length;
      html += '<section class="cat-block" aria-label="Kategori ' + esc(cat) + '">';
      html += '<div class="cat-head"><span class="cat-name">' + esc(cat) + '</span><span class="cat-count">' + items.length + " host" + (downs ? " · " + downs + " down" : "") + "</span></div>";
      html += '<div class="host-grid">';
      items.forEach(function (item) {
        var isMaint = !!(window.maintMap && window.maintMap[item.host]);
        var state = item.pending ? "pending" : (item.up ? "up" : "down");
        var latCls = "host-lat is-" + state;
        var latHtml = item.pending
          ? '<span class="' + latCls + '">—</span>'
          : (item.up
            ? '<span class="' + latCls + '">' + Number(item.lat).toFixed(1) + "<small>ms</small></span>"
            : '<span class="' + latCls + '">RTO</span>');
        var search = (item.displayName + " " + item.host + " " + item.category).toLowerCase();
        var maintNote = isMaint ? '<div class="host-note">Mode perawatan — alarm dimatikan</div>' : "";
        var metrics = "";
        if (item.agent) {
          metrics = '<div class="host-meta mono">' +
            "<span>CPU <b>" + esc(item.agent.cpu) + "%</b></span>" +
            "<span>RAM <b>" + esc(item.agent.ram) + "%</b></span>" +
            "<span>Disk <b>" + esc(item.agent.disk) + "%</b></span>" +
            "<span>↓ <b>" + esc(item.agent.net_in || 0) + "</b> ↑ <b>" + esc(item.agent.net_out || 0) + "</b> Mbps</span>" +
            "</div>";
        }
        html += '<article class="host-card" data-host="' + esc(item.host) + '" data-state="' + state + '" data-search="' + esc(search) + '" tabindex="0" role="link" aria-label="' + esc(item.displayName) + ", " + (item.pending ? "menunggu data" : (item.up ? "up" : "down")) + '">' +
          '<div class="host-top"><div style="flex:1;min-width:0">' +
          '<div class="host-name">' + esc(item.displayName) + "</div>" +
          '<div class="host-ip">' + esc(item.host) + "</div>" +
          "</div>" + pillFor(item, isMaint) +
          '<div class="card-menu-wrap"><button class="card-menu-btn" data-menu="' + esc(item.host) + '" aria-label="Menu host ' + esc(item.displayName) + '" aria-haspopup="menu">···</button></div></div>' +
          '<div class="host-main">' + latHtml +
          '<div style="text-align:right"><div class="host-up" data-m="uptime">—</div><div class="host-loss" data-m="loss"></div></div></div>' +
          maintNote + metrics + "</article>";
      });
      html += "</div></section>";
    });
    grid.innerHTML = html;

    grid.querySelectorAll(".host-card").forEach(function (card) {
      card.addEventListener("click", function () {
        window.location.href = "/host/" + encodeURIComponent(card.getAttribute("data-host"));
      });
      card.addEventListener("keydown", function (e) {
        if (e.key === "Enter") window.location.href = "/host/" + encodeURIComponent(card.getAttribute("data-host"));
      });
    });
    grid.querySelectorAll("[data-menu]").forEach(function (btn) {
      btn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        openCardMenu(btn);
      });
    });

    applyHostVisibility();
    buildExportMenu();
  }

  function openCardMenu(btn) {
    closeAllMenus();
    var host = btn.getAttribute("data-menu");
    var pop = document.createElement("div");
    pop.className = "menu-pop open";
    pop.setAttribute("role", "menu");
    pop.style.cssText = "position:absolute;right:0;top:calc(100% + 6px);z-index:120;";
    pop.innerHTML = '<button type="button" data-act="rename">Ubah nama &amp; kategori</button>' +
      '<button type="button" data-act="detail">Buka halaman detail</button>';
    btn.parentElement.appendChild(pop);
    pop.querySelector('[data-act="rename"]').addEventListener("click", function (e) {
      e.stopPropagation();
      pop.remove();
      openRenameModal(host, (window.aliasMap && window.aliasMap[host]) || host, (window.categoryMap && window.categoryMap[host]) || "Uncategorized");
    });
    pop.querySelector('[data-act="detail"]').addEventListener("click", function (e) {
      e.stopPropagation();
      window.location.href = "/host/" + encodeURIComponent(host);
    });
    setTimeout(function () {
      document.addEventListener("click", function h() {
        pop.remove();
        document.removeEventListener("click", h);
      });
    }, 0);
  }

  function applyHostVisibility() {
    var input = $("hostSearch");
    var q = (input && input.value ? input.value : "").toLowerCase().trim();
    var visible = 0, total = 0;
    document.querySelectorAll(".host-card").forEach(function (card) {
      total++;
      var text = card.getAttribute("data-search") || "";
      var okQ = !q || text.indexOf(q) !== -1;
      var okS = hostStateFilter === "all" || card.getAttribute("data-state") === hostStateFilter;
      var show = okQ && okS;
      card.style.display = show ? "" : "none";
      if (show) visible++;
    });
    document.querySelectorAll(".cat-block").forEach(function (g) {
      var any = Array.prototype.some.call(g.querySelectorAll(".host-card"), function (c) { return c.style.display !== "none"; });
      g.style.display = any ? "" : "none";
    });
    updateHostsCount(visible, total);
  }

  function updateHostsCount(visible, total) {
    var el = $("hostsCount");
    if (el) el.textContent = visible === total ? total + " host" : visible + " dari " + total + " host";
    var f = { all: total, up: 0, down: 0 };
    document.querySelectorAll(".host-card").forEach(function (c) {
      var s = c.getAttribute("data-state");
      if (s === "up") f.up++;
      else if (s === "down") f.down++;
    });
    var set = function (id, v) { var n = $(id); if (n) n.textContent = v; };
    set("countAll", f.all); set("countUp", f.up); set("countDown", f.down);
  }

  function setHostFilter(btn, f) {
    document.querySelectorAll("#filterSeg .seg-btn").forEach(function (b) { b.classList.remove("active"); });
    if (btn) btn.classList.add("active");
    hostStateFilter = f;
    applyHostVisibility();
  }

  /* ---------- Statistik uptime ---------- */
  function renderStats(stats) {
    var entries = Object.entries(stats || {});
    var sumPct = 0, counted = 0;
    entries.forEach(function (pair) {
      var host = pair[0], s = pair[1];
      var pct = s.uptime_pct;
      if (pct !== null && pct !== undefined) { sumPct += Number(pct) || 0; counted++; }
      var card = document.querySelector('.host-card[data-host="' + CSS.escape(host) + '"]');
      if (!card) return;
      var up = card.querySelector('[data-m="uptime"]');
      var loss = card.querySelector('[data-m="loss"]');
      if (up) up.textContent = (pct === null || pct === undefined) ? "Uptime —" : "Uptime " + pct + "% / 24j";
      if (loss) {
        var lv = s.avg_loss;
        loss.textContent = (lv === null || lv === undefined) ? "" : "Loss " + lv + "%";
        if (s.avg_ms !== null && s.avg_ms !== undefined) {
          card.title = "Rata-rata " + s.avg_ms + " ms · Min " + s.min_ms + " ms · Maks " + s.max_ms + " ms";
        }
      }
    });
    var avg = counted ? sumPct / counted : null;
    var el = $("sumUptime");
    if (el) el.textContent = avg !== null ? avg.toFixed(1) + "%" : "—";
    recordKpiSnapshot(avg);
  }

  /* ---------- Grafik ---------- */
  function renderChart(labels, datasets, metric) {
    metric = metric || "latency";
    var canvas = $("latencyChart");
    var empty = $("chartEmpty");
    var cfg = METRIC_CONFIG[metric] || METRIC_CONFIG.latency;
    var hasData = datasets && Object.keys(datasets).some(function (h) {
      return (datasets[h] || []).some(function (v) { return v !== -1 && v !== null && v !== undefined; });
    });
    if (!hasData) {
      if (myChart) { myChart.destroy(); myChart = null; }
      canvas.style.display = "none";
      empty.style.display = "flex";
      return;
    }
    canvas.style.display = "";
    empty.style.display = "none";
    var vars = themeVars();
    var grid = (vars.getPropertyValue("--chart-grid") || "rgba(148,163,184,.12)").trim();
    var tick = (vars.getPropertyValue("--chart-tick") || "#8593a6").trim();
    var ctx = canvas.getContext("2d");
    var unit = cfg.unit;
    var isBandwidth = metric === "bandwidth";
    var cds = Object.entries(datasets).map(function (pair, i) {
      var host = pair[0], vals = pair[1];
      var displayName = window.aliasMap && window.aliasMap[host] ? window.aliasMap[host] : host;
      var c = cfg.color || PALETTE[i % PALETTE.length];
      return {
        label: displayName, data: vals.map(function (v) { return v === -1 ? null : v; }),
        borderColor: c, backgroundColor: c + "22", borderWidth: 2, tension: 0.35, fill: !isBandwidth,
        pointRadius: 0, pointHoverRadius: 4, pointHitRadius: 12, spanGaps: true
      };
    });
    if (isBandwidth && window._bwOutData) {
      Object.entries(window._bwOutData.datasets).forEach(function (pair) {
        var host = pair[0], vals = pair[1];
        var displayName = window.aliasMap && window.aliasMap[host] ? window.aliasMap[host] : host;
        cds.push({
          label: displayName + " (keluar)", data: vals, borderColor: "#0ea5e9", backgroundColor: "#0ea5e922",
          borderWidth: 1.5, tension: 0.35, fill: false, pointRadius: 0, pointHoverRadius: 4,
          pointHitRadius: 12, borderDash: [5, 3], spanGaps: true
        });
      });
    }
    var fmtLabels = labels.map(function (l) { return (l || "").split(" ")[1] || l; });
    var chartOpts = {
      responsive: true, maintainAspectRatio: false,
      animation: REDUCED ? false : { duration: 500 },
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          position: "top", align: "end",
          labels: { color: tick, font: { family: "Inter", size: 11 }, boxWidth: 10, boxHeight: 10, borderRadius: 5, padding: 14, usePointStyle: true, pointStyle: "circle" }
        },
        tooltip: {
          backgroundColor: vars.getPropertyValue("--surface").trim() || "#121821",
          borderColor: vars.getPropertyValue("--border").trim() || "#1e2733", borderWidth: 1,
          titleColor: vars.getPropertyValue("--text").trim() || "#e8edf3",
          bodyColor: vars.getPropertyValue("--text-2").trim() || "#a3afbf", padding: 12,
          titleFont: { family: "Inter", size: 12, weight: "600" },
          bodyFont: { family: "JetBrains Mono", size: 11 },
          callbacks: {
            label: function (c) {
              var v = c.parsed.y;
              if (v === null) return "  " + c.dataset.label + ": —";
              return "  " + c.dataset.label + ": " + v.toFixed(2) + " " + unit;
            }
          }
        }
      },
      scales: {
        x: { grid: { color: "transparent" }, border: { display: false }, ticks: { color: tick, font: { family: "JetBrains Mono", size: 10 }, maxRotation: 0, maxTicksLimit: 9 } },
        y: { grid: { color: grid }, border: { display: false }, ticks: { color: tick, font: { family: "JetBrains Mono", size: 10 }, callback: function (v) { return v + " " + unit; } } }
      }
    };
    if (!myChart) {
      if (window.Chart && Chart.defaults) Chart.defaults.font.family = "Inter";
      myChart = new Chart(ctx, { type: "line", data: { labels: fmtLabels, datasets: cds }, options: chartOpts });
    } else {
      myChart.data.labels = fmtLabels;
      myChart.data.datasets = cds;
      myChart.options = chartOpts;
      myChart.update();
    }
  }

  /* ---------- Kejadian down ---------- */
  function renderEvents(events) {
    var body = $("eventsBody");
    var evCount = $("evCount");
    events = Array.isArray(events) ? events : [];
    if (evCount) evCount.textContent = events.length ? events.length + " kejadian" : "";
    if (!events.length) {
      body.innerHTML = '<div class="empty-box" role="status"><b>Belum ada kejadian down</b>Semua host terpantau normal.</div>';
      $("btnShowMore").style.display = "none";
      $("eventsInfo").textContent = "";
      return;
    }
    var shown = events.slice(0, eventsShown);
    var html = '<table class="events-table"><thead><tr><th scope="col">Host</th><th scope="col">Mulai</th><th scope="col">Pulih</th><th scope="col">Durasi</th><th scope="col">Status</th><th scope="col"><span class="sr-only">Aksi</span></th></tr></thead><tbody>';
    shown.forEach(function (e) {
      var displayName = window.aliasMap && window.aliasMap[e.host] ? window.aliasMap[e.host] : e.host;
      var ongoing = e.status === "ongoing";
      html += "<tr>" +
        '<td class="cell-host"><b>' + esc(displayName) + "</b><span>" + esc(e.host) + "</span></td>" +
        '<td class="cell-time">' + esc(e.started_at) + "</td>" +
        '<td class="cell-time">' + esc(ongoing ? "—" : (e.resolved_at || "—")) + "</td>" +
        '<td class="cell-dur">' + esc(ongoing ? "Berjalan" : (e.duration || "—")) + "</td>" +
        "<td>" + (ongoing ? '<span class="badge-ongoing">Berjalan</span>' : '<span class="badge-done">Pulih</span>') + "</td>" +
        "<td>" + '<button type="button" class="row-del" data-event-id="' + Number(e.id) + '" aria-label="Hapus kejadian">' + '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg></button>' + "</td>" +
        "</tr>";
    });
    html += "</tbody></table>";
    body.innerHTML = html;
    body.querySelectorAll(".row-del").forEach(function (btn) {
      btn.addEventListener("click", function () { deleteEvent(btn.getAttribute("data-event-id")); });
    });
    $("eventsInfo").textContent = "Menampilkan " + shown.length + " dari " + events.length;
    $("btnShowMore").style.display = events.length > shown.length ? "" : "none";
  }

  function deleteEvent(id) {
    if (!confirm("Hapus kejadian ini dari riwayat?")) return;
    fetch("/api/events/" + id, { method: "DELETE", headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (res) {
        if (res.ok) { toast("Kejadian dihapus."); refresh(false); }
        else toast("Gagal menghapus kejadian.", "error");
      })
      .catch(function () { toast("Terjadi kesalahan jaringan.", "error"); });
  }

  function clearAllEvents() {
    if (!confirm("Hapus semua kejadian down? Tindakan ini tidak bisa dibatalkan.")) return;
    fetch("/api/events/clear", { method: "POST", headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (res) {
        if (res.ok) { toast("Semua kejadian dihapus."); refresh(false); }
        else toast("Gagal membersihkan kejadian.", "error");
      })
      .catch(function () { toast("Terjadi kesalahan jaringan.", "error"); });
  }

  /* ---------- Ekspor ---------- */
  function buildExportMenu() {
    var menu = $("exportMenu");
    if (!menu) return;
    if (!hosts.length) {
      menu.innerHTML = '<div style="padding:11px 14px;color:var(--text-3);font-size:13px">Belum ada host</div>';
      return;
    }
    menu.innerHTML = "";
    hosts.forEach(function (h) {
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = h + " (24 jam)";
      b.addEventListener("click", function () { doExport(h, 24); closeAllMenus(); });
      menu.appendChild(b);
    });
    var all = document.createElement("button");
    all.type = "button";
    all.textContent = "Semua host (7 hari)";
    all.addEventListener("click", function () { doExport("all", 168); closeAllMenus(); });
    menu.appendChild(all);
  }
  function doExport(host, hours) {
    if (host === "all") { hosts.forEach(function (h) { window.open("/api/export?host=" + encodeURIComponent(h) + "&hours=" + encodeURIComponent(hours)); }); }
    else window.open("/api/export?host=" + encodeURIComponent(host) + "&hours=" + encodeURIComponent(hours));
  }

  /* ---------- Metrik & rentang ---------- */
  function setMetric(btn, metric) {
    document.querySelectorAll("#metricBtns .seg-btn").forEach(function (b) { b.classList.remove("active"); });
    btn.classList.add("active");
    currentMetric = metric;
    var cfg = METRIC_CONFIG[metric];
    if (cfg) {
      $("chartTitle").textContent = cfg.title;
      $("chartSub").textContent = cfg.sub;
    }
    fetchChart();
  }
  function setRange(btn, range) {
    document.querySelectorAll("#rangeBtns .seg-btn").forEach(function (b) { b.classList.remove("active"); });
    btn.classList.add("active");
    currentRange = range;
    fetchChart();
  }

  /* ---------- Notifikasi ---------- */
  var notifEnabled = false;
  try { notifEnabled = localStorage.getItem("nms_notif") === "1"; } catch (_) { notifEnabled = false; }
  var notifiedDown = new Set();
  function toggleNotif() {
    if (!("Notification" in window)) { toast("Browser tidak mendukung notifikasi.", "error"); return; }
    if (Notification.permission === "granted") { notifEnabled = !notifEnabled; updateNotifBtn(); }
    else Notification.requestPermission().then(function (p) {
      notifEnabled = p === "granted";
      updateNotifBtn();
      if (!notifEnabled) toast("Izin notifikasi ditolak browser.", "error");
    });
  }
  function updateNotifBtn() {
    var btn = $("notifBtn");
    if (!btn) return;
    btn.classList.toggle("on", notifEnabled);
    btn.setAttribute("aria-pressed", notifEnabled ? "true" : "false");
    btn.title = notifEnabled ? "Notifikasi browser aktif (klik untuk mematikan)" : "Notifikasi browser mati (klik untuk menyalakan)";
    try {
      if (notifEnabled) localStorage.setItem("nms_notif", "1");
      else localStorage.removeItem("nms_notif");
    } catch (_) {}
  }
  function pushNotifOnce(host, title, bodyMsg) {
    if (!notifEnabled || Notification.permission !== "granted") return;
    if (notifiedDown.has(host)) return;
    notifiedDown.add(host);
    try { new Notification(title, { body: bodyMsg, icon: NOTIF_ICON }); } catch (_) {}
  }

  /* ---------- Fetch ---------- */
  function fetchChart() {
    var cfg = METRIC_CONFIG[currentMetric];
    var labels = { 1: "1 jam", 6: "6 jam", 24: "24 jam" };
    $("chartSub").textContent = cfg.sub + " · " + (labels[currentRange] || (currentRange + " jam"));
    if (currentMetric === "latency") {
      fetch("/api/history?hours=" + currentRange).then(function (r) {
        if (!r.ok) throw new Error();
        return r.json();
      }).then(function (d) {
        renderChart(d.labels || [], d.datasets || {}, "latency");
      }).catch(function (e) { console.error(e); });
    } else if (currentMetric === "bandwidth") {
      Promise.all([
        fetch("/api/agent/history?metric=net_in&hours=" + currentRange),
        fetch("/api/agent/history?metric=net_out&hours=" + currentRange)
      ]).then(function (rs) { return Promise.all(rs.map(function (r) { return r.json(); })); })
        .then(function (arr) {
          window._bwOutData = arr[1];
          renderChart(arr[0].labels || [], arr[0].datasets || {}, "bandwidth");
        }).catch(function (e) { console.error(e); });
    } else {
      fetch("/api/agent/history?metric=" + currentMetric + "&hours=" + currentRange).then(function (r) {
        if (!r.ok) throw new Error();
        return r.json();
      }).then(function (d) {
        renderChart(d.labels || [], d.datasets || {}, currentMetric);
      }).catch(function (e) { console.error(e); });
    }
  }

  function loadAiops(hostList) {
    var body = $("aiopsBody");
    if (!body) return;
    var results = [];
    Promise.all((hostList || []).map(function (h) {
      return fetch("/api/host/" + encodeURIComponent(h.ip) + "/anomalies?hours=24&metric=latency").then(function (r) {
        if (!r.ok) return;
        return r.json();
      }).then(function (d) {
        (d.anomalies || []).slice(-3).forEach(function (a) { results.push({ host: h.alias || h.ip, a: a }); });
      }).catch(function () {});
    })).then(function () {
      results.sort(function (x, y) { return (y.a.score || 0) - (x.a.score || 0); });
      if (!results.length) {
        body.innerHTML = '<div class="anomaly-ok" role="status"><span class="ok-icon">' + ICON_CHECK + '</span>' +
          "<div><b>Tidak ada anomali dalam 24 jam terakhir</b><br><span style=\"color:var(--text-2);font-size:13px\">Semua host berperilaku normal.</span></div></div>";
        return;
      }
      var sevCls = function (s) {
        s = String(s || "").toLowerCase();
        if (/disaster|critical|high/.test(s)) return "sev-high";
        if (/medium|average|warning/.test(s)) return "sev-medium";
        return "sev-low";
      };
      body.innerHTML = '<div class="anomaly-list">' + results.slice(0, 8).map(function (x) {
        return '<div class="anomaly-item"><span class="sev ' + sevCls(x.a.severity) + '">' + esc(x.a.severity || "info") + "</span>" +
          "<b>" + esc(x.host) + '</b><span class="mono">' + esc(x.a.timestamp || "—") + "</span>" +
          '<span class="mono">nilai ' + Number(x.a.value).toFixed(2) + " · baseline " + Number(x.a.baseline).toFixed(2) + " · skor " + Number(x.a.score).toFixed(2) + "</span></div>";
      }).join("") + "</div>";
    });
  }

  function connectTelemetryStream() {
    if (!window.EventSource) return;
    var stream = new EventSource("/api/telemetry/stream");
    stream.addEventListener("telemetry", function (event) {
      try {
        var items = JSON.parse(event.data) || [];
        var live = document.querySelector(".live-badge");
        if (live) live.title = "Aliran telemetri: " + items.length + " host";
      } catch (_) {}
    });
    stream.onerror = function () { stream.close(); setTimeout(connectTelemetryStream, 15000); };
  }

  function refresh(manual) {
    var p = Promise.all([
      fetch("/api/history?hours=1"), fetch("/api/stats"),
      fetch("/api/events"), fetch("/api/agent/metrics"), fetch("/api/hosts"),
      fetch("/api/maintenance?active=1").catch(function () { return null; }),
      fetch("/api/fiber").catch(function () { return null; }),
      fetch("/api/mikrotik").catch(function () { return null; })
    ]).then(function (rs) {
      var mRes = rs[0], sRes = rs[1], eRes = rs[2], aRes = rs[3], hRes = rs[4];
      var maintRes = rs[5], fRes = rs[6], mtRes = rs[7];
      if ([mRes, sRes, eRes, aRes, hRes].some(function (r) { return r.status === 401; })) return null;
      if (!mRes.ok || !sRes.ok || !hRes.ok) throw new Error("HTTP " + mRes.status + "/" + sRes.status + "/" + hRes.status);
      return Promise.all([
        mRes.json(), sRes.json(),
        eRes.ok ? eRes.json() : [],
        aRes.ok ? aRes.json() : {},
        hRes.json()
      ]).then(function (d) {
        return { m: d[0], s: d[1], e: d[2], a: d[3], h: d[4], maintRes: maintRes, fRes: fRes, mtRes: mtRes };
      });
    }).then(function (ctx) {
      if (!ctx) return;
      window.aliasMap = {};
      window.categoryMap = {};
      window.maintMap = {};
      var pr = ctx.maintRes && ctx.maintRes.ok ? ctx.maintRes.json().catch(function () { return []; }) : Promise.resolve([]);
      return pr.then(function (mList) {
        (mList || []).forEach(function (r) { if (r && r.host) window.maintMap[r.host] = r; });
        hosts = [];
        if (ctx.h && ctx.h.forEach) {
          ctx.h.forEach(function (h) {
            var cat = String(h.category || "").trim() || "Tanpa kategori";
            window.aliasMap[h.ip] = (h.alias || "").trim() || h.ip;
            window.categoryMap[h.ip] = cat;
            hosts.push(h.ip);
          });
        }
        hideErr();
        setLiveState(true);
        renderStatus((ctx.m && ctx.m.datasets) || {}, ctx.a || {});
        fetchChart();
        Object.entries(ctx.s || {}).forEach(function (pair) {
          var host = pair[0], s = pair[1];
          if (window.maintMap && window.maintMap[host]) { notifiedDown.delete(host); return; }
          if (s && s.is_down && !s.is_pending) pushNotifOnce(host, "Host down: " + host, host + " tidak dapat dijangkau");
          else notifiedDown.delete(host);
        });
        renderStats(ctx.s || {});
        renderEvents(ctx.e);
        loadAiops(ctx.h || []);
        var chain = Promise.resolve();
        if (ctx.fRes && ctx.fRes.ok) {
          chain = chain.then(function () {
            return ctx.fRes.json().then(function (fData) {
              var n = (Array.isArray(fData) ? fData : []).filter(function (o) { return o && o.severity; }).length;
              var fEl = $("sumFiber");
              if (fEl) {
                fEl.textContent = n;
                $("fiberCard").classList.toggle("is-alert", n > 0);
              }
            }).catch(function () {});
          });
        }
        if (ctx.mtRes && ctx.mtRes.ok) {
          chain = chain.then(function () {
            return ctx.mtRes.json().then(function (mtData) {
              var n = (Array.isArray(mtData) ? mtData : []).filter(function (o) { return o && o.severity; }).length;
              var mEl = $("sumMt");
              if (mEl) {
                mEl.textContent = n;
                $("mtCard").classList.toggle("is-alert", n > 0);
              }
            }).catch(function () {});
          });
        }
        return chain.then(function () {
          $("last-updated").textContent = new Date().toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
          countdownLeft = REFRESH_MS / 1000;
          if (manual) toast("Data berhasil diperbarui.");
        });
      });
    }).catch(function (err) {
      console.error(err);
      showErr("Koneksi ke server gagal: " + err.message);
      setLiveState(false);
      if (manual) toast("Gagal memperbarui data.", "error");
    });
    return p;
  }

  function setLiveState(on) {
    var badge = document.querySelector(".live-badge");
    if (!badge) return;
    badge.classList.toggle("off", !on);
    var label = badge.querySelector("[data-live-label]");
    if (label) label.textContent = on ? "Live" : "Luring";
    badge.title = on ? "Terhubung — refresh otomatis 30 detik" : "Terputus — periksa koneksi ke server";
  }

  /* ---------- Alias ---------- */
  var _renameTargetIp = "";
  function openRenameModal(ip, currentAlias, currentCategory) {
    _renameTargetIp = ip;
    $("renameIpDisplay").textContent = ip;
    var inpAlias = $("renameInput");
    inpAlias.value = currentAlias === ip ? "" : currentAlias;
    inpAlias.placeholder = ip;
    var inpCat = $("renameCategoryInput");
    inpCat.value = currentCategory === "Tanpa kategori" ? "" : currentCategory;
    $("renameModal").classList.add("open");
    setTimeout(function () { inpAlias.focus(); }, 80);
  }
  function closeRenameModal() {
    $("renameModal").classList.remove("open");
    _renameTargetIp = "";
  }
  function saveAlias() {
    var alias = $("renameInput").value.trim();
    var category = $("renameCategoryInput").value.trim() || "Uncategorized";
    if (!_renameTargetIp) return;
    fetch("/api/hosts/" + encodeURIComponent(_renameTargetIp) + "/alias", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ alias: alias, category: category })
    }).then(function (res) {
      if (res.ok) {
        closeRenameModal();
        toast("Nama host disimpan.");
        return loadHosts().then(function () { refresh(false); });
      }
      toast("Gagal menyimpan perubahan.", "error");
    }).catch(function () { toast("Terjadi kesalahan jaringan.", "error"); });
  }
  function clearAlias() {
    $("renameInput").value = "";
    $("renameCategoryInput").value = "Uncategorized";
    saveAlias();
  }

  /* ---------- Kelola host ---------- */
  function openHostModal() {
    $("hostModal").classList.add("open");
    var f = $("hostFilter");
    if (f) f.value = "";
    loadHosts();
  }
  function closeHostModal() { $("hostModal").classList.remove("open"); }
  function filterHostList() {
    var q = ($("hostFilter").value || "").toLowerCase();
    document.querySelectorAll("#hostList .host-item").forEach(function (el) {
      el.style.display = el.textContent.toLowerCase().indexOf(q) !== -1 ? "" : "none";
    });
  }
  function loadHosts() {
    return fetch("/api/hosts").then(function (res) {
      if (!res.ok) return;
      return res.json();
    }).then(function (data) {
      if (!data) return;
      var lst = $("hostList");
      lst.innerHTML = "";
      window.aliasMap = {};
      window.categoryMap = {};
      hosts = [];
      data.forEach(function (h) {
        window.aliasMap[h.ip] = (h.alias || "").trim() || h.ip;
        window.categoryMap[h.ip] = String(h.category || "").trim() || "Uncategorized";
        hosts.push(h.ip);
        var div = document.createElement("div");
        div.className = "host-item";
        var cat = String(h.category || "").trim() || "Uncategorized";
        var alias = (h.alias || "").trim();
        var info = document.createElement("div");
        info.className = "hi-info";
        var title = document.createElement("div");
        title.className = "hi-title";
        title.appendChild(document.createTextNode(h.ip));
        if (alias) {
          var aliasSpan = document.createElement("span");
          aliasSpan.className = "hi-alias";
          aliasSpan.textContent = alias;
          title.appendChild(aliasSpan);
        }
        info.appendChild(title);
        var badges = document.createElement("div");
        badges.className = "hi-badges";
        var catB = document.createElement("span");
        catB.className = "hi-badge";
        catB.textContent = cat;
        badges.appendChild(catB);
        var profB = document.createElement("span");
        profB.className = "hi-badge";
        profB.title = "Profil SNMP";
        profB.textContent = "SNMP " + (h.snmp_profile || "auto");
        badges.appendChild(profB);
        var ifB = document.createElement("span");
        ifB.className = "hi-badge";
        ifB.title = "SNMP Interface Index";
        ifB.textContent = "iface " + (h.if_index || 1);
        badges.appendChild(ifB);
        var snmpVal = h.snmp_community || "";
        var snmpB = document.createElement("span");
        snmpB.className = "hi-badge";
        snmpB.textContent = snmpVal ? "SNMP aktif" : "Tanpa SNMP";
        badges.appendChild(snmpB);
        if (h.backup_enable) {
          var bk = document.createElement("span");
          bk.className = "hi-badge";
          bk.title = "Backup konfigurasi otomatis (02:00)" + (h.backup_last ? " — " + h.backup_last : "");
          bk.textContent = h.backup_ok ? "Backup OK" : "Backup gagal";
          badges.appendChild(bk);
        }
        info.appendChild(badges);
        var av = document.createElement("div");
        av.className = "hi-avatar";
        av.title = cat;
        av.textContent = (alias || h.ip).charAt(0).toUpperCase();
        var acts = document.createElement("div");
        acts.className = "hi-actions";
        var editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "mini-btn";
        editBtn.setAttribute("data-edit", h.ip);
        editBtn.textContent = "Ubah";
        var delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "mini-btn danger";
        delBtn.setAttribute("data-del", h.ip);
        delBtn.textContent = "Hapus";
        acts.appendChild(editBtn);
        acts.appendChild(delBtn);
        div.appendChild(av);
        div.appendChild(info);
        div.appendChild(acts);
        lst.appendChild(div);
      });
      lst.querySelectorAll("[data-edit]").forEach(function (b) {
        b.addEventListener("click", function () {
          var ip = b.getAttribute("data-edit");
          openRenameModal(ip, (window.aliasMap && window.aliasMap[ip]) || ip, (window.categoryMap && window.categoryMap[ip]) || "Uncategorized");
        });
      });
      lst.querySelectorAll("[data-del]").forEach(function (b) {
        b.addEventListener("click", function () { delHost(b.getAttribute("data-del")); });
      });
      var cnt = $("hostCount");
      if (cnt) cnt.textContent = data.length + " host";
      buildExportMenu();
    }).catch(function (e) { console.error(e); });
  }
  function addHost(event) {
    if (event) event.preventDefault();
    var ip = $("newHostIp").value.trim();
    var alias = $("newHostAlias").value.trim();
    var category = $("newHostCategory").value.trim() || "Uncategorized";
    var snmp = $("newHostSnmp").value.trim();
    var idx = parseInt($("newHostIfIndex").value, 10);
    if (!Number.isFinite(idx) || idx < 1) idx = 1;
    var profile = $("newHostProfile").value;
    var sshUser = $("newHostSshUser").value.trim();
    var sshPass = $("newHostSshPass").value;
    var sshPort = parseInt($("newHostSshPort").value, 10);
    if (!Number.isFinite(sshPort) || sshPort < 1 || sshPort > 65535) sshPort = 22;
    var backupEn = $("newHostBackup").checked ? 1 : 0;
    if (backupEn && (!sshUser || !sshPass)) { toast("Backup aktif membutuhkan user dan sandi SSH.", "error"); return; }
    var oidRe = /^\.?([0-9]+\.)+[0-9]+$/;
    var oids = {};
    [["newHostCpuOid", "cpu_oid"], ["newHostMemOid", "mem_oid"], ["newHostStoOid", "storage_oid"], ["newHostTempOid", "temp_oid"]].forEach(function (pair) {
      var v = $(pair[0]).value.trim();
      if (v) {
        if (!oidRe.test(v) || v.length > 128) { toast(pair[1] + " bukan OID yang valid.", "error"); return; }
        oids[pair[1]] = v;
      }
    });
    if (!ip) return;
    var ipv4 = /^(\d{1,3}\.){3}\d{1,3}$/;
    var hostname = /^[a-zA-Z0-9]([a-zA-Z0-9.\-]{0,253}[a-zA-Z0-9])?$/;
    if (ipv4.test(ip)) {
      if (ip.split(".").map(Number).some(function (o) { return o > 255; })) { toast("Format IP tidak valid.", "error"); return; }
    } else if (!hostname.test(ip)) { toast("IP/hostname tidak valid.", "error"); return; }
    var payload = { ip: ip, alias: alias, category: category, snmp_community: snmp, if_index: idx, snmp_profile: profile, ssh_user: sshUser, ssh_port: sshPort, backup_enable: backupEn };
    Object.keys(oids).forEach(function (k) { payload[k] = oids[k]; });
    if (sshPass) payload.ssh_pass = sshPass;
    fetch("/api/hosts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      .then(function (res) {
        if (!res.ok) return res.json().catch(function () { return {}; }).then(function (err) { throw new Error(err.error || "Gagal menambah host"); });
        ["newHostIp", "newHostAlias", "newHostCategory", "newHostSnmp"].forEach(function (id) { $(id).value = ""; });
        $("newHostIfIndex").value = "1";
        $("newHostProfile").value = "auto";
        $("newHostSshUser").value = "";
        $("newHostSshPass").value = "";
        $("newHostSshPort").value = "22";
        $("newHostBackup").checked = false;
        ["newHostCpuOid", "newHostMemOid", "newHostStoOid", "newHostTempOid"].forEach(function (id) { $(id).value = ""; });
        toast("Host berhasil ditambahkan.");
        return loadHosts().then(function () { refresh(false); });
      })
      .catch(function (e) { console.error(e); toast(e.message || "Terjadi kesalahan jaringan.", "error"); });
  }
  function delHost(ip) {
    if (!confirm("Hapus host " + ip + " dari pemantauan?")) return;
    fetch("/api/hosts/" + encodeURIComponent(ip), { method: "DELETE", headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (res) {
        if (res.ok) { toast("Host dihapus."); return loadHosts().then(function () { refresh(false); }); }
        toast("Gagal menghapus host.", "error");
      })
      .catch(function () { toast("Terjadi kesalahan jaringan.", "error"); });
  }

  /* ---------- Pengaturan ---------- */
  function openSettingsModal() {
    $("settingsModal").classList.add("open");
    fetch("/api/settings").then(function (res) { return res.json(); }).then(function (data) {
      $("cpuThreshold").value = data.cpu_threshold;
      $("ramThreshold").value = data.ram_threshold;
      $("diskThreshold").value = data.disk_threshold || 90.0;
      $("fiberOverload").value = data.fiber_rx_overload != null ? data.fiber_rx_overload : -8;
      $("fiberTarget").value = data.fiber_rx_target != null ? data.fiber_rx_target : -18;
      $("fiberWarn").value = data.fiber_rx_warn != null ? data.fiber_rx_warn : -25;
      $("fiberCrit").value = data.fiber_rx_crit != null ? data.fiber_rx_crit : -27;
      $("fiberTxMin").value = data.fiber_tx_min != null ? data.fiber_tx_min : 0;
      $("fiberTxMax").value = data.fiber_tx_max != null ? data.fiber_tx_max : 5;
      $("fiberDegDb").value = data.fiber_degrade_db != null ? data.fiber_degrade_db : 3;
      $("fiberDegDays").value = data.fiber_degrade_days != null ? data.fiber_degrade_days : 7;
      $("fiberStaleMin").value = data.fiber_stale_min != null ? data.fiber_stale_min : 60;
      $("fiberFlapFlips").value = data.fiber_flap_flips != null ? data.fiber_flap_flips : 4;
      $("fiberFlapHours").value = data.fiber_flap_hours != null ? data.fiber_flap_hours : 24;
      $("fiberParentMin").value = data.fiber_parent_min != null ? data.fiber_parent_min : 5;
      $("tempWarn").value = data.temp_threshold != null ? data.temp_threshold : 60;
      $("tempCrit").value = data.temp_crit != null ? data.temp_crit : 75;
      $("mtCpuOid").value = data.mt_cpu_oid || "";
      $("mtMemOid").value = data.mt_mem_oid || "";
      $("mtStoOid").value = data.mt_storage_oid || "";
      $("mtTempOid").value = data.mt_temp_oid || "";
      $("mtTempDiv").value = data.mt_temp_div != null ? data.mt_temp_div : 10;
      $("mtStaleMin").value = data.mt_stale_min != null ? data.mt_stale_min : 15;
      $("mtBackupKeep").value = data.mt_backup_keep != null ? data.mt_backup_keep : 10;
      $("publicUrl").value = data.public_url || "";
    }).catch(function (e) { console.error(e); });
  }
  function closeSettingsModal() { $("settingsModal").classList.remove("open"); }
  function saveSettings(e) {
    e.preventDefault();
    var num = function (id) { return parseFloat($(id).value); };
    var int = function (id) { return parseInt($(id).value, 10); };
    var cpu = num("cpuThreshold"), ram = num("ramThreshold"), disk = num("diskThreshold");
    var checks = [["CPU", cpu], ["RAM", ram], ["Disk", disk]];
    for (var i = 0; i < checks.length; i++) {
      if (!Number.isFinite(checks[i][1]) || checks[i][1] < 1 || checks[i][1] > 100) { toast("Ambang " + checks[i][0] + " harus angka 1–100.", "error"); return; }
    }
    var fOver = num("fiberOverload"), fTgt = num("fiberTarget"), fWarn = num("fiberWarn"), fCrit = num("fiberCrit");
    var fTxMin = num("fiberTxMin"), fTxMax = num("fiberTxMax");
    var franges = [["overload fiber", fOver], ["target fiber", fTgt], ["warning fiber", fWarn], ["critical fiber", fCrit]];
    for (var j = 0; j < franges.length; j++) {
      if (!Number.isFinite(franges[j][1]) || franges[j][1] < -40 || franges[j][1] > 10) { toast(franges[j][0] + " harus angka -40..10 dBm.", "error"); return; }
    }
    if (!(fCrit < fWarn && fWarn <= fOver)) { toast("Urutan harus: critical < warning ≤ overload.", "error"); return; }
    if (!Number.isFinite(fTxMin) || !Number.isFinite(fTxMax) || fTxMin > fTxMax) { toast("Tx min harus ≤ Tx max.", "error"); return; }
    var fDegDb = num("fiberDegDb"), fDegDays = int("fiberDegDays"), fStaleMin = int("fiberStaleMin");
    var fFlapFlips = int("fiberFlapFlips"), fFlapHours = int("fiberFlapHours");
    if (!Number.isFinite(fDegDb) || fDegDb < 0.5 || fDegDb > 10) { toast("Degradasi harus 0,5–10 dB.", "error"); return; }
    if (!Number.isInteger(fDegDays) || fDegDays < 1 || fDegDays > 30) { toast("Jendela degradasi harus 1–30 hari.", "error"); return; }
    if (!Number.isInteger(fStaleMin) || fStaleMin < 10 || fStaleMin > 10080) { toast("Batas stale harus 10–10080 menit.", "error"); return; }
    if (!Number.isInteger(fFlapFlips) || fFlapFlips < 2 || fFlapFlips > 20) { toast("Flap harus 2–20 kali.", "error"); return; }
    if (!Number.isInteger(fFlapHours) || fFlapHours < 1 || fFlapHours > 72) { toast("Jendela flap harus 1–72 jam.", "error"); return; }
    var fParentMin = int("fiberParentMin");
    if (!Number.isInteger(fParentMin) || fParentMin < 2 || fParentMin > 50) { toast("Batas induk massal harus 2–50 ONT.", "error"); return; }
    var tW = num("tempWarn"), tC = num("tempCrit"), tDiv = num("mtTempDiv");
    if (!Number.isFinite(tW) || tW < 0 || tW > 150 || !Number.isFinite(tC) || tC < 0 || tC > 150) { toast("Suhu warning/critical harus 0–150 °C.", "error"); return; }
    if (!(tW < tC)) { toast("Suhu warning harus lebih kecil dari critical.", "error"); return; }
    if (!Number.isFinite(tDiv) || tDiv < 1 || tDiv > 1000) { toast("Pembagi suhu harus 1–1000.", "error"); return; }
    var mtStale = int("mtStaleMin");
    if (!Number.isInteger(mtStale) || mtStale < 3 || mtStale > 1440) { toast("Batas stale MikroTik harus 3–1440 menit.", "error"); return; }
    var mtKeep = int("mtBackupKeep");
    if (!Number.isInteger(mtKeep) || mtKeep < 3 || mtKeep > 50) { toast("Versi backup harus 3–50.", "error"); return; }
    var oidRe = /^\.?([0-9]+\.)+[0-9]+$/;
    var mtOids = {};
    var oidOk = true;
    [["mtCpuOid", "mt_cpu_oid"], ["mtMemOid", "mt_mem_oid"], ["mtStoOid", "mt_storage_oid"], ["mtTempOid", "mt_temp_oid"]].forEach(function (pair) {
      var v = $(pair[0]).value.trim();
      if (v) {
        if (!oidRe.test(v) || v.length > 128) { toast(pair[1] + " bukan OID yang valid.", "error"); oidOk = false; return; }
        mtOids[pair[1]] = v;
      }
    });
    if (!oidOk) return;
    var payload = {
      cpu_threshold: cpu, ram_threshold: ram, disk_threshold: disk,
      fiber_rx_overload: fOver, fiber_rx_target: fTgt, fiber_rx_warn: fWarn, fiber_rx_crit: fCrit,
      fiber_tx_min: fTxMin, fiber_tx_max: fTxMax, fiber_degrade_db: fDegDb, fiber_degrade_days: fDegDays,
      fiber_stale_min: fStaleMin, fiber_flap_flips: fFlapFlips, fiber_flap_hours: fFlapHours,
      fiber_parent_min: fParentMin, temp_threshold: tW, temp_crit: tC, mt_temp_div: tDiv,
      mt_stale_min: mtStale, mt_backup_keep: mtKeep, public_url: $("publicUrl").value.trim()
    };
    Object.keys(mtOids).forEach(function (k) { payload[k] = mtOids[k]; });
    fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      .then(function (res) {
        if (!res.ok) return res.json().catch(function () { return {}; }).then(function (j) { throw new Error(j.error || "Gagal menyimpan pengaturan"); });
        closeSettingsModal();
        toast("Pengaturan berhasil disimpan.");
      })
      .catch(function (e) { console.error(e); toast(e.message || "Terjadi kesalahan jaringan.", "error"); });
  }

  /* ---------- Ganti password ---------- */
  function openPasswordModal() {
    $("passwordModal").classList.add("open");
    ["pwCurrent", "pwNew", "pwNew2", "pwUser"].forEach(function (id) { $(id).value = ""; });
    var e = $("pwError");
    if (e) e.style.display = "none";
    setTimeout(function () { $("pwCurrent").focus(); }, 80);
  }
  function closePasswordModal() { $("passwordModal").classList.remove("open"); }
  function savePassword(e) {
    if (e) e.preventDefault();
    var cur = $("pwCurrent").value;
    var n1 = $("pwNew").value;
    var n2 = $("pwNew2").value;
    var nu = $("pwUser").value.trim();
    var err = $("pwError");
    var fail = function (m) { err.textContent = m; err.style.display = "block"; };
    if (!cur) { fail("Password saat ini wajib diisi."); $("pwCurrent").focus(); return; }
    if (n1.length < 8) { fail("Password baru minimal 8 karakter."); $("pwNew").focus(); return; }
    if (n1 !== n2) { fail("Ulangi password baru tidak sama."); $("pwNew2").focus(); return; }
    if (nu && !/^[a-zA-Z0-9_.\-]{3,50}$/.test(nu)) { fail("Username 3–50 karakter (huruf/angka/_.-)."); $("pwUser").focus(); return; }
    err.style.display = "none";
    var btn = $("btnSavePassword");
    btn.disabled = true;
    btn.textContent = "Menyimpan…";
    fetch("/api/settings/password", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
      body: JSON.stringify({ current_password: cur, new_password: n1, new_username: nu || undefined })
    }).then(function (res) {
      if (!res.ok) {
        return res.json().catch(function () { return {}; }).then(function (j) {
          throw new Error(j.error || "Gagal menyimpan password");
        });
      }
      closePasswordModal();
      toast("Password berhasil diubah. Sesi perangkat lain dikeluarkan.");
    }).catch(function (ex) {
      console.error(ex);
      fail(ex.message || "Terjadi kesalahan jaringan.");
    }).finally(function () {
      btn.disabled = false;
      btn.textContent = "Simpan Password";
    });
  }

  /* ---------- Inisialisasi ---------- */
  function bind(id, ev, fn) {
    var el = $(id);
    if (el) el.addEventListener(ev, fn);
  }

  document.addEventListener("DOMContentLoaded", function () {
    setActiveNav();
    updateNotifBtn();
    try {
      if (localStorage.getItem("nms_nav") === "collapsed" && window.innerWidth > 1023) document.body.classList.add("nav-collapsed");
    } catch (_) {}

    bind("themeBtn", "click", toggleTheme);
    bind("sidebarToggle", "click", toggleSidebar);
    bind("scrim", "click", closeSidebar);
    bind("collapseBtn", "click", toggleCollapse);
    bind("notifBtn", "click", toggleNotif);
    bind("btnRefresh", "click", function () { refresh(true); });
    bind("btnAddHost", "click", openHostModal);
    bind("btnCloseHosts", "click", closeHostModal);
    bind("btnCloseHosts2", "click", closeHostModal);
    bind("btnCloseSettings", "click", closeSettingsModal);
    bind("btnCancelSettings", "click", closeSettingsModal);
    document.querySelectorAll(".modal-close").forEach(function (b) {
      b.addEventListener("click", function () {
        var ov = b.closest(".modal-overlay");
        if (ov) ov.classList.remove("open");
      });
    });
    bind("btnCloseRename", "click", closeRenameModal);
    bind("btnSaveAlias", "click", saveAlias);
    bind("btnClearAlias", "click", clearAlias);
    bind("btnClearEvents", "click", clearAllEvents);
    bind("btnShowMore", "click", function () {
      eventsShown += EVENTS_PAGE;
      var last = window._lastEvents || [];
      renderEvents(last);
    });
    bind("alertToggle", "click", function () {
      var acc = $("troubleshoot");
      var open = acc.classList.toggle("open");
      this.setAttribute("aria-expanded", open ? "true" : "false");
      this.textContent = open ? "Sembunyikan" : "Lihat langkah";
    });
    bind("exportBtn", "click", function (e) {
      e.stopPropagation();
      closeAllMenus();
      $("exportMenu").classList.toggle("open");
    });

    var userBtn = $("userBtn");
    if (userBtn) userBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      closeAllMenus();
      $("userMenu").classList.toggle("open");
    });
    document.addEventListener("click", function () { closeAllMenus(); });
    document.querySelectorAll(".menu-pop").forEach(function (m) {
      m.addEventListener("click", function (e) { e.stopPropagation(); });
    });
    bind("menuSettings", "click", function () { closeAllMenus(); openSettingsModal(); });
    bind("menuHosts", "click", function () { closeAllMenus(); openHostModal(); });
    bind("menuPassword", "click", function () { closeAllMenus(); openPasswordModal(); });
    bind("btnClosePassword", "click", closePasswordModal);
    bind("btnCancelPassword", "click", closePasswordModal);
    bind("passwordForm", "submit", savePassword);

    document.querySelectorAll("#filterSeg .seg-btn").forEach(function (b) {
      b.addEventListener("click", function () { setHostFilter(b, b.getAttribute("data-filter")); });
    });
    document.querySelectorAll("#metricBtns .seg-btn").forEach(function (b) {
      b.addEventListener("click", function () { setMetric(b, b.getAttribute("data-metric")); });
    });
    document.querySelectorAll("#rangeBtns .seg-btn").forEach(function (b) {
      b.addEventListener("click", function () { setRange(b, parseInt(b.getAttribute("data-range"), 10)); });
    });

    bind("hostSearch", "input", applyHostVisibility);
    bind("hostFilter", "input", filterHostList);
    bind("addHostForm", "submit", addHost);
    bind("settingsForm", "submit", saveSettings);

    var rInput = $("renameInput");
    if (rInput) rInput.addEventListener("keydown", function (e) {
      if (e.key === "Enter") saveAlias();
      if (e.key === "Escape") closeRenameModal();
    });
    var rModal = $("renameModal");
    if (rModal) rModal.addEventListener("click", function (e) { if (e.target === rModal) closeRenameModal(); });
    document.querySelectorAll(".modal-overlay").forEach(function (o) {
      o.addEventListener("click", function (e) { if (e.target === o) o.classList.remove("open"); });
    });

    document.addEventListener("keydown", function (e) {
      var tag = (document.activeElement && document.activeElement.tagName) || "";
      var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      if (e.key === "/" && !typing) {
        e.preventDefault();
        var s = $("hostSearch");
        if (s) s.focus();
      } else if ((e.key === "r" || e.key === "R") && !typing) {
        refresh(true);
      } else if (e.key === "Escape") {
        closeHostModal(); closeSettingsModal(); closeRenameModal(); closePasswordModal(); closeSidebar(); closeAllMenus();
      }
    });

    var cd = setInterval(function () {
      if (document.hidden) return;
      countdownLeft = Math.max(0, countdownLeft - 1);
      var el = $("countdown");
      if (el) el.textContent = "Refresh dalam " + countdownLeft + " dtk";
    }, 1000);

    refresh(false).then(function () { sorotFromQuery(); });
    renderKpiTrends();
    connectTelemetryStream();
    setInterval(function () { refresh(false); }, REFRESH_MS);
  });

  /* Patch kecil: simpan events terakhir untuk tombol show-more */
  var _renderEvents = renderEvents;
  renderEvents = function (events) {
    window._lastEvents = Array.isArray(events) ? events : [];
    _renderEvents(window._lastEvents);
  };
})();
