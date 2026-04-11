# geo-rate-limiter

Geo-distributed rate limiter with AI traffic shaping. Three regional API gateways (US, EU, Asia), CRDT-based cross-region counter sync, a traffic simulator, and an AI agent that dynamically adjusts rate limits per tier and region.

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Docker Compose v2)
- Go 1.22+
- Python 3.11+
- `redis-cli` (for smoke checks — ships with Redis or install via `brew install redis`)

## Quick start

```bash
# 1. Bring up stateful infra
docker compose up -d

# 2. Verify all five services are running
docker compose ps

# 3. Start the gateway (host process for Phase 1)
cd gateway
go mod tidy
go run .
# listens on :8081

# 4. Smoke check
curl http://localhost:8081/health
```

## Services & ports

| Service    | Host port | Purpose                  |
|------------|-----------|--------------------------|
| redis-us   | 6379      | Rate limit counters — US |
| redis-eu   | 6380      | Rate limit counters — EU |
| redis-asia | 6381      | Rate limit counters — Asia |
| prometheus | 9090      | Metrics scraping         |
| grafana    | 3000      | Dashboards (admin/admin) |
| gateway-us | 8081      | API gateway — US region  |

## Repo layout

```
gateway/      Go API gateway (Gin)
sync/         Python CRDT sync service
simulator/    Python traffic simulator
agent/        Python AI rate-limit agent
infra/        Prometheus config, future Grafana provisioning
docs/         contracts.md — the four API/schema contracts
docker-compose.yml
```

## Contracts

See [docs/contracts.md](docs/contracts.md) for the four contracts every component must conform to: Gateway HTTP API, Redis key schema, Prometheus metrics, Policy JSON.

## Current phase

**Phase 1 — Infrastructure scaffolding** (in progress)
```
