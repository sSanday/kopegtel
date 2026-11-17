/* Penyimpanan sementara sisi klien (TTL 5 detik) + penyegar otomatis.
 * Cache menyimpan promise yang sedang berjalan supaya permintaan bersamaan
 * untuk key yang sama hanya menembak backend sekali. */
(function () {
  "use strict";

  const TTL_MS = 5000;
  const cache = new Map();

  /**
   * Ambil dari cache bila segar; jika tidak, jalankan fetcher lalu simpan
   * promise-nya. Pemanggil yang datang saat request masih berjalan berbagi
   * promise yang sama. Kegagalan tidak di-cache agar bisa dicoba lagi.
   * key: string unik, mis. 'devices:page1:query=...'
   */
  function cached(key, fetcher, options) {
    const ttl = (options && options.ttl) || TTL_MS;
    const hit = cache.get(key);
    if (hit && Date.now() - hit.at < ttl) return hit.promise;

    const entry = { at: Date.now(), promise: null };
    entry.promise = Promise.resolve()
      .then(fetcher)
      .then(
        (value) => {
          entry.at = Date.now();
          return value;
        },
        (error) => {
          if (cache.get(key) === entry) cache.delete(key);
          throw error;
        }
      );
    cache.set(key, entry);
    return entry.promise;
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
