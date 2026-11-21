# Background Worker (Celery & Redis)

Answer sheets are processed in the background. Two things must be running alongside the Django server: **Redis** (the queue) and a **Celery worker**. If either is missing, batches stay `pending`.

## Setup (once)

1. Install the project dependencies in your virtual environment. This includes Celery and the Redis client:
   ```bash
   pip install -r requirements.txt
   ```
2. Install Redis:
   - Ubuntu / WSL2: `sudo apt update && sudo apt install -y redis-server`
   - macOS: `brew install redis`
   - Windows: Redis has no native Windows build, so install it inside WSL2 with the Ubuntu command above.
3. Set `OPENROUTER_API_KEY` in `.env`. It is required for reading sheets.

`CELERY_BROKER_URL` defaults to `redis://localhost:6379/0`. Set it in `.env` only if Redis runs somewhere else.

## Run

Run these in separate terminals, with the virtual environment active for the worker.

1. **Redis**
   ```bash
   sudo service redis-server start    # Ubuntu / WSL2 (macOS: redis-server)
   redis-cli ping                     # should print PONG
   ```
2. **Celery worker**
   ```bash
   celery -A autograde worker -l info
   ```
   On Windows, add `--pool=solo`.

Restart the worker after every code or `.env` change. It does not reload by itself. Redis in WSL does not start automatically, so start it again after a reboot or WSL restart.

## Troubleshooting

| Symptom                                                     | Fix                                                                                                                                                                                                                |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `Error 10061 connecting to localhost:6379`                  | Redis is not running. Start it.                                                                                                                                                                                    |
| `Redis transport requires redis-py versions 6.1.0 or later` | Run `pip install -r requirements.txt`, then restart Django and the worker.                                                                                                                                         |
| Batch stays `pending`                                       | The worker is not running. Start it. To queue the batch again, run `from app.tasks import process_batch_task; process_batch_task.delay(<batch_id>)` in `python manage.py shell`.                                   |
| Sheets are `failed` with a provider message                 | Wait a few minutes, then call `POST /api/app/answer-sheet-batch/{id}/retry-failed/`.                                                                                                                               |
| Redis runs in WSL but Django cannot connect                 | Set `CELERY_BROKER_URL=redis://<WSL IP>:6379/0` in `.env` (get the IP with `wsl hostname -I`). For development only, also set `bind 0.0.0.0` and `protected-mode no` in `/etc/redis/redis.conf` and restart Redis. |

## Production (Linux)

```bash
celery -A autograde worker -l info --concurrency=1
```

Run the worker under a process manager such as systemd or Supervisor so it restarts automatically. Redis must be reachable at `CELERY_BROKER_URL`.
