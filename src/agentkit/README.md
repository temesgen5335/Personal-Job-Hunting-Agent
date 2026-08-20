# agentkit

A domain-agnostic agent harness: multi-provider LLM access with capability-aware
routing, governed tools, permissioned confirmations, fenced retrieval, and a
fail-closed audit trail.

**It knows nothing about the application it serves.** `agentkit` may not import a host
package, and three tests enforce that — an import boundary, a vocabulary boundary, and
an import-cycle check. Copy the directory into another project and it works; the only
dependency is the standard library plus, lazily, whichever provider SDK you actually call.

---

## Install

```bash
pip install openai        # for any OpenAI-compatible provider (most of them)
pip install anthropic     # only if you use Anthropic
```

Then vendor `src/agentkit/` or add it to your path. There is no package on PyPI; it is
small enough to copy, which is the point.

---

## 1. The five-minute version

```python
from types import SimpleNamespace
from agentkit.llm.service import LLMService

cfg = SimpleNamespace(groq_api_key="gsk_...", llm_provider="groq")

svc = LLMService.from_settings(cfg)
svc.preflight()                                   # optional; see §4
print(svc.complete("You are terse.", "Name three languages."))
```

`from_settings` reads attributes off **any** object — a pydantic `Settings`, a
dataclass, an `argparse.Namespace`, a `SimpleNamespace` built from `os.environ`. It
never imports your config class.

```python
import os
from types import SimpleNamespace
cfg = SimpleNamespace(**{k.lower(): v for k, v in os.environ.items()})
```

---

## 2. Providers and configuration

A provider appears in the chain when its key is present. Nothing else is required —
adding a key is the whole integration.

| Provider | Key attribute | Model attribute | Default model |
|---|---|---|---|
| `groq` | `groq_api_key` | `groq_model` | `openai/gpt-oss-20b` |
| `cerebras` | `cerebras_api_key` | `cerebras_model` | `llama-3.3-70b` |
| `gemini` | `gemini_api_key` | `gemini_model` | `gemini-flash-latest` |
| `github` | `github_models_token` | `github_models_model` | `openai/gpt-4o-mini` |
| `openrouter` | `openrouter_api_key` | `openrouter_model` | `openai/gpt-oss-20b:free` |
| `qwen` | `qwen_api_key` | `qwen_model` | `qwen-plus` |
| `custom` | `custom_llm_api_key` | `custom_llm_model` | — (needs `custom_llm_base_url`) |
| `openai` | `openai_api_key` | `openai_model` | `gpt-4o-mini` |
| `anthropic` | `anthropic_api_key` | `anthropic_model` | `claude-sonnet-4-6` |

Two more attributes are read if present:

| Attribute | Meaning |
|---|---|
| `llm_provider` | Name of the preferred provider. Ordering only — see the note below. |
| `custom_llm_base_url` | Enables the `custom` provider (Ollama, vLLM, LiteLLM, any OpenAI-compatible server). No key needed. |

Default fallback order: `groq → cerebras → gemini → github → openrouter → qwen → custom
→ openai → anthropic`. Free and fast first, so a paid key is a deliberate escalation
rather than a surprise on the bill.

> **`llm_provider` orders, it does not admit.** With the `Runner` (§5), a preferred
> provider that cannot do the task — no tool support, context too small — is skipped
> with a reason rather than silently attempted. Routing by capability beats routing by
> preference, because the failure mode of the latter is a quietly worse answer.

### Local models

```python
cfg = SimpleNamespace(custom_llm_base_url="http://localhost:11434/v1",
                      custom_llm_model="llama3.2")
```

### Your own provider table

```python
from agentkit.llm.chain import ProviderSpec
from agentkit.llm.service import LLMService

spec = ProviderSpec("housebrand", "house_key", "house_model",
                    "https://llm.internal/v1", default_model="house-1")
svc = LLMService.from_providers([spec], cfg, order=("housebrand",))
```

`ProviderSpec` fields: `name`, `key_field`, `model_field`, `base_url`, `kind`
(`"openai"` | `"anthropic"`), `default_model`, `base_url_field`, `requires_key`.

---

## 3. Knowing why a provider is missing

```python
svc = LLMService.from_settings(cfg)
print(svc.chain)      # ['groq', 'gemini']
print(svc.skipped)    # [('cerebras', 'no cerebras_api_key'), ...]
print(svc.describe()) # chain, skip reasons, and measured health together
```

"No chain" and "no `GROQ_API_KEY`" are the same symptom with very different fixes, so
the reason always travels with the result.

