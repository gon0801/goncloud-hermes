"""Private file creation without a readable interval before applying Windows ACLs."""

from __future__ import annotations

import os
from pathlib import Path


def open_private_file(path: Path, *, exclusive: bool = True) -> int:
    """Return an owned descriptor: POSIX 0600, or a protected owner-only DACL."""
    if os.name != "nt":
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        if exclusive:
            flags |= os.O_EXCL
        return os.open(path, flags, 0o600)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    security.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    security.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", ctypes.c_void_p),
            ("inherit", wintypes.BOOL),
        ]

    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SecurityAttributes),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    token = wintypes.HANDLE()
    descriptor = ctypes.c_void_p()
    sid_text = wintypes.LPWSTR()
    handle = None
    try:
        if not security.OpenProcessToken(
            kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):
            raise OSError("private file security unavailable")
        length = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not length.value:
            raise OSError("private file security unavailable")
        user = ctypes.create_string_buffer(length.value)
        if not security.GetTokenInformation(
            token, 1, user, length, ctypes.byref(length)
        ):
            raise OSError("private file security unavailable")
        # TOKEN_USER begins with SID_AND_ATTRIBUTES; its first member is PSID.
        sid = ctypes.cast(user, ctypes.POINTER(ctypes.c_void_p))[0]
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(sid_text)):
            raise OSError("private file security unavailable")
        sddl = f"O:{sid_text.value}D:P(A;;FA;;;{sid_text.value})"
        if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, ctypes.byref(descriptor), None
        ):
            raise OSError("private file security unavailable")
        attributes = SecurityAttributes(
            ctypes.sizeof(SecurityAttributes), descriptor, False
        )
        handle = kernel.CreateFileW(
            str(path),
            0xC0000000,
            0x00000007,
            ctypes.byref(attributes),
            1 if exclusive else 4,
            0x00200080,
            None,
        )
        if handle == ctypes.c_void_p(-1).value:
            handle = None
            raise OSError("private file creation failed")
        result = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDWR)
        handle = None  # CRT now owns the Windows handle.
        if getattr(os.fstat(result), "st_file_attributes", 0) & 0x400:
            os.close(result)
            raise OSError("private file cannot be a reparse point")
        return result
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
        if sid_text:
            kernel.LocalFree(sid_text)
        if descriptor:
            kernel.LocalFree(descriptor)
        if token:
            kernel.CloseHandle(token)
