# Prompt and checkpoint capacity evidence

Measured on 2026-08-18 with Python 3.12.10, agent-core 0.6.0
(`d0d14afa912bcc752e0cb16632b1602a110ca55a`), LangGraph 1.2.11, and
`langgraph-checkpoint-sqlite` 3.1.1.

## Prompt artifacts

The largest frozen, composed production prompt is the durable Java planning
case: 8,493 UTF-8 bytes, SHA-256
`b64df16db3267e67085bde62f9a4baf27704e143e6f424b19e488944d394c31e`.
The largest checked-in deterministic metadata fixture is 344 bytes on disk.
A conservative production-identity projection is 471 bytes. Using that larger
sidecar, one turn is 8,964 bytes and a 400-turn ceiling task is 3,585,600 bytes
(3.42 MiB): 0.81% of the 1 MiB per-prompt limit and 0.86% of the 400 MiB
per-task limit.

For 30-day prompt retention, use:

```text
prompt_bytes_30d = tasks_30d * turns_p95 * (prompt_bytes_p95 + metadata_bytes_p95)
```

At the conservative ceiling, 299 tasks fit below 1 GiB and task 300 crosses
it, equivalent to exactly ten ceiling-sized tasks per day over 30 days. The
50%-of-volume crossing is
`ceil(0.5 * volume_bytes / 3,585,600)` tasks. Local fixtures therefore pass
the individual prompt and task gates; beta task volume and turn distribution
are still required for the 30-day gate.

## SQLite checkpoints

Each case compiled a changing JSON-safe normalized result into a real
checkpointed LangGraph loop. Closed database size is the retained capacity
number; live database, WAL, and SHM peaks were also checked.

| Payload | Steps | Closed DB | Elapsed |
| --- | ---: | ---: | ---: |
| 256 KiB | 27 | 14.34 MiB | 0.103 s |
| 256 KiB | 120 | 61.10 MiB | 0.432 s |
| 1 MiB | 27 | 57.14 MiB | 0.366 s |
| 1 MiB | 120 | 243.52 MiB | 1.454 s |

The worst synthetic case reaches 1.90 GiB at eight retained units and crosses
the 2 GiB alert at unit nine. Twenty units project to 4.756 GiB. The earlier
8 KiB-state extrapolation is therefore not a safe sizing basis. Use:

```text
checkpoint_bytes_30d = tasks_30d * units_p95 * checkpoint_bytes(payload_p95, steps_p95)
```

Beta acceptance must record payload size, super-step count, units per task,
30-day task count, retained bytes, and state-volume capacity. Recalibrate the
2 GiB/80% alert from those observed p95 values; do not approve production from
the synthetic fixture alone.
