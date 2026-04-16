# Spike: Local Model Setup and Eval on RTX 5090

**Ticket:** WOR-76
**Benchmark ticket:** WOR-58 (Pass UserPreferences into Jinja2 template context)
**Hardware:** RTX 5090 — 32607 MiB VRAM

---

## VRAM budget

| Model | VRAM after load | Free for KV cache | Verdict |
|---|---|---|---|
| qwen2.5-coder:32b-instruct-q4_K_M | 28456 MiB | ~4 GB | Too tight |
| qwen3-coder:30b | 24108 MiB | ~8.5 GB | Use this |

`num_ctx: 65536` (64K tokens) is the recommended default with qwen3-coder:30b — fits within the ~8.5 GB headroom.
256K (native max) will OOM.

---

## Setup

### 1. Install Ollama (PowerShell)

```powershell
irm https://ollama.com/install.ps1 | iex
ollama --version
```

To change where models are stored, set the `OLLAMA_MODELS` environment variable before pulling.

### 2. Pull the model

```bash
ollama pull qwen3-coder:30b
```

### 3. Useful Ollama commands

```bash
ollama ls                        # list downloaded models
ollama run qwen3-coder:30b       # interactive shell (optional — not needed for proxy use)
ollama ps                        # show running models
ollama stop qwen3-coder:30b      # unload from VRAM
```

Local API base: `http://localhost:11434`

### 4. Install LiteLLM proxy

Claude Code sends requests in Anthropic format. Ollama speaks OpenAI format. LiteLLM bridges them.

```bash
pip install 'litellm[proxy]'
```

### 5. Create `litellm-local.yaml` (repo root, not committed)

```yaml
model_list:
    - model_name: claude-sonnet-4-6
      litellm_params:
        model: ollama_chat/qwen3-coder:30b
        api_base: http://localhost:11434
        extra_body:
          options:
            num_ctx: 65536
    - model_name: "*"
      litellm_params:
        model: ollama_chat/qwen3-coder:30b
        api_base: http://localhost:11434
        extra_body:
          options:
            num_ctx: 65536
```

`model_name: claude-sonnet-4-6` must match the model ID Claude Code puts in the request.
The wildcard entry catches any other model names Claude Code may send.

### 6. Start the proxy (keep terminal open)

```bash
litellm --config litellm-local.yaml --port 8082 --drop_params
```

`--drop_params` silently drops Anthropic-specific parameters Ollama doesn't understand. Without it you get 400 errors.

### 7. Launch Claude Code routed to local (new terminal)

```bash
# Linux / macOS
ANTHROPIC_BASE_URL=http://localhost:8082 ANTHROPIC_API_KEY=sk-dummy claude

# Windows CMD
set ANTHROPIC_BASE_URL=http://localhost:8082
set ANTHROPIC_API_KEY=sk-dummy
claude --model qwen3-coder:30b
```

`ANTHROPIC_BASE_URL` must point to LiteLLM (port 8082), not Ollama (port 11434).

---

## Benchmark results — WOR-58

Ticket: add one `PrefsStore.load()` call in `app/cli.py` and pass the result to `generate()`.
Triggers ruff, bandit, and pytest hooks on file edit.

### Hook results

| Stage | Result |
|---|---|
| ruff lint + format | PASS |
| bandit | PASS |
| pytest (80 tests) | PASS — 96% coverage |

### Escalation events

| # | Context config | Stage | Behaviour | Category |
|---|---|---|---|---|
| 1 | 2048 (Ollama default) | Post-tool-result | Called `get_issue`, received result, went silent | Agentic loop failure — context too small to hold system prompt + tool result |
| 2 | 2048 | Task objective | Lost goal mid-session, asked "what would you like me to do?" | Context overflow — objective dropped |
| 3 | 2048 | Tool call generation | Emitted `<function=TaskList>` (wrong API format) | Hallucinated tool call syntax |
| 4 | 65536 | Git operations | `Permission denied` on `.git/FETCH_HEAD` | Expected — session conflict with parent Claude Code process, not a model failure |
| 5 | 65536 | Linear workflow | Did not move ticket to In Progress | Missing behaviour — Linear status updates need orchestrator support |

Events 1–3 disappeared entirely after switching to 32K context. Events 4–5 are environmental/orchestration gaps, not model capability failures.

### Observations

- At 2048 context (Ollama default): agentic loop breaks immediately. The system prompt alone fills the window.
- At 65536 context: model read three files, identified the correct gap in `cli.py`, generated the right two-line fix, and passed all hooks.
- Wall time for the full ticket run: ~3 minutes.
- The model did not move the Linear ticket to In Progress — this needs to be handled by the orchestrator layer or the `/start-ticket` command script.

---

## Go / no-go

**Conditional GO** for bounded coding tasks with human approval gates.

The model is capable of:
- Reading Linear issues and understanding ticket scope
- Reading and comprehending existing code
- Identifying the correct minimal change
- Generating clean, passing code (ruff + pytest)

The model is not suitable for:
- Fully autonomous agentic operation (needs human nudges and approval gates)
- Linear workflow management (ticket state updates need orchestrator support)
- Tasks requiring more than ~20K tokens of context per turn

**Recommended use pattern:** local model handles code generation under human supervision; cloud model handles orchestration, multi-step planning, and Linear updates.
