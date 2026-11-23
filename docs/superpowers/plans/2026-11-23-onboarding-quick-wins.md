# Onboarding & Quick Wins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pengguna baru bisa mencoba fitur NMS Kopegtel dalam 2 menit pertama: tabel tidak kosong, sesi stabil, empty state membimbing, tooltip field wajib, dan hasil uji koneksi jelas.

**Architecture:** Approach 1 (Patchwork cepat). Seed dummy device di backend bootstrap; perpanjang idle timeout default; auto-refresh token di frontend; empty state + tooltip + warna hasil uji di HTML/JS per halaman.

**Tech Stack:** Python 3.13 + Flask + SQLite (backend), Vanilla JS ES6+ (frontend), pytest (test).

**Spec:** `docs/superpowers/specs/2026-11-23-onboarding-quick-wins-design.md`

## Global Constraints

- Vanilla JS, tanpa framework eksternal.
- Idle timeout: 5–1440 menit, default 120.
- Seed device hanya dibuat jika `COUNT(*) FROM devices = 0`.
- Auto-refresh token: tiap 10 menit, hanya saat tab visible.
- Empty state: pola dashboard (judul + deskripsi + tombol "Tambah").
- Tooltip: `title` + `aria-describedby`.
- Hasil uji koneksi: hijau (`ok: true`) / merah (`ok: false`).

## Review Focus

- Seed device tidak boleh dibuat jika sudah ada data (idempotent).
- Auto-refresh token tidak boleh berjalan saat tab hidden (hemat resource).
- Empty state tidak boleh menyembunyikan data yang sudah ada.
- Tooltip tidak boleh mengganggu pengguna (hanya `title` + `aria-describedby`).
- Hasil uji koneksi harus jelas per metode (ping, SNMP, MikroTik).

---

### Task 1: Seed dummy device (backend)

**Files:**
- Modify: `backend/app.py` (bootstrap)
- Create: `tests/test_onboarding.py`

**Interfaces:**
- Consumes: `db.create_device(data)` dari `backend/db/devices.py`.
- Produces: 1 perangkat "contoh-router" saat tabel devices kosong.

- [ ] **Step 1: Write the failing test**

```python
"""Test onboarding: seed dummy device dan idle timeout."""
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

import pytest


def test_seed_dummy_device_when_empty(fresh_db):
    import db
    import app as app_module
    # bootstrap sudah dipanggil di conftest; pastikan seed jalan
    app_module.bootstrap()
    devices = db.get_devices(per_page=100)
    names = [d["name"] for d in devices.get("devices", [])]
    assert "contoh-router" in names, "Seed device 'contoh-router' harus ada saat devices kosong"


def test_no_seed_when_devices_exist(fresh_db):
    import db
    import app as app_module
    # Buat satu device dulu
    db.create_device({
        "name": "existing", "ip": "192.168.1.1", "type": "router",
        "location": "", "poll_interval": 60, "maintenance": False,
        "snmp_enabled": False, "snmp_version": "v2c", "snmp_port": 161,
        "snmp_community": None, "snmp_v3_user": None,
        "snmp_v3_auth_proto": "SHA", "snmp_v3_auth_password": None,
        "snmp_v3_priv_proto": "AES", "snmp_v3_priv_password": None,
        "mikrotik_enabled": False, "mikrotik_port": 8728,
        "mikrotik_user": None, "mikrotik_password": None,
        "mikrotik_ssl": False, "mikrotik_ssl_fingerprint": None,
        "group_id": None, "parent_id": None,
    })
    count_before = len(db.get_devices(per_page=100).get("devices", []))
    app_module.bootstrap()
    count_after = len(db.get_devices(per_page=100).get("devices", []))
    assert count_after == count_before, "Seed tidak boleh ditambahkan jika sudah ada device"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_onboarding.py -v`
Expected: FAIL (seed not implemented yet)

- [ ] **Step 3: Implement seed dummy device in `backend/app.py`**

Tambahkan fungsi `_seed_dummy_device()` dan panggil di `bootstrap()`:

