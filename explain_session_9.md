# Session 9: Browser Agent & Autonomous Web — Complete Technical Guide

---

## 1. What Is This System?

This is a **multi-agent orchestration system** where an AI breaks a user's query into a graph of specialist tasks, executes them in parallel, and assembles a final answer. Session 9 adds a **Browser Skill** — a four-layer cascade that lets the system navigate real websites autonomously.

---

## 2. Bird's-Eye Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        USER QUERY                                   │
│         "What are the top 3 LLMs on HuggingFace this week?"        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   EXECUTOR  (flow.py)                               │
│                                                                     │
│   ┌────────────┐   Reads DAG   ┌──────────────────────────────┐    │
│   │  Memory    │◄──────────────│  Growing Graph Orchestrator  │    │
│   │  (FAISS)   │               │  "Who is ready to run?"      │    │
│   └────────────┘               └──────────────────────────────┘    │
│                                           │                         │
│              Spawns & runs skill nodes    │                         │
│              one or more at a time        │                         │
└───────────────────────────────────────────┼─────────────────────────┘
                                            │
           ┌────────────────────────────────┼────────────────────────┐
           │                                │                        │
           ▼                                ▼                        ▼
    ┌─────────────┐                ┌──────────────┐         ┌──────────────┐
    │   Planner   │                │   Browser    │         │  Researcher  │
    │  (n:1)      │                │  (n:2)       │         │  (n:2)       │
    └─────────────┘                └──────────────┘         └──────────────┘
           │                                │                        │
     Emits NodeSpecs                 Web navigation           Web search
     (builds next layer)            Layer 1–3 cascade         + tool use
           │
           ▼
    ┌──────────────────────────────────────────────────┐
    │   llm_gatewayV9  (running on :8109)              │
    │   /v1/chat  /v1/vision  /v1/embed  /v1/cost      │
    │   Provider routing: Gemini / GPT-4o / Claude     │
    └──────────────────────────────────────────────────┘
```

---

## 3. The Growing-Graph Model

The key insight: **the graph is not planned upfront — it grows as nodes complete.**

```
INITIAL STATE:
  ┌─────────┐
  │ Planner │  ← only node at start
  └─────────┘

AFTER PLANNER RUNS (it emits NodeSpecs):
  ┌─────────┐
  │ Planner │
  └────┬────┘
       │ emits 3 successors
   ┌───┴───┬──────────┐
   ▼       ▼          ▼
┌───────┐ ┌───────┐ ┌──────────┐
│Browser│ │Distill│ │Formatter │
└───────┘ └───────┘ └──────────┘

AFTER DISTILLER RUNS (critic:true — auto-inserts Critic):
  ┌─────────┐
  │ Planner │
  └────┬────┘
   ┌───┴───┬──────────┐
   ▼       ▼          ▼
┌───────┐ ┌───────┐  ┌──────────┐
│Browser│ │Distill│  │Formatter │
└───────┘ └───┬───┘  └────▲─────┘
              │            │   ← edge intercepted!
              ▼            │
         ┌────────┐        │
         │ Critic │────────┘  ← auto-inserted between them
         └────────┘
```

This happens in `code/flow.py`. The executor loop is:

```python
while not done:
    ready = graph.ready_nodes()      # nodes whose all parents completed
    run_in_parallel(ready)           # execute skills concurrently
    extend_graph_from_results()      # add new nodes from successors
