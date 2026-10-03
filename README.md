# Arabic AI Agent Platform

An Arabic-first **Agent Operating Platform** — not a chatbot.

The user gives a *goal*, not a message. The platform plans it, decomposes it into
steps, selects and executes tools, observes the results, verifies the outcome,
and recovers from failure — pausing for human approval on sensitive operations.

```
User → Goal → Plan → Tools → Execute → Observe → Verify → Result
```

The value of the product lives in the runtime: the agent loop, the tool system,
the permission layer, the task engine, memory, integrations and the model
gateway. The LLM is a reasoning engine behind an abstraction — swappable for a
cloud, free or local model without touching the agent core.

## Status

This repository is the **foundation phase**. It is real, runnable code with a
tested core — not a mockup. What is implemented and what is deliberately left
for later is listed in [`TODO.md`](TODO.md); nothing pretends to work.

- ✅ Agent loop with bounded recovery (retry / repair / replan / skip / abort)
- ✅ Tool abstraction + registry, with real sandboxed filesystem and shell tools
- ✅ Permission / policy engine with allow / deny / requires-approval
- ✅ Human-in-the-loop approvals as first-class, audited entities
- ✅ Task engine with durable, resumable plans
- ✅ Memory layer with a write policy (secrets never persisted)
- ✅ Context manager with a script-aware (Arabic-aware) token budget
- ✅ Model gateway with retry, fallback and usage accounting
- ✅ Audit log with secret redaction
- ✅ Multi-tenant identity model and API-key authentication
- ✅ Local/Cloud runtime boundary and integration layer
- ⏳ Postgres/Redis persistence, desktop runtime, workflow triggers, document
  and media tools, social/ERP connectors (see `TODO.md`)

## Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full design. Repository rules
for contributors and agents are in [`AGENTS.md`](AGENTS.md).

## Quick start

```bash
# 1. Install
uv venv && uv pip install -e ".[dev]"

# 2. Configure a model (cloud or local). Never commit secrets.
export AAP_OPENAI_API_KEY=...          # cloud
# or point at a local OpenAI-compatible server (Ollama, vLLM, llama.cpp):
# export AAP_LOCAL_MODEL_BASE_URL=http://localhost:11434/v1

# 3. Run the API
uv run agent-platform
```

Health check:

```bash
curl localhost:8000/health
```

## Development

```bash
uv run pytest          # tests
uv run mypy            # type checking (strict)
uv run ruff check .    # lint
uv run ruff format .   # format
```

## Security

- No secrets in source or Git. Configuration is environment-only (`AAP_*`).
- Deny-by-default policy; sensitive tools require explicit approval.
- All tool and audit payloads are redacted before they are stored or logged.
- Filesystem and shell tools are sandboxed to the run workspace.

## License

Proprietary. All rights reserved.
