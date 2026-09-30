from __future__ import annotations

import ctypes
import ipaddress
import socket
import struct
from typing import Any


_DWORD = ctypes.c_uint32
_TCP_ESTATS_REC = 5
_TCP_TABLE_OWNER_PID_ALL = 5
_MIB_TCP_STATE_ESTABLISHED = 5
_ERROR_ACCESS_DENIED = 5
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_NOT_FOUND = 1168
_ERROR_INVALID_DATA = 13


class _MibTcpRow(ctypes.Structure):
    _fields_ = [
        ("dwState", _DWORD),
        ("dwLocalAddr", _DWORD),
        ("dwLocalPort", _DWORD),
        ("dwRemoteAddr", _DWORD),
        ("dwRemotePort", _DWORD),
    ]


class _MibTcpRowOwnerPid(ctypes.Structure):
    _fields_ = _MibTcpRow._fields_ + [("dwOwningPid", _DWORD)]


class _TcpEstatsRecRwV0(ctypes.Structure):
    _fields_ = [("EnableCollection", ctypes.c_ubyte)]


class _TcpEstatsRecRodV0(ctypes.Structure):
    _fields_ = [
        ("CurRwinSent", _DWORD),
        ("MaxRwinSent", _DWORD),
        ("MinRwinSent", _DWORD),
        ("LimRwin", _DWORD),
        ("DupAckEpisodes", _DWORD),
        ("DupAcksOut", _DWORD),
        ("CeRcvd", _DWORD),
        ("EcnSent", _DWORD),
        ("EcnNoncesRcvd", _DWORD),
        ("CurReasmQueue", _DWORD),
        ("MaxReasmQueue", _DWORD),
        ("CurAppRQueue", ctypes.c_size_t),
        ("MaxAppRQueue", ctypes.c_size_t),
        ("WinScaleSent", ctypes.c_ubyte),
    ]


def _iphlpapi() -> Any:
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise OSError("Windows IP Helper API is unavailable on this platform")
    api = loader("iphlpapi.dll", use_last_error=True)
    api.GetExtendedTcpTable.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_DWORD), ctypes.c_int, _DWORD, ctypes.c_int, _DWORD,
    ]
    api.GetExtendedTcpTable.restype = _DWORD
    api.SetPerTcpConnectionEStats.argtypes = [
        ctypes.POINTER(_MibTcpRow), ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte),
        _DWORD, _DWORD, _DWORD,
    ]
    api.SetPerTcpConnectionEStats.restype = _DWORD
    api.GetPerTcpConnectionEStats.argtypes = [
        ctypes.POINTER(_MibTcpRow), ctypes.c_int,
        ctypes.POINTER(ctypes.c_ubyte), _DWORD, _DWORD,
        ctypes.c_void_p, _DWORD, _DWORD,
        ctypes.POINTER(_TcpEstatsRecRodV0), _DWORD, _DWORD,
    ]
    api.GetPerTcpConnectionEStats.restype = _DWORD
    return api


def _remote_ipv4_addresses(host: str) -> set[str]:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if isinstance(address, ipaddress.IPv4Address):
        return {str(address)}
    if isinstance(address, ipaddress.IPv6Address):
        return set()
    return {
        result[4][0]
        for result in socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_STREAM)
    }