```

The graph grows via five mechanisms:

| Mechanism | How It Works |
|-----------|-------------|
| **Dynamic successors** | A skill emits `successors` in its JSON reply → Executor adds them as new nodes |
| **Internal successors** | YAML `internal_successors` field auto-wires e.g. `coder → sandbox_executor` |
| **Critic auto-insertion** | When a `critic:true` skill finishes, a Critic node is auto-gated on every outgoing edge |
| **Planner re-invocation** | On node failure, Planner is invoked again with failure report + prior completions |
| **Per-node scoping** | Planner emits `metadata.question` to split the user query across parallel workers |

---

## 4. All Skills / Agents Explained

```
┌──────────────────────────────────────────────────────────────────────┐
│                    SKILL CATALOGUE (agent_config.yaml)               │
├────────────────┬───────────┬──────────────────────────────────────── ┤
│ Skill          │ Role      │ What It Does                            │
├────────────────┼───────────┼─────────────────────────────────────────┤
│ planner        │ START     │ Reads user query, emits DAG as          │
│                │ node      │ NodeSpec JSON. The architect of each run │
├────────────────┼───────────┼─────────────────────────────────────────┤
│ researcher     │ WORKER    │ Web search + URL fetch via MCP tools    │
│                │           │ (Tavily → DDG fallback)                  │
├────────────────┼───────────┼─────────────────────────────────────────┤
│ browser        │ WORKER    │ Navigate real websites (NEW in S9)      │
│  (4-layer)     │           │ Four-layer cascade: extract→a11y→vision  │
├────────────────┼───────────┼─────────────────────────────────────────┤
│ distiller      │ WORKER    │ Extracts structured fields from raw text │
│  (critic:true) │           │ Auto-inserts Critic on its outputs       │
├────────────────┼───────────┼─────────────────────────────────────────┤
│ summariser     │ WORKER    │ Condenses long content to short summary  │
├────────────────┼───────────┼─────────────────────────────────────────┤
│ retriever      │ WORKER    │ Searches FAISS memory for past facts     │
├────────────────┼───────────┼─────────────────────────────────────────┤
│ coder          │ WORKER    │ Writes Python code (stub)               │
│  → sandbox     │           │ Auto-wires to sandbox_executor           │
├────────────────┼───────────┼─────────────────────────────────────────┤
│ sandbox_       │ WORKER    │ Runs Python code in isolated subprocess  │
│ executor       │           │ (30s timeout, 1MB output cap)            │
├────────────────┼───────────┼─────────────────────────────────────────┤
│ critic         │ GATE      │ Pass/fail verdict on upstream output     │
│                │           │ temperature=0.0 (deterministic)          │
├────────────────┼───────────┼─────────────────────────────────────────┤
│ formatter      │ TERMINAL  │ Renders final markdown answer            │
│                │           │ Last node; its output = user's answer    │
└────────────────┴───────────┴─────────────────────────────────────────┘
```

### How a Skill Executes (skills.py)

Every skill goes through the same pipeline before calling the LLM:

```
1. resolve_inputs()
   ├─ "USER_QUERY"     → original query text
   ├─ "n:2"            → output of node 2
   ├─ "art:abc123"     → binary artifact bytes
   └─ literal strings  → pass through

2. render_prompt()
   ├─ Base system prompt  (from prompts/<skill>.md)
   ├─ MEMORY HITS block   (8 FAISS results, same for all nodes)
   ├─ USER_QUERY block    (only if in inputs)
   ├─ QUESTION block      (metadata.question for fan-out workers)
   ├─ FAILURE block       (on recovery runs)
   └─ INPUTS JSON         (resolved upstream outputs)

3. dispatch()
   ├─ sandbox_executor  → sandbox.run_python() directly
   ├─ browser           → BrowserSkill.run() directly
   └─ all others        → LLM gateway (with or without MCP tool loop)

4. parse_skill_json()
   └─ Extract JSON from model reply, validate NodeSpecs

