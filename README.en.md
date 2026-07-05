# PR Review Agent with Sandbox Execution

[简体中文](README.md) | **English**

An **AI-powered PR review agent** with sandbox execution. It automatically reads GitHub Pull Requests, analyzes code changes, runs tests, lint, and security scans in an isolated environment, identifies verifiable issues, and — when conditions allow — generates sandbox-validated fix suggestions.

> Core positioning: an automated review assistant that helps developers catch high-confidence defects, security risks, regressions, and maintainability issues earlier. It outputs **evidence-backed review results**, not the model's private chain of thought.

## Key Features

- **Change understanding** — reads PR metadata, diff, changed files, and project context to understand the blast radius
- **Static review** — checks logic errors, security issues, boundary conditions, style, and maintainability
- **Dynamic verification** — runs tests, lint, SAST, and minimal reproduction scripts in a sandbox, turning findings into verifiable evidence
- **Conservative fixes** — generates patches only for high-confidence, low-risk, small-scope issues, published after sandbox validation
- **Evidence chain output** — every finding carries code location, diff, trigger condition, reproduction, tool output, and verification result
- **Security isolation** — all untrusted code executes in a network-isolated, resource-limited, short-lived sandbox

## Architecture

```
GitHub Webhook → Webhook Receiver → Orchestrator
                                      ├─ Planner (review plan)
                                      ├─ Security Reviewer (security review)
                                      ├─ Bug Hunter (defect review)
                                      ├─ Test Executor (sandboxed test execution)
                                      ├─ Issue Aggregator (finding aggregation)
                                      ├─ Patch Generator (fix generation & validation)
                                      └─ Review Publisher (review publishing)
                                           ↓
                                    Tool Gateway (unified audit / rate limiting)
                                      ├─ GitHub API Tool
                                      ├─ Sandbox Manager (Docker isolation)
                                      ├─ Analyzer Tool (lint/SAST)
                                      └─ Memory Tool (optional long-term memory)
```

### Review State Machine

```
RECEIVED → VALIDATING_EVENT → PREPROCESSING → PLANNING
  → SANDBOX_PREPARE → STATIC_REVIEW → DYNAMIC_TESTING
  → ISSUE_AGGREGATION → PATCH_GENERATION → PATCH_VALIDATION
  → REVIEW_PUBLISHING → COMPLETED

Exceptional: FAILED | CANCELLED | SUPERSEDED | TIMEOUT | SKIPPED
```

**Idempotency rule**: each task is uniquely identified by `repository_id + pr_number + head_sha`. A duplicate webhook returns the existing task; when a new commit arrives the old task transitions to `SUPERSEDED` and its sandbox is destroyed.

### Review Pipeline Stages

1. **Preprocessing** — verify webhook signature; fetch PR metadata, changed files, unified diff, and file contents
2. **Planning** — detect languages/risk areas, classify the change (docs/tests/code/config/deps), build the task graph and sandbox requirement; docs-only changes go straight to `SKIPPED`
3. **Static review** — Security Reviewer (heuristic rules + in-sandbox SAST + LLM) and Bug Hunter run in parallel
4. **Dynamic verification** — run tests/lint/type check in the isolated sandbox, distinguishing environment / dependency / code failures
5. **Aggregation** — deduplicate, sort by severity and confidence, suppress low-confidence findings lacking corroboration, decide patch eligibility
6. **Patch generation & validation** — generate a minimal fix only for high-confidence, small-scope issues; only patches that pass sandbox validation (`passed`) are published as GitHub suggestions
7. **Publishing** — line-level review comments / suggestions / summary, bound to head SHA, deduplicated, with minimized disclosure for high-severity security issues

### Design Principles

- **Tool-constrained first** — agents never touch GitHub / filesystem / network / execution directly; everything goes through the Tool Gateway (unified audit / rate limiting / validation)
- **Evidence over speculation** — every finding carries code location, type, severity, confidence, evidence, and verification status; low-confidence findings without corroboration are not disguised as certain defects
- **Least privilege & credential isolation** — PR code is treated as untrusted input; tokens / cloud credentials / internal network never enter the sandbox
- **Conservative auto-fix** — committable patches are generated only for high-confidence, small-scope, verifiable issues that do not change external behavior
- **Idempotent & cancellable** — same-key idempotency, new commits supersede old tasks, timeout exit, safe sandbox reclamation on failure

