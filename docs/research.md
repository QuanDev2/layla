# Research — Multi-Agent Architecture Sources

Raw synthesis from three external sources consulted while designing Layla's
agent layer. Reference material, not a spec — GOALS.md/SPEC.md hold decisions;
this holds the evidence behind them.

---

## Source 1: LangChain/LangGraph — Multi-Agent-AI-System

`github.com/FareedKhan-dev/Multi-Agent-AI-System` — tutorial notebook, customer
support bot over a music-store SQL database (Chinook DB), 376 stars.

### Structure
Single Jupyter notebook, not a deployed app. `requirements.txt`: ~20 packages
(`langgraph`, `langsmith`, `langchain-openai/anthropic/community`,
`langchain-chroma`, `azure-identity`, `scikit-learn`, `openevals`, `pyppeteer`).

### Architecture
| Component | Mechanism |
|---|---|
| `State` (TypedDict) | `customer_id`, `messages`, `loaded_memory`, `remaining_steps` — shared across the graph |
| 2 ReAct sub-agents | `music_catalog_subagent` (hand-built), `invoice_information_subagent` (via `create_react_agent` prebuilt), each bound to its own SQL tool set |
| Supervisor | Central node routes to a sub-agent via `transfer_to_X` tool calls; sub-agent calls `transfer_back_to_supervisor` when done |
| `verify_info` → `interrupt()` → `human_input` | Blocks until identity is confirmed before touching account data |
| `MemorySaver` (checkpointer) | Short-term: resumes a thread mid-graph |
| `InMemoryStore` + `load_memory`/`create_memory` | Long-term: LLM extracts durable facts into a Pydantic profile, namespaced per customer, reloaded next session |
| LangSmith evaluators | Final-response correctness (LLM-as-judge), single-step (right tool chosen?), full-trajectory scoring, against a hand-written dataset |

### Verdict: do not adopt the framework
1. **Billing.** `OPENAI_API_KEY` set directly, paid per-token. Conflicts with
   Layla's settled principle: reasoning rides the existing session login, no
   per-agent API billing.
2. **Wrong problem shape.** Built for many strangers, each a disposable
   thread, checkpointed and resumed cold. Layla is one user, one continuously
   running session — the checkpoint/resume machinery solves a problem that
   doesn't exist here.
3. **Duplicates infrastructure Layla already has for free** through the omp
   harness: model-calling loop, tool execution, session persistence, subagent
   dispatch.
4. Heavy: 20 dependencies for what's structurally a state machine + tool loop
   + a key-value store. Layla's whole ingest pipeline is ~600 lines, no
   framework.

### What's portable (ideas, not code)
- **Evaluators** (final-response / single-step / trajectory correctness) →
  generalizes to a deterministic checker validating a subagent's output
  against its source material. Became the `verify.py` plan.
- **`verify_info` → `interrupt()`** confirms before touching account data →
  already matched by Layla's "confirm before archiving" rule.
- Long-term preference memory (`load_memory`/`create_memory`) — tempting, not
  adopted: collides with "never freeze a worth-watching verdict, judge fresh
  at triage." Parked, not proposed.

---

## Source 2: Google ADK — "Building a Multi-Agent System" codelab

`codelabs.developers.google.com/.../building-a-multi-agent-system` — Course
Creation System: Researcher → Judge → Content Builder → Orchestrator, deployed
as 4 Cloud Run microservices over the A2A protocol.

### Architecture
| Agent | Role | Restriction |
|---|---|---|
| Researcher | Calls `google_search`, summarizes | Free to delegate/chat |
| Judge | Grades research pass/fail against a Pydantic schema | `disallow_transfer_to_parent`/`disallow_transfer_to_peers` — forced to only ever emit the schema |
| Content Builder | Turns approved research into formatted output | Free to delegate/chat |
| Orchestrator | Coordinates the other three | **Owns zero tools of its own** — "its tool is delegation" |

Two orchestration primitives, composed:
- `LoopAgent` — runs sub-agents repeatedly until an escalation signal or
  `max_iterations` (`while` loop).
- `SequentialAgent` — runs sub-agents once, in order (script execution).
- Nested: `LoopAgent(researcher → judge → escalation_checker)` inside
  `SequentialAgent(research_loop → content_builder)`.

**`EscalationChecker`** is not an LLM — a `BaseAgent` subclass, pure Python,
reads shared state (`judge_feedback`) and yields `Event(escalate=True)` to
break the loop, or a pass-through event to continue. The framework gives
deterministic control-flow logic the same interface as an LLM agent so it
composes into the graph without being one.

