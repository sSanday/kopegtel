"""nms.mikrotik_poll — polling SNMP/SSH MikroTik (background jobs)."""

import difflib
import hashlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from nms.config import HYSTERESIS
from nms.crypto import decrypt_secret
from nms.db import (
    _commit_with_retry,
    _insert_system_log,
    db_lock,
    get_db,
    get_setting,
)
from nms.format import _fmt_duration
from nms.mikrotik import (
    IF_HC_IN_OID,
    IF_HC_OUT_OID,
    IF_OPER_OID,
    SYSUP_OID,
    _mt_reboot_kind,
)
from nms.notify import send_telegram_alert
from nms.snmp import _snmp_get, _valid_oid

snmp_state = {}


def get_snmp_bandwidth(ip, community, if_index):
    try:
        if_index = int(if_index)
    except (ValueError, TypeError):
        return None, None
    if if_index < 1:
        return None, None
    vals = _snmp_get(
        ip,
        community,
        [f"1.3.6.1.2.1.31.1.1.1.6.{if_index}", f"1.3.6.1.2.1.31.1.1.1.10.{if_index}"],
    )
    if len(vals) == 2 and vals[0] is not None and vals[1] is not None:
        return vals[0], vals[1]
    vals = _snmp_get(
        ip,
        community,
        [f"1.3.6.1.2.1.2.2.1.10.{if_index}", f"1.3.6.1.2.1.2.2.1.16.{if_index}"],
    )
    if len(vals) == 2 and vals[0] is not None and vals[1] is not None:
        return vals[0], vals[1]
    return None, None


MT_DEFAULT_OIDS = {
    "cpu": "1.3.6.1.4.1.14988.1.1.3.11.0",
    "mem": "1.3.6.1.4.1.14988.1.1.3.12.0",
    "storage": "1.3.6.1.4.1.14988.1.1.3.13.0",
    "temp": "1.3.6.1.4.1.14988.1.1.3.10.0",
}


def resolve_mt_oids(host_row):
    """OID per host (override) atau default global. Kembalikan dict key->oid/None."""
    out = {}
    for key, setting_key in (
        ("cpu", "mt_cpu_oid"),
        ("mem", "mt_mem_oid"),
        ("storage", "mt_storage_oid"),
        ("temp", "mt_temp_oid"),
    ):
        try:
            custom = (host_row[key + "_oid"] or "").strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            custom = ""
        out[key] = (
            _valid_oid(custom)
            or _valid_oid(get_setting(setting_key, MT_DEFAULT_OIDS[key], type_cast=str))
            or MT_DEFAULT_OIDS[key]
        )
    return out


def get_mikrotik_health(ip, community, oids):
    """Ambil CPU/mem/storage (%) + suhu mentah via SNMP.

    Kembalikan dict {cpu, mem, storage, temp_raw} (None bila tak terjawab).
    """
    keys = [k for k in ("cpu", "mem", "storage", "temp") if oids.get(k)]
    if not keys:
        return {"cpu": None, "mem": None, "storage": None, "temp_raw": None}
    vals = _snmp_get(ip, community, [oids[k] for k in keys])
    if len(vals) != len(keys):
        vals = [None] * len(keys)
    res = {"cpu": None, "mem": None, "storage": None, "temp_raw": None}
    for k, v in zip(keys, vals):
        if v is None:
            continue
        try:
            f = float(v)
        except (ValueError, TypeError):
            continue
        if k == "temp":
            res["temp_raw"] = f
        elif 0 <= f <= 100:
            res[k] = f
    return res


def _snmp_poll_one(args):
    """Helper untuk polling SNMP satu host — dijalankan secara paralel."""
    host, community, if_index, now_time = args
    community = community or ""
    if_index = if_index or 1
    if not community or community.strip() == "":
        return host, None, None
    try:
        in_bytes, out_bytes = get_snmp_bandwidth(host, community, if_index)
        return host, in_bytes, out_bytes
    except Exception as e:
        print(f"[SNMP] poll error {host}: {e}")
        return host, None, None


