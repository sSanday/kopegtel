/* Komponen UI bersama: toast dan dialog modal.
 *
 * API:
 *   NMSUi.toast(pesan, { kind: "info" | "error", duration })
 *   NMSUi.alert(pesan, opsi)        -> Promise<void>
 *   NMSUi.confirm(pesan, opsi)      -> Promise<boolean>
 *   NMSUi.prompt(pesan, opsi)       -> Promise<string | null>
 *   NMSUi.dialog(opsi)              -> Promise<string | "confirmed" | null>
 *
 * Dialog memakai satu elemen <dialog> yang dibuat saat pertama dibutuhkan.
 * Esc, klik backdrop, dan tombol tutup dianggap batal; fokus dikembalikan ke
 * elemen pemicu. Tidak ada nilai dinamis yang dimasukkan lewat innerHTML.
 */
(function () {
  "use strict";

  const TOAST_DURATION = 4200;
  const TOAST_EXIT_MS = 250;

  function toast(message, options) {
    const opts = typeof options === "string" ? { kind: options } : options || {};
    // Satu toast pada satu waktu agar tidak saling menumpuk.
    document.querySelectorAll(".toast").forEach((el) => el.remove());

    const el = document.createElement("div");
    el.className = opts.kind === "error" ? "toast toast--error" : "toast";
    el.setAttribute("role", "status");

    const dot = document.createElement("span");
    dot.className = "toast__dot";
    dot.setAttribute("aria-hidden", "true");
    el.append(dot, document.createTextNode(String(message)));

    document.body.appendChild(el);
    requestAnimationFrame(() => el.classList.add("is-visible"));
    window.setTimeout(() => {
      el.classList.remove("is-visible");
      window.setTimeout(() => el.remove(), TOAST_EXIT_MS);
    }, opts.duration || TOAST_DURATION);
  }

  /* Markup statis — tidak ada nilai dinamis di dalamnya. */
  const DIALOG_MARKUP = `
    <form class="admin-form" id="nms-ui-form" novalidate>
      <div class="modal__head">
        <div>
          <h2 class="panel__title" id="nms-ui-title">Konfirmasi</h2>
          <p class="panel__sub" id="nms-ui-desc"></p>
        </div>
        <button class="modal__close" type="button" data-nms-ui-close aria-label="Tutup dialog">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18" /><path d="m6 6 12 12" /></svg>
        </button>
      </div>
      <label class="admin-field" id="nms-ui-field" hidden>
        <span id="nms-ui-label">Nilai</span>
        <input id="nms-ui-input" type="text" />
      </label>
      <p class="form-note" id="nms-ui-note" role="alert" hidden></p>
      <div class="admin-actions">
        <button class="btn btn--primary" id="nms-ui-confirm" type="submit">Lanjutkan</button>
        <button class="btn btn--ghost" id="nms-ui-cancel" type="button" data-nms-ui-close>Batal</button>
      </div>
    </form>`;

  let dialogEl = null;
  let parts = null;
  let pending = null;
  let active = null;
  let restoreFocus = null;

  function clearNote() {
    if (!parts) return;
    parts.note.hidden = true;
    parts.note.textContent = "";
    parts.input.removeAttribute("aria-invalid");
  }

  function showNote(message) {
    parts.note.textContent = message;
    parts.note.hidden = false;
    parts.input.setAttribute("aria-invalid", "true");
    parts.input.focus({ preventScroll: true });
  }

  function settle(value) {
    if (!dialogEl || !dialogEl.open) return;
    const resolve = pending;
    pending = null;
    dialogEl.close();
    if (resolve) resolve(value);
  }

  function onSubmit(event) {
    event.preventDefault();
    if (!dialogEl.open || !active) return;
    if (!active.withInput) {
      settle("confirmed");
      return;
    }
    const value = parts.input.value;
    if (active.required && !value.trim()) {
      showNote("Kolom ini wajib diisi.");
      return;
    }
    if (active.minLength && value.length < active.minLength) {
      showNote(`Minimal ${active.minLength} karakter.`);
      return;
    }
    if (typeof active.validate === "function") {
      const message = active.validate(value);
      if (message) {
        showNote(message);
        return;
      }
    }
    settle(value);
  }

  function ensureDialog() {
    if (dialogEl) return;
    dialogEl = document.createElement("dialog");
    dialogEl.className = "device-dialog dialog--prompt";
    dialogEl.setAttribute("aria-labelledby", "nms-ui-title");
    dialogEl.setAttribute("aria-describedby", "nms-ui-desc");
    dialogEl.innerHTML = DIALOG_MARKUP;
    document.body.appendChild(dialogEl);

    parts = {
      form: document.getElementById("nms-ui-form"),
      title: document.getElementById("nms-ui-title"),
      desc: document.getElementById("nms-ui-desc"),
      field: document.getElementById("nms-ui-field"),
      label: document.getElementById("nms-ui-label"),
      input: document.getElementById("nms-ui-input"),
      note: document.getElementById("nms-ui-note"),
      confirm: document.getElementById("nms-ui-confirm"),
      cancel: document.getElementById("nms-ui-cancel"),
    };

    parts.form.addEventListener("submit", onSubmit);
    parts.input.addEventListener("input", clearNote);
    dialogEl.addEventListener("click", (event) => {
      if (event.target === dialogEl || event.target.closest("[data-nms-ui-close]")) {
        settle(null);
      }
    });
    dialogEl.addEventListener("cancel", (event) => {
      event.preventDefault();
      settle(null);
    });
    dialogEl.addEventListener("close", () => {
      // Bila dialog sudah dibuka lagi sebelum event close sempat diproses,
      // jangan sentuh promise/fokus milik dialog yang baru.
      if (dialogEl.open) return;

      // Pengaman bila dialog ditutup di luar settle().
      const resolve = pending;
      pending = null;
      if (resolve) resolve(null);

      const target = restoreFocus;
      restoreFocus = null;
      if (target && target.isConnected) target.focus({ preventScroll: true });
    });
  }

  function dialog(options) {
    const opts = options || {};
    ensureDialog();
    if (typeof dialogEl.showModal !== "function") return Promise.resolve(null);
    if (dialogEl.open) settle(null);

    const withInput = opts.withInput !== false;
    const required = opts.required !== false;
    active = {
      withInput,
      required,
      minLength: Number(opts.minLength) || 0,
      validate: typeof opts.validate === "function" ? opts.validate : null,
    };

    parts.title.textContent = opts.title || "Konfirmasi";
    parts.desc.textContent = opts.description || "";
    parts.desc.hidden = !opts.description;
    parts.field.hidden = !withInput;
    parts.label.textContent = opts.label || "Nilai";

    parts.input.value = opts.value || "";
    parts.input.type = opts.type || "text";
    if (opts.autocomplete) parts.input.setAttribute("autocomplete", opts.autocomplete);
    else parts.input.removeAttribute("autocomplete");
    parts.input.placeholder = opts.placeholder || "";
    if (active.minLength) parts.input.setAttribute("minlength", String(active.minLength));
    else parts.input.removeAttribute("minlength");
    if (opts.maxLength) parts.input.setAttribute("maxlength", String(opts.maxLength));
    else parts.input.removeAttribute("maxlength");
    if (opts.inputMode) parts.input.setAttribute("inputmode", opts.inputMode);
    else parts.input.removeAttribute("inputmode");
    clearNote();

    parts.confirm.textContent = opts.confirmLabel || "Lanjutkan";
    parts.confirm.classList.toggle("btn--danger", Boolean(opts.danger));
    parts.confirm.classList.toggle("btn--primary", !opts.danger);
    parts.cancel.textContent = opts.cancelLabel || "Batal";
    parts.cancel.hidden = opts.showCancel === false;

    restoreFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;

    return new Promise((resolve) => {
      pending = resolve;
      try {
        dialogEl.showModal();
      } catch {
        pending = null;
        restoreFocus = null;
        resolve(null);
        return;
      }
      if (withInput) {
        parts.input.focus({ preventScroll: true });
        parts.input.select();
      } else {
        parts.confirm.focus({ preventScroll: true });
      }
    });
  }

  function alert(message, options) {
    const opts = options || {};
    return dialog({
      ...opts,
      title: opts.title || "Pemberitahuan",
      description: message,
      withInput: false,
      confirmLabel: opts.confirmLabel || "Tutup",
      showCancel: false,
    }).then(() => undefined);
  }

  function confirm(message, options) {
    const opts = options || {};
    return dialog({
      ...opts,
      title: opts.title || "Konfirmasi",
      description: message,
      withInput: false,
      confirmLabel: opts.confirmLabel || "Lanjutkan",
      showCancel: true,
    }).then((value) => value === "confirmed");
  }

  function prompt(message, options) {
    const opts = options || {};
    return dialog({
      ...opts,
      title: opts.title || "Konfirmasi",
      description: message,
      withInput: true,
      label: opts.label || "Nilai",
      confirmLabel: opts.confirmLabel || "Lanjutkan",
    });
  }

  window.NMSUi = { toast, alert, confirm, prompt, dialog };
})();
