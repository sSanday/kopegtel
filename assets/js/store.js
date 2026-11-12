/* Penyimpanan sementara sisi klien (TTL 5 detik) + penyegar otomatis. */
(function () {
  "use strict";

  const TTL_MS = 5000;
  const cache = new Map();

  /**
   * Ambil dari cache bila segar; jika tidak, jalankan fetcher lalu simpan.
   * key: string unik, mis. 'devices:page1:query=...'
   */
  async function cached(key, fetcher, options) {
    const ttl = (options && options.ttl) || TTL_MS;
    const now = Date.now();
    const hit = cache.get(key);
    if (hit && now - hit.at < ttl) {
      return hit.value;
    }
    const value = await fetcher();
    cache.set(key, { at: Date.now(), value });
    return value;
  }

  function invalidate(prefix) {
    if (!prefix) {
      cache.clear();
      return;
    }
    for (const key of cache.keys()) {
      if (key.startsWith(prefix)) cache.delete(key);
    }
  }

  /**
   * Jalankan fn setiap intervalMs selagi tab terlihat; berhenti otomatis
   * saat dokumen tersembunyi lalu memuat ulang saat terlihat kembali.
   */
  function startAutoRefresh(fn, intervalMs) {
    const interval = intervalMs || 15000;
    let timer = null;

    function tick() {
      if (document.visibilityState === "visible") {
        Promise.resolve()
          .then(fn)
          .catch(() => {
            /* error sudah ditangani halaman */
          });
      }
    }

    function start() {
      if (timer === null) {
        timer = window.setInterval(tick, interval);
      }
    }

    function stop() {
      if (timer !== null) {
        window.clearInterval(timer);
        timer = null;
      }
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        tick();
        start();
      } else {
        stop();
      }
    });
    start();
  }

  window.NMSStore = { cached, invalidate, startAutoRefresh };
})();
