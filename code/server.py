"""Web UI server for the AI Shopping Agent.

Serves a single-page UI at http://localhost:8000.
The CLI (flow.py) remains completely unchanged.

Run:   uv run python server.py
Then open: http://localhost:8000
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

CODE_DIR = Path(__file__).parent
app = FastAPI(title="AI Shopping Agent")


# ── Single-page HTML ──────────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Shopping Agent</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  /* markdown table */
  #resultContent table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 0.5rem;
    font-size: 0.875rem;
  }
  #resultContent th {
    background: #1e293b;
    color: #94a3b8;
    padding: 0.5rem 0.75rem;
    text-align: left;
    font-weight: 600;
    border-bottom: 2px solid #334155;
  }
  #resultContent td {
    padding: 0.5rem 0.75rem;
    border-bottom: 1px solid #1e293b;
    vertical-align: top;
  }
  #resultContent tr:hover td { background: #0f172a; }
  #resultContent del { color: #64748b; }
  #resultContent p { margin: 0.75rem 0; color: #cbd5e1; }

  /* spinner */
  @keyframes spin { to { transform: rotate(360deg); } }
  .spinner {
    display: inline-block;
    width: 1rem; height: 1rem;
    border: 2px solid #334155;
    border-top-color: #3b82f6;
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    vertical-align: middle;
    margin-right: 0.4rem;
  }

  /* log line colours */
  .log-complete { color: #4ade80; }
  .log-running  { color: #facc15; }
  .log-error    { color: #f87171; }
  .log-session  { color: #818cf8; font-weight: 600; }
  .log-dim      { color: #475569; }
  .log-normal   { color: #94a3b8; }

  /* agent graph */
  #agentGraph { display: block; width: 100%; overflow: visible; }
  .ag-node-running rect {
    animation: ag-glow 1.1s ease-in-out infinite alternate;
  }
  @keyframes ag-glow {
    from { filter: none; }
    to   { filter: drop-shadow(0 0 5px #3b82f6); }
  }
</style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen antialiased">

<div class="max-w-5xl mx-auto px-4 py-8">

  <!-- Header -->
  <div class="mb-8">
    <h1 class="text-2xl font-bold tracking-tight text-white">AI Shopping Agent</h1>
    <p class="text-slate-400 text-sm mt-1">Autonomous browser + LLM pipeline &mdash; Session 9</p>
  </div>

  <!-- Query bar -->
  <div class="flex gap-3 mb-6">
    <input id="queryInput" type="text"
      class="flex-1 bg-slate-900 border border-slate-700 rounded-lg px-4 py-2.5
             text-white placeholder-slate-500 text-sm
             focus:outline-none focus:ring-2 focus:ring-blue-600 focus:border-transparent"
      placeholder="Enter your shopping query…"
      value="Find and compare 3 best mechanical wireless keyboards under ₹8,000 on Amazon.in — give name, price, rating, and key specs."
    />
    <button id="runBtn" onclick="runQuery()"
      class="bg-blue-600 hover:bg-blue-500 active:bg-blue-700 disabled:opacity-50
             disabled:cursor-not-allowed text-white px-5 py-2.5 rounded-lg
             text-sm font-semibold transition-colors whitespace-nowrap">
      Search
    </button>
  </div>

  <!-- Agent Graph -->
  <div id="graphCard" class="hidden mb-5">
    <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
      <div class="flex items-center justify-between mb-3">
        <span class="text-xs font-semibold uppercase tracking-wider text-slate-500">Agent Graph</span>
        <span id="graphNodeCount" class="text-xs text-slate-600"></span>
      </div>
      <div class="overflow-x-auto">
        <svg id="agentGraph" height="120"></svg>
      </div>
    </div>
  </div>

  <!-- Pipeline progress -->
  <div id="progressCard" class="hidden mb-5">
    <div class="bg-slate-900 border border-slate-800 rounded-xl p-4">
      <div class="flex items-center justify-between mb-3">
        <span class="text-xs font-semibold uppercase tracking-wider text-slate-500">Pipeline</span>
        <span id="statusBadge"
          class="text-xs px-2 py-0.5 rounded-full bg-blue-900/60 text-blue-300">running</span>
      </div>
      <div id="progressLog" class="font-mono text-xs space-y-0.5 max-h-52 overflow-y-auto"></div>
    </div>
  </div>

  <!-- Results -->
  <div id="resultsCard" class="hidden">
    <div class="bg-slate-900 border border-slate-800 rounded-xl p-5">
      <div class="flex items-center justify-between mb-4">
        <span class="text-xs font-semibold uppercase tracking-wider text-slate-500">Results</span>
        <span id="elapsedBadge" class="text-xs px-2 py-0.5 rounded-full bg-slate-800 text-slate-400"></span>
      </div>
      <div id="resultContent" class="text-slate-200"></div>
    </div>
  </div>

</div>

<script>
const $ = id => document.getElementById(id);
marked.setOptions({ gfm: true, breaks: false });

// ── log helpers ──────────────────────────────────────────────────────────────
function classifyLine(text) {
  if (/complete\b/i.test(text))            return 'log-complete';
  if (/running\b/i.test(text))             return 'log-running';
  if (/error|failed|exception/i.test(text))return 'log-error';
  if (/^session\s+s8-/i.test(text.trim())) return 'log-session';
  if (/^[═─]+$/.test(text.trim()))         return 'log-dim';
  if (/^\[memory\]/.test(text.trim()))     return 'log-dim';
  return 'log-normal';
}
function appendLog(text) {
  if (!text.trim()) return;
  const div = document.createElement('div');
  div.className = classifyLine(text);
  div.textContent = text;
  $('progressLog').appendChild(div);
  $('progressLog').scrollTop = $('progressLog').scrollHeight;
}

// ── agent graph state ────────────────────────────────────────────────────────
let ag = { nodes: {}, edges: [] };

const AG_STATUS_STROKE = { pending:'#475569', running:'#3b82f6', complete:'#22c55e', failed:'#ef4444', skipped:'#f59e0b' };
const AG_STATUS_FILL   = { pending:'#1e293b', running:'#1e3a5f', complete:'#14532d', failed:'#450a0a', skipped:'#431407' };
const AG_STATUS_TEXT   = { pending:'#64748b', running:'#93c5fd', complete:'#86efac', failed:'#fca5a5', skipped:'#fde68a' };
const NW = 118, NH = 40, HG = 22, VG = 58;

function agAdd(id, skill, deps) {
  ag.nodes[id] = { id, skill, deps, status: 'pending', elapsed: null };
  for (const d of deps) ag.edges.push({ from: d, to: id });
  renderAg();
}
function agRun(id) {
  if (ag.nodes[id]) ag.nodes[id].status = 'running';
  renderAg();
}
function agDone(id, status, elapsed) {
  if (ag.nodes[id]) { ag.nodes[id].status = status; ag.nodes[id].elapsed = elapsed; }
  renderAg();
}

function computeAgLevels() {
  const ids = Object.keys(ag.nodes);
  const edges = ag.edges.filter(e => ag.nodes[e.from] && ag.nodes[e.to]);
  const adjOut = {}, inDeg = {};
  ids.forEach(id => { adjOut[id] = []; inDeg[id] = 0; });
  edges.forEach(e => { adjOut[e.from].push(e.to); inDeg[e.to]++; });

  const level = {};
  ids.forEach(id => level[id] = 0);
  const rem = { ...inDeg };
  const q = ids.filter(id => rem[id] === 0);
  while (q.length) {
    const n = q.shift();
    for (const c of adjOut[n]) {
      level[c] = Math.max(level[c], level[n] + 1);
      if (--rem[c] === 0) q.push(c);
    }
  }
  return level;
}

function renderAg() {
  const svg = $('agentGraph');
  const ids = Object.keys(ag.nodes);
  if (!ids.length) return;

  $('graphCard').classList.remove('hidden');
  $('graphNodeCount').textContent = ids.length + ' node' + (ids.length !== 1 ? 's' : '');

  const level = computeAgLevels();
  const byLevel = {};
  ids.forEach(id => {
    const lv = level[id] || 0;
    (byLevel[lv] = byLevel[lv] || []).push(id);
  });

  const maxLv  = Math.max(...Object.keys(byLevel).map(Number));
  const maxCnt = Math.max(...Object.values(byLevel).map(a => a.length));
  const svgW   = Math.max(maxCnt * (NW + HG) + HG, NW + HG * 2);
  const svgH   = (maxLv + 1) * (NH + VG) + VG / 2;

  svg.setAttribute('viewBox', `0 0 ${svgW} ${svgH}`);
  svg.style.height = svgH + 'px';
  svg.style.minWidth = svgW + 'px';

  const pos = {};
  Object.entries(byLevel).forEach(([lv, lvIds]) => {
    const lvNum = +lv;
    const totalW = lvIds.length * NW + (lvIds.length - 1) * HG;
    const sx = (svgW - totalW) / 2;
    lvIds.forEach((id, i) => {
      pos[id] = { x: sx + i * (NW + HG), y: lvNum * (NH + VG) + VG / 2 };
    });
  });

  // build SVG
  let edges = '';
  for (const e of ag.edges) {
    if (!pos[e.from] || !pos[e.to]) continue;
    const f = pos[e.from], t = pos[e.to];
    const x1 = f.x + NW/2, y1 = f.y + NH;
    const x2 = t.x + NW/2, y2 = t.y;
    const my = (y1 + y2) / 2;
    edges += `<path d="M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}"
      fill="none" stroke="#334155" stroke-width="1.5" marker-end="url(#ag-arr)"/>`;
  }

  let nodes = '';
  for (const [id, node] of Object.entries(ag.nodes)) {
    if (!pos[id]) continue;
    const { x, y } = pos[id];
    const st  = node.status;
    const stk = AG_STATUS_STROKE[st] || '#475569';
    const fil = AG_STATUS_FILL[st]   || '#1e293b';
    const tc  = AG_STATUS_TEXT[st]   || '#64748b';
    const sub = node.elapsed !== null ? `${node.elapsed}s` : st;
    const cls = st === 'running' ? ' class="ag-node-running"' : '';
    nodes += `<g${cls}>
      <rect x="${x}" y="${y}" width="${NW}" height="${NH}" rx="7"
            fill="${fil}" stroke="${stk}" stroke-width="1.5"/>
      <text x="${x+NW/2}" y="${y+15}" text-anchor="middle"
            fill="${tc}" font-size="11.5" font-family="ui-monospace,monospace" font-weight="700">
        ${node.skill}
      </text>
      <text x="${x+NW/2}" y="${y+29}" text-anchor="middle"
            fill="${tc}" font-size="9" font-family="ui-monospace,monospace" opacity="0.7">
        ${id} · ${sub}
      </text>
    </g>`;
  }

  svg.innerHTML = `<defs>
    <marker id="ag-arr" viewBox="0 0 10 10" refX="9" refY="5"
            markerWidth="5" markerHeight="5" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#334155"/>
    </marker>
  </defs>${edges}${nodes}`;
}

// ── main query runner ────────────────────────────────────────────────────────
let _startTime = 0;

async function runQuery() {
  const query = $('queryInput').value.trim();
  if (!query) return;

  const btn = $('runBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Running…';

  // reset UI
  ag = { nodes: {}, edges: [] };
  $('graphCard').classList.add('hidden');
  $('progressCard').classList.remove('hidden');
  $('resultsCard').classList.add('hidden');
  $('progressLog').innerHTML = '';
  $('resultContent').innerHTML = '';
  $('statusBadge').textContent = 'running';
  $('statusBadge').className   = 'text-xs px-2 py-0.5 rounded-full bg-blue-900/60 text-blue-300';
  $('elapsedBadge').textContent = '';
  _startTime = Date.now();

  try {
    const resp = await fetch('/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    });

    const reader  = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop() ?? '';

      for (const chunk of parts) {
        if (!chunk.startsWith('data:')) continue;
        let evt;
        try { evt = JSON.parse(chunk.slice(5).trim()); } catch { continue; }

        if (evt.type === 'log') {
          appendLog(evt.text);
        } else if (evt.type === 'graph_add') {
          agAdd(evt.id, evt.skill, evt.deps);
        } else if (evt.type === 'graph_run') {
          agRun(evt.id);
        } else if (evt.type === 'graph_done') {
          agDone(evt.id, evt.status, evt.elapsed);
        } else if (evt.type === 'done') {
          const elapsed = ((Date.now() - _startTime) / 1000).toFixed(1);
          $('statusBadge').textContent = 'done';
          $('statusBadge').className   = 'text-xs px-2 py-0.5 rounded-full bg-green-900/60 text-green-300';
          $('elapsedBadge').textContent = elapsed + 's';
          if (evt.result && evt.result.trim()) {
            $('resultsCard').classList.remove('hidden');
            $('resultContent').innerHTML = marked.parse(evt.result);
          } else {
            appendLog('⚠ No result returned.');
          }
        }
      }
    }
  } catch (e) {
    appendLog('Error: ' + e.message);
    $('statusBadge').textContent = 'error';
    $('statusBadge').className   = 'text-xs px-2 py-0.5 rounded-full bg-red-900/60 text-red-300';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Search';
  }
}

$('queryInput').addEventListener('keydown', e => { if (e.key === 'Enter') runQuery(); });
</script>
</body>
</html>
"""


