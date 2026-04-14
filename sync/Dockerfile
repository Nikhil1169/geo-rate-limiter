FROM python:3.11-slim

WORKDIR /app

# Install dependencies first so this layer is cached independently of code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the sync package.  Build context is ./sync, so "." is the sync directory.
# The result: /app/sync/{counter.py,sync_service.py,admin.py,merge.lua,...}
COPY . ./sync/

ENV PYTHONUNBUFFERED=1

# 8001 = FastAPI admin   9101 = Prometheus metrics
EXPOSE 8001 9101

CMD ["python", "-m", "sync.sync_service"]
