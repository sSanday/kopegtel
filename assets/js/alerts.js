/* Alerts: aturan aksi, template pesan, kanal notifikasi (uji kirim nyata). */
(function () {
  "use strict";

  const { escapeHtml, SEVERITY_META } = window.NMSFormat;

  const VIEWS = {
    actions: { label: "Actions", desc: "Aturan aksi otomatis saat masalah terbuka." },
    "message-templates": { label: "Message templates", desc: "Template pesan notifikasi dengan placeholder." },
    "media-types": { label: "Media types", desc: "Kanal pengiriman notifikasi." },
    escalation: { label: "Eskalasi", desc: "Kebijakan kenaikan notifikasi bila problem masih terbuka dan belum diakui." },
  };

  const views = document.querySelectorAll("[data-view]");
  const viewTitle = document.getElementById("view-title");
  const viewDesc = document.getElementById("view-desc");
  const canEdit = () => {
    const role = window.__nmsMe && window.__nmsMe.role;
    return role === "admin" || role === "operator";
  };
  const isAdmin = () => {
    const role = window.__nmsMe && window.__nmsMe.role;
    return role === "admin";
  };

  function currentView() {
    const raw = window.location.hash.replace(/^#/, "");
    return Object.prototype.hasOwnProperty.call(VIEWS, raw) ? raw : "actions";
  }

  let current = currentView();

  function activateView() {
    current = currentView();
    const meta = VIEWS[current];
    views.forEach((view) => {
      view.hidden = view.dataset.view !== current;
    });
    viewTitle.textContent = meta.label;
    viewDesc.textContent = meta.desc;
    document.title = `${meta.label} · Alerts · NMS Kopegtel`;
    if (current === "actions") loadRules();
    else if (current === "message-templates") loadTemplates();
    else if (current === "media-types") loadChannels();
    else if (current === "escalation") loadEscalation();
  }

  window.addEventListener("hashchange", activateView);
  document.getElementById("refresh").addEventListener("click", activateView);

  function whenReady(fn) {
    if (window.__nmsMe) fn();
    else window.addEventListener("nms:me-ready", fn, { once: true });
  }

  function scopeLabel(rule) {
    if (rule.device_id) return `Perangkat: ${rule.device_name || rule.device_id}`;
    if (rule.group_id) return `Grup: ${rule.group_name || rule.group_id}`;
    return "Global";
  }

  function actionButtons(kind, id) {
    return canEdit()
      ? `<button class="ack-toggle" type="button" data-kind="${kind}" data-action="edit" data-id="${id}">Ubah</button> ` +
        `<button class="ack-toggle" type="button" data-kind="${kind}" data-action="delete" data-id="${id}">Hapus</button>`
      : "—";
  }

  /* =====================================================
     Actions (aturan aksi)
     ===================================================== */

  const ruleRows = document.getElementById("rule-rows");
  const ruleCount = document.getElementById("rule-count");
  const ruleForm = document.getElementById("rule-form");
  const ruleFormTitle = document.getElementById("rule-form-title");
  const ruleName = document.getElementById("rule-name");
  const ruleMinSeverity = document.getElementById("rule-min-severity");
  const ruleScope = document.getElementById("rule-scope");
  const ruleGroupWrap = document.getElementById("rule-scope-group-wrap");
  const ruleDeviceWrap = document.getElementById("rule-scope-device-wrap");
  const ruleGroup = document.getElementById("rule-group");
  const ruleDevice = document.getElementById("rule-device");
  const ruleChannel = document.getElementById("rule-channel");
  const ruleTemplate = document.getElementById("rule-template");
  const ruleEnabled = document.getElementById("rule-enabled");
  const ruleEscalation = document.getElementById("rule-escalation");
  const ruleNote = document.getElementById("rule-note");
  const ruleSave = document.getElementById("rule-save");
  const ruleCancel = document.getElementById("rule-cancel");

  let rulesCache = [];
  let groupsCache = [];
  let devicesCache = [];
  let channelsCache = [];
  let templatesCache = [];
  let policiesCache = [];
  let editingRuleId = null;

  async function ensureOptionsLoaded() {
    const jobs = [];
    if (!groupsCache.length) jobs.push(window.apiFetch("/api/groups").then((d) => (groupsCache = d.groups || [])));
    if (!devicesCache.length) jobs.push(window.apiFetch("/api/devices?per_page=100").then((d) => (devicesCache = d.devices || [])));
    if (!channelsCache.length && canEdit()) jobs.push(window.apiFetch("/api/channels").then((d) => (channelsCache = d.channels || [])));
    if (!templatesCache.length) jobs.push(window.apiFetch("/api/templates").then((d) => (templatesCache = d.templates || [])));
    if (!policiesCache.length) jobs.push(window.apiFetch("/api/escalation-policies").then((d) => (policiesCache = d.policies || [])));
    await Promise.allSettled(jobs);
    ruleGroup.innerHTML = '<option value="">Pilih grup…</option>' + groupsCache.map((g) => `<option value="${g.id}">${escapeHtml(g.name)}</option>`).join("");
    ruleDevice.innerHTML = '<option value="">Pilih perangkat…</option>' + devicesCache.map((d) => `<option value="${d.id}">${escapeHtml(d.name)} (${escapeHtml(d.ip)})</option>`).join("");
    ruleChannel.innerHTML = '<option value="">Pilih kanal…</option>' + channelsCache.map((c) => `<option value="${c.id}">${escapeHtml(c.name)} (${escapeHtml(c.type)})</option>`).join("");
    ruleTemplate.innerHTML = '<option value="">Pilih template…</option>' + templatesCache.map((t) => `<option value="${t.id}">${escapeHtml(t.name)}</option>`).join("");
    const esc = document.getElementById("rule-escalation");
    if (esc) {
      esc.innerHTML = '<option value="">— tidak pakai / kirim langsung —</option>'
        + policiesCache.filter((p) => p.enabled).map((p) => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join("");
    }
  }

  function syncScopeVisibility() {
    ruleGroupWrap.hidden = ruleScope.value !== "group";
    ruleDeviceWrap.hidden = ruleScope.value !== "device";
  }

  function renderRules() {
    ruleCount.textContent = `${rulesCache.length} aturan terdaftar`;
    if (rulesCache.length === 0) {
      ruleRows.innerHTML = '<tr class="empty-row"><td colspan="8">Belum ada aturan aksi. Tambahkan aturan pertama agar masalah terbuka memicu notifikasi.</td></tr>';
      return;
    }
    ruleRows.innerHTML = rulesCache
      .map((rule) => {
        const sev = SEVERITY_META[rule.min_severity] || SEVERITY_META.warning;
        return (
          `<tr>` +
          `<td><strong>${escapeHtml(rule.name)}</strong></td>` +
          `<td><span class="${escapeHtml(sev.cls)}">${escapeHtml(sev.label)}</span></td>` +
          `<td>${escapeHtml(scopeLabel(rule))}</td>` +
          `<td>${escapeHtml(rule.channel_name || "-")}</td>` +
          `<td>${escapeHtml(rule.template_name || "-")}</td>` +
          `<td>${escapeHtml(rule.escalation_policy_name || "—")}</td>` +
          `<td>${rule.enabled ? '<span class="status status--ok">Aktif</span>' : '<span class="status status--idle">Nonaktif</span>'}</td>` +
          `<td>${actionButtons("rule", rule.id)}</td>` +
          `</tr>`
        );
      })
      .join("");
  }

  async function loadRules() {
    try {
      const data = await window.apiFetch("/api/rules");
      rulesCache = data.rules || [];
    } catch (error) {
      ruleRows.innerHTML = `<tr class="empty-row"><td colspan="8">Gagal memuat aturan: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    renderRules();
  }

  async function openRuleForm(rule) {
    await ensureOptionsLoaded();
    editingRuleId = rule ? rule.id : null;
    ruleFormTitle.textContent = rule ? "Ubah aturan aksi" : "Tambah aturan aksi";
    ruleName.value = rule ? rule.name : "";
    ruleMinSeverity.value = rule ? rule.min_severity : "warning";
    ruleScope.value = rule ? (rule.device_id ? "device" : rule.group_id ? "group" : "global") : "global";
    ruleGroup.value = rule && rule.group_id ? String(rule.group_id) : "";
    ruleDevice.value = rule && rule.device_id ? String(rule.device_id) : "";
    ruleChannel.value = rule ? String(rule.channel_id) : "";
    ruleTemplate.value = rule ? String(rule.template_id) : "";
    ruleEnabled.checked = rule ? !!rule.enabled : true;
    if (ruleEscalation) ruleEscalation.value = rule && rule.escalation_policy_id ? String(rule.escalation_policy_id) : "";
    ruleNote.hidden = true;
    syncScopeVisibility();
    ruleForm.hidden = false;
    ruleName.focus({ preventScroll: true });
  }

  function closeRuleForm() {
    ruleForm.hidden = true;
    editingRuleId = null;
  }

  ruleScope.addEventListener("change", syncScopeVisibility);
  document.getElementById("rule-add").addEventListener("click", () => openRuleForm(null));
  ruleCancel.addEventListener("click", closeRuleForm);

  ruleForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (ruleSave.disabled) return;
    const name = ruleName.value.trim();
    if (!name) {
      ruleNote.textContent = "Nama aturan wajib diisi.";
      ruleNote.hidden = false;
      ruleName.focus();
      return;
    }
    if (!ruleChannel.value || !ruleTemplate.value) {
      ruleNote.textContent = "Kanal dan template wajib dipilih.";
      ruleNote.hidden = false;
      return;
    }
    ruleSave.disabled = true;
    ruleNote.hidden = true;
    try {
      const payload = {
        name,
        min_severity: ruleMinSeverity.value,
        group_id: ruleScope.value === "group" ? Number(ruleGroup.value) || null : null,
        device_id: ruleScope.value === "device" ? Number(ruleDevice.value) || null : null,
        channel_id: Number(ruleChannel.value),
        template_id: Number(ruleTemplate.value),
        escalation_policy_id: ruleEscalation && ruleEscalation.value ? Number(ruleEscalation.value) : null,
        enabled: ruleEnabled.checked,
      };
      if (editingRuleId) {
        await window.apiFetch(`/api/rules/${editingRuleId}`, { method: "PUT", body: payload });
      } else {
        await window.apiFetch("/api/rules", { method: "POST", body: payload });
      }
      closeRuleForm();
      await loadRules();
    } catch (error) {
      ruleNote.textContent = error.message || "Gagal menyimpan aturan.";
      ruleNote.hidden = false;
    } finally {
      ruleSave.disabled = false;
    }
  });

  /* =====================================================
     Message templates
     ===================================================== */

  const templateRows = document.getElementById("template-rows");
  const templateCount = document.getElementById("template-count");
  const templateForm = document.getElementById("template-form");
  const templateFormTitle = document.getElementById("template-form-title");
  const tplName = document.getElementById("tpl-name");
  const tplBody = document.getElementById("tpl-body");
  const tplNote = document.getElementById("tpl-note");
  const tplSave = document.getElementById("tpl-save");
  const tplCancel = document.getElementById("tpl-cancel");

  let templatesState = [];
  let editingTemplateId = null;

  function renderTemplates() {
    templateCount.textContent = `${templatesState.length} template terdaftar`;
    if (templatesState.length === 0) {
      templateRows.innerHTML = '<tr class="empty-row"><td colspan="3">Belum ada template. Tambahkan template pesan pertama.</td></tr>';
      return;
    }
    templateRows.innerHTML = templatesState
      .map((tpl) => {
        const preview = tpl.body.length > 90 ? `${tpl.body.slice(0, 90)}…` : tpl.body;
        return (
          `<tr>` +
          `<td><strong>${escapeHtml(tpl.name)}</strong></td>` +
          `<td><code class="cell-code">${escapeHtml(preview)}</code></td>` +
          `<td>${actionButtons("template", tpl.id)}</td>` +
          `</tr>`
        );
      })
      .join("");
  }

  async function loadTemplates() {
    try {
      const data = await window.apiFetch("/api/templates");
      templatesState = data.templates || [];
    } catch (error) {
      templateRows.innerHTML = `<tr class="empty-row"><td colspan="3">Gagal memuat template: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    renderTemplates();
  }

  function openTemplateForm(tpl) {
    editingTemplateId = tpl ? tpl.id : null;
    templateFormTitle.textContent = tpl ? "Ubah template" : "Tambah template";
    tplName.value = tpl ? tpl.name : "";
    tplBody.value = tpl ? tpl.body : "";
    tplNote.hidden = true;
    templateForm.hidden = false;
    tplName.focus({ preventScroll: true });
  }

  document.getElementById("template-add").addEventListener("click", () => openTemplateForm(null));
  tplCancel.addEventListener("click", () => {
    templateForm.hidden = true;
    editingTemplateId = null;
  });

  templateForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (tplSave.disabled) return;
    const name = tplName.value.trim();
    const body = tplBody.value.trim();
    if (!name || !body) {
      tplNote.textContent = "Nama dan isi template wajib diisi.";
      tplNote.hidden = false;
      return;
    }
    tplSave.disabled = true;
    tplNote.hidden = true;
    try {
      if (editingTemplateId) {
        await window.apiFetch(`/api/templates/${editingTemplateId}`, { method: "PUT", body: { name, body } });
      } else {
        await window.apiFetch("/api/templates", { method: "POST", body: { name, body } });
      }
      templateForm.hidden = true;
      editingTemplateId = null;
      await loadTemplates();
    } catch (error) {
      tplNote.textContent = error.message || "Gagal menyimpan template.";
      tplNote.hidden = false;
    } finally {
      tplSave.disabled = false;
    }
  });

  /* =====================================================
     Media types (kanal)
     ===================================================== */

  const channelRows = document.getElementById("channel-rows");
  const channelCount = document.getElementById("channel-count");
  const channelForm = document.getElementById("channel-form");
  const channelFormTitle = document.getElementById("channel-form-title");
  const chType = document.getElementById("ch-type");
  const chName = document.getElementById("ch-name");
  const chEnabled = document.getElementById("ch-enabled");
  const chNote = document.getElementById("ch-note");
  const chSave = document.getElementById("ch-save");
  const chCancel = document.getElementById("ch-cancel");
  const chTelegram = document.getElementById("ch-telegram-fields");
  const chSmtp = document.getElementById("ch-smtp-fields");
  const chWebhook = document.getElementById("ch-webhook-fields");
  const chTestResult = document.getElementById("channel-test-result");

  let channelsState = [];
  let editingChannelId = null;

  function syncChannelTypeFields() {
    chTelegram.hidden = chType.value !== "telegram";
    chSmtp.hidden = chType.value !== "smtp";
    if (chWebhook) chWebhook.hidden = chType.value !== "webhook";
  }

  function channelTarget(channel) {
    const cfg = channel.config || {};
    if (channel.type === "telegram") return cfg.has_chat_id ? "Chat tersimpan" : "-";
    if (channel.type === "webhook") return cfg.has_url ? "URL tersimpan" : "-";
    return cfg.to_addr || "-";
  }

  function channelRequiresAdmin(channel) {
    const cfg = channel.config || {};
    if (channel.type === "webhook") return !!cfg.allow_private || !!cfg.allow_http;
    if (channel.type === "smtp") return !!cfg.allow_private;
    return false;
  }

  function canTestChannel(channel) {
    return canEdit() && (isAdmin() || !channelRequiresAdmin(channel));
  }

  function renderChannels() {
    channelCount.textContent = `${channelsState.length} kanal terdaftar`;
    if (channelsState.length === 0) {
      channelRows.innerHTML = '<tr class="empty-row"><td colspan="6">Belum ada kanal. Tambahkan kanal Telegram, SMTP, atau Webhook pertama.</td></tr>';
      return;
    }
    channelRows.innerHTML = channelsState
      .map((channel) => (
        `<tr>` +
        `<td><strong>${escapeHtml(channel.name)}</strong></td>` +
        `<td>${escapeHtml(channel.type === "telegram" ? "Telegram" : channel.type === "webhook" ? "Webhook" : "Email (SMTP)")}</td>` +
        `<td class="mono">${escapeHtml(channelTarget(channel))}</td>` +
        `<td>${channel.enabled ? '<span class="status status--ok">Aktif</span>' : '<span class="status status--idle">Nonaktif</span>'}</td>` +
        `<td>${canTestChannel(channel) ? `<button class="ack-toggle" type="button" data-action="test" data-id="${channel.id}">Uji kirim</button>` : "-"}</td>` +
        `<td>${actionButtons("channel", channel.id)}</td>` +
        `</tr>`
      ))
      .join("");
  }

  async function loadChannels() {
    if (!canEdit()) {
      channelsState = [];
      channelCount.textContent = "Kanal hanya untuk admin/operator.";
      channelRows.innerHTML = '<tr class="empty-row"><td colspan="6">Hanya admin dan operator yang dapat melihat kanal notifikasi.</td></tr>';
      return;
    }
    try {
      const data = await window.apiFetch("/api/channels");
      channelsState = data.channels || [];
    } catch (error) {
      channelRows.innerHTML = `<tr class="empty-row"><td colspan="6">Gagal memuat kanal: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    renderChannels();
  }

  function openChannelForm(channel) {
    editingChannelId = channel ? channel.id : null;
    channelFormTitle.textContent = channel ? "Ubah kanal" : "Tambah kanal";
    chType.value = channel ? channel.type : "telegram";
    chName.value = channel ? channel.name : "";
    chEnabled.checked = channel ? !!channel.enabled : true;
    chType.disabled = !!channel; // tipe tidak diubah setelah dibuat
    const cfg = channel && channel.config ? channel.config : {};
    const editing = !!channel;
    const keepPlaceholder = (present, fallback) =>
      (editing && present ? "Tersimpan — kosongkan untuk mempertahankan" : fallback);

    const chBotToken = document.getElementById("ch-bot-token");
    chBotToken.value = "";
    chBotToken.placeholder = keepPlaceholder(cfg.has_bot_token, "123456:ABC-DEF…");
    const chChatId = document.getElementById("ch-chat-id");
    chChatId.value = "";
    chChatId.placeholder = keepPlaceholder(cfg.has_chat_id, "-1001234567890");

    const chSmtpHost = document.getElementById("ch-smtp-host");
    chSmtpHost.value = "";
    chSmtpHost.placeholder = keepPlaceholder(cfg.has_host, "smtp.office365.com");
    document.getElementById("ch-smtp-port").value = cfg.port ? String(cfg.port) : "587";
    document.getElementById("ch-smtp-tls").checked = cfg.use_tls !== false;
    const chSmtpUsername = document.getElementById("ch-smtp-username");
    chSmtpUsername.value = "";
    chSmtpUsername.placeholder = keepPlaceholder(cfg.has_username, "opsional");
    const chSmtpPassword = document.getElementById("ch-smtp-password");
    chSmtpPassword.value = "";
    chSmtpPassword.placeholder = keepPlaceholder(cfg.has_password, "opsional");
    document.getElementById("ch-smtp-from").value = cfg.from_addr || "";
    document.getElementById("ch-smtp-to").value = cfg.to_addr || "";

    const chWebhookUrl = document.getElementById("ch-webhook-url");
    chWebhookUrl.value = "";
    chWebhookUrl.placeholder = keepPlaceholder(cfg.has_url, "https://hooks.slack.com/services/T...");
    document.getElementById("ch-webhook-preset").value = cfg.preset || "generic";
    const chWebhookSecret = document.getElementById("ch-webhook-secret");
    chWebhookSecret.value = "";
    chWebhookSecret.placeholder = editing
      ? "kosongkan agar mempertahankan yang tersimpan"
      : "wajib diisi";
    const chWebhookHeaders = document.getElementById("ch-webhook-headers");
    chWebhookHeaders.value = "";
    chWebhookHeaders.placeholder = editing && cfg.has_headers
      ? "Header tersimpan — kosongkan untuk mempertahankan"
      : '{"Authorization": "Bearer ..."}';
    const chSmtpPrivate = document.getElementById("ch-smtp-private");
    chSmtpPrivate.checked = !!cfg.allow_private;
    chSmtpPrivate.disabled = !isAdmin();
    chSmtpPrivate.title = isAdmin() ? "" : "Hanya admin yang dapat mengubah izin target privat.";
    const chWebhookHttp = document.getElementById("ch-webhook-http");
    chWebhookHttp.checked = !!cfg.allow_http;
    chWebhookHttp.disabled = !isAdmin();
    chWebhookHttp.title = isAdmin() ? "" : "Hanya admin yang dapat mengizinkan http.";
    const chWebhookPrivate = document.getElementById("ch-webhook-private");
    chWebhookPrivate.checked = !!cfg.allow_private;
    chWebhookPrivate.disabled = !isAdmin();
    chWebhookPrivate.title = isAdmin() ? "" : "Hanya admin yang dapat mengizinkan target privat.";
    chNote.hidden = true;
    chTestResult.hidden = true;
    syncChannelTypeFields();
    channelForm.hidden = false;
    chName.focus({ preventScroll: true });
  }

  chType.addEventListener("change", syncChannelTypeFields);
  document.getElementById("channel-add").addEventListener("click", () => openChannelForm(null));
  chCancel.addEventListener("click", () => {
    channelForm.hidden = true;
    editingChannelId = null;
  });

  channelForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (chSave.disabled) return;
    const name = chName.value.trim();
    if (!name) {
      chNote.textContent = "Nama kanal wajib diisi.";
      chNote.hidden = false;
      chName.focus();
      return;
    }
    chSave.disabled = true;
    chNote.hidden = true;
    try {
      const type = chType.value;
      const common = { name, type, enabled: chEnabled.checked };
      let config;
      if (type === "telegram") {
        config = {
          bot_token: document.getElementById("ch-bot-token").value.trim(),
          chat_id: document.getElementById("ch-chat-id").value.trim(),
        };
      } else if (type === "webhook") {
        config = {
          url: document.getElementById("ch-webhook-url").value.trim(),
          preset: document.getElementById("ch-webhook-preset").value,
          secret: document.getElementById("ch-webhook-secret").value.trim(),
        };
        if (isAdmin()) {
          config.allow_http = document.getElementById("ch-webhook-http").checked;
          config.allow_private = document.getElementById("ch-webhook-private").checked;
        }
        const headersRaw = document.getElementById("ch-webhook-headers").value.trim();
        if (headersRaw) {
          try {
            const parsed = JSON.parse(headersRaw);
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
            config.headers = parsed;
          } catch (parseError) {
            chNote.textContent = "Header kustom harus berupa objek JSON, mis. {\"Authorization\": \"Bearer ...\"}";
            chNote.hidden = false;
            chSave.disabled = false;
            return;
          }
        }
      } else {
        config = {
          host: document.getElementById("ch-smtp-host").value.trim(),
          port: Number(document.getElementById("ch-smtp-port").value || 587),
          use_tls: document.getElementById("ch-smtp-tls").checked,
          username: document.getElementById("ch-smtp-username").value.trim(),
          password: document.getElementById("ch-smtp-password").value,
          from_addr: document.getElementById("ch-smtp-from").value.trim(),
          to_addr: document.getElementById("ch-smtp-to").value.trim(),
        };
        if (isAdmin()) {
          config.allow_private = document.getElementById("ch-smtp-private").checked;
        }
      }
      if (editingChannelId) {
        await window.apiFetch(`/api/channels/${editingChannelId}`, { method: "PUT", body: { ...common, config } });
      } else {
        await window.apiFetch("/api/channels", { method: "POST", body: { ...common, config } });
      }
      channelForm.hidden = true;
      editingChannelId = null;
      await loadChannels();
    } catch (error) {
      chNote.textContent = error.message || "Gagal menyimpan kanal.";
      chNote.hidden = false;
    } finally {
      chSave.disabled = false;
    }
  });

  /* =====================================================
     Delegasi klik (ubah/hapus/uji kirim)
     ===================================================== */

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-kind]");
    const testButton = event.target.closest("button[data-action='test']");

    if (testButton) {
      const id = Number(testButton.dataset.id);
      testButton.disabled = true;
      testButton.textContent = "Mengirim…";
      try {
        const res = await window.apiFetch(`/api/channels/${id}/test`, { method: "POST" });
        chTestResult.textContent = res.detail || (res.ok ? "Uji kirim berhasil." : "Uji kirim gagal.");
        chTestResult.classList.toggle("is-ok", !!res.ok);
        chTestResult.classList.toggle("is-error", !res.ok);
        chTestResult.hidden = false;
      } catch (error) {
        chTestResult.textContent = error.message || "Uji kirim gagal.";
        chTestResult.classList.add("is-error");
        chTestResult.classList.remove("is-ok");
        chTestResult.hidden = false;
      } finally {
        testButton.disabled = false;
        testButton.textContent = "Uji kirim";
      }
      return;
    }

    if (!button) return;
    const kind = button.dataset.kind;
    const id = Number(button.dataset.id);

    if (button.dataset.action === "edit") {
      if (kind === "rule") await openRuleForm(rulesCache.find((r) => r.id === id));
      else if (kind === "template") openTemplateForm(templatesState.find((t) => t.id === id));
      else if (kind === "channel") openChannelForm(channelsState.find((c) => c.id === id));
      return;
    }
    if (button.dataset.action === "delete") {
      const names = { rule: "aturan aksi", template: "template", channel: "kanal" };
      if (!(await window.NMSUi.confirm(`Hapus ${names[kind] || "item"} ini?`, {
        title: "Hapus item",
        confirmLabel: "Hapus",
        danger: true,
      }))) return;
      const paths = { rule: `/api/rules/${id}`, template: `/api/templates/${id}`, channel: `/api/channels/${id}` };
      try {
        await window.apiFetch(paths[kind], { method: "DELETE" });
        if (kind === "rule") await loadRules();
        else if (kind === "template") await loadTemplates();
        else if (kind === "channel") await loadChannels();
      } catch (error) {
        await window.NMSUi.alert(error.message || "Gagal menghapus.");
      }
    }
  });

  /* =====================================================
     Eskalasi (E2)
     ===================================================== */

  const escalationRows = document.getElementById("escalation-rows");
  const escalationCount = document.getElementById("escalation-count");
  const escalationAdd = document.getElementById("escalation-add");
  const escalationForm = document.getElementById("escalation-form");
  const escalationFormTitle = document.getElementById("escalation-form-title");
  const escName = document.getElementById("esc-name");
  const escDescription = document.getElementById("esc-description");
  const escRepeat = document.getElementById("esc-repeat");
  const escBusinessEnabled = document.getElementById("esc-business-enabled");
  const escBusinessStart = document.getElementById("esc-business-start");
  const escBusinessEnd = document.getElementById("esc-business-end");
  const escBusinessDays = document.getElementById("esc-business-days");
  const escEnabled = document.getElementById("esc-enabled");
  const escStepRows = document.getElementById("esc-step-rows");
  const escStepAdd = document.getElementById("esc-step-add");
  const escNote = document.getElementById("esc-note");
  const escSave = document.getElementById("esc-save");
  const escCancel = document.getElementById("esc-cancel");

  let policiesState = [];
  let escalationTargets = null;
  let editingPolicyId = null;

  async function loadEscalationTargets() {
    if (escalationTargets) return escalationTargets;
    try {
      const data = await window.apiFetch("/api/escalation-targets");
      escalationTargets = {
        users: data.users || [],
        channels: data.channels || [],
      };
    } catch (error) {
      escalationTargets = { users: [], channels: [] };
    }
    return escalationTargets;
  }

  function targetOptions(type, selectedId) {
    const targets = escalationTargets || { users: [], channels: [] };
    const items = type === "user" ? targets.users : targets.channels;
    const options = items.map((item) => {
      const label = type === "user"
        ? `${item.username} (${item.role})`
        : `${item.name} (${item.type})${item.enabled ? "" : " [nonaktif]"}`;
      const selected = String(item.id) === String(selectedId) ? " selected" : "";
      return `<option value="${item.id}"${selected}>${escapeHtml(label)}</option>`;
    });
    return '<option value="">Pilih.</option>' + options.join("");
  }

  function renderEscalation() {
    escalationCount.textContent = `${policiesState.length} kebijakan terdaftar`;
    if (policiesState.length === 0) {
      escalationRows.innerHTML = '<tr class="empty-row"><td colspan="6">Belum ada kebijakan. Tambahkan kebijakan eskalasi pertama.</td></tr>';
      return;
    }
    escalationRows.innerHTML = policiesState.map((policy) => `
      <tr>
        <td><strong>${escapeHtml(policy.name)}</strong>${policy.description ? `<br><span class="panel__sub">${escapeHtml(policy.description)}</span>` : ""}</td>
        <td>${(policy.steps || []).length} langkah</td>
        <td>${policy.business_hours_enabled
          ? `${escapeHtml(policy.business_start)}–${escapeHtml(policy.business_end)} WIB (hari ${escapeHtml(policy.business_days)})`
          : "Selalu"}</td>
        <td>${Number(policy.repeat_minutes || 0) ? `Tiap ${Number(policy.repeat_minutes)} mnt` : "Tidak"}</td>
        <td>${policy.enabled ? '<span class="status status--ok">Aktif</span>' : '<span class="status status--idle">Nonaktif</span>'}</td>
        <td>${canEdit()
          ? `<button class="ack-toggle" type="button" data-esc-action="edit" data-id="${policy.id}">Ubah</button> <button class="ack-toggle" type="button" data-esc-action="delete" data-id="${policy.id}">Hapus</button>`
          : "-"}</td>
      </tr>`).join("");
    escalationRows.querySelectorAll("[data-esc-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        const policy = policiesState.find((item) => String(item.id) === button.dataset.id);
        if (!policy) return;
        if (button.dataset.escAction === "edit") {
          await openPolicyForm(policy);
        } else if (button.dataset.escAction === "delete") {
          if (!(await window.NMSUi.confirm(`Hapus kebijakan "${policy.name}"? Aturan aksi yang memakainya akan kembali mengirim langsung.`, {
            title: "Hapus kebijakan",
            confirmLabel: "Hapus",
            danger: true,
          }))) return;
          try {
            await window.apiFetch(`/api/escalation-policies/${policy.id}`, { method: "DELETE" });
            policiesCache = [];
            await loadEscalation();
          } catch (error) {
            await window.NMSUi.alert(error.message || "Gagal menghapus.");
          }
        }
      });
    });
  }

  async function loadEscalation() {
    try {
      const data = await window.apiFetch("/api/escalation-policies");
      policiesState = data.policies || [];
      policiesCache = policiesState;
    } catch (error) {
      escalationRows.innerHTML = `<tr class="empty-row"><td colspan="6">Gagal memuat kebijakan: ${escapeHtml(error.message)}</td></tr>`;
      return;
    }
    renderEscalation();
  }

  function addStepRow(step) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td class="esc-step-order"></td>
      <td><input type="number" class="esc-step-delay" min="0" max="10080" step="1" inputmode="numeric" value="${step ? Number(step.delay_minutes) : 5}" aria-label="Jeda (menit)" /></td>
      <td><select class="esc-step-type" aria-label="Tipe target"><option value="channel">Kanal</option><option value="user">Pengguna</option></select></td>
      <td><select class="esc-step-target" aria-label="Target"></select></td>
      <td><label class="check"><input type="checkbox" class="esc-step-business"${step && step.business_hours_only ? " checked" : ""} /> Jam kerja</label></td>
      <td><button class="ack-toggle" type="button">Hapus</button></td>`;
    const typeSelect = row.querySelector(".esc-step-type");
    const targetSelect = row.querySelector(".esc-step-target");
    typeSelect.value = step ? step.target_type : "channel";
    targetSelect.innerHTML = targetOptions(typeSelect.value, step ? step.target_id : "");
    typeSelect.addEventListener("change", () => {
      targetSelect.innerHTML = targetOptions(typeSelect.value, "");
    });
    row.querySelector("button").addEventListener("click", () => {
      row.remove();
      renumberSteps();
    });
    escStepRows.appendChild(row);
    renumberSteps();
  }

  function renumberSteps() {
    escStepRows.querySelectorAll("tr").forEach((row, index) => {
      row.querySelector(".esc-step-order").textContent = String(index + 1);
    });
  }

  async function openPolicyForm(policy) {
    await loadEscalationTargets();
    editingPolicyId = policy ? policy.id : null;
    escalationFormTitle.textContent = policy ? "Ubah kebijakan" : "Tambah kebijakan";
    escName.value = policy ? policy.name : "";
    escDescription.value = policy ? policy.description || "" : "";
    escRepeat.value = policy ? Number(policy.repeat_minutes || 0) : 0;
    escBusinessEnabled.checked = policy ? !!policy.business_hours_enabled : false;
    escBusinessStart.value = policy ? policy.business_start || "08:00" : "08:00";
    escBusinessEnd.value = policy ? policy.business_end || "17:00" : "17:00";
    escBusinessDays.value = policy ? policy.business_days || "1,2,3,4,5" : "1,2,3,4,5";
    escEnabled.checked = policy ? !!policy.enabled : true;
    escStepRows.innerHTML = "";
    const steps = policy && policy.steps && policy.steps.length
      ? policy.steps
      : [{ delay_minutes: 0, target_type: "channel", target_id: "", business_hours_only: false }];
    steps.forEach(addStepRow);
    escNote.hidden = true;
    escalationForm.hidden = false;
    escName.focus({ preventScroll: true });
  }

  if (escStepAdd) escStepAdd.addEventListener("click", () => addStepRow(null));
  if (escalationAdd) escalationAdd.addEventListener("click", () => openPolicyForm(null));
  if (escCancel) escCancel.addEventListener("click", () => { escalationForm.hidden = true; editingPolicyId = null; });

  if (escalationForm) {
    escalationForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const steps = [...escStepRows.querySelectorAll("tr")].map((row) => ({
        delay_minutes: Number(row.querySelector(".esc-step-delay").value || 0),
        target_type: row.querySelector(".esc-step-type").value,
        target_id: Number(row.querySelector(".esc-step-target").value || 0),
        business_hours_only: row.querySelector(".esc-step-business").checked,
      }));
      if (steps.length === 0) {
        escNote.textContent = "Tambahkan minimal satu langkah.";
        escNote.hidden = false;
        return;
      }
      escSave.disabled = true;
      escNote.hidden = true;
      const payload = {
        name: escName.value.trim(),
        description: escDescription.value.trim(),
        repeat_minutes: Number(escRepeat.value || 0),
        business_hours_enabled: escBusinessEnabled.checked,
        business_start: escBusinessStart.value || "08:00",
        business_end: escBusinessEnd.value || "17:00",
        business_days: escBusinessDays.value.trim() || "1,2,3,4,5",
        enabled: escEnabled.checked,
        steps,
      };
      try {
        const url = editingPolicyId ? `/api/escalation-policies/${editingPolicyId}` : "/api/escalation-policies";
        await window.apiFetch(url, { method: editingPolicyId ? "PUT" : "POST", body: payload });
        escalationForm.hidden = true;
        editingPolicyId = null;
        policiesCache = [];
        await loadEscalation();
      } catch (error) {
        escNote.textContent = error.message || "Gagal menyimpan kebijakan.";
        escNote.hidden = false;
      } finally {
        escSave.disabled = false;
      }
    });
  }

  /* =====================================================
     Boot
     ===================================================== */

  whenReady(() => {
    if (!canEdit()) {
      ["rule-add", "template-add", "channel-add", "escalation-add", "esc-step-add", "esc-save"].forEach((id) => {
        const el = document.getElementById(id);
        if (el) el.hidden = true;
      });
    }
    activateView();
    window.NMSStore.startAutoRefresh(activateView, 15000);
  });
})();