Context propagation: agents write outputs to shared `session.state` keys
(`research_findings`, `judge_feedback`); downstream agents read them
implicitly via their prompt.

### Verdict: do not adopt the infra, keep the shape
Same disqualifier as LangChain — `GOOGLE_GENAI_USE_VERTEXAI=true`, a real GCP
project, 4 separate `gcloud run deploy` calls, A2A discovery over HTTP via
`.well-known/agent-card.json`. Solves "many concurrent strangers hit a hosted
product, each agent scales independently" — not Layla's problem (one user,
one process, on your laptop).

### What validates and sharpens Layla's design
Second independent framework, same shape as LangGraph's supervisor pattern —
not a coincidence, the actual industry consensus (see Anthropic section
below):

| Google's version | Layla's version |
|---|---|
| Orchestrator: no tools, delegation only | Layla: no tools of her own beyond calling scripts |
| Judge: schema-only, no delegation | `agent.py`: `read`+`write` only, forced deterministic shape |
| `session.state` keys | `summary.md`/`transcript.md` on disk — same idea, filesystem instead of in-memory, survives process exit |
| `LoopAgent`/`EscalationChecker` | Plain Python retry loop in `ingest.py` — a `for` loop already *is* the primitive, no framework object needed |

Named pattern: this Judge/Loop shape is Anthropic's **evaluator-optimizer**,
one of five canonical workflow patterns (below).

---

## Source 3: Vercel Academy — "Build Your Own AI Coding Agent Harness" (TeensyCode)

`vercel.com/academy/build-ai-agent-harness` — 38-lesson build-along course,
TypeScript + AI SDK + Vercel Sandbox. Builds a coding-agent harness from zero
tools to a full system: tool loop, sandbox abstraction, context management,
subagent delegation, sandbox lifecycle, human-in-the-loop, planning,
surfaces, extensibility.

### Top-line finding
**Most of this course doesn't apply to Layla, and that's the finding, not a
gap.** TeensyCode builds a tool loop, sandbox abstraction, approval gates,
streaming, and session lifecycle *from zero*. Layla runs inside omp, which
already solved all of that one layer down.

| Module | Covered by |
|---|---|
| 1 (Agent Loop), 4 (Sandbox Abstraction) | omp's tool loop + sandboxed bash |
| 5's harness plumbing (`pruneMessages`, cache control) | omp manages the model-call loop; `agent.py` shells out to it, builds no loop of its own |
| 6's subagent mechanics (fresh context, task routing) | omp's `task` tool |
| 7 (state machine, snapshot, durable workflows) | No billable VM exists; `agent.py` is stateless-per-call, on-demand only |
| 8's approval config | omp's approval gates; `agent.py`'s `read`+`write` restriction is already narrower |
| 10 (CLI/streaming/web surfaces) | omp's terminal rendering, headless `-p` mode |
| 11's general event bus | Over-engineering for one deterministic subagent |

### What's genuinely new — full findings, by module

**Module 1–2 (Agent Loop, Tool Design):**
- Tool description as a 5-section contract (WHEN TO USE / WHEN NOT TO USE /
  DO NOT USE FOR / EXAMPLES), doubled-up negative steering — models default to
  the wrong tool ("bash gravity") unless told twice.
- Bounded output + truncation sentinel: cap results (500 lines / 50 matches),
  always state the total count so the caller can paginate. Prevention, not
  cleanup.
- Factory/seam pattern: separate a tool's model-facing contract from its
  swappable execution backend — enables mocking for tests.
- Approval as a discriminated union (`interactive | background | delegated`),
  `delegated` carrying a trust-slice (allowlist) — config as data, not code.
- Core principle: a blocked/failed tool call must return a **truthful
  string**, never throw — an uncaught exception breaks the parent's loop, and
  a silent failure makes the model confabulate success.

**Module 3 (System Prompt):**
- `buildSystemPrompt(ctx)` as a **pure function** taking a typed context
  (tool names actually wired up, working dir, etc.) — testable, composable,
  can't silently drift from the real tool restriction.