5. return AgentResult(success, output, successors, cost, elapsed, provider, error)
```

---

## 5. The Browser Skill — Four-Layer Cascade

This is the centerpiece of Session 9. It lives in `code/browser/skill.py` (~280 lines, the **only** new file in S9).

```
INCOMING REQUEST: { url: "https://huggingface.co", goal: "find top models" }
                              │
                              ▼
        ┌─────────────────────────────────────────┐
        │  LAYER 1: HTML Extract (NO LLM, FREE)   │
        │                                         │
        │  httpx.get(url) → trafilatura.extract() │
        │  Returns clean text from page HTML      │
        │  Cost: $0, no browser, no LLM           │
        └───────────────┬─────────────────────────┘
                        │
          ┌─────────────┴──────────────────────────┐
          │ Is content useful?                      │
          │ • length ≥ 200 chars  AND               │
          │ • goal has no interactive verbs         │
          │   (click/fill/select/login/submit...)   │
          └─────────────────────────────────────────┘
                YES ─────────────────────────────────► DONE  (path=extract)
                │
                NO (too short / needs interaction / gateway-blocked)
                │
                ▼
        ┌─────────────────────────────────────────────────┐
        │  LAYER 2a: Deterministic Selectors (optional)   │
        │                                                 │
        │  Only fires if caller provides:                 │
        │  metadata.selectors = [{action, selector, val}] │
        │  Executes exactly: click / fill / key           │
        │  No LLM involved at all                         │
        └──────────────────┬──────────────────────────────┘
                           │ success → DONE (path=deterministic)
                           │ no selectors given → fall to 2b
                           ▼
        ┌─────────────────────────────────────────────────┐
        │  LAYER 2b: A11y-Text Driver (Playwright + LLM)  │
        │                                                 │
        │  1. Launch Chromium (headless, no webdriver)    │
        │  2. Navigate to URL, viewport 1366×900          │
        │  3. enumerate_interactives(page) via JavaScript │
        │     → [{id, tag, role, name, x, y, w, h}, ...]  │
        │     → Rendered as: [5]<button>Search</button>   │
        │  4. Send TEXT legend (no screenshot) to LLM     │
        │     via V9 /v1/chat (cheaper than vision)       │
        │  5. LLM responds: {"action":"click","id":5}     │
        │  6. Playwright executes action                  │
        │  7. Repeat up to 12 turns                       │
        │  Escalates if: empty legend OR 3 consec fails   │
        └──────────────────┬──────────────────────────────┘
                           │ success → DONE (path=a11y)
                           │ DOM empty / model gives up → escalate
                           ▼
        ┌──────────────────────────────────────────────────────┐
        │  LAYER 3: Set-of-Marks Vision (Playwright + Vision)  │
        │                                                      │
        │  1. Take full viewport screenshot (PNG)              │
        │  2. annotate() via Pillow:                           │
        │     • Dashed numbered boxes over each element        │
        │     • Color-coded: link=blue, button=green,          │
        │       input=orange, select=purple                    │
        │     • Badge: filled rect + white number              │
        │  3. Encode to base64 data URL                        │
        │  4. Send annotated image + legend to V9 /v1/vision   │
        │  5. Vision LLM responds: {"action":"click","id":12}  │
        │  6. Execute; save raw PNG + marked PNG as artifacts  │
        │  7. Repeat                                           │
        └──────────────────────────────────────────────────────┘
                           │
                           ▼
                      DONE  (path=vision)
```

### Gateway Block Detection

Before escalating layers, the system detects when a site refuses access:

```python
# code/browser/skill.py: detect_gateway_block(html)

Patterns checked:
  CAPTCHA:     "Let's confirm you are human"
               "Enter the characters you see below"
               "Robot Check"
               class="h-captcha" / class="g-recaptcha"

  Cloudflare:  "Checking your browser before accessing"
               cf-browser-verification / cf-challenge-running

  Login walls: "You must be logged in"
               "Sign in to continue"
               "Please log in to continue"

Result:  error_code = "gateway_blocked"
Action:  Recovery Planner re-routes (e.g. uses Researcher + web_search instead)
```

---

## 6. DOM Enumeration — How the Browser "Sees" a Page

`code/browser/dom.py` runs a single JavaScript pass inside Playwright to find all interactive elements:

```
Real web page:                   Legend sent to LLM:
┌─────────────────────────┐
│  [Search] [Login]       │      [1]<a href>Search</a>
│                         │      [2]<a href>Login</a>
│  ┌─────────────────┐    │      [3]<input placeholder="Query">
│  │ Search box...   │    │      [4]<button>Submit</button>
│  └─────────────────┘    │      [5]<a href>Top Models</a>
│  [Submit]               │      [6]<select>Sort By</select>
│                         │
│  Top Models ↓           │
│  Sort By: [Likes ▼]     │
└─────────────────────────┘

Selectors scanned:  a[href], button, input, textarea, select,
                    ARIA roles, onclick handlers, cursor:pointer

Filters applied:
  ✗ width or height < 4px (invisible micro-elements)
  ✗ off-screen elements (x/y outside viewport)
  ✗ hidden / display:none / opacity:0
  ✗ child of an already-listed interactive (dedup: outermost wins)
  ✓ everything else gets a sequential 1-based ID