def poll_snmp_bandwidth():
    """Poll bandwidth SNMP semua host secara paralel menggunakan ThreadPoolExecutor."""
    conn, c = get_db()
    try:
        c.execute("SELECT ip, snmp_community, if_index FROM hosts ORDER BY id ASC")
        hosts = c.fetchall()
        hosts = [(r["ip"], r["snmp_community"], r["if_index"]) for r in hosts]
    finally:
        conn.close()

    if not hosts:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_time = time.time()

    candidates = [(h, c_, idx, now_time) for h, c_, idx in hosts if (c_ or "").strip()]
    if not candidates:
        return

    max_workers = min(len(candidates), 20)
    raw_results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(_snmp_poll_one, args): args[0] for args in candidates
        }
        for future in as_completed(future_map):
            try:
                host, in_b, out_b = future.result()
                raw_results[host] = (in_b, out_b)
            except Exception as e:
                h = future_map.get(future, "?")
                print(f"[SNMP] future error {h}: {e}")

    pending_inserts = []
    for host, community, if_index, _ in candidates:
        in_bytes, out_bytes = raw_results.get(host, (None, None))
        if in_bytes is not None and out_bytes is not None:
            if host in snmp_state:
                prev = snmp_state[host]
                time_diff = now_time - prev["time"]

                diff_in = in_bytes - prev["in_bytes"]
                diff_out = out_bytes - prev["out_bytes"]

                if diff_in < 0 or diff_out < 0:
                    snmp_state[host] = {
                        "in_bytes": in_bytes,
                        "out_bytes": out_bytes,
                        "time": now_time,
                    }
                    continue

                if time_diff > 0:
                    net_in = (diff_in * 8) / (1024 * 1024 * time_diff)
                    net_out = (diff_out * 8) / (1024 * 1024 * time_diff)
                    if net_in > 100000 or net_out > 100000:
                        snmp_state[host] = {
                            "in_bytes": in_bytes,
                            "out_bytes": out_bytes,
                            "time": now_time,
                        }
                        continue
                else:
                    net_in, net_out = 0.0, 0.0

                pending_inserts.append(
                    (
                        host,
                        None,
                        None,
                        None,
                        round(net_in, 2),
                        round(net_out, 2),
                        timestamp,
                        "snmp",
                    )
                )

            snmp_state[host] = {
                "in_bytes": in_bytes,
                "out_bytes": out_bytes,
                "time": now_time,
            }

    if not pending_inserts:
        return
    with db_lock:
        conn, c = get_db()
        try:
            c.executemany(
                "INSERT INTO agent_metrics (host, cpu_percent, ram_percent, disk_percent, net_in, net_out, timestamp, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                pending_inserts,
            )
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] poll_snmp_bandwidth gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()


MT_TEMP_HYST = 2.0

mt_alarm_memory = {}
mt_is_mikrotik = {}
mt_sysup = {}
mt_iface_oper = {}
iface_state = {}
mt_iface_tg = {}
mt_iface_flaps = {}
MT_IFACE_TG_COOLDOWN_S = 600


def _mt_check_one(args):
    host, community, oids = args
    try:
        res = get_mikrotik_health(host, community, oids)
    except Exception as e:
        print(f"[MT] {host}: {e}")
        res = {"cpu": None, "mem": None, "storage": None, "temp_raw": None}
    sysup = None
    try:
        vals = _snmp_get(host, community, [SYSUP_OID], timeout=2.0)
        if vals and vals[0] is not None and vals[0] >= 0:
            sysup = round(vals[0] / 100.0, 1)
    except Exception as e:
        print(f"[MT] {host} sysup: {e}")
    return host, res, sysup


