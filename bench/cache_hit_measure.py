"""Measure vLLM prefix-cache benefit before/after the prompt-layout refactor.

Run this against a vLLM endpoint that has prefix caching enabled.

Usage:
  python3 -m cadillac.bench.cache_hit_measure --api-url http://<host>:8000/v1

The benchmark sends four scenarios, each as a cold request followed by 2 warm
repeats. We capture the warm-to-cold ratio for each. A lower ratio means
better cache utilization.

  Scenario A — identical prompt repeated:
    establishes the upper bound (perfect cache reuse). Should hit ~10% ratio.

  Scenario B — prompt with scratch refreshed mid-build:
    proves Change 1 (scratch moved to user message). Pre-refactor, every
    scratch refresh dropped cache hit to ~100%. Post-refactor, scratch
    refresh leaves the system prefix intact, so we should still hit ~10%.

  Scenario C — prompt with new code_map (file edit):
    proves Change 3 (code_map sits at a stable middle position). The cached
    prefix should cover everything up to code_map. Ratio should be ~50%
    of cold rather than 100%.

  Scenario D — prompt with new validation_failures:
    proves Change 3 (failures at the tail). Even when failures change, the
    entire prefix up to failures should remain cached. Ratio close to A.

Reports an aggregate score and prints what each ratio means.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request


def _send_chat(api_url: str, model: str, messages: list[dict],
                timeout: float = 60.0) -> tuple[float, int, int]:
    """Send one chat completion, return (elapsed_s, prompt_tokens, completion_tokens)."""
    body = {
        "model": model,
        "messages": messages,
        "max_tokens": 5,
        "temperature": 0,
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{api_url.rstrip('/')}/chat/completions",
        data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.URLError as e:
        raise RuntimeError(f"endpoint unreachable: {e}")
    elapsed = time.time() - t0
    parsed = json.loads(raw)
    usage = parsed.get("usage", {})
    return (elapsed,
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)))


def _detect_model(api_url: str) -> str:
    req = urllib.request.Request(f"{api_url.rstrip('/')}/models")
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    return data["data"][0]["id"]


def _build_baseline_system_prompt(target_tokens: int = 8000) -> str:
    """Produce a deterministic prompt of ~target_tokens that mirrors cadillac's
    BUILD shape: instructions + few-shot examples + lessons.

    Targets ≥6K tokens because below that, prefill cost is dominated by
    network jitter, not prefill compute — the cache-hit signal disappears
    into noise.
    """
    section = "You are a senior software engineer. Follow these rules carefully. " * 300
    quality = "## Coding Standards\n- Be precise.\n- Avoid hacks. Test things.\n" * 80
    few_shot = (
        "## Example\n```python\n"
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "```\n"
    ) * 60
    lessons = "[lesson] when situation X happens during a build, always do Y first then Z\n" * 120
    return f"{section}\n\n{quality}\n\n{few_shot}\n\n{lessons}"


def measure_scenario(name: str, api_url: str, model: str,
                       cold_messages: list[dict],
                       warm_messages: list[dict],
                       n_warm: int = 2,
                       cache_evict_seed: int = 0) -> dict:
    """Run one cold call then `n_warm` warm calls. Return summary stats.

    A cache-eviction prompt is sent FIRST so each scenario's "cold" call
    starts from a clean slate — without it, warmth from prior scenarios
    bleeds in and the cold timing collapses to the warm timing.
    """
    print(f"\n── {name} ──")
    # Cache eviction: a different-content prompt of similar size pushes
    # the prior scenario's KV blocks out of vLLM's prefix cache.
    evict_seed = f"EVICT-{cache_evict_seed}-{time.time():.6f}"
    evict_msgs = [
        {"role": "system", "content": f"{evict_seed} " * 1500},
        {"role": "user", "content": "Say OK"},
    ]
    try:
        _send_chat(api_url, model, evict_msgs)
    except Exception:
        pass  # eviction is best-effort
    time.sleep(0.3)

    cold_elapsed, prompt_toks, _ = _send_chat(api_url, model, cold_messages)
    print(f"  cold:  {cold_elapsed:.3f}s  ({prompt_toks} prompt tokens)")
    time.sleep(0.3)

    warm_times: list[float] = []
    for i in range(n_warm):
        e, pt, _ = _send_chat(api_url, model, warm_messages)
        warm_times.append(e)
        print(f"  warm {i+1}: {e:.3f}s  ({pt} prompt tokens)")
        time.sleep(0.3)

    warm_median = statistics.median(warm_times)
    ratio = warm_median / cold_elapsed if cold_elapsed > 0 else 1.0
    print(f"  → warm/cold ratio: {ratio*100:.0f}%")
    return {
        "name": name,
        "cold_elapsed": cold_elapsed,
        "warm_median": warm_median,
        "ratio": ratio,
        "prompt_tokens": prompt_toks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--api-url", required=True,
                         help="OpenAI-compatible endpoint, e.g. http://host:8000/v1")
    parser.add_argument("--model", default=None,
                         help="Model name (defaults to /v1/models[0])")
    parser.add_argument("--n-warm", type=int, default=2,
                         help="Warm calls per scenario (default: 2)")
    args = parser.parse_args(argv)

    model = args.model or _detect_model(args.api_url)
    print(f"Model: {model}")
    print(f"Endpoint: {args.api_url}")
    print(f"n_warm per scenario: {args.n_warm}")

    # Each scenario uses a unique random prefix so vLLM can't have it in cache
    # from prior runs or prior scenarios. This is the only way to get a clean
    # "warm vs cold" comparison on a long-lived endpoint that's seen our
    # generic prompt structure many times before.
    unique_token = f"BENCH-{int(time.time()*1000)}-{hash(model) & 0xffff:x}"
    base_system_a = unique_token + "-A " + _build_baseline_system_prompt()
    base_system_b = unique_token + "-B " + _build_baseline_system_prompt()
    base_system_c = unique_token + "-C " + _build_baseline_system_prompt()
    base_system_d = unique_token + "-D " + _build_baseline_system_prompt()

    # Step 0: a true-cold measurement using a never-before-seen system prompt
    print("\n── BASELINE (never-before-seen prompt, forces full prefill) ──")
    base_system_baseline = unique_token + "-BASE " + _build_baseline_system_prompt()
    base_cold_elapsed, base_pt, _ = _send_chat(args.api_url, model, [
        {"role": "system", "content": base_system_baseline},
        {"role": "user", "content": "Repeat exactly: HELLO"},
    ])
    print(f"  baseline cold: {base_cold_elapsed:.3f}s  ({base_pt} prompt tokens)")
    print(f"  → this is what a no-cache-hit prefill costs us")

    # Override base_system below to use a per-scenario unique version so
    # each scenario forces a real cold first call
    base_system = base_system_a

    # Scenario A: same prompt twice — establishes upper bound (best case)
    msgs_a = [
        {"role": "system", "content": base_system},
        {"role": "user", "content": "Repeat exactly: HELLO"},
    ]
    a = measure_scenario("A: identical prompt (best-case bound)",
                          args.api_url, model, msgs_a, msgs_a, args.n_warm,
                          cache_evict_seed=1)

    # Scenario B: scratch refresh as separate user message
    # Tests Change 1: scratch swap leaves the cached SYSTEM prefix intact.
    msgs_b_cold = [
        {"role": "system", "content": base_system_b},
        {"role": "user", "content": "## Your Scratchpad\nNote v1\n"},
        {"role": "user", "content": "Repeat exactly: HELLO"},
    ]
    msgs_b_warm = [
        {"role": "system", "content": base_system_b},  # SAME system
        {"role": "user", "content": "## Your Scratchpad\nNote v2 (different)\n"},  # only this changed
        {"role": "user", "content": "Repeat exactly: HELLO"},
    ]
    b = measure_scenario("B: scratch refreshed in user message",
                          args.api_url, model, msgs_b_cold, msgs_b_warm, args.n_warm,
                          cache_evict_seed=2)

    # Scenario C: code_map at a stable middle position (Change 3)
    base_with_codemap = base_system_c + "\n\n## CODE MAP\nfunction add(a, b) { return a + b; }\n"
    base_with_codemap_v2 = base_system_c + "\n\n## CODE MAP\nfunction sub(a, b) { return a - b; }\n"
    msgs_c_cold = [
        {"role": "system", "content": base_with_codemap},
        {"role": "user", "content": "Repeat exactly: HELLO"},
    ]
    msgs_c_warm = [
        {"role": "system", "content": base_with_codemap_v2},
        {"role": "user", "content": "Repeat exactly: HELLO"},
    ]
    c = measure_scenario("C: only code_map changed",
                          args.api_url, model, msgs_c_cold, msgs_c_warm, args.n_warm,
                          cache_evict_seed=3)

    # Scenario D: failure_text at the tail (Change 3)
    base_with_fails_v1 = base_system_d + "\n\n## VALIDATION FAILURES\nERROR_V1\n"
    base_with_fails_v2 = base_system_d + "\n\n## VALIDATION FAILURES\nERROR_V2_DIFFERENT\n"
    msgs_d_cold = [
        {"role": "system", "content": base_with_fails_v1},
        {"role": "user", "content": "Repeat exactly: HELLO"},
    ]
    msgs_d_warm = [
        {"role": "system", "content": base_with_fails_v2},
        {"role": "user", "content": "Repeat exactly: HELLO"},
    ]
    d = measure_scenario("D: only validation_failures changed (at the tail)",
                          args.api_url, model, msgs_d_cold, msgs_d_warm, args.n_warm,
                          cache_evict_seed=4)

    # Summary — compare each warm time against the baseline (true cold)
    # because the per-scenario "cold" timings are themselves cache-hit
    # affected once the base prefix is in the KV cache.
    print()
    print("─" * 60)
    print(f"SUMMARY (baseline true-cold prefill = {base_cold_elapsed:.3f}s)")
    print("─" * 60)
    for r in (a, b, c, d):
        # Use warm_median vs baseline_cold for an honest "what does my
        # cached prompt cost relative to a fresh cold one" number.
        speedup = base_cold_elapsed / r["warm_median"] if r["warm_median"] > 0 else 0
        warm_pct = r["warm_median"] / base_cold_elapsed * 100
        verdict = ""
        if warm_pct < 15:
            verdict = "✅ excellent — full prefix cache hit"
        elif warm_pct < 40:
            verdict = "✓  good — most of prefix cached"
        elif warm_pct < 70:
            verdict = "⚠  partial — some prefix re-prefilled"
        else:
            verdict = "❌ poor — re-prefilling most of prompt"
        print(f"  {r['name'][:50]:50s} warm={r['warm_median']:.3f}s  ({warm_pct:.0f}% of cold, {speedup:.1f}× speedup)  {verdict}")
    print()
    print("Reading the numbers:")
    print(f"  Cold prefill cost: {base_cold_elapsed:.3f}s (this is what we pay if no caching)")
    print("  A is the ceiling — identical prompt, full prefix hit.")
    print("  B (scratch refresh) should match A — proves Change 1.")
    print("  C (code_map changes) should be partial — its position determines how")
    print("     much of the prefix stays cached.")
    print("  D (failures at tail) should match A — proves Change 3 (failures last).")
    print()
    print("If B and D match A, the refactor is delivering its promised speedup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
