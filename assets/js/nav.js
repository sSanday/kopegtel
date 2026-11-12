/* Sidebar bersama: dirender dari satu konfigurasi sesuai peran pengguna.
 * Tidak ada tautan mati — semua item menu menuju file & view yang ada.
 */
(function () {
  "use strict";

  const ICONS = {
    dashboard:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="9" rx="1.5" /><rect x="14" y="3" width="7" height="5" rx="1.5" /><rect x="14" y="12" width="7" height="9" rx="1.5" /><rect x="3" y="16" width="7" height="5" rx="1.5" /></svg>',
    monitoring:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="12" height="7" rx="2" /><path d="M3 15h6" /><path d="M3 19h4" /><circle cx="16.5" cy="16.5" r="3.4" /><path d="m19 19 2.4 2.4" /></svg>',
    services:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="2" width="6" height="5" rx="1.2" /><path d="M12 7v3.5" /><path d="M4 10.5h16" /><path d="M4 10.5v4" /><path d="M12 10.5v4" /><path d="M20 10.5v4" /><rect x="1.5" y="14.5" width="5" height="5" rx="1.2" /><rect x="9.5" y="14.5" width="5" height="5" rx="1.2" /><rect x="17.5" y="14.5" width="5" height="5" rx="1.2" /></svg>',
    fiber:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h4" /><path d="M7 12c1.8-4.4 3.6-4.4 5.4 0s3.6 4.4 5.4 0 3.4-4.1 4.6-1.6" /><path d="M3 18h4" /><path d="M15 6h6" /></svg>',
    mikrotik:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="12" width="19" height="7" rx="2" /><path d="M6 12V8" /><path d="M18 12V8" /><path d="M6 6.5A3 3 0 0 1 9 4" /><path d="M18 6.5A3 3 0 0 0 15 4" /><circle cx="7" cy="15.5" r="1" /><path d="M11 15.5h6" /></svg>',
    inventory:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2.8 21 8v8l-9 5.2L3 16V8l9-5.2Z" /><path d="M3 8l9 5.2L21 8" /><path d="M12 13.2v8" /></svg>',
    events:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 4h16v12H7l-3 3V4Z" /><path d="M8 8h8" /><path d="M8 11h5" /></svg>',
    traffic:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h4" /><path d="M7 12c1.8-4.4 3.6-4.4 5.4 0s3.6 4.4 5.4 0 3.4-4.1 4.6-1.6" /><path d="M3 18h4" /><path d="M15 6h6" /></svg>',
    reports:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" /><path d="M14 2v6h6" /><path d="M8 18v-3" /><path d="M12 18v-6" /><path d="M16 18v-4" /></svg>',
    dataCollection:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v10" /><path d="m7.5 8.5 4.5 4.5 4.5-4.5" /><path d="M4 16v2.5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V16" /></svg>',
    alerts:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.7 21a2 2 0 0 1-3.4 0" /></svg>',
    users:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M22 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" /></svg>',
    administration:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3.2" /><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4 0v-.09a1.7 1.7 0 0 0-1.11-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3a2 2 0 1 1 0-4h.09A1.7 1.7 0 0 0 4.65 8.86a1.7 1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.7 1.7 0 0 0 9 4.6h.09A1.7 1.7 0 0 0 10.12 3.04V3a2 2 0 1 1 4 0v.09a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.7 1.7 0 0 0 19.4 9v.09a1.7 1.7 0 0 0 1.56 1.03H21a2 2 0 1 1 0 4h-.09A1.7 1.7 0 0 0 19.4 15Z" /></svg>',
    profile:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="3.6" /><path d="M5 20a7 7 0 0 1 14 0" /></svg>',
    logout:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><path d="m16 17 5-5-5-5" /><path d="M21 12H9" /></svg>',
  };

  const LOCKUP =
    '<a class="lockup" href="dashboard.html" aria-label="NMS Kopegtel">' +
    '<svg class="lockup__mark" viewBox="0 0 48 48" aria-hidden="true" focusable="false">' +
    '<defs><linearGradient id="markGradient" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#2fe0c4" /><stop offset="1" stop-color="#5b8dff" /></linearGradient></defs>' +
    '<rect x="1" y="1" width="46" height="46" rx="13" fill="rgba(255,255,255,.04)" stroke="rgba(255,255,255,.12)" />' +
    '<g stroke="url(#markGradient)" stroke-width="1.8" stroke-linecap="round"><path d="M24 24 15.5 15" /><path d="M24 24 33 18" /><path d="M24 24 26 34" /></g>' +
    '<circle cx="24" cy="24" r="4.2" fill="url(#markGradient)" />' +
    '<circle cx="15.5" cy="15" r="2.6" fill="#2fe0c4" /><circle cx="33" cy="18" r="2.6" fill="#5b8dff" /><circle cx="26" cy="34" r="2.6" fill="#2fe0c4" />' +
    "</svg>" +
    '<span class="lockup__text"><span class="lockup__name">NMS KOPEGTEL</span><span class="lockup__sub">Network Management System</span></span>' +
    "</a>";

  const CHEVRON =
    '<svg class="nav__chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>';

  // Konfigurasi menu bersama. `roles` = siapa yang melihat item.
  const MENU = [
    { label: "Dasbor", href: "dashboard.html", icon: "dashboard", roles: ["admin", "operator", "viewer"] },
    {
      label: "Monitoring",
      icon: "monitoring",
      id: "monitoring",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Problems", href: "monitoring.html#problems", badge: "problems" },
        { label: "Hosts", href: "monitoring.html#hosts" },
        { label: "Latest data", href: "monitoring.html#latest-data" },
        { label: "Maps", href: "monitoring.html#maps" },
      ],
    },
    {
      label: "Services",
      icon: "services",
      id: "services",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Services", href: "services.html#services" },
        { label: "SLA", href: "services.html#sla" },
        { label: "SLA report", href: "services.html#sla-report" },
      ],
    },
    {
      label: "Fiber",
      icon: "fiber",
      id: "fiber",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Ringkasan", href: "fiber.html#ringkasan" },
        { label: "Port optik", href: "fiber.html#ports" },
        { label: "Link & redaman", href: "fiber.html#links" },
        { label: "Pengukuran manual", href: "fiber.html#measurements" },
        { label: "Kalkulator", href: "fiber.html#calculator" },
        { label: "Pengaturan fiber", href: "fiber.html#settings", roles: ["admin"] },
      ],
    },
    {
      label: "MikroTik",
      icon: "mikrotik",
      id: "mikrotik",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Armada", href: "mikrotik.html#armada" },
      ],
    },
    {
      label: "Traffic",
      icon: "traffic",
      id: "traffic",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Ringkasan", href: "traffic.html#summary" },
        { label: "Top Talkers", href: "traffic.html#talkers", roles: ["admin", "operator"] },
        { label: "Aplikasi / Port", href: "traffic.html#apps", roles: ["admin", "operator"] },
        { label: "Percakapan", href: "traffic.html#conversations", roles: ["admin", "operator"] },
        { label: "Status eksporter", href: "traffic.html#status", roles: ["admin", "operator"] },
      ],
    },
    {
      label: "Kejadian",
      icon: "events",
      id: "events",
      roles: ["admin", "operator"],
      children: [
        { label: "Syslog", href: "events.html#syslog" },
        { label: "Trap", href: "events.html#traps" },
        { label: "Aturan", href: "events.html#rules" },
        { label: "Status penerima", href: "events.html#status" },
      ],
    },
    {
      label: "Inventory",
      icon: "inventory",
      id: "inventory",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Overview", href: "inventory.html#overview" },
        { label: "Hosts", href: "inventory.html#hosts" },
      ],
    },
    {
      label: "Reports",
      icon: "reports",
      id: "reports",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "System information", href: "reports.html#system-information" },
        { label: "Availability report", href: "reports.html#availability-report" },
        { label: "Top problems", href: "reports.html#top-problems" },
        { label: "Laporan redaman", href: "reports.html#fiber-attenuation" },
        { label: "Armada MikroTik", href: "reports.html#mikrotik-fleet" },
        { label: "Audit log", href: "reports.html#audit-log", roles: ["admin"] },
        { label: "Action log", href: "reports.html#action-log" },
      ],
    },
    {
      label: "Data collection",
      icon: "dataCollection",
      id: "data-collection",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Host groups", href: "data-collection.html#host-groups" },
        { label: "Profil pemantauan", href: "data-collection.html#profiles" },
        { label: "Hosts", href: "data-collection.html#hosts" },
        { label: "Maintenance", href: "data-collection.html#maintenance" },
        { label: "Discovery", href: "data-collection.html#discovery" },
      ],
    },
    {
      label: "Alerts",
      icon: "alerts",
      id: "alerts",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Actions", href: "alerts.html#actions" },
        { label: "Message templates", href: "alerts.html#message-templates" },
        { label: "Media types", href: "alerts.html#media-types" },
        { label: "Eskalasi", href: "alerts.html#escalation", roles: ["admin", "operator"] },
      ],
    },
    {
      label: "Users",
      icon: "users",
      id: "users",
      roles: ["admin"],
      children: [
        { label: "Users", href: "users.html#users" },
        { label: "User roles", href: "users.html#user-roles" },
      ],
    },
    {
      label: "Administration",
      icon: "administration",
      id: "administration",
      roles: ["admin"],
      children: [
        { label: "Pengaturan umum", href: "administration.html#general" },
        { label: "Retensi data", href: "administration.html#housekeeping" },
        { label: "Penerima kejadian", href: "administration.html#ingest" },
        { label: "Token API & metrik", href: "administration.html#tokens" },
        { label: "MikroTik", href: "administration.html#mikrotik" },
        { label: "Informasi instalasi", href: "administration.html#installation" },
      ],
    },
  ];

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function childHtml(child, role, index, pageName, hash) {
    const roles = child.roles || ["admin", "operator", "viewer"];
    if (!roles.includes(role)) return "";
    const [childPage, childHash] = child.href.split("#");
    const isActive = childPage === pageName && ("#" + (childHash || "")) === hash;
    const badge = child.badge === "problems" ? '<span class="nav__badge nav__badge--danger" data-problems-badge hidden>0</span>' : "";
    return (
      `<a class="nav__sublink${isActive ? " is-active" : ""}" href="${escapeHtml(child.href)}"${isActive ? ' aria-current="page"' : ""}>` +
      `<span>${escapeHtml(child.label)}</span>${badge}</a>`
    );
  }

  function renderNav(container, me) {
    const role = me && me.role ? me.role : "viewer";
    const pageName = (window.location.pathname.split("/").pop() || "dashboard.html").toLowerCase();
    const hash = window.location.hash || "";

    const parts = [LOCKUP, '<nav class="nav" aria-label="Navigasi utama"><p class="nav__label">Menu</p>'];

    for (const item of MENU) {
      if (item.href) {
        if (!item.roles.includes(role)) continue;
        const isActive = item.href === pageName;
        parts.push(
          `<a class="nav__link${isActive ? " is-active" : ""}" href="${escapeHtml(item.href)}"${isActive ? ' aria-current="page"' : ""}>` +
            ICONS[item.icon] + `<span class="nav__toggle-label">${escapeHtml(item.label)}</span></a>`
        );
        continue;
      }
      if (!item.roles.includes(role)) continue;
      const children = (item.children || []).map((child, i) => childHtml(child, role, i, pageName, hash)).join("");
      if (!children) continue;
      const groupCurrent = (item.children || []).some((child) => child.href.split("#")[0] === pageName);
      const badge = item.id === "monitoring" ? '<span class="nav__badge" data-problems-badge-group hidden>0</span>' : "";
      // Hanya grup halaman aktif yang mulai terbuka; lainnya tertutup (klik chevron untuk membuka).
      const isOpen = groupCurrent;
      parts.push(
        `<div class="nav__group${groupCurrent ? " is-current" : ""}${isOpen ? " is-open" : ""}" data-nav-group>` +
          `<button class="nav__link nav__toggle" type="button" data-nav-toggle aria-expanded="${isOpen}" aria-controls="nav-${escapeHtml(item.id)}">` +
            ICONS[item.icon] +
            `<span class="nav__toggle-label">${escapeHtml(item.label)}</span>${badge}${CHEVRON}` +
          "</button>" +
          `<div class="nav__sub" id="nav-${escapeHtml(item.id)}"${isOpen ? "" : " hidden"}>${children}</div>` +
        "</div>"
      );
    }

    parts.push("</nav>");
    parts.push(
      '<div class="sidebar__foot"><div class="user">' +
        `<span class="user__avatar" id="user-avatar" aria-hidden="true">${escapeHtml((me.username || "?").charAt(0).toUpperCase())}</span>` +
        '<span class="user__meta"><span class="user__name" id="user-name"></span><span class="user__role" id="user-role"></span></span>' +
      "</div>" +
      `<a class="nav__link" href="profile.html" style="margin-top: .35rem;">${ICONS.profile}<span class="nav__toggle-label">Profil</span></a>` +
      `<button class="logout" id="logout" type="button">${ICONS.logout}<span>Keluar</span></button>` +
      "</div>"
    );

    container.innerHTML = parts.join("");

    document.getElementById("user-name").textContent = me.username || "";
    document.getElementById("user-role").textContent = me.role_label || me.role || "";

    // Toggle grup (buka/tutup).
    container.querySelectorAll("[data-nav-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        const expanded = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", String(!expanded));
        const group = button.closest("[data-nav-group]");
        if (group) group.classList.toggle("is-open", !expanded);
        const sub = group ? group.querySelector(".nav__sub") : null;
        if (sub) sub.hidden = expanded;
      });
    });

    // Logout.
    document.getElementById("logout").addEventListener("click", async () => {
      try {
        await window.apiFetch("/api/logout", { method: "POST" });
      } catch {
        /* tetap keluar di sisi klien */
      }
      window.location.replace("index.html");
    });
  }

  /** Perbarui lencana masalah dari /api/summary. */
  async function refreshProblemsBadge() {
    try {
      const me = window.__nmsMe;
      if (!me) return;
      const summary = await window.apiFetch("/api/summary");
      const count = summary.problems_active || 0;
      document.querySelectorAll("[data-problems-badge]").forEach((el) => {
        el.hidden = count === 0;
        el.textContent = String(count);
        el.setAttribute("aria-label", `${count} masalah aktif`);
      });
      document.querySelectorAll("[data-problems-badge-group]").forEach((el) => {
        el.hidden = count === 0;
        el.textContent = String(count);
        el.setAttribute("aria-label", `${count} masalah aktif`);
      });
    } catch {
      /* lencana bersifat informatif; gagal memuat tidak menghentikan halaman */
    }
  }

  async function init() {
    const container = document.querySelector(".sidebar");
    if (!container) return;
    try {
      const me = await window.apiFetch("/api/me");
      window.__nmsMe = me;
      window.rememberCsrf(me.csrf_token);
      renderNav(container, me);
      refreshProblemsBadge();
      window.addEventListener("hashchange", () => renderNav(container, window.__nmsMe || me));
      window.addEventListener("nms:problems-changed", refreshProblemsBadge);
      window.dispatchEvent(new CustomEvent("nms:me-ready"));
    } catch {
      /* apiFetch sudah menangani redirect 401 */
    }
  }

  init();
})();
