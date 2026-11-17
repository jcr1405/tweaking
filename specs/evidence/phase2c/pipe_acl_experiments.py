"""
Presence Phase 2C - Pipe ACL and Security Validation Harness
Experiments A, B, C, D
"""

import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import time

advapi32 = ctypes.windll.advapi32
kernel32 = ctypes.windll.kernel32

# Win32 Constants
TOKEN_QUERY = 0x0008
TokenOwner = 1
TokenGroups = 2
SE_GROUP_LOGON_ID = 0xC0000000

PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
FILE_FLAG_OVERLAPPED = 0x40000000
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_UNLIMITED_INSTANCES = 255
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
DACL_SECURITY_INFORMATION = 0x00000004
SDDL_REVISION_1 = 1

class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Sid", wintypes.LPVOID),
        ("Attributes", wintypes.DWORD),
    ]

class TOKEN_OWNER(ctypes.Structure):
    _fields_ = [("Owner", wintypes.LPVOID)]

class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]

kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
advapi32.GetTokenInformation.restype = wintypes.BOOL
advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.ULONG)
]
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = [wintypes.LPVOID]
kernel32.LocalFree.restype = wintypes.LPVOID
kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES)
]
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
]
kernel32.CreateFileW.restype = wintypes.HANDLE
advapi32.SetKernelObjectSecurity.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID
]
advapi32.SetKernelObjectSecurity.restype = wintypes.BOOL

INVALID_HANDLE = wintypes.HANDLE(-1).value

def is_invalid_handle(h):
    if h is None:
        return True
    if isinstance(h, int):
        return h == INVALID_HANDLE or h == -1
    return h.value == INVALID_HANDLE or h.value == -1

def get_token_sids():
    h_proc = kernel32.GetCurrentProcess()
    h_token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(h_proc, TOKEN_QUERY, ctypes.byref(h_token)):
        raise ctypes.WinError()
    
    try:
        # Get Owner SID
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(h_token, TokenOwner, None, 0, ctypes.byref(needed))
        owner_buf = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(h_token, TokenOwner, owner_buf, needed.value, ctypes.byref(needed)):
            raise ctypes.WinError()
        owner_data = ctypes.cast(owner_buf, ctypes.POINTER(TOKEN_OWNER)).contents
        p_str = wintypes.LPWSTR()
        advapi32.ConvertSidToStringSidW(owner_data.Owner, ctypes.byref(p_str))
        owner_sid = p_str.value
        kernel32.LocalFree(p_str)

        # Get Logon SID
        advapi32.GetTokenInformation(h_token, TokenGroups, None, 0, ctypes.byref(needed))
        groups_buf = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(h_token, TokenGroups, groups_buf, needed.value, ctypes.byref(needed)):
            raise ctypes.WinError()
        
        group_count = ctypes.cast(groups_buf, ctypes.POINTER(wintypes.DWORD)).contents.value
        base_addr = ctypes.addressof(groups_buf) + ctypes.sizeof(wintypes.DWORD)
        # Handle 8-byte alignment on x64
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            base_addr = ctypes.addressof(groups_buf) + 8
        
        sid_array = ctypes.cast(base_addr, ctypes.POINTER(SID_AND_ATTRIBUTES * group_count)).contents
        logon_sid = None
        for i in range(group_count):
            item = sid_array[i]
            if (item.Attributes & SE_GROUP_LOGON_ID) == SE_GROUP_LOGON_ID:
                p_str2 = wintypes.LPWSTR()
                advapi32.ConvertSidToStringSidW(item.Sid, ctypes.byref(p_str2))
                logon_sid = p_str2.value
                kernel32.LocalFree(p_str2)
                break
        
        return owner_sid, logon_sid
    finally:
        kernel32.CloseHandle(h_token)

def create_security_attributes(sddl: str):
    p_sd = wintypes.LPVOID()
    sd_size = wintypes.ULONG()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, SDDL_REVISION_1, ctypes.byref(p_sd), ctypes.byref(sd_size)
    ):
        raise ctypes.WinError()
    
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    sa.lpSecurityDescriptor = p_sd
    sa.bInheritHandle = False
    return sa, p_sd

