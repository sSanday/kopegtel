/* Format bersama: escaping, waktu relatif Indonesia, satuan bps/byte, status. */
(function () {
  "use strict";

  /** Escape teks agar aman disisipkan ke innerHTML. */
  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /** Waktu UTC dari server ('YYYY-MM-DDTHH:MM:SSZ') → Date. */
  function parseServerTs(value) {
    if (!value) return null;
    const iso = String(value).includes("T") ? String(value) : String(value).replace(" ", "T");
    const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
    return Number.isNaN(d.getTime()) ? null : d;
  }

  function formatDateTime(value, timeZone) {
    const d = parseServerTs(value);
    if (!d) return "—";
    try {
      return new Intl.DateTimeFormat("id-ID", {
        day: "2-digit",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
        timeZone: timeZone || "Asia/Jakarta",
      }).format(d);
    } catch {
      return d.toISOString();
    }
  }

  function formatDateTimeFull(value, timeZone) {
    const d = parseServerTs(value);
    if (!d) return "—";
    try {
      return new Intl.DateTimeFormat("id-ID", {
        weekday: "long",
        day: "numeric",
        month: "long",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
        timeZone: timeZone || "Asia/Jakarta",
      }).format(d);
    } catch {
      return d.toISOString();
    }
  }

  function timeAgo(value) {
    const d = parseServerTs(value);
    if (!d) return "—";
    const diffSec = Math.max(0, Math.floor((Date.now() - d.getTime()) / 1000));
    if (diffSec < 10) return "baru saja";
    if (diffSec < 60) return `${diffSec} detik yang lalu`;
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return `${diffMin} menit yang lalu`;
    const diffHour = Math.floor(diffMin / 60);
    if (diffHour < 24) return `${diffHour} jam yang lalu`;
    const diffDay = Math.floor(diffHour / 24);
    if (diffDay < 30) return `${diffDay} hari yang lalu`;
    return formatDateTime(value);
  }

  /** Persen dengan koma (id-ID). null/NaN → '—'. */
  function formatPercent(value, digits) {
    const n = Number(value);
    if (value == null || Number.isNaN(n)) return "—";
    const fractionDigits = digits == null ? 1 : digits;
    return `${n.toLocaleString("id-ID", { minimumFractionDigits: fractionDigits, maximumFractionDigits: fractionDigits })}%`;
  }

  function formatBytes(bytes) {
    const n = Number(bytes);
    if (bytes == null || Number.isNaN(n)) return "—";
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let value = n;
    let index = 0;
    while (value >= 1024 && index < units.length - 1) {
      value /= 1024;
      index += 1;
    }
    const rounded = value >= 100 || index === 0 ? Math.round(value) : value.toFixed(1);
    return `${String(rounded).replace(".", ",")} ${units[index]}`;
  }

  function formatBps(bps) {
    const n = Number(bps);
    if (bps == null || Number.isNaN(n)) return "—";
    const units = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"];
    let value = n;
    let index = 0;
    while (value >= 1000 && index < units.length - 1) {
      value /= 1000;
      index += 1;
    }
    const rounded = value >= 100 ? Math.round(value) : value.toFixed(1);
    return `${String(rounded).replace(".", ",")} ${units[index]}`;
  }

  function formatUptime(seconds) {
    const n = Number(seconds);
    if (seconds == null || Number.isNaN(n)) return "—";
    const days = Math.floor(n / 86400);
    const hours = Math.floor((n % 86400) / 3600);
    const minutes = Math.floor((n % 3600) / 60);
    if (days > 0) return `${days} hari ${hours} jam`;
    if (hours > 0) return `${hours} jam ${minutes} menit`;
    return `${minutes} menit`;
  }

  /** Metadata status → kelas + label (tanpa mengandalkan warna saja). */
  const STATUS_META = {
    online: { cls: "status--ok", label: "Online" },
    degraded: { cls: "status--warn", label: "Degradasi" },
    offline: { cls: "status--down", label: "Offline" },
    maintenance: { cls: "status--idle", label: "Pemeliharaan" },
    unknown: { cls: "status--idle", label: "Tidak diketahui" },
  };

  const TYPE_LABELS = {
    mikrotik: "MikroTik",
    router: "Router",
    switch: "Switch",
    "access-point": "Access Point",
    server: "Server",
    olt: "OLT",
    ont: "ONT",
    firewall: "Firewall",
    lainnya: "Lainnya",
  };

  const SEVERITY_META = {
    disaster: { cls: "sev sev--disaster", label: "Disaster", rank: 5 },
    high: { cls: "sev sev--high", label: "High", rank: 4 },
    average: { cls: "sev sev--average", label: "Average", rank: 3 },
    warning: { cls: "sev sev--warning", label: "Warning", rank: 2 },
    information: { cls: "sev sev--information", label: "Information", rank: 1 },
  };

  /** Sel berwarna nilai → kelas avail (--ok/--warn/--down) + label teks. */
  function availMeta(pct, warn, crit) {
    if (pct == null) return { cls: "avail", label: "—" };
    if (pct >= crit) return { cls: "avail avail--down", label: formatPercent(pct, 0) };
    if (pct >= warn) return { cls: "avail avail--warn", label: formatPercent(pct, 0) };
    return { cls: "avail avail--ok", label: formatPercent(pct, 0) };
  }

  function availabilityClass(pct) {
    if (pct == null) return "avail";
    if (pct >= 99) return "avail avail--ok";
    if (pct >= 95) return "avail avail--warn";
    return "avail avail--down";
  }

  window.NMSFormat = {
    escapeHtml,
    parseServerTs,
    formatDateTime,
    formatDateTimeFull,
    timeAgo,
    formatPercent,
    formatBytes,
    formatBps,
    formatUptime,
    STATUS_META,
    TYPE_LABELS,
    SEVERITY_META,
    availMeta,
    availabilityClass,
  };
})();
