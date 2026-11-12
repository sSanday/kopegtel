/* Interaksi halaman Data collection: filter, pilih baris, ekspor, dan modal. */
(function () {
  "use strict";

  var filterToggle = document.getElementById("dcFilterToggle");
  var filterPanel = document.getElementById("dcFilterPanel");
  var filterInput = document.getElementById("dcFilterName");

  if (filterToggle && filterPanel) {
    filterToggle.addEventListener("click", function () {
      var open = filterToggle.getAttribute("aria-expanded") === "true";
      filterToggle.setAttribute("aria-expanded", open ? "false" : "true");
      if (open) {
        filterPanel.setAttribute("hidden", "");
      } else {
        filterPanel.removeAttribute("hidden");
        if (filterInput) filterInput.focus();
      }
    });
  }

  var selectAll = document.getElementById("dcSelectAll");
  var rowChecks = Array.prototype.slice.call(
    document.querySelectorAll(".dc-row-check")
  );
  var selectedCount = document.getElementById("dcSelectedCount");
  var deleteBtn = document.getElementById("dcDeleteBtn");
  var deleteClearBtn = document.getElementById("dcDeleteClearBtn");
  var deleteForm = document.getElementById("dc-delete-form");
  var exportWrap = document.querySelector(".dc-export");
  var exportMenu = document.getElementById("dcExportMenu");
  var exportToggle = document.getElementById("dcExportToggle");
  var exportMain = document.getElementById("dcExportBtn");
  var exportUrl = exportWrap ? exportWrap.getAttribute("data-export-url") : "";

  function checkedRows() {
    return rowChecks.filter(function (checkbox) {
      return checkbox.checked;
    });
  }

  function closeExportMenu() {
    if (exportMenu) exportMenu.setAttribute("hidden", "");
    if (exportToggle) exportToggle.setAttribute("aria-expanded", "false");
  }

  function updateSelection() {
    var count = checkedRows().length;
    if (selectedCount) selectedCount.textContent = String(count);
    if (deleteBtn) deleteBtn.disabled = count === 0;
    if (deleteClearBtn) deleteClearBtn.disabled = count === 0;
    document.querySelectorAll("[data-dc-export]").forEach(function (button) {
      button.disabled = count === 0;
    });
    if (count === 0) closeExportMenu();
    if (selectAll) {
      selectAll.checked = rowChecks.length > 0 && count === rowChecks.length;
      selectAll.indeterminate = count > 0 && count < rowChecks.length;
    }
  }

  if (selectAll) {
    selectAll.addEventListener("change", function () {
      rowChecks.forEach(function (checkbox) {
        checkbox.checked = selectAll.checked;
      });
      updateSelection();
    });
  }

  rowChecks.forEach(function (checkbox) {
    checkbox.addEventListener("change", updateSelection);
  });
  updateSelection();

  function selectedValues() {
    return checkedRows().map(function (checkbox) {
      return checkbox.value;
    });
  }

  function exportSelected(format) {
    if (!exportUrl) return;
    var params = [];
    selectedValues().forEach(function (value) {
      params.push(["selected", value]);
    });
    params.push(["format", format]);
    var query = params
      .map(function (pair) {
        return (
          encodeURIComponent(pair[0]) + "=" + encodeURIComponent(pair[1])
        );
      })
      .join("&");
    window.location.href =
      exportUrl + (exportUrl.indexOf("?") >= 0 ? "&" : "?") + query;
  }

  if (exportMain) {
    exportMain.addEventListener("click", function () {
      exportSelected("csv");
    });
  }

  if (exportToggle && exportMenu) {
    exportToggle.addEventListener("click", function (event) {
      event.stopPropagation();
      var willOpen = exportMenu.hasAttribute("hidden");
      closeExportMenu();
      if (willOpen) {
        exportMenu.removeAttribute("hidden");
        exportToggle.setAttribute("aria-expanded", "true");
      }
    });

    exportMenu
      .querySelectorAll("[data-export-format]")
      .forEach(function (button) {
        button.addEventListener("click", function () {
          exportSelected(button.getAttribute("data-export-format"));
        });
      });
  }

  document.addEventListener("click", function (event) {
    if (exportMenu && exportWrap && !exportWrap.contains(event.target)) {
      closeExportMenu();
    }
  });

  /* Pemilih daftar sederhana untuk filter Template groups / Linked templates. */
  document.querySelectorAll("[data-pick-toggle]").forEach(function (button) {
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      var menu = document.getElementById(
        button.getAttribute("data-pick-toggle")
      );
      var willOpen = menu && menu.hasAttribute("hidden");
      document
        .querySelectorAll(".dc-tpl-pick-menu:not([hidden])")
        .forEach(function (other) {
          other.setAttribute("hidden", "");
        });
      if (menu && willOpen) menu.removeAttribute("hidden");
    });
  });

  document.querySelectorAll("[data-pick-input]").forEach(function (option) {
    option.addEventListener("click", function () {
      var input = document.getElementById(
        option.getAttribute("data-pick-input")
      );
      if (input) {
        input.value = option.getAttribute("data-pick-value") || "";
        input.focus();
      }
      var menu = option.closest(".dc-tpl-pick-menu");
      if (menu) menu.setAttribute("hidden", "");
    });
  });

  document.addEventListener("click", function (event) {
    document
      .querySelectorAll(".dc-tpl-pick-menu:not([hidden])")
      .forEach(function (menu) {
        if (!menu.parentElement.contains(event.target)) {
          menu.setAttribute("hidden", "");
        }
      });
  });

  /* Baris filter tag dinamis. */
  var tagRows = document.getElementById("dcTagRows");
  var addTag = document.getElementById("dcAddTag");

  function bindTagRow(row) {
    var remove = row.querySelector(".dc-tpl-remove");
    if (!remove) return;
    remove.addEventListener("click", function () {
      if (!tagRows) return;
      var rows = tagRows.querySelectorAll(".dc-tpl-tag-row");
      if (rows.length > 1) {
        row.remove();
        return;
      }
      row.querySelectorAll("input").forEach(function (input) {
        input.value = "";
      });
      var select = row.querySelector("select");
      if (select) select.value = "contains";
    });
  }

  if (tagRows) {
    tagRows.querySelectorAll(".dc-tpl-tag-row").forEach(bindTagRow);
  }

  if (addTag && tagRows) {
    addTag.addEventListener("click", function () {
      var template = tagRows.querySelector(".dc-tpl-tag-row");
      if (!template) return;
      var clone = template.cloneNode(true);
      clone.querySelectorAll("input").forEach(function (input) {
        input.value = "";
      });
      var label = clone.querySelector(".dc-tpl-filter-label");
      if (label) label.textContent = "";
      var select = clone.querySelector("select");
      if (select) select.value = "contains";
      tagRows.appendChild(clone);
      bindTagRow(clone);
      var first = clone.querySelector('input[name="tag"]');
      if (first) first.focus();
    });
  }

  var tagEval = document.getElementById("dcTagEval");
  document.querySelectorAll("[data-tag-eval]").forEach(function (button) {
    button.addEventListener("click", function () {
      var value = button.getAttribute("data-tag-eval");
      if (tagEval) tagEval.value = value;
      document
        .querySelectorAll("[data-tag-eval]")
        .forEach(function (other) {
          other.classList.toggle("active", other === button);
        });
    });
  });

  if (deleteForm) {
    deleteForm.addEventListener("submit", function (event) {
      var count = checkedRows().length;
      if (!count) {
        event.preventDefault();
        return;
      }
      var entity = (deleteForm.getAttribute("data-dc-entity") || "item").trim();
      var label = count === 1 ? entity : entity + "s";
      if (!window.confirm("Delete " + count + " selected " + label + "?")) {
        event.preventDefault();
      }
    });
  }

  function setupModal(modal, openBtn, focusElement) {
    if (!modal) return;
    var lastFocus = null;

    function openModal() {
      lastFocus = document.activeElement;
      modal.classList.add("open");
      document.body.style.overflow = "hidden";
      if (focusElement) {
        if (focusElement.tagName === "INPUT") focusElement.value = "";
        window.setTimeout(function () {
          focusElement.focus();
        }, 0);
      }
    }

    function closeModal() {
      modal.classList.remove("open");
      document.body.style.overflow = "";
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    }

    if (openBtn) openBtn.addEventListener("click", openModal);
    modal.querySelectorAll("[data-dc-modal-close]").forEach(function (button) {
      button.addEventListener("click", closeModal);
    });
    modal.addEventListener("click", function (event) {
      if (event.target === modal) closeModal();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && modal.classList.contains("open")) {
        closeModal();
      }
    });
  }

  setupModal(
    document.getElementById("dcCreateModal"),
    document.getElementById("dcCreateOpen"),
    document.getElementById("dcCreateName")
  );
  setupModal(
    document.getElementById("dcImportModal"),
    document.getElementById("dcImportOpen"),
    document.getElementById("dcImportText")
  );
})();