def poll_mikrotik_health():
    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT ip, snmp_community, snmp_profile, cpu_oid, mem_oid,"
                " storage_oid, temp_oid FROM hosts ORDER BY id ASC"
            )
            hosts = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f"[MT] load hosts gagal: {e}")
        return
    cands = []
    for h in hosts:
        if not (h.get("snmp_community") or "").strip():
            continue
        if (h.get("snmp_profile") or "auto") == "generic":
            mt_is_mikrotik.pop(h["ip"], None)
            continue
        cands.append((h["ip"], h["snmp_community"], resolve_mt_oids(h)))
    if not cands:
        return

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(cands), 10)) as ex:
        for host, res, sysup in ex.map(_mt_check_one, cands):
            results[host] = (res, sysup)

    try:
        temp_warn = get_setting("temp_threshold", 60.0)
        temp_crit = get_setting("temp_crit", 75.0)
        cpu_thresh = get_setting("cpu_threshold", 85.0)
        mem_thresh = get_setting("ram_threshold", 90.0)
        st_thresh = get_setting("disk_threshold", 90.0)
        temp_div = get_setting("mt_temp_div", 10, type_cast=float) or 10
    except Exception:
        temp_warn, temp_crit, cpu_thresh, mem_thresh, st_thresh, temp_div = (
            60.0,
            75.0,
            85.0,
            90.0,
            90.0,
            10,
        )
    cpu_clear = max(cpu_thresh - HYSTERESIS, 0)
    mem_clear = max(mem_thresh - HYSTERESIS, 0)
    st_clear = max(st_thresh - HYSTERESIS, 0)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tg_queue = []
    with db_lock:
        conn, c = get_db()
        try:
            for host, (res, sysup) in results.items():
                cpu, mem, sto = res.get("cpu"), res.get("mem"), res.get("storage")
                temp = (
                    res["temp_raw"] / temp_div
                    if res.get("temp_raw") is not None and temp_div
                    else None
                )
                if temp is not None:
                    temp = round(temp, 1)
                if cpu is None and mem is None and sto is None and temp is None:
                    mt_is_mikrotik.pop(host, None)
                    continue
                mt_is_mikrotik[host] = True
                prev_up = mt_sysup.get(host)
                if sysup is not None:
                    _kind = (
                        _mt_reboot_kind(prev_up, sysup) if prev_up is not None else None
                    )
                    if _kind:
                        if _kind == "wrap":
                            tg_queue.append(
                                f"ℹ️ *MIKROTIK UPTIME WRAP?*\nHost: `{host}`\n"
                                f"Uptime sebelumnya: {_fmt_duration(int(prev_up or 0))} → sekarang: {_fmt_duration(int(sysup or 0))}\n"
                                f"Kemungkinan wrap counter TimeTicks (>467 hari), bukan reboot beneran — verifikasi uptime.\n"
                                f"Waktu: {timestamp}"
                            )
                            try:
                                _insert_system_log(
                                    c,
                                    "MT_REBOOT",
                                    host,
                                    f"kemungkinan wrap (uptime {int(prev_up or 0)}s -> {int(sysup or 0)}s)",
                                    timestamp,
                                )
                            except Exception:
                                pass
                        else:
                            tg_queue.append(
                                f"🔄 *MIKROTIK REBOOT TERDETEKSI*\nHost: `{host}`\n"
                                f"Uptime sebelumnya: {_fmt_duration(int(prev_up or 0))} → sekarang: {_fmt_duration(int(sysup or 0))}\n"
                                f"Waktu: {timestamp}"
                            )
                            try:
                                _insert_system_log(
                                    c,
                                    "MT_REBOOT",
                                    host,
                                    f"reboot (uptime {int(prev_up or 0)}s -> {int(sysup or 0)}s)",
                                    timestamp,
                                )
                            except Exception:
                                pass
                    mt_sysup[host] = sysup
                try:
                    c.execute(
                        "INSERT INTO device_health (host, cpu, mem_used, storage_used,"
                        " temp_c, uptime_s, timestamp, source) VALUES (?,?,?,?,?,?,?,'snmp-mikrotik')",
                        (host, cpu, mem, sto, temp, sysup, timestamp),
                    )
                except sqlite3.OperationalError as e:
                    print(f"[DB LOCK] device_health {host} gagal: {e}")
                    continue

                prev = mt_alarm_memory.get(host) or {}
                cur = dict(prev)

                def _flip(key, is_bad, is_clear, name, unit, val, thresh, sev):
                    if val is None:
                        return
                    if is_bad and not prev.get(key):
                        cur[key] = True
                        tg_queue.append(
                            f"⚠️ *MIKROTIK {name} TINGGI*\nHost: `{host}`\n"
                            f"{name}: *{val}{unit}* (batas: {thresh}{unit})\nWaktu: {timestamp}"
                        )
                        log_queue.append(
                            (f"MT_{key.upper()}", f"{val}{unit} (batas {thresh}{unit})")
                        )
                    elif is_clear and prev.get(key):
                        cur[key] = False
                        tg_queue.append(
                            f"✅ *MIKROTIK {name} NORMAL*\nHost: `{host}`\n"
                            f"{name}: {val}{unit}\nWaktu: {timestamp}"
                        )
                        log_queue.append(
                            (f"MT_{key.upper()}_OK", f"pulih: {val}{unit}")
                        )

                log_queue = []
                _flip(
                    "cpu",
                    cpu is not None and cpu > cpu_thresh,
                    cpu is not None and cpu <= cpu_clear,
                    "CPU",
                    "%",
                    cpu,
                    cpu_thresh,
                    "warning",
                )
                _flip(
                    "mem",
                    mem is not None and mem > mem_thresh,
                    mem is not None and mem <= mem_clear,
                    "Memory",
                    "%",
                    mem,
                    mem_thresh,
                    "warning",
                )
                _flip(
                    "storage",
                    sto is not None and sto > st_thresh,
                    sto is not None and sto <= st_clear,
                    "Storage",
                    "%",
                    sto,
                    st_thresh,
                    "warning",
                )
                _flip(
                    "temp_warn",
                    temp is not None and temp >= temp_warn,
                    temp is not None and temp <= temp_warn - MT_TEMP_HYST,
                    "Suhu",
                    "°C",
                    temp,
                    temp_warn,
                    "warning",
                )
                _flip(
                    "temp_crit",
                    temp is not None and temp >= temp_crit,
                    temp is not None and temp <= temp_crit - MT_TEMP_HYST,
                    "Suhu KRITIS",
                    "°C",
                    temp,
                    temp_crit,
                    "high",
                )
                mt_alarm_memory[host] = cur
                for ev, msg in log_queue:
                    try:
                        _insert_system_log(c, ev, host, msg, timestamp)
                    except Exception:
                        pass
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] poll_mikrotik gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
    for msg in tg_queue:
        try:
            send_telegram_alert(msg)
        except Exception as e:
            print(f"[WARN] telegram mikrotik gagal: {e}")


