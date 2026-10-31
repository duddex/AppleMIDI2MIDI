"""Tests for the RTP-MIDI bridge: packet parsing, mDNS records and a loopback session."""

import socket
import struct
import threading
import time
import unittest

import rtpmidi_bridge as rb


def dns_name(name: str) -> bytes:
    return rb._encode_dns_name(name)


class MidiCommandTests(unittest.TestCase):
    def test_short_header_single_cc(self) -> None:
        payload = bytes([0x03, 0xB0, 7, 100])
        self.assertEqual(rb.parse_midi_commands(payload), [bytes([0xB0, 7, 100])])

    def test_running_status(self) -> None:
        payload = bytes([0x03, 0x90, 60, 100, 0x00, 62, 110])
        self.assertEqual(
            rb.parse_midi_commands(payload),
            [bytes([0x90, 60, 100]), bytes([0x90, 62, 110])],
        )

    def test_two_byte_message(self) -> None:
        payload = bytes([0x02, 0xC0, 5, 0x00, 0xC0, 6])
        self.assertEqual(
            rb.parse_midi_commands(payload),
            [bytes([0xC0, 5]), bytes([0xC0, 6])],
        )

    def test_recovery_journal_is_unsupported(self) -> None:
        self.assertIsNone(rb.parse_midi_commands(bytes([0x43, 0, 0, 0])))

    def test_full_rtp_packet(self) -> None:
        packet = bytes([0x80, 0x61, 0, 1]) + bytes(8) + bytes([0x03, 0x90, 60, 127])
        self.assertEqual(rb.parse_rtp_midi(packet), [bytes([0x90, 60, 127])])

    def test_non_rtp_packet_is_ignored(self) -> None:
        self.assertEqual(rb.parse_rtp_midi(b"\x00" * 20), [])


class DnsTests(unittest.TestCase):
    # A real reply captured from a "Faderling Debug SM-A525F" tablet. Its PTR,
    # SRV and A records use DNS compression pointers inside the RDATA, which is
    # exactly what a synthetic uncompressed fixture does not exercise.
    REAL_TABLET_REPLY = bytes.fromhex(
        "00008400000000010000000e0b5f6170706c652d6d696469045f756470056c6f63616c00000c0001"
        "00001194001b1846616465726c696e6720446562756720534d2d4135323546c00cc02e00108001"
        "00001194000100c02e0021800100000078001900000000138c10416e64726f69645f5a58314946"
        "503831c01dc02e002f8001000000780009c02e00050000800040c068002f8001000000780008c0"
        "68000440000008c068001c8001000000780010fe80000000000000b85e33fffe1ad0bcc068001c"
        "8001000000780010200300e5df019899b85e33fffe1ad0bcc068001c8001000000780010200300"
        "e5df029b84b85e33fffe1ad0bcc068001c8001000000780010200300e5df027d66b85e33fffe1a"
        "d0bcc068001c8001000000780010200300e5df035976b85e33fffe1ad0bcc068001c8001000000"
        "780010200300e5df048516b85e33fffe1ad0bcc068001c8001000000780010200300e5df0648"
        "94b85e33fffe1ad0bcc068001c8001000000780010200300e5df07b216b85e33fffe1ad0bcc068"
        "001c8001000000780010200300e5df07cb62b85e33fffe1ad0bcc06800018001000000780004c0"
        "a80278"
    )

    def test_name_round_trip(self) -> None:
        encoded = dns_name("_apple-midi._udp.local")
        labels, offset = rb._decode_dns_name(encoded, 0)
        self.assertEqual(labels, ["_apple-midi", "_udp", "local"])
        self.assertEqual(offset, len(encoded))

    def test_compression_pointer(self) -> None:
        base = dns_name("local")  # starts at offset 6 below
        # label "foo", then a pointer to offset 6 ("local")
        data = bytes([3]) + b"foo" + bytes([0xC0, 0x06]) + base
        labels, _ = rb._decode_dns_name(data, 0)
        self.assertEqual(labels, ["foo", "local"])

    def _response(self) -> bytes:
        question = dns_name("_apple-midi._udp.local") + struct.pack(">HH", 12, 1)
        ptr_target = dns_name("Faderling KFAUWI._apple-midi._udp.local")
        answer = (dns_name("_apple-midi._udp.local")
                  + struct.pack(">HHIH", 12, 1, 120, len(ptr_target)) + ptr_target)
        srv_target = dns_name("Android.local")
        srv = (dns_name("Faderling KFAUWI._apple-midi._udp.local")
               + struct.pack(">HHIH", 33, 1, 120, 6 + len(srv_target))
               + struct.pack(">HHH", 0, 0, 5004) + srv_target)
        a = (dns_name("Android.local")
             + struct.pack(">HHIH", 1, 1, 120, 4) + bytes([192, 168, 1, 50]))
        return (struct.pack(">HHHHHH", 0, 0, 1, 1, 0, 2)
                + question + answer + srv + a)

    def test_real_tablet_reply_uses_compressed_names(self) -> None:
        discovery = rb.MdnsDiscovery()
        discovery._handle_records(rb._parse_dns_packet(self.REAL_TABLET_REPLY))
        snapshot = discovery.snapshot()
        self.assertEqual(len(snapshot), 1)
        service = snapshot[0]
        self.assertEqual(service["name"], "Faderling Debug SM-A525F")
        self.assertEqual(service["port"], 5004)
        self.assertEqual(service["addresses"], ["192.168.2.120"])

    def test_local_addresses_exclude_loopback(self) -> None:
        self.assertNotIn("127.0.0.1", rb.local_ipv4_addresses())

    def test_discovery_assembles_service(self) -> None:
        discovery = rb.MdnsDiscovery()
        discovery._handle_records(rb._parse_dns_packet(self._response()))
        snapshot = discovery.snapshot()
        self.assertEqual(len(snapshot), 1)
        service = snapshot[0]
        self.assertEqual(service["name"], "Faderling KFAUWI")
        self.assertEqual(service["port"], 5004)
        self.assertEqual(service["addresses"], ["192.168.1.50"])


