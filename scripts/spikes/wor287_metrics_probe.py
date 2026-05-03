"""WOR-287 — vLLM /metrics endpoint probe.

Lists every Prometheus metric vLLM exposes that mentions cache / prefix /
kv. Polls twice with a 5-second gap so counter movement (or stillness)
is visible.

Usage:
    python scripts/spikes/wor287_metrics_probe.py
    python scripts/spikes/wor287_metrics_probe.py --base-url http://localhost:8000

Exit codes:
    0  ok (metrics endpoint reachable, JSON dump written)
    2  /metrics endpoint unreachable
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_BASE_URL = "http://localhost:8000"
_FILTER_PATTERN = re.compile(r"cache|prefix|kv|hit|miss", re.IGNORECASE)
_ARTIFACT_DIR = Path("docs/spikes/_wor287_artifacts")


def _fetch_metrics(base_url: str) -> str:
    url = f"{base_url.rstrip('/')}/metrics"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310 — localhost only
        text: str = resp.read().decode("utf-8", errors="replace")
    return text


def _parse_prometheus(text: str) -> list[dict[str, Any]]:
    """Parse Prometheus text format. Returns list of {name, value, type, help, labels}."""
    metric_meta: dict[str, dict[str, str]] = {}
    samples: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# HELP "):
            parts = line[len("# HELP ") :].split(" ", 1)
            if len(parts) == 2:
                name, help_text = parts
                metric_meta.setdefault(name, {})["help"] = help_text
            continue
        if line.startswith("# TYPE "):
            parts = line[len("# TYPE ") :].split(" ", 1)
            if len(parts) == 2:
                name, type_text = parts
                metric_meta.setdefault(name, {})["type"] = type_text
            continue
        if line.startswith("#"):
            continue
        # sample line: name{labels} value
        m = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(.+)$", line)
        if not m:
            continue
        name, labels_blob, value = m.group(1), m.group(2) or "", m.group(3)
        try:
            v: float | str = float(value.split()[0])
        except ValueError:
            v = value
        meta = metric_meta.get(name, {})
        samples.append(
            {
                "name": name,
                "labels": labels_blob,
                "value": v,
                "type": meta.get("type", "?"),
                "help": meta.get("help", ""),
            }
        )
    return samples


def _filter_relevant(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for s in samples:
        if _FILTER_PATTERN.search(s["name"]) or _FILTER_PATTERN.search(s["help"]):
            out.append(s)
    return out


def _print_table(samples: list[dict[str, Any]], title: str) -> None:
    print(f"\n=== {title} ({len(samples)} matching metrics) ===")
    if not samples:
        print("  (none)")
        return
    width_name = min(60, max(len(s["name"]) + len(s["labels"]) for s in samples))
    print(f"  {'metric':{width_name}}  {'type':12}  value")
    print(f"  {'-' * width_name}  {'-' * 12}  {'-' * 12}")
    for s in samples:
        full = (s["name"] + s["labels"])[:width_name]
        v = s["value"]
        v_str = f"{v:,.4g}" if isinstance(v, (int, float)) else str(v)
        print(f"  {full:{width_name}}  {s['type']:12}  {v_str}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=_DEFAULT_BASE_URL)
    parser.add_argument("--gap-seconds", type=float, default=5.0)
    args = parser.parse_args()

    print(f"Probing {args.base_url}/metrics …")
    try:
        raw_first = _fetch_metrics(args.base_url)
    except (urllib.error.URLError, OSError) as exc:
        print(f"ERROR: cannot reach {args.base_url}/metrics — {exc}", file=sys.stderr)
        return 2

    samples_first = _parse_prometheus(raw_first)
    relevant_first = _filter_relevant(samples_first)
    _print_table(relevant_first, "PASS 1 (idle baseline)")

    if args.gap_seconds > 0:
        print(f"\nSleeping {args.gap_seconds}s before pass 2 …")
        time.sleep(args.gap_seconds)

    raw_second = _fetch_metrics(args.base_url)
    samples_second = _parse_prometheus(raw_second)
    relevant_second = _filter_relevant(samples_second)
    _print_table(relevant_second, "PASS 2")

    # Compute deltas for any matching name+labels pair
    by_key: dict[str, dict[str, Any]] = {
        f"{s['name']}{s['labels']}": s for s in relevant_first
    }
    deltas: list[dict[str, Any]] = []
    for s in relevant_second:
        key = f"{s['name']}{s['labels']}"
        prev = by_key.get(key)
        if (
            prev is None
            or not isinstance(s["value"], (int, float))
            or not isinstance(prev["value"], (int, float))
        ):
            continue
        delta = s["value"] - prev["value"]
        if delta != 0:
            deltas.append({"metric": key, "delta": delta, "type": s["type"]})

    print(f"\n=== DELTAS (over {args.gap_seconds}s gap) ===")
    if deltas:
        for d in deltas:
            print(f"  {d['metric']:60}  Δ {d['delta']:+,.4g}  ({d['type']})")
    else:
        print("  (no movement on cache/prefix/kv counters — vLLM is idle)")

    # Dump JSON artifact
    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = _ARTIFACT_DIR / f"metrics_probe_{ts}.json"
    out_path.write_text(
        json.dumps(
            {
                "base_url": args.base_url,
                "timestamp_utc": ts,
                "pass1_relevant": relevant_first,
                "pass2_relevant": relevant_second,
                "deltas": deltas,
                "total_metrics_first_pass": len(samples_first),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nArtifact: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
