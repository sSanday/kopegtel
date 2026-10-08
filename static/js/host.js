/* ============================================================
   NMS Kopegtel — Halaman detail host (hanya lapisan tampilan).
   Membaca API yang sudah ada, tidak mengubah backend.
   ============================================================ */
(function () {
  "use strict";

  var HOST_IP = (document.body.getAttribute("data-host-ip") || "").trim();
  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (REDUCED) document.body.classList.add("reduced-motion");

  var METRIC_CONFIG = {
    latency: { label: "Latensi (Ping)", unit: "ms",   color: "#0ea5e9", metricKey: "latency" },
    cpu:     { label: "CPU",            unit: "%",    color: "#f43f5e", metricKey: "cpu" },
    ram:     { label: "RAM",            unit: "%",    color: "#f59e0b", metricKey: "ram" },
    disk:    { label: "Disk",           unit: "%",    color: "#a78bfa", metricKey: "disk" },
    bw:      { label: "Bandwidth",      unit: "Mbps", color: "#14b8a6", metricKey: "net_in" }
  };

  var PERIODS = [
    { hours: 6,   id: "6h",   title: "6 Jam Terakhir", sub: "Sampel 30 detik",  fmtFn: function (t) { return (t.split(" ")[1] || t).slice(0, 5); } },
    { hours: 24,  id: "24h",  title: "24 Jam Terakhir", sub: "Rata-rata 5 menit", fmtFn: function (t) { return (t.split(" ")[1] || t).slice(0, 5); } },
    { hours: 72,  id: "72h",  title: "3 Hari Terakhir", sub: "Rata-rata 15 menit", fmtFn: function (t) { var d = new Date(t); return (d.getMonth() + 1) + "/" + d.getDate() + " " + String(d.getHours()).padStart(2, "0") + ":00"; } },
    { hours: 168, id: "168h", title: "7 Hari Terakhir", sub: "Rata-rata 30 menit",  fmtFn: function (t) { var d = new Date(t); return ["Min", "Sen", "Sel", "Rab", "Kam", "Jum", "Sab"][d.getDay()] + " " + String(d.getHours()).padStart(2, "0") + ":00"; } }
  ];

  var charts = {};
  var currentMetric = "latency";

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function themeVars() { return getComputedStyle(document.documentElement); }

  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("nms_theme", theme); } catch (_) {}
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#0b0f14" : "#eef1f6");
  }

  function buildChart(canvasId, labels, values, cfg, fmtFn) {
    var cv = $(canvasId);
    if (!cv) return null;
    var vars = themeVars();
    var grid = (vars.getPropertyValue("--chart-grid") || "rgba(148,163,184,.12)").trim();
    var tick = (vars.getPropertyValue("--chart-tick") || "#8593a6").trim();
    return new Chart(cv.getContext("2d"), {
      type: "line",
      data: {
        labels: labels.map(fmtFn),
        datasets: [{
          label: cfg.label,
          data: values,
          borderColor: cfg.color,
          backgroundColor: cfg.color + "22",
          borderWidth: 2,
          tension: 0.35,
          fill: true,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointHitRadius: 12,
          spanGaps: true
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: REDUCED ? false : { duration: 400 },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: vars.getPropertyValue("--surface").trim() || "#121821",
            borderColor: vars.getPropertyValue("--border").trim() || "#1e2733", borderWidth: 1,
            titleColor: vars.getPropertyValue("--text").trim() || "#e8edf3",
            bodyColor: vars.getPropertyValue("--text-2").trim() || "#a3afbf", padding: 10,
            titleFont: { family: "JetBrains Mono", size: 10 },
            bodyFont: { family: "JetBrains Mono", size: 11 },
            callbacks: {
              label: function (c) {
                var v = c.parsed.y;
                if (v === null) return "  —";
                return "  " + v.toFixed(2) + " " + cfg.unit;
              }
            }
          }
        },
        scales: {
          x: { grid: { color: "transparent" }, border: { display: false }, ticks: { color: tick, font: { family: "JetBrains Mono", size: 9 }, maxRotation: 0, maxTicksLimit: 8, autoSkip: true } },
          y: { grid: { color: grid }, border: { display: false }, ticks: { color: tick, font: { family: "JetBrains Mono", size: 9 }, callback: function (v) { return v + " " + cfg.unit; } } }
        }
      }
    });
  }

  function computeStats(values) {
    var valid = values.filter(function (v) { return v !== null && v !== undefined && !isNaN(v); });
    if (!valid.length) return { max: null, avg: null, cur: null };
    return {
      max: Math.max.apply(null, valid),
      avg: valid.reduce(function (a, b) { return a + b; }, 0) / valid.length,
      cur: valid[valid.length - 1]
    };
  }

  function updatePanelStats(periodId, values, unit) {
    var s = computeStats(values);
    var fmt = function (v) { return v !== null ? v.toFixed(2) + " " + unit : "—"; };
    var set = function (id, v) { var el = $(id); if (el) el.textContent = v; };
    set("s" + periodId + "-maxin", fmt(s.max));
    set("s" + periodId + "-avgin", fmt(s.avg));
    set("s" + periodId + "-curin", fmt(s.cur));
  }

  function loadPeriod(period, metric) {
    var cfg = METRIC_CONFIG[metric];
    var loadEl = $("load-" + period.id);
    var canvasEl = $("chart-" + period.id);
    if (loadEl) { loadEl.textContent = "Memuat data…"; loadEl.style.display = "flex"; }
    if (canvasEl) canvasEl.style.display = "none";
    if (charts[period.id]) { charts[period.id].destroy(); charts[period.id] = null; }
    fetch("/api/host/" + encodeURIComponent(HOST_IP) + "/history?hours=" + period.hours + "&metric=" + cfg.metricKey)
      .then(function (res) { return res.json(); })
      .then(function (data) {
        var values = data.values || [];
        var labels = data.labels || [];
        var hasData = values.some(function (v) { return v !== null && v !== undefined; });
        if (!labels.length || !hasData) {
          if (loadEl) { loadEl.textContent = "Belum ada data pada rentang ini."; loadEl.style.display = "flex"; }
          updatePanelStats(period.id, [], cfg.unit);
          return;
        }
        if (loadEl) loadEl.style.display = "none";
        if (canvasEl) canvasEl.style.display = "block";
        charts[period.id] = buildChart("chart-" + period.id, labels, values, cfg, period.fmtFn);
        updatePanelStats(period.id, values, cfg.unit);
      })
      .catch(function (e) {
        console.error(e);
        if (loadEl) { loadEl.textContent = "Gagal memuat data."; loadEl.style.display = "flex"; }
      });
  }

  function loadAllCharts() {
    var cfg = METRIC_CONFIG[currentMetric];
    PERIODS.forEach(function (p) {
      var titleEl = $("title-" + p.id);
      if (titleEl) titleEl.textContent = cfg.label + " · " + p.title;
      loadPeriod(p, currentMetric);
    });
  }

  function setChartMetric(metric, btn) {
    document.querySelectorAll("#metricSeg .seg-btn").forEach(function (b) { b.classList.remove("active"); });
    if (btn) btn.classList.add("active");
    currentMetric = metric;
    loadAllCharts();
  }

  function meterColor(v) {
    v = parseFloat(v) || 0;
    if (v >= 90) return "var(--down-text)";
    if (v >= 70) return "var(--warn-text)";
    return "var(--ok-text)";
  }

  function loadStats() {
    fetch("/api/host/" + encodeURIComponent(HOST_IP) + "/stats")
      .then(function (res) { return res.json(); })
      .then(function (d) {
        var pill = $("statusPill");
        var txt = $("statusText");
        var state = d.is_pending ? "pending" : (d.is_up ? "up" : "down");
        if (pill) pill.className = "pill pill-" + state;
        if (txt) txt.textContent = d.is_pending ? "Menunggu" : (d.is_up ? "Up" : "Down");

        var fmtMs = function (v) { return v === null || v === undefined ? "—" : v + "<small>ms</small>"; };
        var fmtPct = function (v) { return v === null || v === undefined ? "—" : v + "<small>%</small>"; };
        var setHtml = function (id, v) { var el = $(id); if (el) el.innerHTML = v; };
        if (d.is_pending && d.latest_ms === null) {
          setHtml("statLatency", "Menunggu");
        } else {
          setHtml("statLatency", (d.latest_ms !== null && d.latest_ms !== undefined) ? d.latest_ms + "<small>ms</small>" : "RTO");
        }
        setHtml("statAvg", fmtMs(d.avg_ms));
        setHtml("statMin", fmtMs(d.min_ms));
        setHtml("statMax", fmtMs(d.max_ms));
        setHtml("statLoss", fmtPct(d.avg_loss));

        var pct = d.uptime_pct;
        var vars = themeVars();
        var color = (pct === null || pct === undefined)
          ? vars.getPropertyValue("--text-3").trim()
          : pct >= 99 ? vars.getPropertyValue("--ok-text").trim()
          : pct >= 95 ? vars.getPropertyValue("--warn-text").trim()
          : vars.getPropertyValue("--down-text").trim();
        var pctEl = $("uptimePct");
        if (pctEl) { pctEl.textContent = (pct === null || pct === undefined) ? "—" : pct + "%"; pctEl.style.color = color; }
        var bar = $("uptimeBar");
        if (bar) { bar.style.width = (pct === null || pct === undefined ? 0 : pct) + "%"; bar.style.background = color; }
        var verdict = $("upVerdict");
        if (verdict) {
          verdict.textContent = (pct === null || pct === undefined) ? "Belum ada data" : pct >= 99 ? "Sehat" : pct >= 95 ? "Stabil" : "Perlu perhatian";
          verdict.style.color = color;
          verdict.style.borderColor = color;
        }
        var setT = function (id, v) { var el = $(id); if (el) el.textContent = v; };
        setT("u-avg", (d.avg_ms === null || d.avg_ms === undefined) ? "—" : d.avg_ms + " ms");
        setT("u-min", (d.min_ms === null || d.min_ms === undefined) ? "—" : d.min_ms + " ms");
        setT("u-max", (d.max_ms === null || d.max_ms === undefined) ? "—" : d.max_ms + " ms");
        setT("u-loss", (d.avg_loss === null || d.avg_loss === undefined) ? "—" : d.avg_loss + " %");

        var ag = $("agentGrid");
        if (ag) {
          if (d.agent) {
            ag.style.display = "grid";
            [["agCpu", "agCpuBar", d.agent.cpu, "%"], ["agRam", "agRamBar", d.agent.ram, "%"], ["agDisk", "agDiskBar", d.agent.disk, "%"]].forEach(function (r) {
              var v = parseFloat(r[2]) || 0;
              var vEl = $(r[0]), bEl = $(r[1]);
              if (vEl) { vEl.textContent = v + r[3]; vEl.style.color = meterColor(v); }
              if (bEl) { bEl.style.width = Math.min(100, v) + "%"; bEl.style.background = meterColor(v); }
            });
            var ni = $("agNetIn"), no = $("agNetOut");
            if (ni) ni.textContent = d.agent.net_in + " Mbps";
            if (no) no.textContent = d.agent.net_out + " Mbps";
          } else {
            ag.style.display = "none";
          }
        }
        var lu = $("lastUpdated");
        if (lu) lu.textContent = new Date().toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      })
      .catch(function (e) { console.error(e); });
  }

  function loadHostEvents() {
    var body = $("hostEvents");
    if (!body) return;
    fetch("/api/events", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (events) {
        var rows = (Array.isArray(events) ? events : []).filter(function (e) { return e.host === HOST_IP; }).slice(0, 5);
        if (!rows.length) {
          body.innerHTML = '<p style="color:var(--text-2);font-size:13.5px">Tidak ada kejadian tercatat untuk host ini.</p>';
          return;
        }
        body.innerHTML = rows.map(function (e) {
          var ongoing = e.status === "ongoing";
          return '<div class="mini-ev">' +
            '<span class="mono">' + esc(e.started_at) + "</span>" +
            (ongoing ? '<span class="badge-ongoing">Berjalan</span>' : '<span class="badge-done">Pulih</span>') +
            '<span class="mono">' + esc(ongoing ? "berjalan" : (e.duration || "—")) + "</span></div>";
        }).join("");
      })
      .catch(function (e) { console.error(e); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var tb = $("themeBtn");
    if (tb) tb.addEventListener("click", function () {
      var cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
      setTheme(cur === "dark" ? "light" : "dark");
      loadAllCharts();
    });
    var cp = $("copyIpBtn");
    if (cp) cp.addEventListener("click", function () {
      var done = function () { cp.textContent = "Disalin"; setTimeout(function () { cp.textContent = "Salin"; }, 1500); };
      if (navigator.clipboard) navigator.clipboard.writeText(HOST_IP).then(done).catch(function () { cp.textContent = HOST_IP; });
      else cp.textContent = HOST_IP;
    });
    document.querySelectorAll("#metricSeg .seg-btn").forEach(function (b) {
      b.addEventListener("click", function () { setChartMetric(b.getAttribute("data-metric"), b); });
    });
    var rb = $("btnReloadCharts");
    if (rb) rb.addEventListener("click", loadAllCharts);
    document.addEventListener("keydown", function (e) {
      var tag = (document.activeElement && document.activeElement.tagName) || "";
      if ((e.key === "r" || e.key === "R") && tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") {
        loadStats();
        loadAllCharts();
      }
    });
    if (window.Chart && Chart.defaults) Chart.defaults.font.family = "Inter";
    loadStats();
    loadAllCharts();
    loadHostEvents();
    setInterval(function () { loadStats(); loadHostEvents(); }, 30000);
  });
})();