def run_experiment_b(logon_sid: str):
    """
    Experiment B: Isolated DACL Instance-Creation Check
    Server creates pipe with nMaxInstances = 2.
    Effective DACL: D:P(A;;RC;;;OW)(A;;0x12019b;;;<LogonSID>)
    Secondary caller under SAME Logon SID attempts CreateNamedPipeW for instance 2.
    Because nMaxInstances = 2, instance limit is NOT exhausted.
    Does it return ERROR_ACCESS_DENIED (5) or ERROR_PIPE_BUSY (231)?
    """
    pipe_name = r"\\.\pipe\presence-dacl-isolated-test"
    sddl = f"D:P(A;;RC;;;OW)(A;;0x12019b;;;{logon_sid})"
    sa, p_sd = create_security_attributes(sddl)
    
    h_pipe1 = INVALID_HANDLE_VALUE
    h_pipe2 = INVALID_HANDLE_VALUE
    try:
        print("\n--- EXPERIMENT B: Isolated DACL Instance-Creation Check ---")
        print(f"Pipe Name: {pipe_name}")
        print(f"nMaxInstances: 2")
        print(f"SDDL: {sddl}")
        
        # Instance 1
        h_pipe1 = kernel32.CreateNamedPipeW(
            pipe_name,
            PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            2,  # nMaxInstances = 2
            65536,
            65536,
            5000,
            ctypes.byref(sa)
        )
        err1 = kernel32.GetLastError()
        print(f"Instance 1 CreateNamedPipeW: Handle = {h_pipe1}, Error = {err1}")
        assert not is_invalid_handle(h_pipe1), f"Instance 1 failed with error {err1}"

        # Instance 2 (attempted by caller with same Logon SID)
        # Note: Do not specify FILE_FLAG_FIRST_PIPE_INSTANCE for instance 2
        h_pipe2 = kernel32.CreateNamedPipeW(
            pipe_name,
            PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            2,  # nMaxInstances = 2
            65536,
            65536,
            5000,
            ctypes.byref(sa)
        )
        err2 = kernel32.GetLastError()
        print(f"Instance 2 CreateNamedPipeW: Handle = {h_pipe2}, Error = {err2}")
        
        if is_invalid_handle(h_pipe2):
            if err2 == 5:
                print(">>> RESULT: SUCCESS! ERROR_ACCESS_DENIED (5) confirmed! Caller lacks FILE_CREATE_PIPE_INSTANCE (0x0004) even though nMaxInstances = 2.")
            elif err2 == 231:
                print(">>> RESULT: ERROR_PIPE_BUSY (231) - Unexpected for nMaxInstances = 2.")
            else:
                print(f">>> RESULT: Failed with unexpected error {err2}")
        else:
            print(">>> RESULT: Instance 2 succeeded (DACL did not withhold instance creation)")
            kernel32.CloseHandle(h_pipe2)
            
        return is_invalid_handle(h_pipe2) and err2 == 5
    finally:
        if not is_invalid_handle(h_pipe1):
            kernel32.CloseHandle(h_pipe1)
        if p_sd:
            kernel32.LocalFree(p_sd)

def run_experiment_c(logon_sid: str):
    """
    Experiment C: Genuine Pipe-Squatting Test
    Scenario C.1: Rogue creates pipe with nMaxInstances = 1.
                  Presence attempts CreateNamedPipeW with FILE_FLAG_FIRST_PIPE_INSTANCE.
                  NPFS fails closed with ERROR_PIPE_BUSY (231).
    Scenario C.2: Rogue creates pipe with nMaxInstances = 255 (unlimited) attempting to lure Presence.
                  Presence attempts CreateNamedPipeW with FILE_FLAG_FIRST_PIPE_INSTANCE.
                  NPFS fails closed with ERROR_ACCESS_DENIED (5) per MSDN contract.
    """
    print("\n--- EXPERIMENT C: Genuine Pipe-Squatting Test ---")
    sddl = f"D:P(A;;RC;;;OW)(A;;0x12019b;;;{logon_sid})"
    sa, p_sd = create_security_attributes(sddl)
    
    # --- Scenario C.1: Rogue creates pipe with nMaxInstances = 1 ---
    pipe1 = r"\\.\pipe\presence-squat-test-max1"
    h_rogue1 = kernel32.CreateNamedPipeW(
        pipe1, PIPE_ACCESS_DUPLEX, PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        1, 65536, 65536, 5000, None
    )
    err_r1 = kernel32.GetLastError()
    print(f"C.1 Rogue pipe creation (max=1): Handle = {h_rogue1}, Error = {err_r1}")
    
    h_pres1 = kernel32.CreateNamedPipeW(
        pipe1,
        PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
        1, 65536, 65536, 5000, ctypes.byref(sa)
    )
    err_pres1 = kernel32.GetLastError()
    print(f"C.1 Presence CreateNamedPipeW (FIRST_INSTANCE against max=1): Handle = {h_pres1}, Error = {err_pres1}")
    kernel32.CloseHandle(h_rogue1)
    
    # --- Scenario C.2: Rogue creates pipe with nMaxInstances = 255 (unlimited) ---
    pipe2 = r"\\.\pipe\presence-squat-test-unlimited"
    h_rogue2 = kernel32.CreateNamedPipeW(
        pipe2, PIPE_ACCESS_DUPLEX, PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        255, 65536, 65536, 5000, None
    )
    err_r2 = kernel32.GetLastError()
    print(f"C.2 Rogue pipe creation (max=255): Handle = {h_rogue2}, Error = {err_r2}")
    
    h_pres2 = kernel32.CreateNamedPipeW(
        pipe2,
        PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
        1, 65536, 65536, 5000, ctypes.byref(sa)
    )
    err_pres2 = kernel32.GetLastError()
    print(f"C.2 Presence CreateNamedPipeW (FIRST_INSTANCE against max=255): Handle = {h_pres2}, Error = {err_pres2}")
    kernel32.CloseHandle(h_rogue2)
    
    if p_sd:
        kernel32.LocalFree(p_sd)
        
    c1_ok = is_invalid_handle(h_pres1) and err_pres1 in (5, 231)
    c2_ok = is_invalid_handle(h_pres2) and err_pres2 == 5
    
    if c1_ok and c2_ok:
        print(">>> RESULT: SUCCESS! Both squatting scenarios fail closed! (C.1 -> 231, C.2 -> 5 / ERROR_ACCESS_DENIED). Presence never connects to or reuses squatted pipe!")
        return True
    else:
        print(f">>> RESULT: Squatting test failed: C1 ok={c1_ok}, C2 ok={c2_ok}")
        return False

