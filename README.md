# Asynchronous Task Processing API – Design Doc

## 1. Problem Statement

Build a backend service that accepts text-processing jobs, executes them asynchronously, and exposes endpoints to submit jobs, poll status, list jobs, and fetch results. The system must be robust to message queue outages, support idempotency, and be ready for multi-tenant and LLM use-cases.

## 2. Goals

-   Low latency for job submission and status polling
    
-   Horizontally scalable workers and API servers
    
-   Exactly-once task execution with retry safety
    
-   Outbox pattern for reliable queue publishing
    
-   Clear separation of concerns using SOLID and Clean Architecture principles
    
-   Operable in production with strong observability and security
    
-   Initial support for multi-tenant authentication (no granular RBAC)
    

## 3. Non-Goals

-   UI or client SDK
    
-   Real-time streaming updates (WebSockets, SSE)
    
-   Granular RBAC (can be added later)
    

---

## 4. High-Level Architecture (with Outbox Pattern)

-   **API Gateway / Load Balancer** — routes HTTPS traffic to FastAPI pods.
    
-   **FastAPI Service (API Pods)** — validates requests, persists job metadata in Postgres, appends an outbox entry in the same transaction, and returns `task_id`. Accepts requests even if RabbitMQ is down.
    
-   **Outbox Publisher (sidecar or CronJob)** — reads outbox rows with publisher confirms enabled; only marks published = true after receiving the ACK from RabbitMQ. This prevents message loss if the pod crashes right after basic_publish. Ordering is per-task, not global; two updates for the same task serialize through the DB row but may reach workers out of order. For multi-step workflows, consider a `step` field or message versioning.
    
-   **Task Queue (RabbitMQ)** — durable queues for Celery tasks (default, long, dlq_transient, dlq_permanent).
    
-   **Worker Pool (Celery/LLM Workers)** — Celery workers consume tasks, perform text analysis or LLM calls, update job row in Postgres.
    
-   **Relational DB (Postgres)** — single source of truth for job state, payload, results, audit timestamps, and outbox.
    
-   **Object Storage (S3, optional)** — stores large result files, referenced by result_url.
    
-   **Observability Stack** — Prometheus metrics, Grafana dashboards, OpenTelemetry traces, Loki logs.
    
-   **Dead Letter Queues (DLQs)** — `dlq_transient` (ops can auto-replay), `dlq_permanent` (page ops on arrival).
    

> **Rationale:** The outbox pattern ensures API pods remain available and tasks are reliably published to RabbitMQ, even during outages. This enables exactly-once delivery and robust recovery.

### Updated Architecture Diagram

```text
Clients ──▶ ALB/Gateway ──▶ API Pods
                                   │   writes
                                   │   (tx)
                                   ▼
                             Postgres (tasks)
                                   │
                                   │  INSERT outbox_row   ▲
                                   └────────────┬─────────┘
                                                │
                                   Outbox Publisher (sidecar / CronJob)
                                                │ publish
                             RabbitMQ (default | long | dlq_transient | dlq_permanent)
                                                │ consume
                                   Celery / Worker Pods
```

API pod and DB commit a single transaction that both inserts the task row and appends an outbox entry. A lightweight publisher reads un-published rows and emits them to RabbitMQ, marking them published atomically. This guarantees exactly-once even if either part crashes. If RabbitMQ is down, the outbox simply piles up; API remains writable and publisher retries with exponential back-off.

---

## 5. Component Definitions and Rationale

| Component         | Responsibility                | Key Design Points                                      |
|-------------------|------------------------------|--------------------------------------------------------|
| API Gateway       | TLS termination, rate limiting| Offload certs, enforce quotas, per-API-key rate limit  |
| FastAPI Service   | Synchronous HTTP layer        | Pydantic validation, OpenAPI docs, Outbox write        |
| Outbox Publisher  | Reliable queue publishing     | Reads outbox, atomic publish, exponential backoff, per-task ordering only, publisher confirms for ACK safety |
| Celery + RabbitMQ | Task dispatch                 | Multiple queues (default, long, dlq_transient, dlq_permanent), supports retries|
| Worker            | Execute idempotent job logic  | Pure functions, LLM circuit breaker, external_call_done|
| Postgres          | Job metadata, outbox          | ACID guarantees, partitioning, SQL indexing            |
| Object Storage    | Large result storage          | S3, result_url in DB                                   |
| Dead Letter Queues| Catch irrecoverable tasks     | dlq_transient (auto-replay), dlq_permanent (page ops)  |
| Observability     | Metrics, logs, traces         | Detect stuck tasks, alert on error rate, latency probe |

