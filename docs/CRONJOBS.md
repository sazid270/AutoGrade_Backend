# Scheduled Tasks (Cronjobs)

This document lists management commands that should be run on a schedule.

## 1. Expired OTP Deletion

Automatically deletes expired OTP records to keep the database clean.

**Usage:**

```sh
python manage.py delete_expired_otps
```

**Container/Dokploy:**

```sh
sh -c "cd /app && /opt/venv/bin/python manage.py delete_expired_otps"
```

**Cron Schedule (every day at midnight):**

```cron
0 0 * * * cd /path/to/autograde_backend && /path/to/venv/bin/python manage.py delete_expired_otps
```