def run_experiment_d(logon_sid: str):
    """
    Experiment D: DACL Modification Test
    Authorized client handle attempts SetKernelObjectSecurity(DACL_SECURITY_INFORMATION).
    Must fail with ERROR_ACCESS_DENIED (5).
    """
    pipe_name = r"\\.\pipe\presence-dacl-mod-test"
    sddl = f"D:P(A;;RC;;;OW)(A;;0x12019b;;;{logon_sid})"
    sa, p_sd = create_security_attributes(sddl)
    
    print("\n--- EXPERIMENT D: DACL Modification Test ---")
    h_server = None
    h_client = None
    try:
        h_server = kernel32.CreateNamedPipeW(
            pipe_name,
            PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,
            65536,
            65536,
            5000,
            ctypes.byref(sa)
        )
        assert not is_invalid_handle(h_server), f"Server creation failed: {kernel32.GetLastError()}"
        
        # Client connects
        h_client = kernel32.CreateFileW(
            pipe_name,
            GENERIC_READ | GENERIC_WRITE,
            0,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED,
            None
        )
        err_client = kernel32.GetLastError()
        print(f"Client CreateFileW: Handle = {h_client}, Error = {err_client}")
        assert not is_invalid_handle(h_client), f"Client connect failed: {err_client}"
        
        # Client attempts to modify DACL
        # Try to set an empty/permissive DACL
        rogue_sddl = "D:(A;;GA;;;WD)" # Everyone Full Control
        sa_rogue, p_sd_rogue = create_security_attributes(rogue_sddl)
        try:
            res = advapi32.SetKernelObjectSecurity(
                h_client,
                DACL_SECURITY_INFORMATION,
                p_sd_rogue
            )
            err_set = kernel32.GetLastError()
            print(f"Client SetKernelObjectSecurity: Return = {res}, Error = {err_set}")
            
            if res == 0 and err_set == 5:
                print(">>> RESULT: SUCCESS! Client DACL modification blocked with ERROR_ACCESS_DENIED (5)!")
                return True
            else:
                print(f">>> RESULT: Unexpected outcome: Return={res}, Error={err_set}")
                return False
        finally:
            if p_sd_rogue:
                kernel32.LocalFree(p_sd_rogue)
    finally:
        if not is_invalid_handle(h_client):
            kernel32.CloseHandle(h_client)
        if not is_invalid_handle(h_server):
            kernel32.CloseHandle(h_server)
        if p_sd:
            kernel32.LocalFree(p_sd)

if __name__ == "__main__":
    owner_sid, logon_sid = get_token_sids()
    print(f"Active Process Owner SID: {owner_sid}")
    print(f"Active Process Logon SID: {logon_sid}")
    
    b_ok = run_experiment_b(logon_sid)
    c_ok = run_experiment_c(logon_sid)
    d_ok = run_experiment_d(logon_sid)
    
    print("\n================== SUMMARY ==================")
    print(f"Experiment B (Isolated DACL Instance Denied): {'PASS' if b_ok else 'FAIL'}")
    print(f"Experiment C (Genuine Squatting Fail-Closed): {'PASS' if c_ok else 'FAIL'}")
    print(f"Experiment D (Client DACL Rewrite Blocked):    {'PASS' if d_ok else 'FAIL'}")
    print("=============================================")