```python
def _seed_dummy_device():
    try:
        devices = db.get_devices(per_page=1)
        if devices.get("total", 0) > 0:
            return
    except Exception:
        return
    try:
        db.create_device({
            "name": "contoh-router", "ip": "192.168.30.99", "type": "router",
            "location": "Contoh (bisa dihapus)", "poll_interval": 60, "maintenance": False,
            "snmp_enabled": False, "snmp_version": "v2c", "snmp_port": 161,
            "snmp_community": None, "snmp_v3_user": None,
            "snmp_v3_auth_proto": "SHA", "snmp_v3_auth_password": None,
            "snmp_v3_priv_proto": "AES", "snmp_v3_priv_password": None,
            "mikrotik_enabled": False, "mikrotik_port": 8728,
            "mikrotik_user": None, "mikrotik_password": None,
            "mikrotik_ssl": False, "mikrotik_ssl_fingerprint": None,
            "group_id": None, "parent_id": None,
        })
        log.info("Perangkat contoh 'contoh-router' dibuat (bisa dihapus).")
    except Exception as exc:
        log.warning("Gagal membuat perangkat contoh: %s", exc)
```

Dan di `bootstrap()`:

```python
def bootstrap():
    db.init_db()
    _seed_admin()
    _seed_dummy_device()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_onboarding.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app.py tests/test_onboarding.py
git commit -m "feat(onboarding): seed dummy device saat pertama kali"
```

---

### Task 2: Idle timeout default 30 → 120 menit

**Files:**
- Modify: `backend/db/settings.py:233`
- Modify: `tests/test_onboarding.py` (tambah test)

**Interfaces:**
- Consumes: `db.get_setting("idle_timeout_minutes", "30")`.
- Produces: default 120.

- [ ] **Step 1: Write the failing test**

Tambahkan ke `tests/test_onboarding.py`:

```python
def test_idle_timeout_default_120(fresh_db):
    import db
    value = db.get_setting("idle_timeout_minutes", "30")
    assert value == "120", f"Default idle timeout harus 120 menit, dapat {value}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_onboarding.py::test_idle_timeout_default_120 -v`
Expected: FAIL (value is "30")

- [ ] **Step 3: Change default in `backend/db/settings.py`**

```python
"idle_timeout_minutes": "120",
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_onboarding.py::test_idle_timeout_default_120 -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/db/settings.py tests/test_onboarding.py
git commit -m "config(auth): perpanjang idle timeout default ke 120 menit"
```

---

### Task 3: Auto-refresh token (frontend)

**Files:**
- Modify: `assets/js/api.js` (tambah auto-refresh)

**Interfaces:**
- Consumes: `window.apiFetch("/api/me")`, `window.rememberCsrf`.
- Produces: sesi diperpanjang tiap 10 menit saat tab visible.

- [ ] **Step 1: Implement auto-refresh in `assets/js/api.js`**

Tambahkan setelah `window.apiFetch = apiFetch;`:

```javascript
// Auto-refresh token: perpanjang sesi tiap 10 menit selama tab terlihat.
(function autoRefresh() {
  const INTERVAL_MS = 10 * 60 * 1000;
  setInterval(() => {
    if (document.visibilityState !== "visible") return;
    window.apiFetch("/api/me")
      .then((me) => { if (me && me.csrf_token) window.rememberCsrf(me.csrf_token); })
      .catch(() => { /* abaikan; sesi mungkin kadaluarsa */ });
  }, INTERVAL_MS);
})();
```

- [ ] **Step 2: Manual verify**

Buka browser, login, biarkan 10 menit, cek Network tab untuk request `/api/me` setiap 10 menit. Sesi tidak kadaluarsa.

- [ ] **Step 3: Commit**

```bash
git add assets/js/api.js
git commit -m "feat(auth): auto-refresh token tiap 10 menit saat tab terlihat"
```

---

### Task 4: Empty state + tombol "Tambah" di 6 halaman

**Files:**
- Modify: `inventory.html`, `services.html`, `fiber.html`, `traffic.html`, `events.html`, `mikrotik.html`

**Interfaces:**
- Consumes: pola empty state dari `dashboard.html` (judul + deskripsi + tombol).
- Produces: empty state yang membimbing pengguna.

- [ ] **Step 1: Tambah empty state markup di setiap halaman**

Pola (contoh inventory):

```html
<div class="empty-state" id="inventory-empty" hidden>
  <p class="empty-state__title">Belum ada perangkat</p>
  <p class="empty-state__desc">Tambahkan perangkat pertama Anda untuk mulai memantau jaringan.</p>
  <div class="empty-state__actions">
    <button class="btn btn--primary" id="inv-add-first" type="button">Tambah perangkat pertama</button>
  </div>
</div>
```

Tambahkan di setiap halaman yang relevan, dengan teks dan tombol yang sesuai.

- [ ] **Step 2: Tambah logika show/hide di JS per halaman**