```

Name extraction priority order:
1. `aria-label`
2. `aria-labelledby` referenced element text
3. `innerText` / `value` / `placeholder` / `title` / `alt`
4. Descendant `aria-label` or `title`

---

## 7. Screenshot Annotation (Set-of-Marks)

`code/browser/highlight.py` annotates screenshots so the vision model can identify elements by number:

```
BEFORE annotation:              AFTER annotation (Pillow):
┌───────────────────┐           ┌───────────────────────────────┐
│  HuggingFace      │           │  HuggingFace                  │
│                   │           │                               │
│  [Search Models]  │           │  ┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐  │
│                   │           │  ┊ [1] Search Models       ┊  │ ← dashed green box
│  Top Downloads ▼  │           │  └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘  │
│                   │           │                               │
│  [Login] [Sign Up]│           │  ┌ ─ ─ ─ ─ ─ ┐ ┌ ─ ─ ─ ─ ─┐ │
└───────────────────┘           │  ┊ [2] Login  ┊ ┊[3] Sign Up│ │ ← dashed blue boxes
                                │  └ ─ ─ ─ ─ ─ ┘ └ ─ ─ ─ ─ ─┘ │
                                └───────────────────────────────┘

Color coding:
  link/anchor  → blue
  button       → green
  input/textarea → orange
  select       → purple
  default      → red

CSS-px → device-px scaling: all coords multiplied by devicePixelRatio (dpr)
```

The LLM sees this image and responds: `{"action": "click", "element_id": 2}`.
Playwright then executes `page.click("element_id_selector")`.

---

## 8. Driver Classes Hierarchy

```
code/browser/driver.py

┌──────────────────────────────────────────────────────────┐
│                      BaseDriver                          │
│                                                          │
│  Per-turn loop:                                          │
│    1. enumerate_interactives(page) → legend              │
│    2. _decide(goal, legend, history) → actions  [ABSTRACT]│
│    3. _dispatch(actions) → execute via Playwright        │
│    4. save_artifacts(screenshot, legend, marked)         │
│    5. check done / failure conditions                    │
│                                                          │
│  Shared action dispatcher:                               │
│    click / type / key / scroll / drag / wait / done      │
│                                                          │
│  Failure logic:                                          │
│    ≥ 3 consecutive action errors → fail                  │
│    done(success=False) → fail                            │
│    done(success=True)  → exit with DriverResult          │
└───────────────────────────┬──────────────────────────────┘
                            │
              ┌─────────────┴─────────────┐
              │                           │
    ┌─────────▼──────────┐    ┌───────────▼──────────────┐
    │    A11yDriver       │    │    SetOfMarksDriver       │
    │    (Layer 2b)       │    │    (Layer 3)              │
    │                     │    │                           │
    │  _decide():         │    │  _decide():               │
    │    client.chat(     │    │    1. capture screenshot  │
    │      legend + goal  │    │    2. annotate() via Pillow│
    │    )                │    │    3. client.vision(      │
    │  No screenshot      │    │         image + legend    │
    │  Cheaper            │    │       )                   │
    │                     │    │  Saves: raw PNG,          │
    │  Saves: raw PNG,    │    │         marked PNG,       │
    │         legend.txt  │    │         legend.txt        │
    └─────────────────────┘    └───────────────────────────┘
```

---

## 9. Memory System

```
USER QUERY
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│              MEMORY READ  (session start, once)          │
│                                                          │
│   1. Embed query text → 768-dim float vector             │
│      (via V9 /v1/embed, retrieval_query task type)       │
│   2. FAISS IndexFlatIP: cosine similarity search         │
│      (inner product on L2-normalized vectors)            │
│   3. Return top-8 hits + keyword fallback                │
│                                                          │
│   Hit format:                                            │
│   kind: fact | preference | tool_outcome | scratchpad    │
│   descriptor: "HuggingFace shows model likes count"      │
│   value: "The models page at /models shows..."           │
└──────────────────────────────┬───────────────────────────┘
                               │
               injected into EVERY skill's prompt
               (all nodes see the same 8 hits)
                               │
                               ▼
              ┌────────────────────────────────┐
              │  MEMORY WRITE  (session end)   │
              │                                │
              │  formatter output → LLM        │
              │  classifies: kind, descriptor, │
              │  keywords                      │
              │  embed descriptor → FAISS add  │
              │  persist memory.json + .faiss  │
              └────────────────────────────────┘

Memory files:
  state/memory.json        ← all MemoryItem records
  state/index.faiss        ← FAISS binary index
  state/index_ids.json     ← integer position → item_id map
