(function () {
  "use strict";

  const form = document.getElementById("ganti-form");
  const current = document.getElementById("current-password");
  const next = document.getElementById("new-password");
  const confirm = document.getElementById("confirm-password");
  const submit = document.getElementById("submit");
  const note = document.getElementById("form-note");

  function setError(input, errorId, message) {
    const error = document.getElementById(errorId);
    if (!error) return;
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

  async function checkSession() {
    try {
      const me = await window.apiFetch("/api/me");
      window.rememberCsrf(me.csrf_token);
      if (!me.must_change_password) {
        window.location.replace("dashboard.html");
      }
    } catch {
      /* apiFetch sudah mengalihkan bila 401 */
    }
  }

  checkSession();

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setError(current, "current-error", current.value ? "" : "Kata sandi saat ini wajib diisi.");
    setError(next, "new-error", next.value.length >= 10 ? "" : "Kata sandi baru minimal 10 karakter.");
    setError(confirm, "confirm-error", next.value === confirm.value ? "" : "Konfirmasi kata sandi tidak sama.");

    if (document.querySelector("[aria-invalid='true']")) return;

    submit.classList.add("is-loading");
    submit.disabled = true;
    note.textContent = "";
    note.classList.remove("is-ok", "is-error");

    try {
      await window.apiFetch("/api/users/me/password", {
        method: "POST",
        body: { current_password: current.value, new_password: next.value },
      });
      note.textContent = "Kata sandi tersimpan. Mengalihkan ke dasbor…";
      note.classList.add("is-ok");
      window.setTimeout(() => window.location.replace("dashboard.html"), 700);
    } catch (error) {
      note.textContent = error.message || "Gagal mengubah kata sandi.";
      note.classList.add("is-error");
      submit.classList.remove("is-loading");
      submit.disabled = false;
    }
  });
})();
