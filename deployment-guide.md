# Deployment Guide — Geo-Distributed Rate Limiter

## Architecture Overview

```
Internet
   │
   ▼
[nginx :8080]  ──/api/──▶  [api :5001]  ──▶  Redis × 3
   │                              │
   └── serves dashboard.html      └── Prometheus :9090
                                       Grafana    :3000
                                       Gateways × 3 :808x
```

---

## 1. Local Testing with docker-compose.production.yml

```bash
# Copy and edit env file
cp .env.example .env

# Build and start all services
docker compose -f docker-compose.production.yml up --build -d

# Verify health
docker compose -f docker-compose.production.yml ps
curl http://localhost:5001/api/health    # API
curl -I http://localhost:8080/          # Dashboard via nginx

# Seed demo policies
curl -X POST http://localhost:5001/api/control/policies/seed

# Tear down
docker compose -f docker-compose.production.yml down
```

**Service ports (production compose):**

| Service      | Host port | Purpose                    |
|--------------|-----------|----------------------------|
| dashboard    | 8080      | Nginx — serves UI + proxies /api/ |
| api          | 5001      | Flask dashboard API        |
| gateway-us   | 8081      | Rate-limit gateway (US)    |
| gateway-eu   | 8082      | Rate-limit gateway (EU)    |
| gateway-asia | 8083      | Rate-limit gateway (Asia)  |
| prometheus   | 9090      | Metrics scrape             |
| grafana      | 3000      | Dashboards (admin/admin)   |
| redis-us     | 6379      | US counter store           |
| redis-eu     | 6380      | EU counter store           |
| redis-asia   | 6381      | Asia counter store         |

---

## 2. AWS Deployment (EC2 + Docker)

### 2.1 Launch an EC2 instance

- **AMI:** Amazon Linux 2023 or Ubuntu 24.04 LTS
- **Instance type:** t3.medium (2 vCPU, 4 GB RAM) or larger
- **Security group — inbound rules:**

| Port  | Source    | Purpose              |
|-------|-----------|----------------------|
| 22    | Your IP   | SSH                  |
| 80    | 0.0.0.0/0 | Dashboard (HTTP)     |
| 443   | 0.0.0.0/0 | Dashboard (HTTPS)    |
| 3000  | Your IP   | Grafana (optional)   |
| 9090  | Your IP   | Prometheus (optional)|

### 2.2 Install Docker

```bash
# Amazon Linux 2023
sudo dnf update -y
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
newgrp docker

# Install Docker Compose plugin
sudo dnf install -y docker-compose-plugin
```

### 2.3 Clone and configure

```bash
git clone https://github.com/<your-org>/geo-rate-limiter.git
cd geo-rate-limiter
cp .env.example .env
# Edit .env as needed — defaults work for single-host deployment
```

### 2.4 Start services

```bash
docker compose -f docker-compose.production.yml up --build -d
```

### 2.5 Point port 80 to the dashboard container

The dashboard service already binds `8080:80` internally. To expose it on host port 80, either:

- Change the port mapping to `"80:80"` in `docker-compose.production.yml`, or
- Use nginx on the host as a reverse proxy (recommended for SSL — see §4).

---

## 3. GCP Deployment (Compute Engine + Docker)

### 3.1 Create a VM

```bash
gcloud compute instances create geo-rate-limiter \
  --machine-type=e2-medium \
  --image-family=ubuntu-2404-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=20GB \
  --tags=http-server,https-server \
  --zone=us-central1-a
```

### 3.2 Open firewall ports

```bash
gcloud compute firewall-rules create allow-http \
  --allow=tcp:80,tcp:443 \
  --target-tags=http-server,https-server

# Optional: Grafana / Prometheus (restrict source range in production)
gcloud compute firewall-rules create allow-monitoring \
  --allow=tcp:3000,tcp:9090 \
  --source-ranges=<YOUR_IP>/32 \
  --target-tags=http-server
```

### 3.3 SSH and install Docker

```bash
gcloud compute ssh geo-rate-limiter --zone=us-central1-a

# Inside the VM
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker
```

### 3.4 Deploy

```bash
git clone https://github.com/<your-org>/geo-rate-limiter.git
cd geo-rate-limiter
cp .env.example .env
docker compose -f docker-compose.production.yml up --build -d
```

---

## 4. Environment Variable Setup

All configuration is driven by environment variables. Copy `.env.example` to `.env` and set:

```bash
# Required for production (override Docker service names if needed)
ENVIRONMENT=production
PROMETHEUS_URL=http://prometheus:9090

# Redis — keep as Docker service names within the compose network
REDIS_US_HOST=redis-us
REDIS_EU_HOST=redis-eu
REDIS_ASIA_HOST=redis-asia

# Gateways (internal Docker names)
GATEWAY_US_URL=http://gateway-us:8080
GATEWAY_EU_URL=http://gateway-eu:8080
GATEWAY_ASIA_URL=http://gateway-asia:8080

# If you restrict CORS in production, set this:
CORS_ORIGINS=https://yourdomain.com
```

Pass the file to Docker Compose:

```bash
docker compose -f docker-compose.production.yml --env-file .env up -d
```

---

## 5. SSL/TLS with Let's Encrypt

Install Certbot on the host and use it as a reverse proxy in front of the dashboard container.

```bash
# Ubuntu/Debian
sudo apt install -y certbot python3-certbot-nginx

# Obtain certificate (replace with your domain)
sudo certbot --nginx -d yourdomain.com

# Host nginx config — /etc/nginx/sites-available/geo-rate-limiter
server {
    listen 80;
    server_name yourdomain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://localhost:8080;   # dashboard container
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx

# Auto-renewal (runs twice daily via systemd timer)
sudo systemctl enable --now certbot.timer
```

---

## 6. Monitoring Setup

### Prometheus

Prometheus is scraped automatically via `infra/prometheus.yml`. Access it at:
- Local: `http://localhost:9090`
- Production: restrict to your IP via security group / firewall rule.

### Grafana

Grafana is pre-provisioned with the rate-limiter dashboard via `infra/grafana/provisioning`.

- Default credentials: `admin / admin` — **change immediately in production**
- Access: `http://<host>:3000`
- To expose Grafana securely, add a second `server {}` block in nginx pointing to port 3000 with authentication.

### Key metrics to watch

| Metric | What it shows |
|--------|--------------|
| `rl_requests_total{decision="denied"}` | Rate-limit rejections |
| `rl_sync_lag_seconds` | Cross-region CRDT propagation delay |
| `rl_policy_version` | Policy rollout confirmation per region |
| `rl_counter_value` | Top-N user counters (sampled) |

---

## 7. Verifying the Deployment

```bash
# API health
curl https://yourdomain.com/api/health

# Metrics summary
curl https://yourdomain.com/api/metrics | python3 -m json.tool

# Rate-limit a request through the US gateway
curl -X POST http://<host>:8081/check \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"test_01","tier":"free","region":"us","endpoint":"/search"}'

# Start a traffic scenario
curl -X POST https://yourdomain.com/api/control/scenario \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"product_launch"}'
```