```

Memory kinds:

| Kind | Description | Embedded? |
|------|-------------|-----------|
| `fact` | Structured knowledge | Yes |
| `preference` | User preferences | Yes |
| `tool_outcome` | Past tool call results | Yes |
| `scratchpad` | Run-scoped notes | No |

---

## 10. MCP Tools Available to Skills

`code/mcp_server.py` exposes 11 tools via stdio (Model Context Protocol):

```
┌────────────────────────────────────────────────────────────────┐
│                  MCP SERVER  (stdio transport)                  │
├──────────────────────┬─────────────────────────────────────────┤
│ Tool                 │ What it does                            │
├──────────────────────┼─────────────────────────────────────────┤
│ web_search           │ Tavily → DDG fallback (5 results cap)   │
│ fetch_url            │ Headless Chromium crawl (crawl4ai)      │
│ get_time             │ IANA timezone lookup                    │
│ currency_convert     │ Live FX rates                           │
│ read_file            │ Read file from sandbox/                 │
│ list_dir             │ List sandbox/ directory                 │
│ create_file          │ Write new file to sandbox/              │
│ update_file          │ Overwrite sandbox/ file                 │
│ edit_file            │ Patch sandbox/ file                     │
│ index_document       │ Chunk + embed file into FAISS memory    │
│ search_knowledge     │ Vector search over memory               │
└──────────────────────┴─────────────────────────────────────────┘
```

Tool-use loop (`code/mcp_runner.py`):

```
┌──────────────┐     ┌──────────────────────┐     ┌──────────────┐
│  LLM gateway │────►│ response has          │────►│  MCP server  │
│  /v1/chat    │     │ tool_calls?           │     │  executes    │
│              │◄────│ append results,       │◄────│  returns     │
│              │     │ call again            │     │  result text │
└──────────────┘     └──────────────────────┘     └──────────────┘
                           (max 6 hops)

Per-tool result capped at 8 000 tokens.
Skills with tools_allowed: [] skip the loop entirely.
```

---

## 11. Failure Recovery System

```
NODE FAILS
    │
    ▼
┌─────────────────────────────────────────────┐
│  classify_failure(error_text)               │
│  code/recovery.py                           │
│                                             │
│  "503 / timeout / connection reset"         │
│        → transient                          │
│                                             │
│  "ValidationError / malformed JSON"         │
│        → validation_error                   │
│                                             │
│  anything else                              │
│        → upstream_failure                   │
└───────────────────────┬─────────────────────┘
                        │
      ┌─────────────────┼──────────────────────┐
      │                 │                      │
      ▼                 ▼                      ▼
 transient         validation           upstream_failure
      │                 │                      │
    SKIP              SKIP          planner already failed?
 (gateway                                      │
 already retried              YES → SKIP  (avoid infinite loop)
 on its own)                  NO  → REPLAN
                                             │
                                             ▼
                                  Invoke Planner with:
                                  • failure report
                                  • prior completed nodes
                                  → grows new sub-DAG
                                    (route-around)
```

### Gateway Block Recovery Example

```
Query: "Go to redfin.com, find 3-bedroom houses under $500k"

Step 1: Layer 1 fetch → HTTP 405 (WAF blocks plain requests)
Step 2: Fall to Layer 2b → Playwright renders page
Step 3: detect_gateway_block() matches CAPTCHA/Cloudflare patterns
Step 4: error_code = "gateway_blocked" returned to Executor
Step 5: classify_failure → upstream_failure → REPLAN
Step 6: Recovery Planner prompt includes:
        "Node n:2 (browser) failed: gateway_blocked on redfin.com.
         Prior completions: none. Find an alternative."
Step 7: New DAG: Researcher node using web_search("redfin 3br <500k")
Step 8: Honest final answer: "Unable to retrieve from Redfin directly
        (site security). Here is what web search found instead: ..."
```

---

## 12. Critic Auto-Insertion

Skills marked `critic: true` in `agent_config.yaml` (currently only `distiller`) automatically get a Critic gate on every outgoing edge:

```
BEFORE distiller completes:

  distiller ──────────────────────────► formatter

AFTER distiller completes  (Graph.extend_from called):

  distiller ──────────► CRITIC ──────► formatter
                           │
                     if verdict == "fail":
                       formatter is SKIPPED
                       recovery Planner spawned

