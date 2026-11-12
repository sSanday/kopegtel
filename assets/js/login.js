(function () {
  "use strict";

  const form = document.getElementById("login-form");
  const username = document.getElementById("username");
  const password = document.getElementById("password");
  const toggle = document.getElementById("toggle-password");
  const submit = document.getElementById("submit");
  const note = document.getElementById("form-note");
  const remember = document.getElementById("remember");

  const fields = [
    { input: username, error: document.getElementById("username-error"), message: "Nama pengguna wajib diisi." },
    { input: password, error: document.getElementById("password-error"), message: "Kata sandi wajib diisi." }
  ];

  // Muat nama organisasi & versi (jika tersedia) — tanpa placeholder palsu.
  fetch("/api/public-config", { credentials: "same-origin" })
    .then((res) => (res.ok ? res.json() : null))
    .then((cfg) => {
      if (!cfg) return;
      const org = document.getElementById("org-name");
      if (org && cfg.org_name) org.textContent = cfg.org_name;
      const build = document.getElementById("build-badge");
      if (build && cfg.version) build.textContent = `Internal · v${cfg.version}`;
    })
    .catch(() => {
      /* biarkan teks bawaan */
    });

  function setFieldError(field, message) {
    const { input, error } = field;
    if (message) {
      input.setAttribute("aria-invalid", "true");
      error.textContent = message;
      error.hidden = false;
    } else {
      input.removeAttribute("aria-invalid");
      error.textContent = "";
      error.hidden = true;
    }
  }

  function validate() {
    let firstInvalid = null;
    fields.forEach((field) => {
      const value = field.input.value.trim();
      const message = value ? "" : field.message;
      setFieldError(field, message);
      if (message && !firstInvalid) firstInvalid = field.input;
    });
    return firstInvalid;
  }

  function clearNote() {
    note.textContent = "";
    note.classList.remove("is-ok", "is-error");
  }

  fields.forEach((field) => {
    field.input.addEventListener("input", () => {
      if (field.input.getAttribute("aria-invalid") === "true" && field.input.value.trim()) {
        setFieldError(field, "");
      }
      clearNote();
    });
  });

  toggle.addEventListener("click", () => {
    const show = password.type === "password";
    password.type = show ? "text" : "password";
    toggle.setAttribute("aria-pressed", String(show));
    toggle.setAttribute("aria-label", show ? "Sembunyikan kata sandi" : "Tampilkan kata sandi");
    password.focus({ preventScroll: true });
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearNote();

    const firstInvalid = validate();
    if (firstInvalid) {
      note.textContent = "Periksa kembali data yang ditandai merah.";
      note.classList.add("is-error");
      firstInvalid.focus();
      return;
    }

    submit.classList.add("is-loading");
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");

    try {
      const data = await window.apiFetch("/api/login", {
        method: "POST",
        body: {
          username: username.value.trim(),
          password: password.value,
          remember: Boolean(remember && remember.checked)
        }
      });

      window.rememberCsrf(data.csrf_token);

      if (data.mfa_required) {
        showMfaStep();
        submit.classList.remove("is-loading");
        submit.disabled = false;
        submit.removeAttribute("aria-busy");
        return;
      }

      note.textContent = "Login berhasil. Mengalihkan…";
      note.classList.add("is-ok");

      const target = data.must_change_password ? "ganti-password.html" : "dashboard.html";
      window.setTimeout(() => {
        window.location.href = target;
      }, 500);
    } catch (error) {
      note.textContent = error.message || "Nama pengguna atau kata sandi salah.";
      note.classList.add("is-error");
      password.select();
      submit.classList.remove("is-loading");
      submit.disabled = false;
      submit.removeAttribute("aria-busy");
    }
  });

  // ---------------- MFA (langkah kedua) ----------------
  const mfaForm = document.getElementById("mfa-form");
  const mfaCode = document.getElementById("mfa-code");
  const mfaNote = document.getElementById("mfa-note");
  const mfaSubmit = document.getElementById("mfa-submit");
  const mfaBack = document.getElementById("mfa-back");

  function showMfaStep() {
    form.hidden = true;
    mfaForm.hidden = false;
    mfaNote.textContent = "Masukkan kode 6 digit dari aplikasi authenticator (atau kode pemulihan).";
    mfaNote.classList.remove("is-error", "is-ok");
    mfaCode.value = "";
    mfaCode.focus({ preventScroll: true });
  }

  if (mfaBack) {
    mfaBack.addEventListener("click", () => {
      mfaForm.hidden = true;
      form.hidden = false;
      password.select();
    });
  }

  if (mfaForm) {
    mfaForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      mfaNote.textContent = "";
      mfaNote.classList.remove("is-error", "is-ok");
      const code = mfaCode.value.trim();
      if (!code) {
        mfaNote.textContent = "Kode wajib diisi.";
        mfaNote.classList.add("is-error");
        return;
      }
      mfaSubmit.classList.add("is-loading");
      mfaSubmit.disabled = true;
      mfaSubmit.setAttribute("aria-busy", "true");
      try {
        const data = await window.apiFetch("/api/mfa/verify-login", {
          method: "POST",
          body: { code }
        });
        window.rememberCsrf(data.csrf_token);
        mfaNote.textContent = "Verifikasi berhasil. Mengalihkan.";
        mfaNote.classList.add("is-ok");
        mfaSubmit.classList.remove("is-loading");
        mfaSubmit.disabled = false;
        mfaSubmit.removeAttribute("aria-busy");
        const target = data.must_change_password ? "ganti-password.html" : "dashboard.html";
        window.setTimeout(() => {
          window.location.href = target;
        }, 500);
      } catch (error) {
        mfaNote.textContent = error.message || "Kode MFA salah.";
        mfaNote.classList.add("is-error");
        mfaSubmit.classList.remove("is-loading");
        mfaSubmit.disabled = false;
        mfaSubmit.removeAttribute("aria-busy");
      }
    });
  }
})();
