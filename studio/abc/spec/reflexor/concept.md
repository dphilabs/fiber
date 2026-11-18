# fiber.studio.abc.spec.reflexor.concept
@desc: Reflexor — Reflex workflow for AI: concept and terminology

> **Agents explore. Reflexor repeat.**

Component behavior: [MCP Connector Spec](../gateway/mcp.connector.md) · [Interceptor Spec](../llm/interceptor.md)

---

## 1. Why Reflexor

When AI is connected to real systems, approvals, keys, leak prevention, and spend limits end up configured in different places, and they interact.

| Controls configured separately | Result |
| --- | --- |
| Approval wait + retry | The same action runs twice |
| Leak prevention + model switch | Masking rules must be realigned for each model |
| Spend cap + streaming | In-flight calls run past the budget |
| Key management + many tools | No single view of where keys are used |

Reflexor manages them in a single configuration.

| AI touches | Configured in Reflexor |
| --- | --- |
| Tools | Approval rules, duplicate handling, key resolution |
| Models | Masking and blocking, budgets, provider routing |
| Workflow | Order of steps, pause points, resume behavior |

---

## 2. Definition

**Reflexor configures control over the tools, models, and workflows AI uses, and runs repeated work as fixed paths called reflexes.**

We call this a *reflex workflow*.

| | Agentic workflow | Reflex workflow |
| --- | --- | --- |
| Next step chosen by | The model, at runtime | The reflex, before execution |
| Role of AI | Decides and acts | Interprets, extracts, generates |
| Side effects | The model calls tools | Gates and actuators execute per policy |
| Controls configured | Per tool, model, workflow | In one configuration |

---

## 3. Relation to Fiber

| Layer | Component |
| --- | --- |
| Tool path | Fiber · Local MCP Gateway |
| Model path | Fiber · LLM Pipeline |
| Workflow path | Reflex workflow layer (separate spec) |

---

## 4. Terminology

### 4.1. Core terms

| Term | Definition |
| --- | --- |
| **Reflexor** | The system that configures controls and builds, admits, and fires reflexes |
| **Reflex workflow** (reflex) | A workflow whose paths and permissions are set before execution. States: draft / admitted |
| **No model authority** | Model permissions are set by policy; model output cannot change that policy |
| **transform · gate · actuator** | Where AI produces data · where policy and approval apply · where admitted side effects execute |

### 4.2. Lifecycle verbs

| Verb | Typical term | Meaning |
| --- | --- | --- |
| **admit** | activate | Passes checks; may fire |
| **reject** | runtime error | Blocked before execution |
| **fire** | run | Proceeds along its path on a trigger |
| **park** | wait | Pauses for approval or a response, keeping position |
| **resume** | retry | Continues from the parked position without repeating committed side effects |

### 4.3. Interface terms

| Typical term | Reflexor |
| --- | --- |
| Workflow | **Reflex** |
| AI agent node | **Transform node** |
| Tool | **Actuator** |
| Wait / approval node | **Gate** |

---

## 5. Analogies

Each analogy splits roles the same way: AI does the work it is good at, and the path decides what changes.

| Context | Analogy | Without unified control |
| --- | --- | --- |
| General | Agents build the prototype. Reflexor runs the production line. | — |
| Manufacturing | AI is the inspector, not the foreman. | Give the inspector the foreman's authority, and one wrong call changes line settings and sends the same order twice. |
| Software teams | AI opens the PR; branch protection decides the merge. | If AI merges its own PRs, one retry ships the deploy twice, and a single inbound email can issue it commands. |
| Model providers | The model is the locomotive; Reflexor is the track. | Without track, a stronger engine derails farther, and the blame lands on the locomotive. |
| Agent builders | Agents find the road; Reflexor paves it. | Driving an unpaved road every day means paying to find the way each time, with no map to show a customer's security team. |