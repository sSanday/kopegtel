/* ============================================================
   NMS Kopegtel — Alarm Aktif & Maintenance (hanya tampilan).
   Membaca API yang sudah ada, tidak mengubah backend.
   ============================================================ */
(function () {
  "use strict";

  var REFRESH_TRIGGERS_MS = 10000;
  var REFRESH_MAINT_MS = 30000;
  var SEV_ORDER = { disaster: 1, high: 2, warning: 3 };
  var SEV_LABEL = { disaster: "Disaster", high: "High", warning: "Warning" };
  var CAT_LABEL = {
    availability: "Ketersediaan", agent: "Agent", resource: "Resource",
    service: "Layanan", maintenance: "Maintenance", fiber: "Fiber", mikrotik: "MikroTik"
  };
  var MSG_ID = {
    "Host is DOWN (Unreachable)": "Host tidak dapat dijangkau (RTO)",
    "NMS Agent is Offline / Not reporting": "Agent NMS offline / tidak melapor",
    "High CPU Usage": "Penggunaan CPU tinggi",
    "High RAM Usage": "Penggunaan RAM tinggi",
    "High Disk Usage": "Penggunaan disk tinggi"
  };

  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (REDUCED) document.body.classList.add("reduced-motion");

  var aliasMap = {};
  var allAlarms = [];
  var sevSet = new Set();
  var catFilter = "all";
  var searchQuery = "";
  var curPage = 1;
  var pageSize = 25;
  var lastFetch = 0;
  var firstLoad = true;

  var ICON_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>';
  var ICON_ALERT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 17H2L12 3z"/><path d="M12 10v4M12 17.5v.5"/></svg>';
  var ICON_MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 13.5A8 8 0 0 1 10.5 4 8 8 0 1 0 20 13.5z"/></svg>';
  var ICON_SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function apiHeaders() { return { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" }; }

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

  function showErr(msg) { $("errMsg").textContent = msg; $("errBanner").classList.add("on"); }
  function hideErr() { $("errBanner").classList.remove("on"); }

  /* ---------- Filter ---------- */
  function initFilterFromURL() {
    try {
      var p = new URLSearchParams(window.location.search || "");
      var raw = (p.get("severity") || "").toLowerCase();
      sevSet = new Set(raw.split(",").map(function (s) { return s.trim(); }).filter(function (s) { return SEV_ORDER[s]; }));
      var cat = (p.get("cat") || p.get("category") || "all").toLowerCase();
      catFilter = CAT_LABEL[cat] || cat === "all" ? cat : "all";
      searchQuery = (p.get("q") || "").toLowerCase().trim();
    } catch (_) { sevSet = new Set(); catFilter = "all"; searchQuery = ""; }
  }

  function syncFilterUI() {
    document.querySelectorAll("#sevSeg .seg-btn").forEach(function (b) {
      var sev = b.getAttribute("data-sev");
      b.classList.toggle("active", sev === "all" ? sevSet.size === 0 : sevSet.has(sev));
    });
    ["disaster", "high", "warning"].forEach(function (s) {
      var chip = $("chip-" + s);
      if (chip) chip.classList.toggle("active", sevSet.size === 1 && sevSet.has(s));
    });
    var sel = $("catSelect");
    if (sel) sel.value = catFilter;
    var si = $("hostSearch");
    if (si && document.activeElement !== si) si.value = searchQuery;
    var reset = $("resetFilter");
    var active = sevSet.size > 0 || catFilter !== "all" || !!searchQuery;
    if (reset) reset.classList.toggle("on", active);
    try {
      var p = new URLSearchParams();
      if (sevSet.size) p.set("severity", Array.from(sevSet).sort().join(","));
      if (catFilter !== "all") p.set("cat", catFilter);
      if (searchQuery) p.set("q", searchQuery);
      var qs = p.toString();
      history.replaceState(null, "", window.location.pathname + (qs ? "?" + qs : ""));
    } catch (_) {}
  }

  function setSevFilter(sev, exclusive) {
    if (sev === "all") sevSet.clear();
    else if (exclusive) {
      if (sevSet.size === 1 && sevSet.has(sev)) sevSet.clear();
      else { sevSet.clear(); sevSet.add(sev); }
    } else {
      if (sevSet.has(sev)) sevSet.delete(sev);
      else sevSet.add(sev);
    }
    curPage = 1;
    syncFilterUI();
    renderTable();
  }

  function getFiltered() {
    var q = (searchQuery || "").toLowerCase().trim();
    return (allAlarms || []).filter(function (a) {
      var sev = String(a.severity || "").toLowerCase();
      if (sevSet.size && !sevSet.has(sev)) return false;
      if (catFilter !== "all" && String(a.category || "").toLowerCase() !== catFilter) return false;
      if (q) {
        var hn = String(aliasMap[a.host] || a.host || "").toLowerCase();
        var hay = (a.host + " " + hn + " " + (a.message || "") + " " + (a.category || "")).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    }).sort(function (x, y) {
      return (SEV_ORDER[x.severity] || 4) - (SEV_ORDER[y.severity] || 4);
    });
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

  function catName(c) { return CAT_LABEL[c] || (c ? c.charAt(0).toUpperCase() + c.slice(1) : "—"); }
  function msgText(m) { return MSG_ID[m] || m; }

  function renderTable() {
    var counts = { disaster: 0, high: 0, warning: 0 };
    (allAlarms || []).forEach(function (a) {
      var s = String(a.severity || "").toLowerCase();
      if (counts[s] !== undefined) counts[s]++;
    });
    animCount($("cnt-disaster"), counts.disaster);
    animCount($("cnt-high"), counts.high);
    animCount($("cnt-warn"), counts.warning);

    var filtered = getFiltered();
    var totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
    if (curPage > totalPages) curPage = totalPages;
    if (curPage < 1) curPage = 1;
    var start = (curPage - 1) * pageSize;
    var rows = filtered.slice(start, start + pageSize);
    var tbody = $("triggersBody");

    if (!filtered.length) {
      var filtering = sevSet.size > 0 || catFilter !== "all" || !!searchQuery;
      tbody.innerHTML = '<tr><td colspan="5">' +
        '<div class="empty-box" role="status"><b>' + (filtering ? "Tidak ada alarm yang cocok" : "Semua sistem normal") + "</b>" +
        (filtering ? "Coba ubah filter atau klik Reset filter." : "Tidak ada alarm aktif saat ini. Jaringan berjalan normal.") +
        "</div></td></tr>";
    } else {
      var html = "";
      rows.forEach(function (a) {
        var sev = String(a.severity || "").toLowerCase();
        var name = aliasMap[a.host] || a.host;
        var ack = !!a.acknowledged_at;
        var ackInfo = ack
          ? '<span class="ack-badge">' + ICON_CHECK + "Di-ack " + (a.acknowledged_by ? "oleh " + esc(a.acknowledged_by) : "") + (a.acknowledged_at ? " · " + esc(a.acknowledged_at) : "") + "</span>"
          : "";
        var assignInfo = a.assigned_to ? '<div style="font-size:11.5px;color:var(--text-2);margin-top:5px">PIC: ' + esc(a.assigned_to) + "</div>" : "";
        html += '<tr class="sev-' + esc(sev) + '">' +
          '<td data-label="Severity"><span class="sev-tag ' + esc(sev) + '">' + esc(SEV_LABEL[sev] || sev) + "</span></td>" +
          '<td data-label="Host" class="a-host"><a href="/host/' + encodeURIComponent(a.host) + '"><b>' + esc(name) + "</b></a>" +
            (name !== a.host ? '<span class="a-ip">' + esc(a.host) + "</span>" : "") + "</td>" +
          '<td data-label="Kategori"><span class="a-cat">' + esc(catName(String(a.category || "").toLowerCase())) + "</span></td>" +
          '<td data-label="Masalah"><div class="a-msg">' + esc(msgText(a.message || "")) + "</div></td>" +
          '<td data-label="Workflow"><div class="a-actions">' +
            '<button type="button" class="act-btn ' + (ack ? "" : "primary") + '" data-ack="' + Number(a.id) + '" data-state="' + (ack ? "1" : "0") + '">' + (ack ? "Batalkan ack" : "Acknowledge") + "</button>" +
            '<button type="button" class="act-btn icon" data-assign="' + Number(a.id) + '" aria-label="Tugaskan atau beri catatan">⋯</button>' +
            "</div>" + ackInfo + assignInfo + "</td>" +
          "</tr>";
      });
      tbody.innerHTML = html;
      tbody.querySelectorAll("[data-ack]").forEach(function (b) {
        b.addEventListener("click", function () {
          toggleAck(Number(b.getAttribute("data-ack")), b.getAttribute("data-state") !== "1");
        });
      });
      tbody.querySelectorAll("[data-assign]").forEach(function (b) {
        b.addEventListener("click", function () {
          var a = findAlarm(Number(b.getAttribute("data-assign")));
          if (a) openAssignModal(a);
        });
      });
    }

    var info = $("pageInfo");
    if (info) info.textContent = "Halaman " + curPage + " dari " + totalPages + " · " + filtered.length + " alarm";
    var prev = $("prevBtn"), next = $("nextBtn");
    if (prev) prev.disabled = curPage <= 1;
    if (next) next.disabled = curPage >= totalPages;
  }

  function findAlarm(id) {
    for (var i = 0; i < allAlarms.length; i++) if (Number(allAlarms[i].id) === id) return allAlarms[i];
    return null;
  }

  /* ---------- Data ---------- */
  function fetchTriggers(manual) {
    var btn = $("btnRefresh");
    if (btn) btn.classList.add("loading");
    return fetch("/api/triggers", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (res) {
        if (res.status === 401) return null;
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      }).then(function (data) {
        if (data === null) return;
        allAlarms = Array.isArray(data) ? data : [];
        hideErr();
        renderTable();
        lastFetch = Date.now();
        updateUpdatedNote();
        if (manual) toast("Daftar alarm diperbarui.");
      }).catch(function (e) {
        console.error(e);
        showErr("Gagal memuat alarm: " + e.message);
        if (manual) toast("Gagal memuat alarm.", "error");
      }).finally(function () {
        if (btn) btn.classList.remove("loading");
      });
  }

  function updateUpdatedNote() {
    var el = $("updatedNote");
    if (!el || !lastFetch) return;
    var s = Math.max(0, Math.round((Date.now() - lastFetch) / 1000));
    var txt;
    if (s < 5) txt = "Diperbarui baru saja";
    else if (s < 60) txt = "Diperbarui " + s + " detik lalu";
    else txt = "Diperbarui " + Math.floor(s / 60) + " menit lalu";
    el.textContent = txt;
  }

  function loadHosts() {
    return fetch("/api/hosts", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (res) { return res.ok ? res.json() : []; })
      .then(function (data) {
        data.forEach(function (h) { aliasMap[h.ip] = (h.alias || "").trim() || h.ip; });
        var dl = $("maintHostList");
        if (!dl) return;
        var opts = data.map(function (h) { return '<option value="' + esc(h.ip) + '">' + esc(aliasMap[h.ip]) + "</option>"; }).join("");
        return fetch("/api/fiber", { headers: { "X-Requested-With": "XMLHttpRequest" } })
          .then(function (r) { return r.ok ? r.json() : []; })
          .then(function (onts) {
            opts += (Array.isArray(onts) ? onts : []).map(function (o) { return '<option value="' + esc(o.ont_sn) + '">ONT ' + esc(o.customer || "") + "</option>"; }).join("");
            return fetch("/api/odps", { headers: { "X-Requested-With": "XMLHttpRequest" } });
          })
          .then(function (r) { return r.ok ? r.json() : []; })
          .then(function (odps) {
            opts += (Array.isArray(odps) ? odps : []).filter(function (o) { return o.id; }).map(function (o) { return '<option value="ODP:' + esc(o.name) + '">ODP · ' + esc(o.total || 0) + " ONT</option>"; }).join("");
            return fetch("/api/olts", { headers: { "X-Requested-With": "XMLHttpRequest" } });
          })
          .then(function (r) { return r.ok ? r.json() : []; })
          .then(function (olts) {
            opts += (Array.isArray(olts) ? olts : []).map(function (o) { return '<option value="OLT:' + esc(o.name) + '">OLT · ' + esc(o.ip || "") + "</option>"; }).join("");
            dl.innerHTML = opts;
          })
          .catch(function () { dl.innerHTML = opts; });
      }).catch(function () {});
  }

  /* ---------- Workflow ---------- */
  function updateWorkflow(id, payload) {
    return fetch("/api/alerts/" + id + "/workflow", { method: "PATCH", headers: apiHeaders(), body: JSON.stringify(payload) })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
          return data;
        });
      });
  }
  function toggleAck(id, acknowledged) {
    updateWorkflow(id, { acknowledged: acknowledged })
      .then(function () {
        toast(acknowledged ? "Alarm di-acknowledge." : "Acknowledge dibatalkan.");
        fetchTriggers(false);
      })
      .catch(function (e) { toast(e.message, "error"); });
  }

  /* ---------- Modal Tugaskan / Catatan ---------- */
  var assignTarget = null;
  function openAssignModal(alarm) {
    assignTarget = alarm;
    $("assignAssignee").value = alarm.assigned_to || "";
    $("assignNote").value = alarm.note || "";
    $("assignHost").textContent = (aliasMap[alarm.host] || alarm.host);
    $("assignModal").classList.add("open");
    setTimeout(function () { $("assignAssignee").focus(); }, 80);
  }
  function closeAssignModal() {
    $("assignModal").classList.remove("open");
    assignTarget = null;
  }
  function saveAssign() {
    if (!assignTarget) return;
    var payload = { assigned_to: $("assignAssignee").value.trim(), note: $("assignNote").value.trim() };
    var btn = $("btnAssignSave");
    btn.disabled = true;
    updateWorkflow(assignTarget.id, payload)
      .then(function () {
        closeAssignModal();
        toast("Catatan workflow tersimpan.");
        fetchTriggers(false);
      })
      .catch(function (e) { toast(e.message, "error"); })
      .finally(function () { btn.disabled = false; });
  }
  function trapFocus(modal, e) {
    if (e.key !== "Tab") return;
    var els = modal.querySelectorAll("button, input, textarea, select, [tabindex]");
    if (!els.length) return;
    var first = els[0], last = els[els.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  /* ---------- Maintenance ---------- */
  function parseLocal(s) {
    if (!s) return null;
    var d = new Date(String(s).replace(" ", "T"));
    return isNaN(d.getTime()) ? null : d;
  }
  function fmtRemain(ms) {
    if (!ms || ms <= 0) return null;
    var m = Math.round(ms / 60000);
    if (m < 1) return "kurang dari 1 menit";
    if (m < 60) return m + " menit";
    var h = Math.floor(m / 60), rm = m % 60;
    if (h < 24) return rm ? h + " jam " + rm + " mnt" : h + " jam";
    var d = Math.floor(h / 24), rh = h % 24;
    return rh ? d + " hari " + rh + " jam" : d + " hari";
  }
  function isFinishedMaint(r) {
    var end = parseLocal(r.end_at);
    return !r.is_active && end && end.getTime() <= Date.now();
  }
  function maintStatus(r) {
    var start = parseLocal(r.start_at);
    var end = parseLocal(r.end_at);
    var now = Date.now();
    if (r.is_active) {
      var cd = end && end.getTime() > now ? '<div class="cell-sub" data-end="' + esc(r.end_at) + '">Berakhir dalam …</div>' : "";
      return '<span class="st-badge live">Sedang berlangsung</span>' + cd;
    }
    if (start && start.getTime() > now) {
      return '<span class="st-badge sched">Dijadwalkan</span><div class="cell-sub" data-start="' + esc(r.start_at) + '">Mulai dalam …</div>';
    }
    return '<span class="st-badge done">Selesai</span>';
  }
  function updateMaintCountdowns() {
    document.querySelectorAll("[data-end]").forEach(function (el) {
      var end = parseLocal(el.getAttribute("data-end"));
      var t = end ? fmtRemain(end.getTime() - Date.now()) : null;
      el.textContent = t ? "Berakhir dalam " + t : "Segera berakhir";
    });
    document.querySelectorAll("[data-start]").forEach(function (el) {
      var start = parseLocal(el.getAttribute("data-start"));
      var t = start ? fmtRemain(start.getTime() - Date.now()) : null;
      el.textContent = t ? "Mulai dalam " + t : "Segera dimulai";
    });
  }
  function maintRowHtml(r) {
    return "<tr" + (r.is_active ? ' class="mnt-live"' : "") + ">" +
      '<td data-label="Target" class="a-host"><b>' + esc(r.host) + "</b></td>" +
      '<td data-label="Mulai" class="cell-time">' + esc(r.start_at) + "</td>" +
      '<td data-label="Selesai" class="cell-time">' + esc(r.end_at) + "</td>" +
      '<td data-label="Status">' + maintStatus(r) + "</td>" +
      '<td data-label="Alasan" class="a-msg">' + esc(r.reason || "—") + "</td>" +
      '<td data-label="Aksi"><button type="button" class="row-del" data-del="' + Number(r.id) + '" aria-label="Batalkan jadwal">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13"/></svg></button></td>' +
      "</tr>";
  }
  function bindMaintDeletes(tb) {
    tb.querySelectorAll("[data-del]").forEach(function (b) {
      b.addEventListener("click", function () { deleteMaintenance(Number(b.getAttribute("data-del"))); });
    });
  }
  function toLocalInput(d) {
    var pad = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function setDefaultMaintTimes() {
    var s = $("maintStart"), e = $("maintEnd");
    if (!s || !e) return;
    if (!s.value) {
      var now = new Date(Math.ceil(Date.now() / (5 * 60000)) * (5 * 60000));
      s.value = toLocalInput(now);
      if (!e.value) e.value = toLocalInput(new Date(now.getTime() + 60 * 60000));
    } else if (!e.value) {
      var st = new Date(s.value);
      if (!isNaN(st.getTime())) e.value = toLocalInput(new Date(st.getTime() + 60 * 60000));
    }
  }
  function applyPreset(min, btn) {
    var s = $("maintStart"), e = $("maintEnd");
    if (!s.value) setDefaultMaintTimes();
    var st = new Date(s.value);
    if (isNaN(st.getTime())) return;
    e.value = toLocalInput(new Date(st.getTime() + min * 60000));
    document.querySelectorAll("#presetRow .preset-btn").forEach(function (b) { b.classList.remove("active"); });
    btn.classList.add("active");
    $("presetHint").textContent = "Selesai: " + e.value.replace("T", " ");
  }
  function clearPreset() {
    document.querySelectorAll("#presetRow .preset-btn").forEach(function (b) { b.classList.remove("active"); });
    $("presetHint").textContent = "";
  }
  function fetchMaintenance() {
    var tb = $("maintBody");
    return fetch("/api/maintenance", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (res) {
        if (res.status === 401) return null;
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      }).then(function (rows) {
        if (rows === null) return;
        rows = Array.isArray(rows) ? rows : [];
        var live = [], done = [];
        rows.forEach(function (r) { (isFinishedMaint(r) ? done : live).push(r); });
        done.sort(function (a, b) { return String(b.end_at).localeCompare(String(a.end_at)); });

        if (!live.length && !done.length) {
          tb.innerHTML = '<tr><td colspan="6"><div class="empty-box" role="status"><b>Belum ada jadwal maintenance</b>Isi form di atas untuk meredam alarm saat perbaikan terjadwal.</div></td></tr>';
        } else if (!live.length) {
          tb.innerHTML = '<tr><td colspan="6"><div class="empty-box" role="status"><b>Tidak ada jadwal aktif</b>Semua jadwal sudah selesai — lihat riwayat di bawah.</div></td></tr>';
        } else {
          tb.innerHTML = live.map(maintRowHtml).join("");
        }
        bindMaintDeletes(tb);

        var hist = $("maintHistory");
        var hb = $("maintHistoryBody");
        if (hist && hb) {
          if (done.length) {
            hist.style.display = "";
            $("maintHistorySummary").textContent = "Riwayat selesai (" + done.length + ")";
            hb.innerHTML = done.slice(0, 10).map(maintRowHtml).join("");
            bindMaintDeletes(hb);
          } else {
            hist.style.display = "none";
            hb.innerHTML = "";
          }
        }
        updateMaintCountdowns();
      }).catch(function (e) {
        tb.innerHTML = '<tr><td colspan="6" style="text-align:center;padding:20px">Gagal memuat jadwal: ' + esc(e.message) + "</td></tr>";
      });
  }

  function createMaintenance(ev) {
    ev.preventDefault();
    var host = $("maintHost").value.trim();
    var start = $("maintStart").value;
    var end = $("maintEnd").value;
    var reason = $("maintReason").value.trim();
    var err = $("maintError");
    var fail = function (m) { err.textContent = m; err.style.display = "block"; };
    if (!host) { fail("Target wajib diisi."); $("maintHost").focus(); return; }
    if (!start) { fail("Waktu mulai wajib diisi."); $("maintStart").focus(); return; }
    if (!end) { fail("Waktu selesai wajib diisi."); $("maintEnd").focus(); return; }
    if (end <= start) { fail("Waktu selesai harus setelah waktu mulai."); $("maintEnd").focus(); return; }
    err.style.display = "none";
    var btn = $("btnSched");
    btn.disabled = true;
    btn.textContent = "Menjadwalkan…";
    fetch("/api/maintenance", {
      method: "POST",
      headers: apiHeaders(),
      body: JSON.stringify({ host: host, start_at: start.replace("T", " ") + ":00", end_at: end.replace("T", " ") + ":00", reason: reason })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) throw new Error(body.error || "Gagal menjadwalkan (HTTP " + res.status + ")");
        $("maintReason").value = "";
        clearPreset();
        setDefaultMaintTimes();
        toast("Maintenance berhasil dijadwalkan.");
        fetchMaintenance();
        fetchTriggers(false);
      });
    }).catch(function (e) { fail(e.message); })
      .finally(function () { btn.disabled = false; btn.textContent = "Jadwalkan"; });
  }

  function deleteMaintenance(id) {
    if (!confirm("Batalkan jadwal maintenance #" + id + "? Alarm akan aktif normal kembali.")) return;
    fetch("/api/maintenance/" + id, { method: "DELETE", headers: apiHeaders() })
      .then(function (res) {
        if (!res.ok) return res.json().catch(function () { return {}; }).then(function (b) { throw new Error(b.error || "HTTP " + res.status); });
        toast("Jadwal maintenance dibatalkan.");
        fetchMaintenance();
        fetchTriggers(false);
      })
      .catch(function (e) { toast(e.message, "error"); });
  }

  /* ---------- Init ---------- */
  function bind(id, ev, fn) { var el = $(id); if (el) el.addEventListener(ev, fn); }

  document.addEventListener("DOMContentLoaded", function () {
    setTheme(document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark");
    try {
      if (localStorage.getItem("nms_nav") === "collapsed" && window.innerWidth > 1023) document.body.classList.add("nav-collapsed");
    } catch (_) {}
    bind("sidebarToggle", "click", function () { document.body.classList.toggle("nav-open"); });
    bind("scrim", "click", function () { document.body.classList.remove("nav-open"); });
    bind("collapseBtn", "click", function () {
      document.body.classList.toggle("nav-collapsed");
      try { localStorage.setItem("nms_nav", document.body.classList.contains("nav-collapsed") ? "collapsed" : "open"); } catch (_) {}
    });
    initFilterFromURL();
    syncFilterUI();

    bind("themeBtn", "click", function () {
      var cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
      setTheme(cur === "dark" ? "light" : "dark");
    });
    bind("btnRefresh", "click", function () { fetchTriggers(true); });

    document.querySelectorAll("#sevSeg .seg-btn").forEach(function (b) {
      b.addEventListener("click", function () { setSevFilter(b.getAttribute("data-sev"), false); });
    });
    ["disaster", "high", "warning"].forEach(function (s) {
      var chip = $("chip-" + s);
      if (chip) chip.addEventListener("click", function () { setSevFilter(s, true); });
    });
    bind("catSelect", "change", function () { catFilter = this.value; curPage = 1; syncFilterUI(); renderTable(); });
    bind("resetFilter", "click", function () {
      sevSet.clear(); catFilter = "all"; searchQuery = ""; curPage = 1;
      syncFilterUI(); renderTable();
    });

    var si = $("hostSearch");
    var deb = null;
    if (si) si.addEventListener("input", function () {
      clearTimeout(deb);
      deb = setTimeout(function () {
        searchQuery = si.value.toLowerCase().trim();
        curPage = 1;
        syncFilterUI();
        renderTable();
      }, 180);
    });

    bind("pgSize", "change", function () { pageSize = parseInt(this.value, 10) || 25; curPage = 1; renderTable(); });
    bind("prevBtn", "click", function () { if (curPage > 1) { curPage--; renderTable(); } });
    bind("nextBtn", "click", function () { curPage++; renderTable(); });

    bind("btnAssignSave", "click", saveAssign);
    bind("btnAssignCancel", "click", closeAssignModal);
    bind("btnAssignClose", "click", closeAssignModal);
    bind("assignForm", "submit", function (e) { e.preventDefault(); saveAssign(); });
    var am = $("assignModal");
    if (am) am.addEventListener("click", function (e) { if (e.target === am) closeAssignModal(); });
    if (am) am.addEventListener("keydown", function (e) { trapFocus(am, e); });

    bind("maintForm", "submit", createMaintenance);
    setDefaultMaintTimes();
    document.querySelectorAll("#presetRow .preset-btn").forEach(function (b) {
      b.addEventListener("click", function () { applyPreset(parseInt(b.getAttribute("data-min"), 10) || 60, b); });
    });
    bind("maintStart", "change", clearPreset);
    bind("maintEnd", "change", clearPreset);

    document.addEventListener("keydown", function (e) {
      var tag = (document.activeElement && document.activeElement.tagName) || "";
      var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      if (e.key === "/" && !typing) { e.preventDefault(); if (si) si.focus(); }
      else if (e.key === "Escape") { closeAssignModal(); document.body.classList.remove("nav-open"); }
    });

    setInterval(function () { updateUpdatedNote(); updateMaintCountdowns(); }, 10000);

    loadHosts().then(function () { renderTable(); });
    fetchTriggers(false);
    fetchMaintenance();
    setInterval(function () { fetchTriggers(false); }, REFRESH_TRIGGERS_MS);
    setInterval(fetchMaintenance, REFRESH_MAINT_MS);
  });
})();
