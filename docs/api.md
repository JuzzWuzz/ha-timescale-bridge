# WebSocket API v1

Use HA's authenticated `/api/websocket` connection (the later card can call `hass.callWS`).
HA's normal request IDs and result/error envelope apply. Only active authenticated HA users are
accepted. Unknown fields, including SQL, hosts and credentials, are rejected by the HA schema.
`api_version: 1` appears in successful results. Series IDs are local to this database; do not
invent or persist IDs across database rebuilds without revalidating the catalog.

## Catalog

```json
{"id":1,"type":"timescale_bridge/catalog","after_id":0,"limit":200}
```

Result:

```json
{"api_version":1,"series":[{"id":1,"device":"meter_a","metric":"energy","subtype":""}],"next_after_id":null}
```

Entries come directly from `public.devices_metrics`, ordered by ID. `limit` defaults to 200,
maximum 500; `after_id` defaults to zero. Continue using `next_after_id` until null. Catalog pages
are fresh reads, not a snapshot. Build dependent device/metric/subtype dropdowns from these records.
An empty subtype is meaningful. No unit metadata is invented: the documented table has no units.

## Historical query

```json
{"id":2,"type":"timescale_bridge/query","series_ids":[1,2],"start":"2026-11-21T22:00:00Z","end":"2026-11-22T22:00:00Z","preset":"day"}
```

`start` is inclusive; `end` is exclusive. Timestamps must have an explicit offset and be in
1970–2100. Supply 1–8 distinct catalog IDs. Results preserve requested series order. Unknown IDs
fail the entire request; no silent omission or fallback.

| Preset | Maximum span | Resolution/source |
|---|---:|---|
| `1h` | 1 hour | 1-minute raw buckets |
| `3h` | 3 hours | 2-minute raw buckets |
| `6h` | 6 hours | 5-minute raw buckets |
| `12h` | 12 hours | 15-minute raw buckets |
| `day` | 24 hours | `cagg_metrics_thirty_mins` |
| `week` | 7 days | Require `resolution: "1h"` or `"1d"`; hourly/daily view |
| `month` | 31 days | `cagg_metrics_daily` |
| `year` | 366 days | `cagg_metrics_monthly` |

For other presets, optional `resolution` must match the fixed selection (`1m`, `2m`, `5m`, `15m`,
`30m`, `1d`, `1mo`). The week metric mapping is deliberately not guessed. A future verified
mapping can remove the need for the caller's explicit choice. Presets select a source and maximum
span; they do not calculate dates. Month/year ranges use calendar boundaries, not fixed 30/365-day
approximations. All rollup boundaries must align to whole buckets in `Africa/Johannesburg`.
Unaligned rollup ranges are rejected, not rounded or partially attributed. Rolling raw queries
allow arbitrary boundaries and mark clipped edge buckets.

A Johannesburg civil day is midnight to the next midnight in **that zone**, regardless of browser
zone. The example is November 22 in Johannesburg, represented as 22:00Z November 21 to 22:00Z
November 22. The adapter must explicitly calculate that window; the backend cannot detect a
browser that accidentally sends its own local-midnight window. Equivalent `+02:00` timestamps
produce the same result. Calendar conversion in real browsers remains an adapter acceptance test.

Result fields:

- `api_version`, normalized UTC `start`/`end`, `timezone`, `resolution`, `source` (`raw`/`rollup`).
- `energy_semantics: "legacy_bucket_delta"`; no whole-window energy total is calculated.
- `completeness: "unknown"`: row presence does not establish completeness or replica freshness.
- `series`: catalog fields plus `points` and matching `coverage` arrays.
- `points`: ordered `[epoch_ms, value_or_null]` pairs, timestamped at bucket start. Raw edge buckets
  may begin before `start`, but their aggregates only include samples inside `[start,end)`.
- `coverage`: one entry per point with `state`, `sample_count`, `live`, and `clipped`.

Coverage states:

| State | Meaning |
|---|---|
| `missing` | No row, or zero sample count; value is null, never fabricated zero |
| `partial` | Observations exist but bucket is clipped, not yet closed, or aggregate is null |
| `observed` | Closed whole bucket has a value; **not proof of full sampling coverage** |

A numeric zero stays zero. Missing buckets are explicitly emitted as nulls. `live` means the bucket
ends after the server's current time; it does not certify real-time aggregate freshness. The server
cannot detect gaps within an observed bucket without expected sampling cadence and watermark
information. Never label `observed` as “complete” or sum nulls as zero in the adapter.

## Resource bounds and errors

Four active API requests, two database connections, five-second acquisition/connect timeout,
ten-second database statement timeout, fifteen-second per-read timeout, thirty-second total
request timeout, and 2,048 points per series. Excess active requests return `busy`; oversized
responses fail rather than truncate. Values are bound parameters; table names are internal allowlists.
Series reads use separate transactions and are not guaranteed to share one replica snapshot.

Bridge errors: `invalid_request`, `unknown_series`, `busy`, `replica_required`,
`replica_unavailable`, `timeout`, `too_many_points`, `invalid_data`, `unauthorized`, `not_ready`.
HA additionally returns normal `invalid_format` errors. Database exception text is not returned.
No automatic primary, raw-data, or MQTT fallback is performed.

## Electricity topology

An active authenticated HA user can send `{"id":1,"type":"timescale_bridge/topology"}`. The HA success envelope contains a result like the following.

```json
{"api_version":1,"meters":[{"series_id":1,"label":"Feed A","parent_id":null},{"series_id":2,"label":"Feed B","parent_id":null},{"series_id":4,"label":"Lighting","parent_id":2}]}
```

Each null parent identifies an independent grid feed. Other parents are catalog series IDs. Reconstruct the forest by ID regardless of list order. An empty list means no meters have been configured. Use the existing query command with a shared date range to obtain measurements. Root totals already include downstream usage, so summing every level would double count. Meter writes go through HA's permission-checked configuration UI.