Critic inputs:
  ["USER_QUERY", "n:distiller"]

Critic prompt:
  - Did the distiller output actually answer the user's question?
  - temperature = 0.0  (no randomness in pass/fail decisions)

Critic verdict JSON:
  {"verdict": "pass" | "fail", "rationale": "..."}
```

If the Critic fires `fail` twice for the same target node, the system gives up on that path and the final answer reflects the gap transparently.

---

## 13. Persistence & Replay

Every session is stored on disk atomically (write temp file, rename):

```
state/
├── sessions/
│   └── <session_id>/
│       ├── query.txt         ← verbatim user query
│       ├── graph.json        ← full NetworkX DiGraph (node_link_data)
│       └── nodes/
│           ├── n_001.json    ← NodeState for each node
│           ├── n_002.json
│           └── ...
├── memory.json               ← all MemoryItem records
├── index.faiss               ← FAISS binary index
├── index_ids.json            ← integer position → item_id map
└── artifacts/
    ├── <sha256[:16]>.bin     ← binary content (screenshots, etc.)
    └── <sha256[:16]>.json    ← artifact metadata
```

NodeState record (per node):

```python
NodeState(
    node_id    = "n:2",
    skill      = "browser",
    status     = "complete",          # pending/running/complete/failed/skipped
    inputs     = ["USER_QUERY"],
    result     = AgentResult(...),    # full LLM response + metadata
    prompt_sent = "...",              # exact prompt (load-bearing for replay)
    started_at  = 1719234567.3,
    completed_at = 1719234584.7,
    retries     = 0
)
```

Replay CLI:

```bash
uv run python code/replay.py <session_id>

# Interactive commands:
# Enter  → next node
# p      → expand full prompt_sent
# o      → expand full output
# q      → quit
```

---

## 14. Sandbox Code Execution

```
Coder skill emits Python code
          │
          ▼  (internal_successors auto-wire)
sandbox_executor skill receives code
          │
          ▼
sandbox.run_python(code)
  ├─ Write code to tempdir
  ├─ subprocess.run(["python", file], ...)
  │     timeout:        30 seconds
  │     stdout cap:     1 MB
  │     stderr cap:     1 MB
  │     env whitelist:  PATH, HOME, LANG, LC_*
  │     cwd:            sandbox/
  └─ Return: {exit_code, stdout, stderr, files_written, timed_out}
          │
          ▼
sandbox_executor formats result as AgentResult.output
```

---

## 15. Complete End-to-End Execution Trace

Full trace for: *"What are the top 3 LLMs on HuggingFace this week?"*

```
flow.py: Executor.run("What are the top 3 LLMs...")
│
├─ [SETUP]
│   ├─ SessionStore created: state/sessions/abc123/
│   ├─ Memory.read(query) → 8 FAISS hits about HuggingFace
│   └─ Graph: add n:1 (planner, inputs=["USER_QUERY"])
│
├─ [ITERATION 1]  Ready: n:1 (planner)
│   ├─ resolve_inputs(["USER_QUERY"]) → query text
│   ├─ render_prompt → planner.md + memory hits + query
│   ├─ LLM.chat(prompt, agent="planner") → NodeSpec JSON:
│   │     {"nodes": [
│   │       {skill:"browser",   inputs:["USER_QUERY"],
│   │        metadata:{url:"https://huggingface.co/models", goal:"top 3"}},
│   │       {skill:"distiller", inputs:["n:2"]},
│   │       {skill:"formatter", inputs:["USER_QUERY","n:3"]}
│   │     ]}
│   └─ Graph extended: n:2 (browser), n:3 (distiller), n:4 (formatter)
│
├─ [ITERATION 2]  Ready: n:2 (browser)
│   ├─ BrowserSkill.run({url, goal})
│   │   ├─ LAYER 1: httpx.get(url) → trafilatura → 80 chars → escalate
│   │   └─ LAYER 2b: A11yDriver.run()
│   │       ├─ Playwright: launch Chromium, navigate to URL
│   │       ├─ [Turn 1] enumerate() → 50 elements
│   │       │           chat("goal + legend") → "click [5] Trending"
│   │       │           Playwright: page.click("[id=5]")
│   │       ├─ [Turn 2] enumerate() → 80 elements (page reloaded)
│   │       │           chat() → "scroll down to see full list"
│   │       ├─ [Turn 3] chat() → done(success=true, content="1. deepseek-R1...")
│   │       └─ DriverResult(success=True, turns=3, content="1. deepseek...")
│   └─ AgentResult(success=True, output={path:"a11y", content:"1. deepseek..."})
│
├─ [ITERATION 3]  Ready: n:3 (distiller, critic:true)
│   ├─ resolve_inputs(["n:2"]) → browser output
│   ├─ LLM.chat() → {"fields": {"model_1": "deepseek-R1", ...}}
│   └─ Graph.extend_from(n:3, result):
│       ├─ critic:true triggered
│       ├─ Remove edge n:3 → n:4
│       ├─ Add n:5 (critic, inputs=["USER_QUERY","n:3"])
│       └─ n:4 inputs updated to use n:5
│
├─ [ITERATION 4]  Ready: n:5 (critic)
│   ├─ resolve_inputs(["USER_QUERY","n:3"])
│   ├─ LLM.chat(temperature=0.0)
│   │   → {"verdict": "pass", "rationale": "All 3 models named with details"}
│   └─ verdict == pass → n:4 not skipped
│
├─ [ITERATION 5]  Ready: n:4 (formatter)
│   ├─ resolve_inputs(["USER_QUERY","n:3"])
│   ├─ LLM.chat() → {"final_answer": "## Top 3 LLMs on HuggingFace\n1. ..."}
│   └─ No successors emitted → graph terminates
│
└─ [DONE]
    ├─ SessionStore.write_graph() + write all NodeStates
    ├─ Memory.remember(answer, source="formatter")
    └─ Return "## Top 3 LLMs on HuggingFace\n1. deepseek-ai/DeepSeek-R1..."
