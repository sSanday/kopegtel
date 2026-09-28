# NMS Dashboard - Staging Deployment Log
## Deployment to Staging Environment

**Date:** 2026-09-28 07:38 UTC  
**Target:** Staging  
**Duration Plan:** 24-hour monitoring before production

---

## Pre-Deployment Checklist

```bash
# 1. Verification
bash verify-deployment.sh

# 2. Health check
bash healthcheck.sh

# 3. Git status
git status
git log --oneline -1
```

---

## Deployment Steps

### Step 1: Prepare Staging Environment
```bash
cd /home/sandayy/nms-dashboard1

# Backup current database (if exists)
cp network.db network.db.backup.$(date +%s)

# Verify docker-compose file
cat docker-compose.yml | head -20
```

### Step 2: Deploy with Docker Compose
```bash
# Start services
docker-compose up -d

# Wait for initialization (30 seconds)
sleep 30

# Verify containers are running
docker-compose ps
```

### Step 3: Verify Deployment
```bash
# Check health endpoint
curl http://localhost:5000/health

# Check application logs
docker-compose logs app | tail -20

# Verify database
sqlite3 network.db "SELECT COUNT(*) FROM hosts;"
```

### Step 4: Login Test
```bash
# Open in browser: http://localhost:5000
# Username: admin
# Password: (from .env DASHBOARD_PASSWORD)
```

---

## Monitoring Setup (24 Hours)

### Terminal 1: Real-time Monitoring
```bash
bash monitor.sh
# Runs continuously, refreshes every 10 seconds
# Shows: CPU, memory, database, logs
```

### Terminal 2: Log Streaming
```bash
tail -f logs/app.log
# Watch for errors in real-time
```

### Terminal 3: Health Checks (Every 30 min)
```bash
while true; do
  echo "=== $(date) ==="
  bash healthcheck.sh --report
  sleep 1800  # 30 minutes
done
```

### Terminal 4: System Monitoring (Optional)
```bash
watch -n 5 'docker stats --no-stream'
# Or: ps aux | grep python3
```

---

## 24-Hour Monitoring Plan

### Hour 0-1: Critical Monitoring (Highest Alert)
- ✅ Monitor every 5 minutes
- ✅ Check all logs for errors
- ✅ Verify application startup
- ✅ Test login functionality
- ✅ Check database connectivity

**Expected behavior:**
- CPU: < 50%
- Memory: < 500MB
- Response time: < 500ms
- No errors in logs

### Hour 1-12: Normal Monitoring
- ✅ Monitor every 30 minutes
- ✅ Check for error patterns
- ✅ Verify database growth normal
- ✅ Test basic features

**Expected behavior:**
- CPU: < 30% (idle)
- Memory: steady
- No recurring errors
- Database size stable

### Hour 12-24: Baseline Monitoring
- ✅ Monitor every hour
- ✅ Collect performance data
- ✅ Verify stability
- ✅ Test all major features

**Expected behavior:**
- All metrics stable
- No errors
- Features working normally

---

## Verification Checklist (24 Hours)

### Functionality Tests
- [ ] Login successful
- [ ] Dashboard loads
- [ ] All hosts visible
- [ ] Status updates in real-time
- [ ] Alerts trigger correctly
- [ ] Logs accessible
- [ ] API endpoints responding
- [ ] Database queries fast

### Performance Metrics
- [ ] CPU usage < 50%
- [ ] Memory usage < 500MB
- [ ] Response time < 500ms
- [ ] Database size normal growth
- [ ] No query timeouts
- [ ] No memory leaks

### Error Monitoring
- [ ] No SQL errors
- [ ] No connection errors
- [ ] No authentication errors
- [ ] No file system errors
- [ ] All threads running
- [ ] Database integrity OK

### Security Checks
- [ ] Login page accessible
- [ ] Credentials working
- [ ] No unauthorized access
- [ ] No exposed errors
- [ ] SSL/TLS (if configured)
- [ ] CORS headers correct

### Monitoring Tools
- [ ] Health endpoint: 200 OK
- [ ] Logs: readable and rotating
- [ ] Monitor script: working
- [ ] Health check script: passing
- [ ] Database: accessible
- [ ] Disk space: adequate

---

## Troubleshooting During Staging

### If Application Won't Start
```bash
# Check logs
docker-compose logs -f app

# Check syntax
python3 -m py_compile app.py

# Check dependencies
pip list | grep -E "Flask|APScheduler"

# Restart
docker-compose restart app
```

### If High CPU Usage
```bash
# Check which process
top -p $(pgrep -f python3)

# Check logs for slow queries
grep SLOW logs/app.log

# Restart if needed
docker-compose restart app
```

### If Database Errors
```bash
# Check integrity
sqlite3 network.db "PRAGMA integrity_check;"

# Restore from backup if needed
cp network.db.backup network.db

# Reinitialize
python3 -c "from nms.db import init_db; init_db()"
```

### If Memory Keeps Growing
```bash
# Check for memory leaks
docker-compose logs app | grep -i memory

# Restart to reset
docker-compose restart app

# Check cleanup jobs are running
grep cleanup logs/app.log
```

---

## Decision Criteria for Production

### GREEN - Ready for Production ✅
- ✅ All 24-hour tests passing
- ✅ No errors in logs
- ✅ Performance metrics normal
- ✅ All features working
- ✅ Database stable
- ✅ Monitoring working

### YELLOW - Conditional Approval ⚠️
- ⚠️ Minor issues found & fixed
- ⚠️ Some features need tweaking
- ⚠️ Performance needs optimization
- ⚠️ Need another 24 hours testing

### RED - Not Ready ❌
- ❌ Critical errors found
- ❌ Performance unacceptable
- ❌ Security issues
- ❌ Database corruption
- ❌ Need code fixes

---

## Post-Staging Decision

After 24 hours monitoring:

### If Ready (GREEN)
```bash
# Prepare for production
docker-compose down
# Backup staging database
cp network.db network.db.staging.$(date +%s)

# Deploy to production
# (Use same commands but different environment)
```

### If Needs More Time (YELLOW)
```bash
# Continue monitoring
# Fix identified issues
# Re-test for another 24 hours
```

### If Issues Found (RED)
```bash
# Rollback if needed
docker-compose down

# Fix issues in code
git log --oneline -5
# Review recent commits

# Re-test locally
python3 app.py

# Redeploy to staging
docker-compose up -d
```

---

## Monitoring URLs & Commands

```bash
# Health check
curl http://localhost:5000/health

# Dashboard
http://localhost:5000

# API
curl http://localhost:5000/api/dashboard/summary

# Docker logs
docker-compose logs -f

# System metrics
docker stats

# Database check
sqlite3 network.db "SELECT COUNT(*) FROM hosts;"

# Monitor script
bash monitor.sh

# Health check script
bash healthcheck.sh
```

---

## Contact & Support

If issues during staging:
1. Check logs: `docker-compose logs app`
2. Run health check: `bash healthcheck.sh`
3. Review MONITORING_GUIDE.md
4. Check IMPROVEMENT_REPORT.md

---

**Status:** Ready for 24-hour staging deployment  
**Next Phase:** Production deployment (if approved)  
**Duration:** 24 hours continuous monitoring
