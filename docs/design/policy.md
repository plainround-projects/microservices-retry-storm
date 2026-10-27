# The resilience policy of the chain, on one page (lesson step 1)

Decided before any code: for every hop, the timeout, whether it retries, and what it returns when its dependency is down; and the end-to-end deadline the gateway gives a request. `tests/system/test_policy_design.py` reads the table below and checks it against the numbers in the code, so this page cannot drift from what the services do.

## The table

<!-- policy-table:begin -->
| Hop | Timeout (ms) | Retries | When the dependency is down |
| --- | --- | --- | --- |
| gateway to search | 1800 | yes: up to 3 attempts, exponential backoff with full jitter, retry budget of 10 percent | the client gets 503 with `Retry-After: 1` |
| search to pricing | 1200 | no | a partial result flagged `degraded` (sku, no price) |
| pricing to stock | 700 | no | the last known price flagged `stale`, or the base price when there is none |
<!-- policy-table:end -->

End-to-end deadline, set by the gateway and sent down in `X-Deadline-Ms`: **2000 ms**. Margin kept by each hop for its own work: **50 ms**. A hop refuses a request when less than **100 ms** of useful time is left.

## Why these numbers hold together

- The gateway's timeout to search (1800) is below the deadline (2000), so a request that reaches the gateway's last attempt still has a margin to answer.
- Search gets at most what is left of the deadline and never more than 1200 ms for pricing; 1200 + 50 is below 1800, so search can always answer inside the gateway's timeout.
- Pricing gets at most 700 ms for stock; 700 + 50 is below 1200, so pricing can always answer inside search's timeout.
- Every timeout is also capped by the remaining deadline minus the margin, so a call made late in a request's life gets a smaller timeout, never a bigger one.

## Only one layer retries

Only the gateway retries. Search, pricing and the stock store make one attempt each and answer, with a degraded result instead of an error where a degraded result exists. With a retry budget at the edge, a failure anywhere below adds at most 10 percent to the load below it.

## The profiles that are switched at runtime

| Profile | Gateway | Search | Pricing |
| --- | --- | --- | --- |
| `naive` | 3 immediate attempts, 2 s timeout, no deadline | 3 immediate attempts, 2 s timeout | 3 immediate attempts, 2 s timeout |
| `deadline-retry` | the policy above | one attempt inside the deadline | one attempt inside the deadline |
| `full` | the policy above | the same, plus a concurrency limit (503 with `Retry-After`) | the same, plus a circuit breaker |

The naive profile is the lesson's starting point (`call_naive` in the lesson's code retries three times in a row with a 2 second timeout and no backoff); the table's policy is its fix.
