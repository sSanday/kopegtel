"""Test prediksi tren Rx: fungsi murni, lapisan DB, endpoint, dan alert.

Skenario:
- fungsi murni compute_trend_from_points (linear, ber-noise, stabil,
  membaik, lompatan mendadak, data kurang, sudah di bawah level,
  override threshold per ONT, cap 90 hari).
- load_daily_points + compute_all_trends + endpoint /api/fiber/<id>/trend
  dan /api/fiber/trend/summary (butuh login).
- peringatan: simulator/mute/maintenance/stale/horizon men-suppress alert,
  dedupe 7 hari, reset saat status tak lagi degrading (mock send_alert).
"""

import os
import random
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

_tmp = tempfile.mkdtemp(prefix="nms_test_fiber_trend_")
os.environ.setdefault("NMS_DB_PATH", os.path.join(_tmp, "test.db"))
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("DASHBOARD_USERNAME", "admin")
os.environ.setdefault("DASHBOARD_PASSWORD", "PantauNms2026")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("TELEGRAM_CHAT_ID", "")
os.environ.setdefault("AGENT_API_KEY", "test-agent-key")
os.environ.setdefault("NMS_DISABLE_SCHEDULER", "1")

import app as m

try:
    m.scheduler.shutdown(wait=False)
except Exception:
    pass

from nms import fiber_poll as _fmod
from nms import fiber_trend as _tmod

JSON_HDR = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
XRW_HDR = {"X-Requested-With": "XMLHttpRequest"}

TH = {"warn": -25.0, "crit": -27.0}

P_DAILY = "TREND-DAILY-"
P_API = "TREND-API-"
P_JOB = "TREND-JOB-"
P_ALERT = "TREND-ALERT-"


def _mk_points(
    n=10,
    start_rx=-19.0,
    slope=-0.2,
    nsamp=48,
    start=(2026, 9, 1),
    noise=0.0,
    seed=7,
):
    """Points sintetis: list (tanggal, rata-rata, jumlah sampel)."""
    rng = random.Random(seed)
    d0 = date(*start)
    out = []
    for i in range(n):
        rx = start_rx + slope * i
        if noise:
            rx += rng.uniform(-noise, noise)
        out.append(((d0 + timedelta(days=i)).strftime("%Y-%m-%d"), round(rx, 3), nsamp))
    return out


def _cleanup_onts(prefixes):
    conn, c = m.get_db()
    try:
        for prefix in prefixes:
            c.execute(
                "DELETE FROM fiber_history WHERE ont_id IN "
                "(SELECT id FROM fiber_onts WHERE ont_sn LIKE ?)",
                (prefix + "%",),
            )
            c.execute("DELETE FROM fiber_onts WHERE ont_sn LIKE ?", (prefix + "%",))
            c.execute(
                "DELETE FROM maintenance_windows WHERE host LIKE ?", (prefix + "%",)
            )
        m._commit_with_retry(conn)
    finally:
        conn.close()
    _tmod.fiber_trend_memory.clear()
    _tmod.fiber_trend_alert_mem.clear()


def _mk_ont(
    sn,
    source="manual",
    rx=-23.0,
    customer="Uji Tren",
    mute=0,
    mute_until="",
    last_seen=None,
    rx_warn=None,
    rx_crit=None,
):
    """Buat ONT langsung via SQL; kembalikan id."""
    conn, c = m.get_db()
    try:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            "INSERT INTO fiber_onts (ont_sn, customer, olt_name, pon_port, "
            "odp_name, rx_power, tx_power, status, source, created_at, "
            "updated_at, last_checked, mute_alarm, mute_until, last_seen, "
            "rx_warn, rx_crit) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sn,
                customer,
                "OLT-UJI",
                "1/1/1",
                "ODP-UJI",
                rx,
                2.0,
                "unknown",
                source,
                stamp,
                stamp,
                stamp,
                mute,
                mute_until,
                last_seen or stamp,
                rx_warn,
                rx_crit,
            ),
        )
        m._commit_with_retry(conn)
        c.execute("SELECT id FROM fiber_onts WHERE ont_sn=?", (sn,))
        return c.fetchone()["id"]
    finally:
        conn.close()


