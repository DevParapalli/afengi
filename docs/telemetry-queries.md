# Telemetry queries — request-flow reconstruction & cost

Reusable SQL for the two telemetry tables (`telemetry_api_request`, `telemetry_model_call`).
PostgreSQL syntax; Oracle notes at the bottom. Bind params shown as `:name`.

> **Cross-agent assumption:** reconstructing a flow that spans multiple agents only
> works if every agent writes telemetry to the **same** database (point each agent's
> `DB_URL` at one telemetry store) or you aggregate them externally. Within a single
> agent it always works.

## The columns that matter

| Field | Meaning |
| --- | --- |
| `correlation_id` | The **case** id (contextId / sharedStateId). Threads one case across every agent + re-run. Indexed. |
| `app_meta->>'flow_id'` | One **handoff chain** (a single invocation of a case). Seeded by the root request, propagated to every hop. |
| `parent_request_id` | The **calling agent's** request span (`NULL` = root). The precise edge; indexed. |
| `caller_app` | The calling agent's `app_name` (the parent node). |
| `app_name` | This node. `caller_app -> app_name` = one edge. |
| `app_meta->>'hop'` | Handoff depth (0 = root). |
| `trace_id` | Reserved for an OpenTelemetry trace id (mapped in later — do not overload). |
| `telemetry_model_call.request_id` | Soft-FK to `telemetry_api_request.id`; join for token usage. |

---

## Flow reconstruction

### Whole flow, in order — who called whom, when
```sql
SELECT c.created_at,
       COALESCE(p.app_name, '(root)') AS caller,
       c.app_name                     AS callee,
       c.method, c.outcome, c.duration_ms,
       (c.app_meta->>'hop')::int      AS hop
FROM telemetry_api_request c
LEFT JOIN telemetry_api_request p ON c.parent_request_id = p.id
WHERE c.app_meta->>'flow_id' = :flow_id
ORDER BY c.created_at;
```

### The call tree (recursive, indented)
```sql
WITH RECURSIVE flow AS (
  SELECT id, parent_request_id, app_name, created_at, 0 AS depth
  FROM telemetry_api_request
  WHERE parent_request_id IS NULL
    AND app_meta->>'flow_id' = :flow_id
  UNION ALL
  SELECT r.id, r.parent_request_id, r.app_name, r.created_at, f.depth + 1
  FROM telemetry_api_request r
  JOIN flow f ON r.parent_request_id = f.id
)
SELECT repeat('  ', depth) || app_name AS tree, created_at, depth
FROM flow
ORDER BY created_at;
```

### Find a case's flows (each invocation of a case)
```sql
SELECT DISTINCT app_meta->>'flow_id' AS flow_id, MIN(created_at) AS started
FROM telemetry_api_request
WHERE correlation_id = :case_id
GROUP BY app_meta->>'flow_id'
ORDER BY started DESC;
```

### Everything ever routed to a given agent
```sql
SELECT r.created_at, r.correlation_id, r.caller_app, r.outcome, r.duration_ms
FROM telemetry_api_request r
WHERE r.app_name = :agent
ORDER BY r.created_at DESC;
```

---

## Token usage & cost

### Tokens per hop within one flow
```sql
SELECT r.app_name,
       (r.app_meta->>'hop')::int          AS hop,
       COUNT(m.id)                         AS model_calls,
       COALESCE(SUM(m.input_tokens), 0)    AS in_tok,
       COALESCE(SUM(m.output_tokens), 0)   AS out_tok
FROM telemetry_api_request r
LEFT JOIN telemetry_model_call m ON m.request_id = r.id
WHERE r.app_meta->>'flow_id' = :flow_id
GROUP BY r.app_name, r.app_meta->>'hop', r.id
ORDER BY MIN(r.created_at);
```

### How much did one case cost (tokens)
```sql
SELECT r.correlation_id,
       COUNT(DISTINCT r.id)                       AS requests,
       COUNT(m.id)                                AS model_calls,
       COALESCE(SUM(m.input_tokens), 0)           AS in_tok,
       COALESCE(SUM(m.output_tokens), 0)          AS out_tok,
       COALESCE(SUM(m.input_tokens + m.output_tokens), 0) AS total_tok
FROM telemetry_api_request r
LEFT JOIN telemetry_model_call m ON m.request_id = r.id
WHERE r.correlation_id = :case_id
GROUP BY r.correlation_id;
```

### How much did one case cost (dollars)
Edit the `pricing` table to your rates (USD per 1,000,000 tokens); handles a case that
used several models at different rates.
```sql
WITH pricing (model_name, in_per_m, out_per_m) AS (
  VALUES
    ('gpt-5-nano', 0.05::numeric, 0.40::numeric)   -- <- your rates ($ / 1M tokens)
),
per_model AS (
  SELECT r.correlation_id, m.model_name,
         SUM(m.input_tokens)  AS in_tok,
         SUM(m.output_tokens) AS out_tok
  FROM telemetry_api_request r
  JOIN telemetry_model_call m ON m.request_id = r.id
  WHERE r.correlation_id = :case_id
  GROUP BY r.correlation_id, m.model_name
)
SELECT pm.correlation_id,
       ROUND(SUM(pm.in_tok * p.in_per_m / 1e6
               + pm.out_tok * p.out_per_m / 1e6), 4) AS usd
FROM per_model pm
JOIN pricing p USING (model_name)
GROUP BY pm.correlation_id;
```

### Most expensive cases (leaderboard)
```sql
SELECT r.correlation_id,
       COALESCE(SUM(m.input_tokens + m.output_tokens), 0) AS total_tok,
       COUNT(m.id) AS model_calls,
       MIN(r.created_at) AS first_seen
FROM telemetry_api_request r
LEFT JOIN telemetry_model_call m ON m.request_id = r.id
GROUP BY r.correlation_id
ORDER BY total_tok DESC
LIMIT 20;
```

### Spend per agent (which node burns the tokens)
```sql
SELECT r.app_name,
       COUNT(m.id)                                        AS model_calls,
       COALESCE(SUM(m.input_tokens + m.output_tokens), 0) AS total_tok
FROM telemetry_api_request r
JOIN telemetry_model_call m ON m.request_id = r.id
GROUP BY r.app_name
ORDER BY total_tok DESC;
```

### Spend per day
```sql
SELECT date_trunc('day', m.created_at) AS day,
       SUM(m.input_tokens)  AS in_tok,
       SUM(m.output_tokens) AS out_tok
FROM telemetry_model_call m
GROUP BY 1
ORDER BY 1 DESC;
```

---

## Oracle notes

`app_meta` is a CLOB of JSON on Oracle (not JSONB). Replace the PostgreSQL JSON
operators:

- `app_meta->>'flow_id'`  ->  `JSON_VALUE(app_meta, '$.flow_id')`
- `(app_meta->>'hop')::int`  ->  `TO_NUMBER(JSON_VALUE(app_meta, '$.hop'))`
- `date_trunc('day', x)`  ->  `TRUNC(x)`
- `repeat(s, n)`  ->  `RPAD(' ', n*2)` (or `LPAD`)
- `x::numeric` / `1e6` literals  ->  plain numerics; `CAST(... AS NUMBER)`
- `LIMIT n`  ->  `FETCH FIRST n ROWS ONLY`

Recursive CTE syntax is the same (`WITH ... AS ( ... UNION ALL ... )`); drop the
`RECURSIVE` keyword on Oracle.
