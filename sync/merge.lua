-- G-Counter max-merge for one region slot.
-- KEYS[1] = rl:global:{tier}:{user_id}:{window_id}
-- ARGV[1] = region slot name ("us"|"eu"|"asia")
-- ARGV[2] = incoming absolute value (int)
-- ARGV[3] = ttl_seconds (e.g. 120)
--
-- Applies max(current, incoming) to the named slot.
-- Sending absolute values (not deltas) makes this idempotent and
-- safe to re-apply from reconciliation or duplicate pub/sub messages.

local current  = tonumber(redis.call('HGET', KEYS[1], ARGV[1])) or 0
local incoming = tonumber(ARGV[2])

if incoming > current then
  redis.call('HSET',   KEYS[1], ARGV[1], incoming)
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
  return incoming
end
return current
