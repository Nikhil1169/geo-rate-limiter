-- token_bucket.lua
-- Atomic token bucket rate limiter. Single EVAL = one decision; no client round-trips.
--
-- KEYS[1] = bucket key  e.g. rl:local:us:free:user_123
-- ARGV[1] = limit_per_minute  int, tokens that accrue per 60_000 ms
-- ARGV[2] = burst             int, max bucket depth; new keys start full
-- ARGV[3] = now_ms            int, caller wall-clock in milliseconds
--
-- Returns: { allowed (0|1), remaining_tokens (int floor), retry_after_ms (int) }

local key      = KEYS[1]
local limit    = tonumber(ARGV[1])
local burst    = tonumber(ARGV[2])
local now_ms   = tonumber(ARGV[3])
local ttl_secs = 120  -- Contract 2 invariant; never drift this per call site

-- 1. Read. Absent key → fresh bucket starts full so the first request is allowed.
local data           = redis.call("HMGET", key, "tokens", "last_refill_ms")
local tokens         = tonumber(data[1]) or burst
local last_refill_ms = tonumber(data[2]) or now_ms

-- 2. Refill. Clamp elapsed >= 0: a backward NTP step must not drain the bucket.
local elapsed_ms = now_ms - last_refill_ms
if elapsed_ms < 0 then elapsed_ms = 0 end

if limit > 0 then
    tokens = math.min(burst, tokens + elapsed_ms * limit / 60000.0)
end
-- limit == 0: no refill; bucket drains to deny-all. No division below.

-- 3. Decide. Decrement only when serving the request.
local allowed        = 0
local retry_after_ms = 0

if tokens >= 1.0 then
    tokens  = tokens - 1.0
    allowed = 1
else
    if limit > 0 then
        retry_after_ms = math.ceil((1.0 - tokens) * 60000.0 / limit)
    else
        retry_after_ms = -1  -- sentinel: this bucket never refills
    end
end

-- 4. Persist. Advance last_refill_ms monotonically — never move it backward,
--    so a backward-skewed call cannot inflate the next forward call's elapsed.
local new_refill_ms = last_refill_ms
if now_ms > new_refill_ms then new_refill_ms = now_ms end
redis.call("HMSET", key, "tokens", tokens, "last_refill_ms", new_refill_ms)
redis.call("EXPIRE", key, ttl_secs)  -- refresh even on denial; see F3

-- 5. Return. Fractional precision stays in storage; callers get a clean integer floor.
return { allowed, math.floor(tokens), retry_after_ms }
