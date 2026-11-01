#!/usr/bin/env python3
"""Pure-Python RTP-MIDI (AppleMIDI, RFC 6295) receiver and MIDI forwarder.

Discovers AppleMIDI sessions advertised on the local network via mDNS/Bonjour
(``_apple-midi._udp.local``) using only the standard library, connects to them
as an initiator (the role the rtpMIDI tool by Tobias Erichsen plays on a PC),
and forwards every received MIDI message (Control Change, Note On/Off, ...) to
a MIDI output port on this machine.

Typical use::

    python applemidi2midi.py --list                    # show local MIDI outputs
    python applemidi2midi.py --scan                    # show devices on the network
    python applemidi2midi.py --to "loopMIDI Port"      # discover, connect, forward
    python applemidi2midi.py --host 192.168.1.50       # skip discovery
    python applemidi2midi.py --host 192.168.1.50:5006  # non-default session port

The bridge connects to every discovered device at once and merges their streams
into the selected output. It keeps each AppleMIDI session alive with periodic
clock-sync packets and re-invites a device automatically when it reappears.

Only the Python standard library is used for networking and discovery; mido
(with python-rtmidi) is used for the local MIDI output.
"""

from __future__ import annotations

import argparse
import random
import socket
import struct
import sys
import threading
import time

try:
    import mido
except ImportError:  # handled with a friendly message in main()
    mido = None


# --------------------------------------------------------------------------- #
# Protocol constants
# --------------------------------------------------------------------------- #

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
APPLEMIDI_SERVICE = ("_apple-midi", "_udp", "local")

PROTOCOL_VERSION = 2
CMD_INVITATION = 0x494E  # "IN"
CMD_ACCEPT = 0x4F4B      # "OK"
CMD_REJECT = 0x4E4F      # "NO"
CMD_BYE = 0x4259         # "BY"
CMD_CLOCK_SYNC = 0x434B  # "CK"

RTP_MIDI_PAYLOAD_TYPE = 0x61

# DNS record types used by DNS-SD
DNS_TYPE_A = 1
DNS_TYPE_PTR = 12
DNS_TYPE_TXT = 16
DNS_TYPE_AAAA = 28
DNS_TYPE_SRV = 33


def log(message: str) -> None:
    """Print a timestamped status line."""
    print(f"{time.strftime('%H:%M:%S')}  {message}", flush=True)


def local_ipv4_addresses() -> list[str]:
    """Return the machine's own IPv4 addresses (excluding loopback).

    Used both to send mDNS queries on every interface and to recognise (and
    skip) an AppleMIDI session advertised by this machine itself.
    """
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))  # no packets sent; picks the default route
        addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    addresses.discard("127.0.0.1")
    return sorted(addresses)


# --------------------------------------------------------------------------- #
# DNS-SD / mDNS (standard library only)
# --------------------------------------------------------------------------- #

def _encode_dns_name(name: str) -> bytes:
    """Encode a dotted name into DNS wire format (no compression)."""
    out = bytearray()
    for label in name.rstrip(".").split("."):
        encoded = label.encode("utf-8")
        if len(encoded) > 63:
            raise ValueError(f"DNS label too long: {label!r}")
        out.append(len(encoded))
        out += encoded
    out.append(0)
    return bytes(out)


def _decode_dns_name(data: bytes, offset: int) -> tuple[list[str], int]:
    """Decode a (possibly compressed) DNS name into labels and the next offset."""
    labels: list[str] = []
    jumped = False
    next_offset = offset
    seen: set[int] = set()
    while True:
        if offset >= len(data):
            raise ValueError("truncated DNS name")
        length = data[offset]
        if length == 0:
            offset += 1
            if not jumped:
                next_offset = offset
            break
        if length & 0xC0 == 0xC0:  # compression pointer
            if offset + 1 >= len(data):
                raise ValueError("truncated DNS pointer")
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if pointer in seen:
                raise ValueError("DNS pointer loop")
            seen.add(pointer)
            if not jumped:
                next_offset = offset + 2
                jumped = True
            offset = pointer
            continue
        if length & 0xC0:
            raise ValueError("invalid DNS label length")
        offset += 1
        if offset + length > len(data):
            raise ValueError("truncated DNS label")
        labels.append(data[offset:offset + length].decode("utf-8", "replace"))
        offset += length
    return labels, next_offset