def _mk_history(
    fid,
    days=21,
    start_rx=-19.0,
    slope=-0.24,
    per_day=48,
    noise=0.3,
    seed=42,
    rx_missing=False,
):
    """Isi fiber_history: `days` hari, `per_day` sampel/hari, noise ±."""
    rng = random.Random(seed)
    now = datetime.now()
    rows = []
    for i in range(days):
        day = now - timedelta(days=(days - 1) - i)
        dstr = day.strftime("%Y-%m-%d")
        base = start_rx + slope * i
        for slot in range(per_day):
            ts = f"{dstr} {slot // 2:02d}:{(slot % 2) * 30:02d}:00"
            if rx_missing:
                rows.append((fid, None, None, ts))
            else:
                rows.append(
                    (fid, round(base + rng.uniform(-noise, noise), 2), None, ts)
                )
    conn, c = m.get_db()
    try:
        c.executemany(
            "INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp) "
            "VALUES (?,?,?,?)",
            rows,
        )
        m._commit_with_retry(conn)
    finally:
        conn.close()
    return len(rows)


class TrendComputeTest(unittest.TestCase):
    """Fungsi murni compute_trend_from_points."""

    def test_linear_turun_slope_dan_eta_tepat(self):
        pts = _mk_points(n=10, start_rx=-19.0, slope=-0.2, noise=0.0)
        r = _tmod.compute_trend_from_points(
            pts, TH, now=datetime(2026, 9, 10, 12, 0, 0)
        )
        self.assertEqual(r["status"], "degrading")
        self.assertAlmostEqual(r["slope_db_per_day"], -0.2, places=3)
        # Garis fit pada hari terakhir: -19.0 + (-0.2)*9 = -20.8.
        self.assertAlmostEqual(r["rx_now"], -20.8, delta=0.05)
        self.assertAlmostEqual(r["eta_warn_days"], (-20.8 + 25.0) / 0.2, delta=0.5)
        self.assertAlmostEqual(r["eta_crit_days"], (-20.8 + 27.0) / 0.2, delta=0.5)
        self.assertEqual(r["n_days"], 10)
        self.assertEqual(r["window_days"], 10)
        self.assertEqual(r["eta_warn_at"], "2026-10-01")
        self.assertEqual(r["confidence"], "tinggi")
        rng = r["eta_warn_range"]
        self.assertEqual(rng["min"], rng["max"])  # semua pasangan identik

    def test_turun_bernoise(self):
        pts = _mk_points(n=12, start_rx=-19.0, slope=-0.3, noise=0.15)
        r = _tmod.compute_trend_from_points(pts, TH)
        self.assertEqual(r["status"], "degrading")
        self.assertTrue(-0.36 <= r["slope_db_per_day"] <= -0.24)
        self.assertIn(r["confidence"], ("sedang", "tinggi"))
        self.assertIsNotNone(r["eta_warn_days"])
        rmin = r["eta_warn_range"]["min"]
        rmax = r["eta_warn_range"]["max"]
        self.assertTrue(rmin <= r["eta_warn_days"])
        self.assertTrue(rmax is None or rmax >= r["eta_warn_days"])

    def test_stabil(self):
        pts = _mk_points(n=10, start_rx=-19.0, slope=0.0, noise=0.05)
        r = _tmod.compute_trend_from_points(pts, TH)
        self.assertEqual(r["status"], "stable")
        self.assertLess(abs(r["slope_db_per_day"]), 0.05)
        self.assertIsNone(r["eta_warn_days"])
        self.assertIsNone(r["eta_crit_days"])

    def test_membaik(self):
        pts = _mk_points(n=10, start_rx=-24.0, slope=0.2, noise=0.03)
        r = _tmod.compute_trend_from_points(pts, TH)
        self.assertEqual(r["status"], "improving")
        self.assertGreater(r["slope_db_per_day"], 0)
        self.assertIsNone(r["eta_warn_days"])
        self.assertEqual(r["confidence"], "rendah")

    def test_lompatan_terpotong_pakai_data_setelahnya(self):
        pts = []
        d0 = date(2026, 9, 1)
        for i in range(12):
            rx = -19.0 - 0.1 * i if i < 6 else -15.0
            pts.append(((d0 + timedelta(days=i)).strftime("%Y-%m-%d"), rx, 48))
        r = _tmod.compute_trend_from_points(pts, TH)
        self.assertEqual(r["n_days"], 6)
        self.assertEqual(r["status"], "stable")
        self.assertIsNone(r["reason"])
        self.assertAlmostEqual(r["rx_now"], -15.0, delta=0.1)

    def test_lompatan_sisa_hari_kurang(self):
        pts = []
        d0 = date(2026, 9, 1)
        for i in range(9):
            rx = -19.0 - 0.1 * i if i < 5 else -15.0
            pts.append(((d0 + timedelta(days=i)).strftime("%Y-%m-%d"), rx, 48))
        r = _tmod.compute_trend_from_points(pts, TH)
        self.assertEqual(r["status"], "insufficient_data")
        self.assertEqual(r["reason"], "perubahan mendadak")

    def test_hari_sampel_kurang_dibuang(self):
        pts = _mk_points(n=6, start_rx=-19.0, slope=-0.2)
        pts[0] = (pts[0][0], pts[0][1], 10)  # hari pertama < 20 sampel
        r = _tmod.compute_trend_from_points(pts, TH)
        self.assertEqual(r["n_days"], 5)
        self.assertEqual(r["status"], "degrading")

    def test_data_kurang_dari_5_hari(self):
        pts = _mk_points(n=4, start_rx=-19.0, slope=-0.2)
        r = _tmod.compute_trend_from_points(pts, TH)
        self.assertEqual(r["status"], "insufficient_data")
        self.assertIsNone(r["reason"])
        self.assertIsNone(r["rx_now"])

    def test_rx_sudah_di_bawah_crit_tanpa_eta(self):
        pts = _mk_points(n=10, start_rx=-27.5, slope=-0.05)
        r = _tmod.compute_trend_from_points(pts, TH)
        self.assertEqual(r["status"], "degrading")
        self.assertIsNone(r["eta_warn_days"])
        self.assertIsNone(r["eta_crit_days"])

    def test_rx_di_antara_warn_dan_crit(self):
        pts = _mk_points(n=10, start_rx=-25.5, slope=-0.05)
        r = _tmod.compute_trend_from_points(pts, TH)
        self.assertEqual(r["status"], "degrading")
        self.assertIsNone(r["eta_warn_days"])  # sudah di bawah warn
        self.assertIsNotNone(r["eta_crit_days"])

    def test_override_threshold_per_ont(self):
        pts = _mk_points(n=10, start_rx=-19.0, slope=-0.2)
        # rx_now garis fit = -20.8; override global (-25/-27) menjadi
        # warn -21 / crit -23 sehingga ETA jauh lebih dekat.
        r = _tmod.compute_trend_from_points(pts, {"warn": -21.0, "crit": -23.0})
        self.assertEqual(r["status"], "degrading")
        self.assertAlmostEqual(r["eta_warn_days"], 1.0, delta=0.05)
        self.assertAlmostEqual(r["eta_crit_days"], 11.0, delta=0.05)

    def test_eta_di_atas_90_hari_diberi_flag(self):
        pts = _mk_points(n=10, start_rx=-19.0, slope=-0.01)
        r = _tmod.compute_trend_from_points(pts, TH, settings={"min_slope": 0.004})
        self.assertEqual(r["status"], "degrading")
        self.assertIsNone(r["eta_warn_days"])
        self.assertEqual(r["eta_warn_flag"], "lebih dari 90 hari")
        self.assertIsNone(r["eta_crit_days"])
        self.assertEqual(r["eta_crit_flag"], "lebih dari 90 hari")

    def test_slope_batas_tepat_masih_degrading(self):
        pts = _mk_points(n=10, start_rx=-19.0, slope=-0.05, noise=0.0)
        r = _tmod.compute_trend_from_points(pts, TH)
        self.assertEqual(r["status"], "degrading")


