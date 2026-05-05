-- sliding_window.lua
-- Atomic sliding-window-log rate limiter via Redis sorted set.
-- Single EVAL = one decision; no client round-trips.
--
-- KEYS[1] = window key  e.g. rl:local:us:free:user_123
-- ARGV[1] = limit       int, max requests in the window
-- ARGV[2] = window_ms   int, window length in milliseconds (60000 = per minute)
-- ARGV[3] = now_ms      int, caller wall-clock in milliseconds
-- ARGV[4] = nonce       string, unique per call (prevents score+member dedup at
--                        high QPS when multiple calls share the same now_ms)
--
-- Returns: { allowed (0|1), remaining (int), retry_after_ms (int) }

local key       = KEYS[1]
local limit     = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local now_ms    = tonumber(ARGV[3])
local nonce     = ARGV[4]
local ttl_secs  = 120  -- Contract 2 invariant

-- 1. Drop entries older than the window (score < now_ms - window_ms).
--    Must happen before ZCARD so the count reflects the live window only.
redis.call("ZREMRANGEBYSCORE", key, "-inf", now_ms - window_ms)

-- 2. Count surviving entries.
local count = redis.call("ZCARD", key)

-- 3. Decide.
if count >= limit then
    -- Retry hint: time until the oldest in-window entry exits.
    local oldest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")
    local retry_after_ms = 0
    if oldest[2] then
        retry_after_ms = math.ceil((tonumber(oldest[2]) + window_ms) - now_ms)
        if retry_after_ms < 0 then retry_after_ms = 0 end
    end
    redis.call("EXPIRE", key, ttl_secs)
    return { 0, 0, retry_after_ms }
end

-- 4. Record this request.
--    Member = "<now_ms>:<nonce>" guarantees a unique (score, member) pair even
--    when two requests arrive within the same millisecond.
redis.call("ZADD", key, now_ms, now_ms .. ":" .. nonce)
redis.call("EXPIRE", key, ttl_secs)

return { 1, limit - count - 1, 0 }