```

---

## 16. Key Schemas (schemas.py)

```python
AgentResult(
    success: bool,
    agent_name: str,
    output: dict | str,
    artifacts: list[str],       # art:<sha256> handles
    successors: list[NodeSpec], # new nodes to add to graph
    cost: float,
    elapsed_s: float,
    provider: str,
    error: str | None,
    error_code: ErrorCode | None   # NEW in S9
)

BrowserOutput(
    url: str,
    goal: str,
    path: "extract" | "deterministic" | "a11y" | "vision",
    turns: int,
    content: str,
    actions: list[StepRecord],
    final_url: str
)

NodeSpec(skill: str, inputs: list[str], metadata: dict)

NodeState(
    node_id: str,
    skill: str,
    status: "pending" | "running" | "complete" | "failed" | "skipped",
    inputs: list[str],
    result: AgentResult | None,
    prompt_sent: str | None,
    started_at: float | None,
    completed_at: float | None,
    retries: int
)

ErrorCode = Literal[
    "gateway_blocked",      # CAPTCHA / login wall / geo-block
    "extraction_failed",    # no useful content
    "interaction_failed",   # goal not met within turn cap
    "timeout",              # wall-clock cap exceeded
    "vlm_unavailable",      # vision provider refused / 503
]
```

---

## 17. File Structure Summary

```
S9SharedCode/code/
│
├── flow.py            ← The heart: graph executor & growing-DAG loop
├── schemas.py         ← All typed contracts (AgentResult, NodeState, etc.)
├── skills.py          ← Skill dispatch: resolve → render → call LLM
├── agent_config.yaml  ← Skill catalogue with prompt paths & settings
│
├── memory.py          ← FAISS vector search + keyword fallback
├── vector_index.py    ← Raw FAISS wrapper (IndexFlatIP, cosine similarity)
│
├── mcp_server.py      ← 11 tools via stdio (web_search, fetch_url, etc.)
├── mcp_runner.py      ← Tool-use loop (up to 6 hops max)
│
├── recovery.py        ← Failure classification + skip/replan decision
├── persistence.py     ← Session store (graph.json + node JSONs)
├── artifacts.py       ← SHA-256 content-addressed binary blob store
├── sandbox.py         ← Safe Python execution (subprocess, 30s cap)
├── gateway.py         ← Bridge to llm_gatewayV9 (:8109, auto-starts)
├── replay.py          ← CLI to step through past sessions interactively
│
├── browser/           ← SESSION 9: the only new directory
│   ├── skill.py       ← Four-layer cascade (280 lines)
│   ├── client.py      ← V9Client: /v1/vision and /v1/chat calls
│   ├── driver.py      ← BaseDriver, A11yDriver, SetOfMarksDriver
│   ├── dom.py         ← JavaScript DOM enumeration via Playwright
│   └── highlight.py   ← Pillow annotation: dashed boxes + numbered badges
│
└── prompts/           ← System prompts for each skill (markdown)
    ├── planner.md
    ├── browser.md
    ├── researcher.md
    ├── distiller.md
    ├── critic.md
    ├── formatter.md
    ├── summariser.md
    ├── coder.md
    ├── sandbox_executor.md
    └── retriever.md
