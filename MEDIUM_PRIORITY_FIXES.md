# Medium Priority Fixes - Performance & Stability

**Status:** Documentation + Optimization Framework Created  
**Date:** 28 September 2026

---

## 1. N+1 Query Optimization

### Problem Identified
20 endpoints identified with N+1 query patterns:
- `/api/stats` - loops over hosts, queries ping_logs for each
- `/api/agent/metrics` - loops over hosts, queries agent_metrics for each
- `/api/metrics` - loops over hosts, queries ping_logs for each
- `/api/history` - loops over hosts, queries ping_logs for each
- `/api/export/stats` - same pattern
- `/api/dashboard/summary` - multiple loops
- And more...

### Impact
- **Performance:** Query count: ~1 + N (N = number of hosts)
- **Database Load:** Increases linearly with host count
- **Latency:** Noticeable at 50+ hosts

### Solution Provided
Created `nms/query_helpers.py` with optimized patterns:

```python
# BEFORE (N+1 problem)
stats = {}
for host in get_target_hosts():  # 1 query
    c.execute("SELECT ... FROM ping_logs WHERE host=?", (host,))  # N queries
    r = c.fetchone()
    stats[host] = {...}

# AFTER (Single query)
from nms.query_helpers import get_all_host_stats
stats = get_all_host_stats(c)  # 1 query instead of N+1
```

### Helper Functions Available
1. `get_all_host_stats()` - All host ping stats in 1 query
2. `get_all_agent_metrics()` - Latest metrics for all hosts in 1 query
3. `get_host_ping_history()` - Ping history for multiple hosts in 1 query
4. `get_service_uptime_map()` - Service uptime for all services in 1 query

### Implementation Priority
**High Priority Endpoints** (call most frequently):
1. `/api/stats` - Dashboard uses this
2. `/api/agent/metrics` - Real-time dashboard
3. `/api/dashboard/summary` - Every page load

**Medium Priority Endpoints**:
- `/api/metrics`
- `/api/history`
- `/api/export/stats`

**Lower Priority**:
- Public status page
- Inventory endpoints
- Report endpoints

### Estimated Performance Improvement
- **Query count:** 1 + N → 1 (99% reduction for N=100)
- **Latency:** 500ms → 50ms (10x faster) for 100 hosts
- **Database load:** O(N) → O(1)

---

## 2. Thread Safety for Global State

### Problem Identified
12 global dictionaries without thread safety:
```python
status_memory = {}          # No lock
agent_status_memory = {}    # No lock
down_since = {}             # No lock
mt_alarm_memory = {}        # No lock
fiber_alarm_memory = {}     # No lock
fiber_degrade_memory = {}   # No lock
fiber_flap_memory = {}      # No lock
mt_iface_oper = {}          # No lock
mt_iface_tg = {}            # No lock
mt_iface_flaps = {}         # No lock
iface_state = {}            # No lock
snmp_state = {}             # No lock
```

Only `login_failures` has lock (good example).

### Risk
- **Race conditions** possible in multi-threaded environment
- **Data corruption** if multiple threads modify simultaneously
- **Inconsistent state** between requests

### Solution Pattern
```python
from threading import Lock

# Initialize with lock
status_memory_lock = Lock()
status_memory = {}

# Usage
with status_memory_lock:
    status_memory[host] = value
```

### Implementation Steps
1. Add `from threading import Lock` at top of app.py
2. Create lock for each shared dict:
   ```python
   status_memory_lock = Lock()
   agent_status_memory_lock = Lock()
   # ... etc
   ```
3. Wrap all accesses with lock:
   ```python
   with status_memory_lock:
       status_memory[host] = True
   ```

### Priority
**Medium** - May not trigger in production (depends on load), but good practice

---

## 3. Error Handling Improvements

### Problem Identified
63 generic `except Exception:` clauses that silently fail

### High-Risk Locations
1. **Database operations** (~15 instances)
   - Silent failures can corrupt data
   - Example: backup operations, commit with retry

2. **API endpoints** (~12 instances)
   - Users see no error, confusion results

3. **Monitoring loops** (~10 instances)
   - Could miss alarms/alerts

### Solution Pattern
```python
# BEFORE
try:
    do_something()
except Exception:
    pass  # Silent failure!

# AFTER
try:
    do_something()
except sqlite3.OperationalError as e:
    logger.error(f"Database operation failed: {e}")
    return jsonify({"error": "Operation failed"}), 500
except ValueError as e:
    logger.error(f"Invalid value: {e}")
    return jsonify({"error": "Invalid input"}), 400
except Exception as e:
    logger.error(f"Unexpected error: {e}")
    return jsonify({"error": "Internal error"}), 500
```

### Most Critical to Fix
1. Line 78: Database migration failures
2. Line 913: Monitoring loop (could miss alarms)
3. Line 1084: Commit retry failures
4. Line 2576: API error handling

### Framework Ready
- `logger.error()` already in place
- Use this consistently everywhere

---

## Implementation Roadmap

### Phase 1: Quick Wins (1-2 hours)
1. ✅ Created `query_helpers.py` with optimized patterns
2. ✅ Documented N+1 query endpoints
3. ✅ Documented thread safety issues
4. ✅ Provided solution patterns

### Phase 2: Implementation (Recommended - 2-4 hours)
1. Apply `query_helpers` to 3 high-priority endpoints
2. Add thread safety to critical dictionaries
3. Add proper error handling to critical paths
4. Test & verify performance improvement

### Phase 3: Verification (1-2 hours)
1. Load test (100+ hosts)
2. Monitor query count
3. Check latency improvement
4. Verify thread safety

---

## Testing Strategy

### Performance Baseline Test
```bash
# Before optimization
curl -w "@curl_format.txt" http://localhost:5000/api/stats

# Measure query count from logs
grep "SELECT.*FROM ping_logs" logs/app.log | wc -l
```

### Thread Safety Test
```python
# Simulate concurrent requests
import concurrent.futures
import requests

with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
    futures = [executor.submit(requests.get, 'http://localhost:5000/api/stats') 
               for _ in range(100)]
    for future in concurrent.futures.as_completed(futures):
        print(future.result().status_code)
```

### Error Handling Test
```bash
# Trigger various error conditions
curl -X POST http://localhost:5000/api/users \
  -H "Content-Type: application/json" \
  -d '{"invalid": "data"}'

# Check logs for proper error messages
tail -f logs/app.log
```

---

## Next Steps

1. **Immediate (Optional):**
   - Review `nms/query_helpers.py`
   - Understand optimization patterns

2. **Short-term (Recommended):**
   - Update 3 high-priority endpoints to use helpers
   - Add thread safety to critical dictionaries
   - Improve error handling on critical paths

3. **Medium-term:**
   - Optimize remaining 17 endpoints
   - Add comprehensive error handling
   - Performance testing

---

## Files Provided

| File | Purpose |
|------|---------|
| `nms/query_helpers.py` | Optimized query patterns |
| `MEDIUM_PRIORITY_FIXES.md` | This documentation |

---

## Estimated Impact

After implementing all 3 fixes:
- **Query Performance:** 10-50x faster (depending on host count)
- **Thread Safety:** Eliminating race condition risk
- **Error Handling:** Better debugging & user experience
- **Overall:** Significantly improved stability & performance

---

**Status:** Framework Ready ✅  
**Implementation:** Recommended for next sprint  
**Effort:** 4-6 hours total  
**Impact:** High (Performance + Stability)

