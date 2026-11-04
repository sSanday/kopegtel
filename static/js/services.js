/* ============================================================
   NMS Kopegtel — halaman Services.
   Daftar service gaya konsol Zabbix, filter, detail, dan grafik.
   ============================================================ */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }
  function esc(v) { return window.svcEsc(v); }
  function on(id, event, handler) {
    var el = $(id);
    if (el) el.addEventListener(event, handler);
  }

  var MODE_VIEW = "view";
  var MODE_EDIT = "edit";

  var ICON_PLUS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';
  var ICON_PENCIL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16.5 3.5l4 4L7 21H3v-4z"/><path d="M13.5 6.5l4 4"/></svg>';
  var ICON_X = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  var ICON_SEARCH = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/></svg>';

  var state = {
    services: [],
    hours: 24,
    selectedId: null,
    selectedIds: [],
    mode: MODE_VIEW,
    detailId: null,
    editId: null,
    chart: null,
    history: null,
    historySeq: 0,
    loading: false,
    error: "",
    filterOpen: true,
    filterStatus: "all",
    tagEval: "and_or",
    tagScope: "service",
    parentId: null,
    childIds: [],
    pickerMode: "",
    pickerSelection: [],
    pickerQuery: "",
    childQuery: "",
    formTab: "service"
  };

  function parseTags(value) {
    if (Array.isArray(value)) {
      return value.map(function (t) { return String(t || "").trim(); }).filter(Boolean);
    }
    return String(value == null ? "" : value)
      .split(",")
      .map(function (t) { return t.trim(); })
      .filter(Boolean);
  }

  function norm(value) {
    return String(value == null ? "" : value).trim().toLowerCase();
  }

  function statusKey(svc) {
    var isObject = svc && typeof svc === "object";
    if (isObject && svc.stale) return "nodata";
    var st = String(isObject ? svc.status : svc).toUpperCase();
    if (st === "ONLINE") return "online";
    if (st === "OFFLINE") return "offline";
    return "nodata";
  }

  function statusLabel(key) {
    if (key === "online") return "OK";
    if (key === "offline") return "Problem";
    return "No data";
  }

  function statusHtml(svc) {
    var key = statusKey(svc);
    return '<span class="zbx-status ' + key + '">' + statusLabel(key) + "</span>";
  }

  function isHttps(svc) {
    return String(svc.type || "").toLowerCase() === "http" &&
      String(svc.url || "").toLowerCase().indexOf("https://") === 0;
  }

  function sslDays(svc) {
    var days = svc.ssl_days_left;
    if (days === null || days === undefined || days === "" || isNaN(days)) return null;
    return Number(days);
  }

  function rootCauseHtml(svc) {
    var key = statusKey(svc);
    if (key === "offline") {
      return '<span class="zbx-cause">Check failed</span>';
    }
    if (key === "nodata") {
      return '<span class="zbx-muted">No recent check</span>';
    }
    var days = sslDays(svc);
    if (isHttps(svc) && days !== null && days < 0) {
      return '<span class="zbx-cause">SSL certificate expired</span>';
    }
    return '<span class="zbx-muted">—</span>';
  }

  function tagLabel(tag) {
    var parts = tagParts(tag);
    return parts.value ? parts.name + ": " + parts.value : parts.name;
  }

  function tagsHtml(value) {
    var tags = parseTags(value);
    if (!tags.length) return '<span class="zbx-muted">—</span>';
    return '<span class="zbx-tags">' + tags.map(function (tag) {
      return '<span class="zbx-tag">' + esc(tagLabel(tag)) + "</span>";
    }).join("") + "</span>";
  }

  function problemTags(svc) {
    if (!svc) return [];
    var raw = svc.problem_tags;
    if (raw === undefined || raw === null) raw = svc.problemTags;
    return parseTags(raw);
  }

  function hasChildren(svc) {
    if (!svc) return false;
    var raw = svc.children;
    if (Array.isArray(raw)) return raw.length > 0;
    var count = svc.child_count;
    if (count === undefined || count === null) count = svc.children_count;
    if (count === undefined || count === null || count === "") return false;
    return !isNaN(count) && Number(count) > 0;
  }

  function tagsForScope(svc, scope) {
    var serviceTags = parseTags(svc.tags);
    if (scope === "service") return serviceTags;
    var probTags = problemTags(svc);
    if (scope === "problem") return probTags;
    return serviceTags.concat(probTags);
  }

  function findService(id) {
    var wanted = Number(id);
    for (var i = 0; i < state.services.length; i += 1) {
      if (Number(state.services[i].id) === wanted) return state.services[i];
    }
    return null;
  }

  function descendantIds(id) {
    var root = Number(id);
    var result = [];
    var queue = [root];
    while (queue.length) {
      var current = queue.shift();
      state.services.forEach(function (svc) {
        var sid = Number(svc.id);
        if (Number(svc.parent_id) === current && result.indexOf(sid) === -1) {
          result.push(sid);
          queue.push(sid);
        }
      });
    }
    return result;
  }

  function ancestorIds(id) {
    var result = [];
    var current = Number(id);
    var guard = 0;
    while (current && guard < 1000) {
      var svc = findService(current);
      if (!svc) break;
      current = Number(svc.parent_id) || 0;
      if (!current || result.indexOf(current) !== -1) break;
      result.push(current);
      guard += 1;
    }
    return result;
  }

  function tagParts(tag) {
    var raw = String(tag == null ? "" : tag).trim();
    var idx = raw.indexOf(":");
    if (idx === -1) return { name: raw, value: "" };
    return { name: raw.slice(0, idx).trim(), value: raw.slice(idx + 1).trim() };
  }

  function tagRowValues(row) {
    return {
      tag: norm(row.querySelector(".zbx-tag-name").value),
      op: row.querySelector(".zbx-tag-op").value || "contains",
      value: norm(row.querySelector(".zbx-tag-value").value)
    };
  }

  function readTagRows() {
    var rows = [];
    Array.prototype.forEach.call(document.querySelectorAll("#filterTagList [data-tag-row]"), function (row) {
      var values = tagRowValues(row);
      if (!values.tag) return;
      rows.push(values);
    });
    return rows;
  }

  function tagMatchPositive(serviceTags, row, op) {
    return serviceTags.some(function (rawTag) {
      var parts = tagParts(rawTag);
      var name = norm(parts.name);
      var value = norm(parts.value);
      if (op === "exists") return name === row.tag;
      if (op === "equals") return name === row.tag && value === row.value;
      if (op === "contains") {
        return name.indexOf(row.tag) !== -1 && (row.value === "" || value.indexOf(row.value) !== -1);
      }
      return false;
    });
  }

  function tagRowMatches(serviceTags, row) {
    if (row.op === "not_exists") return !tagMatchPositive(serviceTags, row, "exists");
    if (row.op === "not_equal") return !tagMatchPositive(serviceTags, row, "equals");
    if (row.op === "not_contains") return !tagMatchPositive(serviceTags, row, "contains");
    return tagMatchPositive(serviceTags, row, row.op);
  }

  function matchesTags(serviceTags, rows, evalType) {
    if (!rows.length) return true;
    if (evalType === "or") {
      return rows.some(function (row) { return tagRowMatches(serviceTags, row); });
    }
    var groups = {};
    var order = [];
    rows.forEach(function (row) {
      if (!groups[row.tag]) {
        groups[row.tag] = [];
        order.push(row.tag);
      }
      groups[row.tag].push(row);
    });
    return order.every(function (name) {
      return groups[name].some(function (row) { return tagRowMatches(serviceTags, row); });
    });
  }

  function filterValues() {
    return {
      name: norm($("filterName").value),
      status: state.filterStatus || "all",
      tagRows: readTagRows(),
      tagEval: state.tagEval || "and_or",
      tagScope: "service"
    };
  }

  function filteredServices() {
    var filters = filterValues();
    return state.services.filter(function (svc) {
      if (filters.status !== "all" && statusKey(svc) !== filters.status) return false;
      if (filters.name) {
        var haystack = norm([svc.name, svc.target, svc.ip].join(" "));
        if (haystack.indexOf(filters.name) === -1) return false;
      }
      return matchesTags(tagsForScope(svc, filters.tagScope), filters.tagRows, filters.tagEval);
    });
  }

  function isSelected(id) {
    return state.selectedIds.indexOf(Number(id)) !== -1;
  }

  function setSelected(id, selected) {
    var wanted = Number(id);
    var idx = state.selectedIds.indexOf(wanted);
    if (selected && idx === -1) state.selectedIds.push(wanted);
    if (!selected && idx !== -1) state.selectedIds.splice(idx, 1);
  }

  function updateSelectionUi() {
    var count = state.selectedIds.length;
    var selectedCount = $("selectedCount");
    if (selectedCount) selectedCount.textContent = count + " selected";
    var mass = $("btnMassUpdate");
    var del = $("btnBulkDelete");
    if (mass) mass.disabled = count === 0;
    if (del) del.disabled = count === 0;

    var all = $("selectAll");
    if (all) {
      var rows = filteredServices();
      var selectedVisible = 0;
      rows.forEach(function (svc) {
        if (isSelected(svc.id)) selectedVisible += 1;
      });
      all.checked = rows.length > 0 && selectedVisible === rows.length;
      all.indeterminate = selectedVisible > 0 && selectedVisible < rows.length;
    }
  }

  function emptyStateHtml(colspan, title, hint, action, actionLabel) {
    var button = action
      ? '<button class="zbx-btn primary" type="button" data-act="' + action + '">' + esc(actionLabel) + "</button>"
      : "";
    return '<tr class="zbx-empty-row"><td colspan="' + colspan + '" class="zbx-empty-cell">' +
      '<div class="zbx-empty-state">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5L21 21"/></svg>' +
      '<span class="zbx-empty-title">' + esc(title) + "</span>" +
      '<span>' + esc(hint) + "</span>" + button +
      "</div></td></tr>";
  }

  function renderTable() {
    var rows = filteredServices();
    var body = $("servicesBody");
    var edit = state.mode === MODE_EDIT;
    var colspan = edit ? 8 : 6;
    var table = $("servicesTable");
    if (table) table.setAttribute("aria-busy", state.loading ? "true" : "false");

    if (state.loading && !state.services.length) {
      body.innerHTML = '<tr class="zbx-empty-row"><td colspan="' + colspan + '" class="zbx-empty-cell">' +
        '<div class="zbx-empty-state">' +
        '<span class="zbx-spinner" aria-hidden="true"></span>' +
        '<span>Memuat data…</span>' +
        "</div></td></tr>";
    } else if (state.error) {
      body.innerHTML = '<tr class="zbx-empty-row"><td colspan="' + colspan + '" class="zbx-empty-cell">' +
        '<div class="zbx-empty-state" role="alert">' +
        '<span class="zbx-empty-title">Gagal memuat data</span>' +
        '<span>' + esc(state.error) + '</span>' +
        '<button class="zbx-btn primary" type="button" data-act="retry">Coba lagi</button>' +
        "</div></td></tr>";
    } else if (!rows.length) {
      var filtered = state.services.length > 0;
      body.innerHTML = emptyStateHtml(
        colspan,
        filtered ? "No matching checks" : "No service checks yet",
        filtered
          ? "Ubah atau reset filter untuk melihat hasil lain."
          : "Tambahkan pemeriksaan TCP, HTTP, ICMP, atau DNS.",
        filtered ? "reset" : "create",
        filtered ? "Reset filter" : "Create check"
      );
    } else {
      body.innerHTML = rows.map(function (svc) {
        var id = Number(svc.id);
        var key = statusKey(svc);
        var label = statusLabel(key);
        var selected = state.selectedId === id || (edit && isSelected(id));
        var rowClass = selected ? ' class="selected"' : "";
        var cells = "";

        if (edit) {
          cells += '<td class="zbx-col-check"><input class="zbx-check zbx-row-check" type="checkbox" data-id="' + id + '"' +
            (isSelected(id) ? " checked" : "") +
            ' aria-label="Pilih ' + esc(svc.name) + '"></td>';
        }

        cells += '<td class="zbx-col-name">' +
          (edit
            ? esc(svc.name)
            : '<button type="button" class="zbx-name-link" data-act="view" data-id="' + id + '" title="Buka detail pemeriksaan">' + esc(svc.name) + "</button>") +
          "</td>";
        cells += '<td class="zbx-col-status zbx-status-cell ' + key + '">' + label + "</td>";
        cells += '<td class="zbx-col-checked zbx-nowrap" title="' + esc(svc.last_checked || "") + '">' +
          esc(window.svcFmtAge(svc.last_checked_age)) + "</td>";
        cells += '<td class="zbx-col-cause">' + rootCauseHtml(svc) + "</td>";
        cells += '<td class="zbx-col-created zbx-nowrap">' + esc(window.svcFmtDateTime(svc.created_at)) + "</td>";
        cells += '<td class="zbx-col-tags">' + tagsHtml(svc.tags) + "</td>";

        if (edit) {
          cells += '<td class="zbx-col-actions"><span class="zbx-rowacts">' +
            '<button type="button" class="zbx-rowact" data-act="edit" data-id="' + id + '" title="Edit service check" aria-label="Edit service check">' + ICON_PENCIL + "</button>" +
            '<button type="button" class="zbx-rowact danger" data-act="delete" data-id="' + id + '" title="Delete service check" aria-label="Delete service check">' + ICON_X + "</button>" +
            "</span></td>";
        }

        return '<tr data-id="' + id + '"' + rowClass + ">" + cells + "</tr>";
      }).join("");
    }

    var info = $("tableInfo");
    if (info) {
      if (state.loading) info.textContent = "Memuat…";
      else if (state.error) info.textContent = "—";
      else info.textContent = "Displaying " + rows.length + " of " + state.services.length + " found";
    }
    updateSelectionUi();
  }

  function markSelected(id) {
    state.selectedId = id === null ? null : Number(id);
    Array.prototype.forEach.call(document.querySelectorAll("#servicesBody tr[data-id]"), function (tr) {
      tr.classList.toggle("selected", Number(tr.getAttribute("data-id")) === state.selectedId);
    });
  }

  function applyMode(mode, skipRender) {
    state.mode = mode === MODE_EDIT ? MODE_EDIT : MODE_VIEW;
    var page = $("konten");
    if (page) page.setAttribute("data-mode", state.mode);

    var viewBtn = $("btnView");
    var editBtn = $("btnEdit");
    if (viewBtn) {
      viewBtn.classList.toggle("on", state.mode === MODE_VIEW);
      viewBtn.setAttribute("aria-pressed", state.mode === MODE_VIEW ? "true" : "false");
    }
    if (editBtn) {
      editBtn.classList.toggle("on", state.mode === MODE_EDIT);
      editBtn.setAttribute("aria-pressed", state.mode === MODE_EDIT ? "true" : "false");
    }
    if (state.mode === MODE_VIEW) state.selectedIds = [];
    if (!skipRender) renderTable();
  }

  function detailRange() {
    Array.prototype.forEach.call(document.querySelectorAll("#rangeSeg button[data-h]"), function (btn) {
      btn.classList.toggle("on", Number(btn.getAttribute("data-h")) === state.hours);
    });
  }

  function renderDetail() {
    var svc = findService(state.detailId);
    if (!svc) return;
    $("detailTitle").textContent = svc.name;
    $("detailStatus").innerHTML = statusHtml(svc);
    $("detailType").textContent = window.svcTypeLabel(svc.type);
    $("detailTarget").textContent = svc.target || "—";
    $("detailLatency").textContent = window.svcFmtLatency(svc.latency, svc.status);
    $("detailAvailability").textContent = window.svcFmtPct(svc.availability);
    $("detailSsl").innerHTML = window.svcSslHtml(svc);
    var ageText = window.svcFmtAge(svc.last_checked_age);
    $("detailLastChecked").textContent = window.svcFmtDateTime(svc.last_checked) +
      (ageText === "—" ? "" : " (" + ageText + ")");
    $("detailCreated").textContent = window.svcFmtDateTime(svc.created_at);
    $("detailTags").innerHTML = tagsHtml(svc.tags);
    $("btnSslCheck").hidden = !isHttps(svc);
    detailRange();
  }

  function renderHistoryChart() {
    var canvas = $("svcChart");
    var empty = $("chartEmpty");
    var data = state.history;
    if (!data || !data.count) {
      if (state.chart) { state.chart.destroy(); state.chart = null; }
      canvas.style.display = "none";
      empty.hidden = false;
      empty.textContent = data && data.service
        ? "Belum ada riwayat latency untuk service ini pada rentang waktu tersebut."
        : "Pilih service untuk menampilkan riwayat latency.";
      return;
    }
    if (typeof window.Chart !== "function") {
      canvas.style.display = "none";
      empty.hidden = false;
      empty.textContent = "Grafik tidak tersedia.";
      return;
    }
    canvas.style.display = "block";
    empty.hidden = true;
    if (state.chart) { state.chart.destroy(); state.chart = null; }
    var colors = window.svcChartColors();
    state.chart = new window.Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: data.labels,
        datasets: [{
          label: "Latency",
          data: data.values,
          borderColor: colors.accent,
          backgroundColor: "rgba(46,119,206,.12)",
          borderWidth: 1.6,
          pointRadius: 0,
          pointHoverRadius: 3,
          tension: 0.22,
          spanGaps: false,
          fill: true
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (item) {
                if (item.parsed.y === null || item.parsed.y === undefined) return "OFFLINE";
                return "Latency: " + Number(item.parsed.y).toFixed(2) + " ms";
              }
            }
          }
        },
        scales: {
          x: {
            grid: { color: colors.grid, drawBorder: false },
            ticks: { color: colors.text3, maxTicksLimit: 12, font: { size: 10 } }
          },
          y: {
            beginAtZero: true,
            grid: { color: colors.grid, drawBorder: false },
            ticks: {
              color: colors.text3,
              font: { size: 10 },
              callback: function (value) { return value + " ms"; }
            }
          }
        }
      }
    });
  }

  function loadHistory() {
    if (!state.detailId) return;
    var id = state.detailId;
    var seq = ++state.historySeq;
    state.history = null;
    $("svcChart").style.display = "none";
    $("chartEmpty").hidden = false;
    $("chartEmpty").textContent = "Memuat riwayat latency…";
    window.svcJson("/api/services/" + encodeURIComponent(id) + "/history?hours=" + state.hours)
      .then(function (data) {
        if (seq !== state.historySeq || state.detailId !== id) return;
        state.history = data;
        renderHistoryChart();
      })
      .catch(function (err) {
        if (seq !== state.historySeq || state.detailId !== id) return;
        state.history = null;
        $("svcChart").style.display = "none";
        $("chartEmpty").hidden = false;
        $("chartEmpty").textContent = err.message;
        window.svcToast(err.message, "error");
      });
  }

  function showDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  }

  function hideDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function openDetail(id) {
    var svc = findService(id);
    if (!svc) {
      window.svcToast("Service tidak ditemukan.", "error");
      return;
    }
    state.selectedId = Number(svc.id);
    state.detailId = Number(svc.id);
    renderTable();
    renderDetail();
    showDialog($("detailDialog"));
    setTimeout(function () {
      loadHistory();
      if (state.chart) state.chart.resize();
    }, 40);
  }

  function resetDetailState() {
    state.detailId = null;
    state.history = null;
    state.historySeq += 1;
    if (state.chart) { state.chart.destroy(); state.chart = null; }
  }

  function closeDetail() {
    hideDialog($("detailDialog"));
    resetDetailState();
  }

  function loadServices() {
    if (state.loading) return Promise.resolve();
    state.loading = true;
    state.error = "";
    renderTable();
    return window.svcJson("/api/services?hours=24")
      .then(function (data) {
        state.services = Array.isArray(data) ? data : [];
        state.error = "";
        var counts = {};
        state.services.forEach(function (svc) {
          if (svc.parent_id) {
            counts[Number(svc.parent_id)] = (counts[Number(svc.parent_id)] || 0) + 1;
          }
        });
        state.services.forEach(function (svc) {
          svc.child_count = counts[Number(svc.id)] || 0;
          var parent = svc.parent_id ? findService(svc.parent_id) : null;
          svc.parent_name = parent ? parent.name : "";
        });
        var ids = state.services.map(function (svc) { return Number(svc.id); });
        if (state.selectedId !== null && ids.indexOf(Number(state.selectedId)) === -1) {
          state.selectedId = null;
        }
        state.selectedIds = state.selectedIds.filter(function (id) {
          return ids.indexOf(Number(id)) !== -1;
        });
        if (state.detailId !== null) {
          if (ids.indexOf(Number(state.detailId)) === -1) {
            closeDetail();
          } else {
            renderDetail();
          }
        }
      })
      .catch(function (err) {
        if (!state.services.length) {
          state.error = err.message || "Gagal memuat data.";
        }
        window.svcToast(err.message || "Gagal memuat data.", "error");
      })
      .then(function () {
        state.loading = false;
        renderTable();
      });
  }

  function toggleFormFields() {
    var type = $("svcType").value;
    $("portField").hidden = type !== "tcp";
    $("urlField").hidden = type !== "http";
    $("queryField").hidden = type !== "dns";
    $("expectedField").hidden = type === "tcp" || type === "icmp";
    $("expectedLabel").innerHTML = type === "dns"
      ? 'Expected answer <span class="svc-muted">(opsional)</span>'
      : 'Expected keyword <span class="svc-muted">(opsional)</span>';
  }

  function updateStatusWarn() {
    var warn = $("statusRuleWarn");
    if (!warn) return;
    var rule = $("svcStatusRule") ? $("svcStatusRule").value : "";
    var count = state.childIds.length;
    if (rule === "most_critical_all") {
      warn.title = "Status dihitung dari service ini dan seluruh child service.";
    } else if (count) {
      warn.title = "Status dihitung dari " + count + " child service.";
    } else {
      warn.title = "Status calculation rule membutuhkan child service.";
    }
  }

  function setFormTab(tab) {
    state.formTab = tab;
    Array.prototype.forEach.call(
      document.querySelectorAll("#addDialog .zbx-svc-tab"),
      function (btn) {
        var on = btn.getAttribute("data-tab") === tab;
        btn.classList.toggle("on", on);
        btn.setAttribute("aria-selected", on ? "true" : "false");
        btn.tabIndex = on ? 0 : -1;
      }
    );
    var panes = {
      service: "paneService",
      tags: "paneTags",
      children: "paneChildren"
    };
    Object.keys(panes).forEach(function (key) {
      var pane = $(panes[key]);
      if (pane) pane.hidden = key !== tab;
    });
    if (tab === "children") renderChildList();
  }

  function setAdvanced(open) {
    var btn = $("btnAdvanced");
    var body = $("advancedBody");
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (body) body.hidden = !open;
  }

  function problemTagRowHtml(name, op, value) {
    var ops = [
      ["equals", "Equals"],
      ["contains", "Contains"],
      ["like", "Like"],
      ["exists", "Exists"],
      ["not_exists", "Does not exist"]
    ];
    var selected = op || "equals";
    var options = ops.map(function (pair) {
      return '<option value="' + pair[0] + '"' +
        (pair[0] === selected ? " selected" : "") + ">" + pair[1] + "</option>";
    }).join("");
    return '<tr data-problem-tag>' +
      '<td><input class="zbx-input" type="text" data-tag-name maxlength="64" autocomplete="off" value="' + esc(name || "") + '"></td>' +
      '<td><select class="zbx-input" data-tag-op>' + options + "</select></td>" +
      '<td><input class="zbx-input" type="text" data-tag-value maxlength="64" autocomplete="off" value="' + esc(value || "") + '"></td>' +
      '<td class="zbx-mini-actions"><button type="button" class="zbx-text-link" data-remove-row>Remove</button></td>' +
      "</tr>";
  }

  function syncProblemTagRow(row) {
    if (!row) return;
    var op = row.querySelector("[data-tag-op]");
    var value = row.querySelector("[data-tag-value]");
    if (!op || !value) return;
    var noValue = op.value === "exists" || op.value === "not_exists";
    value.disabled = noValue;
    if (noValue) value.value = "";
  }

  function addProblemTagRow(name, op, value) {
    var body = $("problemTagList");
    if (!body) return null;
    body.insertAdjacentHTML("beforeend", problemTagRowHtml(name, op, value));
    var rows = body.querySelectorAll("[data-problem-tag]");
    var row = rows[rows.length - 1];
    syncProblemTagRow(row);
    return row;
  }

  function serviceTagRowHtml(name, value) {
    return '<tr data-service-tag>' +
      '<td><input class="zbx-input" type="text" data-tag-name maxlength="64" autocomplete="off" value="' + esc(name || "") + '"></td>' +
      '<td><input class="zbx-input" type="text" data-tag-value maxlength="64" autocomplete="off" value="' + esc(value || "") + '"></td>' +
      '<td class="zbx-mini-actions"><button type="button" class="zbx-text-link" data-remove-row>Remove</button></td>' +
      "</tr>";
  }

  function addServiceTagRow(name, value) {
    var body = $("serviceTagList");
    if (!body) return null;
    body.insertAdjacentHTML("beforeend", serviceTagRowHtml(name, value));
    var rows = body.querySelectorAll("[data-service-tag]");
    return rows[rows.length - 1];
  }

  var RULE_CONDITIONS = [
    'If at least <span class="zbx-rule-chip">N</span> child services have <span class="zbx-rule-chip">Status</span> status or above',
    'If at least <span class="zbx-rule-chip">N%</span> of child services have <span class="zbx-rule-chip">Status</span> status or above',
    'If less than <span class="zbx-rule-chip">N</span> child services have <span class="zbx-rule-chip">Status</span> status or below',
    'If less than <span class="zbx-rule-chip">N%</span> of child services have <span class="zbx-rule-chip">Status</span> status or below',
    'If weight of child services with <span class="zbx-rule-chip">Status</span> status or above is at least <span class="zbx-rule-chip">N</span>',
    'If weight of child services with <span class="zbx-rule-chip">Status</span> status or above is at least <span class="zbx-rule-chip">N%</span>',
    'If weight of child services with <span class="zbx-rule-chip">Status</span> status or below is less than <span class="zbx-rule-chip">N</span>',
    'If weight of child services with <span class="zbx-rule-chip">Status</span> status or below is less than <span class="zbx-rule-chip">N%</span>'
  ];
  var RULE_PERCENT_TYPES = { 1: true, 3: true, 5: true, 7: true };
  var ruleConditionType = 0;
  var ruleMenuOpen = false;

  function additionalRuleRowHtml(name) {
    return '<tr data-additional-rule>' +
      '<td class="zbx-rule-name" data-rule-text>' + esc(name || "") + "</td>" +
      '<td class="zbx-mini-actions"><button type="button" class="zbx-text-link" data-remove-row>Remove</button></td>' +
      "</tr>";
  }

  function addAdditionalRuleRow(name) {
    var body = $("additionalRuleList");
    if (!body) return null;
    body.insertAdjacentHTML("beforeend", additionalRuleRowHtml(name));
    var rows = body.querySelectorAll("[data-additional-rule]");
    return rows[rows.length - 1];
  }

  function setRuleMenu(open) {
    var menu = $("ruleConditionMenu");
    var combo = $("ruleCondition");
    ruleMenuOpen = !!open;
    if (menu) menu.hidden = !open;
    if (combo) combo.setAttribute("aria-expanded", open ? "true" : "false");
  }

  function syncRuleCondition() {
    var view = $("ruleConditionView");
    var html = RULE_CONDITIONS[ruleConditionType] || RULE_CONDITIONS[0];
    if (view) view.innerHTML = html;
    var menu = $("ruleConditionMenu");
    if (!menu) return;
    Array.prototype.forEach.call(menu.querySelectorAll("[data-cond]"), function (btn) {
      var on = Number(btn.getAttribute("data-cond")) === ruleConditionType;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  function openRuleDialog() {
    ruleConditionType = 0;
    var newStatus = $("ruleNewStatus");
    var limit = $("ruleLimit");
    var limitStatus = $("ruleLimitStatus");
    var err = $("ruleError");
    if (newStatus) newStatus.value = "0";
    if (limit) limit.value = "1";
    if (limitStatus) limitStatus.value = "-1";
    if (err) { err.hidden = true; err.textContent = ""; }
    syncRuleCondition();
    syncRuleLimitMax();
    setRuleMenu(false);
    showDialog($("ruleDialog"));
    setTimeout(function () { if (newStatus) newStatus.focus(); }, 30);
  }

  function closeRuleDialog() {
    setRuleMenu(false);
    hideDialog($("ruleDialog"));
  }

  function ruleConditionText(n, status) {
    var pct = RULE_PERCENT_TYPES[ruleConditionType] ? "%" : "";
    switch (ruleConditionType) {
      case 1:
        return "If at least " + n + "% of child services have " + status + " status or above";
      case 2:
        return "If less than " + n + " child services have " + status + " status or below";
      case 3:
        return "If less than " + n + "% of child services have " + status + " status or below";
      case 4:
        return "If weight of child services with " + status + " status or above is at least " + n;
      case 5:
        return "If weight of child services with " + status + " status or above is at least " + n + "%";
      case 6:
        return "If weight of child services with " + status + " status or below is less than " + n;
      case 7:
        return "If weight of child services with " + status + " status or below is less than " + n + "%";
      default:
        return "If at least " + n + pct + " child services have " + status + " status or above";
    }
  }

  function syncRuleLimitMax() {
    var limit = $("ruleLimit");
    if (!limit) return;
    var max = RULE_PERCENT_TYPES[ruleConditionType] ? 100 : 100000;
    limit.max = String(max);
    if (Number(limit.value) > max) limit.value = String(max);
  }

  function addRuleFromDialog() {
    var limit = $("ruleLimit");
    var err = $("ruleError");
    var n = parseInt(limit && limit.value, 10);
    var max = RULE_PERCENT_TYPES[ruleConditionType] ? 100 : 100000;
    if (isNaN(n) || n < 1 || n > max) {
      if (err) {
        err.textContent = "N harus angka 1–" + max + ".";
        err.hidden = false;
      }
      if (limit) limit.focus();
      return;
    }
    var newStatus = $("ruleNewStatus");
    var limitStatus = $("ruleLimitStatus");
    var newLabel = newStatus && newStatus.selectedIndex >= 0
      ? newStatus.options[newStatus.selectedIndex].text : "Not classified";
    var statusLabel = limitStatus && limitStatus.selectedIndex >= 0
      ? limitStatus.options[limitStatus.selectedIndex].text : "OK";
    addAdditionalRuleRow(newLabel + ": " + ruleConditionText(n, statusLabel));
    closeRuleDialog();
  }

  function readFormTagRows(container) {
    var out = [];
    if (!container) return out;
    Array.prototype.forEach.call(container.querySelectorAll("tr"), function (row) {
      var nameEl = row.querySelector("[data-tag-name]");
      var valueEl = row.querySelector("[data-tag-value]");
      if (!nameEl) return;
      var name = nameEl.value.trim();
      if (!name) return;
      var value = valueEl && !valueEl.disabled ? valueEl.value.trim() : "";
      out.push(value ? name + ": " + value : name);
    });
    return out;
  }

  function populateTagRows(container, raw, kind) {
    if (!container) return;
    container.innerHTML = "";
    var tags = parseTags(raw);
    if (!tags.length) {
      if (kind === "problem") addProblemTagRow("", "equals", "");
      else addServiceTagRow("", "");
      return;
    }
    tags.forEach(function (tag) {
      var parts = tagParts(tag);
      if (kind === "problem") addProblemTagRow(parts.name, "equals", parts.value);
      else addServiceTagRow(parts.name, parts.value);
    });
  }

  function parseAdditionalRules(raw) {
    if (Array.isArray(raw)) {
      return raw.map(function (item) {
        return String(item || "").trim();
      }).filter(Boolean);
    }
    var text = String(raw == null ? "" : raw).trim();
    if (!text) return [];
    try {
      var parsed = JSON.parse(text);
      if (Array.isArray(parsed)) {
        return parsed.map(function (item) {
          return String(item || "").trim();
        }).filter(Boolean);
      }
    } catch (_) {}
    return text.split(",").map(function (item) {
      return item.trim();
    }).filter(Boolean);
  }

  function setParentService(id) {
    var wanted = id === null || id === undefined || id === "" ? null : Number(id);
    var svc = wanted ? findService(wanted) : null;
    state.parentId = svc ? Number(svc.id) : null;
    var input = $("svcParentSearch");
    if (input) input.value = svc ? svc.name : "";
    updateStatusWarn();
  }

  function setChildIds(ids) {
    var seen = [];
    (ids || []).forEach(function (id) {
      var wanted = Number(id);
      if (wanted && seen.indexOf(wanted) === -1) seen.push(wanted);
    });
    state.childIds = seen;
    renderChildList();
    updateStatusWarn();
  }

  function childRowHtml(svc) {
    return '<tr data-child-id="' + Number(svc.id) + '">' +
      '<td class="zbx-child-name">' + esc(svc.name) + "</td>" +
      '<td class="zbx-child-tags">' + tagsHtml(svc.problem_tags) + "</td>" +
      '<td class="zbx-child-action"><button type="button" class="zbx-text-link" data-remove-child="' + Number(svc.id) + '">Remove</button></td>' +
      "</tr>";
  }

  function renderChildList() {
    var add = $("btnAddChild");
    if (add) add.disabled = false;
    var body = $("childServiceList");
    if (!body) return;
    var query = norm(state.childQuery);
    var children = state.childIds.map(function (id) {
      return findService(id);
    }).filter(Boolean).filter(function (svc) {
      return !query || norm(svc.name).indexOf(query) !== -1;
    });
    body.innerHTML = children.map(childRowHtml).join("");
  }

  function clearChildFilter() {
    state.childQuery = "";
    var input = $("childSearch");
    if (input) input.value = "";
  }

  function applyChildFilter() {
    var input = $("childSearch");
    state.childQuery = input ? input.value.trim() : "";
    renderChildList();
  }

  function resetChildFilter() {
    clearChildFilter();
    renderChildList();
    var input = $("childSearch");
    if (input) input.focus();
  }

  function eligiblePickerServices(mode) {
    var selfId = state.editId === null ? null : Number(state.editId);
    var excluded = [];
    if (selfId !== null) {
      if (mode === "parent") excluded = descendantIds(selfId).concat([selfId]);
      else excluded = ancestorIds(selfId).concat([selfId]);
    } else if (mode === "child" && state.parentId) {
      excluded = ancestorIds(state.parentId).concat([state.parentId]);
    }
    return state.services.filter(function (svc) {
      return excluded.indexOf(Number(svc.id)) === -1;
    });
  }

  function pickerVisibleIds() {
    var list = $("pickerList");
    if (!list) return [];
    return Array.prototype.map.call(
      list.querySelectorAll("tr[data-picker-id]"),
      function (row) { return Number(row.getAttribute("data-picker-id")); }
    );
  }

  function updatePickerHeader() {
    var all = $("pickerSelectAll");
    if (!all) return;
    var ids = pickerVisibleIds();
    var selected = ids.filter(function (id) {
      return state.pickerSelection.indexOf(id) !== -1;
    });
    all.checked = ids.length > 0 && selected.length === ids.length;
    all.indeterminate = selected.length > 0 && selected.length < ids.length;
  }

  function syncPickerCheckboxes() {
    var list = $("pickerList");
    if (!list) return;
    Array.prototype.forEach.call(
      list.querySelectorAll('input[name="pickerPick"]'),
      function (input) {
        input.checked = state.pickerSelection.indexOf(Number(input.value)) !== -1;
      }
    );
    updatePickerHeader();
  }

  function showPickerError(message) {
    var el = $("pickerError");
    if (!el) return;
    el.textContent = message;
    el.hidden = false;
  }

  function clearPickerError() {
    var el = $("pickerError");
    if (!el) return;
    el.textContent = "";
    el.hidden = true;
  }

  function renderPicker() {
    var list = $("pickerList");
    if (!list) return;
    var mode = state.pickerMode || "parent";
    var query = norm(state.pickerQuery);
    var items = eligiblePickerServices(mode).filter(function (svc) {
      if (!query) return true;
      return norm(svc.name).indexOf(query) !== -1;
    });
    if (!items.length) {
      list.innerHTML = '<tr class="zbx-picker-empty-row"><td colspan="4">' +
        '<div class="zbx-picker-empty">' + ICON_SEARCH +
        "<div>No data found</div></div></td></tr>";
      updatePickerHeader();
      return;
    }
    list.innerHTML = items.map(function (svc) {
      var id = Number(svc.id);
      var checked = state.pickerSelection.indexOf(id) !== -1;
      return '<tr data-picker-id="' + id + '">' +
        '<td class="zbx-picker-check"><input type="checkbox" name="pickerPick" value="' + id + '"' +
        (checked ? " checked" : "") + ' aria-label="Pilih ' + esc(svc.name) + '"></td>' +
        '<td class="zbx-picker-name">' + esc(svc.name) + "</td>" +
        '<td class="zbx-picker-tags">' + tagsHtml(svc.tags) + "</td>" +
        '<td class="zbx-picker-problem-tags">' + tagsHtml(svc.problem_tags) + "</td>" +
        "</tr>";
    }).join("");
    updatePickerHeader();
  }

  function openPicker(mode) {
    state.pickerMode = mode === "child" ? "child" : "parent";
    var title = $("pickerTitle");
    if (title) {
      title.textContent = state.pickerMode === "parent"
        ? "Add parent services"
        : "Add child services";
    }
    var typed = "";
    if (state.pickerMode === "parent") {
      typed = $("svcParentSearch").value.trim();
      var currentParent = state.parentId ? findService(state.parentId) : null;
      if (currentParent && typed === currentParent.name) typed = "";
      state.pickerSelection = state.parentId ? [state.parentId] : [];
    } else {
      state.pickerSelection = state.childIds.slice();
    }
    state.pickerQuery = typed;
    $("pickerSearch").value = typed;
    clearPickerError();
    renderPicker();
    showDialog($("pickerDialog"));
    setTimeout(function () {
      var input = $("pickerSearch");
      input.focus();
      if (input.value) input.select();
    }, 30);
  }

  function cancelPicker() {
    if (state.pickerMode === "parent") {
      var svc = state.parentId ? findService(state.parentId) : null;
      if ($("svcParentSearch")) $("svcParentSearch").value = svc ? svc.name : "";
    }
    clearPickerError();
    state.pickerQuery = "";
    hideDialog($("pickerDialog"));
  }

  function applyPickerFilter() {
    state.pickerQuery = $("pickerSearch").value.trim();
    clearPickerError();
    renderPicker();
  }

  function resetPickerFilter() {
    $("pickerSearch").value = "";
    state.pickerQuery = "";
    clearPickerError();
    renderPicker();
    $("pickerSearch").focus();
  }

  function applyPicker() {
    if (state.pickerMode === "parent") {
      if (state.pickerSelection.length > 1) {
        showPickerError("Pilih satu parent service.");
        return;
      }
      setParentService(state.pickerSelection.length ? state.pickerSelection[0] : null);
    } else {
      setChildIds(state.pickerSelection);
    }
    hideDialog($("pickerDialog"));
  }

  function resetFormRows() {
    var problemBody = $("problemTagList");
    if (problemBody) {
      problemBody.innerHTML = "";
      addProblemTagRow("", "equals", "");
    }
    var serviceBody = $("serviceTagList");
    if (serviceBody) {
      serviceBody.innerHTML = "";
      addServiceTagRow("", "");
    }
    var rulesBody = $("additionalRuleList");
    if (rulesBody) rulesBody.innerHTML = "";
  }

  function resetAddDialogState() {
    state.editId = null;
    state.parentId = null;
    state.childIds = [];
    clearChildFilter();
    var title = $("addTitle");
    var btn = $("btnSaveService");
    if (title) title.textContent = "New service check";
    if (btn) btn.textContent = "Add";
    setAdvanced(false);
    setFormTab("service");
    renderChildList();
  }

  function openAddDialog(parentId) {
    state.editId = null;
    state.parentId = parentId ? Number(parentId) : null;
    state.childIds = [];
    clearChildFilter();
    $("addTitle").textContent = "New service check";
    $("btnSaveService").textContent = "Add";
    $("formError").hidden = true;
    $("addForm").reset();
    $("svcSortOrder").value = "0";
    $("svcWeight").value = "0";
    $("svcStatusRule").value = "most_critical_child";
    $("svcPropagation").value = "as_is";
    $("svcDescription").value = "";
    $("svcParentSearch").value = "";
    resetFormRows();
    setAdvanced(false);
    setFormTab("service");
    setParentService(state.parentId);
    renderChildList();
    toggleFormFields();
    showDialog($("addDialog"));
    setTimeout(function () { $("svcName").focus(); }, 30);
  }

  function openEditDialog(id) {
    var svc = findService(id);
    if (!svc) {
      window.svcToast("Service tidak ditemukan.", "error");
      return;
    }
    state.editId = Number(svc.id);
    state.parentId = svc.parent_id ? Number(svc.parent_id) : null;
    state.childIds = state.services.filter(function (item) {
      return Number(item.parent_id) === Number(svc.id);
    }).map(function (item) { return Number(item.id); });
    clearChildFilter();
    $("addTitle").textContent = "Edit service check";
    $("btnSaveService").textContent = "Update";
    $("formError").hidden = true;
    $("addForm").reset();
    $("svcName").value = svc.name || "";
    $("svcType").value = String(svc.type || "http").toLowerCase();
    $("svcIp").value = svc.ip || "";
    $("svcPort").value = svc.port === null || svc.port === undefined ? "" : svc.port;
    $("svcUrl").value = svc.url || "";
    $("svcQuery").value = svc.query || "";
    $("svcExpected").value = svc.expected || "";
    $("svcSortOrder").value = svc.sort_order === null || svc.sort_order === undefined
      ? 0 : svc.sort_order;
    $("svcStatusRule").value = svc.status_rule || "most_critical_child";
    $("svcDescription").value = svc.description || "";
    $("svcPropagation").value = svc.propagation_rule || "as_is";
    $("svcWeight").value = svc.weight === null || svc.weight === undefined
      ? 0 : svc.weight;
    populateTagRows($("problemTagList"), svc.problem_tags, "problem");
    populateTagRows($("serviceTagList"), svc.tags, "service");
    var rulesBody = $("additionalRuleList");
    rulesBody.innerHTML = "";
    var rules = parseAdditionalRules(svc.additional_rules);
    rules.forEach(function (rule) { addAdditionalRuleRow(rule); });
    setAdvanced(
      rules.length > 0 ||
      String(svc.propagation_rule || "as_is") !== "as_is" ||
      Number(svc.weight || 0) !== 0
    );
    setFormTab("service");
    setParentService(state.parentId);
    renderChildList();
    toggleFormFields();
    showDialog($("addDialog"));
    setTimeout(function () { $("svcName").focus(); }, 30);
  }

  function closeAddDialog() {
    hideDialog($("addDialog"));
    resetAddDialogState();
  }

  function submitForm(e) {
    e.preventDefault();
    var type = $("svcType").value;
    var editing = state.editId !== null;
    var editId = state.editId;
    var tags = readFormTagRows($("serviceTagList"));
    var problemTags = readFormTagRows($("problemTagList"));
    var rules = [];
    Array.prototype.forEach.call(
      document.querySelectorAll("#additionalRuleList [data-rule-text]"),
      function (cell) {
        var value = (cell.textContent || "").trim();
        if (value) rules.push(value);
      }
    );
    var sortOrder = parseInt($("svcSortOrder").value, 10);
    if (isNaN(sortOrder)) sortOrder = 0;
    var weight = parseInt($("svcWeight").value, 10);
    if (isNaN(weight)) weight = 0;
    var payload = {
      name: $("svcName").value.trim(),
      type: type,
      ip: $("svcIp").value.trim(),
      tags: tags.join(", "),
      problem_tags: problemTags,
      sort_order: sortOrder,
      status_rule: $("svcStatusRule").value,
      description: $("svcDescription").value.trim(),
      propagation_rule: $("svcPropagation").value,
      weight: weight,
      additional_rules: rules,
      parent_id: state.parentId,
      child_ids: state.childIds.slice()
    };
    if (type === "tcp") payload.port = $("svcPort").value;
    if (type === "http") {
      payload.url = $("svcUrl").value.trim();
      payload.expected = $("svcExpected").value.trim();
    }
    if (type === "dns") {
      payload.query = $("svcQuery").value.trim();
      payload.expected = $("svcExpected").value.trim();
    }
    var error = "";
    if (!payload.name || !payload.ip) {
      error = "Name dan Host/IP wajib diisi.";
    } else if (sortOrder < 0 || sortOrder > 999) {
      error = "Sort order harus angka 0-999.";
    } else if (weight < 0 || weight > 1000000) {
      error = "Weight harus angka 0-1000000.";
    } else if (type === "tcp" && !payload.port) {
      error = "TCP port wajib diisi.";
    } else if (type === "http" && !payload.url) {
      error = "URL wajib diisi.";
    } else if (type === "dns" && !payload.query) {
      error = "DNS query wajib diisi.";
    }
    if (error) {
      $("formError").textContent = error;
      $("formError").hidden = false;
      setFormTab("service");
      return;
    }
    var btn = $("btnSaveService");
    btn.disabled = true;
    window.svcJson(editing ? "/api/services/" + encodeURIComponent(editId) : "/api/services", {
      method: editing ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    }).then(function (data) {
      closeAddDialog();
      window.svcToast(editing ? "Service berhasil diperbarui." : "Service berhasil ditambahkan.", "success");
      if (editing) state.selectedId = Number(editId);
      else if (data && data.id) state.selectedId = Number(data.id);
      setTimeout(loadServices, 700);
    }).catch(function (err) {
      $("formError").textContent = err.message;
      $("formError").hidden = false;
    }).then(function () { btn.disabled = false; });
  }

  function deleteService(id) {
    var svc = findService(id);
    if (!svc) return;
    if (!window.confirm('Hapus service "' + svc.name + '"? Riwayat pemeriksaannya juga akan dihapus.')) return;
    window.svcJson("/api/services/" + encodeURIComponent(id), { method: "DELETE" })
      .then(function () {
        window.svcToast("Service dihapus.", "success");
        if (Number(state.detailId) === Number(id)) closeDetail();
        if (Number(state.selectedId) === Number(id)) state.selectedId = null;
        setSelected(id, false);
        loadServices();
      })
      .catch(function (err) { window.svcToast(err.message, "error"); });
  }

  function bulkDelete() {
    var ids = state.selectedIds.slice();
    if (!ids.length) return;
    if (!window.confirm("Hapus " + ids.length + " service terpilih? Riwayat pemeriksaannya juga akan dihapus.")) return;
    var btn = $("btnBulkDelete");
    if (btn) btn.disabled = true;
    Promise.all(ids.map(function (id) {
      return window.svcJson("/api/services/" + encodeURIComponent(id), { method: "DELETE" });
    })).then(function () {
      window.svcToast(ids.length + " service dihapus.", "success");
      if (ids.indexOf(Number(state.detailId)) !== -1) closeDetail();
      state.selectedIds = [];
      loadServices();
    }).catch(function (err) {
      window.svcToast(err.message, "error");
      loadServices();
    }).then(function () {
      updateSelectionUi();
    });
  }

  function sslCheck(id) {
    window.svcJson("/api/services/" + encodeURIComponent(id) + "/ssl-check", { method: "POST" })
      .then(function () {
        window.svcToast("Pemeriksaan SSL dijadwalkan.", "info");
        setTimeout(loadServices, 3500);
      })
      .catch(function (err) { window.svcToast(err.message, "error"); });
  }

  function checkNow(button, message, id) {
    if (!button) return;
    button.disabled = true;
    var url = id
      ? "/api/services/" + encodeURIComponent(id) + "/check"
      : "/api/services/check";
    window.svcJson(url, { method: "POST" })
      .then(function () {
        window.svcToast(message || "Pemeriksaan service dijadwalkan.", "info");
        [1500, 4500].forEach(function (delay) {
          setTimeout(function () {
            loadServices().then(function () {
              if (state.detailId) loadHistory();
            });
          }, delay);
        });
      })
      .catch(function (err) { window.svcToast(err.message, "error"); })
      .then(function () { button.disabled = false; });
  }

  function setFilterOpen(open) {
    state.filterOpen = !!open;
    $("filterForm").hidden = !state.filterOpen;
    var panel = $("filterPanel");
    if (panel) panel.classList.toggle("is-collapsed", !state.filterOpen);
    var btn = $("btnToggleFilter");
    btn.setAttribute("aria-expanded", state.filterOpen ? "true" : "false");
    btn.classList.toggle("collapsed", !state.filterOpen);
    var text = $("filterToggleText");
    if (text) text.textContent = "Filter";
  }

  function setStatusFilter(value) {
    state.filterStatus = value || "all";
    Array.prototype.forEach.call(document.querySelectorAll("#filterStatus button[data-status]"), function (btn) {
      var on = btn.getAttribute("data-status") === state.filterStatus;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function setTagEval(value) {
    state.tagEval = value === "or" ? "or" : "and_or";
    Array.prototype.forEach.call(document.querySelectorAll("#filterTagsOp button[data-eval]"), function (btn) {
      var on = btn.getAttribute("data-eval") === state.tagEval;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function setTagScope(value) {
    state.tagScope = value === "any" || value === "problem" ? value : "service";
    Array.prototype.forEach.call(document.querySelectorAll("#filterTagsScope button[data-scope]"), function (btn) {
      var on = btn.getAttribute("data-scope") === state.tagScope;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function tagRowHtml() {
    return '<div class="zbx-tag-row" data-tag-row>' +
      '<input class="zbx-input zbx-tag-name" type="text" placeholder="tag" aria-label="Tag name" autocomplete="off">' +
      '<select class="zbx-input zbx-tag-op" aria-label="Tag operator">' +
        '<option value="exists">Exists</option>' +
        '<option value="equals">Equals</option>' +
        '<option value="contains" selected>Contains</option>' +
        '<option value="not_exists">Does not exist</option>' +
        '<option value="not_equal">Does not equal</option>' +
        '<option value="not_contains">Does not contain</option>' +
      "</select>" +
      '<input class="zbx-input zbx-tag-value" type="text" placeholder="value" aria-label="Tag value" autocomplete="off">' +
      '<button type="button" class="zbx-text-link zbx-remove-tag" data-remove-tag>Remove</button>' +
    "</div>";
  }

  function addTagRow() {
    var list = $("filterTagList");
    list.insertAdjacentHTML("beforeend", tagRowHtml());
    var rows = list.querySelectorAll("[data-tag-row]");
    var row = rows[rows.length - 1];
    var input = row && row.querySelector(".zbx-tag-name");
    if (input) input.focus();
  }

  function resetTagRows() {
    $("filterTagList").innerHTML = tagRowHtml();
  }

  function resetFilters() {
    $("filterName").value = "";
    $("filterNoChildren").checked = false;
    $("filterNoProblemTags").checked = false;
    setStatusFilter("all");
    setTagEval("and_or");
    setTagScope("service");
    resetTagRows();
    renderTable();
  }

  function bindDialogBackdrop(dialog, onClose) {
    if (!dialog) return;
    dialog.addEventListener("click", function (e) {
      if (e.target === dialog) onClose();
    });
  }

  function bind() {
    on("btnCreateService", "click", function () { openAddDialog(); });
    on("btnView", "click", function () { applyMode(MODE_VIEW); });
    on("btnEdit", "click", function () { applyMode(MODE_EDIT); });
    on("btnHelp", "click", function () { showDialog($("helpDialog")); });

    on("btnToggleFilter", "click", function () { setFilterOpen(!state.filterOpen); });
    on("filterForm", "submit", function (e) {
      e.preventDefault();
      renderTable();
    });
    on("filterName", "input", renderTable);
    on("filterStatus", "click", function (e) {
      var btn = e.target.closest("button[data-status]");
      if (!btn) return;
      setStatusFilter(btn.getAttribute("data-status"));
      renderTable();
    });
    on("filterTagsScope", "click", function (e) {
      var btn = e.target.closest("button[data-scope]");
      if (!btn) return;
      setTagScope(btn.getAttribute("data-scope"));
      renderTable();
    });
    on("filterTagsOp", "click", function (e) {
      var btn = e.target.closest("button[data-eval]");
      if (!btn) return;
      setTagEval(btn.getAttribute("data-eval"));
      renderTable();
    });
    on("filterNoChildren", "change", renderTable);
    on("filterNoProblemTags", "change", renderTable);
    on("filterTagList", "input", renderTable);
    on("filterTagList", "change", renderTable);
    on("filterTagList", "click", function (e) {
      var btn = e.target.closest("[data-remove-tag]");
      if (!btn) return;
      var row = btn.closest("[data-tag-row]");
      if (row) row.remove();
      renderTable();
    });
    on("btnAddTag", "click", function () {
      addTagRow();
      renderTable();
    });
    on("btnResetFilter", "click", resetFilters);

    on("selectAll", "change", function () {
      var rows = filteredServices();
      if (this.checked) {
        rows.forEach(function (svc) {
          if (!isSelected(svc.id)) state.selectedIds.push(Number(svc.id));
        });
      } else {
        rows.forEach(function (svc) { setSelected(svc.id, false); });
      }
      renderTable();
    });

    on("servicesBody", "change", function (e) {
      var cb = e.target.closest(".zbx-row-check");
      if (!cb) return;
      setSelected(cb.getAttribute("data-id"), cb.checked);
      var tr = cb.closest("tr[data-id]");
      if (tr) {
        tr.classList.toggle("selected", cb.checked || Number(tr.getAttribute("data-id")) === state.selectedId);
      }
      updateSelectionUi();
    });

    on("servicesBody", "click", function (e) {
      var act = e.target.closest("[data-act]");
      if (act) {
        var actId = act.getAttribute("data-id");
        var action = act.getAttribute("data-act");
        if (action === "retry") {
          loadServices();
        } else if (action === "reset") {
          resetFilters();
        } else if (action === "create") {
          openAddDialog();
        } else if (action === "view") {
          e.preventDefault();
          openDetail(actId);
        } else if (action === "edit") {
          openEditDialog(actId);
        } else if (action === "delete") {
          deleteService(actId);
        }
        return;
      }
      if (e.target.closest("input,button")) return;
      var tr = e.target.closest("tr[data-id]");
      if (!tr) return;
      if (state.mode === MODE_EDIT) {
        var id = Number(tr.getAttribute("data-id"));
        setSelected(id, !isSelected(id));
        renderTable();
      } else {
        markSelected(tr.getAttribute("data-id"));
      }
    });

    on("servicesBody", "dblclick", function (e) {
      if (e.target.closest("input,button")) return;
      var tr = e.target.closest("tr[data-id]");
      if (!tr) return;
      if (state.mode === MODE_EDIT) openEditDialog(tr.getAttribute("data-id"));
      else openDetail(tr.getAttribute("data-id"));
    });

    on("btnMassUpdate", "click", function () {
      if (!state.selectedIds.length) return;
      window.svcToast("Mass update belum tersedia.", "info");
    });
    on("btnBulkDelete", "click", bulkDelete);

    on("btnCloseDetail", "click", closeDetail);
    on("btnCloseDetailFoot", "click", closeDetail);
    on("btnDeleteService", "click", function () {
      if (state.detailId) deleteService(state.detailId);
    });
    on("btnEditService", "click", function () {
      if (!state.detailId) return;
      var id = state.detailId;
      closeDetail();
      openEditDialog(id);
    });
    on("btnCheckService", "click", function () {
      checkNow(
        this,
        "Pemeriksaan service ini dijadwalkan.",
        state.detailId
      );
    });
    on("btnSslCheck", "click", function () {
      if (state.detailId) sslCheck(state.detailId);
    });

    on("rangeSeg", "click", function (e) {
      var btn = e.target.closest("button[data-h]");
      if (!btn) return;
      state.hours = Number(btn.getAttribute("data-h"));
      detailRange();
      loadHistory();
    });

    on("btnCloseHelp", "click", function () { hideDialog($("helpDialog")); });
    on("btnCloseHelpFoot", "click", function () { hideDialog($("helpDialog")); });

    on("btnCloseDialog", "click", closeAddDialog);
    on("btnCancelDialog", "click", closeAddDialog);
    on("btnDialogHelp", "click", function () { showDialog($("helpDialog")); });
    on("svcType", "change", toggleFormFields);
    on("addForm", "submit", submitForm);

    on("addDialog", "click", function (e) {
      var tab = e.target.closest(".zbx-svc-tab");
      if (!tab) return;
      setFormTab(tab.getAttribute("data-tab"));
    });
    on("btnAdvanced", "click", function () {
      setAdvanced(this.getAttribute("aria-expanded") !== "true");
    });
    on("svcStatusRule", "change", updateStatusWarn);
    on("btnAddProblemTag", "click", function () {
      addProblemTagRow("", "equals", "");
    });
    on("problemTagList", "click", function (e) {
      var btn = e.target.closest("[data-remove-row]");
      if (!btn) return;
      var row = btn.closest("tr");
      if (row) row.remove();
    });
    on("problemTagList", "change", function (e) {
      var sel = e.target.closest("[data-tag-op]");
      if (sel) syncProblemTagRow(sel.closest("tr"));
    });
    on("btnAddServiceTag", "click", function () {
      addServiceTagRow("", "");
    });
    on("serviceTagList", "click", function (e) {
      var btn = e.target.closest("[data-remove-row]");
      if (!btn) return;
      var row = btn.closest("tr");
      if (row) row.remove();
    });
    on("btnAddRule", "click", openRuleDialog);
    on("additionalRuleList", "click", function (e) {
      var btn = e.target.closest("[data-remove-row]");
      if (!btn) return;
      var row = btn.closest("tr");
      if (row) row.remove();
    });
    on("btnCloseRule", "click", closeRuleDialog);
    on("btnRuleCancel", "click", closeRuleDialog);
    on("btnRuleAdd", "click", addRuleFromDialog);
    on("ruleCondition", "click", function (e) {
      e.stopPropagation();
      setRuleMenu(!ruleMenuOpen);
    });
    on("ruleCondition", "keydown", function (e) {
      if (e.key === "Escape") {
        e.stopPropagation();
        setRuleMenu(false);
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setRuleMenu(true);
        var first = $("ruleConditionMenu") && $("ruleConditionMenu").querySelector("[data-cond]");
        if (first) first.focus();
      }
    });
    on("ruleConditionMenu", "click", function (e) {
      var opt = e.target.closest("[data-cond]");
      if (!opt) return;
      var type = Number(opt.getAttribute("data-cond"));
      if (!isNaN(type)) ruleConditionType = type;
      syncRuleCondition();
      syncRuleLimitMax();
      setRuleMenu(false);
      if ($("ruleCondition")) $("ruleCondition").focus();
    });
    on("ruleConditionMenu", "keydown", function (e) {
      if (e.key !== "Escape") return;
      e.preventDefault();
      setRuleMenu(false);
      if ($("ruleCondition")) $("ruleCondition").focus();
    });
    on("ruleLimit", "input", function () {
      if ($("ruleError")) $("ruleError").hidden = true;
    });
    document.addEventListener("click", function (e) {
      if (!e.target.closest("#ruleCondition") && !e.target.closest("#ruleConditionMenu")) {
        setRuleMenu(false);
      }
    });
    syncRuleCondition();
    on("btnSelectParent", "click", function () { openPicker("parent"); });
    on("svcParentSearch", "keydown", function (e) {
      if (e.key !== "Enter") return;
      e.preventDefault();
      openPicker("parent");
    });
    on("btnAddChild", "click", function () {
      openPicker("child");
    });
    on("btnChildFilter", "click", applyChildFilter);
    on("btnChildReset", "click", resetChildFilter);
    on("childSearch", "keydown", function (e) {
      if (e.key !== "Enter") return;
      e.preventDefault();
      applyChildFilter();
    });
    on("childServiceList", "click", function (e) {
      var btn = e.target.closest("[data-remove-child]");
      if (!btn) return;
      var id = Number(btn.getAttribute("data-remove-child"));
      setChildIds(state.childIds.filter(function (childId) {
        return childId !== id;
      }));
    });

    on("btnClosePicker", "click", cancelPicker);
    on("btnPickerCancel", "click", cancelPicker);
    on("btnPickerSelect", "click", applyPicker);
    on("btnPickerFilter", "click", applyPickerFilter);
    on("btnPickerReset", "click", resetPickerFilter);
    on("pickerSearch", "input", clearPickerError);
    on("pickerSearch", "keydown", function (e) {
      if (e.key !== "Enter") return;
      e.preventDefault();
      applyPickerFilter();
    });
    on("pickerSelectAll", "change", function () {
      var checked = this.checked;
      var ids = pickerVisibleIds();
      clearPickerError();
      if (state.pickerMode === "parent") {
        state.pickerSelection = checked ? ids.slice() : [];
      } else {
        ids.forEach(function (id) {
          var idx = state.pickerSelection.indexOf(id);
          if (checked && idx === -1) state.pickerSelection.push(id);
          if (!checked && idx !== -1) state.pickerSelection.splice(idx, 1);
        });
      }
      syncPickerCheckboxes();
    });
    on("pickerList", "click", function (e) {
      if (e.target.closest("input")) return;
      var row = e.target.closest("tr[data-picker-id]");
      if (!row) return;
      var input = row.querySelector('input[name="pickerPick"]');
      if (!input) return;
      if (state.pickerMode === "parent") {
        var id = Number(input.value);
        var onlySelected = state.pickerSelection.length === 1 &&
          state.pickerSelection[0] === id;
        state.pickerSelection = onlySelected ? [] : [id];
        clearPickerError();
        syncPickerCheckboxes();
        return;
      }
      input.checked = !input.checked;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
    on("pickerList", "change", function (e) {
      var input = e.target.closest('input[name="pickerPick"]');
      if (!input) return;
      var id = Number(input.value);
      clearPickerError();
      if (state.pickerMode === "parent") {
        state.pickerSelection = input.checked ? [id] : [];
      } else {
        var idx = state.pickerSelection.indexOf(id);
        if (input.checked && idx === -1) state.pickerSelection.push(id);
        if (!input.checked && idx !== -1) state.pickerSelection.splice(idx, 1);
      }
      syncPickerCheckboxes();
    });

    bindDialogBackdrop($("detailDialog"), closeDetail);
    bindDialogBackdrop($("helpDialog"), function () { hideDialog($("helpDialog")); });
    bindDialogBackdrop($("addDialog"), closeAddDialog);
    bindDialogBackdrop($("ruleDialog"), closeRuleDialog);
    bindDialogBackdrop($("pickerDialog"), cancelPicker);
    on("ruleDialog", "close", function () { setRuleMenu(false); });
    on("detailDialog", "close", resetDetailState);
    on("addDialog", "close", resetAddDialogState);
    on("pickerDialog", "close", function () {
      if (state.pickerMode === "parent") {
        var svc = state.parentId ? findService(state.parentId) : null;
        if ($("svcParentSearch")) {
          $("svcParentSearch").value = svc ? svc.name : "";
        }
      }
      state.pickerMode = "";
      state.pickerQuery = "";
      clearPickerError();
    });

    document.addEventListener("nms-theme", function () {
      if (state.chart) renderHistoryChart();
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    bind();
    setFilterOpen(true);
    applyMode(MODE_VIEW, true);
    loadServices();
    setInterval(function () {
      if (!document.hidden) loadServices();
    }, 30000);
  });
})();