```

---

## 18. Demo Scenarios (run_demo.sh)

```bash
./run_demo.sh tests        # Run pytest (29 tests: recovery, critic, amnesia)
./run_demo.sh hello        # planner → formatter  (minimal 2-node DAG)
./run_demo.sh shannon      # planner → researcher → formatter
./run_demo.sh populations  # planner → researcher×3 (parallel) → formatter
./run_demo.sh structured   # planner → researcher → distiller → CRITIC → formatter
./run_demo.sh fail         # planner → formatter  (graceful-fail-by-planning)
./run_demo.sh browser      # planner → browser → distiller? → formatter
./run_demo.sh wipe         # Clear state/sessions, FAISS, memory.json, artifacts
```

---

## 19. Key Concepts Q&A for New Learners

**Q: Why a growing graph instead of a fixed pipeline?**  
Because you cannot know upfront what a query requires. A HuggingFace question needs a Browser node. A math question needs a Coder node. The Planner decides dynamically, and nodes can spawn more nodes.

**Q: Why four layers in Browser instead of always using vision?**  
Cost and speed. Layer 1 (pure HTML) is free and instant. A vision LLM call costs money and takes 2–5 seconds. The cascade escalates only as far as necessary — most pages are handled by Layer 1 or 2b.

**Q: Why FAISS for memory instead of a cloud database?**  
FAISS runs entirely in-process — no server, no network call. It finds *semantically* similar past facts using vector similarity, so "capital of France" matches "Paris is where France's government sits", not just keyword overlap.

**Q: What is MCP (Model Context Protocol)?**  
A standard for LLMs to call tools. The LLM returns `tool_calls` in its response; `mcp_runner.py` dispatches them to `mcp_server.py` which executes the real work (HTTP fetch, file write, etc.) and returns results. Up to 6 rounds before the loop stops.

**Q: Why does Critic use temperature=0.0?**  
The Critic is a binary pass/fail quality gate. Any randomness would make quality control unreliable. Zero temperature gives the same output for the same input — fully deterministic.

**Q: What is content-addressed storage (artifacts.py)?**  
Files are stored by the SHA-256 hash of their content, not their name. Two identical screenshots produce one file. Skills reference blobs by handle (`art:<digest>`) and never worry about naming conflicts.

**Q: Why does the Planner re-receive prior completions on recovery?**  
Without them, the recovery Planner has "amnesia" — it might re-plan work that already succeeded, or fail to use results already in hand. Passing all prior completions avoids wasted work.

---

## 20. Session 9 vs Session 8 — What Changed

| Component | Session 8 | Session 9 |
|-----------|-----------|-----------|
| `browser/` directory | Not present | **Added** (6 files) |
| `browser/skill.py` | Not present | **Added** (280 lines, 4-layer cascade) |
| `browser/driver.py` | Not present | **Added** (BaseDriver, A11yDriver, SoMDriver) |
| `browser/dom.py` | Not present | **Added** (JS interactive enumeration) |
| `browser/highlight.py` | Not present | **Added** (Pillow annotation) |
| `browser/client.py` | Not present | **Added** (V9Client vision + chat) |
| `schemas.py` | No `error_code` | Added `ErrorCode` + `BrowserOutput` |
| `flow.py` | No gateway-block handling | No change (recovery handles it generically) |
| `agent_config.yaml` | No browser skill | Added `browser` entry |
| `prompts/browser.md` | Not present | **Added** |
| Everything else | Unchanged | Unchanged |

The core growing-graph orchestrator from Session 8 required **zero modifications** to support the Browser skill — it plugged in as a new skill entry like any other.