- Verification-as-contract: a `# Verification` section that forces **scoped
  claims** ("Ran npm test: 47 passed, 3 failed, pre-existing in X") instead of
  a vibe ("looks good"). Named diagnostic: hedged future tense ("should be
  fine") is a confabulation tell; specific past tense means the check
  actually ran.
- `AGENTS.md` discovery/injection at startup — already Layla's pattern,
  nothing new.

**Module 4 (Sandbox Abstraction):** entirely solved by omp — interface,
local/in-memory/cloud backends, lifecycle hooks are from-scratch sandbox
concerns Layla's harness already owns.

**Module 5 (Context Management):**
- Per-step token telemetry (`onStepFinish` logging input/output tokens) —
  input climbs linearly because every step re-sends full history; curve shape
  classifies task type. "Telemetry first, fix second."
- Bounded-output/truncation-message contract, same as Module 2 — applies
  directly to unbounded transcript dumps.

**Module 6 (Subagent Delegation):**
- Explorer role: fresh context per call (never reuse), read-only, cheap
  model, small step budget (5), errors returned as strings not thrown.
- Executor role: stronger model, delegated-trust `bash` (small allowlist:
  `npm test`/`build`/`tsc`, never `install`/`rm -rf`), larger step budget
  (15), **must not ask questions** — forces act-or-fail, no stalling.
- Task tool as a thin router, dispatching by role to per-role builders.
- **Spawn-permissions table**: `Record<role, allowedSubagentRoles[]>` — a
  subagent can't spawn arbitrary sub-roles. Real gap check for any subagent
  design.
- Named **reviewer** role: read-only, stronger model, a `verdict` tool
  (pass/fail + feedback), auto-runs after the executor, retries **capped at
  2**. This is the evaluator-optimizer pattern again, third confirmation.

**Module 7 (Sandbox Lifecycle):** almost entirely cloud-VM-specific, not
applicable (state machine, snapshot/restore, durable workflows). One portable
idea: a `--chaos` fault-injection flag that randomly injects one known
failure per test run, to confirm the system's own checks catch it.

**Module 8 (Human-in-the-Loop):**
- `askUser` tool returns "(Awaiting response)" rather than actually
  blocking — the harness supplies the answer next turn.
- The load-bearing part is a `# Handling Ambiguity` prompt section: **search
  first, ask second, act third**, with a 2–4 option constraint. Without this
  prompt section, models treat `askUser` as optional and guess instead.

**Module 9 (Planning and Verification):**
- Todo tool with a single-active-item constraint — only relevant if/when a
  multi-step bulk workflow exists; not needed for a single-shot subagent.
- **Grep-first policy**: "Search before reading. Use grep first, then read
  only what you'll change. Don't read files 'just in case.'" Strongest single
  reusable idea in the whole course for Layla's discuss mode.
- Verification contract: discover the actual gates that exist, run
  cheapest-first, enforce scoped claims (distinguish failures you caused from
  pre-existing ones).

**Module 10 (Surfaces):** CLI/streaming/web — all solved by omp's headless
mode and terminal rendering. The one durable principle: **the agent never
branches on which surface is calling it** — same rule already followed by
Layla's discuss/verdict split.

**Module 11 (Extensibility):**
- Skills system: progressive disclosure — name + one-line description always
  in the prompt, full content loaded only on demand, first-dir-wins override
  (project beats global). Formalizes what Layla's per-domain `AGENTS.md`
  reading already does.
- `wrapTool` composition seam: wrap a tool's input/output without forking it.
- General event bus: over-engineering here — `verify.py`'s targeted
  retry-with-feedback is the judge-loop version of "block a call, feed the
  reason back," purpose-built instead of general.

---

## Cross-source synthesis

Three independent frameworks (LangGraph, Google ADK, Vercel/TeensyCode),
**the same shape converges every time**: an orchestrator with no tools of its
own routes between narrow-capability specialists; the connective tissue
(loop-until-condition, state handoff, retry-with-feedback) is deterministic
code, not a third model call. This is not a coincidence — it matches
Anthropic's own published framing (*Building Effective Agents*): workflows
(predefined code paths) beat agents (LLM decides dynamically) whenever a
task's structure is stable enough to encode; the industry rule of thumb is
*if a decision has one objectively valid outcome, enforce it in code — use a
model only when the correct choice depends on interpreting language.*

Layla's architecture, decided independently before any of these three
sources were read, already matches this pattern:

| Role | Layla's instance |
|---|---|
| Orchestrator (no tools, delegation only) | Layla, reading domain `AGENTS.md` |
| Specialist (narrow capability, forced-deterministic shape) | `youtube.py` (built; the summarizer subagent was cut — decision 21) |
| Evaluator (deterministic Judge) | `dev/judge.py` (built, offline only — decision 20) |
| Loop-until-pass (deterministic glue) | Retry loop inside `ingest.py` (planned) |
| Sequential pipeline (deterministic glue) | `ingest.py`: playlist → diff → per-video → index (planned) |

What none of the three sources needed to teach Layla, because omp already
provides it: the tool-calling loop itself, sandboxed execution, approval
gating, session persistence, streaming, multi-provider model routing.
