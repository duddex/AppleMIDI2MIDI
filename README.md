# python_rtpmidi

A small, pure-Python **RTP-MIDI (AppleMIDI) receiver** that replaces the PC-side
rtpMIDI tool by Tobias Erichsen. It discovers devices that advertise themselves
as `_apple-midi._udp.local` on the local network, connects to them as an
initiator (the same role rtpMIDI plays), and forwards everything they send —
Control Change, Note On/Off, pitch bend, aftertouch, program change — to a MIDI
output port on this machine.

Networking and mDNS/Bonjour discovery use **only the Python standard library**.
The local MIDI output uses [`mido`](https://mido.readthedocs.io/) /
`python-rtmidi`. Any device that speaks AppleMIDI should work; the main target
is [Faderling](https://github.com/duddex/faderling), which sends MIDI over
AppleMIDI and advertises via Bonjour.

## Install

Requires Python 3.8 or newer.

```powershell
pip install -r requirements.txt
```

## Use

```powershell
python rtpmidi_bridge.py --list                    # show local MIDI outputs
python rtpmidi_bridge.py --scan                    # show AppleMIDI devices on the network
python rtpmidi_bridge.py --to "loopMIDI Port"      # discover, connect, forward
python rtpmidi_bridge.py --host 192.168.1.50       # skip discovery
python rtpmidi_bridge.py --host 192.168.1.50:5006  # non-default session port
```

If `--to` is omitted and more than one output exists, the bridge prints a list
and asks which one to use. The bridge forwards to a normal MIDI output port
(a loopMIDI port, a hardware synth, ...); enabling that port as an input in your
DAW gives you the tablet's MIDI, with no virtual rtpMIDI port involved.

Options:

| Option | Meaning |
| --- | --- |
| `--list` | list local MIDI output ports and exit |
| `--scan` | scan for AppleMIDI sessions, list them and exit |
| `--to NAME` | local MIDI output to forward to (case-insensitive substring) |
| `--host HOST[:PORT]` | connect straight to a device, skipping mDNS |
| `--port PORT` | control port for `--host` (default `5004`) |
| `--name NAME` | session name the device shows (default `RTP-MIDI Bridge`) |
| `--discover SECONDS` | how long `--scan` listens (default `4`) |
| `--allow-local` | also connect to sessions advertised by this machine itself |
| `-v`, `--verbose` | log every MIDI message and connection detail |

The bridge connects to **all** discovered devices at once and merges their
streams into the one output. It keeps each session alive with a clock-sync
packet every 5 s (AppleMIDI peers drop an idle session after ~60 s) and
re-invites a device automatically after it reconnects.

## Background

### RTP-MIDI (RFC 6295)

[RTP-MIDI](https://www.rfc-editor.org/rfc/rfc6295) (RFC 6295, November 2011,
which obsoletes RFC 4695) defines how MIDI 1.0 is carried inside RTP packets.
A data packet is a normal RTP header (version 2, payload type 97 / `0x61`, the
value AppleMIDI uses) followed by a *MIDI command section*:

```
+--------+---------+---------+-----+
| 0x80 0x61 | seq | timestamp | ssrc |   RTP header (12 bytes)
+--------+---------+---------+-----+
| flags   | MIDI commands ...        |   command section
+---------+-------------------------+
```

The first byte is a flags byte (`B` long/short header, `J` recovery journal,
`Z`/`P` phantom/chapter, plus a 4-bit length for the first command). Each
following command has a variable-length delta time and may rely on MIDI running
status. The RTP timestamp counts in 100 µs units — the same unit AppleMIDI uses
for its clock sync — so timing survives the network without a sample-accurate
clock.

This bridge implements the bytes Faderling and Apple actually produce: the
short header with an explicit first command, running status, multi-message
packets and SysEx. The optional *recovery journal* (a redundancy mechanism for
lost packets) is the one part it skips; Faderling never emits it.

### AppleMIDI session control

RFC 6295 only defines the payload — how MIDI rides in RTP. The *invitation*
handshake lives outside it, in the protocol Apple shipped with its **MIDI
Network** feature (macOS *Audio MIDI Setup → MIDI Network Setup*). It is usually
called **AppleMIDI**, and the Bonjour/mDNS service type is `_apple-midi._udp`.
Each session uses a control port `P` (conventionally `5004`) and a data port
`P+1` (`5005`). Control packets start with `0xFFFF` and a two-character
command:

| Command | Meaning |
| --- | --- |
| `IN` | invitation: protocol version, random token, SSRC and session name |
| `OK` / `NO` | accept / reject, echoing the token |
| `BY` | end the session |
| `CK` | clock sync: count `0` asks, count `1` answers, with 64-bit 100 µs timestamps |

The initiator opens both ports and invites both, but the order matters — see
below.

### Apple-specific behaviour

Apple's CoreMIDI stack is the de facto reference implementation of AppleMIDI
and is stricter than Faderling's:

- **Invitation ordering.** Apple rejects the data invitation if it arrives
  before the control session exists. The bridge therefore sends the control
  `IN`, waits for its `OK`, and only then invites the data port. Faderling
  tolerates the two being sent back-to-back; Apple does not.
- **Shared identity.** The control and data connections belong to one session,
  so they share the same SSRC and token. The bridge uses one SSRC per process
  and one token per peer on both ports.

Faderling, the main target, has its own RFC 6295 implementation in Kotlin and
is more forgiving of lost or reordered invitations, which is why the bridge
retries invitations every couple of seconds and re-invites a device after a
`BY`.

## How it works

1. **Discovery** — sends an mDNS query for `_apple-midi._udp.local` (PTR) with
   the unicast-response bit set and parses the PTR/SRV/TXT/A answers. The query
   is sent out of **every local interface** (a machine with WSL, VPN or Hyper-V
   adapters otherwise sends it out the wrong one) and the answers are decoded
   including the DNS compression pointers that real responders use inside the
   PTR/SRV RDATA. Sessions this machine advertises itself (e.g. rtpMIDI's own
   service) are skipped unless `--allow-local` is given.
2. **Session setup** — sends an AppleMIDI `IN` invitation from a control socket
   to the device's control port (`P`), waits for the control `OK`, then invites
   the data port (`P+1`) from the data socket. The two are sequenced because
   Apple's RTP-MIDI (iPad, Audio MIDI Setup) rejects the data invitation when it
   arrives before the control session exists. The data socket is where MIDI
   arrives.
3. **Clock sync** — exchanges `CK` packets every 5 s so the device keeps the
   session open (AppleMIDI peers drop an idle session after ~60 s).
4. **Forwarding** — parses the RTP-MIDI command section (short header, running
   status, SysEx) and feeds every message to `mido`.
5. **Teardown** — sends `BY` for each session on exit.

## Verified against

Tested end to end against real Faderling tablets on a Wi-Fi LAN (TB305FU,
KFAUWI, Moto G Play, SM-A525F): mDNS discovery, `IN`/`OK` on both ports, `CK`
keepalive, and CC/Note forwarding. An Apple iPad Air 2 (Apple's CoreMIDI
RTP-MIDI stack) also completes the control and data handshake and clock sync. A
captured real mDNS reply is part of the unit tests
(`test_real_tablet_reply_uses_compressed_names`).

## Limitations

- IPv4 only. Devices that advertise only an IPv6 (AAAA) address are ignored.
- Recovery-journal packets (`J` bit set) are not implemented. Faderling never
  sends them; a packet that carries one is logged and skipped.
- The bridge is receive-only, matching send-only devices such as Faderling.
- MIDI *forwarding* has only been exercised with Faderling; other AppleMIDI
  devices are verified for session setup and clock sync.

## Tests

```powershell
python -m unittest -v
```
