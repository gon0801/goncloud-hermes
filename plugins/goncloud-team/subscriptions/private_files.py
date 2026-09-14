"""Private file creation without a readable interval before applying Windows ACLs."""

from __future__ import annotations

import os
import errno
import stat
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from pathlib import Path

_DIRECTORIES = ContextVar("subscription_directories", default={})


def directory_is_pinned(path):
    return path in _DIRECTORIES.get()


def _native_open(path, access, share, disposition, *, directory=False, security=None):
    """No-follow, handle-relative NT open; no mutable ancestor is re-resolved."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.USHORT),
            ("maximum", wintypes.USHORT),
            ("buffer", wintypes.LPWSTR),
        ]

    class Attributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.ULONG),
            ("root", wintypes.HANDLE),
            ("name", ctypes.POINTER(UnicodeString)),
            ("attributes", wintypes.ULONG),
            ("security", ctypes.c_void_p),
            ("qos", ctypes.c_void_p),
        ]

    class IoStatus(ctypes.Structure):
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    root = _DIRECTORIES.get().get(path.parent) if path.parent != path else None
    spelling = path.name if root is not None else "\\??\\" + str(path)
    buffer = ctypes.create_unicode_buffer(spelling)
    name = UnicodeString(
        len(spelling.encode("utf-16-le")),
        ctypes.sizeof(buffer),
        ctypes.cast(buffer, wintypes.LPWSTR),
    )
    attributes = Attributes(
        ctypes.sizeof(Attributes),
        msvcrt.get_osfhandle(root) if root is not None else None,
        ctypes.pointer(name),
        0x1040,
        security,
        None,
    )
    native = ctypes.WinDLL("ntdll")
    native.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(Attributes),
        ctypes.POINTER(IoStatus),
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    native.NtCreateFile.restype = ctypes.c_long
    handle = wintypes.HANDLE()
    status = native.NtCreateFile(
        ctypes.byref(handle),
        access | 0x100000,
        ctypes.byref(attributes),
        ctypes.byref(IoStatus()),
        None,
        0x80,
        share,
        disposition,
        0x00200020 | (1 if directory else 0x40),
        None,
        0,
    )
    if status < 0:
        if status in (-1073741772, -1073741766):
            raise FileNotFoundError("owned object absent")
        raise OSError(status, "safe relative object open failed")
    return handle.value


def open_private_file(
    path: Path, *, exclusive: bool = True, _directory=False
) -> int | None:
    """Return an owned descriptor: POSIX 0600, or a protected owner-only DACL."""
    if os.name != "nt":
        parent_fd = _DIRECTORIES.get().get(path.parent)
        if _directory:
            os.mkdir(
                path.name if parent_fd is not None else path, 0o700, dir_fd=parent_fd
            )
            return None
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        if exclusive:
            flags |= os.O_EXCL
        return os.open(
            path.name if parent_fd is not None else path, flags, 0o600, dir_fd=parent_fd
        )

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
        if _directory:
            handle = _native_open(
                path, 0x80000000, 3, 2, directory=True, security=descriptor
            )
            return None
        handle = _native_open(
            path,
            0xC0000000,
            7 if exclusive else 3,
            2 if exclusive else 3,
            security=descriptor,
        )
        if handle == ctypes.c_void_p(-1).value:
            handle = None
            raise OSError("private file creation failed")
        result = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDWR)
        handle = None  # CRT now owns the Windows handle.
        try:
            info = os.fstat(result)
            if getattr(info, "st_file_attributes", 0) & 0x400 or info.st_nlink != 1:
                raise OSError("private file cannot be a reparse point")
        except BaseException:
            os.close(result)
            raise
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


def create_private_directory(path: Path) -> None:
    """Create an exclusive protected transaction directory before any contents."""
    open_private_file(path, _directory=True)
    sync_directory(path.parent)


def sync_directory(path: Path) -> None:
    """Make rename/link/unlink durable on POSIX filesystems supporting fsync."""
    if os.name != "posix":
        return
    anchored = _DIRECTORIES.get().get(path)
    descriptor = (
        os.dup(anchored)
        if anchored is not None
        else os.open(
            path,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    )
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(descriptor)


def _kernel():
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel


@contextmanager
def _windows_owned(path: Path, *, directory=False, readonly=False, protect=False):
    """A real read handle with no write/delete sharing pins this exact object.

    Attribute-only opens do NOT enforce sharing on Windows. Existing writable
    handles cause a sharing violation: mutation fails closed before any move.
    """
    import ctypes
    import msvcrt

    kernel = _kernel()
    access = 0x80000000 | (0 if directory or readonly else 0x00010000)
    if protect:
        access |= 0x00040000  # WRITE_DAC, for the verified object's private backup.
    handle = _native_open(
        path, access, 3 if directory else 7 if readonly else 1, 1, directory=directory
    )
    if handle == ctypes.c_void_p(-1).value:
        code = ctypes.get_last_error()
        if code in (2, 3):
            raise FileNotFoundError("owned object absent")
        raise OSError("exclusive object ownership unavailable")
    descriptor = None
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_BINARY | os.O_RDONLY)
        handle = None
        info = os.fstat(descriptor)
        if getattr(info, "st_file_attributes", 0) & 0x400:
            raise OSError("owned object cannot be a reparse point")
        if directory != stat.S_ISDIR(info.st_mode):
            raise OSError("owned object type mismatch")
        if not directory and info.st_nlink != 1:
            raise OSError("owned object has aliases")
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if handle is not None:
            kernel.CloseHandle(handle)


@contextmanager
def stable_directories(paths):
    """Pin all directory ancestors before any effects, including Git validation.

    Windows denies rename for the entire lifetime. Reparse changes cannot
    redirect effects: every open/rename is relative to a pinned parent handle.
    POSIX callers likewise use the returned no-follow directory descriptors.
    """
    directories = {ancestor for path in paths for ancestor in (path, *path.parents)}
    descriptors = dict(_DIRECTORIES.get())
    token = _DIRECTORIES.set(descriptors)
    with ExitStack() as stack:
        stack.callback(_DIRECTORIES.reset, token)
        for directory in sorted(directories, key=lambda p: (len(p.parts), str(p))):
            if directory in descriptors:
                continue
            if os.name == "nt":
                try:
                    descriptors[directory] = stack.enter_context(
                        _windows_owned(directory, directory=True)
                    )
                except FileNotFoundError:
                    create_private_directory(directory)
                    descriptors[directory] = stack.enter_context(
                        _windows_owned(directory, directory=True)
                    )
            else:
                parent_fd = descriptors.get(directory.parent)
                if not directory.exists():
                    os.mkdir(directory.name, mode=0o700, dir_fd=parent_fd)
                    sync_directory(directory.parent)
                descriptor = os.open(
                    directory.name if parent_fd is not None else directory,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                stack.callback(os.close, descriptor)
                descriptors[directory] = descriptor
        yield descriptors


@contextmanager
def read_owned(path):
    with stable_directories([path.parent]) as directories:
        if os.name == "nt":
            with _windows_owned(path, readonly=True) as descriptor:
                yield descriptor
        else:
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directories[path.parent]
            )
            try:
                yield descriptor
            finally:
                os.close(descriptor)


def file_fingerprint(descriptor):
    info = os.fstat(descriptor)
    import hashlib

    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 65536):
        digest.update(chunk)
    after = os.fstat(descriptor)

    def identity(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    if identity(info) != identity(after):
        raise OSError("owned object changed while reading")
    return identity(after), digest.hexdigest()


def _rename_handle(
    descriptor: int, destination: Path, directory_descriptor: int
) -> None:
    """Rename the opened object, NEVER replacing a destination object."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    name = destination.name

    class RenameInfo(ctypes.Structure):
        _fields_ = [
            ("replace", wintypes.BOOLEAN),
            ("root", wintypes.HANDLE),
            ("length", wintypes.DWORD),
            ("name", wintypes.WCHAR * (len(name) + 1)),
        ]

    class IoStatus(ctypes.Structure):
        _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

    native = ctypes.WinDLL("ntdll")
    native.NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(IoStatus),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    native.NtSetInformationFile.restype = ctypes.c_long
    information = RenameInfo(
        False,
        msvcrt.get_osfhandle(directory_descriptor),
        len(name.encode("utf-16-le")),
        name,
    )
    status = native.NtSetInformationFile(
        msvcrt.get_osfhandle(descriptor),
        ctypes.byref(IoStatus()),
        ctypes.byref(information),
        ctypes.sizeof(information),
        10,
    )
    if status < 0:
        raise OSError(status, "exclusive object publication failed")


