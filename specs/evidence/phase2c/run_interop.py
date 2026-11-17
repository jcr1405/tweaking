"""
Presence Phase 2C - Runner for Experiment A (Node.js/libuv Interoperability)
"""

import subprocess
import sys
import time
import os

def run_experiment_a():
    cwd = os.getcwd()
    print("--- EXPERIMENT A: Production Configuration (Node.js/libuv Client) ---")
    server_proc = subprocess.Popen(
        [sys.executable, os.path.join(cwd, "specs", "evidence", "phase2c", "pipe_interop_server.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Wait for server to start and print waiting message
    time.sleep(0.8)
    
    # Start Node.js client
    client_proc = subprocess.Popen(
        ["node", os.path.join(cwd, "specs", "evidence", "phase2c", "pipe_interop_client.js")],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    client_out, client_err = client_proc.communicate(timeout=10)
    server_out, server_err = server_proc.communicate(timeout=10)
    
    print("\n--- SERVER OUTPUT ---")
    print(server_out)
    if server_err:
        print("SERVER STDERR:", server_err)
        
    print("--- CLIENT OUTPUT ---")
    print(client_out)
    if client_err:
        print("CLIENT STDERR:", client_err)
        
    client_success = "CLIENT: Socket closed." in client_out and client_proc.returncode == 0
    server_success = "SERVER: Clean shutdown completed." in server_out and server_proc.returncode == 0
    
    print("\n--- RESULT ---")
    print(f"Client Status: {'PASS' if client_success else 'FAIL'} (code {client_proc.returncode})")
    print(f"Server Status: {'PASS' if server_success else 'FAIL'} (code {server_proc.returncode})")
    
    return client_success and server_success

if __name__ == "__main__":
    ok = run_experiment_a()
    sys.exit(0 if ok else 1)