---

## 4. Pre-flight and the trace ledger

Failover is *reactive*: it discovers a dead provider by calling it and waiting. A chain
with two dead backends in front pays both timeouts on every request, and the only
symptom is latency — which nobody watches. Two dead model slugs survived weeks here for
exactly that reason.

```python
report = svc.preflight(timeout_s=30)     # concurrent: one small real call each
print(report.render())
```

```
probed 3 backend(s) in 9.1s
  ✓ groq/openai/gpt-oss-20b  0.84s
  ✓ openrouter/openai/gpt-oss-20b:free  4.16s
  ✓ gemini/gemini-flash-latest  9.06s
```

Afterwards the chain is ordered by **measured latency**, fastest first. Reordering
changes sequence, never membership — a probe is a snapshot, not a verdict, so this can
never leave you with an empty chain even if every probe fails.

The **ledger** records every call, probe or real:

```python
print(svc.ledger.render())
svc.ledger.working()     # [('groq', 'openai/gpt-oss-20b')]
svc.ledger.broken()      # [('gemini', 'gemini-2.0-flash')]
svc.ledger.as_dict()     # serialisable: per-verdict counts, latencies, last error
svc.ledger.events("groq")  # the last 50 attempts for one backend
```

```
✓ groq/openai/gpt-oss-20b     100%  0.84s    3 call(s)
~ gemini/gemini-flash-latest   50%  4.10s    2 call(s)  — rate_limit: 429 quota exceeded
✗ openrouter/gpt-oss-20b:free    0%  0.31s    1 call(s)  — capability: model_not_found
```

Health is one of `ok` · `degraded` · `dead` · `untried`. An untried backend reports a
success rate of `None`, **not** 100% — a vacuous rate is how a provider nobody has
called reads as healthy.

**Costs**: one small completion per provider, run concurrently, so a probe takes as long
as the slowest single provider rather than the sum. Worth it before a batch job; skip it
for a single call.

---

## 5. Error classification

Every failure is classified before anything acts on it. This is what makes a 401 and a
429 behave differently instead of both becoming "try the next one".

| Verdict | Meaning | Effect |
|---|---|---|
| `PERMANENT` | Bad key, disabled account | Breaker opens indefinitely; never retried |
| `RATE_LIMIT` | 429 | Honours `Retry-After`, else switches provider |
| `TRANSIENT` | 5xx, timeout, reset | Retried with backoff |
| `BAD_REQUEST` | Our payload is wrong | Retrying changes nothing |
| `CAPABILITY` | Model can't do this / unknown model | Route to a different model |
| `CONTEXT` | Prompt too long for this model | Route to a larger one |
| `CONTENT_FILTER` | Refused by the provider | Surfaced, not retried |
| `UNKNOWN` | Unrecognised | Treated cautiously |

```python
from agentkit.llm.errors import classify
c = classify(exc)      # c.verdict, c.message, c.status, c.retry_after_s
```

Classification is by SDK exception **type name**, so `errors.py` imports with no SDK
installed.

---

## 6. Agentic loops with tools

`LLMService` is the simple door. For tool-using work with capability routing and
degradation strategies, use the `Runner`.

```python
from agentkit.llm.chain import build_chain
from agentkit.llm.runner import Runner
from agentkit.llm.tasks import Budget, TaskSpec
from agentkit.llm.types import ToolSpec
from agentkit.tools import ToolBox

box = ToolBox()
box.register(
    ToolSpec("get_weather", "Current weather for a city.",
             {"type": "object",
              "properties": {
                  # Every property needs a description — the model reads it, and
                  # `validate_tool_schema` rejects the tool at registration without one.
                  "city": {"type": "string", "description": "City name, e.g. Berlin"},
              },
              "required": ["city"]}),
    lambda args: f"{args['city']}: 17C, cloudy",     # takes a dict, returns a STRING
)

runner = Runner(backends=build_chain(cfg), toolbox=box)
outcome = runner.run(TaskSpec(
    name="weather",
    needs_tools=True,
    max_tool_steps=4,
    tools=box.specs(),
    budget=Budget(max_attempts=3, max_tool_calls=12, wall_clock_s=120),
), messages=[...])
```

A tool takes **one dict** of validated arguments and returns a **string** — that string
is what the model reads. A tool that raises becomes an error *result*, not a crash: one
bad tool must not end the turn.

