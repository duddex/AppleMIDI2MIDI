# AppleMIDI2MIDI

A small, pure-Python **RTP-MIDI (AppleMIDI) receiver** that replaces the PC-side
rtpMIDI tool by Tobias Erichsen. It discovers devices that advertise themselves
as `_apple-midi._udp.local` on the local network, connects to them as an
initiator (the same role rtpMIDI plays), and forwards everything they send —
Control Change, Note On/Off, pitch bend, aftertouch, program change — to a MIDI
output port on this machine.

Networking and mDNS/Bonjour discovery use **only the Python standard library**.
The local MIDI output uses [`mido`](https://mido.readthedocs.io/) /
`python-rtmidi`. Any device that speaks AppleMIDI should work; it is built for
and verified against **[Faderling](https://github.com/duddex/faderling)**, the
Android app that turns phones and tablets into MIDI controllers and sends over
AppleMIDI, advertising itself via Bonjour.

## Install

Requires Python 3.8 or newer.

```powershell
pip install -r requirements.txt
```

## Use

```powershell
python applemidi2midi.py --list                    # show local MIDI outputs
python applemidi2midi.py --scan                    # show AppleMIDI devices on the network
python applemidi2midi.py --to "loopMIDI Port"      # discover, connect, forward
python applemidi2midi.py --host 192.168.1.50       # skip discovery
python applemidi2midi.py --host 192.168.1.50:5006  # non-default session port
```

If `--to` is omitted and more than one output exists, the bridge prints a list
and asks which one to use. The bridge forwards to a normal MIDI output port
(a loopMIDI port, a hardware synth, ...); enabling that port as an input in your
DAW gives you the device's MIDI, with no virtual rtpMIDI port involved.

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

[RTP-MIDI](https://www.rfc-editor.org/rfc/rfc6295) (RFC 6295, June 2011, which
obsoletes RFC 4695) defines how MIDI 1.0 commands are carried inside RTP
packets. A data packet is a normal RTP header followed by a *MIDI command
section*:

```
+-----------------------------+--------------------------------+
| RTP header (12 bytes)       | MIDI command section           |
| V=2, PT=97 (0x61 in Apple-  | B J Z P LEN | commands ...     |
| MIDI), seq, timestamp, ssrc |                                |
+-----------------------------+--------------------------------+
```

RTP payload types are dynamically assigned: the RFC's SDP examples use 96, while
AppleMIDI uses **97 (`0x61`)**. The first byte of the command section is a flags
byte — `B` (long/short header), `J` (recovery journal present), `Z` and `P`
(phantom/chapter) — whose low nibble is the length of the first command. Each
following command carries a variable-length delta time and may use MIDI running
status. The format encodes everything that can appear on a MIDI 1.0 DIN cable,
including SysEx.

RFC 6295 leaves the RTP **clock rate** to session setup (typically `rtpmap` in
SDP, often an audio rate such as 44.1 kHz). AppleMIDI instead fixes the rate at
**10 kHz — one timestamp unit is 100 µs** — and the same unit is used by its
`CK` clock-sync messages.

This bridge implements what Faderling and Apple put on the wire: the short and
long command-section headers (`B`), the 4-bit and 12-bit list length (`LEN`),
the `Z` delta-time rule, running status, multi-message lists and SysEx. It does
not implement *journal repair* (RFC 6295's loss-recovery mechanism, backed by
the `RS` feedback message): a journal-bearing packet's MIDI list is forwarded
normally, and the journal bytes that follow it are skipped. Faderling never
emits a journal.

### AppleMIDI session control

RFC 6295 suggests SIP/SDP for session management, but Apple shipped a much
lighter protocol with the **MIDI Network** feature (macOS *Audio MIDI Setup →
MIDI Network Setup*). Wireshark names it **AppleMIDI**, and the Bonjour/mDNS
service type is `_apple-midi._udp`. A session is always between exactly two
participants: an *initiator* and a *listener* (some devices, e.g. iOS, are
listener-only).

A session uses **two consecutive UDP ports** — the control port `P`
(conventionally `5004`) and the data port `P+1` (`5005`) — and no MIDI may flow
before the session reaches its *open* state. Control packets begin with
`0xFF 0xFF` followed by a two-character command:

| Command | Value | Meaning |
| --- | --- | --- |
| `IN` | `0x494E` | invitation: version, random token, SSRC, session name |
| `OK` / `NO` | `0x4F4B` / `0x4E4F` | accept / reject, echoing the token |
| `BY` | `0x4259` | end the session (either side may send it) |
| `CK` | `0x434B` | clock sync: `CK0` asks, `CK1` answers, `CK2` closes the round trip |
| `RS` | `0x5253` | receiver feedback for the recovery journal |
| `RL` | `0x524C` | bitrate / receive limit |

To open a session the initiator sends `IN` to the control port and, once that is
accepted, **repeats the invitation on the data port** before the clock sync
starts. This bridge follows exactly that sequence (see the Apple note below).

### Apple-specific behaviour

Apple's CoreMIDI stack is the de facto reference for AppleMIDI and is stricter
than Faderling's:

- **Invitation ordering.** Apple rejects the data `IN` when it arrives before
  the control session exists. The bridge sends the control `IN`, waits for its
  `OK`, and only then invites the data port. Faderling tolerates the two being
  sent back-to-back; Apple does not.
- **Session identity.** The control and data connections are two halves of one
  session and carry the same token and SSRC; the bridge uses one SSRC per
  process and one token per peer on both ports.
- **Clock sync.** Apple expects the full `CK0` → `CK1` → `CK2` round trip and
  repeats it a few times a minute, mainly to detect a lost partner. The bridge
  sends `CK0` as a 5 s keepalive and consumes `CK1`; it does not send `CK2`,
  since it does no latency compensation. Faderling needs no more than this.

Faderling, the main target, has its own RFC 6295 implementation in Kotlin and is
more forgiving of lost or reordered invitations, which is why the bridge retries
invitations every couple of seconds and re-invites a device after a `BY`.

Further reading: [RFC 6295](https://www.rfc-editor.org/rfc/rfc6295), the
[Wikipedia article on RTP-MIDI](https://en.wikipedia.org/wiki/RTP-MIDI) (which
documents Apple's session protocol), and the
[rtpMIDI tool](https://www.tobias-erichsen.de/software/rtpmidi.html).

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
4. **Forwarding** — parses the RTP-MIDI command section (short/long header,
   `LEN`, the `Z` delta rule, running status, SysEx), skips the recovery journal
   if present, and feeds every message to `mido`.
5. **Teardown** — sends `BY` for each session on exit.

## Verified against

Tested end to end against Faderling running on real phones and tablets on a
Wi-Fi LAN (TB305FU, KFAUWI, Moto G Play, SM-A525F): mDNS discovery, `IN`/`OK`
on both ports, `CK` keepalive, and CC/Note forwarding. An Apple iPad Air 2 (Apple's CoreMIDI
RTP-MIDI stack) also completes the control and data handshake and clock sync. A
captured real mDNS reply is part of the unit tests
(`test_real_tablet_reply_uses_compressed_names`).

## Limitations

- IPv4 only. Devices that advertise only an IPv6 (AAAA) address are ignored.
- Recovery-journal *repair* is not implemented: a packet with the `J` bit set
  has its MIDI list forwarded, but the journal bytes are skipped. Faderling
  never sends a journal.
- The bridge is receive-only, matching send-only devices such as Faderling.
- The `CK2` leg of the clock-sync round trip and the `RS` receiver-feedback
  message are not sent.
- MIDI *forwarding* has only been exercised with Faderling; other AppleMIDI
  devices are verified for session setup and clock sync.

## Related

- **[Faderling](https://github.com/duddex/faderling)** — the Android app this
  bridge was built for: a tablet or phone whose screen becomes MIDI faders,
  buttons, a keyboard, drum pads, LFOs and XY pads, sent over AppleMIDI.
- [rtpMIDI](https://www.tobias-erichsen.de/software/rtpmidi.html) — Tobias
  Erichsen's Windows/macOS RTP-MIDI driver, which this project replaces with a
  single pure-Python script.

## Tests

```powershell
python -m unittest -v
```
