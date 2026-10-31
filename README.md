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

## How it works

1. **Discovery** — sends an mDNS query for `_apple-midi._udp.local` (PTR) with
   the unicast-response bit set and parses the PTR/SRV/TXT/A answers. The query
   is sent out of **every local interface** (a machine with WSL, VPN or Hyper-V
   adapters otherwise sends it out the wrong one) and the answers are decoded
   including the DNS compression pointers that real responders use inside the
   PTR/SRV RDATA. Sessions this machine advertises itself (e.g. rtpMIDI's own
   service) are skipped unless `--allow-local` is given.
2. **Session setup** — sends an AppleMIDI `IN` invitation from a control socket
   to the device's control port (`P`) and from a data socket to its data port
   (`P+1`), and handles `OK` / `NO`. The data socket is where MIDI arrives.
3. **Clock sync** — exchanges `CK` packets every 5 s so the device keeps the
   session open (AppleMIDI peers drop an idle session after ~60 s).
4. **Forwarding** — parses the RTP-MIDI command section (short header, running
   status, SysEx) and feeds every message to `mido`.
5. **Teardown** — sends `BY` for each session on exit.

## Verified against

Tested end to end against real Faderling tablets on a Wi-Fi LAN (TB305FU,
KFAUWI, Moto G Play, SM-A525F): mDNS discovery, `IN`/`OK` on both ports, `CK`
keepalive, and CC/Note forwarding. A captured real mDNS reply is part of the
unit tests (`test_real_tablet_reply_uses_compressed_names`).

## Limitations

- IPv4 only. Devices that advertise only an IPv6 (AAAA) address are ignored.
- Recovery-journal packets (`J` bit set) are not implemented. Faderling never
  sends them; a packet that carries one is logged and skipped.
- The bridge is receive-only, matching send-only devices such as Faderling.
- Only Faderling has been verified end to end. Other AppleMIDI stacks may be
  stricter: an Apple iPad Air 2 accepted the control session but not the data
  session in testing.

## Tests

```powershell
python -m unittest -v
```
