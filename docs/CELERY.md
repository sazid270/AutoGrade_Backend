# Background worker (Celery and Redis)

Answer sheets are processed in the background. Run **Redis** (the queue) and a
**Celery worker** alongside the Django server. Without them, batches stay
`pending`.

## Setup

1. Install the project dependencies, including Celery and the Redis client:

   ```bash
   pip install -r requirements.txt
   ```

2. Install Redis. Ubuntu/WSL2: `sudo apt install -y redis-server`; macOS:
   `brew install redis`. Windows has no native Redis build; use WSL2.

`CELERY_BROKER_URL` defaults to `redis://localhost:6379/0`. Set it in `.env` only
if Redis runs elsewhere.

## Run

Start Redis and the worker in separate terminals:

```bash
sudo service redis-server start
redis-cli ping
celery -A autograde worker -l info
```

On Windows, add `--pool=solo` to the worker command. Restart the worker after
every code or `.env` change.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Connection refused on port 6379 | Redis is not running. Start it. |
| Redis transport requires redis-py 6.1.0 or later | Run `pip install -r requirements.txt`, then restart Django and the worker. |
| Batch stays `pending` | The worker is not running. Start it. |
| Sheets are `failed` | Check `error_message`, fix the cause, then use `POST /api/app/answer-sheet-batch/{id}/retry-failed/`. |

## Production

Run the worker under systemd or Supervisor so it restarts automatically. Redis
must be reachable at `CELERY_BROKER_URL`.