# ── API ───────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    query: str


@app.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return HTMLResponse(_HTML)


@app.post("/query")
async def run_query(req: QueryRequest) -> StreamingResponse:
    """Run flow.py as a subprocess, stream its stdout as SSE events."""

    async def generate():
        proc = await asyncio.create_subprocess_exec(
            "uv", "run", "python", "flow.py", req.query,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(CODE_DIR),
        )

        final_lines: list[str] = []
        in_final = False

        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")

            # ── graph structured events (not forwarded to log) ──────────────
            if line.startswith("[graph] add "):
                parts = line.split()
                if len(parts) >= 5:
                    nid   = parts[2]
                    skill = parts[3][len("skill="):]
                    raw_d = parts[4][len("deps="):]
                    deps  = [] if raw_d == "-" else [d for d in raw_d.split(",") if d.startswith("n:")]
                    yield f"data: {json.dumps({'type':'graph_add','id':nid,'skill':skill,'deps':deps})}\n\n"
                continue  # skip forwarding to log

            if line.startswith("[graph] run "):
                parts = line.split()
                if len(parts) >= 3:
                    yield f"data: {json.dumps({'type':'graph_run','id':parts[2]})}\n\n"
                continue

            if line.startswith("[graph] done "):
                parts = line.split()
                if len(parts) >= 5:
                    nid     = parts[2]
                    status  = parts[3][len("status="):]
                    elapsed = float(parts[4][len("elapsed="):]) if len(parts) > 4 else 0.0
                    yield f"data: {json.dumps({'type':'graph_done','id':nid,'status':status,'elapsed':elapsed})}\n\n"
                continue

            # ── FINAL block capture ─────────────────────────────────────────
            if "FINAL:" in line:
                in_final = True
                after = line.split("FINAL:", 1)[1].strip()
                if after:
                    final_lines.append(after)
            elif in_final:
                if line.startswith("═" * 10):
                    in_final = False
                else:
                    final_lines.append(line)

            # Suppress internal memory noise from the pipeline log
            if line.startswith("[memory"):
                continue

            yield f"data: {json.dumps({'type': 'log', 'text': line})}\n\n"

        await proc.wait()
        result = "\n".join(final_lines).strip()
        yield f"data: {json.dumps({'type': 'done', 'result': result})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"\nAI Shopping Agent UI  →  http://localhost:{port}\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
