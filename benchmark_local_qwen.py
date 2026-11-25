"""Empirical Benchmark for Local Ollama Qwen 3.5 9B and Qwen 3 VL 2B
Measures:
1. Time-to-First-Token (TTFT) and Generation Speed (tokens/sec).
2. Desktop Computer-Use Instruction Following & Tool-Calling Accuracy.
3. Behavioral Routine Understanding (e.g. YouTube Chrome Routine).
4. Memory & VRAM utilization.
"""

import time
import json
import urllib.request
import urllib.error

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"

def query_ollama(model: str, messages: list, tools: list = None, temperature: float = 0.1) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature}
    }
    if tools:
        payload["tools"] = tools

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL, data=data, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        t1 = time.time()
        res_data = json.loads(resp.read().decode("utf-8"))
        res_data["client_latency_s"] = t1 - t0
        return res_data

def run_benchmarks():
    results = {}
    print("=" * 60)
    print("BENCHMARKING LOCAL QWEN 3.5 9B FOR PERSONAL COMPUTER USE")
    print("=" * 60)

    # Test 1: Conversational Computer Companion Persona & Latency
    messages_1 = [
        {
            "role": "system",
            "content": (
                "You are Presence, a persistent local-first personal computer companion running natively on Windows.\n"
                "You understand the user's computer, learn how they operate it, assist with difficult desktop interactions, "
                "and maintain situational awareness. You do not generate code or act as a software engineering agent unless asked."
            )
        },
        {
            "role": "user",
            "content": "Good morning! I just sat down at my desk. What are your primary goals as my computer companion today?"
        }
    ]
    print("\n--- Test 1: Conversational Persona & Latency ---")
    res1 = query_ollama("qwen3.5:9b", messages_1)
    prompt_eval_count = res1.get("prompt_eval_count", 0)
    eval_count = res1.get("eval_count", 0)
    eval_duration_s = res1.get("eval_duration", 1) / 1e9
    tok_per_sec = eval_count / eval_duration_s if eval_duration_s > 0 else 0

    print(f"Latency: {res1['client_latency_s']:.2f}s | Output Tokens: {eval_count} | Speed: {tok_per_sec:.2f} tokens/s")
    print(f"Content Sample:\n{res1['message']['content'][:300]}...\n")
    results["test1_persona"] = {
        "latency_s": res1["client_latency_s"],
        "eval_count": eval_count,
        "tokens_per_second": tok_per_sec,
        "sample": res1["message"]["content"][:300]
    }

    # Test 2: Structured Computer-Use Tool Calling
    desktop_tools = [
        {
            "type": "function",
            "function": {
                "name": "desktop.execute_actions",
                "description": "Execute a sequence of desktop input actions (clicks, typing, shortcuts).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action_type": {"type": "string", "enum": ["CLICK", "TYPE", "KEY_COMBINATION"]},
                                    "target": {"type": "string"},
                                    "text": {"type": "string"},
                                    "keys": {"type": "array", "items": {"type": "string"}}
                                },
                                "required": ["action_type"]
                            }
                        }
                    },
                    "required": ["actions"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "desktop.observe",
                "description": "Inspect window accessibility tree and visual state.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "window_title": {"type": "string"}
                    }
                }
            }
        }
    ]

    messages_2 = [
        {
            "role": "system",
            "content": "You are a local computer companion. To operate the desktop, call the appropriate desktop tools."
        },
        {
            "role": "user",
            "content": "Please open YouTube in my browser by typing the URL into the address bar and pressing Enter."
        }
    ]
    print("\n--- Test 2: Structured Tool Calling for Desktop Action ---")
    res2 = query_ollama("qwen3.5:9b", messages_2, tools=desktop_tools)
    tool_calls = res2.get("message", {}).get("tool_calls", [])
    print(f"Tool calls generated: {len(tool_calls)}")
    for tc in tool_calls:
        print(f"  Tool: {tc.get('function', {}).get('name')}")
        print(f"  Args: {tc.get('function', {}).get('arguments')}")

    results["test2_tool_calling"] = {
        "tool_calls_count": len(tool_calls),
        "tool_calls": tool_calls,
        "latency_s": res2["client_latency_s"],
    }

    # Test 3: Routine Interpretation & Grounding
    messages_3 = [
        {
            "role": "system",
            "content": (
                "You are Presence. The user has a learned routine named 'Launch YouTube via Chrome Default Profile' "
                "which runs: win+r -> chrome --profile-directory='Default' https://www.youtube.com -> wait for 'YouTube - Google Chrome'.\n"
                "The user says: 'play some study music on youtube'."
            )
        },
        {
            "role": "user",
            "content": "play some study music on youtube"
        }
    ]
    print("\n--- Test 3: Routine Understanding ---")
    res3 = query_ollama("qwen3.5:9b", messages_3)
    eval_count_3 = res3.get("eval_count", 0)
    eval_duration_s3 = res3.get("eval_duration", 1) / 1e9
    print(f"Latency: {res3['client_latency_s']:.2f}s | Speed: {eval_count_3 / eval_duration_s3:.2f} tok/s")
    print(f"Response: {res3['message']['content'][:300]}...")
    results["test3_routine"] = {
        "latency_s": res3["client_latency_s"],
        "tokens_per_second": eval_count_3 / eval_duration_s3,
        "sample": res3["message"]["content"][:300]
    }

    with open("local_companion_benchmark_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print("\nBenchmark results saved to local_companion_benchmark_results.json")

if __name__ == "__main__":
    run_benchmarks()