**Tool schemas are deliberately a small subset** — flat objects, primitive properties,
one nesting level, and a `description` on every property. Gemini's OpenAI-compat endpoint and grammar-constrained local servers
reject or silently mangle the rest, and weak models fail on it. `validate_tool_schema`
enforces the subset at registration, where the error is cheap.

### Degradation

Nine executors sit behind one signature. The load-bearing one is
`prefetch_single_shot`: Python runs the plan and the model only writes the answer, so a
model that cannot use a tool *result* still answers correctly. Measured: the same
question answered through a native tool loop on a 70B model and through prefetch on an
8B model that scores 0/5 on loops.

`plans_for()` filters backends by capability **first** and returns a ranked queue that
doubles as the failover queue — so failover can never land on a model that would do the
job badly.

---

## 7. Governed tools

Use this when an LLM can trigger actions with consequences.

```python
from agentkit.audit import Auditor
from agentkit.guard import GuardedToolBox
from agentkit.permissions import Confirm, Gatekeeper, Permission, PolicyBook, ToolPolicy
from agentkit.session import SessionContext, Surface

book = PolicyBook(
    policies={
        "search":      ToolPolicy("search", Permission.READ,  Confirm.NEVER),
        "send_email":  ToolPolicy("send_email", Permission.ACT, Confirm.SESSION),
        "set_config":  ToolPolicy("set_config", Permission.ADMIN, Confirm.ALWAYS,
                                  describes="Change a system setting"),
    },
    excluded=frozenset({"delete_everything"}),
    cost_budget=20,
)

guarded = GuardedToolBox(
    inner=box,
    gate=Gatekeeper(book),
    audit=Auditor(sink=my_sink),
    context=SessionContext(actor="operator", surface=Surface.CLI, run_id="run-1"),
    ask=lambda name, args, policy: input(f"Run {name}? [y/N] ").lower() == "y",
)

runner = Runner(backends=build_chain(cfg), toolbox=guarded)   # same shape as ToolBox
```

`GuardedToolBox` has the **same shape** as `ToolBox`, so it drops straight into the
`Runner` and there is no ungoverned path.

Order inside `execute()` is fixed and load-bearing: **audit intent → allow-list → policy
→ audit decision → run → audit result.** A dead audit sink stops the call *before* the
gatekeeper is asked anything.

| Permission | Meaning |
|---|---|
| `READ` | Observes only |
| `ACT` | Changes something in the world |
| `ADMIN` | Changes the system's own configuration |

| Confirm | Meaning |
|---|---|
| `NEVER` | Runs freely |
| `SESSION` | Confirms once, then remembered for the session |
| `ALWAYS` | Confirms every time |

`costly=True` is orthogonal to permission: a tool can change nothing and still need a
budget, and a prompt would just train the operator to click through.

### Two rules worth internalising

**Exclusion over gating.** A capability the agent must never have is *not registered*.
A gate is a runtime check an attacker defeats once; absence has no code path to attack.
`PolicyBook` unions a universal exclusion set (`execute_sql`, `run_shell`, `http_fetch`,
`eval`, filesystem access) into whatever you pass, so a host cannot drop them by
overwriting the field. They are dangerous precisely because they are *general*: any one
collapses every other restriction into a suggestion.

**The policy input carries no text.** `SessionContext` holds actor, surface, run id and
prior grants — no transcript, no retrieved chunks, no model output. An injection can make
the model *request* a config rewrite; it cannot make the gatekeeper approve one, because
the gatekeeper cannot read it.

`admin_surfaces` decides where ADMIN tools may be approved at all. Leaving
`Surface.CHAT` out of it means a chat interface structurally cannot approve a config
change — the refusal comes from the gatekeeper, not from the chat module remembering.

---

## 8. Retrieval with trust

```python
import sqlite3
from agentkit.knowledge import Chunk, FtsIndex, Trust

index = FtsIndex(sqlite3.connect("app.db"), table="agent_knowledge")
index.rebuild([
    Chunk(doc_id="doc:1", kind="note", title="Onboarding",
          body="…", source="wiki", trust=Trust.INTERNAL, ref="1"),
])
hits = index.search("onboarding", limit=8, min_trust=Trust.INTERNAL)
```

SQLite FTS5, in whatever connection you hand it — nothing extra to deploy.

**Mark third-party text `Trust.UNTRUSTED`.** Retrieved content the model reads is
rendered inside a per-turn nonce fence, so a document saying *"ignore previous
instructions"* arrives labelled as data rather than instruction. Verified against a real
injection: the model reported the directive instead of following it.

