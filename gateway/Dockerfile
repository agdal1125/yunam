FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# gosu lets the entrypoint drop privileges to appuser after optional uid remap.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gosu \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer first for build cache reuse
COPY requirements.txt .
RUN pip install -r requirements.txt

# Non-root user created at uid 1000 by default; entrypoint remaps if PUID differs.
RUN useradd --create-home --uid 1000 appuser

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

COPY . .
RUN chown -R appuser:appuser /app

# Container starts as root so entrypoint can remap uid/gid and chown /data/yunam.
# The entrypoint then `exec gosu appuser ...` to drop privileges before running the app.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-u", "main.py"]
