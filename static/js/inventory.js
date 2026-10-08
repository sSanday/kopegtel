/* ============================================================
   NMS Kopegtel — Inventaris Perangkat (hanya lapisan tampilan).
   Membaca API yang sudah ada, tidak mengubah backend / format CSV.
   ============================================================ */
(function () {
  "use strict";

  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (REDUCED) document.body.classList.add("reduced-motion");

  var ICON_CHECK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>';
  var ICON_ALERT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l10 17H2L12 3z"/><path d="M12 10v4M12 17.5v.5"/></svg>';
  var ICON_MOON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M20 13.5A8 8 0 0 1 10.5 4 8 8 0 1 0 20 13.5z"/></svg>';
  var ICON_SUN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  var ICON_PIN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s7-5.5 7-11a7 7 0 10-14 0c0 5.5 7 11 7 11z"/><circle cx="12" cy="10" r="2.5"/></svg>';
  var ICON_COPY = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 012-2h10"/></svg>';
  var ICON_SERVER = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/></svg>';

  var TYPE_ICONS = {
    router: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="8" width="18" height="8" rx="2"/><path d="M7 12h.01M11 12h4M8 4l3 4M16 20l-3-4"/></svg>',
    switch: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="3" y="9" width="18" height="7" rx="1.5"/><path d="M7 12.5h.01M11 12.5h.01M15 12.5h.01"/></svg>',
    "access point": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M5 10a10 10 0 0114 0M8 13a5.5 5.5 0 018 0M11 16a2 2 0 012 0"/><circle cx="12" cy="19" r="1" fill="currentColor"/></svg>',
    server: ICON_SERVER,
    olt: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="6" rx="1.5"/><path d="M7 8h.01M11 8h.01M15 8h.01M12 11v4M9 19h6"/></svg>',
    "ont/modem": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="4" y="9" width="16" height="9" rx="1.5"/><path d="M8 14h.01M12 14h.01M8 5v4M16 5v4"/></svg>',
    cctv: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8l14-4 2 5-14 4z"/><path d="M10 13v5M7 21h7"/></svg>',
    laptop: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="11" rx="1.5"/><path d="M2 19h20"/></svg>',
    hp: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 18h2"/></svg>',
    lainnya: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9h8M8 13h5"/></svg>'
  };

  var DATA = [];
  var q = "";
  var fType = "";
  var fLive = "";
  var sortKey = "hostname";
  var sortDir = 1;
  var lastFetch = 0;

  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
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

  function liveInfo(v) {
    v = String(v || "unmonitored").toLowerCase();
    if (v === "up") return { cls: "b-ok", txt: "Up" };
    if (v === "down") return { cls: "b-bad", txt: "Down" };
    if (v === "pending") return { cls: "b-warn", txt: "Pending" };
    return { cls: "b-mut", txt: "Tidak dimonitor" };
  }
  function typeIcon(t) {
    var k = String(t || "").toLowerCase().trim();
    return TYPE_ICONS[k] || TYPE_ICONS.lainnya;
  }
  function assetBadge(st) {
    st = String(st || "aktif").toLowerCase();
    if (st === "aktif") return '<span class="b-ok">Aktif</span>';
    if (st === "cadangan") return '<span class="b-type">Cadangan</span>';
    return '<span class="b-bad">' + esc(st.charAt(0).toUpperCase() + st.slice(1)) + "</span>";
  }

  function getFiltered() {
    var rows = DATA.filter(function (d) {
      if (fType && d.device_type !== fType) return false;
      if (fLive && String(d.live || "unmonitored").toLowerCase() !== fLive) return false;
      if (q) {
        var hay = (d.hostname + " " + d.ip + " " + (d.location || "") + " " + (d.pic_name || "") + " " + (d.asset_no || "")).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });
    var val = function (d) {
      if (sortKey === "status") return String(d.live || "");
      if (sortKey === "tipe") return String(d.device_type || "");
      if (sortKey === "lokasi") return String(d.location || "");
      if (sortKey === "pic") return String(d.pic_name || "");
      if (sortKey === "aset") return String(d.asset_no || "");
      return String(d.hostname || "").toLowerCase();
    };
    rows.sort(function (a, b) {
      var x = val(a), y = val(b);
      return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
    });
    return rows;
  }

  function syncFilterUI() {
    var chips = "";
    if (fType) chips += '<span class="fchip">Tipe: ' + esc(fType) + '<button type="button" data-chip="type" aria-label="Hapus filter tipe">✕</button></span>';
    if (fLive) chips += '<span class="fchip">Status: ' + esc(liveInfo(fLive).txt) + '<button type="button" data-chip="live" aria-label="Hapus filter status">✕</button></span>';
    if (q) chips += '<span class="fchip">Cari: "' + esc(q) + '"<button type="button" data-chip="q" aria-label="Hapus pencarian">✕</button></span>';
    $("fchips").innerHTML = chips;
    $("fchips").querySelectorAll("[data-chip]").forEach(function (b) {
      b.addEventListener("click", function () {
        var k = b.getAttribute("data-chip");
        if (k === "type") { fType = ""; $("fType").value = ""; }
        else if (k === "live") { fLive = ""; $("fLive").value = ""; }
        else { q = ""; $("q").value = ""; }
        syncFilterUI();
        render();
      });
    });
    var reset = $("resetFilter");
    if (reset) reset.classList.toggle("on", !!(fType || fLive || q));
  }

  function render() {
    var rows = getFiltered();
    var tb = $("tbody");
    document.querySelectorAll("th.sortable").forEach(function (th) {
      var k = th.getAttribute("data-sort");
      th.querySelector(".arr").textContent = k === sortKey ? (sortDir > 0 ? "▲" : "▼") : "";
    });

    if (!DATA.length) {
      tb.innerHTML = '<tr><td colspan="7"><div class="empty-box" role="status">' + ICON_SERVER +
        '<b>Belum ada perangkat di inventaris</b>Catat perangkat jaringan beserta lokasi dan penanggung jawabnya.<br>' +
        '<button type="button" class="btn btn-primary" id="btnEmptyAdd" style="margin-top:14px">+ Tambah perangkat pertama</button></div></td></tr>';
      var ea = $("btnEmptyAdd");
      if (ea) ea.addEventListener("click", function () { openModal(); });
      return;
    }
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="7"><div class="empty-box" role="status"><b>Tidak ada perangkat yang cocok</b>' +
        'Coba ubah kata kunci atau <button type="button" class="reset-btn on" id="btnEmptyReset" style="display:inline">Reset filter</button>.</div></td></tr>';
      var er = $("btnEmptyReset");
      if (er) er.addEventListener("click", resetAll);
      return;
    }

    tb.innerHTML = rows.map(function (d) {
      var li = liveInfo(d.live);
      var t = String(d.device_type || "");
      var sub = [];
      if (d.brand_model) sub.push(esc(d.brand_model));
      return "<tr>" +
        '<td data-label="Status"><span class="' + li.cls + '">' + li.txt + "</span>" +
          (d.last_latency != null ? '<div class="cell-sub mono">' + esc(d.last_latency) + " ms</div>" : "") + "</td>" +
        '<td data-label="Perangkat"><span class="dev-cell"><span class="type-ic" title="' + esc(t || "Perangkat") + '">' + typeIcon(t) + "</span>" +
          '<span style="min-width:0"><b style="display:block">' + esc(d.hostname) + '</b><span class="a-ip">' + esc(d.ip) + "</span></span></span></td>" +
        '<td data-label="Tipe">' + (t ? '<span class="b-mut">' + esc(t) + "</span>" : "—") +
          (d.brand_model ? '<div class="cell-sub">' + esc(d.brand_model) + "</div>" : "") + "</td>" +
        '<td data-label="Lokasi">' + (d.location ? '<span class="loc-cell">' + ICON_PIN + "<span>" + esc(d.location) + "</span></span>" : "—") + "</td>" +
        '<td data-label="PIC">' + (d.pic_name
          ? '<span class="pic-cell"><span class="hi-avatar">' + esc(String(d.pic_name).charAt(0).toUpperCase()) + '</span><span style="min-width:0"><b style="display:block;font-size:13px">' + esc(d.pic_name) + "</b>" + (d.pic_phone ? '<span class="pc-sub">' + esc(d.pic_phone) + "</span>" : "") + "</span></span>"
          : "—") + "</td>" +
        '<td data-label="Aset"><span class="asset-cell">' +
          (d.asset_no ? '<span class="asset-no">' + esc(d.asset_no) + '<button type="button" class="copy-btn" data-copy="' + esc(d.asset_no) + '" aria-label="Salin nomor aset">' + ICON_COPY + "</button></span>" : "<span>—</span>") +
          assetBadge(d.asset_status) + "</span></td>" +
        '<td data-label="Aksi"><div class="card-menu-wrap"><button type="button" class="card-menu-btn" data-menu="' + Number(d.id) + '" aria-label="Menu aksi perangkat" aria-haspopup="menu">···</button></div></td>' +
        "</tr>";
    }).join("");

    tb.querySelectorAll("[data-copy]").forEach(function (b) {
      b.addEventListener("click", function () {
        var t = b.getAttribute("data-copy");
        if (navigator.clipboard) navigator.clipboard.writeText(t).then(function () { toast("Nomor aset disalin."); });
        else toast(t);
      });
    });
    tb.querySelectorAll("[data-menu]").forEach(function (b) {
      b.addEventListener("click", function () { openRowMenu(b, Number(b.getAttribute("data-menu"))); });
    });
  }

  function openRowMenu(btn, id) {
    document.querySelectorAll(".menu-pop.open").forEach(function (m) { m.remove(); });
    var d = DATA.find(function (x) { return Number(x.id) === id; });
    if (!d) return;
    var pop = document.createElement("div");
    pop.className = "menu-pop open";
    pop.setAttribute("role", "menu");
    pop.innerHTML = '<button type="button" data-act="edit">Ubah perangkat</button>' +
      (d.monitored
        ? '<a href="/host/' + encodeURIComponent(d.ip) + '" data-act="mon">Lihat di monitoring</a>'
        : '<button type="button" data-act="monitor">Monitor perangkat ini</button>') +
      '<button type="button" data-act="label">Cetak label aset</button>' +
      '<button type="button" data-act="del" class="danger">Hapus</button>';
    btn.parentElement.appendChild(pop);
    pop.querySelector('[data-act="edit"]').addEventListener("click", function () { pop.remove(); openModal(d); });
    var mBtn = pop.querySelector('[data-act="monitor"]');
    if (mBtn) mBtn.addEventListener("click", function () { pop.remove(); monitorDevice(d); });
    pop.querySelector('[data-act="label"]').addEventListener("click", function () { pop.remove(); openLabelModal(d); });
    pop.querySelector('[data-act="del"]').addEventListener("click", function () { pop.remove(); del(d.id, d.hostname); });
    setTimeout(function () {
      document.addEventListener("click", function h() { pop.remove(); document.removeEventListener("click", h); });
    }, 0);
  }

  /* ---------- Data ---------- */
  function load(manual) {
    var btn = $("btnRefresh");
    if (btn) btn.disabled = true;
    return fetch("/api/inventory", { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (res) {
        if (res.status === 401) return null;
        if (!res.ok) throw new Error("HTTP " + res.status);
        return res.json();
      }).then(function (data) {
        if (data === null) return;
        DATA = Array.isArray(data) ? data : [];
        var types = Array.from(new Set(DATA.map(function (d) { return d.device_type; }).filter(Boolean))).sort();
        var sel = $("fType"), cur = sel.value;
        sel.innerHTML = '<option value="">Semua tipe</option>' + types.map(function (t) {
          return '<option value="' + esc(t) + '"' + (t === cur ? " selected" : "") + ">" + esc(t) + "</option>";
        }).join("");
        var up = DATA.filter(function (d) { return String(d.live).toLowerCase() === "up"; }).length;
        var down = DATA.filter(function (d) { return String(d.live).toLowerCase() === "down"; }).length;
        animCount($("cTotal"), DATA.length);
        animCount($("cUp"), up);
        animCount($("cDown"), down);
        render();
        lastFetch = Date.now();
        updateUpdatedNote();
        if (manual) toast("Data inventaris diperbarui.");
      }).catch(function (e) {
        console.error(e);
        toast("Gagal memuat inventaris: " + e.message, "error");
      }).finally(function () { if (btn) btn.disabled = false; });
  }

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

  function updateUpdatedNote() {
    var el = $("updatedNote");
    if (!el || !lastFetch) return;
    var s = Math.max(0, Math.round((Date.now() - lastFetch) / 1000));
    el.textContent = s < 5 ? "Diperbarui baru saja" : s < 60 ? "Diperbarui " + s + " detik lalu" : "Diperbarui " + Math.floor(s / 60) + " menit lalu";
  }

  /* ---------- Modal ---------- */
  function openModal(d) {
    var isEdit = !!d;
    $("mTitle").textContent = isEdit ? "Ubah Perangkat" : "Tambah Perangkat";
    $("mSub").textContent = isEdit ? "Perbarui data " + (d.hostname || "") : "Catat perangkat baru ke inventaris";
    $("fId").value = isEdit ? d.id : "";
    $("fHostname").value = isEdit ? d.hostname || "" : "";
    $("fIp").value = isEdit ? d.ip || "" : "";
    $("fDeviceType").value = isEdit ? d.device_type || "" : "";
    $("fBrand").value = isEdit ? d.brand_model || "" : "";
    $("fLocation").value = isEdit ? d.location || "" : "";
    $("fAssetNo").value = isEdit ? d.asset_no || "" : "";
    $("fPic").value = isEdit ? d.pic_name || "" : "";
    $("fPhone").value = isEdit ? d.pic_phone || "" : "";
    $("fInstall").value = isEdit ? d.install_date || "" : "";
    $("fAssetStatus").value = isEdit ? d.asset_status || "aktif" : "aktif";
    $("fNotes").value = isEdit ? d.notes || "" : "";
    var err = $("formError");
    if (err) err.style.display = "none";
    var sb = $("saveBtn");
    if (sb) { sb.disabled = false; sb.textContent = "Simpan"; }
    $("modalOverlay").classList.add("open");
    setTimeout(function () { $("fHostname").focus(); }, 60);
  }
  function closeModal() { $("modalOverlay").classList.remove("open"); }

  function validIP(ip) {
    if (!/^(\d{1,3}\.){3}\d{1,3}$/.test(ip)) return false;
    return ip.split(".").every(function (p) { return Number(p) <= 255; });
  }

  function save() {
    var err = $("formError");
    var fail = function (m) { err.textContent = m; err.style.display = "block"; };
    var hostname = $("fHostname").value.trim();
    var ip = $("fIp").value.trim();
    if (!hostname) { fail("Hostname wajib diisi."); $("fHostname").focus(); return; }
    if (!ip) { fail("IP address wajib diisi."); $("fIp").focus(); return; }
    if (!validIP(ip)) { fail("Format IP tidak valid."); $("fIp").focus(); return; }
    var body = {
      hostname: hostname, ip: ip,
      device_type: $("fDeviceType").value,
      brand_model: $("fBrand").value.trim(),
      location: $("fLocation").value.trim(),
      asset_no: $("fAssetNo").value.trim(),
      pic_name: $("fPic").value.trim(),
      pic_phone: $("fPhone").value.trim(),
      install_date: $("fInstall").value,
      asset_status: $("fAssetStatus").value,
      notes: $("fNotes").value.trim()
    };
    var id = $("fId").value;
    var btn = $("saveBtn");
    btn.disabled = true;
    btn.textContent = "Menyimpan…";
    fetch(id ? "/api/inventory/" + encodeURIComponent(id) : "/api/inventory", {
      method: id ? "PUT" : "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
      body: JSON.stringify(body)
    }).then(function (res) {
      if (res.status === 401) return null;
      return res.json().catch(function () { return {}; }).then(function (j) {
        if (!res.ok) throw new Error(j.error || "HTTP " + res.status);
        closeModal();
        toast(id ? "Perangkat '" + hostname + "' diperbarui." : "Perangkat '" + hostname + "' ditambahkan.");
        load(false);
      });
    }).catch(function (e) { if (e) fail(e.message); })
      .finally(function () { btn.disabled = false; btn.textContent = "Simpan"; });
  }

  function monitorDevice(d) {
    fetch("/api/hosts", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
      body: JSON.stringify({
        ip: d.ip, alias: d.hostname || "", category: d.device_type || "Uncategorized",
        snmp_community: "", if_index: 1, snmp_profile: "auto",
        ssh_user: "", ssh_port: 22, backup_enable: 0
      })
    }).then(function (res) {
      if (!res.ok) return res.json().catch(function () { return {}; }).then(function (j) { throw new Error(j.error || "Gagal menambah ke monitoring"); });
      toast("Perangkat '" + (d.hostname || d.ip) + "' mulai dimonitor.");
      load(false);
    }).catch(function (e) { toast(e.message, "error"); });
  }

  /* ---------- Cetak label aset ---------- */
  function openLabelModal(d) {
    var url = location.origin + "/host/" + encodeURIComponent(d.ip);
    $("lsHost").textContent = d.hostname || d.ip;
    $("lsIp").textContent = d.ip;
    $("lsAsset").textContent = d.asset_no || "—";
    $("lsLoc").textContent = d.location ? "Lokasi: " + d.location : "";
    $("lsUrl").textContent = url;
    var box = $("lsQr");
    box.innerHTML = "";
    try {
      var qr = qrcode(0, "M");
      qr.addData(url);
      qr.make();
      box.innerHTML = qr.createImgTag(3, 0);
    } catch (e) {
      box.textContent = "QR tidak tersedia";
    }
    $("labelModal").classList.add("open");
  }
  function closeLabelModal() { $("labelModal").classList.remove("open"); }
  function printLabel() {
    document.body.classList.add("printing-label");
    window.print();
    setTimeout(function () { document.body.classList.remove("printing-label"); }, 400);
  }

  function del(id, hostname) {
    if (!confirm("Hapus perangkat '" + (hostname || id) + "' dari inventaris?")) return;
    fetch("/api/inventory/" + encodeURIComponent(id), { method: "DELETE", headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then(function (r) {
        if (r.status === 401) return;
        if (!r.ok) return r.json().catch(function () { return {}; }).then(function (j) { throw new Error(j.error || "Gagal menghapus"); });
        toast("Perangkat '" + (hostname || id) + "' dihapus.");
        load(false);
      }).catch(function (e) { if (e && e.message) toast(e.message, "error"); });
  }

  function resetAll() {
    q = ""; fType = ""; fLive = "";
    $("q").value = ""; $("fType").value = ""; $("fLive").value = "";
    syncFilterUI();
    render();
  }

  function setSort(k) {
    if (sortKey === k) sortDir = -sortDir;
    else { sortKey = k; sortDir = 1; }
    render();
  }

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
    });
    bind("sidebarToggle", "click", function () { document.body.classList.toggle("nav-open"); });
    bind("scrim", "click", function () { document.body.classList.remove("nav-open"); });

    bind("btnRefresh", "click", function () { load(true); });
    bind("btnAdd", "click", function () { openModal(); });
    var exp = $("btnExport");
    if (exp) exp.addEventListener("click", function () { toast("Mengunduh CSV inventaris…"); });

    bind("q", "input", function () {
      var v = this.value.toLowerCase().trim();
      clearTimeout(this._deb);
      this._deb = setTimeout(function () { q = v; syncFilterUI(); render(); }, 180);
    });
    bind("fType", "change", function () { fType = this.value; syncFilterUI(); render(); });
    bind("fLive", "change", function () { fLive = this.value; syncFilterUI(); render(); });
    bind("resetFilter", "click", resetAll);

    [["chip-total", ""], ["chip-up", "up"], ["chip-down", "down"]].forEach(function (pair) {
      var chip = $(pair[0]);
      if (chip) chip.addEventListener("click", function () {
        fLive = fLive === pair[1] ? "" : pair[1];
        $("fLive").value = fLive;
        syncFilterUI();
        render();
      });
    });

    document.querySelectorAll("th.sortable").forEach(function (th) {
      th.addEventListener("click", function () { setSort(th.getAttribute("data-sort")); });
    });

    bind("saveBtn", "click", save);
    bind("btnCancelModal", "click", closeModal);
    bind("btnCloseModal", "click", closeModal);
    bind("deviceForm", "submit", function (e) { e.preventDefault(); save(); });
    bind("btnCloseLabel", "click", closeLabelModal);
    bind("btnCloseLabel2", "click", closeLabelModal);
    bind("btnPrintLabel", "click", printLabel);
    var lm = $("labelModal");
    if (lm) lm.addEventListener("click", function (e) { if (e.target === lm) closeLabelModal(); });
    var ov = $("modalOverlay");
    if (ov) {
      ov.addEventListener("click", function (e) { if (e.target === ov) closeModal(); });
      ov.addEventListener("keydown", function (e) { trapFocus(ov, e); });
    }
    document.addEventListener("keydown", function (e) {
      var tag = (document.activeElement && document.activeElement.tagName) || "";
      var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
      if (e.key === "/" && !typing) { e.preventDefault(); var s = $("q"); if (s) s.focus(); }
      else if (e.key === "Escape") { closeModal(); closeLabelModal(); document.body.classList.remove("nav-open"); }
    });

    setInterval(updateUpdatedNote, 10000);
    syncFilterUI();
    load(false);
    setInterval(function () { load(false); }, 30000);
  });
})();