---

## 6. API Design (Delta)

### Headers
- `Idempotency-Key: <uuid>` (optional, max 36 chars, expires after 24h; index on (idempotency_key, created_at) to avoid table bloat)

### POST /tasks
- **Request body:**
```json
{
  "text": "string",
  "language": "en"
}
```
- If the same idempotency key appears again and the prior task is non-terminal, return `409 Conflict` with the original `task_id`.

### GET /tasks/{id}

**Response:**
```jsonc
{
  // Task status and timestamps
  "task_id"    : "uuid",
  "status"     : "PROCESSING",
  "queued_at"  : "ISO8601",
  "started_at" : "ISO8601",
  "finished_at": null,

  // Result and error
  "result_url" : null,
  "error"      : null
}
```

### GET /tasks
- Query params: `status`, `limit`, `cursor`, `created_since`, `created_until`.

### Error Envelope
All errors use a uniform envelope:
```json
{
  "code": "TASK_NOT_FOUND",
  "message": "Task id xyz not found",
  "trace_id": "00-abcd..."
}
```

---

## 7. Asynchronous Processing

| Queue    | TTL    | Use-case                  |
|----------|--------|---------------------------|
| default  | 30 s   | sub-10-second local compute|
| long     | 10 min | LLM or external calls      |

- Workers wrap LLM calls with a shared circuit breaker (e.g., python-breaker) persisted in Redis so all pods share state. In Kubernetes, circuit breaker state can also be stored in a dedicated RabbitMQ exchange to avoid an extra dependency if Redis is only used for this purpose.
- Retries use Celery exponential strategy with jitter.
- Each task row holds `external_call_done` flag so a retry either skips the LLM or reuses cached response, avoiding double charges.
- Use `SELECT ... FOR UPDATE SKIP LOCKED` for quick row pickup.

---

## 8. Data Model

### tasks
```sql
tasks (
  id              UUID PK,
  tenant_id       UUID NOT NULL,
  text            TEXT,
  language        VARCHAR(8),
  status          VARCHAR(10),
  result_url      TEXT,          -- nullable S3 link
  error           TEXT,
  queued_at       TIMESTAMPTZ,
  started_at      TIMESTAMPTZ,
  finished_at     TIMESTAMPTZ,
  fingerprint     CHAR(64) UNIQUE,
  row_version     INT,                 -- renamed for clarity
  external_call_done BOOL DEFAULT false
  -- Results up to 64 KB are stored inline; larger results are offloaded to S3 and referenced by result_url.
) PARTITION BY RANGE (queued_at);

task_events (                       -- audit trail
  id              BIGSERIAL PK,
  task_id         UUID REFERENCES tasks(id),
  old_status      VARCHAR(10),
  new_status      VARCHAR(10),
  actor           TEXT,              -- 'worker', 'api', etc.
  event_time      TIMESTAMPTZ DEFAULT now()
);
```
- Monthly partitions: `tasks_2025_06`, etc. Old partitions detach to cheaper storage.

---

## 9. Technology Stack

| Layer         | Choice         | Reason                                         |
|---------------|---------------|------------------------------------------------|
| Web           | FastAPI        | Async-ready, Pydantic validation, native OpenAPI|
| Web (alt)     | Starlette/WebSockets | For future real-time or streaming needs   |
| Queue         | RabbitMQ       | Multiple queues, at-least-once, Celery integration|
| Worker        | Celery         | Retries, scheduling, large ecosystem           |
| Worker (alt)  | Go + Temporal  | For high-throughput, workflow orchestration    |
| DB            | Postgres       | ACID, partitioning, JSONB, outbox pattern      |
| Object Storage| S3             | Large result storage, result_url               |
| Container     | Docker         | Consistent dev-prod parity                     |
| Infra         | Kubernetes     | Horizontal scaling via HPA, PDBs               |
| Observability | Prometheus, Grafana, Loki, OpenTelemetry | Unified tracing and metrics |

---

## 10. Scalability and Reliability

