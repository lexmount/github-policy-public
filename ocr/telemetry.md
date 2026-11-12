# Policy client audit logs

The client writes `ocr-policy-telemetry/v1` JSON lines to the existing Actions job log. It sends no audit callback and requires no extra secret, workflow permission or dependency. The review submission endpoints, 90-second deadline, four-attempt HTTP retry limit and exit behavior are unchanged.

Each invocation emits a sequence containing `ocr_client_started` after validating the PR snapshot, `ocr_gateway_request` for each HTTP attempt, and one `ocr_client_result` on normal completion or a handled failure. Every record carries a UTC timestamp, repository/run/attempt context and sequence number. Validated snapshots add `request_id`, PR number, head/base SHAs and base branch. Credentials and response bodies are excluded.

Final outcomes:

| Outcome | Meaning |
| --- | --- |
| `accepted` | A validated response confirmed durable backend acceptance; this does not mean the review passed. |
| `skipped` | The validated response explicitly says `repository_not_enabled`. |
| `timeout` | The client's overall ACK deadline expired (`ack_deadline_exceeded`). |
| `failed` | Configuration, transport, retry exhaustion or response validation failed. This is not automatically an ACK timeout. |

A request with `error_code=http_timeout` is a single HTTP timeout. Later retries may still obtain a valid ACK. A v2 durable response can be accepted even when the independent review outcome is failed. The final result is written before the optional step summary, so a summary-write failure does not erase a confirmed ACK.

Cancellation, runner termination or lost output can prevent a final line. Collectors must retain these as cancelled/unknown, never infer an ACK timeout from a failed/timed-out Actions conclusion or human-readable output. Logging errors do not change the acceptance result.

The independent ReviewHub collector reads completed job logs through the GitHub Actions API, validates the snapshot and run attempt, then writes PostgreSQL. Its App needs Actions read permission on target repositories; this is separate from this workflow's `permissions: {}`. Existing historical logs do not acquire structured results retroactively. Client and collector rollout need a real target-repository run to verify the path.

Run `python3 ocr/render_workflow.py` and `python3 -m unittest discover -s ocr -p 'test_*.py'` after editing the client. The generated YAML must stay in sync.
