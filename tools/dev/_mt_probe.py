import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
sys.path.insert(0, os.path.join(BASE_DIR, "tests"))

FAKE_PASSWORD = os.environ.get("NMS_FAKE_ROUTEROS_PASSWORD", "test-password")

from collectors import mikrotik  # noqa: E402
from fake_routeros import FakeRouterOS, module_rows  # noqa: E402

server = FakeRouterOS(rows=module_rows(), counts={"/ip/firewall/connection/print": 1234})
port = server.start()
try:
    result = mikrotik.check_mikrotik(
        {"ip": "127.0.0.1", "port": port, "user": "api", "password": FAKE_PASSWORD, "ssl": False},
        plan={"layers": ["fast", "medium", "slow"], "modules": {}, "capabilities": {}, "log_cursor": {}},
    )
finally:
    server.stop()

print("ok:", result.get("ok"), "module_ok:", result.get("module_ok"), "error:", result.get("module_error"))
print("counts:", result.get("module_counts"))
print("system:", result.get("system"))
print("layer_status:", result.get("layer_status"))
print("probe_errors:", result.get("probe_errors"))
print("caps:", result.get("capabilities"))
for key, payload in (result.get("modules") or {}).items():
    print(key, "ok=", payload.get("ok"), "count=", payload.get("count"), "disabled=", payload.get("disabled"), "reason=", payload.get("reason"))
print("connections:", (result.get("modules") or {}).get("connections"))
