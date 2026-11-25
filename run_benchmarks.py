"""
Presence Desktop Automation Performance Benchmark.
Collects high-resolution timings (median & p95 across 20 iterations) for:
1. Window enumeration
2. UIA tree extraction
3. Semantic Invoke
4. Semantic Toggle
5. Targeted window screenshot capture
6. SendInput dispatch
7. Local Qwen 3.5 9B multimodal vision inference (1-3 iterations due to LLM runtime)
"""

import os
import sys
import time
import json
import statistics
from typing import List, Dict, Any

from runtime.desktop.windows import WindowsManager
from runtime.desktop.accessibility import AccessibilityBackend
from runtime.desktop.capture import ScreenCaptureBackend
from runtime.desktop.input import DesktopInputBackend
from runtime.desktop.harness import PresenceDesktopHarness
from runtime.desktop.perception import VisionPerception
from runtime.providers.ollama import OllamaModelProvider

def percentile(data: List[float], p: float) -> float:
    if not data:
        return 0.0
    k = (len(data) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(data) - 1)
    d = k - f
    sorted_d = sorted(data)
    return sorted_d[f] + (sorted_d[c] - sorted_d[f]) * d

def run_benchmarks() -> Dict[str, Any]:
    print("Starting Desktop Performance Benchmarks...")
    results = {}

    with PresenceDesktopHarness() as harness:
        wm = WindowsManager()
        win = harness.get_window(wm)
        assert win is not None, "Failed to resolve harness window"
        acc = AccessibilityBackend()
        cap = ScreenCaptureBackend(windows_manager=wm)
        inp = DesktopInputBackend(windows_manager=wm, dry_run=False)

        # 1. Window Enumeration (20 iterations)
        enum_times = []
        for _ in range(20):
            t0 = time.perf_counter()
            _ = wm.list_windows()
            enum_times.append((time.perf_counter() - t0) * 1000.0)
        results["window_enumeration_ms"] = {
            "median": round(statistics.median(enum_times), 2),
            "p95": round(percentile(enum_times, 95), 2),
            "min": round(min(enum_times), 2),
            "max": round(max(enum_times), 2),
            "samples": len(enum_times),
        }
        print(f"Window enumeration: median={results['window_enumeration_ms']['median']}ms, p95={results['window_enumeration_ms']['p95']}ms")

        # 2. UIA Tree Inspection (20 iterations)
        tree_times = []
        for _ in range(20):
            t0 = time.perf_counter()
            obs = acc.inspect_window(win)
            tree_times.append((time.perf_counter() - t0) * 1000.0)
        results["uia_tree_inspection_ms"] = {
            "median": round(statistics.median(tree_times), 2),
            "p95": round(percentile(tree_times, 95), 2),
            "min": round(min(tree_times), 2),
            "max": round(max(tree_times), 2),
            "samples": len(tree_times),
        }
        print(f"UIA tree inspection: median={results['uia_tree_inspection_ms']['median']}ms, p95={results['uia_tree_inspection_ms']['p95']}ms")

        # Find elements for invoke and toggle
        obs = acc.inspect_window(win)
        btn = next(e for e in obs.elements if e.name == "Open Options")
        chk = next(e for e in obs.elements if e.name == "Feature Toggle")

        # 3. Semantic Invoke (20 iterations)
        inv_times = []
        for _ in range(20):
            t0 = time.perf_counter()
            _ = acc.invoke_element(btn.element_id)
            inv_times.append((time.perf_counter() - t0) * 1000.0)
        results["semantic_invoke_ms"] = {
            "median": round(statistics.median(inv_times), 2),
            "p95": round(percentile(inv_times, 95), 2),
            "min": round(min(inv_times), 2),
            "max": round(max(inv_times), 2),
            "samples": len(inv_times),
        }
        print(f"Semantic invoke: median={results['semantic_invoke_ms']['median']}ms, p95={results['semantic_invoke_ms']['p95']}ms")

        # 4. Semantic Toggle (20 iterations alternating On/Off)
        tog_times = []
        st = True
        for _ in range(20):
            t0 = time.perf_counter()
            _ = acc.set_toggle(chk.element_id, st)
            tog_times.append((time.perf_counter() - t0) * 1000.0)
            st = not st
        results["semantic_toggle_ms"] = {
            "median": round(statistics.median(tog_times), 2),
            "p95": round(percentile(tog_times, 95), 2),
            "min": round(min(tog_times), 2),
            "max": round(max(tog_times), 2),
            "samples": len(tog_times),
        }
        print(f"Semantic toggle: median={results['semantic_toggle_ms']['median']}ms, p95={results['semantic_toggle_ms']['p95']}ms")

        # 5. Targeted Window Capture (20 iterations)
        cap_times = []
        for _ in range(20):
            t0 = time.perf_counter()
            _ = cap.capture_window(win.window_id)
            cap_times.append((time.perf_counter() - t0) * 1000.0)
        results["window_capture_ms"] = {
            "median": round(statistics.median(cap_times), 2),
            "p95": round(percentile(cap_times, 95), 2),
            "min": round(min(cap_times), 2),
            "max": round(max(cap_times), 2),
            "samples": len(cap_times),
        }
        print(f"Targeted window capture: median={results['window_capture_ms']['median']}ms, p95={results['window_capture_ms']['p95']}ms")

        # 6. SendInput Dispatch Latency (20 iterations)
        send_times = []
        cx = (win.bounds.left + win.bounds.right) // 2
        cy = (win.bounds.top + win.bounds.bottom) // 2
        for _ in range(20):
            t0 = time.perf_counter()
            _ = inp.move_to(cx, cy)
            send_times.append((time.perf_counter() - t0) * 1000.0)
        results["sendinput_dispatch_ms"] = {
            "median": round(statistics.median(send_times), 2),
            "p95": round(percentile(send_times, 95), 2),
            "min": round(min(send_times), 2),
            "max": round(max(send_times), 2),
            "samples": len(send_times),
        }
        print(f"SendInput dispatch: median={results['sendinput_dispatch_ms']['median']}ms, p95={results['sendinput_dispatch_ms']['p95']}ms")

        # 7. Local Qwen 3.5 9B Multimodal Vision Inference Latency (3 iterations)
        shot = cap.capture_window(win.window_id)
        ollama = OllamaModelProvider()
        per = VisionPerception(provider=ollama)
        vis_times = []
        for i in range(2):
            print(f"Running vision benchmark iteration {i+1}/2...")
            t0 = time.perf_counter()
            _ = per.analyze_screenshot(screenshot=shot, query="custom visual button")
            vis_times.append((time.perf_counter() - t0) * 1000.0)
        results["qwen_vision_inference_ms"] = {
            "median": round(statistics.median(vis_times), 2),
            "p95": round(percentile(vis_times, 95), 2),
            "min": round(min(vis_times), 2),
            "max": round(max(vis_times), 2),
            "samples": len(vis_times),
        }
        print(f"Qwen vision inference: median={results['qwen_vision_inference_ms']['median']}ms, p95={results['qwen_vision_inference_ms']['p95']}ms")

    with open("benchmark_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("Benchmark complete! Saved to benchmark_results.json")
    return results

if __name__ == "__main__":
    run_benchmarks()