Di setiap file JS (`inventory.js`, `services.js`, `fiber.js`, `traffic.js`, `events.js`, `mikrotik.js`), tambahkan:

```javascript
const emptyState = document.getElementById("...-empty");
const grid = document.getElementById("...-grid"); // atau tabel
// setelah load data:
if (total === 0) {
  emptyState.hidden = false;
  grid.hidden = true;
} else {
  emptyState.hidden = true;
  grid.hidden = false;
}
```

- [ ] **Step 3: Manual verify**

Buka setiap halaman dengan data kosong, pastikan empty state muncul dengan tombol "Tambah". Klik tombol, pastikan form/dialog terbuka.

- [ ] **Step 4: Commit**

```bash
git add inventory.html services.html fiber.html traffic.html events.html mikrotik.html assets/js/inventory.js assets/js/services.js assets/js/fiber.js assets/js/traffic.js assets/js/events.js assets/js/mikrotik.js
git commit -m "feat(ux): empty state + tombol Tambah di 6 halaman"
```

---

### Task 5: Tooltip di field wajib

**Files:**
- Modify: `assets/js/device-form.js`
- Modify: `assets/js/services.js` (form layanan)
- Modify: `assets/js/fiber.js` (form link)

**Interfaces:**
- Consumes: field wajib (nama, IP, interval, community, user/password RouterOS).
- Produces: tooltip via `title` + `aria-describedby`.

- [ ] **Step 1: Tambah tooltip di device-form.js**

Tambahkan atribut `title` dan `aria-describedby` ke field wajib:

```javascript
// Contoh untuk nama:
document.getElementById("df-name").setAttribute("title", "Nama unik perangkat (mis. core-sw-01)");
document.getElementById("df-name").setAttribute("aria-describedby", "df-name-help");
// Tambah elemen help:
const help = document.createElement("span");
help.id = "df-name-help";
help.className = "field-help";
help.textContent = "Nama unik perangkat (mis. core-sw-01)";
document.getElementById("df-name").parentElement.appendChild(help);
```

Ulangi untuk IP, interval, community, user/password RouterOS.

- [ ] **Step 2: Tambah tooltip di services.js dan fiber.js**

Tambahkan `title` + `aria-describedby` ke field wajib di form layanan dan link fiber.

- [ ] **Step 3: Manual verify**

Hover/focus field wajib, pastikan tooltip muncul. Screen reader membaca deskripsi.

- [ ] **Step 4: Commit**

```bash
git add assets/js/device-form.js assets/js/services.js assets/js/fiber.js
git commit -m "feat(a11y): tooltip di field wajib"
```

---

### Task 6: Perbaiki tampilan hasil "Uji koneksi"

**Files:**
- Modify: `assets/js/device-form.js` (fungsi `runTest`)

**Interfaces:**
- Consumes: `result` dari `/api/devices/test`.
- Produces: list dengan warna hijau (ok) / merah (gagal) per metode.

- [ ] **Step 1: Tambah warna di runTest**

Ubah `runTest` agar menambahkan class `test-results__item--ok` / `--down` (sudah ada) dan pastikan CSS-nya berwarna hijau/merah:

```css
.test-results__item--ok { color: var(--ok); }
.test-results__item--down { color: var(--danger); }
```

Pastikan CSS `.test-results__item--ok` dan `--down` sudah ada di dashboard.css. Jika belum, tambahkan.

- [ ] **Step 2: Manual verify**

Klik "Uji koneksi" dengan perangkat yang bisa dijangkau → hijau. Dengan perangkat yang tidak bisa dijangkau → merah.

- [ ] **Step 3: Commit**

```bash
git add assets/js/device-form.js assets/css/dashboard.css
git commit -m "feat(ux): warna hasil uji koneksi (hijau/merah)"
```

---

## Self-Review

1. **Spec coverage**: Semua 5 quick wins (G1–G6) tercakup di Task 1–6.
2. **Step scan**: Setiap step memiliki perintah dan expected output.
3. **Type consistency**: `db.create_device(data)` dipakai konsisten; `window.apiFetch` konsisten.
4. **Review Focus**: 5 input classes/failure modes tercakup di test dan manual verify.
5. **Proportion**: Plan ini ~150 baris, proporsional dengan scope.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-11-23-onboarding-quick-wins.md`. Please review the plan. Does it capture what you want?<|end|><|start|>write to=assistant<|channel|>analysis<|message|>Created file successfully: docs/superpowers/plans/2026-11-23-onboarding-quick-wins.md