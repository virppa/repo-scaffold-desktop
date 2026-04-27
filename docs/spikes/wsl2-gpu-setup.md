# WSL2 GPU Setup — RTX 5090 / Windows 11

Setup log for running vLLM (and other GPU workloads) under WSL2 on Windows 11.
Tied to WOR-118 (vLLM spike). Updated as we go.

**Hardware:** RTX 5090 32 GB (Blackwell, SM_120) · Windows 11 Pro 10.0.26200
**WSL2 config:** 32 processors · 80 GB RAM (of 96 GB total) · 12 GB swap on S:\30_Pagefile\wsl-swap.vhdx · vmIdleTimeout=-1 · sparseVhd=true
**Ubuntu VHD location:** `D:\Data\80_VMs\WSL_Backups\ext4.vhdx` (moved from C: via `wsl --manage Ubuntu-24.04 --move`)
**`.wslconfig` location:** `C:\Users\Antti\.wslconfig`

---

## Status

| Step | Status | Notes |
|---|---|---|
| WSL2 installed | ✅ | v2.6.3, Ubuntu 24.04 |
| Reboot | ✅ | |
| Ubuntu first-run | ✅ | user: antti, host: Puntti |
| NVIDIA driver check | ✅ | Driver 596.21 · CUDA 13.2 · RTX 5090 32 GB visible |
| CUDA toolkit (WSL) | ✅ | cuda-toolkit-12-8 · nvcc 12.8.93 confirmed |
| Python / venv | ✅ | Python 3.12, vllm-env |
| vLLM install | ✅ | vllm-0.20.0 prebuilt wheel — no source build needed, Blackwell wheels available |
| Smoke test | ✅ | Qwen3-0.6B — FlashAttention, CUDA graphs, localhost→WSL2 all working |

---

## Step 1 — Reboot

After reboot, Ubuntu 24.04 will auto-launch and prompt for a Linux username and
password. These are independent of your Windows credentials — pick anything.

```powershell
# Or from PowerShell after reboot, launch manually:
wsl -d Ubuntu-24.04
```

---

## Step 2 — Ubuntu first-run

On first launch Ubuntu will ask:
```
Enter new UNIX username: antti        # lowercase, no spaces
New password: ****
```

Then update packages:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y build-essential git curl wget
```

---

## Step 3 — NVIDIA driver (Windows side — nothing to install in Linux)

This is the main WSL2 gotcha. The NVIDIA driver lives on the Windows side.
**Do NOT run `apt install nvidia-driver-*` inside WSL2** — it will break GPU access.

Verify the Windows driver is visible from inside WSL2:
```bash
nvidia-smi
```

Expected output: your RTX 5090, driver version, CUDA version. If this fails,
the Windows NVIDIA driver is too old — update it on the Windows side first.

---

## Step 4 — CUDA toolkit (Linux side)

Install the CUDA toolkit inside WSL2 — the compute libraries only, not the driver.
Use NVIDIA's WSL-specific repo:

```bash
# Add NVIDIA CUDA repo for WSL-Ubuntu
wget https://developer.download.nvidia.com/compute/cuda/repos/wsl-ubuntu/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt update

# Install toolkit (no driver — that's the wsl-ubuntu repo's default)
sudo apt install -y cuda-toolkit-12-8
```

Add to `~/.bashrc`:
```bash
export PATH=/usr/local/cuda/bin:$PATH
export LD_LIBRARY_PATH=/usr/local/cuda/lib64:$LD_LIBRARY_PATH
```

Reload and verify:
```bash
source ~/.bashrc
nvcc --version   # should show 12.8.x
```

---

## Step 5 — Python environment

Ubuntu 24.04 ships Python 3.12. Use a venv for vLLM to avoid polluting the system:

```bash
sudo apt install -y python3-pip python3-venv
python3 -m venv ~/vllm-env
source ~/vllm-env/bin/activate
pip install --upgrade pip
```

---

## Step 6 — vLLM install

```bash
pip install vllm
```

**If this fails with SM_120 / Blackwell errors:** the prebuilt wheel doesn't include
SM_120 kernels. Build from source:

```bash
pip install ninja packaging wheel
git clone https://github.com/vllm-project/vllm.git
cd vllm
TORCH_CUDA_ARCH_LIST="12.0" pip install -e . --no-build-isolation
```

This takes 20–40 minutes. Document the outcome here.

---

## Step 7 — Smoke test

Start with a small model to confirm GPU access before loading the 35B weights:

```bash
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-0.6B \
  --port 8000 \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.3