class TrendDbTest(unittest.TestCase):
    """Lapisan DB + endpoint (butuh login)."""

    @classmethod
    def setUpClass(cls):
        _cleanup_onts([P_DAILY, P_API])
        cls.client = m.app.test_client()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
        assert r.status_code == 302, f"login gagal, status={r.status_code}"

    @classmethod
    def tearDownClass(cls):
        _cleanup_onts([P_DAILY, P_API])

    def test_load_daily_points_agregat(self):
        fid = _mk_ont(P_DAILY + "01", rx=-20.0)
        fid2 = _mk_ont(P_DAILY + "02", rx=-21.0, source="simulator")
        now = datetime.now()
        rows = []
        plan = {
            fid: [
                (0, -19.0, 20, None),
                (0, -20.0, 20, None),
                (1, -21.0, 15, None),
                (2, None, 10, None),  # rx NULL dibuang
            ],
            fid2: [(0, -18.0, 24, None)],
        }
        for f, entries in plan.items():
            for days_back, rx, _n, _x in entries:
                dstr = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")
                rows.append((f, rx, None, f"{dstr} 08:00:00"))
        conn, c = m.get_db()
        try:
            c.executemany(
                "INSERT INTO fiber_history (ont_id, rx_power, tx_power, timestamp) "
                "VALUES (?,?,?,?)",
                rows,
            )
            m._commit_with_retry(conn)
        finally:
            conn.close()
        out = _tmod.load_daily_points([fid, fid2, 999999], 7)
        d1 = out[fid]
        self.assertEqual(len(d1), 2)  # hari dgn rx NULL tak masuk
        today = now.strftime("%Y-%m-%d")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        # Terurut naik: entri pertama adalah hari terlama.
        self.assertEqual(d1[0][0], yesterday)
        self.assertAlmostEqual(d1[0][1], -21.0)
        self.assertEqual(d1[0][2], 1)
        self.assertEqual(d1[1][0], today)
        self.assertAlmostEqual(d1[1][1], -19.5)
        self.assertEqual(d1[1][2], 2)
        self.assertEqual(out[fid2][0][2], 1)
        self.assertEqual(out[999999], [])

    def test_endpoint_trend_dan_404(self):
        sn = P_API + "TURUN"
        fid = _mk_ont(sn, rx=-23.5)
        _mk_history(fid, days=21, start_rx=-19.0, slope=-0.24)
        r = self.client.get(f"/api/fiber/{fid}/trend", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        j = r.get_json()
        self.assertEqual(j["status"], "degrading")
        self.assertAlmostEqual(j["slope_db_per_day"], -0.24, delta=0.06)
        self.assertFalse(j["simulated"])
        self.assertIsNotNone(j["eta_warn_days"])
        self.assertIsNotNone(j["eta_warn_at"])
        self.assertEqual(j["ont_sn"], sn)
        r = self.client.get("/api/fiber/99999998/trend", headers=XRW_HDR)
        self.assertEqual(r.status_code, 404)

    def test_endpoint_trend_butuh_login(self):
        anon = m.app.test_client()
        r = anon.get("/api/fiber/1/trend", headers=XRW_HDR)
        self.assertEqual(r.status_code, 401)
        r = anon.get("/api/fiber/trend/summary", headers=XRW_HDR)
        self.assertEqual(r.status_code, 401)

    def test_endpoint_summary_excludes_simulator(self):
        sn_real = P_API + "SUMMARY"
        fid = _mk_ont(sn_real, rx=-23.5)
        _mk_history(fid, days=21, start_rx=-19.0, slope=-0.24)
        sn_sim = P_API + "SIM"
        fid_sim = _mk_ont(sn_sim, rx=-21.0, source="simulator")
        _mk_history(fid_sim, days=21, start_rx=-19.0, slope=-0.24, seed=11)
        _tmod.compute_all_trends()
        res_real = _tmod.fiber_trend_memory.get(fid) or {}
        self.assertEqual(res_real.get("status"), "degrading")
        res_sim = _tmod.fiber_trend_memory.get(fid_sim) or {}
        self.assertTrue(res_sim.get("simulated"))
        r = self.client.get("/api/fiber/trend/summary?limit=10", headers=XRW_HDR)
        self.assertEqual(r.status_code, 200)
        j = r.get_json()
        sns = [it["ont_sn"] for it in j["items"]]
        self.assertIn(sn_real, sns)
        self.assertNotIn(sn_sim, sns)
        item = next(it for it in j["items"] if it["ont_sn"] == sn_real)
        self.assertEqual(item["eta_level"], "Warn")
        self.assertIsNotNone(item["eta_days"])
        self.assertEqual(item["ont_id"], fid)


class TrendAlertTest(unittest.TestCase):
    """Peringatan dini poll_fiber_trend (mock send_alert)."""

    @classmethod
    def setUpClass(cls):
        _cleanup_onts([P_ALERT, P_JOB])
        cls.client = m.app.test_client()
        r = cls.client.post(
            "/login", data={"username": "admin", "password": "PantauNms2026"}
        )
        assert r.status_code == 302

    @classmethod
    def tearDownClass(cls):
        _cleanup_onts([P_ALERT, P_JOB])

    @staticmethod
    def _res(fid, sn, **over):
        res = {
            "status": "degrading",
            "reason": None,
            "slope_db_per_day": -0.3,
            "rx_now": -23.5,
            "n_days": 10,
            "window_days": 10,
            "eta_warn_days": 5.0,
            "eta_warn_min_days": 4.0,
            "eta_warn_max_days": 6.0,
            "eta_warn_range": {"min": 4.0, "max": 6.0},
            "eta_warn_at": "2026-10-01",
            "eta_warn_flag": None,
            "eta_crit_days": 11.7,
            "eta_crit_min_days": 10.0,
            "eta_crit_max_days": 13.0,
            "eta_crit_range": {"min": 10.0, "max": 13.0},
            "eta_crit_at": "2026-10-08",
            "eta_crit_flag": None,
            "confidence": "sedang",
            "simulated": False,
            "ont_id": fid,
            "ont_sn": sn,
            "customer": "Uji Tren",
            "warn": -25.0,
            "crit": -27.0,
            "source": "manual",
            "computed_at": "2026-09-25 08:00:00",
        }
        res.update(over)
        return res

    def _run(self, fid, res):
        with patch("nms.fiber_poll.send_alert") as mock_alert:
            _fmod.check_fiber_trend_alerts(results={fid: res})
            return mock_alert

    def test_alert_dikirim_untuk_manual_segaur(self):
        sn = P_ALERT + "OK"
        fid = _mk_ont(sn, rx=-23.5)
        mock = self._run(fid, self._res(fid, sn))
        self.assertEqual(mock.call_count, 1)
        args, kwargs = mock.call_args
        msg = args[0]
        self.assertIn(sn, msg)
        self.assertIn("Warn", msg)
        self.assertEqual(kwargs.get("severity"), "warning")
        self.assertEqual(kwargs.get("event"), "fiber_trend")

    def test_simulator_tidak_mengirim_alert(self):
        sn = P_ALERT + "SIM"
        fid = _mk_ont(sn, source="simulator", rx=-21.0)
        _tmod.compute_all_trends()
        res = _tmod.fiber_trend_memory.get(fid) or {}
        self.assertTrue(res.get("simulated"))
        mock = self._run(fid, self._res(fid, sn, simulated=True))
        self.assertEqual(mock.call_count, 0)

    def test_confidence_rendah_tidak_alert(self):
        sn = P_ALERT + "RENDAH"
        fid = _mk_ont(sn, rx=-23.5)
        mock = self._run(fid, self._res(fid, sn, confidence="rendah", n_days=5))
        self.assertEqual(mock.call_count, 0)

    def test_eta_di_luar_horizon_tidak_alert(self):
        sn = P_ALERT + "JAUH"
        fid = _mk_ont(sn, rx=-23.5)
        mock = self._run(
            fid, self._res(fid, sn, eta_warn_days=30.0, eta_crit_days=40.0)
        )
        self.assertEqual(mock.call_count, 0)

    def test_mute_tidak_alert(self):
        sn = P_ALERT + "MUTE"
        fid = _mk_ont(sn, rx=-23.5, mute=1, mute_until="")
        mock = self._run(fid, self._res(fid, sn))
        self.assertEqual(mock.call_count, 0)

    def test_maintenance_tidak_alert(self):
        sn = P_ALERT + "MAINT"
        fid = _mk_ont(sn, rx=-23.5)
        now = datetime.now()
        conn, c = m.get_db()
        try:
            c.execute(
                "INSERT INTO maintenance_windows (host, start_at, end_at, "
                "reason, created_by, created_at) VALUES (?,?,?,?,?,?)",
                (
                    sn,
                    (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
                    (now + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
                    "uji",
                    "test",
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            m._commit_with_retry(conn)
        finally:
            conn.close()
        try:
            mock = self._run(fid, self._res(fid, sn))
            self.assertEqual(mock.call_count, 0)
        finally:
            conn, c = m.get_db()
            try:
                c.execute("DELETE FROM maintenance_windows WHERE host=?", (sn,))
                m._commit_with_retry(conn)
            finally:
                conn.close()

    def test_stale_tidak_alert(self):
        sn = P_ALERT + "STALE"
        old = (datetime.now() - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        fid = _mk_ont(sn, source="snmp", rx=-23.5, last_seen=old)
        mock = self._run(fid, self._res(fid, sn, source="snmp"))
        self.assertEqual(mock.call_count, 0)

    def test_rx_sudah_lewat_level_tidak_alert(self):
        sn = P_ALERT + "LEWAT"
        fid = _mk_ont(sn, rx=-27.5)
        # eta masih terisi (cache lama) tapi rx_now sudah di bawah kedua level.
        mock = self._run(fid, self._res(fid, sn, rx_now=-27.5, eta_crit_days=8.0))
        self.assertEqual(mock.call_count, 0)

    def test_dedupe_7_hari_dan_reset(self):
        sn = P_ALERT + "DEDUPE"
        fid = _mk_ont(sn, rx=-23.5)
        res = self._res(fid, sn)
        mock = self._run(fid, res)
        self.assertEqual(mock.call_count, 1)
        mock = self._run(fid, res)
        self.assertEqual(mock.call_count, 0)  # dedupe 7 hari aktif
        # Status tidak lagi degrading -> kunci dedupe dibersihkan.
        self._run(fid, {**res, "status": "stable"})
        self.assertIsNone(_tmod.fiber_trend_alert_mem.get((fid, "warn")))
        self.assertIsNone(_tmod.fiber_trend_alert_mem.get((fid, "crit")))
        mock = self._run(fid, res)
        self.assertEqual(mock.call_count, 1)  # alert segar lagi

    def test_poll_job_end_to_end(self):
        sn = P_JOB + "TURUN"
        fid = _mk_ont(sn, rx=-23.5)
        _mk_history(fid, days=21, start_rx=-19.0, slope=-0.24)
        conn, c = m.get_db()
        try:
            c.execute("UPDATE settings SET value='1' WHERE key='fiber_trend_enabled'")
            m._commit_with_retry(conn)
        finally:
            conn.close()
        with patch("nms.fiber_poll.send_alert") as mock_alert:
            _fmod.poll_fiber_trend()
            res = _tmod.fiber_trend_memory.get(fid)
            self.assertIsNotNone(res)
            self.assertEqual(res["status"], "degrading")
            self.assertAlmostEqual(res["slope_db_per_day"], -0.24, delta=0.06)
            # ONT lain ber-riwayat (dibuat test job lain) boleh ikut alert.
            self.assertGreaterEqual(mock_alert.call_count, 1)
            msgs = [c[0][0] for c in mock_alert.call_args_list]
            self.assertTrue(any(sn in msg for msg in msgs))

    def test_poll_job_dimatikan_via_setting(self):
        sn = P_JOB + "OFF"
        fid = _mk_ont(sn, rx=-23.5)
        _mk_history(fid, days=21, start_rx=-19.0, slope=-0.24, seed=5)
        conn, c = m.get_db()
        try:
            c.execute("UPDATE settings SET value='0' WHERE key='fiber_trend_enabled'")
            m._commit_with_retry(conn)
        finally:
            conn.close()
        try:
            _tmod.fiber_trend_memory.set(fid, {"status": "lama"})
            with patch("nms.fiber_poll.send_alert") as mock_alert:
                _fmod.poll_fiber_trend()
                self.assertEqual(mock_alert.call_count, 0)
                # cache tidak diisi ulang saat fitur mati
                self.assertEqual(_tmod.fiber_trend_memory.get(fid), {"status": "lama"})
        finally:
            conn, c = m.get_db()
            try:
                c.execute(
                    "UPDATE settings SET value='1' WHERE key='fiber_trend_enabled'"
                )
                m._commit_with_retry(conn)
            finally:
                conn.close()


class SeedDemoScriptTest(unittest.TestCase):
    """scripts/seed_fiber_trend_demo.py: seed lalu --cleanup (subprocess DB tmp).

    Regressi bootstrap nms: tanpa sys.path bootstrap, pemanggilan persis
    seperti di docstring (python scripts/seed_fiber_trend_demo.py) gagal
    ModuleNotFoundError. DB yang dipakai bukan DB modul test ini.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile

        cls._tmpdir = tempfile.mkdtemp(prefix="nms_review_seed_demo_")
        cls._db = os.path.join(cls._tmpdir, "seed_demo.db")
        cls._repos = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    @classmethod
    def tearDownClass(cls):
        import shutil

        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def _run(self, arg=""):
        import subprocess

        env = os.environ.copy()
        env["NMS_DB_PATH"] = self._db
        env["NMS_DISABLE_SCHEDULER"] = "1"
        env["NMS_DISABLE_TRAP_RECEIVER"] = "1"
        env["TELEGRAM_BOT_TOKEN"] = ""
        env["TELEGRAM_CHAT_ID"] = ""
        cmd = [sys.executable, os.path.join("scripts", "seed_fiber_trend_demo.py")]
        if arg:
            cmd.append(arg)
        return subprocess.run(
            cmd,
            cwd=self._repos,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def _demo_count(self):
        import sqlite3

        conn = sqlite3.connect(self._db)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM fiber_onts WHERE ont_sn LIKE 'DEMO-TREND-%'"
            ).fetchone()[0]
        finally:
            conn.close()

    def test_seed_lalu_cleanup(self):
        r = self._run()
        self.assertEqual(r.returncode, 0, f"seed gagal: {r.stdout} {r.stderr}")
        self.assertEqual(self._demo_count(), 4)
        r2 = self._run(arg="--cleanup")
        self.assertEqual(r2.returncode, 0, f"cleanup gagal: {r2.stdout} {r2.stderr}")
        self.assertEqual(self._demo_count(), 0)


if __name__ == "__main__":
    unittest.main()