def _parse_dns_packet(data: bytes) -> list[tuple[list[str], int, int, bytes, bytes, int]]:
    """Return one tuple per record in a DNS packet.

    Each tuple is ``(labels, type, class, rdata, message, rdata_offset)``.
    ``message`` and ``rdata_offset`` are needed to resolve the compression
    pointers that RDATA names (PTR, SRV) may contain, because those pointers
    are relative to the start of the whole packet, not to the RDATA slice.
    """
    if len(data) < 12:
        return []
    _, _, qdcount, ancount, nscount, arcount = struct.unpack(">HHHHHH", data[:12])
    offset = 12
    for _ in range(qdcount):
        _, offset = _decode_dns_name(data, offset)
        offset += 4  # qtype + qclass
    records = []
    for _ in range(ancount + nscount + arcount):
        labels, offset = _decode_dns_name(data, offset)
        if offset + 10 > len(data):
            break
        rtype, rclass, _ttl, rdlength = struct.unpack(">HHIH", data[offset:offset + 10])
        offset += 10
        rdata_offset = offset
        rdata = data[offset:offset + rdlength]
        offset += rdlength
        records.append((labels, rtype, rclass, rdata, data, rdata_offset))
    return records


class MdnsDiscovery:
    """Continuous mDNS/DNS-SD discovery of ``_apple-midi._udp.local`` services.

    The query asks for a unicast response (the QU bit), so no fixed port has to
    be bound and the discovery does not fight with other mDNS services.
    """

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self._services: dict[str, dict] = {}
        self._host_addresses: dict[tuple[str, ...], set[str]] = {}
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._resolve_cache: dict[str, str | None] = {}
        self._interfaces: list[str] = []
        self._interfaces_at = 0.0

    # -- lifecycle ---------------------------------------------------------- #

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        except OSError:
            pass
        sock.bind(("", 0))
        sock.settimeout(0.5)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(target=self._run, name="mdns-discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    # -- querying ----------------------------------------------------------- #

    def _query_packet(self) -> bytes:
        header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
        question = _encode_dns_name(".".join(APPLEMIDI_SERVICE))
        question += struct.pack(">HH", DNS_TYPE_PTR, 0x8001)  # PTR, QU (unicast reply)
        return header + question

    def _run(self) -> None:
        assert self._sock is not None
        query = self._query_packet()
        last_query = 0.0
        while self._running:
            now = time.monotonic()
            if now - last_query >= 2.0:
                if now - self._interfaces_at > 15.0:
                    self._interfaces = local_ipv4_addresses()
                    self._interfaces_at = now
                self._send_query(query)
                last_query = now
            try:
                data, _addr = self._sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                records = _parse_dns_packet(data)
            except Exception as exc:  # malformed packet from the network
                if self.verbose:
                    log(f"mDNS: ignoring malformed packet ({exc})")
                continue
            self._handle_records(records)

    def _send_query(self, query: bytes) -> None:
        """Send the mDNS query out of every local interface.

        Multicast only leaves through one interface by default, which on a
        machine with WSL, VPN or Hyper-V adapters can be the wrong one.
        """
        assert self._sock is not None
        for address in self._interfaces or ["0.0.0.0"]:
            try:
                self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                      socket.inet_aton(address))
            except OSError:
                continue
            for _ in range(2):  # a single multicast packet can be dropped on Wi-Fi
                try:
                    self._sock.sendto(query, (MDNS_GROUP, MDNS_PORT))
                except OSError:
                    pass
                time.sleep(0.02)

    def _handle_records(self, records: list[tuple[list[str], int, int, bytes, bytes, int]]) -> None:
        for labels, rtype, _rclass, rdata, message, rdata_offset in records:
            low = [label.lower() for label in labels]
            if rtype == DNS_TYPE_PTR:
                try:
                    target, _ = _decode_dns_name(message, rdata_offset)
                except Exception:
                    continue
                if len(target) == 4 and [label.lower() for label in target[1:]] == list(APPLEMIDI_SERVICE):
                    with self._lock:
                        self._services.setdefault(target[0], {
                            "name": target[0],
                            "host": None,
                            "host_labels": (),
                            "port": None,
                            "txt": {},
                        })
            elif rtype == DNS_TYPE_SRV and len(labels) == 4:
                if low[1:] != list(APPLEMIDI_SERVICE) or len(rdata) < 6:
                    continue
                try:
                    target, _ = _decode_dns_name(message, rdata_offset + 6)
                except Exception:
                    continue
                _priority, _weight, port = struct.unpack(">HHH", rdata[:6])
                with self._lock:
                    service = self._services.setdefault(labels[0], {
                        "name": labels[0],
                        "host": None,
                        "host_labels": (),
                        "port": None,
                        "txt": {},
                    })
                    service["host"] = ".".join(target)
                    service["host_labels"] = tuple(label.lower() for label in target)
                    service["port"] = port
            elif rtype == DNS_TYPE_TXT and len(labels) == 4:
                if low[1:] != list(APPLEMIDI_SERVICE):
                    continue
                txt: dict[str, str] = {}
                index = 0
                while index < len(rdata):
                    length = rdata[index]
                    index += 1
                    if index + length > len(rdata):
                        break
                    entry = rdata[index:index + length].decode("utf-8", "replace")
                    index += length
                    key, _, value = entry.partition("=")
                    txt[key] = value
                with self._lock:
                    service = self._services.get(labels[0])
                    if service is not None:
                        service["txt"] = txt
            elif rtype == DNS_TYPE_A and len(rdata) == 4:
                address = socket.inet_ntoa(rdata)
                with self._lock:
                    self._host_addresses.setdefault(tuple(low), set()).add(address)
            elif rtype == DNS_TYPE_AAAA and len(rdata) == 16:
                # Recorded for completeness; the bridge only dials IPv4.
                try:
                    address = socket.inet_ntop(socket.AF_INET6, rdata)
                except OSError:
                    continue
                with self._lock:
                    self._host_addresses.setdefault(tuple(low), set()).add(address)

    # -- results ------------------------------------------------------------ #

    def snapshot(self) -> list[dict]:
        """Return the currently known services with resolved IPv4 addresses."""
        with self._lock:
            result = []
            for service in self._services.values():
                if not service.get("port"):
                    continue
                addresses = [
                    address
                    for address in self._host_addresses.get(service.get("host_labels", ()), ())
                    if ":" not in address  # IPv4 only
                ]
                addresses = [a for a in addresses if not a.startswith("169.254.")] or addresses
                if not addresses and service.get("host"):
                    resolved = self._resolve_fallback(service["host"])
                    if resolved:
                        addresses = [resolved]
                if not addresses:
                    continue
                entry = dict(service)
                entry["addresses"] = sorted(addresses)
                result.append(entry)
            return result

    def _resolve_fallback(self, host: str) -> str | None:
        """Best-effort OS resolution when the mDNS responder omitted the A record."""
        if host in self._resolve_cache:
            return self._resolve_cache[host]
        address = None
        try:
            infos = socket.getaddrinfo(host, None, socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            infos = ()
        for info in infos:
            candidate = info[4][0]
            if ":" not in candidate:
                address = candidate
                break
        self._resolve_cache[host] = address
        return address


# --------------------------------------------------------------------------- #
# AppleMIDI session packets
# --------------------------------------------------------------------------- #

def apple_command(packet: bytes) -> int | None:
    """Return the ``IN``/``OK``/``NO``/``BY``/``CK`` command, or None for data."""
    if len(packet) < 4 or packet[0] != 0xFF or packet[1] != 0xFF:
        return None
    return struct.unpack(">H", packet[2:4])[0]


def session_packet(command: int, token: int, ssrc: int, name: str | None = None) -> bytes:
    """Build an AppleMIDI session-control packet."""
    packet = struct.pack(">HHIII", 0xFFFF, command, PROTOCOL_VERSION, token, ssrc)
    if name:
        packet += name.encode("utf-8") + b"\x00"
    return packet


def parse_session(packet: bytes) -> tuple[int, int, int, int, str] | None:
    """Parse a session-control packet into ``(command, version, token, ssrc, name)``."""
    if len(packet) < 16:
        return None
    command = struct.unpack(">H", packet[2:4])[0]
    version, token, ssrc = struct.unpack(">III", packet[4:16])
    name = packet[16:].split(b"\x00", 1)[0].decode("utf-8", "replace")
    return command, version, token, ssrc, name


def clock_sync_packet(ssrc: int, timestamp1: int) -> bytes:
    """Build a count-0 clock-sync packet (``t1`` filled in, ``t2``/``t3`` zero)."""
    return struct.pack(">HHIB3xQQQ", 0xFFFF, CMD_CLOCK_SYNC, ssrc, 0, timestamp1, 0, 0)


def parse_clock_sync(packet: bytes) -> tuple[int, int, int, int, int] | None:
    """Parse a clock-sync packet into ``(ssrc, count, t1, t2, t3)``."""
    if len(packet) < 36 or apple_command(packet) != CMD_CLOCK_SYNC:
        return None
    ssrc = struct.unpack(">I", packet[4:8])[0]
    count = packet[8]
    t1, t2, t3 = struct.unpack(">QQQ", packet[12:36])
    return ssrc, count, t1, t2, t3


# --------------------------------------------------------------------------- #
# RTP-MIDI packet parsing
# --------------------------------------------------------------------------- #

def _read_vlq(data: bytes, offset: int, end: int | None = None) -> tuple[int | None, int]:
    """Read a variable-length quantity; return ``(value, next_offset)``.

    ``end`` bounds the read so a truncated value cannot run into the recovery
    journal that follows the MIDI list.
    """
    limit = len(data) if end is None else min(end, len(data))
    value = 0
    for _ in range(4):
        if offset >= limit:
            return None, offset
        byte = data[offset]
        offset += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, offset
    return value, offset


def _data_byte_count(status: int) -> int | None:
    """Number of data bytes for a MIDI status byte; None means variable (SysEx)."""
    if 0x80 <= status <= 0xEF:
        return 1 if (status & 0xF0) in (0xC0, 0xD0) else 2
    if status == 0xF0:
        return None
    if status in (0xF1, 0xF3):
        return 1
    if status == 0xF2:
        return 2
    return 0


def parse_midi_commands(payload: bytes) -> list[bytes]:
    """Extract complete MIDI messages from an RTP-MIDI command section.

    Implements RFC 6295 section 3: the ``B``/``J``/``Z``/``P`` flags, the 4-bit
    (``B=0``) or 12-bit (``B=1``) ``LEN`` of the MIDI list, the initial delta
    time controlled by ``Z``, running status and SysEx.  A recovery journal
    (``J=1``) follows the MIDI list; it is skipped rather than parsed, because
    forwarding does not need loss repair.  The MIDI list itself is still
    forwarded, so senders that use the default journal work.
    """
    if not payload:
        return []
    flags = payload[0]

    # B=0: one header octet, 4-bit LEN.  B=1: two header octets, 12-bit LEN
    # (the low nibble of the first octet holds the most significant 4 bits).
    if flags & 0x80:
        if len(payload) < 2:
            return []
        header_length = 2
        list_length = ((flags & 0x0F) << 8) | payload[1]
    else:
        header_length = 1
        list_length = flags & 0x0F

    start = header_length
    end = min(start + list_length, len(payload))
    if start >= end:
        return []

    messages: list[bytes] = []
    running: int | None = None
    index = start

    # Z=1: the MIDI list starts with "Delta Time 0" before the first command.
    # Z=0: the first command has an implicit delta of 0 and no delta octets.
    if flags & 0x20:
        delta, index = _read_vlq(payload, index, end)
        if delta is None or index >= end:
            return messages

    first = True
    while index < end:
        if not first:
            delta, index = _read_vlq(payload, index, end)
            if delta is None or index >= end:
                break  # a final delta time with no command codes void time
        first = False

        octet = payload[index]
        if octet >= 0x80:
            status = octet
            index += 1
            if status < 0xF0:
                running = status
            elif status < 0xF8:
                running = None  # system common cancels running status
            # 0xF8-0xFF are real-time and leave running status unchanged
        elif running is not None:
            status = running
        else:
            break  # a data byte without a status byte cannot be decoded

        count = _data_byte_count(status)
        if count is None:  # SysEx: data up to and including F7
            sysex_end = payload.find(b"\xF7", index, end)
            if sysex_end < 0:
                sysex_end = end - 1
            messages.append(bytes([status]) + bytes(payload[index:sysex_end + 1]))
            index = sysex_end + 1
            running = None
            continue
        if index + count > end:
            break
        messages.append(bytes([status]) + bytes(payload[index:index + count]))
        index += count

    return messages


def parse_rtp_midi(packet: bytes) -> list[bytes]:
    """Return the MIDI messages in an RTP-MIDI data packet."""
    if len(packet) < 13:
        return []
    if packet[0] >> 6 != 2:  # not RTP version 2
        return []
    if packet[1] & 0x7F != RTP_MIDI_PAYLOAD_TYPE:
        return []
    return parse_midi_commands(packet[12:])


def _bind_udp_pair() -> tuple[socket.socket, socket.socket]:
    """Bind a control/data UDP socket pair on adjacent ports (P, P+1)."""
    for _ in range(60):
        port = random.randrange(49152, 65534, 2)
        control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            control.bind(("", port))
        except OSError:
            control.close()
            continue
        data = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            data.bind(("", port + 1))
        except OSError:
            data.close()
            control.close()
            continue
        control.settimeout(0.5)
        data.settimeout(0.5)
        return control, data

    # Fall back to two unrelated ephemeral ports.
    control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    control.bind(("", 0))
    control.settimeout(0.5)
    data = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    data.bind(("", 0))
    data.settimeout(0.5)
    return control, data


# --------------------------------------------------------------------------- #
# The bridge
# --------------------------------------------------------------------------- #

class Peer:
    """One remote AppleMIDI session we talk to."""

    __slots__ = (
        "key", "name", "ip", "control_port", "data_port", "token",
        "remote_ssrc", "control_ok", "data_ok", "next_invite", "last_seen",
        "last_ck",
    )

    def __init__(self, key: str, name: str, ip: str, control_port: int) -> None:
        self.key = key
        self.name = name
        self.ip = ip
        self.control_port = control_port
        self.data_port = control_port + 1
        self.token = random.getrandbits(32)
        self.remote_ssrc: int | None = None
        self.control_ok = False
        self.data_ok = False
        self.next_invite = 0.0
        self.last_seen = time.monotonic()
        self.last_ck = 0.0


class Bridge:
    """Connects to AppleMIDI sessions and forwards their MIDI to one output."""

    INVITE_INTERVAL = 2.0   # seconds between invitations while not accepted
    CK_INTERVAL = 5.0       # clock-sync keepalive interval (peer times out at 60 s)
    STALE_AFTER = 30.0      # re-invite when nothing was heard for this long

    def __init__(self, output, session_name: str, verbose: bool = False,
                 allow_local: bool = False) -> None:
        self.output = output
        self.session_name = session_name
        self.verbose = verbose
        self.allow_local = allow_local
        self.ssrc = random.getrandbits(32)
        self._peers: dict[str, Peer] = {}
        self._lock = threading.Lock()
        self._running = False
        self._control: socket.socket | None = None
        self._data: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._local_addresses = set() if allow_local else set(local_ipv4_addresses())
        self._skipped_local: set[str] = set()

    # -- peer bookkeeping --------------------------------------------------- #

    def add_manual_peer(self, host: str, port: int) -> None:
        key = f"manual:{host}:{port}"
        with self._lock:
            if key not in self._peers:
                self._peers[key] = Peer(key, host, host, port)
                log(f"manual device {host}:{port}")

    def sync_discovery(self, discovery: MdnsDiscovery) -> None:
        for service in discovery.snapshot():
            remote = [a for a in service["addresses"] if a not in self._local_addresses]
            if not remote:  # a session this machine advertises itself
                if service["name"] not in self._skipped_local:
                    self._skipped_local.add(service["name"])
                    log(f"skipping local session {service['name']!r}")
                continue
            address = remote[0]
            port = service["port"]
            key = service["name"]
            with self._lock:
                peer = self._peers.get(key)
                if peer is None:
                    self._peers[key] = Peer(key, service["name"], address, port)
                    log(f"found {service['name']} at {address}:{port}")
                elif peer.ip != address or peer.control_port != port:
                    log(f"{service['name']} moved to {address}:{port}")
                    peer.ip = address
                    peer.control_port = port
                    peer.data_port = port + 1
                    peer.control_ok = False
                    peer.data_ok = False
                    peer.next_invite = 0.0

    # -- sockets and threads ------------------------------------------------ #

    def _open_sockets(self) -> None:
        self._control, self._data = _bind_udp_pair()
        if self.verbose:
            log(f"local ports: control {self._control.getsockname()[1]}, "
                f"data {self._data.getsockname()[1]}")

    def _start_threads(self) -> None:
        for target, name in ((self._control_loop, "control"),
                             (self._data_loop, "data")):
            thread = threading.Thread(target=target, name=f"rtpmidi-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def _control_loop(self) -> None:
        assert self._control is not None
        while self._running:
            try:
                packet, address = self._control.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            command = apple_command(packet)
            if command == CMD_ACCEPT:
                self._on_accept(packet, address, is_data=False)
            elif command == CMD_REJECT:
                self._on_reject(packet, address)
            elif command == CMD_BYE:
                self._on_bye(packet, address)

    def _data_loop(self) -> None:
        assert self._data is not None
        while self._running:
            try:
                packet, address = self._data.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            command = apple_command(packet)
            if command == CMD_ACCEPT:
                self._on_accept(packet, address, is_data=True)
            elif command == CMD_REJECT:
                self._on_reject(packet, address)
            elif command == CMD_BYE:
                self._on_bye(packet, address)
            elif command == CMD_CLOCK_SYNC:
                self._on_clock_sync(packet, address)
            else:
                self._on_rtp_midi(packet, address)

    # -- incoming packets --------------------------------------------------- #

    def _peer_for(self, address: tuple[str, int], is_data: bool) -> Peer | None:
        ip, port = address[0], address[1]
        with self._lock:
            peers = list(self._peers.values())
        for peer in peers:  # prefer an exact IP + port match
            expected = peer.data_port if is_data else peer.control_port
            if peer.ip == ip and expected == port:
                return peer
        for peer in peers:  # fall back to the port only (multi-homed devices)
            expected = peer.data_port if is_data else peer.control_port
            if expected == port:
                return peer
        return None

    def _on_accept(self, packet: bytes, address: tuple[str, int], is_data: bool) -> None:
        parsed = parse_session(packet)
        peer = self._peer_for(address, is_data)
        if parsed is None or peer is None:
            return
        _command, _version, token, ssrc, _name = parsed
        if token != peer.token:
            return
        peer.remote_ssrc = ssrc
        peer.last_seen = time.monotonic()
        if is_data:
            if not peer.data_ok:
                log(f"{peer.name}: data session accepted")
            peer.data_ok = True
        else:
            if not peer.control_ok:
                log(f"{peer.name}: control session accepted")
            peer.control_ok = True
            peer.next_invite = 0.0  # invite the data session on the next tick

    def _on_reject(self, packet: bytes, address: tuple[str, int]) -> None:
        parsed = parse_session(packet)
        if parsed is None:
            return
        _command, _version, token, _ssrc, _name = parsed
        for is_data in (False, True):
            peer = self._peer_for(address, is_data)
            if peer is not None and peer.token == token:
                if is_data:
                    peer.data_ok = False
                else:
                    peer.control_ok = False
                peer.next_invite = time.monotonic() + self.INVITE_INTERVAL
                if self.verbose:
                    log(f"{peer.name}: invitation rejected, retrying")

    def _on_bye(self, packet: bytes, address: tuple[str, int]) -> None:
        parsed = parse_session(packet)
        if parsed is None:
            return
        _command, _version, _token, ssrc, _name = parsed
        with self._lock:
            all_peers = list(self._peers.values())
        peers = [peer for peer in all_peers if peer.remote_ssrc == ssrc]
        if not peers:
            peers = [peer for peer in all_peers
                     if peer.ip == address[0]
                     and address[1] in (peer.control_port, peer.data_port)]
        for peer in peers:
            log(f"{peer.name}: session ended (BY)")
            peer.control_ok = False
            peer.data_ok = False
            peer.next_invite = time.monotonic() + self.INVITE_INTERVAL

    def _on_clock_sync(self, packet: bytes, address: tuple[str, int]) -> None:
        parsed = parse_clock_sync(packet)
        peer = self._peer_for(address, is_data=True)
        if parsed is None or peer is None:
            return
        peer.last_seen = time.monotonic()
        _ssrc, count, _t1, _t2, _t3 = parsed
        if count == 0:
            return
        if self.verbose:
            log(f"{peer.name}: clock sync")

    def _on_rtp_midi(self, packet: bytes, address: tuple[str, int]) -> None:
        messages = parse_rtp_midi(packet)
        peer = self._peer_for(address, is_data=True)
        if peer is not None:
            peer.last_seen = time.monotonic()
        for raw in messages:
            try:
                message = mido.Message.from_bytes(list(raw))
            except (ValueError, IndexError):
                if self.verbose:
                    log(f"unparsable MIDI: {raw.hex(' ')}")
                continue
            try:
                self.output.send(message)
            except Exception as exc:  # output port disappeared, etc.
                log(f"MIDI output error: {exc}")
                return
            if self.verbose:
                source = peer.name if peer else address[0]
                log(f"{source}: {message}")

    # -- outgoing ----------------------------------------------------------- #

    def _invite_control(self, peer: Peer) -> None:
        assert self._control is not None
        packet = session_packet(CMD_INVITATION, peer.token, self.ssrc, self.session_name)
        try:
            self._control.sendto(packet, (peer.ip, peer.control_port))
        except OSError as exc:
            if self.verbose:
                log(f"{peer.name}: control invite failed ({exc})")
        peer.next_invite = time.monotonic() + self.INVITE_INTERVAL

    def _invite_data(self, peer: Peer) -> None:
        """Invite the data session.

        Only ever called once the control session is accepted. Apple's RTP-MIDI
        (an iPad, Audio MIDI Setup) rejects the data invitation when it arrives
        before the control session exists, so the two must be sequenced.
        """
        assert self._data is not None
        packet = session_packet(CMD_INVITATION, peer.token, self.ssrc, self.session_name)
        try:
            self._data.sendto(packet, (peer.ip, peer.data_port))
        except OSError as exc:
            if self.verbose:
                log(f"{peer.name}: data invite failed ({exc})")
        peer.next_invite = time.monotonic() + self.INVITE_INTERVAL

    def _send_clock_sync(self, peer: Peer) -> None:
        assert self._data is not None
        timestamp = int(time.perf_counter() * 10000)
        try:
            self._data.sendto(clock_sync_packet(self.ssrc, timestamp),
                              (peer.ip, peer.data_port))
        except OSError:
            return
        peer.last_ck = time.monotonic()

    # -- main loop ---------------------------------------------------------- #

    def run(self, discovery: MdnsDiscovery | None) -> None:
        self._open_sockets()
        self._running = True
        self._start_threads()
        next_discovery = 0.0
        try:
            while self._running:
                now = time.monotonic()
                if discovery is not None and now >= next_discovery:
                    self.sync_discovery(discovery)
                    next_discovery = now + 5.0
                self._tick(now)
                time.sleep(0.2)
        except KeyboardInterrupt:
            log("stopping ...")
        finally:
            self._shutdown()

    def _tick(self, now: float) -> None:
        with self._lock:
            peers = list(self._peers.values())
        for peer in peers:
            if peer.control_ok and peer.data_ok:
                if now - peer.last_ck >= self.CK_INTERVAL:
                    self._send_clock_sync(peer)
                if now - peer.last_seen > self.STALE_AFTER:
                    log(f"{peer.name}: no reply, reconnecting")
                    peer.control_ok = False
                    peer.data_ok = False
                    peer.next_invite = now
            elif now >= peer.next_invite:
                # Sequence the two invitations: control first, data only after
                # the control session has been accepted (needed for AppleMIDI).
                if peer.control_ok:
                    self._invite_data(peer)
                else:
                    self._invite_control(peer)

    def _shutdown(self) -> None:
        self._running = False
        with self._lock:
            peers = list(self._peers.values())
        if self._control is not None:
            for peer in peers:
                if peer.control_ok or peer.data_ok:
                    try:
                        self._control.sendto(
                            session_packet(CMD_BYE, peer.token, self.ssrc),
                            (peer.ip, peer.control_port),
                        )
                    except OSError:
                        pass
        for sock in (self._control, self._data):
            if sock is not None:
                sock.close()
        for thread in self._threads:
            thread.join(timeout=1)
        try:
            self.output.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #

def list_outputs() -> list[str]:
    return list(mido.get_output_names())


def choose_output(name: str | None) -> str:
    names = list_outputs()
    if not names:
        sys.exit("No MIDI output ports found. Install a virtual MIDI port "
                 "(e.g. loopMIDI) or connect a MIDI device.")
    if name:
        matches = [candidate for candidate in names if name.lower() in candidate.lower()]
        if not matches:
            sys.exit(f"No MIDI output matches {name!r}. Known outputs:\n  " + "\n  ".join(names))
        if len(matches) > 1:
            log(f"{len(matches)} outputs match {name!r}, using {matches[0]!r}")
        return matches[0]
    if len(names) == 1:
        return names[0]
    print("Local MIDI outputs:")
    for index, candidate in enumerate(names):
        print(f"  {index}: {candidate}")
    try:
        answer = input("Forward to output number [0]: ").strip() or "0"
    except EOFError:
        answer = "0"
    try:
        return names[int(answer)]
    except (ValueError, IndexError):
        sys.exit("Invalid output number.")


def run_scan(discover_seconds: float) -> None:
    discovery = MdnsDiscovery(verbose=True)
    discovery.start()
    log(f"scanning for AppleMIDI sessions for {discover_seconds:g} s ...")
    deadline = time.monotonic() + discover_seconds
    try:
        while time.monotonic() < deadline:
            time.sleep(0.2)
        services = discovery.snapshot()
    finally:
        discovery.stop()
    if not services:
        print("No AppleMIDI sessions found. Is the app running and on the same network?")
        return
    print(f"\nFound {len(services)} AppleMIDI session(s):")
    for service in services:
        print(f"  {service['name']}")
        print(f"    host : {service.get('host') or '?'}")
        print(f"    addrs: {', '.join(service['addresses'])}")
        print(f"    port : {service['port']} (data {service['port'] + 1})")


def parse_host(value: str, default_port: int) -> tuple[str, int]:
    """Accept ``host`` or ``host:port``."""
    if value.count(":") == 1:
        host, _, port = value.partition(":")
        if port.isdigit():
            return host, int(port)
    return value, default_port


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover RTP-MIDI (AppleMIDI) devices and forward their MIDI "
                    "to a local MIDI output port.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  applemidi2midi.py --list\n"
               "  applemidi2midi.py --scan\n"
               "  applemidi2midi.py --to \"loopMIDI Port\"\n"
               "  applemidi2midi.py --host 192.168.1.50:5006 --to \"X390\"\n",
    )
    parser.add_argument("--list", action="store_true", help="list local MIDI output ports and exit")
    parser.add_argument("--scan", action="store_true", help="scan for devices, list them and exit")
    parser.add_argument("--to", metavar="NAME", help="local MIDI output (name substring)")
    parser.add_argument("--host", metavar="HOST[:PORT]", help="connect to this device instead of discovering")
    parser.add_argument("--port", type=int, default=5004, help="control port for --host (default 5004)")
    parser.add_argument("--name", default="RTP-MIDI Bridge", help="session name shown on the device")
    parser.add_argument("--discover", type=float, default=4.0, help="seconds to scan for --scan (default 4)")
    parser.add_argument("--allow-local", action="store_true",
                        help="also connect to sessions advertised by this machine")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every MIDI message and detail")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.scan:
        run_scan(args.discover)
        return 0

    if mido is None:
        print("mido is required for MIDI output:  pip install mido python-rtmidi", file=sys.stderr)
        return 2

    if args.list:
        for name in list_outputs():
            print(name)
        return 0

    output_name = choose_output(args.to)
    output = mido.open_output(output_name)
    log(f"forwarding to MIDI output: {output_name!r}")

    discovery: MdnsDiscovery | None = None
    if args.host:
        host, port = parse_host(args.host, args.port)
    else:
        host = port = None
        discovery = MdnsDiscovery(verbose=args.verbose)
        discovery.start()
        log("discovering AppleMIDI sessions ...")

    bridge = Bridge(output, args.name, verbose=args.verbose, allow_local=args.allow_local)
    try:
        if host is not None:
            bridge.add_manual_peer(host, port)
        bridge.run(discovery)
    finally:
        if discovery is not None:
            discovery.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
