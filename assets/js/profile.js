/* Profil: ganti kata sandi + MFA TOTP. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const { escapeHtml } = window.NMSFormat;

  const pwForm = $("pw-form");
  const pwNote = $("pw-note");
  const graceBanner = $("grace-banner");

  const statusEl = $("mfa-status");
  const offBlock = $("mfa-off-block");
  const setupBlock = $("mfa-setup-block");
  const recoveryBlock = $("mfa-recovery-block");
  const onBlock = $("mfa-on-block");

  async function loadStatus() {
    let status;
    try {
      status = await window.apiFetch("/api/mfa/status");
    } catch (error) {
      statusEl.textContent = error.message;
      return;
    }
    graceBanner.hidden = !(status.required_by_policy && !status.enabled && status.grace_open);
    offBlock.hidden = true;
    setupBlock.hidden = true;
    recoveryBlock.hidden = true;
    onBlock.hidden = true;
    if (status.enabled) {
      statusEl.textContent = `MFA aktif. Sisa kode pemulihan: ${status.recovery_codes_left}.`;
      $("mfa-recovery-left").textContent = status.recovery_codes_left;
      onBlock.hidden = false;
      return;
    }
    if (status.pending_setup) {
      // Secret pending tersimpan server; pengguna bisa lanjutkan verifikasi,
      // tetapi QR hanya tampil saat setup baru. Tampilkan form verifikasi saja.
      statusEl.textContent = "Pendaftaran MFA menunggu verifikasi kode.";
      setupBlock.hidden = false;
      $("mfa-qr").innerHTML = "";
      $("mfa-secret").textContent = "(secret hanya tampil saat setup baru)";
      $("mfa-uri").textContent = "";
      return;
    }
    statusEl.textContent = status.required_by_policy
      ? "MFA belum aktif — wajib untuk peran Anda (masa tenggang berjalan)."
      : "MFA belum aktif (opsional).";
    offBlock.hidden = false;
  }

  $("mfa-refresh").addEventListener("click", loadStatus);

  // ---------------- Password ----------------
  pwForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const current = $("pw-current").value;
    const next = $("pw-new").value;
    const confirm = $("pw-confirm").value;
    pwNote.hidden = true;
    if (next.length < 10) {
      pwNote.textContent = "Kata sandi minimal 10 karakter.";
      pwNote.hidden = false;
      return;
    }
    if (next !== confirm) {
      pwNote.textContent = "Ulangi kata sandi tidak cocok.";
      pwNote.hidden = false;
      return;
    }
    try {
      await window.apiFetch("/api/users/me/password", {
        method: "POST",
        body: { current_password: current, new_password: next },
      });
      pwNote.textContent = "Kata sandi berhasil diganti.";
      pwNote.classList.remove("is-error");
      pwNote.classList.add("is-ok");
      pwNote.hidden = false;
      pwForm.reset();
    } catch (error) {
      pwNote.textContent = error.message;
      pwNote.classList.add("is-error");
      pwNote.classList.remove("is-ok");
      pwNote.hidden = false;
    }
  });

  // ---------------- MFA setup ----------------
  $("mfa-start").addEventListener("click", async () => {
    try {
      const data = await window.apiFetch("/api/mfa/setup", { method: "POST" });
      $("mfa-qr").innerHTML = data.qr_svg
        ? `<img alt="QR MFA" width="180" height="180" src="data:image/svg+xml;base64,${btoa(unescape(encodeURIComponent(data.qr_svg)))}" />`
        : "";
      $("mfa-secret").textContent = data.secret || "";
      $("mfa-uri").textContent = data.otpauth_uri || "";
      $("mfa-code").value = "";
      $("mfa-setup-note").hidden = true;
      offBlock.hidden = true;
      setupBlock.hidden = false;
      statusEl.textContent = "Pendaftaran MFA menunggu verifikasi.";
    } catch (error) {
      statusEl.textContent = error.message;
    }
  });

  $("mfa-cancel").addEventListener("click", () => {
    setupBlock.hidden = true;
    loadStatus();
  });

  $("mfa-verify-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const note = $("mfa-setup-note");
    note.hidden = true;
    try {
      const data = await window.apiFetch("/api/mfa/verify", {
        method: "POST",
        body: { code: $("mfa-code").value.trim() },
      });
      setupBlock.hidden = true;
      const list = $("mfa-recovery-list");
      list.innerHTML = (data.recovery_codes || []).map((code) => `<li>${escapeHtml(code)}</li>`).join("");
      recoveryBlock.hidden = false;
      statusEl.textContent = "MFA aktif. Sisa kode pemulihan: 10.";
    } catch (error) {
      note.textContent = error.message;
      note.classList.add("is-error");
      note.hidden = false;
    }
  });

  $("mfa-recovery-done").addEventListener("click", () => {
    recoveryBlock.hidden = true;
    loadStatus();
  });

  $("mfa-disable-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const note = $("mfa-disable-note");
    note.hidden = true;
    try {
      await window.apiFetch("/api/mfa/disable", {
        method: "POST",
        body: {
          password: $("mfa-disable-pw").value,
          code: $("mfa-disable-code").value.trim(),
        },
      });
      $("mfa-disable-pw").value = "";
      $("mfa-disable-code").value = "";
      note.textContent = "MFA berhasil dinonaktifkan.";
      note.classList.remove("is-error");
      note.classList.add("is-ok");
      note.hidden = false;
      await loadStatus();
    } catch (error) {
      note.textContent = error.message;
      note.classList.add("is-error");
      note.classList.remove("is-ok");
      note.hidden = false;
    }
  });

  function whenReady(fn) {
    if (window.__nmsMe) fn();
    else window.addEventListener("nms:me-ready", fn, { once: true });
  }

  whenReady(() => {
    loadStatus();
  });
})();