## Quick Start

### Requirements

- Python ≥ 3.10
- Docker (for sandbox execution; optional — dev can fall back to local execution)
- Redis (optional; falls back to in-process memory when unset)
- PostgreSQL (optional; dev defaults to SQLite)

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Configure

```bash
cp .env.example .env
# Edit .env: fill in GitHub App credentials or a PAT, LLM API key, etc.
cp config/config.example.yaml config/config.yaml
# Override defaults as needed
```

Key config options are in `config/default.yaml`.

### Build the Sandbox Image (optional, for dynamic testing)

```bash
./docker/sandbox/build.sh
```

### Start the Service

```bash
pr-review-agent serve              # use .env config
pr-review-agent serve --port 9000  # specify a port
```

### Trigger a Review Manually

```bash
pr-review-agent review --repo org/repo --pr 42 --sha abc123
```

### Check Task Status

```bash
pr-review-agent status --review-id repo-123-pr-42-sha-abc123def456
```

## GitHub App Configuration

Recommended least-privilege permissions:

| Permission | Level | Purpose |
|------|------|------|
| Pull requests | Read & write | Read PRs, publish reviews |
| Contents | Read | Read code |
| Checks | Read & write | Publish check results |
| Metadata | Read | Basic repo metadata |
| Issues | Read & write | Publish PR comments |

Set the Webhook URL to `https://your-host/webhook/github`, Content-Type to `application/json`, and put the secret in `GITHUB_WEBHOOK_SECRET`.

## Sandbox Security

The sandbox follows least-privilege and strong-isolation design:

- **Two-phase network model**: dependency-install phase uses a restricted network (allowlist registry); the test-execution phase has no network at all
- **Container hardening**: non-root, no privileged mode, read-only rootfs, no-new-privileges, dropped capabilities, cgroup resource limits
- **Credential isolation**: GitHub token / cloud credentials / database credentials never enter the sandbox; the code archive is downloaded by the manager (outside the container) and injected in
- **Lifecycle**: ≤30 minutes by default, auto-reclaimed

| Risk level | Isolation |
|---------|---------|
| Low-risk internal repos | Docker + seccomp + AppArmor + cgroup |
| Medium-risk private repos | gVisor or Kata Containers |
| High-risk public repos | Firecracker microVM |

## Project Structure

```
PRReviewAgent/
├── app/
│   ├── main.py              # FastAPI entry point
│   ├── cli.py               # command-line tool
│   ├── config.py            # config loading (YAML + env vars)
│   ├── models/              # Pydantic data models (ReviewTask/Issue/Patch/Plan/Blackboard)
│   ├── db/                  # SQLAlchemy ORM + repository layer
│   ├── orchestrator/        # orchestrator + state machine
│   ├── agents/              # specialized agents
│   ├── tools/               # tool gateway + GitHub/Sandbox/Analyzer/Memory tools
│   ├── blackboard/          # shared state across sub-agents
│   ├── llm/                 # LLM client + prompts
│   ├── webhook/             # GitHub webhook receiver
│   └── logging_setup.py     # structured logging (redacted)
├── config/                  # YAML config
├── docker/sandbox/          # sandbox image Dockerfile
├── tests/                   # test suite
└── pyproject.toml
```

## Testing

```bash
pytest tests/ -v
```

Test coverage: data models, state machine, analyzer, aggregator, config, GitHub tool, LLM client, Memory tool, webhook receiver.

## Degradation Strategy

The system supports partial success:

- Static review succeeds, dynamic testing fails → still publish static findings
- Tests pass, patch fails → publish the issue, do not publish the patch
- SAST fails → keep running lint and tests
- Memory unavailable → do not block the review
- GitHub suggestion unavailable → fall back to a patch comment

## License

MIT
