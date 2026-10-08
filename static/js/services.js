/* ============================================================
   NMS Kopegtel — Services (hanya lapisan tampilan).
   Membaca API yang sudah ada, tidak mengubah backend.
   ============================================================ */
(function () {
  "use strict";

  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (REDUCED) document.body.classList.add("reduced-motion");

  var ICON_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>';
  var ICON_ALERT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 17H2L12 3z"/><path d="M12 10v4M12 17.5v.5"/></svg>';
  var ICON_MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 13.5A8 8 0 0 1 10.5 4 8 8 0 1 0 20 13.5z"/></svg>';
  var ICON_SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  var ICON_COPY = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 012-2h10"/></svg>';
  var ICON_CHART = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17l5-5 3 3 5-6 5 5"/><path d="M3 21h18"/></svg>';

  var allServices = [];
  var statusFilter = "all";
  var typeFilter = "all";
  var searchQuery = "";
  var svcChart = null;
  var histHours = 1;
  var lastFetch = 0;

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function themeVars() { return getComputedStyle(document.documentElement); }

  function toast(msg, type) {
    var wrap = $("toasts");
    if (!wrap) return;
    var el = document.createElement("div");
    el.className = "toast " + (type === "error" ? "error" : "success");
    el.setAttribute("role", "status");
    el.innerHTML = (type === "error" ? ICON_ALERT : ICON_CHECK) + "<span>" + esc(msg) + "</span>";
    wrap.appendChild(el);
    setTimeout(function () { el.style.opacity = "0"; setTimeout(function () { el.remove(); }, 250); }, 3500);
  }

  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { localStorage.setItem("nms_theme", theme); } catch (_) {}
    var btn = $("themeBtn");
    if (btn) {
      btn.innerHTML = theme === "dark" ? ICON_SUN : ICON_MOON;
      btn.title = theme === "dark" ? "Ganti ke mode terang" : "Ganti ke mode gelap";
      btn.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
    }
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#0b0f14" : "#eef1f6");
  }

  /* ---------- Waktu relatif ---------- */
  function parseLocal(s) {
    if (!s) return null;
    var d = new Date(String(s).replace(" ", "T"));
    return isNaN(d.getTime()) ? null : d;
  }
  function relTime(s) {
    var d = parseLocal(s);
    if (!d) return "—";
    var ms = Date.now() - d.getTime();
    if (ms < 0) ms = 0;
    var sec = Math.floor(ms / 1000);
    if (sec < 10) return "baru saja";
    if (sec < 60) return sec + " dtk lalu";
    var m = Math.floor(sec / 60);
    if (m < 60) return m + " mnt lalu";
    var h = Math.floor(m / 60);
    if (h < 24) return h + " jam lalu";
    return Math.floor(h / 24) + " hari lalu";
  }

  /* ---------- SSL ---------- */
  function sslBadge(s) {
    var isHttps = String(s.type || "").toLowerCase() === "http" && String(s.url || "").toLowerCase().indexOf("https://") === 0;
    if (!isHttps) return '<span class="b-mut">—</span>';
    var d = s.ssl_days_left;
    if (d === null || d === undefined) return '<span class="b-mut" title="SSL belum dicek — dicek otomatis tiap 6 jam">Belum dicek</span>';
    var n = Number(d);
    var exp = esc(s.ssl_expires_at || "");
    if (n < 0) return '<span class="sev-tag disaster" title="Kedaluwarsa sejak ' + exp + '">Kedaluwarsa</span>';
    if (n <= 7) return '<span class="sev-tag disaster" title="Kedaluwarsa ' + exp + '">Kritis · ' + n + ' hari</span>';
    if (n <= 30) return '<span class="sev-tag warning" title="Kedaluwarsa ' + exp + '">' + n + ' hari lagi</span>';
    return '<span class="b-ok" title="Kedaluwarsa ' + exp + '">Berlaku ' + n + ' hari</span>';
  }

  /* ---------- Filter ---------- */
  function getFiltered() {
    var q = (searchQuery || "").toLowerCase().trim();
    return (allServices || []).filter(function (s) {
      var st = String(s.status || "pending").toLowerCase();
      if (statusFilter !== "all" && st !== statusFilter) return false;
      if (typeFilter !== "all" && String(s.type || "").toLowerCase() !== typeFilter) return false;
      if (q) {
        var target = s.type === "tcp" ? s.ip + ":" + (s.port != null ? s.port : "-") : (s.url || "");
        var hay = (s.name + " " + s.ip + " " + target + " " + s.type).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });
  }

  function syncResetBtn() {
    var btn = $("resetFilter");
    if (btn) btn.classList.toggle("on", statusFilter !== "all" || typeFilter !== "all" || !!searchQuery);
  }

  /* ---------- Render ---------- */
  function animCount(el, to) {
    if (!el) return;
    if (REDUCED) { el.textContent = to; return; }
    var from = parseInt(el.textContent) || 0;
    if (from === to) { el.textContent = to; return; }
    var start = null;
    function frame(t) {
      if (!start) start = t;
      var p = Math.min(1, (t - start) / 350);
      el.textContent = Math.round(from + (to - from) * p);
      if (p < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function targetOf(s) {
    return s.type === "tcp" ? s.ip + ":" + (s.port != null ? s.port : "-") : (s.url || "-");
  }

  function renderServices() {
    var on = 0, off = 0, pen = 0;
    (allServices || []).forEach(function (s) {
      var st = String(s.status || "pending").toLowerCase();
      if (st === "online") on++;
      else if (st === "offline") off++;
      else pen++;
    });
    animCount($("cnt-online"), on);
    animCount($("cnt-offline"), off);
    animCount($("cnt-pending"), pen);

    var rows = getFiltered();
    var tbody = $("servicesBody");

    if (!allServices || !allServices.length) {
      tbody.innerHTML = '<tr><td colspan="7"><div class="empty-box" role="status">' + ICON_CHART +
        '<b>Belum ada service yang dipantau</b>Tambahkan service pertama untuk mulai memantau ketersediaan dan latency.<br>' +
        '<button type="button" class="btn btn-primary" id="btnEmptyAdd" style="margin-top:14px">+ Tambah service pertama</button></div></td></tr>';
      var ea = $("btnEmptyAdd");
      if (ea) ea.addEventListener("click", openModal);
    } else if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="7"><div class="empty-box" role="status"><b>Tidak ada service yang cocok</b>Coba ubah filter atau klik Reset filter.</div></td></tr>';
    } else {
      var html = "";
      rows.forEach(function (s) {
        var st = String(s.status || "PENDING").toLowerCase();
        var lat = (st === "online" && s.latency !== null && s.latency !== undefined)
          ? Number(s.latency).toFixed(1) + " ms" : "—";
        var tgt = targetOf(s);
        var pillCls = st === "online" ? "b-ok" : (st === "offline" ? "b-bad" : "b-warn");
        var pillTxt = st === "online" ? "Online" : (st === "offline" ? "Offline" : "Pending");
        html += '<tr data-sid="' + Number(s.id) + '" tabindex="0" role="button" aria-label="' + esc(s.name) + ", " + pillTxt + '">' +
          '<td data-label="Service" class="a-host"><b>' + esc(s.name) + '</b><span class="a-ip">' + esc(String(s.type || "").toUpperCase()) + " · #" + Number(s.id) + "</span></td>" +
          '<td data-label="Tipe"><span class="b-type">' + esc(String(s.type || "").toUpperCase()) + "</span></td>" +
          '<td data-label="Target"><span class="cell-time tgt" title="' + esc(tgt) + '">' + esc(tgt) + '</span><button type="button" class="copy-btn" data-copy="' + esc(tgt) + '" aria-label="Salin target">' + ICON_COPY + "</button></td>" +
          '<td data-label="Status"><span class="' + pillCls + '">' + pillTxt + "</span></td>" +
          '<td data-label="Latency" class="cell-time">' + esc(lat) + "</td>" +
          '<td data-label="SSL">' + sslBadge(s) + "</td>" +
          '<td data-label="Cek terakhir" title="' + esc(s.last_checked || "Belum pernah dicek") + '">' + relTime(s.last_checked) + "</td>" +
          '<td data-label="Aksi"><div class="card-menu-wrap"><button type="button" class="card-menu-btn" data-menu="' + Number(s.id) + '" aria-label="Menu aksi service" aria-haspopup="menu">···</button></div></td>' +
          "</tr>";
      });
      tbody.innerHTML = html;
      tbody.querySelectorAll("tr[data-sid]").forEach(function (tr) {
        tr.addEventListener("click", function () { selectSvcHistory(Number(tr.getAttribute("data-sid"))); });
        tr.addEventListener("keydown", function (e) { if (e.key === "Enter") selectSvcHistory(Number(tr.getAttribute("data-sid"))); });
      });
      tbody.querySelectorAll("[data-copy]").forEach(function (b) {
        b.addEventListener("click", function (e) {
          e.stopPropagation();
          var t = b.getAttribute("data-copy");
          var done = function () { toast("Target disalin."); };
          if (navigator.clipboard) navigator.clipboard.writeText(t).then(done).catch(function () { toast(t); });
          else toast(t);
        });
      });
      tbody.querySelectorAll("[data-menu]").forEach(function (b) {
        b.addEventListener("click", function (e) {
          e.stopPropagation();
          openRowMenu(b, Number(b.getAttribute("data-menu")));
        });
      });
    }
    syncHistSelect();
  }

  function openRowMenu(btn, sid) {
    document.querySelectorAll(".menu-pop.open").forEach(function (m) { m.remove(); });
    var s = allServices.find(function (x) { return Number(x.id) === sid; });
    if (!s) return;
    var pop = document.createElement("div");
    pop.className = "menu-pop open";
    pop.setAttribute("role", "menu");
    var isHttps = String(s.type || "").toLowerCase() === "http" && String(s.url || "").toLowerCase().indexOf("https://") === 0;
    pop.innerHTML = (isHttps ? '<button type="button" data-act="ssl">Cek SSL sekarang</button>' : "") +
      '<button type="button" data-act="del" class="danger">Hapus service</button>';
    btn.parentElement.appendChild(pop);
    var sslBtn = pop.querySelector('[data-act="ssl"]');
    if (sslBtn) sslBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      pop.remove();
      fetch("/api/services/" + sid + "/ssl-check", { method: "POST", headers: { "X-Requested-With": "XMLHttpRequest" } })
        .then(function (r) {
          toast(r.ok ? "Cek SSL dijadwalkan." : "Gagal menjadwalkan cek SSL.", r.ok ? "success" : "error");
        }).catch(function () { toast("Terjadi kesalahan jaringan.", "error"); });
    });
    pop.querySelector('[data-act="del"]').addEventListener("click", function (e) {
      e.stopPropagation();
      pop.remove();
      deleteService(sid, s.name);
    });
    setTimeout(function () {
      document.addEventListener("click", function h() { pop.remove(); document.removeEventListener("click", h); });
    }, 0);
  }

  /* ---------- Riwayat latency ---------- */
  function syncHistSelect() {
    var sel = $("histSvc");
    if (!sel) return;
    var cur = sel.value;
    var cur2 = $("histSvc2") ? $("histSvc2").value : "";
    var opts = (allServices || []).map(function (s) { return '<option value="' + Number(s.id) + '">' + esc(s.name) + " (#" + Number(s.id) + ")</option>"; }).join("");
    sel.innerHTML = '<option value="">— Pilih service —</option>' + opts;
    if (cur && Array.from(sel.options).some(function (o) { return o.value === cur; })) sel.value = cur;
    var sel2 = $("histSvc2");
    if (sel2) {
      sel2.innerHTML = '<option value="">+ Bandingkan…</option>' + opts;
      if (cur2 && cur2 !== sel.value && Array.from(sel2.options).some(function (o) { return o.value === cur2; })) sel2.value = cur2;
    }
    updateHistEmpty();
  }

  function updateHistEmpty() {
    var sel = $("histSvc");
    var empty = $("histEmpty");
    var wrap = $("histCanvasWrap");
    if (!sel.value) {
      empty.style.display = "flex";
      empty.innerHTML = ICON_CHART + "<b>" + (allServices.length ? "Pilih service untuk melihat riwayat latency" : "Belum ada service yang dipantau") + "</b>" +
        (allServices.length ? "<span>Pilih dari dropdown di kanan atas.</span>" : "<span>Tambah service dulu untuk mulai merekam riwayat.</span>");
      wrap.style.display = "none";
      $("histSummary").style.display = "none";
      lastData = null;
      setExportEnabled(false);
      return;
    }
    empty.style.display = "none";
    wrap.style.display = "";
  }

  function selectSvcHistory(id) {
    var sel = $("histSvc");
    if (sel) {
      sel.value = String(id);
      fetchSvcHistory();
    }
    var sec = $("histSection");
    if (sec) sec.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "center" });
  }

  function setHistRange(btn, h) {
    histHours = h;
    document.querySelectorAll("#rangeSeg .seg-btn").forEach(function (b) { b.classList.remove("active"); });
    if (btn) btn.classList.add("active");
    fetchSvcHistory();
  }

  function alignSeries(labelsA, labelsB, valuesB) {
    var map = {};
    labelsB.forEach(function (l, i) { map[l] = valuesB[i]; });
    return labelsA.map(function (l) { return (l in map) ? map[l] : null; });
  }

  function fetchSvcHistory() {
    var sel = $("histSvc");
    var sid = sel ? sel.value : null;
    updateHistEmpty();
    if (!sid) return;
    var sel2 = $("histSvc2");
    var sid2 = sel2 ? sel2.value : "";
    if (sid2 === sid) sid2 = "";
    var reqs = [fetch("/api/services/" + encodeURIComponent(sid) + "/history?hours=" + histHours)];
    if (sid2) reqs.push(fetch("/api/services/" + encodeURIComponent(sid2) + "/history?hours=" + histHours));
    Promise.all(reqs).then(function (rs) {
      if (rs.some(function (r) { return r.status === 401; })) return null;
      return Promise.all(rs.map(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      }));
    }).then(function (arr) {
      if (!arr) return;
      var j = arr[0];
      var jB = arr[1] || null;
      var title = $("histTitle");
      if (title && j.service) title.textContent = "Riwayat Latency — " + j.service.name + (jB && jB.service ? " vs " + jB.service.name : "");
      var sub = $("histSub");
      if (sub) sub.textContent = "Rentang " + histHours + " jam · " + (j.count || 0) + " titik · OFFLINE ditampilkan sebagai gap + penanda merah";
      var series = [{ name: j.service ? j.service.name : "Service", values: j.values || [], color: "#14b8a6" }];
      if (jB && jB.service) {
        series.push({
          name: jB.service.name,
          values: alignSeries(j.labels || [], jB.labels || [], jB.values || []),
          color: "#a78bfa"
        });
      }
      drawSvcChart(j.labels || [], series, j.count || 0, !!jB);
    }).catch(function (e) {
      console.error(e);
      toast("Gagal memuat riwayat.", "error");
    });
  }

  var lastData = null;

  function setExportEnabled(on) {
    var a = $("btnExpPng"), b = $("btnExpCsv");
    if (a) a.disabled = !on;
    if (b) b.disabled = !on;
  }

  function drawSvcChart(labels, series, count, isCompare) {
    var canvas = $("svcChart");
    var empty = $("histEmpty");
    var wrap = $("histCanvasWrap");
    var summary = $("histSummary");
    var primary = series[0];
    var hasData = series.some(function (s) {
      return s.values.some(function (v) { return v !== null && v !== undefined; });
    });

    if (!count || !hasData) {
      if (svcChart) { svcChart.destroy(); svcChart = null; }
      wrap.style.display = "none";
      summary.style.display = "none";
      lastData = null;
      setExportEnabled(false);
      empty.style.display = "flex";
      empty.innerHTML = ICON_CHART + "<b>Belum ada data pada rentang ini</b><span>Coba perpanjang rentang waktu atau tunggu pengecekan berikutnya.</span>";
      return;
    }
    empty.style.display = "none";
    wrap.style.display = "";

    function statOf(values) {
      var vals = values.filter(function (v) { return v !== null && v !== undefined; });
      if (!vals.length) return null;
      return {
        avg: vals.reduce(function (a, b) { return a + b; }, 0) / vals.length,
        min: Math.min.apply(null, vals),
        max: Math.max.apply(null, vals),
        uptime: Math.round((vals.length / values.length) * 100)
      };
    }
    var stA = statOf(primary.values);
    if (isCompare) {
      var stB = statOf(series[1].values);
      summary.style.display = "flex";
      summary.innerHTML =
        '<span class="hs"><span class="k">' + esc(primary.name) + '</span><br><span class="v">' + (stA ? stA.avg.toFixed(1) + " ms · up " + stA.uptime + "%" : "—") + "</span></span>" +
        '<span class="hs"><span class="k">' + esc(series[1].name) + '</span><br><span class="v">' + (stB ? stB.avg.toFixed(1) + " ms · up " + stB.uptime + "%" : "—") + "</span></span>";
    } else {
      summary.style.display = "flex";
      summary.innerHTML =
        '<span class="hs"><span class="k">Rata-rata</span><br><span class="v">' + stA.avg.toFixed(1) + " ms</span></span>" +
        '<span class="hs"><span class="k">Min</span><br><span class="v">' + stA.min.toFixed(1) + " ms</span></span>" +
        '<span class="hs"><span class="k">Maks</span><br><span class="v">' + stA.max.toFixed(1) + " ms</span></span>" +
        '<span class="hs"><span class="k">Uptime</span><br><span class="v">' + stA.uptime + "%</span></span>";
    }

    lastData = {
      labels: labels, nameA: primary.name, valuesA: primary.values,
      nameB: isCompare ? series[1].name : null,
      valuesB: isCompare ? series[1].values : null
    };
    setExportEnabled(true);

    var vars = themeVars();
    var grid = (vars.getPropertyValue("--chart-grid") || "rgba(148,163,184,.12)").trim();
    var tick = (vars.getPropertyValue("--chart-tick") || "#8593a6").trim();
    var downColor = (vars.getPropertyValue("--down-text") || "#f87171").trim();

    var datasets = series.map(function (s) {
      return {
        label: s.name,
        data: s.values,
        borderColor: s.color,
        backgroundColor: s.color + "22",
        fill: !isCompare,
        tension: 0.35,
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        pointHitRadius: 12,
        spanGaps: false
      };
    });
    series.forEach(function (s) {
      datasets.push({
        label: "OFFLINE",
        data: s.values.map(function (v) { return v === null || v === undefined ? 0 : null; }),
        borderColor: "transparent",
        backgroundColor: downColor,
        pointStyle: "rectRot",
        pointRadius: 5,
        pointHoverRadius: 6,
        pointHitRadius: 10,
        showLine: false,
        spanGaps: true,
        isOfflineMarker: true
      });
    });

    var ctx = canvas.getContext("2d");
    if (svcChart) svcChart.destroy();
    svcChart = new Chart(ctx, {
      type: "line",
      data: { labels: labels, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: REDUCED ? false : { duration: 400 },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: isCompare,
            position: "top", align: "end",
            labels: { color: tick, font: { family: "Inter", size: 11 }, boxWidth: 10, boxHeight: 10, borderRadius: 5, padding: 12, usePointStyle: true, pointStyle: "circle",
              filter: function (item) { return item.text !== "OFFLINE"; } }
          },
          tooltip: {
            backgroundColor: vars.getPropertyValue("--surface").trim() || "#121821",
            borderColor: vars.getPropertyValue("--border").trim() || "#1e2733", borderWidth: 1,
            titleColor: vars.getPropertyValue("--text").trim() || "#e8edf3",
            bodyColor: vars.getPropertyValue("--text-2").trim() || "#a3afbf", padding: 10,
            titleFont: { family: "JetBrains Mono", size: 10 },
            bodyFont: { family: "JetBrains Mono", size: 11 },
            filter: function (item) { return !(item.dataset.isOfflineMarker && item.parsed.y !== 0); },
            callbacks: {
              label: function (c) {
                if (c.dataset.isOfflineMarker) return "  " + (isCompare ? c.dataset.label + " — " : "") + "OFFLINE";
                var v = c.parsed.y;
                return v === null ? null : "  " + (isCompare ? c.dataset.label + ": " : "") + v.toFixed(2) + " ms";
              }
            }
          }
        },
        scales: {
          x: { grid: { color: "transparent" }, border: { display: false }, ticks: { color: tick, font: { family: "JetBrains Mono", size: 9 }, maxRotation: 0, maxTicksLimit: 9 } },
          y: { beginAtZero: true, grid: { color: grid }, border: { display: false }, ticks: { color: tick, font: { family: "JetBrains Mono", size: 9 }, callback: function (v) { return v + " ms"; } } }
        }
      }
    });
  }

  /* ---------- Ekspor riwayat ---------- */
  function exportPng() {
    if (!svcChart || !lastData) return;
    var src = svcChart.canvas;
    var tmp = document.createElement("canvas");
    tmp.width = src.width;
    tmp.height = src.height;
    var ctx = tmp.getContext("2d");
    ctx.fillStyle = themeVars().getPropertyValue("--surface").trim() || "#121821";
    ctx.fillRect(0, 0, tmp.width, tmp.height);
    ctx.drawImage(src, 0, 0);
    var a = document.createElement("a");
    a.href = tmp.toDataURL("image/png");
    a.download = "latency-" + lastData.nameA.replace(/\s+/g, "-").toLowerCase() + "-" + histHours + "h.png";
    document.body.appendChild(a);
    a.click();
    a.remove();
    toast("Grafik diekspor sebagai PNG.");
  }

  function exportCsv() {
    if (!lastData) return;
    var hdr = ["timestamp", lastData.nameA + "_latency_ms", lastData.nameA + "_status"];
    if (lastData.nameB) hdr.push(lastData.nameB + "_latency_ms", lastData.nameB + "_status");
    var lines = [hdr.join(",")];
    lastData.labels.forEach(function (l, i) {
      var va = lastData.valuesA[i];
      var row = [l, va == null ? "" : va, va == null ? "OFFLINE" : "ONLINE"];
      if (lastData.nameB) {
        var vb = lastData.valuesB[i];
        row.push(vb == null ? "" : vb, vb == null ? "OFFLINE" : "ONLINE");
      }
      lines.push(row.join(","));
    });
    var blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = "latency-" + lastData.nameA.replace(/\s+/g, "-").toLowerCase() + "-" + histHours + "h.csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    toast("Data riwayat diekspor sebagai CSV.");
  }

  /* ---------- Data ---------- */
  function fetchServices(manual) {
    var btn = $("btnRefresh");
    if (btn) btn.disabled = true;
    return fetch("/api/services", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (res) {
        if (res.status === 401) return null;
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      }).then(function (data) {
        if (data === null) return;
        allServices = Array.isArray(data) ? data : [];
        renderServices();
        lastFetch = Date.now();
        updateUpdatedNote();
        var sel = $("histSvc");
        if (sel && sel.value) fetchSvcHistory();
        if (manual) toast("Daftar service diperbarui.");
      }).catch(function (e) {
        console.error(e);
        toast("Gagal memuat service: " + e.message, "error");
      }).finally(function () {
        if (btn) btn.disabled = false;
      });
  }

  function updateUpdatedNote() {
    var el = $("updatedNote");
    if (!el || !lastFetch) return;
    var s = Math.max(0, Math.round((Date.now() - lastFetch) / 1000));
    el.textContent = s < 5 ? "Diperbarui baru saja" : s < 60 ? "Diperbarui " + s + " detik lalu" : "Diperbarui " + Math.floor(s / 60) + " menit lalu";
  }

  /* ---------- Tambah / hapus ---------- */
  function openModal() {
    $("modalOverlay").classList.add("open");
    toggleFields();
    setTimeout(function () { $("svcName").focus(); }, 60);
  }
  function closeModal() {
    $("modalOverlay").classList.remove("open");
    $("addForm").reset();
    toggleFields();
    var err = $("formError");
    if (err) err.style.display = "none";
    var btn = $("submitBtn");
    if (btn) btn.disabled = false;
  }
  function toggleFields() {
    var t = $("svcType").value;
    var isTcp = t === "tcp";
    $("portGroup").style.display = isTcp ? "" : "none";
    $("urlGroup").style.display = isTcp ? "none" : "";
    $("svcPort").required = isTcp;
    $("svcUrl").required = !isTcp;
  }
  function submitForm(e) {
    e.preventDefault();
    var err = $("formError");
    var fail = function (m) { err.textContent = m; err.style.display = "block"; };
    var name = $("svcName").value.trim();
    var type = $("svcType").value;
    var ip = $("svcIp").value.trim();
    var port = $("svcPort").value;
    var url = $("svcUrl").value.trim();
    if (!name) { fail("Nama service wajib diisi."); $("svcName").focus(); return; }
    if (!ip) { fail("Host IP / domain wajib diisi."); $("svcIp").focus(); return; }
    var payload = { name: name, type: type, ip: ip };
    if (type === "tcp") {
      var p = parseInt(port, 10);
      if (!Number.isFinite(p) || p < 1 || p > 65535) { fail("TCP Port harus angka 1–65535."); $("svcPort").focus(); return; }
      payload.port = p;
      payload.url = "";
    } else {
      if (!/^https?:\/\/\S+$/i.test(url)) { fail("URL harus diawali http(s)://"); $("svcUrl").focus(); return; }
      payload.url = url;
      payload.port = "";
    }
    var btn = $("submitBtn");
    btn.disabled = true;
    btn.textContent = "Menyimpan…";
    fetch("/api/services", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
      body: JSON.stringify(payload)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || "Gagal menambah service");
        closeModal();
        toast("Service '" + name + "' ditambahkan.");
        fetchServices(false);
      });
    }).catch(function (ex) { fail(ex.message); })
      .finally(function () { btn.disabled = false; btn.textContent = "Simpan"; });
  }

  function deleteService(id, name) {
    if (!confirm("Hapus service '" + (name || id) + "'? Riwayat latency-nya ikut terhapus.")) return;
    fetch("/api/services/" + encodeURIComponent(id), { method: "DELETE", headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) {
        if (!r.ok && r.status !== 404) {
          return r.json().catch(function () { return {}; }).then(function (j) { throw new Error(j.error || "Gagal menghapus service"); });
        }
        toast("Service '" + (name || id) + "' dihapus.");
        var sel = $("histSvc");
        if (sel && Number(sel.value) === Number(id)) { sel.value = ""; updateHistEmpty(); }
        fetchServices(false);
      }).catch(function (e) { toast(e.message, "error"); });
  }

  /* ---------- Focus trap modal ---------- */
  function trapFocus(modal, e) {
    if (e.key !== "Tab") return;
    var els = modal.querySelectorAll("button, input, select, textarea, [tabindex]");
    if (!els.length) return;
    var first = els[0], last = els[els.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  /* ---------- Init ---------- */
  function bind(id, ev, fn) { var el = $(id); if (el) el.addEventListener(ev, fn); }

  document.addEventListener("DOMContentLoaded", function () {
    setTheme(document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark");
    bind("themeBtn", "click", function () {
      var cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
      setTheme(cur === "dark" ? "light" : "dark");
      if ($("histSvc") && $("histSvc").value) fetchSvcHistory();
    });
    bind("sidebarToggle", "click", function () { document.body.classList.toggle("nav-open"); });
    bind("scrim", "click", function () { document.body.classList.remove("nav-open"); });

    bind("btnRefresh", "click", function () { fetchServices(true); });
    bind("btnAdd", "click", openModal);

    document.querySelectorAll("#statusSeg .seg-btn").forEach(function (b) {
      b.addEventListener("click", function () {
        document.querySelectorAll("#statusSeg .seg-btn").forEach(function (x) { x.classList.remove("active"); });
        b.classList.add("active");
        statusFilter = b.getAttribute("data-status");
        syncResetBtn();
        renderServices();
      });
    });
    ["online", "offline", "pending"].forEach(function (st) {
      var chip = $("chip-" + st);
      if (chip) chip.addEventListener("click", function () {
        statusFilter = statusFilter === st ? "all" : st;
        document.querySelectorAll("#statusSeg .seg-btn").forEach(function (x) {
          x.classList.toggle("active", x.getAttribute("data-status") === statusFilter);
        });
        syncResetBtn();
        renderServices();
      });
    });
    bind("typeSelect", "change", function () { typeFilter = this.value; syncResetBtn(); renderServices(); });
    bind("resetFilter", "click", function () {
      statusFilter = "all"; typeFilter = "all"; searchQuery = "";
      $("svcSearch").value = "";
      $("typeSelect").value = "all";
      document.querySelectorAll("#statusSeg .seg-btn").forEach(function (x) {
        x.classList.toggle("active", x.getAttribute("data-status") === "all");
      });
      syncResetBtn();
      renderServices();
    });

    var si = $("svcSearch");
    var deb = null;
    if (si) si.addEventListener("input", function () {
      clearTimeout(deb);
      deb = setTimeout(function () { searchQuery = si.value; syncResetBtn(); renderServices(); }, 180);
    });

    bind("histSvc", "change", fetchSvcHistory);
    bind("histSvc2", "change", fetchSvcHistory);
    bind("btnExpPng", "click", exportPng);
    bind("btnExpCsv", "click", exportCsv);
    document.querySelectorAll("#rangeSeg .seg-btn").forEach(function (b) {
      b.addEventListener("click", function () { setHistRange(b, parseInt(b.getAttribute("data-h"), 10)); });
    });

    bind("addForm", "submit", submitForm);
    bind("svcType", "change", toggleFields);
    bind("btnCloseModal", "click", closeModal);
    bind("btnCancelModal", "click", closeModal);
    var ov = $("modalOverlay");
    if (ov) {
      ov.addEventListener("click", function (e) { if (e.target === ov) closeModal(); });
      ov.addEventListener("keydown", function (e) { trapFocus(ov, e); });
    }

    document.addEventListener("keydown", function (e) {
      var tag = (document.activeElement && document.activeElement.tagName) || "";
      var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      if (e.key === "/" && !typing) { e.preventDefault(); if (si) si.focus(); }
      else if (e.key === "Escape") { closeModal(); document.body.classList.remove("nav-open"); }
    });

    setInterval(updateUpdatedNote, 10000);
    fetchServices(false);
    setInterval(function () { fetchServices(false); }, 10000);
  });
})();
