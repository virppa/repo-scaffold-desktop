# WOR-344 Spike: vLLM Native Anthropic Messages API

**Question:** Does our pinned vLLM (0.20.0) serve the Anthropic Messages API natively so we can drop the LiteLLM proxy?

**TL;DR:** **Yes — fully supported, no opt-in flag required.** vLLM 0.20.0 mounts `/v1/messages` and `/v1/messages/count_tokens` on the same OpenAI-compatible server. LiteLLM can be retired once the live test script in `scripts/spikes/wor344_vllm_anthropic_probe.py` returns all-PASS against our model.

## Environment

- vLLM: **0.20.0** (`vllm --version` in `vllm-env`, captured 2026-05-04)
- Source confirmation: `vllm/entrypoints/anthropic/{api_router,serving,protocol}.py`
- Mount point: `vllm/entrypoints/openai/generate/api_router.py` calls `register_anthropic_api_router(app)` unconditionally for any model that supports the `generate` task — meaning **any standard `vllm serve <model>` invocation already exposes the Anthropic API**.
- Model: Qwen3.6-35B-A3B-NVFP4 (`/home/antti/models/Qwen3.6-35B-A3B-NVFP4`)

## What vLLM 0.20.0 actually exposes

Routes registered on the OpenAI-compatible HTTP server, alongside `/v1/chat/completions`:

| Route | Method | Purpose |
|---|---|---|
| `/v1/messages` | POST | Anthropic Messages API (request → response, supports `stream: true` SSE) |
| `/v1/messages/count_tokens` | POST | Anthropic token counting |

Internally, `AnthropicServingMessages._convert_anthropic_to_openai_request` translates the inbound payload to vLLM's existing chat-completion code path, then `_convert_openai_to_anthropic_response` (and the streaming counterpart) converts back. Tool definitions, `tool_use` / `tool_result` blocks, system prompts, stop sequences, `top_k`, `kv_transfer_params`, and `chat_template_kwargs` are all forwarded.

**No new CLI flag.** The router is mounted whenever generate is supported. Reasoning + tool parsers are reused from the existing flags:
- `--enable-auto-tool-choice`
- `--tool-call-parser qwen3_coder`  *(qwen3_coder is in the 0.20.0 parser list)*
- `--reasoning-parser qwen3`        *(still a top-level flag)*

## Caveats / things that changed since the spike was filed

1. **Prefix-cache attribution header workaround is no longer needed.** The vLLM doc only recommends `CLAUDE_CODE_ATTRIBUTION_HEADER=0` for vLLM ≤ 0.17.1. We're on 0.20.0, so the per-request hash that previously broke prefix caching is handled server-side.
2. **Claude Code expects three model env vars.** Claude Code routes Opus/Sonnet/Haiku separately; with one served vLLM model we have to pin all three to the same name:
   ```
   ANTHROPIC_DEFAULT_OPUS_MODEL=<served-model-name>
   ANTHROPIC_DEFAULT_SONNET_MODEL=<served-model-name>
   ANTHROPIC_DEFAULT_HAIKU_MODEL=<served-model-name>
   ```
3. **`--served-model-name` is mandatory** if the model path contains `/` (Hugging Face style) — Claude Code doesn't accept slashes in model identifiers. We already use a flat path so this only matters if we ever switch to a Hugging Face spec.
4. **No streaming-specific issues observed in the source** — all four required SSE events (`message_start`, `content_block_start`, `content_block_delta`, `message_stop`) are emitted by `AnthropicServingMessages`.

## Step-by-step verification (operator runbook)

The probe script does steps 4–6 automatically. Steps 1–3 are manual because they affect long-running daemons.

### 1. Stop LiteLLM (terminal it runs in)

```
Ctrl+C  # in the litellm --config litellm-local.yaml --port 8082 terminal
```

LiteLLM is intentionally not stopped programmatically — leaving it manual lets you compare side-by-side if the Anthropic API misbehaves.

### 2. Start vLLM (the existing CLAUDE.md command, unchanged)

In WSL2:
```bash
/home/antti/vllm-env/bin/vllm serve /home/antti/models/Qwen3.6-35B-A3B-NVFP4 \
  --served-model-name qwen3-coder \
  --max-model-len 262144 --max-num-seqs 16 \
  --kv-cache-dtype fp8 --max-num-batched-tokens 4096 \
  --reasoning-parser qwen3 --enable-prefix-caching \
  --language-model-only --safetensors-load-strategy prefetch \
  --enable-auto-tool-choice --tool-call-parser qwen3_coder
```

> The only change vs. the CLAUDE.md command is the explicit `--served-model-name qwen3-coder`. Any short stable name works — write it down, you'll use it in step 4.

Wait for `Application startup complete.` in the logs.

### 3. Run the automated probe (Windows PowerShell or WSL — uses stdlib only)