Note `rebuild()` is a **full replace**, and the index does not notice deletions in your
source data. If you delete records, reindex — otherwise the agent keeps citing rows that
no longer exist.

---

## 9. Configuration reference

Everything is read off your settings object with `getattr`; nothing is required.

```bash
# Pick any subset. A provider joins the chain when its key is present.
GROQ_API_KEY=            # ~free tier, fastest of the majors
CEREBRAS_API_KEY=        # ~1M tokens/day free
GEMINI_API_KEY=
GITHUB_MODELS_TOKEN=     # a GitHub PAT with `models:read`, not a vendor key
OPENROUTER_API_KEY=
QWEN_API_KEY=
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# Optional per-provider model override; sensible defaults otherwise.
GROQ_MODEL=openai/gpt-oss-20b
GEMINI_MODEL=gemini-flash-latest

# Any OpenAI-compatible server (Ollama, vLLM, LiteLLM). No key needed.
CUSTOM_LLM_BASE_URL=
CUSTOM_LLM_MODEL=

# Preferred provider — ordering only, not admission.
LLM_PROVIDER=groq
```

> **Prefer a `-latest` alias to a pinned version** where the provider offers one.
> A pinned `gemini-2.0-flash` was retired and took the whole provider down; the alias
> survives a rotation. Pinning buys reproducibility and costs availability, and for a
> best-effort failover chain the trade runs the other way.

---

## 10. Module map

| Module | Purpose |
|---|---|
| `llm/service.py` | **`LLMService`** — the door: chain + breaker + ledger + pre-flight |
| `llm/chain.py` | `build_chain()`, `ProviderSpec`, the provider table |
| `llm/probe.py` | Concurrent pre-flight; `order_by_health()` |
| `llm/ledger.py` | Per-backend trace: health, verdict counts, latencies |
| `llm/health.py` | Circuit breaker per (provider, model), injected clock |
| `llm/errors.py` | `classify()` by SDK type name — imports with no SDK |
| `llm/capabilities.py` | `resolve_card()`: measured → family → size → UNKNOWN |
| `llm/router.py` | `plans_for()` / `choose_strategy()` — the failover queue |
| `llm/strategies.py` | Nine executors; `prefetch_single_shot` is load-bearing |
| `llm/runner.py` | Walks the plan queue, classifying before acting |
| `llm/types.py` | `ChatRequest`/`ChatResult` IR + tool-schema validation |
| `llm/jsonx.py` | Tolerant JSON parsing; every rule is an observed failure |
| `tools.py` | `ToolBox` — a raising tool becomes a result, not a crash |
| `guard.py` | `GuardedToolBox` — same shape, so there is no ungoverned path |
| `permissions.py` | Tiers, confirmations, universal exclusions |
| `audit.py` | Fail-closed sink; intent recorded before the policy runs |
| `session.py` | `SessionContext` — valuable for what it *lacks* |
| `knowledge.py` | FTS5 index, trust levels, nonce-fenced rendering |

---

## 11. Testing against it

Backends implement one method:

```python
class FakeBackend:
    name, model = "fake", "m"

    def chat(self, req):                      # ChatRequest -> ChatResult
        from agentkit.llm.types import ChatResult
        return ChatResult(text="hi", tool_calls=(), stop_reason="stop",
                          provider=self.name, model=self.model)
```

`stop_reason` is a plain string (`"stop"`, `"tool_calls"`, `"length"`,
`"content_filter"`, `"other"`) — a `Literal`, not an enum.

**Mirror the real seam rather than your memory of it.** A fake written from memory tests
the memory. `Breaker` and `Ledger` take injectable `now`, so time-dependent behaviour is
tested without sleeping.

---

## 12. Design decisions worth knowing

- **Route before running.** Capability filtering happens *before* the call, so failover
  cannot land on a model that would silently do the job badly.
- **Degrade by changing *how*, not *what*.** A weaker model gets a different strategy,
  not a worse answer.
- **The breaker is in-process and not persisted.** A restart is a legitimate "try
  again", and a persisted PERMANENT verdict would outlive the fixed API key that caused
  it.
- **Ledger and probe never raise.** A diagnostic that takes down the thing it was
  measuring is worse than no diagnostic.
- **A probe sends a real request, not a ping.** "Reply ok" fits in a quota where a real
  prompt does not — and a health check must not clear a provider that cannot serve work.
  Equally, the probe budget must leave room for reasoning tokens: too small a
  `max_tokens` returns an empty string with `stop_reason="length"`, and a naive check
  reads healthy providers as dead.