-   API pods scale behind load balancer; stateless aside from DB.
    
-   Celery workers deployed as K8s Deployment; HPA on queue length and external-service quota.
    
-   RabbitMQ clustered across zones with mirrored queues.
    
-   Postgres primary-replica, monthly partitioning, write traffic small (status updates).
    
-   Outbox publisher retries with exponential backoff if RabbitMQ is down.
    
-   Separate DLQs for transient (`dlq_transient`) vs permanent (`dlq_permanent`) failures.
    
-   Latency probe on queue publish.
    
-   EKS with Pod Disruption Budgets (PDBs) so at least N workers remain during node drain.
    
-   Secrets live in AWS Secrets Manager, referenced via IRSA.
    
-   For continuous secret rotation, a sidecar (external-secrets, secrets-store-csi) syncs keys from Secrets Manager to mounted volumes; the application reads by path, eliminating the need to restart pods on each rotation.
    

---

## 11. Containerization and Deployment

-   **Dockerfile concerns:** multi-stage build, non-root user, Poetry or pip-tools, `python -OO -m pip install -r requirements.txt` to strip docstrings, healthcheck CMD.
    
-   In K8s:
    
    -   API Deployment with rolling updates.
        
    -   Worker Deployment with higher CPU limits.
        
    -   Outbox Publisher as sidecar or CronJob.
        
    -   ConfigMap for stop-word list, Secrets for DB creds and API keys.
        
    -   RabbitMQ via Helm chart with persistence.
        

---

## 12. Testing Strategy and Observability

-   **Unit tests:** validate text analytics, outbox serialization.
    
-   **Contract tests:** for API endpoints using test client.
    
-   **Integration tests:** docker-compose with Postgres, RabbitMQ, verify trace propagation.
    
-   **E2E tests:** bring worker replica count to zero, submit task, scale workers back, assert task completes (scale-to-zero and recovery).
    
-   **Load tests:** measure p95 and p99 task completion.
    
-   **Chaos test:** pause RabbitMQ, post tasks, resume, assert all tasks publish and run exactly once.
    
-   **Metrics:** queue depth, publish latency, DLQ size, circuit breaker open ratio.
    
-   **Tests assert Prometheus metrics and OTEL traces.**
    

---

## 13. Monitoring and Logging

-   Metrics: request latency, error rate, queue depth, worker runtime, task success ratio, retries, publish latency, circuit breaker open ratio, count of task_events by transition type (audit).
    
-   Structured JSON logs with correlation id injected at request entry.
    
-   Traces spanning API -> outbox publish -> queue -> worker execute -> DB update.
    
-   Alerts on high failure rate, growing dead letter queue, p95 latency.
    

---

## 14. Security Considerations

-   HTTPS everywhere, HSTS headers.
    
-   JWT bearer token on every API call; RBAC scopes, tenant_id in claims.
    
-   Gateway rate limiting by API key.
    
-   Input size limits (e.g. 256 KB) validated before DB insert.
    
-   Request payload hard cap (256 KB).
    
-   Secrets in K8s Secrets or AWS Secrets Manager tied to IAM roles.
    
-   SQL injection prevented by parameterised queries via ORM.
    
-   RBAC checked in SQL query.
    

---

## 15. Future Extensions

-   `DELETE /tasks/{id}` triggers Celery revoke for PENDING jobs.
    
-   Webhook: POST with HMAC-SHA256 signature header using per-tenant secret.
    
-   Data retention: configurable days before archival or purge.
    
-   Task cancellation and retention policy.
    

---

## 16. ASCII Architecture Diagram

```text
Clients ──▶ ALB/Gateway ──▶ API Pods
                                   │   writes
                                   │   (tx)
                                   ▼
                             Postgres (tasks)
                                   │
                                   │  INSERT outbox_row   ▲
                                   └────────────┬─────────┘
                                                │
                                   Outbox Publisher (sidecar / CronJob)
                                                │ publish
                             RabbitMQ (default | long | dlq_transient | dlq_permanent)
                                                │ consume
                                   Celery / Worker Pods
```

API pod and DB commit a single transaction that both inserts the task row and appends an outbox entry. A lightweight publisher reads un-published rows and emits them to RabbitMQ, marking them published atomically. If RabbitMQ is down, the outbox simply piles up; API remains writable and publisher retries with exponential back-off.
