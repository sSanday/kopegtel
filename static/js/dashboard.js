/* NMS Kopegtel — logika tampilan saja.
   Hanya membaca API GET yang sudah ada (/api/hosts, /api/stats,
   /api/history, /api/events). Tidak mengubah fitur backend. */
(function () {
  "use strict";

  var boot = window.__NMS_BOOT__ || {};
  var PALET = ["#3fb9ae", "#7aa2f7", "#dda44e", "#46c08a", "#bb9af7", "#e2665b", "#4dc3ff"];
  var rentangAktif = 24;
  var hostAktif = {}; // ip -> true/false
  var chart = null;
  var cacheRiwayat = {}; // hours -> {labels, datasets}
  var kurangiGerak = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- Tema ---------- */
  var akar = document.documentElement;
  function temaAwal() {
    try {
      var simpan = localStorage.getItem("nms-tema");
      if (simpan === "terang" || simpan === "gelap") return simpan;
    } catch (e) { /* abaikan */ }
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "terang" : "gelap";
  }
  function terapkanTema(t) {
    akar.setAttribute("data-theme", t);
    var gelap = t === "gelap";
    ["btnTemaSide", "btnTemaTop"].forEach(function (id) {
      var b = document.getElementById(id);
      if (b) b.setAttribute("aria-pressed", String(!gelap));
    });
    try { localStorage.setItem("nms-tema", t); } catch (e) { /* abaikan */ }
  }
  terapkanTema(temaAwal());
  ["btnTemaSide", "btnTemaTop"].forEach(function (id) {
    var b = document.getElementById(id);
    if (b) b.addEventListener("click", function () {
      terapkanTema(akar.getAttribute("data-theme") === "terang" ? "gelap" : "terang");
    });
  });

  /* ---------- Drawer ponsel ---------- */
  var btnMenu = document.getElementById("btnMenu");
  var overlay = document.getElementById("overlay");
  function setDrawer(buka) {
    document.body.classList.toggle("drawer-terbuka", buka);
    if (overlay) overlay.hidden = !buka;
    if (btnMenu) {
      btnMenu.setAttribute("aria-expanded", String(buka));
      btnMenu.setAttribute("aria-label", buka ? "Tutup navigasi" : "Buka navigasi");
    }
    if (buka) {
      var tautan = document.querySelector(".sidebar .nav a");
      if (tautan) tautan.focus();
    }
  }
  if (btnMenu) btnMenu.addEventListener("click", function () {
    setDrawer(!document.body.classList.contains("drawer-terbuka"));
  });
  if (overlay) overlay.addEventListener("click", function () { setDrawer(false); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") setDrawer(false);
  });

  /* ---------- Util ---------- */
  function ambil(url) {
    return fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }
  function fmtBilangan(v, desimal) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return "—";
    return Number(v).toLocaleString("id-ID", { maximumFractionDigits: desimal === undefined ? 1 : desimal });
  }
  // -1 dari API berarti tidak merespons -> null agar garis terputus.
  function keTitik(v) {
    if (v === null || v === undefined || v === -1) return null;
    var n = Number(v);
    if (!isFinite(n)) return null;
    // Skala log tidak bisa menggambar di bawah 0,1: jepit tampilan ke 0,1,
    // nilai asli tetap ditunjukkan di tooltip.
    if (n < 0.1 && n > 0) return 0.1;
    return n;
  }
  function durasiPendek(teks, berlangsung) {
    // API memberi "12m 5s" atau "Ongoing". Tampilkan "1j 48m" yang ditonjolkan.
    if (berlangsung) return "Berlangsung";
    if (!teks || teks === "—" || teks === "Ongoing") return "—";
    var m = /(\d+)\s*m/.exec(teks);
    var menit = m ? parseInt(m[1], 10) : 0;
    var s = /(\d+)\s*s/.exec(teks);
    var detik = s ? parseInt(s[1], 10) : 0;
    if (!menit && detik) return detik + " dtk";
    var jam = Math.floor(menit / 60);
    var sisa = menit % 60;
    if (jam > 0) return jam + "j " + sisa + "m";
    return menit + "m";
  }

  /* ---------- Sparkline ---------- */
  function gambarSpark(canvas) {
    var data;
    try { data = JSON.parse(canvas.getAttribute("data-spark") || "[]"); }
    catch (e) { data = []; }
    var ctx = canvas.getContext("2d");
    var W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    var titik = data.map(keTitik);
    var valid = titik.filter(function (v) { return v !== null; });
    if (!valid.length) {
      ctx.strokeStyle = "#8a94a3";
      ctx.setLineDash([3, 3]);
      ctx.beginPath(); ctx.moveTo(4, H / 2); ctx.lineTo(W - 4, H / 2); ctx.stroke();
      ctx.setLineDash([]);
      return;
    }
    var min = Math.min.apply(null, valid);
    var maks = Math.max.apply(null, valid);
    if (maks - min < 0.001) { maks = min + 1; }
    var warna = getComputedStyle(document.documentElement).getPropertyValue("--aksen").trim() || "#3fb9ae";
    ctx.strokeStyle = warna;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    var penaTurun = false;
    titik.forEach(function (v, i) {
      var x = 2 + (i / Math.max(1, titik.length - 1)) * (W - 4);
      if (v === null) { penaTurun = false; return; }
      var y = H - 3 - ((v - min) / (maks - min)) * (H - 6);
      if (!penaTurun) { ctx.moveTo(x, y); penaTurun = true; }
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }
  function gambarSemuaSpark() {
    document.querySelectorAll("canvas.spark").forEach(gambarSpark);
  }

  /* ---------- Pita 96 blok ---------- */
  function bangunPita(datasets) {
    var el = document.getElementById("pita");
    var kosong = document.getElementById("pitaKosong");
    if (!el) return;
    var hosts = Object.keys(datasets || {});
    if (!hosts.length) {
      el.innerHTML = "";
      if (kosong) kosong.hidden = false;
      return;
    }
    if (kosong) kosong.hidden = true;
    // Bagi deret waktu jadi 96 ember 15 menit.
    var semuaPanjang = Math.max.apply(null, hosts.map(function (h) { return (datasets[h] || []).length; }).concat([1]));
    var perEmber = Math.max(1, Math.floor(semuaPanjang / 96));
    var html = "";
    for (var b = 0; b < 96; b++) {
      var turun = 0, isi = 0;
      hosts.forEach(function (h) {
        var seri = datasets[h] || [];
        var potong = seri.slice(b * perEmber, b * perEmber + perEmber);
        var ada = potong.filter(function (v) { return v !== null && v !== undefined; });
        if (!ada.length) return;
        isi++;
        var mati = ada.filter(function (v) { return v === -1; }).length;
        if (mati === ada.length) turun++;
        else if (mati > 0) turun += 0.5;
      });
      var kelas = "blok-kosong";
      if (isi > 0) {
        if (turun >= isi) kelas = "blok-turun";
        else if (turun > 0) kelas = "blok-sebagian";
        else kelas = "blok-normal";
      }
      html += '<span class="blok ' + kelas + '" title="Blok ' + (b + 1) + ' dari 96"></span>';
    }
    el.innerHTML = html;
  }

  /* ---------- Grafik utama ---------- */
  function warnaHost(i) { return PALET[i % PALET.length]; }

  function bangunToggle(hosts) {
    var wadah = document.getElementById("toggleHost");
    if (!wadah) return;
    wadah.innerHTML = "";
    hosts.forEach(function (ip, i) {
      if (!(ip in hostAktif)) hostAktif[ip] = true;
      var b = document.createElement("button");
      b.type = "button";
      b.setAttribute("aria-pressed", String(hostAktif[ip]));
      var sw = document.createElement("span");
      sw.className = "swatch";
      sw.style.background = warnaHost(i);
      b.appendChild(sw);
      b.appendChild(document.createTextNode(ip));
      b.addEventListener("click", function () {
        hostAktif[ip] = !hostAktif[ip];
        b.setAttribute("aria-pressed", String(hostAktif[ip]));
        gambarUlang();
      });
      wadah.appendChild(b);
    });
  }

  function gambarUlang() {
    var data = cacheRiwayat[rentangAktif];
    var kanvas = document.getElementById("grafikLatensi");
    var kosong = document.getElementById("grafikKosong");
    var galat = document.getElementById("grafikGalat");
    if (!kanvas || typeof Chart === "undefined") return;
    if (!data || !Object.keys(data.datasets || {}).length) {
      if (kosong) kosong.hidden = false;
      return;
    }
    if (kosong) kosong.hidden = true;
    if (galat) galat.hidden = true;

    var hosts = Object.keys(data.datasets).sort();
    bangunToggle(hosts);
    var tampil = hosts.filter(function (h) { return hostAktif[h] !== false; });
    var setData = tampil.map(function (ip, i) {
      return {
        label: ip,
        data: (data.datasets[ip] || []).map(keTitik),
        asli: data.datasets[ip] || [],
        borderColor: warnaHost(hosts.indexOf(ip)),
        backgroundColor: warnaHost(hosts.indexOf(ip)),
        borderWidth: 1.6,
        pointRadius: 0,
        pointHoverRadius: 3,
        tension: 0.25,
        spanGaps: false // penting: garis terputus saat null
      };
    });

    var grid = getComputedStyle(document.documentElement).getPropertyValue("--garis").trim() || "#28313e";
    var redup = getComputedStyle(document.documentElement).getPropertyValue("--teks-redup").trim() || "#9aa7b8";
    if (chart) chart.destroy();
    chart = new Chart(kanvas, {
      type: "line",
      data: { labels: data.labels, datasets: setData },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: kurangiGerak ? false : { duration: 400 },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (c) {
                var a = c.dataset.asli ? c.dataset.asli[c.dataIndex] : null;
                if (a === -1 || a === null || a === undefined) return " " + c.dataset.label + ": tidak merespons";
                return " " + c.dataset.label + ": " + fmtBilangan(a, 2) + " ms";
              }
            }
          }
        },
        scales: {
          x: { ticks: { color: redup, maxTicksLimit: 6 }, grid: { color: grid } },
          y: {
            type: "logarithmic",
            min: 0.1,
            max: 600,
            ticks: {
              color: redup,
              callback: function (v) {
                var izin = [0.1, 1, 10, 100, 500];
                for (var k = 0; k < izin.length; k++) {
                  if (Math.abs(v - izin[k]) < 0.0001) {
                    return v === 0.1 ? "0,1" : String(izin[k]);
                  }
                }
                return "";
              }
            },
            grid: { color: grid },
            title: { display: true, text: "ms (skala log)", color: redup }
          }
        }
      }
    });
  }

  document.querySelectorAll(".rentang button").forEach(function (b) {
    b.addEventListener("click", function () {
      document.querySelectorAll(".rentang button").forEach(function (x) { x.setAttribute("aria-pressed", "false"); });
      b.setAttribute("aria-pressed", "true");
      rentangAktif = parseInt(b.getAttribute("data-rentang"), 10) || 24;
      muatRiwayat(rentangAktif);
    });
  });

  function muatRiwayat(jam) {
    var galat = document.getElementById("grafikGalat");
    if (cacheRiwayat[jam]) { gambarUlang(); return; }
    ambil("/api/history?hours=" + jam).then(function (j) {
      cacheRiwayat[jam] = { labels: j.labels || [], datasets: j.datasets || {} };
      bangunPita(cacheRiwayat[24] ? cacheRiwayat[24].datasets : (jam === 24 ? j.datasets : {}));
      gambarUlang();
    }).catch(function () {
      if (galat) galat.hidden = false;
    });
  }

  /* ---------- Host + ringkasan ---------- */
  function statusDari(stat) {
    if (!stat || stat.is_pending) return "pending";
    if (stat.is_down) return "turun";
    if ((stat.uptime_pct !== null && stat.uptime_pct < 99) || (stat.avg_loss !== null && stat.avg_loss > 5)) return "peringatan";
    return "normal";
  }
  function teksStatus(s) {
    return s === "normal" ? "Merespons" : s === "turun" ? "Tidak merespons" : s === "peringatan" ? "Perlu perhatian" : "Menunggu data";
  }

  function renderRingkasan(daftar, stats) {
    var total = daftar.length;
    var merespons = daftar.filter(function (h) {
      var s = stats[h.ip];
      return s && !s.is_pending && !s.is_down;
    }).length;
    var turun = total - merespons;
    var up = [], lat = [];
    Object.keys(stats).forEach(function (ip) {
      var s = stats[ip];
      if (s && s.uptime_pct !== null && s.uptime_pct !== undefined) up.push(s.uptime_pct);
      if (s && s.avg_ms !== null && s.avg_ms !== undefined) lat.push(s.avg_ms);
    });
    var rataUp = up.length ? up.reduce(function (a, b) { return a + b; }, 0) / up.length : null;
    var rataLat = lat.length ? lat.reduce(function (a, b) { return a + b; }, 0) / lat.length : null;

    var elM = document.getElementById("angkaMerespons");
    var elT = document.getElementById("angkaTotal");
    var elU = document.getElementById("angkaUptime");
    var elR = document.getElementById("angkaRata");
    if (elM) elM.textContent = merespons;
    if (elT) elT.textContent = total;
    if (elU) elU.textContent = rataUp === null ? "—" : fmtBilangan(rataUp, 1);
    if (elR) elR.textContent = rataLat === null ? "—" : fmtBilangan(rataLat, 1);

    var kes = document.getElementById("kesimpulan");
    if (kes) {
      if (!total) kes.textContent = "Belum ada host terdaftar. Tambahkan host dulu agar kondisi jaringan bisa disimpulkan.";
      else if (turun === total) kes.textContent = "Semua " + total + " host tidak merespons bersamaan. Kemungkinan masalah ada di gateway atau uplink, periksa itu dulu sebelum mengecek perangkat satu per satu.";
      else if (turun === 0) kes.textContent = "Semua " + total + " host merespons normal dalam 24 jam terakhir.";
      else kes.textContent = merespons + " dari " + total + " host merespons. " + turun + " host tidak merespons, mulai dari yang terdampak paling lama di daftar bawah.";
    }
    var segar = document.getElementById("waktuSegar");
    if (segar) {
      var d = new Date();
      segar.textContent = d.toLocaleString("id-ID", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
    }
  }

  function renderKelompok(daftar, stats) {
    var wadah = document.getElementById("kelompok");
    if (!wadah) return;
    var grup = {};
    daftar.forEach(function (h) {
      var jenis = (h.category || "Lainnya").trim() || "Lainnya";
      if (!grup[jenis]) grup[jenis] = [];
      grup[jenis].push(h);
    });
    var namaGrup = Object.keys(grup).sort();
    if (!namaGrup.length) {
      wadah.innerHTML = '<div class="panel"><p class="kosong">Belum ada data host untuk ditampilkan. Data awal dari database belum tersedia — tunggu pemindaian pertama selesai, lalu muat ulang halaman.</p></div>';
      return;
    }
    wadah.innerHTML = "";
    namaGrup.forEach(function (jenis) {
      var hosts = grup[jenis];
      var turun = hosts.filter(function (h) {
        var s = stats[h.ip];
        return s && !s.is_pending && s.is_down;
      }).length;
      var div = document.createElement("div");
      div.className = "kelompok";
      var h3 = document.createElement("h3");
      h3.textContent = jenis + " ";
      var info = document.createElement("span");
      info.className = "kelompok-info";
      info.textContent = turun + " dari " + hosts.length + " tidak merespons";
      h3.appendChild(info);
      div.appendChild(h3);
      var ul = document.createElement("ul");
      ul.className = "baris-daftar";
      hosts.forEach(function (h) {
        var s = stats[h.ip] || {};
        var st = statusDari(s);
        var li = document.createElement("li");
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "baris status-" + st;
        btn.setAttribute("data-host", h.ip);
        var nama = (h.alias || h.ip || "").trim() || h.ip;
        btn.innerHTML =
          '<span class="rel" aria-hidden="true"></span>' +
          '<span class="baris-identitas"><span class="baris-nama"></span><span class="mono baris-ip"></span></span>' +
          '<span class="lencana lencana-' + st + '"></span>' +
          '<canvas class="spark" width="120" height="32" aria-hidden="true"></canvas>' +
          '<span class="metrik"></span>';
        btn.querySelector(".baris-nama").textContent = nama;
        btn.querySelector(".baris-ip").textContent = h.ip;
        btn.querySelector(".lencana").textContent = teksStatus(st);
        var met = btn.querySelector(".metrik");
        var sel = [["Rata", s.avg_ms, " ms"], ["Min", s.min_ms, ""], ["Puncak", s.max_ms, ""], ["Loss", s.avg_loss, "%"], ["Uptime", s.uptime_pct, "%"]];
        sel.forEach(function (pas) {
          var sp = document.createElement("span");
          sp.textContent = pas[0] + " ";
          var b = document.createElement("b");
          b.className = "mono";
          b.textContent = (pas[1] === null || pas[1] === undefined ? "—" : fmtBilangan(pas[1], 1)) + pas[2];
          sp.appendChild(b);
          met.appendChild(sp);
        });
        btn.addEventListener("click", function () {
          hostAktif[h.ip] = true;
          document.getElementById("latensi").scrollIntoView({ behavior: kurangiGerak ? "auto" : "smooth", block: "start" });
          gambarUlang();
        });
        li.appendChild(btn);
        ul.appendChild(li);
      });
      div.appendChild(ul);
      wadah.appendChild(div);
    });
  }

  function renderSisi(daftar, stats, sebelum) {
    var ul = document.getElementById("sideDaftar");
    if (!ul) return;
    ul.innerHTML = "";
    if (!daftar.length) {
      ul.innerHTML = '<li class="side-kosong">Belum ada host terdaftar. Tambahkan host dulu agar daftar muncul di sini.</li>';
      return;
    }
    daftar.forEach(function (h) {
      var st = statusDari(stats[h.ip]);
      var li = document.createElement("li");
      var dot = document.createElement("span");
      dot.className = "titik titik-" + st;
      dot.setAttribute("aria-hidden", "true");
      if (sebelum && sebelum[h.ip] && sebelum[h.ip] !== st && !kurangiGerak) {
        dot.classList.add("berubah");
        setTimeout(function () { dot.classList.remove("berubah"); }, 1300);
      }
      var nm = document.createElement("span");
      nm.className = "side-nama";
      nm.textContent = (h.alias || h.ip);
      var ip = document.createElement("span");
      ip.className = "mono side-ip";
      ip.textContent = h.ip;
      li.appendChild(dot); li.appendChild(nm); li.appendChild(ip);
      ul.appendChild(li);
    });
  }

  /* ---------- Timeline ---------- */
  function renderTimeline(events) {
    var ol = document.getElementById("timeline");
    if (!ol) return;
    ol.innerHTML = "";
    if (!events || !events.length) {
      ol.innerHTML = '<li class="kosong-li"><p class="kosong">Tidak ada gangguan dalam 24 jam terakhir. Riwayat baru akan muncul di sini jika ada host yang berhenti merespons.</p></li>';
      return;
    }
    var berjalan = events.filter(function (e) { return e.status === "ongoing"; });
    var selesai = events.filter(function (e) { return e.status !== "ongoing"; });
    berjalan.concat(selesai).slice(0, 20).forEach(function (e) {
      var li = document.createElement("li");
      li.className = "tl" + (e.status === "ongoing" ? " berlangsung" : "");
      var dur = durasiPendek(e.duration, e.status === "ongoing");
      li.innerHTML = '<span class="tl-titik" aria-hidden="true"></span><div class="tl-isi">' +
        '<p class="tl-durasi mono"></p><p class="tl-host"></p><p class="tl-waktu mono"></p></div>';
      var badge = e.status === "ongoing" ? ' <span class="lencana lencana-turun">Berlangsung</span>' : "";
      li.querySelector(".tl-durasi").innerHTML = "";
      li.querySelector(".tl-durasi").appendChild(document.createTextNode(dur));
      li.querySelector(".tl-durasi").insertAdjacentHTML("beforeend", badge);
      li.querySelector(".tl-host").textContent = e.host || "Host tak dikenal";
      li.querySelector(".tl-waktu").textContent = "Mulai " + (e.started_at || "—") + " · Pulih " + (e.status === "ongoing" ? "belum pulih" : (e.resolved_at || "—"));
      ol.appendChild(li);
    });
  }

  /* ---------- Muat awal ---------- */
  var statusSebelum = {};
  function segarkan() {
    ambil("/api/hosts").then(function (hosts) {
      return ambil("/api/stats").then(function (stats) {
        renderSisi(hosts, stats, statusSebelum);
        Object.keys(stats).forEach(function (ip) { statusSebelum[ip] = statusDari(stats[ip]); });
        renderRingkasan(hosts, stats);
        renderKelompok(hosts, stats);
        // Spark memakai ekor riwayat 1 jam bila tersedia.
        return ambil("/api/history?hours=1").then(function (j) {
          cacheRiwayat[1] = { labels: j.labels || [], datasets: j.datasets || {} };
          document.querySelectorAll(".baris").forEach(function (baris) {
            var ip = baris.getAttribute("data-host");
            var seri = (j.datasets || {})[ip] || [];
            var c = baris.querySelector("canvas.spark");
            if (c) {
              c.setAttribute("data-spark", JSON.stringify(seri.slice(-20)));
              gambarSpark(c);
            }
          });
          if (!cacheRiwayat[rentangAktif]) cacheRiwayat[rentangAktif] = cacheRiwayat[1];
          if (rentangAktif === 1) gambarUlang();
        }).catch(function () { gambarSemuaSpark(); });
      });
    }).catch(function () {
      var g = document.getElementById("grafikGalat");
      if (g) g.hidden = false;
    });
    ambil("/api/events").then(renderTimeline).catch(function () { renderTimeline([]); });
    if (rentangAktif !== 1) muatRiwayat(rentangAktif);
  }

  // Data boot dari Jinja (bila ada) agar grafik langsung terisi.
  if (boot && boot.series && Object.keys(boot.series).length) {
    cacheRiwayat[24] = { labels: boot.labels || [], datasets: boot.series };
  }
  gambarSemuaSpark();
  segarkan();
  setInterval(segarkan, 60000);
})();