def _mt_iface_check_one(args):
    """Poll satu host: oper-status + HC counters semua interface termonitor."""
    host, community, indices = args
    out = {"opers": {}, "counters": {}}
    try:
        if indices:
            vals = _snmp_get(
                host, community, [f"{IF_OPER_OID}.{i}" for i in indices], timeout=3.0
            )
            for i, v in zip(indices, vals):
                out["opers"][i] = v
            oids, order = [], []
            for i in indices:
                oids += [f"{IF_HC_IN_OID}.{i}", f"{IF_HC_OUT_OID}.{i}"]
                order += [(i, "in"), (i, "out")]
            vals = _snmp_get(host, community, oids, timeout=3.0)
            if len(vals) == len(order):
                for (i, direction), v in zip(order, vals):
                    out["counters"].setdefault(i, {})[direction] = v
    except Exception as e:
        print(f"[MT-IFACE] {host}: {e}")
    return host, out


def poll_mikrotik_ifaces():
    """Traffic + oper-status per interface termonitor (tiap 60 dtk)."""
    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT h.ip, h.snmp_community, h.snmp_profile,"
                " i.if_index, i.name FROM snmp_interfaces i"
                " JOIN hosts h ON h.ip = i.host"
                " WHERE i.monitor=1 ORDER BY h.ip ASC, i.if_index ASC"
            )
            rows = [dict(r) for r in c.fetchall()]
        finally:
            conn.close()
    except Exception as e:
        print(f"[MT-IFACE] load gagal: {e}")
        return
    by_host = {}
    for r in rows:
        if not (r.get("snmp_community") or "").strip():
            continue
        if (r.get("snmp_profile") or "auto") == "generic":
            continue
        try:
            idx = int(r["if_index"])
        except (ValueError, TypeError):
            continue
        by_host.setdefault(
            r["ip"], {"community": r["snmp_community"], "indices": [], "names": {}}
        )
        by_host[r["ip"]]["indices"].append(idx)
        by_host[r["ip"]]["names"][idx] = r["name"] or f"if{idx}"
    if not by_host:
        return

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(by_host), 10)) as ex:
        futs = {
            ex.submit(_mt_iface_check_one, (h, v["community"], v["indices"])): h
            for h, v in by_host.items()
        }
        for fut in as_completed(futs):
            try:
                host, out = fut.result()
                results[host] = out
            except Exception as e:
                print(f"[MT-IFACE] {futs.get(fut)} gagal: {e}")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_time = time.time()
    tg_queue, traffic_rows = [], []
    with db_lock:
        conn, c = get_db()
        try:
            for host, out in results.items():
                names = by_host[host]["names"]
                for idx in by_host[host]["indices"]:
                    name = names.get(idx, f"if{idx}")
                    oper = out["opers"].get(idx)
                    key = (host, idx)
                    prev_oper = mt_iface_oper.get(key)
                    if oper in (1, 2):
                        if prev_oper is None:
                            mt_iface_oper[key] = oper
                        elif oper != prev_oper:
                            mt_iface_oper[key] = oper
                            try:
                                c.execute(
                                    "UPDATE snmp_interfaces SET oper=?, last_changed=?"
                                    " WHERE host=? AND if_index=?",
                                    (oper, timestamp, host, idx),
                                )
                            except sqlite3.OperationalError:
                                pass
                            _last_tg = mt_iface_tg.get(key, 0)
                            if now_time - _last_tg < MT_IFACE_TG_COOLDOWN_S:
                                _n = mt_iface_flaps.get(key, 0) + 1
                                mt_iface_flaps[key] = _n
                                print(
                                    f"[MT-IFACE] {host} if{idx} "
                                    f"{'down' if oper == 2 else 'up'} disuppress "
                                    f"(cooldown, flap x{_n})"
                                )
                                try:
                                    _insert_system_log(
                                        c,
                                        "MT_PORT_DOWN" if oper == 2 else "MT_PORT_UP",
                                        host,
                                        f"{name} (ifIndex {idx}) "
                                        f"{'down' if oper == 2 else 'up'} (telegram disuppress, flap x{_n})",
                                        timestamp,
                                    )
                                except Exception:
                                    pass
                            else:
                                mt_iface_tg[key] = now_time
                                _flaps = mt_iface_flaps.pop(key, 0)
                                _note = (
                                    f" (flapping {_flaps}x/10 mnt)" if _flaps else ""
                                )
                                if oper == 2:
                                    tg_queue.append(
                                        f"🔌 *MIKROTIK PORT DOWN*\nHost: `{host}`\n"
                                        f"Port: *{name}* (ifIndex {idx}){_note}\nWaktu: {timestamp}"
                                    )
                                    try:
                                        _insert_system_log(
                                            c,
                                            "MT_PORT_DOWN",
                                            host,
                                            f"{name} (ifIndex {idx}) down{_note}",
                                            timestamp,
                                        )
                                    except Exception:
                                        pass
                                else:
                                    tg_queue.append(
                                        f"✅ *MIKROTIK PORT UP*\nHost: `{host}`\n"
                                        f"Port: *{name}* (ifIndex {idx}){_note}\nWaktu: {timestamp}"
                                    )
                                    try:
                                        _insert_system_log(
                                            c,
                                            "MT_PORT_UP",
                                            host,
                                            f"{name} (ifIndex {idx}) up{_note}",
                                            timestamp,
                                        )
                                    except Exception:
                                        pass
                    cnt = out["counters"].get(idx, {})
                    in_b, out_b = cnt.get("in"), cnt.get("out")
                    if in_b is not None and out_b is not None:
                        st = iface_state.get(key)
                        if st is None:
                            iface_state[key] = {
                                "in": in_b,
                                "out": out_b,
                                "time": now_time,
                            }
                        else:
                            dt = now_time - st["time"]
                            di, do = in_b - st["in"], out_b - st["out"]
                            if di < 0 or do < 0 or dt <= 0:
                                iface_state[key] = {
                                    "in": in_b,
                                    "out": out_b,
                                    "time": now_time,
                                }
                            else:
                                net_in = round((di * 8) / (1024 * 1024 * dt), 2)
                                net_out = round((do * 8) / (1024 * 1024 * dt), 2)
                                if net_in <= 100000 and net_out <= 100000:
                                    traffic_rows.append(
                                        (host, idx, net_in, net_out, timestamp)
                                    )
                                iface_state[key] = {
                                    "in": in_b,
                                    "out": out_b,
                                    "time": now_time,
                                }
            if traffic_rows:
                try:
                    c.executemany(
                        "INSERT INTO iface_traffic (host, if_index, net_in, net_out, timestamp)"
                        " VALUES (?,?,?,?,?)",
                        traffic_rows,
                    )
                except sqlite3.OperationalError as e:
                    print(f"[DB LOCK] iface_traffic gagal: {e}")
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] poll_mikrotik_ifaces gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
    for msg in tg_queue:
        try:
            send_telegram_alert(msg)
        except Exception as e:
            print(f"[WARN] telegram mikrotik-iface gagal: {e}")


