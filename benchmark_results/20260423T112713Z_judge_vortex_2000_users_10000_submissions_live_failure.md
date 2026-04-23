# Judge Vortex Live Benchmark Failure

- Run id: `20260423T112713Z_judge_vortex_2000_users_10000_submissions_live_failure`
- Target: `https://judgevortex.duckdns.org`
- Scenario: `2000` concurrent users, `5` judged submissions each, `10000` total submissions
- Deployment under test: single `t3.small` EC2 node with Kafka, Postgres, Redis, executor containers, ConfigControl, DistributedRateLimiter, Prometheus, and Grafana

## Result

The live deployment overloaded before the benchmark completed.

- Public HTTPS health checks timed out.
- Public login page timed out.
- SSH timed out during banner exchange.

## Last Healthy Snapshot Before The Burst

- Free memory: `120 MB`
- Load average: `0.21 / 0.13 / 0.12`
- `judge-vortex`: `active`
- `nginx`: `active`

## Conclusion

The current single-node `t3.small` deployment does **not** cleanly maintain `2000` active users sending `10000` judged submissions as one burst.

## Useful Baseline

The previous stable benchmark result in `20260419T123036Z_judge_vortex_1024_join_submit_stable_manual.json` shows what the stack handled before this larger burst:

- `1024` candidates targeted
- `1024` accepted submissions
- `1024` final passed submissions
- Submission ingest rate: `35.52/s`
- Average execution time: `13.49 ms`
- Average end-to-end time: `61.82 s`

## Interpretation

This points to an infrastructure bottleneck more than a judging-logic bottleneck:

- execution time for individual jobs is low
- the single EC2 node has very little spare RAM
- Kafka, Postgres, Redis, Grafana, Prometheus, web, and executors are all sharing the same `2 vCPU / 2 GB` machine
- when the request burst is too large, the node becomes unreachable instead of degrading gracefully
