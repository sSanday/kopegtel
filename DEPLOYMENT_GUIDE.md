# NMS Dashboard - Deployment Guide

## 🚀 Quick Start (Development)

### 1. Setup Environment
```bash
cd /home/sandayy/nms-dashboard1
cp .env.example .env
```

### 2. Edit .env with Your Values
```bash
nano .env
```

Required changes:
- `TELEGRAM_BOT_TOKEN` - Get from BotFather on Telegram
- `TELEGRAM_CHAT_ID` - Your Telegram chat ID
- `DASHBOARD_PASSWORD` - Change from "ChangeMe123!@#"
- `AGENT_API_KEY` - Already set, but can regenerate if needed

### 3. Install Dependencies
```bash
python3 -m pip install -r requirements.txt
```

### 4. Run Application
```bash
python3 app.py
```

Expected output:
```
[INFO] Scheduler started
[INFO] Startup backup catchup...
[INFO] Dashboard aktif: http://localhost:5000
```

### 5. Access Dashboard
- URL: http://localhost:5000
- Login: admin / (your new password)

---

## 🐳 Docker Deployment

### Build Image
```bash
docker build -t nms-dashboard:latest .
```

### Run Container
```bash
docker run -d \
  --name nms-dashboard \
  -p 5000:5000 \
  -v nms-data:/app/data \
  -v nms-backups:/app/backups \
  -v nms-logs:/app/logs \
  --env-file .env \
  nms-dashboard:latest
```

### With Docker Compose
```bash
docker-compose up -d
```

---

## 🐧 Systemd Service (Linux)

### 1. Copy Service File
```bash
sudo cp nms-dashboard.service /etc/systemd/system/
```

### 2. Edit Service File
```bash
sudo nano /etc/systemd/system/nms-dashboard.service
```

Update paths to match your setup:
- `WorkingDirectory=/path/to/nms-dashboard1`
- ExecStart path
- User account

### 3. Start Service
```bash
sudo systemctl daemon-reload
sudo systemctl start nms-dashboard
sudo systemctl enable nms-dashboard
```

### 4. Check Status
```bash
sudo systemctl status nms-dashboard
```

---

## ✅ Verification Steps

### 1. Health Check
```bash
curl http://localhost:5000/health
```

Expected response:
```json
{
  "status": "healthy",
  "timestamp": "2026-09-28T06:43:29",
  "uptime_seconds": 123.45
}
```

### 2. Login Test
```bash
curl -X POST http://localhost:5000/login \
  -d "username=admin&password=your_password"
```

### 3. API Test (with agent key)
```bash
curl -X POST http://localhost:5000/api/agent/report \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your_agent_api_key" \
  -d '{"host":"test-host","cpu":50,"ram":60,"disk":70}'
```

---

## 📋 Production Checklist

### Security
- [ ] Change all default passwords
- [ ] Set strong AGENT_API_KEY
- [ ] Enable HTTPS (set COOKIE_SECURE=1)
- [ ] Use secrets management system
- [ ] Verify .env not accessible publicly
- [ ] Regular security audits

### Performance
- [ ] Configure log rotation
- [ ] Set up monitoring/alerting
- [ ] Monitor API response times
- [ ] Setup backups (already automated daily at 00:05)
- [ ] Monitor disk space

### Operations
- [ ] Document admin procedures
- [ ] Setup password rotation policy
- [ ] Plan disaster recovery
- [ ] Monitor system resources
- [ ] Keep dependencies updated

---

## 🔧 Troubleshooting

### App won't start
1. Check .env file exists and has AGENT_API_KEY
2. Verify Python 3.10+ installed: `python3 --version`
3. Check port 5000 not in use: `lsof -i :5000`
4. Check logs: `tail -f logs/app.log`

### Can't login
1. Check credentials in .env match
2. Verify SECRET_KEY is set
3. Check session cookies enabled in browser
4. Clear browser cookies and try again

### Database errors
1. Check network.db file permissions
2. Verify disk space available
3. Check logs for detailed error
4. Restart application

### Performance issues
1. Check N+1 queries in API endpoints
2. Monitor system resources (CPU/memory/disk)
3. Check database size: `du -sh network.db`
4. Consider database cleanup for old logs

---

## 📚 Additional Resources

- **Security:** Read `SECURITY_IMPROVEMENTS.md`
- **Audit Report:** Read `AUDIT_FINAL_REPORT.md`
- **Configuration:** See `.env.example`
- **API Testing:** Use `/health` endpoint
- **Logs:** Check `logs/app.log`

---

## 🆘 Support

If you encounter issues:

1. Check logs: `tail -f logs/app.log`
2. Verify configuration: `cat .env`
3. Test health: `curl http://localhost:5000/health`
4. Check system resources: `free -h && df -h`
5. Review this guide's troubleshooting section

---

**Last Updated:** 28 September 2026  
**Version:** 1.0 (Post-Audit)