MT_BACKUP_MAX_BYTES = 2 * 1024 * 1024


def _mt_backup_keep():
    try:
        k = int(float(get_setting("mt_backup_keep", 10)))
    except (ValueError, TypeError):
        k = 10
    return min(50, max(3, k))


def fetch_mikrotik_config(host, username, password, port=22, timeout=20):
    """Ambil /export via SSH. Kembalikan (ok, text_atau_error)."""
    try:
        import paramiko
    except ImportError:
        return False, "paramiko belum terinstal di server"
    if not (username or "").strip() or not (password or ""):
        return False, "SSH user/password belum diisi"
    try:
        port = int(port or 22)
    except (ValueError, TypeError):
        return False, "SSH port tidak valid"
    if not 1 <= port <= 65535:
        return False, "SSH port harus 1-65535"
    client = None
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            host,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
            banner_timeout=timeout,
            auth_timeout=timeout,
            look_for_keys=False,
            allow_agent=False,
        )
        _in, _out, _err = client.exec_command("/export", timeout=timeout)
        raw = _out.read()
        try:
            text = raw.decode("utf-8-sig").strip()
        except Exception:
            text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return False, "export kosong"
        return True, text
    except Exception as e:
        return False, f"SSH gagal: {e}"[:300]
    finally:
        try:
            if client is not None:
                client.close()
        except Exception:
            pass


