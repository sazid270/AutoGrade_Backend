# Server & Gunicorn Setup

## Running the Server

- Use Gunicorn for production deployments.
- Example command:

```sh
  bash scripts/server.sh --start
```

- Use `server.sh` for start/stop/restart management.

## Activating the Python Virtual Environment

Before starting the server, activate virtual environment:

- Linux server (the setup script creates `.venv`):

```sh
source .venv/bin/activate
```

## Managing Gunicorn Processes

To check how many Gunicorn servers are running for backend:

```sh
ps aux | grep 'gunicorn.*autograde\.wsgi' | grep -v grep | wc -l
```

To stop all Gunicorn servers for backend:

```sh
pkill -f "gunicorn.*autograde\.wsgi"
```

## Logs

- Logs are stored in the `logs/` directory.