def move_owned(source: Path, destination: Path, expected) -> None:
    """Retain the exact source object; never overwrite or unlink a public name.

    On POSIX the source name may race: move it into a unique private slot FIRST,
    then inspect it. A mismatching object is preserved and exclusively restored;
    it is never overwritten/deleted. Publication uses rename's NOREPLACE flag.
    """
    with stable_directories([source.parent, destination.parent]) as directories:
        if os.name == "nt":
            with _windows_owned(source, protect=True) as descriptor:
                if file_fingerprint(descriptor) != expected:
                    raise OSError("object ownership changed")
                _protect_displaced(descriptor, directories[destination.parent])
                _rename_handle(descriptor, destination, directories[destination.parent])
        else:
            # The destination is a unique transaction-owned private name.
            os.rename(
                source.name,
                destination.name,
                src_dir_fd=directories[source.parent],
                dst_dir_fd=directories[destination.parent],
            )
            descriptor = os.open(
                destination.name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directories[destination.parent],
            )
            try:
                if file_fingerprint(descriptor) != expected:
                    _posix_rename_exclusive(destination, source, directories)
                    sync_directory(destination.parent)
                    if source.parent != destination.parent:
                        sync_directory(source.parent)
                    raise OSError("object ownership changed; displaced object retained")
            finally:
                os.close(descriptor)
        sync_directory(source.parent)
        if destination.parent != source.parent:
            sync_directory(destination.parent)


