CREATE TABLE ping_logs (
        id          SERIAL PRIMARY KEY,
        host        TEXT    NOT NULL,
        latency     REAL    NOT NULL,
        packet_loss REAL    NOT NULL DEFAULT 0,
        timestamp   TEXT    NOT NULL
    );
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
CREATE TABLE down_events (
        id          SERIAL PRIMARY KEY,
        host        TEXT    NOT NULL,
        started_at  TEXT    NOT NULL,
        resolved_at TEXT,
        duration_s  INTEGER
    , is_maintenance INTEGER NOT NULL DEFAULT 0);
CREATE TABLE hosts (
        id SERIAL PRIMARY KEY,
        ip TEXT UNIQUE NOT NULL
    , snmp_community TEXT DEFAULT '', if_index INTEGER DEFAULT 1, alias TEXT DEFAULT '', category TEXT DEFAULT 'Uncategorized', snmp_profile TEXT DEFAULT 'auto', cpu_oid TEXT DEFAULT '', mem_oid TEXT DEFAULT '', storage_oid TEXT DEFAULT '', temp_oid TEXT DEFAULT '', ssh_user TEXT DEFAULT '', ssh_pass TEXT DEFAULT '', ssh_port INTEGER DEFAULT 22, backup_enable INTEGER NOT NULL DEFAULT 0, backup_last TEXT DEFAULT '', backup_ok INTEGER NOT NULL DEFAULT 0);
CREATE TABLE agent_metrics (
        id SERIAL PRIMARY KEY,
        host TEXT NOT NULL,
        cpu_percent REAL,
        ram_percent REAL,
        disk_percent REAL DEFAULT 0.0,
        net_in REAL DEFAULT 0.0,
        net_out REAL DEFAULT 0.0,
        timestamp TEXT NOT NULL
    , source TEXT DEFAULT 'agent');
CREATE TABLE services (
        id SERIAL PRIMARY KEY,
        ip TEXT NOT NULL,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        port INTEGER,
        url TEXT,
        status TEXT DEFAULT 'PENDING',
        latency REAL,
        last_checked TEXT
    , ssl_expires_at TEXT, ssl_days_left INTEGER, ssl_last_alert TEXT DEFAULT '', ssl_checked_at TEXT);
CREATE TABLE system_logs (
        id SERIAL PRIMARY KEY,
        timestamp TEXT NOT NULL,
        event_type TEXT NOT NULL,
        host TEXT NOT NULL,
        message TEXT NOT NULL
    );
