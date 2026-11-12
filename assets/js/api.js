/* Wrapper fetch terpusat untuk API JSON NMS Kopegtel.
 * - Mengirim X-CSRF-Token untuk semua POST/PUT/DELETE.
 * - 401 → kembali ke halaman login; 403 must_change_password → ganti-password.html.
 * - Kegagalan jaringan → memicu banner "Server tidak dapat dihubungi".
 */
(function () {
  "use strict";

  let csrfToken = null;

  function rememberCsrf(token) {
    if (token) csrfToken = token;
  }

  function currentCsrf() {
    return csrfToken;
  }

  function notifyBackendDown() {
    window.dispatchEvent(new CustomEvent("nms:backend-down"));
  }

  function notifyBackendUp() {
    window.dispatchEvent(new CustomEvent("nms:backend-up"));
  }

  async function rawFetch(path, options = {}, token) {
    const headers = Object.assign(
      { Accept: "application/json" },
      options.body !== undefined ? { "Content-Type": "application/json" } : {},
      options.headers || {}
    );
    if (token && /^(POST|PUT|DELETE|PATCH)$/i.test(options.method || "GET")) {
      headers["X-CSRF-Token"] = token;
    }
    return fetch(path, {
      credentials: "same-origin",
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      method: options.method || "GET",
    });
  }

  async function apiFetch(path, options = {}) {
    let response;
    try {
      response = await rawFetch(path, options, currentCsrf());
    } catch (networkError) {
      notifyBackendDown();
      throw new Error("Server tidak dapat dihubungi.");
    }

    // CSRF kedaluwarsa/hilang → ambil token segar sekali, lalu coba ulang sekali.
    if (response.status === 403) {
      let code = "";
      try {
        code = (await response.clone().json()).code || "";
      } catch {
        code = "";
      }
      if (code === "csrf") {
        try {
          const me = await rawFetch("/api/me", {}, null);
          const meBody = await me.json();
          if (meBody && meBody.csrf_token) {
            rememberCsrf(meBody.csrf_token);
            response = await rawFetch(path, options, currentCsrf());
          }
        } catch {
          /* abaikan — biarkan 403 asli diteruskan */
        }
      }
    }

    if (response.status >= 500) {
      // Server hidup tapi rusaknya parah — anggap backend tidak dapat dihubungi.
      notifyBackendDown();
    }

    if (response.ok) {
      notifyBackendUp();
      if (response.status === 204) return null;
      const contentType = response.headers.get("Content-Type") || "";
      if (contentType.includes("application/json")) return response.json();
      return response.text();
    }

    if (response.status === 401) {
      if (!/^\/api\/login$/.test(path)) {
        const here = window.location.pathname.split("/").pop() || "index.html";
        if (here !== "index.html" && here !== "ganti-password.html") {
          window.location.replace("index.html");
        }
      }
      let message = "Belum login.";
      try {
        message = (await response.json()).error || message;
      } catch {
        /* biarkan pesan bawaan */
      }
      const error = new Error(message);
      error.status = 401;
      throw error;
    }

    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    if (response.status === 403 && body && body.code === "must_change_password") {
      const here = window.location.pathname.split("/").pop() || "";
      if (here !== "ganti-password.html") {
        window.location.replace("ganti-password.html");
      }
    }
    const message = body && body.error ? body.error : `Permintaan gagal (${response.status}).`;
    const error = new Error(message);
    error.status = response.status;
    error.code = body && body.code;
    throw error;
  }

  window.apiFetch = apiFetch;
  window.rememberCsrf = rememberCsrf;
})();
