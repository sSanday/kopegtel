/* Sidebar bersama: dirender dari satu konfigurasi sesuai peran pengguna.
 * Tidak ada tautan mati. Semua item menu menuju file & view yang ada.
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
    '<span class="lockup__text"><span class="lockup__name">NMS KOPEGTEL</span><span class="lockup__sub">Sistem manajemen jaringan</span></span>' +
    "</a>";

  const CHEVRON =
    '<svg class="nav__chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>';

  // Konfigurasi menu bersama. `roles` = siapa yang melihat item.
  const MENU = [
    { label: "Dasbor", href: "dashboard.html", icon: "dashboard", roles: ["admin", "operator", "viewer"] },
    {
      label: "Pemantauan",
      icon: "monitoring",
      id: "monitoring",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Masalah", href: "monitoring.html#problems", badge: "problems" },
        { label: "Daftar Host", href: "monitoring.html#hosts" },
        { label: "Data terbaru", href: "monitoring.html#latest-data" },
        { label: "Peta", href: "monitoring.html#maps" },
      ],
    },
    {
      label: "Layanan",
      icon: "services",
      id: "services",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Layanan", href: "services.html#services" },
        { label: "SLA", href: "services.html#sla" },
        { label: "Laporan SLA", href: "services.html#sla-report" },
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
      label: "Lalu lintas",
      icon: "traffic",
      id: "traffic",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Ringkasan", href: "traffic.html#summary" },
        { label: "Trafik teratas", href: "traffic.html#talkers", roles: ["admin", "operator"] },
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
      label: "Inventaris",
      icon: "inventory",
      id: "inventory",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Ringkasan", href: "inventory.html#overview" },
        { label: "Daftar Host", href: "inventory.html#hosts" },
      ],
    },
    {
      label: "Laporan",
      icon: "reports",
      id: "reports",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Informasi sistem", href: "reports.html#system-information" },
        { label: "Laporan ketersediaan", href: "reports.html#availability-report" },
        { label: "Masalah teratas", href: "reports.html#top-problems" },
        { label: "Laporan redaman", href: "reports.html#fiber-attenuation" },
        { label: "Armada MikroTik", href: "reports.html#mikrotik-fleet" },
        { label: "Log audit", href: "reports.html#audit-log", roles: ["admin"] },
        { label: "Log aksi", href: "reports.html#action-log" },
      ],
    },
    {
      label: "Pengumpulan Data",
      icon: "dataCollection",
      id: "data-collection",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Grup Host", href: "data-collection.html#host-groups" },
        { label: "Profil pemantauan", href: "data-collection.html#profiles" },
        { label: "Daftar Host", href: "data-collection.html#hosts" },
        { label: "Pemeliharaan", href: "data-collection.html#maintenance" },
        { label: "Pemindaian", href: "data-collection.html#discovery" },
      ],
    },
    {
      label: "Notifikasi",
      icon: "alerts",
      id: "alerts",
      roles: ["admin", "operator", "viewer"],
      children: [
        { label: "Aturan aksi", href: "alerts.html#actions" },
        { label: "Template pesan", href: "alerts.html#message-templates" },
        { label: "Kanal notifikasi", href: "alerts.html#media-types", roles: ["admin", "operator"] },
        { label: "Eskalasi", href: "alerts.html#escalation", roles: ["admin", "operator"] },
      ],
    },
    {
      label: "Pengguna",
      icon: "users",
      id: "users",
      roles: ["admin"],
      children: [
        { label: "Pengguna", href: "users.html#users" },
        { label: "Peran pengguna", href: "users.html#user-roles" },
      ],
    },
    {
      label: "Administrasi",
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

  // Satu sumber escape: window.NMSFormat (format.js). Fallback minimal
  // bila format.js gagal dimuat (mis. halaman jarang).
  const escapeHtml = window.NMSFormat
    ? window.NMSFormat.escapeHtml
    : (value) => String(value == null ? "" : value)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

  function childHtml(child, role, index, pageName, hash) {    const roles = child.roles || ["admin", "operator", "viewer"];
    if (!roles.includes(role)) return "";
    const [childPage, childHash] = child.href.split("#");
    // Tanpa hash, tampilan pertama adalah tampilan bawaan halaman.
    const isActive = childPage === pageName && (hash ? "#" + (childHash || "") === hash : index === 0);
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
          `<button class="nav__link nav__toggle" type="button" data-nav-toggle aria-expanded="${isOpen}"${groupCurrent ? ' aria-current="true"' : ""} aria-controls="nav-${escapeHtml(item.id)}">` +
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
      `<a class="nav__link nav__link--profile" href="profile.html">${ICONS.profile}<span class="nav__toggle-label">Profil</span></a>` +
      `<button class="logout" id="logout" type="button" aria-label="Keluar">${ICONS.logout}<span>Keluar</span></button>` +
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

  /** Tab halaman generik: sinkron dengan location.hash dan peran pengguna. */
  function setupPageTabs(me) {
    const bar = document.querySelector(".page-tabs");
    if (!bar) return;

    const role = me && me.role ? me.role : "viewer";
    bar.querySelectorAll("[data-tab]").forEach((button) => {
      const roles = (button.dataset.role || "").split(/\s+/).filter(Boolean);
      if (roles.length && !roles.includes(role)) button.remove();
    });

    const tabs = Array.from(bar.querySelectorAll("[data-tab]"));
    if (!tabs.length) return;

    // Petunjuk gulir: tepi kanan/kiri meredup hanya saat masih ada tab tersembunyi.
    const updateTabFade = () => {
      const max = bar.scrollWidth - bar.clientWidth;
      bar.dataset.fadeStart = String(bar.scrollLeft > 1);
      bar.dataset.fadeEnd = String(max > 1 && bar.scrollLeft < max - 1);
    };
    bar.addEventListener("scroll", updateTabFade, { passive: true });
    window.addEventListener("resize", updateTabFade);
    if ("ResizeObserver" in window) new ResizeObserver(updateTabFade).observe(bar);
    requestAnimationFrame(updateTabFade);
    updateTabFade();

    const views = new Map();
    document.querySelectorAll("[data-view]").forEach((view) => {
      const key = view.dataset.view;
      if (!key) return;
      if (!view.id) view.id = `page-view-${key}`;
      view.setAttribute("role", "tabpanel");
      views.set(key, view);
    });

    tabs.forEach((button) => {
      const key = button.dataset.tab;
      if (!button.id) button.id = `page-tab-${key}`;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", "false");
      button.tabIndex = -1;
      const view = views.get(key);
      if (view) {
        view.setAttribute("aria-labelledby", button.id);
        button.setAttribute("aria-controls", view.id);
      }
      button.addEventListener("click", () => {
        if (window.location.hash === `#${key}`) activate();
        else window.location.hash = key;
      });
    });

    function activate() {
      const hash = window.location.hash.replace(/^#\/?/, "");
      const active = tabs.find((button) => button.dataset.tab === hash) || tabs[0];
      tabs.forEach((button) => {
        const on = button === active;
        button.setAttribute("aria-selected", String(on));
        button.classList.toggle("is-active", on);
        button.tabIndex = on ? 0 : -1;
      });
      if (pendingFocus) {
        const target = pendingFocus;
        pendingFocus = null;
        window.setTimeout(() => target.focus({ preventScroll: true }), 0);
      }
    }

    let pendingFocus = null;

    bar.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const index = tabs.indexOf(document.activeElement);
      let next = index;
      if (event.key === "Home") next = 0;
      else if (event.key === "End") next = tabs.length - 1;
      else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      const target = tabs[next];
      pendingFocus = target;
      target.focus({ preventScroll: true });
      if (window.location.hash === `#${target.dataset.tab}`) activate();
      else window.location.hash = target.dataset.tab;
    });

    window.addEventListener("hashchange", activate);
    activate();
  }

  /** Drawer navigasi untuk layar < 1024px (DESIGN.md §5.1). */
  function setupDrawer(container) {
    const shell = document.querySelector(".shell");
    const topbar = document.querySelector(".topbar");
    if (!shell || !topbar || document.getElementById("nav-open")) return;

    const mobile = window.matchMedia("(max-width: 1023px)");
    container.id = "sidebar-nav";

    const trigger = document.createElement("button");
    trigger.className = "nav-trigger";
    trigger.id = "nav-open";
    trigger.type = "button";
    trigger.setAttribute("aria-controls", "sidebar-nav");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-label", "Buka menu navigasi");
    trigger.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16" /><path d="M4 12h16" /><path d="M4 17h16" /></svg>';
    topbar.prepend(trigger);

    const backdrop = document.createElement("div");
    backdrop.className = "nav-backdrop";
    backdrop.hidden = true;
    backdrop.setAttribute("aria-hidden", "true");
    shell.appendChild(backdrop);

    const close = document.createElement("button");
    close.className = "nav-close";
    close.type = "button";
    close.setAttribute("aria-label", "Tutup menu navigasi");
    close.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18" /><path d="m6 6 12 12" /></svg>';
    container.prepend(close);

    let open = false;
    let restoreFocus = null;

    function focusable() {
      return Array.from(
        container.querySelectorAll('a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])')
      ).filter((el) => !el.hidden && el.offsetParent !== null);
    }

    function setOpen(next) {
      if (!mobile.matches) next = false;
      open = next;
      if (open) restoreFocus = document.activeElement;
      shell.classList.toggle("is-nav-open", open);
      document.body.classList.toggle("nav-open", open);
      trigger.setAttribute("aria-expanded", String(open));
      trigger.setAttribute("aria-label", open ? "Tutup menu navigasi" : "Buka menu navigasi");
      backdrop.hidden = !open;
      container.inert = mobile.matches && !open;
      if (mobile.matches) container.setAttribute("aria-hidden", String(!open));
      else container.removeAttribute("aria-hidden");

      if (open) {
        const first = focusable()[0];
        if (first) first.focus({ preventScroll: true });
      } else {
        if (restoreFocus && restoreFocus.isConnected && restoreFocus !== document.body) {
          restoreFocus.focus({ preventScroll: true });
        }
        restoreFocus = null;
      }
    }

    trigger.addEventListener("click", () => setOpen(!open));
    backdrop.addEventListener("click", () => setOpen(false));
    close.addEventListener("click", () => setOpen(false));

    container.addEventListener("click", (event) => {
      if (event.target.closest("a[href]")) setOpen(false);
    });

    document.addEventListener("keydown", (event) => {
      if (!open) return;
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });

    mobile.addEventListener("change", () => {
      if (!mobile.matches) setOpen(false);
      else if (!open) {
        container.inert = true;
        container.setAttribute("aria-hidden", "true");
      }
    });

    if (mobile.matches) {
      container.inert = true;
      container.setAttribute("aria-hidden", "true");
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
      setupDrawer(container);
      setupPageTabs(me);
      refreshProblemsBadge();
      // Hash berubah: render ulang menu, lalu pindahkan fokus ke judul
      // supaya pengguna keyboard/screen reader tidak kehilangan posisi.
      window.addEventListener("hashchange", () => {
        renderNav(container, window.__nmsMe || me);
        const title = document.getElementById("view-title");
        if (title) {
          title.setAttribute("tabindex", "-1");
          title.focus({ preventScroll: true });
        }
      });
      window.addEventListener("nms:problems-changed", refreshProblemsBadge);
      window.dispatchEvent(new CustomEvent("nms:me-ready"));
    } catch {
      /* apiFetch sudah menangani redirect 401 */
    }
  }

  init();
})();