def _tcp_rows(api: Any) -> tuple[int, list[_MibTcpRowOwnerPid]]:
    size = _DWORD(0)
    status = int(api.GetExtendedTcpTable(
        None, ctypes.byref(size), 0, socket.AF_INET, _TCP_TABLE_OWNER_PID_ALL, 0,
    ))
    if status not in (0, _ERROR_INSUFFICIENT_BUFFER):
        return status, []
    if size.value == 0:
        return 0, []
    buffer = ctypes.create_string_buffer(size.value)
    status = int(api.GetExtendedTcpTable(
        buffer, ctypes.byref(size), 0, socket.AF_INET, _TCP_TABLE_OWNER_PID_ALL, 0,
    ))
    if status != 0:
        return status, []
    count = ctypes.cast(buffer, ctypes.POINTER(_DWORD)).contents.value
    rows_offset = ctypes.sizeof(_DWORD)
    rows = ctypes.cast(
        ctypes.addressof(buffer) + rows_offset,
        ctypes.POINTER(_MibTcpRowOwnerPid),
    )
    row_size = ctypes.sizeof(_MibTcpRowOwnerPid)
    available = max(0, (size.value - rows_offset) // row_size)
    if count > available:
        return _ERROR_INVALID_DATA, []
    return 0, [rows[index] for index in range(count)]


def _decode_ipv4(value: int) -> str:
    return socket.inet_ntoa(struct.pack("=I", value))


def _decode_port(value: int) -> int:
    return socket.ntohs(value & 0xFFFF)


def _read_receiver_stats(api: Any, row: _MibTcpRow) -> tuple[int, _TcpEstatsRecRodV0 | None]:
    rw = _TcpEstatsRecRwV0()
    rod = _TcpEstatsRecRodV0()

    def read() -> int:
        return int(api.GetPerTcpConnectionEStats(
            ctypes.byref(row), _TCP_ESTATS_REC,
            ctypes.cast(ctypes.byref(rw), ctypes.POINTER(ctypes.c_ubyte)),
            0, ctypes.sizeof(rw), None, 0, 0,
            ctypes.byref(rod), 0, ctypes.sizeof(rod),
        ))

    status = read()
    if status != 0:
        return status, None
    if rw.EnableCollection != 1:
        rw.EnableCollection = 1
        status = int(api.SetPerTcpConnectionEStats(
            ctypes.byref(row), _TCP_ESTATS_REC,
            ctypes.cast(ctypes.byref(rw), ctypes.POINTER(ctypes.c_ubyte)),
            0, ctypes.sizeof(rw), 0,
        ))
        if status != 0:
            return status, None
        status = read()
        if status != 0:
            return status, None
    if rw.EnableCollection != 1:
        return _ERROR_INVALID_DATA, None
    return 0, rod


def sample_windows_tcp_receiver_stats(
    host: str,
    port: int,
    owner_pids: set[int] | None,
) -> dict[str, Any]:
    """Read receiver-side TCP EStats for matching probe-owned IPv4 flows.

    Duplicate ACK episodes indicate missing or reordered segments on the
    remote-to-local TCP path. They are not a packet-loss percentage.
    """
    if not owner_pids:
        return {"status": "NO_PROBE_PROCESS", "flows": []}
    try:
        remote_addresses = _remote_ipv4_addresses(host)
    except (OSError, ValueError):
        return {"status": "HOST_RESOLUTION_FAILED", "flows": []}
    if not remote_addresses:
        return {"status": "IPV4_UNAVAILABLE", "flows": []}
    try:
        api = _iphlpapi()
    except (OSError, AttributeError):
        return {"status": "API_UNAVAILABLE", "flows": []}

    status, rows = _tcp_rows(api)
    if status != 0:
        return {"status": "API_ERROR", "flows": []}

    candidates = [
        row for row in rows
        if row.dwState == _MIB_TCP_STATE_ESTABLISHED
        and row.dwOwningPid in owner_pids
        and row.dwRemotePort != 0
        and _decode_port(row.dwRemotePort) == port
        and _decode_ipv4(row.dwRemoteAddr) in remote_addresses
    ]
    if not candidates:
        return {"status": "NO_MATCHING_FLOW", "flows": []}

    flows: list[dict[str, Any]] = []
    failures: list[int] = []
    for owner_row in candidates:
        row = _MibTcpRow(*(getattr(owner_row, name) for name, _field_type in _MibTcpRow._fields_))
        status, counters = _read_receiver_stats(api, row)
        if status == _ERROR_NOT_FOUND:
            continue
        if status != 0 or counters is None:
            failures.append(status)
            continue
        local_address = _decode_ipv4(owner_row.dwLocalAddr)
        remote_address = _decode_ipv4(owner_row.dwRemoteAddr)
        local_port = _decode_port(owner_row.dwLocalPort)
        remote_port = _decode_port(owner_row.dwRemotePort)
        flow_key = f"{owner_row.dwOwningPid}:{local_address}:{local_port}>{remote_address}:{remote_port}"
        flows.append({
            "key": flow_key,
            "duplicate_ack_episodes_total": int(counters.DupAckEpisodes),
            "duplicate_acks_total": int(counters.DupAcksOut),
        })

    if failures:
        status_name = "PERMISSION_DENIED" if _ERROR_ACCESS_DENIED in failures else "API_ERROR"
        return {"status": status_name, "flows": []}
    if not flows:
        return {"status": "NO_MATCHING_FLOW", "flows": []}
    return {"status": "AVAILABLE", "flows": flows}