CREATE INDEX idx_ping_host_clock ON ping_logs(host, timestamp);
CREATE INDEX idx_agent_host_clock ON agent_metrics(host, timestamp);
CREATE INDEX idx_down_host ON down_events(host);
CREATE INDEX idx_syslog_type_host ON system_logs(event_type, host);
CREATE TABLE inventory(
      id SERIAL PRIMARY KEY,
      hostname TEXT NOT NULL, ip TEXT UNIQUE NOT NULL,
      device_type TEXT DEFAULT '', brand_model TEXT DEFAULT '',
      location TEXT DEFAULT '', pic_name TEXT DEFAULT '', pic_phone TEXT DEFAULT '',
      install_date TEXT DEFAULT '', asset_status TEXT DEFAULT 'aktif',
      asset_no TEXT DEFAULT '', notes TEXT DEFAULT '',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
CREATE INDEX idx_inventory_ip ON inventory(ip);
CREATE TABLE maintenance_windows(
      id SERIAL PRIMARY KEY,
      host TEXT NOT NULL,
      start_at TEXT NOT NULL,
      end_at TEXT NOT NULL,
      reason TEXT DEFAULT '',
      created_by TEXT DEFAULT '',
      created_at TEXT NOT NULL
    );
CREATE INDEX idx_maint_host_window ON maintenance_windows(host, start_at, end_at);
CREATE TABLE service_history(
      id SERIAL PRIMARY KEY,
      service_id INTEGER NOT NULL,
      status TEXT NOT NULL,
      latency REAL NOT NULL DEFAULT 0,
      timestamp TEXT NOT NULL
    );
CREATE INDEX idx_svc_hist ON service_history(service_id, timestamp);
CREATE TABLE fiber_onts(
      id SERIAL PRIMARY KEY,
      ont_sn TEXT UNIQUE NOT NULL,
      customer TEXT DEFAULT '',
      olt_name TEXT DEFAULT '',
      pon_port TEXT DEFAULT '',
      odp_name TEXT DEFAULT '',
      rx_power REAL,
      tx_power REAL,
      status TEXT DEFAULT 'unknown',
      last_checked TEXT,
      source TEXT DEFAULT 'manual',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    , mute_alarm INTEGER NOT NULL DEFAULT 0, rx_warn REAL, rx_crit REAL, ont_index TEXT DEFAULT '', mute_until TEXT DEFAULT '', mute_reason TEXT DEFAULT '', last_seen TEXT DEFAULT '');
CREATE TABLE fiber_history(
      id SERIAL PRIMARY KEY,
      ont_id INTEGER NOT NULL,
      rx_power REAL,
      tx_power REAL,
      timestamp TEXT NOT NULL
    );
CREATE INDEX idx_fiber_hist ON fiber_history(ont_id, timestamp);
CREATE INDEX idx_fiber_sn ON fiber_onts(ont_sn);
CREATE TABLE odps(
      id SERIAL PRIMARY KEY,
      name TEXT UNIQUE NOT NULL,
      olt_name TEXT DEFAULT '',
      capacity INTEGER DEFAULT 8,
      location TEXT DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    , lat REAL, lon REAL);
CREATE INDEX idx_odp_name ON odps(name);
CREATE TABLE device_health(
      id SERIAL PRIMARY KEY,
      host TEXT NOT NULL,
      cpu REAL,
      mem_used REAL,
      storage_used REAL,
      temp_c REAL,
      timestamp TEXT NOT NULL,
      source TEXT DEFAULT 'snmp-mikrotik'
    , uptime_s REAL);
CREATE INDEX idx_devhealth_host_clock ON device_health(host, timestamp);
CREATE TABLE snmp_interfaces(
      host TEXT NOT NULL,
      if_index INTEGER NOT NULL,
      name TEXT DEFAULT '',
      oper INTEGER,
      monitor INTEGER NOT NULL DEFAULT 1,
      last_changed TEXT,
      PRIMARY KEY (host, if_index)
    );
CREATE TABLE iface_traffic(
      id SERIAL PRIMARY KEY,
      host TEXT NOT NULL,
      if_index INTEGER NOT NULL,
      net_in REAL DEFAULT 0.0,
      net_out REAL DEFAULT 0.0,
      timestamp TEXT NOT NULL
    );
CREATE INDEX idx_iface_host_idx_clock ON iface_traffic(host, if_index, timestamp);
CREATE TABLE olts(
      id SERIAL PRIMARY KEY,
      name TEXT UNIQUE NOT NULL,
      ip TEXT NOT NULL DEFAULT '',
      community TEXT NOT NULL DEFAULT '',
      vendor TEXT NOT NULL DEFAULT 'generic',
      rx_base TEXT DEFAULT '',
      tx_base TEXT DEFAULT '',
      div REAL NOT NULL DEFAULT 100.0,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    , scale REAL NOT NULL DEFAULT 1.0, "offset" REAL NOT NULL DEFAULT 0.0, last_tested TEXT DEFAULT '', last_test_ok INTEGER NOT NULL DEFAULT 0, last_test_msg TEXT DEFAULT '', lat REAL, lon REAL);
CREATE INDEX idx_olt_name ON olts(name);
CREATE TABLE fiber_downtime(
      id SERIAL PRIMARY KEY,
      ont_id INTEGER NOT NULL,
      ont_sn TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT 'critical',
      rx_dbm REAL,
      started_at TEXT NOT NULL,
      resolved_at TEXT,
      duration_s INTEGER
    );
CREATE INDEX idx_fiber_down ON fiber_downtime(ont_id, resolved_at);
CREATE TABLE mt_backups(
      id SERIAL PRIMARY KEY,
      host TEXT NOT NULL,
      taken_at TEXT NOT NULL,
      size INTEGER NOT NULL DEFAULT 0,
      sha256 TEXT NOT NULL DEFAULT '',
      content TEXT NOT NULL DEFAULT '',
      changed INTEGER NOT NULL DEFAULT 0
    );
CREATE INDEX idx_mt_backup ON mt_backups(host, id);
CREATE TABLE users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'operator',
        created_at TEXT NOT NULL,
        last_login TEXT
    );
CREATE TABLE host_thresholds (
        id SERIAL PRIMARY KEY,
        host TEXT UNIQUE NOT NULL,
        cpu_threshold REAL,
        ram_threshold REAL,
        disk_threshold REAL,
        created_at TEXT NOT NULL
    );
CREATE TABLE alert_history (
        id SERIAL PRIMARY KEY,
        host TEXT NOT NULL,
        alert_type TEXT NOT NULL,
        severity TEXT NOT NULL,
        message TEXT,
        triggered_at TEXT NOT NULL,
        resolved_at TEXT,
        duration_s INTEGER
    , acknowledged_at TEXT, acknowledged_by TEXT, assigned_to TEXT, note TEXT, escalated_at TEXT);
CREATE INDEX idx_down_host_resolved ON down_events(host, resolved_at, id);
CREATE TABLE audit_logs (
        id SERIAL PRIMARY KEY,
        timestamp TEXT NOT NULL,
        username TEXT NOT NULL,
        action TEXT NOT NULL,
        target TEXT,
        detail TEXT,
        ip_address TEXT
    );
