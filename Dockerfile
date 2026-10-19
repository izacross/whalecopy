FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data
WORKDIR /app

COPY pyproject.toml ./
COPY whalecopy ./whalecopy
RUN pip install .

# Copies config.example.yaml and, if you created one, config.yaml (env vars override both).
COPY config* ./

# Runs as root on purpose: Fly mounts the /data volume root-owned, and the container
# holds nothing but this bot.
RUN mkdir -p /data

CMD ["whalecopy", "run"]