def _protect_displaced(descriptor, directory_descriptor):
    """Copy the transaction directory's protected DACL onto the owned object.

    A moved file can retain old inherited grants, and Windows bypass-traverse
    privilege means a private parent alone does not protect a known file name.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    security.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    descriptor_pointer, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    kernel = _kernel()
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    try:
        if security.GetSecurityInfo(
            msvcrt.get_osfhandle(directory_descriptor),
            1,
            4,
            None,
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor_pointer),
        ):
            raise OSError("private backup security unavailable")
        if not dacl or security.SetSecurityInfo(
            msvcrt.get_osfhandle(descriptor), 1, 0x80000004, None, None, dacl, None
        ):
            raise OSError("private backup security unavailable")
    finally:
        if descriptor_pointer:
            kernel.LocalFree(descriptor_pointer)


def _posix_rename_exclusive(source: Path, destination: Path, directories) -> None:
    """Move one link atomically without overwriting another writer's object.

    Unlike link+unlink this cannot leave a second link after interruption, and
    never introduces a check/unlink race. Unsupported native/filesystem support
    fails closed: there is deliberately no ordinary rename or hardlink fallback.
    """
    import ctypes

    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise OSError(errno.ENOTSUP, "exclusive POSIX rename unavailable") from None
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    if rename(
        directories[source.parent],
        os.fsencode(source.name),
        directories[destination.parent],
        os.fsencode(destination.name),
        1,
    ):
        raise OSError(ctypes.get_errno(), "exclusive POSIX rename failed")


def publish_exclusive(source: Path, destination: Path) -> None:
    """Publish a private candidate over an absent name, never an existing file."""
    with stable_directories([source.parent, destination.parent]) as directories:
        if os.name == "nt":
            with _windows_owned(source) as descriptor:
                _rename_handle(descriptor, destination, directories[destination.parent])
        else:
            _posix_rename_exclusive(source, destination, directories)
        sync_directory(destination.parent)
        if source.parent != destination.parent:
            sync_directory(source.parent)


@contextmanager
def certify_files(expected_files):
    """Keep original launchers immutable while retiring a proven store.

    POSIX cannot deny uncooperative writes through existing descriptors. Keep
    the store in that case; no destructive retirement is authorized.
    """
    if os.name != "nt":
        raise OSError("immutable recovery certification unavailable")
    with ExitStack() as stack:
        for path, expected in expected_files:
            descriptor = stack.enter_context(_windows_owned(path))
            if file_fingerprint(descriptor) != expected:
                raise OSError("recovery ownership changed")
        yield
