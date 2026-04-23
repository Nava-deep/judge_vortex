## Judge Vortex Benchmark: 1024 Users / 1024 Submissions

- Date: 2026-04-23
- Target: `http://127.0.0.1:53562`
- Harness: `scripts/run_http_submission_benchmark.py`
- Fixture: `benchmark_results/jv_benchmark_fixture_20260423T062100Z_local.json`
- Mode: bounded concurrency benchmark with `--max-concurrency 64`

### Results

- Candidates targeted: `1024`
- Submissions targeted: `1024`
- Accepted submissions: `1024`
- Submission failures: `0`
- Final passed submissions: `1024`
- Acceptance wall time: `16.1s`
- Submission ingest rate: `63.6/s`
- Average submit latency: `989.41ms`
- P95 submit latency: `1245.44ms`
- P99 submit latency: `1345.41ms`
- Average execution time: `31.79ms`
- P95 execution time: `47.0ms`
- P99 execution time: `64.54ms`
- Average end-to-end time: `102.62s`
- P95 end-to-end time: `193.44s`
- P99 end-to-end time: `201.43s`

### Queue Drain Snapshot

- Around `30s` into the run: `474` passed / `550` pending
- Around `60s` into the run: `698` passed / `326` pending
- Around `90s` into the run: `911` passed / `113` pending
- Final: `1024` passed / `0` pending

### Topology

- Executor services: `2`
- Kafka partitions: `8`
- Sandbox slots: `32`
- Supported languages: `11`

### Notes

- This benchmark ran on the current codebase with the real Django -> Kafka -> executor submission path.
- The harness was updated to support bounded concurrency so we could measure a sustained 1024-user wave instead of an all-at-once burst that mostly exercised throttling.
