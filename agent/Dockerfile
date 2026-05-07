FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ /app/agent/
COPY simulator/ /app/simulator/
COPY tools/ /app/tools/
COPY config.yaml /app/config.yaml

EXPOSE 5001

HEALTHCHECK --interval=10s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:5001/api/health || exit 1

CMD ["python", "-m", "agent.api"]