From the repo root:
```bash
python scripts/spikes/wor344_vllm_anthropic_probe.py --model qwen3-coder
```

You should see six checks: `/v1/models`, non-streaming message, streaming SSE, tool_use, tool_result followup, count_tokens. Exit code is 0 when all required tests pass.

If any test fails, paste the failing test's PASS/FAIL block into the **Live test results** section below — the probe's per-test output already includes the diagnostic detail (HTTP code, stop_reason, missing event types) that we need to make the migration call.

### 4. Live tool-use smoke test with `claude` CLI

In a fresh PowerShell window:
```
$env:ANTHROPIC_BASE_URL = "http://localhost:8000"
$env:ANTHROPIC_API_KEY = "sk-dummy"
$env:ANTHROPIC_AUTH_TOKEN = "sk-dummy"
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = "qwen3-coder"
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = "qwen3-coder"
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = "qwen3-coder"
claude --model claude-sonnet-4-6 -p "List the files in the repo root using the Bash tool, then summarize."
```

This covers the "tool_use → tool_result handshake works in Anthropic format" AC bullet beyond what the probe script can measure (it tests the protocol; this tests Claude Code's full integration).

## Live test results

Captured 2026-05-04 against the production vLLM 0.20.0 launch (Qwen3.6-35B-A3B-NVFP4, served as `qwen3-coder`).

| Check | Status | Notes |
|---|---|---|
| `/v1/models` returns served name | ✅ | `served: ['qwen3-coder']`, 2045 ms (cold) |
| `/v1/messages` non-streaming | ✅ | After probe v2 fix (raised `max_tokens`, accept `thinking`-only): emits a Qwen3 `thinking` block with a `signature` field — Anthropic extended-thinking shape preserved end-to-end. v1 probe rejected this because it only inspected `text` blocks |
| `/v1/messages` streaming SSE | ✅ | All required events plus `content_block_stop` + `message_delta`: `['content_block_delta', 'content_block_start', 'content_block_stop', 'message_delta', 'message_start', 'message_stop']` |
| `/v1/messages` tool_use | ✅ | `stop_reason=tool_use`, `id=chatcmpl-tool-…`, `input={'city': 'Helsinki'}` — qwen3_coder parser produced clean Anthropic-shaped `tool_use` block |
| `/v1/messages` tool_result followup | ✅ | `stop_reason=end_turn`, model reads back: *"The current weather in Helsinki is 4°C and partly cloudy."* — the full tool-use → tool_result handshake works without LiteLLM in the path |
| `/v1/messages/count_tokens` | ✅ | `input_tokens=13` for the 3-word probe |
| Reasoning blocks (Qwen3 thinking) flow through | ✅ | Confirmed via the v1 probe failure log — `content[0]` is `{"type": "thinking", "thinking": "…", "signature": "ab3a9a17…"}`. vLLM synthesises the `signature` per response so Claude Code's signature-passthrough invariant holds |
| `claude --model claude-sonnet-4-6 -p` smoke | ☐ | Operator step — fill in after running the PowerShell snippet in the runbook |

### Key observation

vLLM's `AnthropicServingMessages` doesn't strip the qwen3 `<think>` block. It promotes it to a first-class `thinking` content block alongside any text the model emits. That means moving from LiteLLM to direct vLLM is **strictly more capable** for our stack — LiteLLM didn't preserve Qwen3 thinking blocks in Anthropic format at all.

## Decision criteria

- **All PASS, claude smoke test green** → file the implementation ticket (proposed scope below) and queue for the watcher.
- **Tool-use FAIL or `stop_reason != tool_use`** → likely a `--tool-call-parser` mismatch. Try `--tool-call-parser hermes` as a fallback before declaring partial support.
- **Streaming FAIL but non-streaming PASS** → block migration; LiteLLM stays. Open an upstream issue with the missing-event diagnostic from the probe.
- **count_tokens FAIL** → not blocking. Claude Code rarely calls it; document as a known gap.

## Implementation ticket

Filed as **WOR-368** — *Drop LiteLLM proxy — point Claude Code directly at vLLM /v1/messages*. Watcher-routed (`Fix` + `local-ready`), P3, related to WOR-344 + WOR-339. Live results above were the unblock; the full scope and AC live on the ticket.

## References

- vLLM Claude Code integration doc: <https://docs.vllm.ai/en/stable/serving/integrations/claude_code/#configuring-claude-code>
- Source files inspected: `vllm/entrypoints/anthropic/api_router.py`, `vllm/entrypoints/anthropic/serving.py`, `vllm/entrypoints/openai/generate/api_router.py`
- Probe script: `scripts/spikes/wor344_vllm_anthropic_probe.py`
- Sibling spikes (independent, do not block): WOR-345 (MTP speculative decoding), WOR-346 (draft-model speculative decoding)
- Bug class eliminated: WOR-339 (LiteLLM orphan-tab)
