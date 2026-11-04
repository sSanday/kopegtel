/* ============================================================
   NMS Kopegtel — halaman Inventory > Overview
   (Host inventory overview): hitungan host per field inventory.
   ============================================================ */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  var state = {
    groupBy: "",
    groups: [],
    hostGroups: [],
    fields: [],
    sort: "desc",
    filterOpen: true,
    loaded: false,
    data: null,
  };

  function esc(value) {
    return window.svcEsc ? window.svcEsc(value) : String(value == null ? "" : value);
  }

  function emptyRow(colspan) {
    return '<tr><td colspan="' + colspan + '" class="zbx-sla-empty-cell">' +
      '<div class="zbx-sla-empty">' +
      '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" aria-hidden="true">' +
      '<circle cx="20" cy="20" r="11.5"></circle><path d="M28.5 28.5 39 39"></path></svg>' +
      "<span>No data found</span></div></td></tr>";
  }

  function setFilterOpen(open) {
    state.filterOpen = open;
    var panel = $("invFilterPanel");
    var body = $("invFilterBody");
    var button = $("btnToggleInvFilter");
    if (panel) panel.classList.toggle("is-collapsed", !open);
    if (body) body.hidden = !open;
    if (button) {
      button.classList.toggle("collapsed", !open);
      button.setAttribute("aria-expanded", open ? "true" : "false");
    }
  }

  /* ---------- filter ---------- */

  function fillGroupBySelect(fields) {
    var select = $("invGroupBy");
    if (!select) return;
    var html = '<option value="">not selected</option>';
    for (var i = 0; i < fields.length; i += 1) {
      html += '<option value="' + esc(fields[i].key) + '">' + esc(fields[i].label) + "</option>";
    }
    select.innerHTML = html;
    select.value = state.groupBy;
  }

  function groupTokens() {
    var input = $("invHostGroups");
    if (!input) return [];
    return String(input.value || "")
      .split(",")
      .map(function (t) { return t.trim(); })
      .filter(function (t) { return t !== ""; });
  }

  /** Cocokkan isi kolom dengan daftar group: nama sama atau mengandung kata kunci. */
  function resolveGroups() {
    var tokens = groupTokens();
    var out = [];
    for (var i = 0; i < tokens.length; i += 1) {
      var needle = tokens[i].toLowerCase();
      for (var j = 0; j < state.hostGroups.length; j += 1) {
        var name = state.hostGroups[j];
        var lower = name.toLowerCase();
        if ((lower === needle || lower.indexOf(needle) !== -1) && out.indexOf(name) === -1) {
          out.push(name);
        }
      }
    }
    return out;
  }

  function applyGroupInput() {
    var input = $("invHostGroups");
    if (!input) return;
    state.groups = resolveGroups();
    input.value = state.groups.join(", ");
  }

  function query() {
    var params = ["group_by=" + encodeURIComponent(state.groupBy)];
    if (state.groups.length) {
      params.push("groups=" + encodeURIComponent(state.groups.join(",")));
    }
    return params.join("&");
  }

  /* ---------- tabel ---------- */

  function renderTable(data) {
    var body = $("invBody");
    if (!body) return;
    var head = $("invCountHead");
    if (head) head.setAttribute("aria-sort", state.sort === "asc" ? "ascending" : "descending");

    var groups = (data.groups || []).slice();
    if (!groups.length) {
      body.innerHTML = emptyRow(2);
      return;
    }
    groups.sort(function (a, b) {
      if (a.count !== b.count) return state.sort === "asc" ? a.count - b.count : b.count - a.count;
      return a.value.localeCompare(b.value);
    });

    var html = "";
    for (var i = 0; i < groups.length; i += 1) {
      var cls = groups[i].empty ? ' class="inv-row-empty"' : "";
      html += "<tr" + cls + '><td class="inv-value">' + esc(groups[i].value) + "</td>" +
        '<td class="inv-count">' + esc(groups[i].count) + "</td></tr>";
    }
    body.innerHTML = html;
  }

  function setSort(direction) {
    state.sort = direction === "asc" ? "asc" : "desc";
    var arrow = $("invSortArrow");
    if (arrow) arrow.classList.toggle("desc", state.sort === "desc");
    if (state.data) renderTable(state.data);
  }

  /* ---------- pemilih host groups ---------- */

  function groupChecks() {
    return document.querySelectorAll("#invGroupList .inv-group-check");
  }

  /** Selaraskan checkbox "pilih semua" di kepala tabel dengan baris di bawahnya. */
  function syncSelectAll() {
    var all = $("invGroupSelectAll");
    if (!all) return;
    var checks = groupChecks();
    var selected = 0;
    for (var i = 0; i < checks.length; i += 1) {
      if (checks[i].checked) selected += 1;
    }
    all.checked = checks.length > 0 && selected === checks.length;
    all.indeterminate = selected > 0 && selected < checks.length;
  }

  function renderGroupList() {
    var list = $("invGroupList");
    if (!list) return;
    var html = "";
    for (var i = 0; i < state.hostGroups.length; i += 1) {
      var name = state.hostGroups[i];
      var id = "invGroupCheck" + i;
      var checked = state.groups.indexOf(name) !== -1 ? " checked" : "";
      html += '<tr class="inv-group-row">' +
        '<td class="inv-group-check-col">' +
        '<input type="checkbox" class="inv-group-check" id="' + id + '" value="' + esc(name) + '"' + checked + ">" +
        "</td>" +
        '<td><label class="inv-group-name" for="' + id + '">' + esc(name) + "</label></td></tr>";
    }
    if (!state.hostGroups.length) {
      html = '<tr class="inv-group-empty-row"><td colspan="2" class="inv-group-empty">' +
        "Belum ada host group (category) pada host.</td></tr>";
    }
    list.innerHTML = html;
    syncSelectAll();
  }

  function openGroupDialog() {
    var dialog = $("invGroupDialog");
    renderGroupList();
    if (dialog && dialog.showModal) dialog.showModal();
  }

  function commitGroupDialog() {
    var checked = document.querySelectorAll(".inv-group-check:checked");
    var names = [];
    for (var i = 0; i < checked.length; i += 1) names.push(checked[i].value);
    state.groups = names;
    var input = $("invHostGroups");
    if (input) input.value = names.join(", ");
    var dialog = $("invGroupDialog");
    if (dialog) dialog.close();
  }

  /* ---------- muat data ---------- */

  function load() {
    return window.svcJson("/api/inventory/overview?" + query())
      .then(function (data) {
        state.data = data;
        state.hostGroups = data.host_groups || [];
        if (!state.loaded) {
          state.fields = data.fields || [];
          fillGroupBySelect(state.fields);
          state.loaded = true;
        }
        renderTable(data);
      })
      .catch(function (err) {
        var body = $("invBody");
        if (body) body.innerHTML = emptyRow(2);
        window.svcToast("Gagal memuat inventory: " + err.message, "error");
      });
  }

  function init() {
    var form = $("invFilterForm");
    if (form) form.addEventListener("submit", function (e) {
      e.preventDefault();
      applyGroupInput();
      var select = $("invGroupBy");
      state.groupBy = select ? select.value : "";
      load();
    });

    var reset = $("btnResetInvFilter");
    if (reset) reset.addEventListener("click", function () {
      state.groups = [];
      state.groupBy = "";
      var input = $("invHostGroups");
      if (input) input.value = "";
      var select = $("invGroupBy");
      if (select) select.value = "";
      load();
    });

    var toggle = $("btnToggleInvFilter");
    if (toggle) toggle.addEventListener("click", function () {
      setFilterOpen(!state.filterOpen);
    });

    var sortBtn = $("invSortCount");
    if (sortBtn) sortBtn.addEventListener("click", function () {
      setSort(state.sort === "desc" ? "asc" : "desc");
    });

    var selectBtn = $("btnInvSelectGroups");
    if (selectBtn) selectBtn.addEventListener("click", openGroupDialog);

    var groupList = $("invGroupList");
    if (groupList) groupList.addEventListener("change", function (e) {
      if (e.target && e.target.classList.contains("inv-group-check")) syncSelectAll();
    });

    var selectAll = $("invGroupSelectAll");
    if (selectAll) selectAll.addEventListener("change", function () {
      var checks = groupChecks();
      for (var i = 0; i < checks.length; i += 1) checks[i].checked = selectAll.checked;
      syncSelectAll();
    });

    var groupApply = $("btnInvGroupApply");
    if (groupApply) groupApply.addEventListener("click", commitGroupDialog);
    var groupCancel = $("btnInvGroupCancel");
    if (groupCancel) groupCancel.addEventListener("click", function () {
      var dialog = $("invGroupDialog");
      if (dialog) dialog.close();
    });
    var groupClose = $("btnCloseInvGroupDialog");
    if (groupClose) groupClose.addEventListener("click", function () {
      var dialog = $("invGroupDialog");
      if (dialog) dialog.close();
    });

    var help = $("btnInvHelp");
    if (help) help.addEventListener("click", function () {
      var dialog = $("invHelpDialog");
      if (dialog && dialog.showModal) dialog.showModal();
    });
    var closeHelp = $("btnCloseInvHelp");
    if (closeHelp) closeHelp.addEventListener("click", function () {
      var dialog = $("invHelpDialog");
      if (dialog) dialog.close();
    });

    load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