def _store_mt_backup(c, host, ok, payload, timestamp):
    """Simpan hasil fetch (cursor milik transaksi caller yang pegang db_lock).

    Kembalikan pesan telegram|None. Baseline pertama & kegagalan tak
    bertelegram (kegagalan tampil di triggers + status host).
    """
    if not ok:
        try:
            c.execute(
                "UPDATE hosts SET backup_last=?, backup_ok=0 WHERE ip=?",
                (timestamp, host),
            )
            _insert_system_log(c, "MT_BACKUP_FAIL", host, str(payload)[:200], timestamp)
        except sqlite3.OperationalError:
            pass
        return None
    text = payload if isinstance(payload, str) else ""
    if len(text.encode("utf-8")) > MT_BACKUP_MAX_BYTES:
        try:
            c.execute(
                "UPDATE hosts SET backup_last=?, backup_ok=0 WHERE ip=?",
                (timestamp, host),
            )
            _insert_system_log(
                c, "MT_BACKUP_FAIL", host, "export >2MB, dilewati", timestamp
            )
        except sqlite3.OperationalError:
            pass
        return None
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    try:
        c.execute(
            "SELECT id, sha256, content FROM mt_backups WHERE host=? "
            "ORDER BY id DESC LIMIT 1",
            (host,),
        )
        prev = c.fetchone()
    except sqlite3.OperationalError:
        return None
    if prev and prev["sha256"] == sha:
        try:
            c.execute(
                "UPDATE hosts SET backup_last=?, backup_ok=1 WHERE ip=?",
                (timestamp, host),
            )
        except sqlite3.OperationalError:
            pass
        return None
    added = removed = 0
    if prev and prev["content"] is not None:
        for line in difflib.unified_diff(
            (prev["content"] or "").splitlines(), text.splitlines(), n=0
        ):
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
    try:
        c.execute(
            "INSERT INTO mt_backups (host, taken_at, size, sha256, content, changed)"
            " VALUES (?,?,?,?,?,?)",
            (host, timestamp, len(text.encode("utf-8")), sha, text, 1 if prev else 0),
        )
        keep = _mt_backup_keep()
        c.execute(
            "DELETE FROM mt_backups WHERE host=? AND id NOT IN "
            "(SELECT id FROM mt_backups WHERE host=? ORDER BY id DESC LIMIT ?)",
            (host, host, keep),
        )
        c.execute(
            "UPDATE hosts SET backup_last=?, backup_ok=1 WHERE ip=?", (timestamp, host)
        )
        _insert_system_log(
            c,
            "MT_BACKUP",
            host,
            f"tersimpan {len(text.encode('utf-8')) // 1024} KB"
            + (f" (+{added}/-{removed})" if prev else " (baseline)"),
            timestamp,
        )
    except sqlite3.OperationalError as e:
        print(f"[MT-BACKUP] simpan {host} gagal: {e}")
        return None
    if not prev:
        return None
    return (
        f"💾 *MIKROTIK BACKUP BERUBAH*\nHost: `{host}`\n"
        f"Ukuran: {len(text.encode('utf-8')) // 1024} KB · +{added}/-{removed} baris\n"
        f"Waktu: {timestamp}\nLihat diff di halaman mikrotik."
    )