class SessionTests(unittest.TestCase):
    def test_session_packet_round_trip(self) -> None:
        packet = rb.session_packet(rb.CMD_INVITATION, 0xDEADBEEF, 0x12345678, "Test")
        command, version, token, ssrc, name = rb.parse_session(packet)
        self.assertEqual(command, rb.CMD_INVITATION)
        self.assertEqual(version, rb.PROTOCOL_VERSION)
        self.assertEqual(token, 0xDEADBEEF)
        self.assertEqual(ssrc, 0x12345678)
        self.assertEqual(name, "Test")

    def test_data_invitation_waits_for_control_ok(self) -> None:
        # Apple's RTP-MIDI rejects the data invitation if it arrives before the
        # control session is accepted, so the bridge must sequence them.
        class RecordingSocket:
            def __init__(self) -> None:
                self.sent = []

            def sendto(self, data, address) -> None:
                self.sent.append(address)

            def close(self) -> None:
                pass

        bridge = rb.Bridge(None, "Test")
        bridge._control = RecordingSocket()
        bridge._data = RecordingSocket()
        peer = rb.Peer("key", "Device", "127.0.0.1", 5004)
        bridge._peers["key"] = peer

        bridge._tick(time.monotonic())
        self.assertEqual(bridge._control.sent, [("127.0.0.1", 5004)])
        self.assertEqual(bridge._data.sent, [])

        peer.control_ok = True
        peer.next_invite = 0.0
        bridge._tick(time.monotonic())
        self.assertEqual(bridge._data.sent, [("127.0.0.1", 5005)])

    def test_loopback_session_forwards_midi(self) -> None:
        control_port = 5614
        data_port = control_port + 1
        control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        control.bind(("127.0.0.1", control_port))
        control.settimeout(0.2)
        data = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        data.bind(("127.0.0.1", data_port))
        data.settimeout(0.2)
        state = {"data_ok": False, "bridge_data_address": None}
        stop = threading.Event()

        def responder() -> None:
            while not stop.is_set():
                for sock, is_data in ((control, False), (data, True)):
                    try:
                        packet, address = sock.recvfrom(1024)
                    except socket.timeout:
                        continue
                    command = rb.apple_command(packet)
                    if command == rb.CMD_INVITATION:
                        _c, _v, token, _s, _n = rb.parse_session(packet)
                        sock.sendto(rb.session_packet(rb.CMD_ACCEPT, token, 0xABCD, "Fake"), address)
                        if is_data:
                            state["data_ok"] = True
                            state["bridge_data_address"] = address
                    elif command == rb.CMD_CLOCK_SYNC:
                        _s, _c, t1, _t2, _t3 = rb.parse_clock_sync(packet)
                        now = int(time.perf_counter() * 10000)
                        reply = struct.pack(">HHIB3xQQQ", 0xFFFF, rb.CMD_CLOCK_SYNC,
                                            0xABCD, 1, t1, now, 0)
                        sock.sendto(reply, address)

        class FakeOutput:
            def __init__(self) -> None:
                self.messages = []

            def send(self, message) -> None:
                self.messages.append(message)

            def close(self) -> None:
                pass

        output = FakeOutput()
        bridge = rb.Bridge(output, "Test", verbose=False)
        bridge.add_manual_peer("127.0.0.1", control_port)
        thread = threading.Thread(target=responder, daemon=True)
        thread.start()
        runner = threading.Thread(target=bridge.run, args=(None,), daemon=True)
        runner.start()
        try:
            deadline = time.time() + 3
            while time.time() < deadline and not state["data_ok"]:
                time.sleep(0.05)
            self.assertTrue(state["data_ok"], "data session was not accepted")
            packet = bytes([0x80, 0x61, 0, 0]) + bytes(8) + bytes([0x03, 0xB0, 7, 100])
            data.sendto(packet, state["bridge_data_address"])
            deadline = time.time() + 2
            while time.time() < deadline and not output.messages:
                time.sleep(0.05)
        finally:
            bridge._running = False
            runner.join(timeout=2)
            stop.set()
            thread.join(timeout=2)
            control.close()
            data.close()
        self.assertEqual(len(output.messages), 1)
        self.assertEqual(output.messages[0].type, "control_change")
        self.assertEqual(output.messages[0].control, 7)
        self.assertEqual(output.messages[0].value, 100)


if __name__ == "__main__":
    unittest.main()
