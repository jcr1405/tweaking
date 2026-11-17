"""
Presence Phase 2C - Experiment A: Production Configuration Server
nMaxInstances = 1, SDDL D:P(A;;RC;;;OW)(A;;0x12019b;;;<LogonSID>)
Bidirectional Overlapped I/O
"""

import ctypes
from ctypes import wintypes
import time
import sys

advapi32 = ctypes.windll.advapi32
kernel32 = ctypes.windll.kernel32

PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
FILE_FLAG_OVERLAPPED = 0x40000000
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
SDDL_REVISION_1 = 1
TOKEN_QUERY = 0x0008
TokenGroups = 2
SE_GROUP_LOGON_ID = 0xC0000000

class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD)]

class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]

class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", wintypes.LPVOID),
        ("InternalHigh", wintypes.LPVOID),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
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
kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES)
]
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]
kernel32.ConnectNamedPipe.restype = wintypes.BOOL
kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.ReadFile.restype = wintypes.BOOL
kernel32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
kernel32.WriteFile.restype = wintypes.BOOL
kernel32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
kernel32.GetOverlappedResult.restype = wintypes.BOOL

def get_logon_sid():
    h_proc = kernel32.GetCurrentProcess()
    h_token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(h_proc, TOKEN_QUERY, ctypes.byref(h_token)):
        raise ctypes.WinError()
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(h_token, TokenGroups, None, 0, ctypes.byref(needed))
        groups_buf = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(h_token, TokenGroups, groups_buf, needed.value, ctypes.byref(needed)):
            raise ctypes.WinError()
        group_count = ctypes.cast(groups_buf, ctypes.POINTER(wintypes.DWORD)).contents.value
        base_addr = ctypes.addressof(groups_buf) + (8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4)
        sid_array = ctypes.cast(base_addr, ctypes.POINTER(SID_AND_ATTRIBUTES * group_count)).contents
        for i in range(group_count):
            item = sid_array[i]
            if (item.Attributes & SE_GROUP_LOGON_ID) == SE_GROUP_LOGON_ID:
                p_str = wintypes.LPWSTR()
                advapi32.ConvertSidToStringSidW(item.Sid, ctypes.byref(p_str))
                sid = p_str.value
                kernel32.LocalFree(p_str)
                return sid
        raise RuntimeError("Logon SID not found")
    finally:
        kernel32.CloseHandle(h_token)

def main():
    logon_sid = get_logon_sid()
    pipe_name = r"\\.\pipe\presence-production-interop-test"
    sddl = f"D:P(A;;RC;;;OW)(A;;0x12019b;;;{logon_sid})"
    
    p_sd = wintypes.LPVOID()
    sd_size = wintypes.ULONG()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, SDDL_REVISION_1, ctypes.byref(p_sd), ctypes.byref(sd_size)):
        raise ctypes.WinError()
    
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), p_sd, False)
    
    h_pipe = kernel32.CreateNamedPipeW(
        pipe_name,
        PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
        1, # nMaxInstances = 1
        65536,
        65536,
        5000,
        ctypes.byref(sa)
    )
    if h_pipe == wintypes.HANDLE(-1).value or h_pipe == -1:
        print(f"SERVER ERROR: CreateNamedPipeW failed: {kernel32.GetLastError()}")
        sys.exit(1)
        
    print(f"SERVER: Created pipe {pipe_name} with handle {h_pipe}")
    print("SERVER: Waiting for client connection...")
    sys.stdout.flush()
    
    h_event = kernel32.CreateEventW(None, True, False, None)
    ol = OVERLAPPED(None, None, 0, 0, h_event)
    
    connected = kernel32.ConnectNamedPipe(h_pipe, ctypes.byref(ol))
    if not connected:
        err = kernel32.GetLastError()
        if err == 997: # ERROR_IO_PENDING
            kernel32.WaitForSingleObject(h_event, 5000)
        elif err == 535: # ERROR_PIPE_CONNECTED
            pass
        else:
            print(f"SERVER ERROR: ConnectNamedPipe failed: {err}")
            sys.exit(1)
            
    print("SERVER: Client connected successfully!")
    sys.stdout.flush()
    
    # Read client message
    in_buf = ctypes.create_string_buffer(4096)
    bytes_read = wintypes.DWORD()
    kernel32.ReadFile(h_pipe, in_buf, 4096, ctypes.byref(bytes_read), ctypes.byref(ol))
    kernel32.WaitForSingleObject(h_event, 5000)
    kernel32.GetOverlappedResult(h_pipe, ctypes.byref(ol), ctypes.byref(bytes_read), False)
    
    msg = in_buf.raw[:bytes_read.value].decode("utf-8", errors="replace")
    print(f"SERVER: Received {bytes_read.value} bytes from client: '{msg}'")
    sys.stdout.flush()
    
    # Send server ack
    reply = f"PRESENCE_SERVER_ACK: {msg}".encode("utf-8")
    bytes_written = wintypes.DWORD()
    kernel32.WriteFile(h_pipe, reply, len(reply), ctypes.byref(bytes_written), ctypes.byref(ol))
    kernel32.WaitForSingleObject(h_event, 5000)
    kernel32.GetOverlappedResult(h_pipe, ctypes.byref(ol), ctypes.byref(bytes_written), False)
    print(f"SERVER: Sent {bytes_written.value} bytes ACK to client.")
    sys.stdout.flush()
    
    time.sleep(0.5)
    kernel32.DisconnectNamedPipe(h_pipe)
    kernel32.CloseHandle(h_event)
    kernel32.CloseHandle(h_pipe)
    kernel32.LocalFree(p_sd)
    print("SERVER: Clean shutdown completed.")

if __name__ == "__main__":
    main()