def _mt_backup_candidates():
    try:
        conn, c = get_db()
        try:
            c.execute(
                "SELECT ip, ssh_user, ssh_pass, ssh_port FROM hosts "
                "WHERE backup_enable=1"
            )
            rows = [dict(r) for r in c.fetchall()]
            for h in rows:
                h["ssh_pass"] = decrypt_secret(h.get("ssh_pass") or "")
            return rows
        finally:
            conn.close()
    except Exception as e:
        print(f"[MT-BACKUP] load kandidat gagal: {e}")
        return []


def poll_mt_backups():
    """Job scheduler: backup konfigurasi semua host opt-in (tiap 02:00)."""
    cands = _mt_backup_candidates()
    if not cands:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _one(h):
        try:
            return h["ip"], fetch_mikrotik_config(
                h["ip"],
                h.get("ssh_user") or "",
                h.get("ssh_pass") or "",
                h.get("ssh_port") or 22,
            )
        except Exception as e:
            return h["ip"], (False, f"fetch gagal: {e}"[:200])

    results = {}
    with ThreadPoolExecutor(max_workers=min(len(cands), 5)) as ex:
        for host, res in ex.map(_one, cands):
            results[host] = res
    tg_queue = []
    with db_lock:
        conn, c = get_db()
        try:
            for host, (ok, payload) in results.items():
                try:
                    msg = _store_mt_backup(c, host, ok, payload, timestamp)
                except Exception as e:
                    print(f"[MT-BACKUP] {host} gagal: {e}")
                    continue
                if msg:
                    tg_queue.append(msg)
            _commit_with_retry(conn)
        except sqlite3.OperationalError as e:
            print(f"[DB LOCK] poll_mt_backups gagal: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()
    for msg in tg_queue:
        try:
            send_telegram_alert(msg)
        except Exception as e:
            print(f"[WARN] telegram backup gagal: {e}")
