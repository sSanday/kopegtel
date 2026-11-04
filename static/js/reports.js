/* ============================================================
   NMS Kopegtel — aksi halaman Reports.
   Kirim laporan harian dan pesan uji notifikasi.
   ============================================================ */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  function toast(message, type) {
    if (window.svcToast) {
      window.svcToast(message, type);
      return;
    }
    if (type === "error") window.alert(message);
  }

  function postJson(url, button, busyText, payload) {
    var oldText = button.innerHTML;
    button.disabled = true;
    button.textContent = busyText;
    var headers = {
      "Content-Type": "application/json",
      "X-Requested-With": "XMLHttpRequest"
    };
    return fetch(url, {
      method: "POST",
      headers: headers,
      body: JSON.stringify(payload || {}),
    })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (!response.ok) {
            throw new Error(data && data.error ? data.error : "HTTP " + response.status);
          }
          return data;
        });
      })
      .then(function (data) {
        return data;
      })
      .catch(function (error) {
        toast(error.message || "Permintaan gagal.", "error");
        button.disabled = false;
        button.innerHTML = oldText;
        throw error;
      });
  }

  var daily = $("btnSendDaily");
  if (daily) {
    daily.addEventListener("click", function () {
      if (!window.confirm("Kirim laporan harian sekarang ke channel notifikasi yang aktif?")) {
        return;
      }
      postJson("/api/reports/daily/send", daily, "Mengirim…")
        .then(function () {
          toast("Laporan harian berhasil dikirim.", "success");
          window.setTimeout(function () { window.location.reload(); }, 900);
        })
        .catch(function () {});
    });
  }

  var test = $("btnNotifyTest");
  if (test) {
    test.addEventListener("click", function () {
      if (!window.confirm("Kirim pesan uji ke semua channel notifikasi yang aktif?")) {
        return;
      }
      postJson("/api/notify/test", test, "Mengirim…")
        .then(function (data) {
          var results = (data && data.results) || {};
          var parts = [];
          Object.keys(results).forEach(function (key) {
            var item = results[key] || {};
            parts.push(key + "=" + (item.result || "skipped"));
          });
          toast(parts.length ? "Hasil: " + parts.join(", ") : "Pesan uji selesai.", "success");
          window.setTimeout(function () { window.location.reload(); }, 1200);
        })
        .catch(function () {});
    });
  }

  var jobsTable = $("rptJobsTable");
  if (jobsTable && $("rptSelectedCount")) {
    var rowChecks = Array.prototype.slice.call(jobsTable.querySelectorAll(".rpt-row-check"));
    var checkAll = $("rptCheckAll");
    var countLabel = $("rptSelectedCount");
    var btnEnable = $("btnJobsEnable");
    var btnDisable = $("btnJobsDisable");
    var btnDelete = $("btnJobsDelete");
    var actionsBar = $("rptActionsBar");

    function selectedIds() {
      return rowChecks
        .filter(function (box) { return box.checked; })
        .map(function (box) { return box.value; });
    }

    function refreshSelection() {
      var ids = selectedIds();
      countLabel.textContent = ids.length + " selected";
      if (actionsBar) actionsBar.hidden = ids.length === 0;
      if (btnEnable) btnEnable.disabled = ids.length === 0;
      if (btnDisable) btnDisable.disabled = ids.length === 0;
      if (btnDelete) btnDelete.disabled = ids.length === 0;
      if (checkAll) {
        checkAll.checked = rowChecks.length > 0 && ids.length === rowChecks.length;
        checkAll.indeterminate = ids.length > 0 && ids.length < rowChecks.length;
      }
    }

    rowChecks.forEach(function (box) {
      box.addEventListener("change", refreshSelection);
    });

    if (checkAll) {
      checkAll.addEventListener("change", function () {
        rowChecks.forEach(function (box) { box.checked = checkAll.checked; });
        refreshSelection();
      });
    }

    function requireUpdated(data, button, oldHtml) {
      var updated = (data && data.updated) || [];
      if (!updated.length) {
        button.disabled = false;
        button.innerHTML = oldHtml;
        toast(
          "No scheduler job was updated. Restart the application and try again.",
          "error"
        );
        throw new Error("no-scheduler-job");
      }
      return updated;
    }

    function bulkUpdate(action, message, button) {
      var ids = selectedIds();
      if (!ids.length) return;
      if (!window.confirm(message)) return;
      var oldHtml = button.innerHTML;
      postJson("/api/reports/scheduled/bulk", button, "Processing…", {
        action: action,
        ids: ids,
      })
        .then(function (data) {
          var updated = requireUpdated(data, button, oldHtml);
          toast(updated.length + " report(s) updated.", "success");
          window.setTimeout(function () { window.location.reload(); }, 800);
        })
        .catch(function () {});
    }

    if (btnEnable) {
      btnEnable.addEventListener("click", function () {
        bulkUpdate("enable", "Enable selected scheduled report(s)?", btnEnable);
      });
    }
    if (btnDisable) {
      btnDisable.addEventListener("click", function () {
        bulkUpdate("disable", "Disable selected scheduled report(s)?", btnDisable);
      });
    }
    if (btnDelete) {
      btnDelete.addEventListener("click", function () {
        toast("System reports cannot be deleted.", "error");
      });
    }

    // Klik status (Enabled/Disabled) untuk toggle, seperti Zabbix.
    Array.prototype.forEach.call(
      document.querySelectorAll(".rpt-sched-status-toggle"),
      function (link) {
        link.addEventListener("click", function () {
          var jobId = link.getAttribute("data-job-id");
          if (!jobId) return;
          var enable = link.getAttribute("data-enable") === "1";
          var oldHtml = link.innerHTML;
          postJson("/api/reports/scheduled/bulk", link, "Updating…", {
            action: enable ? "enable" : "disable",
            ids: [jobId],
          })
            .then(function (data) {
              requireUpdated(data, link, oldHtml);
              toast("Report " + (enable ? "enabled" : "disabled") + ".", "success");
              window.setTimeout(function () { window.location.reload(); }, 600);
            })
            .catch(function () {});
        });
      }
    );

    refreshSelection();
  }

  // Filter toggle
  var filterToggle = $("rptFilterToggle");
  var filterCard = $("rptFilterCard");
  if (filterToggle && filterCard) {
    filterToggle.addEventListener("click", function () {
      filterCard.classList.toggle("collapsed");
      filterToggle.setAttribute("aria-expanded", !filterCard.classList.contains("collapsed"));
    });
  }

  // ============================================================
  // Scheduled Report Create Form View & Modals
  // ============================================================
  var createForm = $("rptSchedCreateForm");
  var selectModal = $("rptSelectModal");
  var selectModalTitle = $("rptSelectModalTitle");
  var selectModalBody = $("rptSelectModalBody");
  var btnSelectApply = $("btnSelectModalApply");
  var btnSelectClose = $("btnSelectModalClose");
  var btnSelectCancel = $("btnSelectModalCancel");

  var currentSelectCallback = null;

  function openSelectModal(title, options, defaultValue, onSelect) {
    if (!selectModal || !selectModalBody) return;
    currentSelectCallback = onSelect;
    if (selectModalTitle) selectModalTitle.textContent = title;

    var html = '<div class="rpt-select-list">';
    options.forEach(function (opt, idx) {
      var isChecked = opt === defaultValue || (idx === 0 && !defaultValue);
      html += '<label class="rpt-select-item">' +
        '<input type="radio" name="selModalRadio" value="' + opt.replace(/"/g, '&quot;') + '"' + (isChecked ? ' checked' : '') + '> ' +
        '<span>' + opt + '</span>' +
        '</label>';
    });
    html += '</div>';
    selectModalBody.innerHTML = html;
    selectModal.style.display = "flex";
    selectModal.setAttribute("aria-hidden", "false");
  }

  function closeSelectModal() {
    if (selectModal) {
      selectModal.style.display = "none";
      selectModal.setAttribute("aria-hidden", "true");
    }
    currentSelectCallback = null;
  }

  if (btnSelectClose) btnSelectClose.addEventListener("click", closeSelectModal);
  if (btnSelectCancel) btnSelectCancel.addEventListener("click", closeSelectModal);
  if (btnSelectApply) {
    btnSelectApply.addEventListener("click", function () {
      var checked = selectModalBody ? selectModalBody.querySelector('input[name="selModalRadio"]:checked') : null;
      var val = checked ? checked.value : "";
      if (typeof currentSelectCallback === "function") {
        currentSelectCallback(val);
      }
      closeSelectModal();
    });
  }
  if (selectModal) {
    selectModal.addEventListener("click", function (e) {
      if (e.target === selectModal) closeSelectModal();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && selectModal.style.display !== "none") closeSelectModal();
    });
  }

  if (createForm) {
    // Cycle-specific schedule fields (Weekly displays repeat days).
    var cycleRadios = Array.prototype.slice.call(createForm.querySelectorAll('input[name="cycle"]'));
    var weeklyOptions = $("rptWeeklyOptions");

    function updateCycleFields() {
      var checkedCycle = createForm.querySelector('input[name="cycle"]:checked');
      var isWeekly = checkedCycle && checkedCycle.value === "Weekly";
      if (weeklyOptions) weeklyOptions.hidden = !isWeekly;
    }

    cycleRadios.forEach(function (radio) {
      radio.addEventListener("change", updateCycleFields);
    });
    updateCycleFields();

    // Users Modal for Owner Selection (persis tampilan Zabbix)
    var usersModal = $("rptUsersModal");
    var btnUsersClose = $("btnUsersModalClose");
    var btnUsersCancel = $("btnUsersModalCancel");
    var usersList = $("rptUsersList");
    var inputOwner = $("rptOwner");
    var btnOwner = $("btnSelectOwner");
    var ownerBox = $("rptOwnerBox");
    var ownerChipText = $("rptOwnerChipText");
    var btnOwnerRemove = $("btnOwnerChipRemove");
    var currentUserSelectCallback = null;

    function setOwner(value) {
      var display = (value || "").trim();
      if (inputOwner) inputOwner.value = display;
      if (ownerChipText) ownerChipText.textContent = display;
      if (ownerBox) ownerBox.classList.toggle("is-empty", !display);
    }

    function openUsersModal(callback) {
      currentUserSelectCallback = callback || null;
      if (usersModal) {
        usersModal.style.display = "flex";
        usersModal.setAttribute("aria-hidden", "false");
      }
    }

    function closeUsersModal() {
      if (usersModal) {
        usersModal.style.display = "none";
        usersModal.setAttribute("aria-hidden", "true");
      }
      currentUserSelectCallback = null;
    }

    if (btnUsersClose) btnUsersClose.addEventListener("click", closeUsersModal);
    if (btnUsersCancel) btnUsersCancel.addEventListener("click", closeUsersModal);
    if (usersModal) {
      usersModal.addEventListener("click", function (e) {
        if (e.target === usersModal) closeUsersModal();
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && usersModal.style.display !== "none") closeUsersModal();
      });
    }

    if (usersList) {
      usersList.addEventListener("click", function (e) {
        var target = e.target.closest(".rpt-user-link") || e.target.closest("tr");
        if (target) {
          var displayVal = target.getAttribute("data-display") || target.getAttribute("data-username") || target.innerText.trim();
          if (typeof currentUserSelectCallback === "function") {
            currentUserSelectCallback(displayVal);
          } else if (displayVal) {
            setOwner(displayVal);
          }
          closeUsersModal();
        }
      });
    }

    if (btnOwner) {
      btnOwner.addEventListener("click", function () {
        openUsersModal(null);
      });
    }

    if (ownerBox) {
      ownerBox.addEventListener("click", function (e) {
        if (e.target.closest(".rpt-sched-owner-chip-remove")) return;
        openUsersModal(null);
      });
    }

    if (btnOwnerRemove) {
      btnOwnerRemove.addEventListener("click", function (e) {
        e.stopPropagation();
        setOwner("");
        if (btnOwner) btnOwner.focus();
      });
    }

    setOwner(inputOwner ? inputOwner.value : "");

    // Dashboards Modal for Dashboard Selection (persis tampilan Zabbix)
    var dashboardsModal = $("rptDashboardsModal");
    var btnDashboardsClose = $("btnDashboardsModalClose");
    var btnDashboardsCancel = $("btnDashboardsModalCancel");
    var dashboardsList = $("rptDashboardsList");
    var btnDashboard = $("btnSelectDashboard");
    var inputDashboard = $("rptDashboard");

    function closeDashboardsModal() {
      if (dashboardsModal) {
        dashboardsModal.style.display = "none";
        dashboardsModal.setAttribute("aria-hidden", "true");
      }
    }

    if (btnDashboardsClose) btnDashboardsClose.addEventListener("click", closeDashboardsModal);
    if (btnDashboardsCancel) btnDashboardsCancel.addEventListener("click", closeDashboardsModal);
    if (dashboardsModal) {
      dashboardsModal.addEventListener("click", function (e) {
        if (e.target === dashboardsModal) closeDashboardsModal();
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && dashboardsModal.style.display !== "none") closeDashboardsModal();
      });
    }

    if (dashboardsList) {
      dashboardsList.addEventListener("click", function (e) {
        var link = e.target.closest(".rpt-dashboard-link");
        if (link && inputDashboard) {
          inputDashboard.value = link.getAttribute("data-dashboard") || link.innerText.trim();
          closeDashboardsModal();
        }
      });
    }

    if (btnDashboard) {
      btnDashboard.addEventListener("click", function () {
        if (dashboardsModal) {
          dashboardsModal.style.display = "flex";
          dashboardsModal.setAttribute("aria-hidden", "false");
        }
      });
    }

    // Calendar Pickers
    var btnPickStart = $("btnPickStartDate");
    var inputStart = $("rptStartDate");
    var nativeStart = $("nativeStartDate");
    if (btnPickStart && inputStart) {
      btnPickStart.addEventListener("click", function () {
        if (nativeStart && typeof nativeStart.showPicker === "function") {
          try { nativeStart.showPicker(); } catch (_) { nativeStart.focus(); }
        } else {
          var today = new Date().toISOString().slice(0, 10);
          inputStart.value = inputStart.value || today;
        }
      });
      if (nativeStart) {
        nativeStart.addEventListener("change", function () {
          inputStart.value = nativeStart.value;
        });
      }
    }

    var btnPickEnd = $("btnPickEndDate");
    var inputEnd = $("rptEndDate");
    var nativeEnd = $("nativeEndDate");
    if (btnPickEnd && inputEnd) {
      btnPickEnd.addEventListener("click", function () {
        if (nativeEnd && typeof nativeEnd.showPicker === "function") {
          try { nativeEnd.showPicker(); } catch (_) { nativeEnd.focus(); }
        } else {
          var today = new Date().toISOString().slice(0, 10);
          inputEnd.value = inputEnd.value || today;
        }
      });
      if (nativeEnd) {
        nativeEnd.addEventListener("change", function () {
          inputEnd.value = nativeEnd.value;
        });
      }
    }

    // Subscriptions Table: Add User / Group & Remove
    var subList = $("rptSubList");
    var btnAddUser = $("btnAddUser");
    var btnAddUserGroup = $("btnAddUserGroup");

    function addSubRow(name, generateBy, status) {
      if (!subList || !name) return;
      var safeName = name.replace(/[&<>"']/g, function (char) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[char];
      });
      var safeGenerateBy = (generateBy || "Current user").replace(/[&<>"']/g, function (char) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[char];
      });
      var include = (status || "Include") === "Include";
      var tr = document.createElement("tr");
      tr.innerHTML =
        '<td><span class="rpt-sub-name">' + safeName + '</span></td>' +
        '<td>' + safeGenerateBy + '</td>' +
        '<td><span class="rpt-sched-status-tag ' + (include ? "enabled" : "disabled") + '">' + (include ? "Include" : "Exclude") + '</span></td>' +
        '<td><button type="button" class="rpt-sched-btn-link rpt-remove-sub">Remove</button></td>';
      subList.appendChild(tr);
    }

    // Add user opens the full Subscription form (Zabbix-style).
    var subscriptionModal = $("rptSubscriptionModal");
    var subscriptionRecipient = $("rptSubscriptionRecipient");
    var btnSubscriptionAdd = $("btnSubscriptionAdd");
    var btnSubscriptionCancel = $("btnSubscriptionCancel");
    var btnSubscriptionClose = $("btnSubscriptionModalClose");
    var btnRecipientSelect = $("btnSubscriptionRecipientSelect");
    var subscriptionUsersModal = $("rptSubscriptionUsersModal");
    var subscriptionUsersList = $("rptSubscriptionUsersList");
    var btnSubscriptionUsersClose = $("btnSubscriptionUsersClose");
    var btnSubscriptionUsersCancel = $("btnSubscriptionUsersCancel");
    var subscriptionGroupsModal = $("rptSubscriptionGroupsModal");
    var subscriptionGroupsList = $("rptSubscriptionGroupsList");
    var btnSubscriptionGroupsClose = $("btnSubscriptionGroupsClose");
    var btnSubscriptionGroupsCancel = $("btnSubscriptionGroupsCancel");
    var btnSubscriptionGroupSelect = $("btnSubscriptionGroupSelect");
    var subscriptionStatusField = $("rptSubscriptionStatusField");
    var subscriptionMode = "user";

    function closeSubscriptionModal() {
      if (!subscriptionModal) return;
      subscriptionModal.style.display = "none";
      subscriptionModal.setAttribute("aria-hidden", "true");
    }

    function openSubscriptionModal(mode) {
      if (!subscriptionModal) return;
      subscriptionMode = mode || "user";
      var isGroup = subscriptionMode === "group";
      if (subscriptionRecipient) {
        subscriptionRecipient.value = "";
        subscriptionRecipient.placeholder = isGroup ? "type here to search" : "type here to search";
      }
      var currentUser = $("rptGenerateCurrentUser");
      var include = $("rptSubscriptionInclude");
      if (currentUser) currentUser.checked = true;
      if (include) include.checked = true;
      if (subscriptionStatusField) subscriptionStatusField.hidden = isGroup;
      if (subscriptionModal) subscriptionModal.setAttribute("data-mode", subscriptionMode);
      subscriptionModal.style.display = "flex";
      subscriptionModal.setAttribute("aria-hidden", "false");
      if (subscriptionRecipient) subscriptionRecipient.focus();
    }

    function closeSubscriptionUsersModal() {
      if (!subscriptionUsersModal) return;
      subscriptionUsersModal.style.display = "none";
      subscriptionUsersModal.setAttribute("aria-hidden", "true");
    }

    function closeSubscriptionGroupsModal() {
      if (!subscriptionGroupsModal) return;
      subscriptionGroupsModal.style.display = "none";
      subscriptionGroupsModal.setAttribute("aria-hidden", "true");
    }

    if (btnAddUser) btnAddUser.addEventListener("click", function () { openSubscriptionModal("user"); });
    if (btnAddUserGroup) btnAddUserGroup.addEventListener("click", function () { openSubscriptionModal("group"); });
    if (btnSubscriptionCancel) btnSubscriptionCancel.addEventListener("click", closeSubscriptionModal);
    if (btnSubscriptionClose) btnSubscriptionClose.addEventListener("click", closeSubscriptionModal);

    if (btnRecipientSelect) {
      btnRecipientSelect.addEventListener("click", function () {
        if (subscriptionMode === "group" && subscriptionGroupsModal) {
          subscriptionGroupsModal.style.display = "flex";
          subscriptionGroupsModal.setAttribute("aria-hidden", "false");
        } else if (subscriptionUsersModal) {
          subscriptionUsersModal.style.display = "flex";
          subscriptionUsersModal.setAttribute("aria-hidden", "false");
        }
      });
    }

    if (btnSubscriptionGroupsClose) btnSubscriptionGroupsClose.addEventListener("click", closeSubscriptionGroupsModal);
    if (btnSubscriptionGroupsCancel) btnSubscriptionGroupsCancel.addEventListener("click", closeSubscriptionGroupsModal);

    if (subscriptionGroupsList) {
      subscriptionGroupsList.addEventListener("click", function (e) {
        var link = e.target.closest(".rpt-subscription-group-link");
        if (!link || !subscriptionRecipient) return;
        subscriptionRecipient.value = link.innerText.trim();
        closeSubscriptionGroupsModal();
        subscriptionModal.style.display = "flex";
        subscriptionModal.setAttribute("aria-hidden", "false");
        subscriptionRecipient.focus();
      });
    }

    if (btnSubscriptionUsersClose) btnSubscriptionUsersClose.addEventListener("click", closeSubscriptionUsersModal);
    if (btnSubscriptionUsersCancel) btnSubscriptionUsersCancel.addEventListener("click", closeSubscriptionUsersModal);

    if (subscriptionUsersList) {
      subscriptionUsersList.addEventListener("click", function (e) {
        var link = e.target.closest(".rpt-subscription-user-link");
        if (!link || !subscriptionRecipient) return;
        subscriptionRecipient.value = link.getAttribute("data-display") || link.innerText.trim();
        closeSubscriptionUsersModal();
        subscriptionModal.style.display = "flex";
        subscriptionModal.setAttribute("aria-hidden", "false");
        subscriptionRecipient.focus();
      });
    }

    if (btnSubscriptionAdd) {
      btnSubscriptionAdd.addEventListener("click", function () {
        var recipient = subscriptionRecipient ? subscriptionRecipient.value.trim() : "";
        if (!recipient) {
          toast("Recipient is required.", "error");
          if (subscriptionRecipient) subscriptionRecipient.focus();
          return;
        }
        var generateBy = document.querySelector('input[name="subscription_generate_by"]:checked');
        var status = document.querySelector('input[name="subscription_status"]:checked');
        addSubRow(
          recipient,
          generateBy ? generateBy.value : "Current user",
          subscriptionMode === "group" ? "Include" : (status ? status.value : "Include")
        );
        closeSubscriptionModal();
      });
    }

    [subscriptionModal, subscriptionUsersModal, subscriptionGroupsModal].forEach(function (modal) {
      if (!modal) return;
      modal.addEventListener("click", function (e) {
        if (e.target === modal) {
          if (modal === subscriptionModal) closeSubscriptionModal();
          else if (modal === subscriptionUsersModal) closeSubscriptionUsersModal();
          else closeSubscriptionGroupsModal();
        }
      });
    });

    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      if (subscriptionUsersModal && subscriptionUsersModal.style.display !== "none") closeSubscriptionUsersModal();
      else if (subscriptionGroupsModal && subscriptionGroupsModal.style.display !== "none") closeSubscriptionGroupsModal();
      else if (subscriptionModal && subscriptionModal.style.display !== "none") closeSubscriptionModal();
    });

    if (subList) {
      subList.addEventListener("click", function (e) {
        if (e.target && e.target.classList.contains("rpt-remove-sub")) {
          var row = e.target.closest("tr");
          if (row) row.remove();
        }
      });
    }

    // Test Button
    var btnTest = $("btnReportTest");
    if (btnTest) {
      btnTest.addEventListener("click", function () {
        postJson("/api/reports/daily/send", btnTest, "Testing…")
          .then(function () {
            toast("Report test sent successfully.", "success");
          })
          .catch(function () {});
      });
    }

    // Submit / Add Button
    createForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var nameInput = $("rptName");
      var name = nameInput ? nameInput.value.trim() : "";
      if (!name) {
        toast("Report name is required.", "error");
        if (nameInput) nameInput.focus();
        return;
      }

      var owner = inputOwner ? inputOwner.value.trim() : "";
      if (!owner) {
        toast("Owner is required.", "error");
        if (btnOwner) btnOwner.focus();
        return;
      }

      var dashboard = inputDashboard ? inputDashboard.value.trim() : "";
      if (!dashboard) {
        toast("Dashboard is required.", "error");
        if (inputDashboard) inputDashboard.focus();
        return;
      }

      var periodRadio = createForm.querySelector('input[name="period"]:checked');
      var cycleRadio = createForm.querySelector('input[name="cycle"]:checked');
      var btnAdd = $("btnReportAdd");

      var payload = {
        name: name,
        owner: owner,
        dashboard: dashboard,
        period: periodRadio ? periodRadio.value : "Previous day",
        cycle: cycleRadio ? cycleRadio.value : "Daily",
        start_time: (($("rptStartTimeH") && $("rptStartTimeH").value) || "00") + ":" + (($("rptStartTimeM") && $("rptStartTimeM").value) || "00"),
        start_date: inputStart ? inputStart.value.trim() : "",
        end_date: inputEnd ? inputEnd.value.trim() : "",
        subject: ($("rptSubject") && $("rptSubject").value.trim()) || "",
        message: ($("rptMessage") && $("rptMessage").value.trim()) || "",
        description: ($("rptDescription") && $("rptDescription").value.trim()) || "",
        enabled: $("rptEnabled") ? $("rptEnabled").checked : true
      };

      postJson("/api/reports/scheduled/create", btnAdd, "Adding…", payload)
        .then(function () {
          toast("Report '" + name + "' added successfully.", "success");
          window.setTimeout(function () {
            window.location.href = "/reports/scheduled";
          }, 600);
        })
        .catch(function () {});
    });
  }
})();
