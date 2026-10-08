/* ============================================================
   NMS Kopegtel — SNMP Auto-Discovery (hanya lapisan tampilan).
   Community hanya dipakai in-memory untuk request scan/import,
   tidak disimpan di localStorage/cookie/URL dan tidak dicetak.
   ============================================================ */
(function () {
  "use strict";

  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (REDUCED) document.body.classList.add("reduced-motion");

  var ICON_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>';
  var ICON_ALERT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 17H2L12 3z"/><path d="M12 10v4M12 17.5v.5"/></svg>';
  var ICON_EYE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/></svg>';
  var ICON_EYE_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 12S6 5.5 12 5.5c2 0 3.7.7 5.1 1.6M21.5 12S18 18.5 12 18.5c-2 0-3.7-.7-5.1-1.6"/><path d="M4 4l16 16"/></svg>';
  var ICON_MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 13.5A8 8 0 0 1 10.5 4 8 8 0 1 0 20 13.5z"/></svg>';
  var ICON_SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  var ICON_RADAR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.2" fill="currentColor"/><path d="M12 12l6-6"/></svg>';

  var devices = [];
  var registered = new Set();
  var lastNetwork = "";
  var scanning = false;

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

  /* ---------- Validasi CIDR ---------- */
  function validateCidr(str) {
    var m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\/(\d{1,2})$/.exec((str || "").trim());
    if (!m) return { ok: false, error: "Format CIDR tidak valid, contoh: 192.168.1.0/24" };
    for (var i = 1; i <= 4; i++) {
      if (parseInt(m[i], 10) > 255) return { ok: false, error: "Oktet IP harus 0–255" };
    }
    var prefix = parseInt(m[5], 10);
    if (prefix > 32) return { ok: false, error: "Prefix maksimal /32" };
    var total = Math.pow(2, 32 - prefix);
    if (total > 256) return { ok: false, error: "Maksimum 256 alamat — gunakan prefix /24 atau lebih sempit" };
    var usable = total <= 2 ? total : total - 2;
    return { ok: true, count: usable };
  }

  function refreshCidrHint() {
    var hint = $("cidrHint");
    var v = $("cidrInput").value.trim();
    var btn = $("btnScan");
    if (!v) {
      hint.textContent = "";
      hint.className = "field-hint";
      if (btn) btn.disabled = false;
      return;
    }
    var r = validateCidr(v);
    if (r.ok) {
      hint.textContent = "±" + r.count + " alamat akan dipindai";
      hint.className = "field-hint ok";
      if (btn) btn.disabled = false;
    } else {
      hint.textContent = r.error;
      hint.className = "field-hint err";
      if (btn) btn.disabled = true;
    }
  }

  /* ---------- Status jadwal & riwayat ---------- */
  function loadScheduleStatus() {
    fetch("/api/settings", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (s) {
        var el = $("schedStatus");
        if (el) {
          if (s.discovery_network) {
            el.innerHTML = "Jadwal aktif: <b>" + esc(s.discovery_network) + "</b> · dipindai otomatis tiap 6 jam";
          } else {
            el.textContent = "Belum ada jadwal — simpan untuk mengaktifkan scan otomatis tiap 6 jam.";
          }
        }
        var line = $("lastScanLine");
        var txt = $("lastScanText");
        if (line && txt && s.discovery_last_scan) {
          try {
            var last = JSON.parse(s.discovery_last_scan);
            var when = last.at ? new Date(String(last.at).replace(" ", "T")) : null;
            var whenTxt = when && !isNaN(when.getTime())
              ? when.toLocaleString("id-ID", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })
              : (last.at || "—");
            txt.innerHTML = "Scan terakhir: <b>" + esc(whenTxt) + "</b> · " + esc(last.count || 0) + " perangkat (" + esc(last.network || "—") + ")";
            line.hidden = false;
          } catch (_) { line.hidden = true; }
        }
      }).catch(function () {});
  }

  /* ---------- Host terdaftar ---------- */
  function loadRegistered() {
    return fetch("/api/hosts", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (data) {
        registered = new Set((Array.isArray(data) ? data : []).map(function (h) { return h.ip; }));
      }).catch(function () {});
  }

  /* ---------- Scan ---------- */
  function setScanState(on) {
    scanning = on;
    var btn = $("btnScan");
    if (btn) {
      btn.disabled = on;
      btn.textContent = on ? "Memindai…" : "Mulai Scan";
      btn.setAttribute("aria-busy", on ? "true" : "false");
    }
  }

  function doScan(e) {
    if (e) e.preventDefault();
    if (scanning) return;
    var network = $("cidrInput").value.trim();
    var community = $("communityInput").value;
    var hint = $("cidrHint");
    var r = validateCidr(network);
    if (!r.ok) {
      hint.textContent = r.error;
      hint.className = "field-hint err";
      $("cidrInput").focus();
      return;
    }
    if (!community) {
      toast("SNMP community wajib diisi.", "error");
      $("communityInput").focus();
      return;
    }
    lastNetwork = network;
    setScanState(true);
    $("resultBody").innerHTML = '<tr><td colspan="5"><div class="skeleton" style="height:140px"></div></td></tr>';
    $("scanState").innerHTML = "Memindai <b>" + esc(network) + "</b> …";
    $("resultCount").textContent = "";

    fetch("/api/discovery/snmp", {
      method: "POST", headers: apiHeaders(),
      body: JSON.stringify({ network: network, community: community })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (d) {
        if (!res.ok) throw new Error(d.error || "Discovery gagal (HTTP " + res.status + ")");
        return d;
      });
    }).then(function (d) {
      devices = Array.isArray(d.devices) ? d.devices : [];
      renderResults();
      var n = devices.length;
      $("scanState").textContent = n
        ? "Pemindaian selesai."
        : "Pemindaian selesai — tidak ada perangkat SNMP yang merespons.";
      toast(n ? n + " perangkat ditemukan." : "Tidak ada perangkat yang merespons.", n ? "success" : "error");
      loadScheduleStatus();
    }).catch(function (err) {
      devices = [];
      renderResults();
      $("scanState").textContent = err.message;
      toast(err.message, "error");
    }).finally(function () { setScanState(false); });
  }

  /* ---------- Render hasil ---------- */
  function vendorInitial(v) {
    var s = String(v || "?").trim();
    return (s.charAt(0) || "?").toUpperCase();
  }

  function renderResults() {
    var tb = $("resultBody");
    var q = ($("resultSearch").value || "").toLowerCase().trim();
    $("resultCount").textContent = devices.length ? devices.length + " perangkat" : "";

    if (!devices.length) {
      if (!lastNetwork) {
        tb.innerHTML = '<tr><td colspan="5"><div class="empty-box" role="status">' + ICON_RADAR +
          '<b>Belum ada hasil pemindaian</b>Masukkan rentang jaringan lalu klik Mulai Scan.</div></td></tr>';
      } else {
        tb.innerHTML = '<tr><td colspan="5"><div class="empty-box" role="status">' + ICON_ALERT +
          '<b>Tidak ada perangkat SNMP yang merespons</b>Periksa community, firewall, dan pastikan perangkat mengaktifkan SNMP.</div></td></tr>';
      }
      updateSelection();
      return;
    }

    var filtered = devices.filter(function (d) {
      if (!q) return true;
      var hay = (d.ip + " " + (d.vendor || "") + " " + (d.sys_name || "") + " " + (d.sys_descr || "")).toLowerCase();
      return hay.indexOf(q) !== -1;
    });

    if (!filtered.length) {
      tb.innerHTML = '<tr><td colspan="5"><div class="empty-box" role="status"><b>Tidak cocok dengan filter</b>Hapus kata kunci untuk melihat semua hasil.</div></td></tr>';
      updateSelection();
      return;
    }

    tb.innerHTML = filtered.map(function (d) {
      var reg = registered.has(d.ip);
      var idx = devices.indexOf(d);
      return "<tr>" +
        '<td data-label="Pilih"><span class="pick-wrap"><input type="checkbox" class="xcheck pick" data-idx="' + idx + '"' + (reg ? " disabled" : "") + ' aria-label="Pilih ' + esc(d.ip) + '"></span></td>' +
        '<td data-label="IP" class="cell-time">' + esc(d.ip) + (reg ? '<span class="reg-badge">Sudah terdaftar</span>' : "") + "</td>" +
        '<td data-label="Vendor"><span class="vendor-cell"><span class="vendor-ic" aria-hidden="true">' + esc(vendorInitial(d.vendor)) + "</span><span>" + esc(d.vendor || "—") + "</span></span></td>" +
        '<td data-label="System name" class="cell-time">' + esc(d.sys_name || "—") + "</td>" +
        '<td data-label="Deskripsi"><span class="desc-clamp" title="' + esc(d.sys_descr || "") + '">' + esc(d.sys_descr || "—") + "</span></td>" +
        "</tr>";
    }).join("");

    tb.querySelectorAll(".pick").forEach(function (cb) {
      cb.addEventListener("change", updateSelection);
    });
    updateSelection();
  }

  function updateSelection() {
    var picks = Array.from(document.querySelectorAll(".pick:not(:disabled)"));
    var checked = picks.filter(function (p) { return p.checked; });
    var n = checked.length;
    $("selCount").textContent = n ? n + " dipilih" : "";
    var btn = $("btnImport");
    btn.disabled = n === 0;
    btn.textContent = n ? "Import terpilih (" + n + ")" : "Import terpilih";
    var all = $("selectAll");
    if (all) {
      all.checked = picks.length > 0 && n === picks.length;
      all.indeterminate = n > 0 && n < picks.length;
      all.disabled = picks.length === 0;
    }
  }

  function toggleSelectAll() {
    var all = $("selectAll");
    var picks = document.querySelectorAll(".pick:not(:disabled)");
    picks.forEach(function (p) { p.checked = all.checked; });
    updateSelection();
  }

  /* ---------- Import ---------- */
  function openImportModal() {
    var n = document.querySelectorAll(".pick:checked:not(:disabled)").length;
    if (!n) return;
    $("importCount").textContent = n;
    $("importModal").classList.add("open");
  }
  function closeImportModal() { $("importModal").classList.remove("open"); }

  function doImport() {
    var picked = Array.from(document.querySelectorAll(".pick:checked:not(:disabled)"));
    if (!picked.length) return;
    var list = picked.map(function (p) {
      var d = devices[Number(p.getAttribute("data-idx"))];
      return { ip: d.ip, sys_name: d.sys_name || "", vendor: d.vendor || "" };
    });
    var btn = $("btnImportConfirm");
    btn.disabled = true;
    btn.textContent = "Mengimpor…";
    fetch("/api/discovery/import", {
      method: "POST", headers: apiHeaders(),
      body: JSON.stringify({
        devices: list,
        community: $("communityInput").value,
        save_community: $("importSaveCommunity").checked,
        category: $("importCategory").value
      })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (d) {
        if (!res.ok) throw new Error(d.error || "Import gagal (HTTP " + res.status + ")");
        return d;
      });
    }).then(function (d) {
      closeImportModal();
      toast((d.count != null ? d.count : list.length) + " host ditambahkan ke monitoring.");
      picked.forEach(function (p) {
        var d = devices[Number(p.getAttribute("data-idx"))];
        if (d) registered.add(d.ip);
        p.checked = false;
      });
      renderResults();
    }).catch(function (err) {
      toast(err.message, "error");
    }).finally(function () {
      btn.disabled = false;
      btn.textContent = "Import";
    });
  }

  /* ---------- Jadwal ---------- */
  function saveSchedule() {
    var network = $("cidrInput").value.trim();
    var r = validateCidr(network);
    if (!r.ok) { toast(r.error, "error"); $("cidrInput").focus(); return; }
    var community = $("communityInput").value;
    if (!community) { toast("SNMP community wajib diisi untuk jadwal.", "error"); $("communityInput").focus(); return; }
    var btn = $("btnSaveSched");
    btn.disabled = true;
    fetch("/api/settings", {
      method: "POST", headers: apiHeaders(),
      body: JSON.stringify({ discovery_network: network, discovery_community: community })
    }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (d) {
        if (!res.ok) throw new Error(d.error || "Gagal menyimpan jadwal");
        toast("Jadwal 6 jam tersimpan.");
        loadScheduleStatus();
      });
    }).catch(function (e) { toast(e.message, "error"); })
      .finally(function () { btn.disabled = false; });
  }

  function runSchedule() {
    var btn = $("btnRunSched");
    btn.disabled = true;
    btn.textContent = "Menjalankan…";
    fetch("/api/discovery/scheduled/run", { method: "POST", headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (d) {
          if (!res.ok) throw new Error(d.error || "Gagal menjalankan scan");
          var added = (d.added && d.added.length) || 0;
          var removed = (d.removed && d.removed.length) || 0;
          toast("Scan terjadwal " + (d.status || "selesai") + " — baru: " + added + ", hilang: " + removed + ".");
        });
      }).catch(function (e) { toast(e.message, "error"); })
      .finally(function () { btn.disabled = false; btn.textContent = "Jalankan sekarang"; });
  }

  /* ---------- Password toggle ---------- */
  function toggleCommunity() {
    var pw = $("communityInput");
    var btn = $("communityToggle");
    if (pw.type === "password") {
      pw.type = "text";
      btn.innerHTML = ICON_EYE_OFF;
      btn.setAttribute("aria-label", "Sembunyikan community");
      btn.setAttribute("aria-pressed", "true");
    } else {
      pw.type = "password";
      btn.innerHTML = ICON_EYE;
      btn.setAttribute("aria-label", "Tampilkan community");
      btn.setAttribute("aria-pressed", "false");
    }
  }

  /* ---------- Init ---------- */
  function bind(id, ev, fn) { var el = $(id); if (el) el.addEventListener(ev, fn); }

  document.addEventListener("DOMContentLoaded", function () {
    setTheme(document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark");
    bind("themeBtn", "click", function () {
      var cur = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
      setTheme(cur === "dark" ? "light" : "dark");
    });
    bind("sidebarToggle", "click", function () { document.body.classList.toggle("nav-open"); });
    bind("scrim", "click", function () { document.body.classList.remove("nav-open"); });

    bind("scanForm", "submit", doScan);
    bind("cidrInput", "input", refreshCidrHint);
    bind("communityToggle", "click", toggleCommunity);
    var ct = $("communityToggle");
    if (ct) ct.innerHTML = ICON_EYE;

    bind("selectAll", "change", toggleSelectAll);
    bind("resultSearch", "input", renderResults);
    bind("btnImport", "click", openImportModal);
    bind("btnImportConfirm", "click", doImport);
    bind("btnImportCancel", "click", closeImportModal);
    bind("btnImportClose", "click", closeImportModal);
    var im = $("importModal");
    if (im) im.addEventListener("click", function (e) { if (e.target === im) closeImportModal(); });
    bind("btnSaveSched", "click", saveSchedule);
    bind("btnRunSched", "click", runSchedule);

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { closeImportModal(); document.body.classList.remove("nav-open"); }
    });

    renderResults();
    loadRegistered().then(renderResults);
    loadScheduleStatus();
  });
})();