```

Then from another terminal (or Windows):
```bash
curl http://localhost:8000/v1/models
```

---

## Step 8 — 35b-a3b server

Once smoke test passes:

```bash
python -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3.6-35B-A3B \
  --port 8000 \
  --max-model-len 131072 \
  --gpu-memory-utilization 0.90 \
  --enable-prefix-caching \
  --reasoning-parser qwen3 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder
```

Note: `--reasoning-parser`, `--enable-auto-tool-choice`, and `--tool-call-parser`
are required for tool use. Omitting them silently breaks all tool calls.

---

## WSL2 essentials for Linux users

Things that differ from a native Linux install:

**Filesystem:**
- Your Linux home is `~/` inside WSL2 — stored in a virtual disk (`.vhdx`)
- Windows drives are at `/mnt/c/`, `/mnt/d/`, etc.
- Keep project files and model weights in `~/` (Linux fs) — I/O across `/mnt/c/`
  is 5–10× slower due to the translation layer
- HuggingFace cache default `~/.cache/huggingface/` is fine; don't move it to `/mnt/`

**Networking:**
- `localhost` in WSL2 resolves to Windows localhost — a server started in WSL2
  is accessible from Windows at `localhost:<port>` without any extra config
- WSL2 gets its own IP (visible with `ip addr`) but this changes on restart

**Memory:**
- WSL2 defaults to ~50% of system RAM. With 96 GB, set to 80 GB via the WSL
  Settings GUI (Memory and processor → 81920 MB), leaving ~16 GB for Windows.
- VM idle timeout defaults to 60 seconds — WSL shuts down automatically after
  60s of idle. Set to 0 (disabled) in Optional Features; essential for running
  a persistent vLLM server.
- Sparse VHD: enable in Optional Features — the Ubuntu virtual disk grows on
  demand rather than pre-allocating, saving space on C:.
- Swap file location GUI picker is non-functional in WSL 2.6.3. Set via
  `.wslconfig` after first run (see Step 1 notes).
- Changes require `wsl --shutdown` and restart to take effect.

**GPU:**
- The WDDM driver on Windows is exposed to WSL2 — this is why `nvidia-smi` works
  without installing a Linux driver. CUDA compute goes through this same path.
- GPU memory is shared with Windows. If Ollama is running on Windows, it holds
  VRAM. Shut it down before starting vLLM in WSL2.

**Restarting WSL2:**
```powershell
# From Windows PowerShell — shuts down all WSL instances cleanly
wsl --shutdown
# Then relaunch: wsl -d Ubuntu-24.04
```

---

## Findings log

| Date | Finding |
|---|---|
| 2026-04-27 | WSL2 2.6.3 + Ubuntu 24.04 installed successfully |
| 2026-04-27 | nvidia-smi works in WSL2 — driver 596.21, CUDA 13.2, RTX 5090 32 GB visible. Ollama holds 2466 MiB on Windows side. |
| 2026-04-27 | vllm-0.20.0 installs from prebuilt wheel — SM_120 included, no source build required |
| 2026-04-27 | Smoke test passed — vLLM 0.20.0 on RTX 5090/SM_120, no source build needed. FlashAttention v2, CUDA graphs, Windows→WSL2 localhost all working. |
| 2026-04-27 | WSL2 overhead source identified: pin_memory=False (pinned CPU↔GPU transfers disabled). GPU compute path unaffected. Impact TBD vs Ollama baseline. |
| — | Overhead vs Ollama baseline: TBD (run prefill_shared tier against both) |
