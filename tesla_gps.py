#!/usr/bin/env python3
"""tesla_gps.py — extract GPS/telemetry from Tesla dashcam clips and write GPX.

Tesla firmware (2025.44.25+, HW3+) embeds per-frame telemetry in the H.264
bitstream as SEI NAL units (NAL type 6, payloadType 5 "user_data_unregistered").
The SEI payload is a 4-byte magic prefix (42 42 42 69) followed by a protobuf
`SeiMetadata` message.  This module decodes that protobuf by hand (wire format
only — no protobuf library needed) and converts the samples to a GPX track.

Only clips recorded while DRIVING carry SEI; stationary/parked clips have none
and are skipped gracefully.

Dependencies: Python standard library + the `ffmpeg`/`ffprobe` binaries.

CLI:
    python3 tesla_gps.py <event_folder> [--camera front] [-o out.gpx]
    python3 tesla_gps.py <event_folder> --probe

Reusable API (for later integration into tesla_combine.py):
    find_ffmpeg() / find_ffprobe()          -> str
    extract_samples(clip, ffmpeg, ffprobe)  -> list[dict]
    build_gpx(clip_paths, ffmpeg, ffprobe, out_path, csv_path=...) -> summary
    write_gpx(samples, out_path)
    write_telemetry_csv(samples, out_path)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SEI_MAGIC = b"\x42\x42\x42\x69"  # "BBBi" prefix before the protobuf payload

# SeiMetadata protobuf schema: field number -> (name, kind).
# kind: "varint" (wire type 0), "float" (wire type 5, fixed32),
#       "double" (wire type 1, fixed64).
SEI_FIELDS = {
    1:  ("version",              "varint"),
    2:  ("gear_state",           "varint"),
    3:  ("frame_seq_no",         "varint"),
    4:  ("vehicle_speed_mps",    "float"),
    5:  ("accelerator_pedal_position", "float"),
    6:  ("steering_wheel_angle", "float"),
    7:  ("blinker_on_left",      "varint"),
    8:  ("blinker_on_right",     "varint"),
    9:  ("brake_applied",        "varint"),
    10: ("autopilot_state",      "varint"),
    11: ("latitude_deg",         "double"),
    12: ("longitude_deg",        "double"),
    13: ("heading_deg",          "double"),
    14: ("linear_acceleration_mps2_x", "double"),
    15: ("linear_acceleration_mps2_y", "double"),
    16: ("linear_acceleration_mps2_z", "double"),
}

# Boolean SeiMetadata fields.  proto3 omits default (false/0) values, so an
# absent boolean is reported as False; absent numeric fields as None.
SEI_BOOL_FIELDS = ("blinker_on_left", "blinker_on_right", "brake_applied")

CAMERAS = ("front", "back", "left_pillar", "left_repeater",
           "right_pillar", "right_repeater")

# Raw dashcam clip filename: 2026-07-14_18-57-37-front.mp4.  The camera list
# is explicit so derived files in the same folder (e.g. *-front-combined.mp4,
# *-sentryview.mp4) are never picked up.
CLIP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-(\d{2})-(%s)\.mp4$"
    % "|".join(CAMERAS)
)


# --------------------------------------------------------------------------
# Tool discovery
# --------------------------------------------------------------------------

def find_ffmpeg() -> str:
    """Locate the ffmpeg binary (homebrew ffmpeg-full preferred)."""
    candidate = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
    if os.path.exists(candidate):
        return candidate
    found = shutil.which("ffmpeg")
    if not found:
        raise FileNotFoundError("ffmpeg not found (homebrew ffmpeg-full or PATH)")
    return found


def find_ffprobe(ffmpeg: str | None = None) -> str:
    """Locate ffprobe, preferring the one sitting next to ffmpeg."""
    if ffmpeg:
        sibling = os.path.join(os.path.dirname(ffmpeg), "ffprobe")
        if os.path.exists(sibling):
            return sibling
    found = shutil.which("ffprobe")
    if not found:
        raise FileNotFoundError("ffprobe not found next to ffmpeg or on PATH")
    return found


# --------------------------------------------------------------------------
# Low-level bitstream parsing
# --------------------------------------------------------------------------

def looks_tesla_encrypted(clip_path: str) -> bool:
    """True if the clip lacks an ISO-BMFF ftyp box -- the signature of Tesla's
    'Encrypt Dashcam Recordings' output, which is opaque ciphertext."""
    try:
        with open(clip_path, "rb") as f:
            head = f.read(12)
    except OSError:
        return False
    return len(head) == 12 and head[4:8] != b"ftyp"


TESLA_ENCRYPTED_HINT = (
    'clip appears to be Tesla-encrypted ("Encrypt Dashcam Recordings" is ON in '
    "the car). Decrypt clips at https://dashcam.tesla.com or in the car's "
    "Dashcam app (select clips, then the padlock button), or turn encryption "
    "off: Controls > Safety > Encrypt Dashcam Recordings. For batch decryption "
    "see https://github.com/XGxF3/tesla-dashcam-decrypt."
)


def _demux_h264(clip_path: str, ffmpeg: str) -> bytes:
    """Return the clip's raw annex-b H.264 elementary stream."""
    proc = subprocess.run(
        [ffmpeg, "-v", "error", "-i", clip_path, "-c:v", "copy", "-f", "h264", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        if looks_tesla_encrypted(clip_path):
            raise RuntimeError(f"{clip_path}: {TESLA_ENCRYPTED_HINT}")
        raise RuntimeError(
            f"ffmpeg demux failed for {clip_path}: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout


def _iter_nals(stream: bytes):
    """Yield (nal_type, payload_bytes) for each NAL in an annex-b stream.

    Splits on 00 00 01 start codes (a preceding 00 from the 4-byte form is
    harmless — it lands at the tail of the previous NAL, which we never parse
    past its declared SEI payload size anyway).
    """
    i = stream.find(b"\x00\x00\x01")
    while i != -1:
        start = i + 3  # first byte after start code = NAL header
        nxt = stream.find(b"\x00\x00\x01", start)
        end = len(stream) if nxt == -1 else nxt
        if start < end:
            nal = stream[start:end]
            yield nal[0] & 0x1F, nal
        i = nxt


def _strip_emulation_prevention(data: bytes) -> bytes:
    """Convert an EBSP to RBSP by removing emulation_prevention_three_byte.

    Per H.264 spec, a 0x03 after 00 00 is an emulation-prevention byte only
    when the byte that follows it is <= 0x03 (i.e. 00 00 03 0x with x in
    {00,01,02,03}); a naive global replace of 00 00 03 would corrupt payloads
    where 0x03 is real data followed by a byte > 0x03.
    """
    if b"\x00\x00\x03" not in data:
        return data
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        if (data[i] == 0x03 and i >= 2 and data[i - 2] == 0 and data[i - 1] == 0
                and (i + 1 == n or data[i + 1] <= 0x03)):
            i += 1  # drop the emulation-prevention byte
            continue
        out.append(data[i])
        i += 1
    return bytes(out)


def _parse_sei_payloads(nal: bytes):
    """Yield (payload_type, payload_bytes) for each SEI message in a type-6 NAL.

    `nal` includes the 1-byte NAL header.  Emulation-prevention bytes are
    removed from the RBSP first.
    """
    rbsp = _strip_emulation_prevention(nal[1:])
    pos, n = 0, len(rbsp)
    while pos < n and rbsp[pos] != 0x80:  # 0x80 = rbsp_trailing_bits
        # payloadType: run of 0xff bytes plus a final byte, all summed.
        ptype = 0
        while pos < n and rbsp[pos] == 0xFF:
            ptype += 255
            pos += 1
        if pos >= n:
            return  # truncated
        ptype += rbsp[pos]
        pos += 1
        # payloadSize: same encoding.
        psize = 0
        while pos < n and rbsp[pos] == 0xFF:
            psize += 255
            pos += 1
        if pos >= n:
            return
        psize += rbsp[pos]
        pos += 1
        if pos + psize > n:
            return  # truncated payload — bail out gracefully
        yield ptype, rbsp[pos:pos + psize]
        pos += psize


def _read_varint(buf: bytes, pos: int):
    """Decode a protobuf varint; return (value, new_pos) or (None, pos) if truncated."""
    result, shift = 0, 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            break  # malformed
    return None, pos


def decode_sei_metadata(payload: bytes) -> dict | None:
    """Decode one SEI payload (magic prefix + protobuf) into a telemetry dict.

    Returns None if the payload is not a Tesla SeiMetadata blob or is too
    damaged to yield coordinates.
    """
    if not payload.startswith(SEI_MAGIC):
        return None
    # Protobuf starts at the first 0x08 byte (tag of field 1 `version`, varint).
    start = payload.find(b"\x08", len(SEI_MAGIC))
    if start == -1:
        return None
    buf = payload[start:]
    out: dict = {}
    pos, n = 0, len(buf)
    while pos < n:
        tag, pos = _read_varint(buf, pos)
        if tag is None:
            break
        field, wtype = tag >> 3, tag & 7
        if wtype == 0:  # varint
            val, pos = _read_varint(buf, pos)
            if val is None:
                break
        elif wtype == 5:  # fixed32 -> float
            if pos + 4 > n:
                break
            val = struct.unpack_from("<f", buf, pos)[0]
            pos += 4
        elif wtype == 1:  # fixed64 -> double
            if pos + 8 > n:
                break
            val = struct.unpack_from("<d", buf, pos)[0]
            pos += 8
        elif wtype == 2:  # length-delimited — skip (not in schema, be tolerant)
            ln, pos = _read_varint(buf, pos)
            if ln is None or pos + ln > n:
                break
            val = buf[pos:pos + ln]
            pos += ln
        else:  # unknown wire type — cannot continue safely
            break
        info = SEI_FIELDS.get(field)
        if info:
            out[info[0]] = val
    if "latitude_deg" not in out or "longitude_deg" not in out:
        return None
    return out


# --------------------------------------------------------------------------
# Clip-level extraction
# --------------------------------------------------------------------------

def _parse_rate(rate: str) -> float | None:
    """Parse an ffprobe rate string ('num/den' or plain float) to fps, or None."""
    m = re.match(r"^(\d+)/(\d+)$", rate)
    if m:
        num, den = int(m.group(1)), int(m.group(2))
        return num / den if den and num else None
    try:
        val = float(rate)
        return val if val > 0 else None
    except ValueError:
        return None


def probe_fps(clip_path: str, ffprobe: str, default: float = 36.0) -> float:
    """Return the clip's frame rate via ffprobe.

    Preference order: avg_frame_rate, then nb_frames/duration, then
    r_frame_rate, then `default` (Tesla dashcam clips are ~36 fps).  A note is
    printed to stderr whenever avg_frame_rate was unusable.

    Every candidate must land in a plausible range (see PLAUSIBLE_FPS) or it is
    rejected and the next source is tried.  This matters because these clips'
    r_frame_rate is a bogus base clock (e.g. 10000/1) -- trusting it would date a
    60 s clip as ~0.2 s.  nb_frames/duration is the true measured average, so it
    outranks r_frame_rate; r_frame_rate stays only as a last resort before the
    hard default.
    """
    PLAUSIBLE_FPS = (1.0, 120.0)

    def plausible(v):
        return v is not None and PLAUSIBLE_FPS[0] <= v <= PLAUSIBLE_FPS[1]

    proc = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=avg_frame_rate,r_frame_rate,nb_frames,duration",
         "-of", "json", clip_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    stream = {}
    try:
        streams = json.loads(proc.stdout or "{}").get("streams") or []
        if streams:
            stream = streams[0]
    except json.JSONDecodeError:
        pass

    name = os.path.basename(clip_path)
    fps = _parse_rate(str(stream.get("avg_frame_rate", "")))
    if plausible(fps):
        return fps

    try:
        nb = int(stream.get("nb_frames", 0))
        dur = float(stream.get("duration", 0))
        measured = nb / dur if nb > 0 and dur > 0 else None
    except (TypeError, ValueError):
        measured = None
    if plausible(measured):
        print(f"warning: {name}: avg_frame_rate unusable, "
              f"using nb_frames/duration={measured:.3f}", file=sys.stderr)
        return measured

    fps = _parse_rate(str(stream.get("r_frame_rate", "")))
    if plausible(fps):
        print(f"warning: {name}: avg_frame_rate and nb_frames/duration unusable, "
              f"using r_frame_rate={fps:.3f}", file=sys.stderr)
        return fps

    print(f"warning: {name}: could not determine a plausible fps, "
          f"using default {default}", file=sys.stderr)
    return default


def parse_clip_time(clip_path: str) -> datetime | None:
    """Parse the wall-clock start time from a raw clip filename, or None."""
    m = CLIP_RE.match(os.path.basename(clip_path))
    if not m:
        return None
    date, hh, mm, ss = m.group(1), m.group(2), m.group(3), m.group(4)
    return datetime.strptime(f"{date} {hh}:{mm}:{ss}", "%Y-%m-%d %H:%M:%S")


def _extract_raw(clip_path: str, ffmpeg: str) -> list[tuple[int, dict]]:
    """Extract (video_frame_index, telemetry_dict) pairs from one clip.

    The frame index matters: some clips carry SEI on only part of their frames
    (the vehicle started/stopped driving mid-clip), so timestamps must be
    anchored to the video frame each SEI belongs to, not the SEI ordinal.

    In these streams each picture is a single slice NAL (type 1/5) with
    first_mb_in_slice == 0; an SEI NAL precedes the slice of its access unit.
    We buffer decoded SEIs and bind them to the next picture.  If a frame
    carries more than one Tesla SEI (observed occasionally), the last one wins
    so there is exactly one sample per frame.
    """
    stream = _demux_h264(clip_path, ffmpeg)
    out: list[tuple[int, dict]] = []
    frame_idx = 0        # index of the next picture to appear
    pending: dict | None = None
    for nal_type, nal in _iter_nals(stream):
        if nal_type == 6:
            for ptype, payload in _parse_sei_payloads(nal):
                if ptype != 5:  # user_data_unregistered only
                    continue
                meta = decode_sei_metadata(payload)
                if meta is not None:
                    pending = meta
        elif nal_type in (1, 5):
            # first_mb_in_slice is the first ue(v) of the slice header;
            # value 0 (a new picture) is encoded as a leading '1' bit.
            if len(nal) > 1 and (nal[1] & 0x80):
                if pending is not None:
                    out.append((frame_idx, pending))
                    pending = None
                frame_idx += 1
    if pending is not None:  # SEI at EOF with no following slice — keep it
        out.append((frame_idx, pending))
    return out


def extract_samples(clip_path: str, ffmpeg: str | None = None,
                    ffprobe: str | None = None) -> list[dict]:
    """Extract all telemetry samples from one clip.

    Returns a list of dicts (one per SEI-bearing video frame).  Convenience
    keys: lat, lon, heading, speed_mps, frame_seq, time (datetime).  Plus the
    complete SeiMetadata telemetry under its canonical field names (version,
    gear_state, accelerator_pedal_position, steering_wheel_angle,
    blinker_on_left/right, brake_applied, autopilot_state,
    linear_acceleration_mps2_x/y/z, ...).  proto3 omits default values, so
    absent booleans are normalized to False and absent numerics to None.

    Empty list if the clip carries no Tesla SEI (e.g. recorded while parked).

    `time` is clip_start (from the filename) + frame_index / fps, so spacing
    stays correct even when SEI starts partway into a clip.  If the filename
    has no parseable timestamp, `time` is None.
    """
    ffmpeg = ffmpeg or find_ffmpeg()
    ffprobe = ffprobe or find_ffprobe(ffmpeg)

    raw = _extract_raw(clip_path, ffmpeg)
    if not raw:
        return []

    start = parse_clip_time(clip_path)
    fps = probe_fps(clip_path, ffprobe)
    source_clip = os.path.basename(clip_path)
    samples = []
    for frame_idx, meta in raw:
        t = start + timedelta(seconds=frame_idx / fps) if start else None
        sample = {name: meta.get(name) for name, _ in SEI_FIELDS.values()}
        for name in SEI_BOOL_FIELDS:
            sample[name] = bool(meta.get(name, 0))
        sample.update({
            # Convenience aliases used by the GPX writer and existing callers.
            "lat": meta["latitude_deg"],
            "lon": meta["longitude_deg"],
            "heading": meta.get("heading_deg"),
            "speed_mps": meta.get("vehicle_speed_mps"),
            "frame_seq": meta.get("frame_seq_no"),
            "time": t,
            # Provenance: 0-based video-frame ordinal within source_clip, so a
            # downstream pipeline can map each sample to an exact frame PTS.
            "frame_index": frame_idx,
            "source_clip": source_clip,
        })
        samples.append(sample)
    return samples


def count_sei(clip_path: str, ffmpeg: str | None = None) -> int:
    """Count SEI-bearing video frames in a clip (for --probe)."""
    ffmpeg = ffmpeg or find_ffmpeg()
    return len(_extract_raw(clip_path, ffmpeg))


# --------------------------------------------------------------------------
# GPX generation
# --------------------------------------------------------------------------

def parse_utc_offset(spec: str) -> timezone:
    """Parse a '+HH:MM' / '-HH:MM' UTC offset string into a timezone."""
    m = re.match(r"^([+-])(\d{2}):(\d{2})$", spec)
    if not m:
        raise ValueError(f"invalid UTC offset {spec!r} (expected e.g. -07:00)")
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(-delta if m.group(1) == "-" else delta)


def _iso_time(t: datetime, tz: timezone | None = None) -> str:
    """Format a datetime as ISO-8601 with millisecond precision.

    Clip filename times are local wall-clock with no zone information, so by
    default we emit naive local timestamps (no 'Z', no offset) — relative
    spacing, which is what the downstream animator uses, is exact either way.
    If `tz` is given (from --utc-offset) the wall-clock is qualified with that
    offset, producing an unambiguous absolute timestamp.
    """
    base = t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}"
    if tz is None:
        return base
    total = int(tz.utcoffset(None).total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{base}{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def write_gpx(samples: list[dict], out_path: str,
              track_name: str = "Tesla dashcam route",
              tz: timezone | None = None) -> None:
    """Write samples (dicts with lat/lon/time and optional speed/heading) as a
    single-track GPX 1.1 file.  `tz` qualifies timestamps with a UTC offset;
    without it they are naive local wall-clock times."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<gpx version="1.1" creator="tesla_gps.py"',
        '     xmlns="http://www.topografix.com/GPX/1/1"',
        '     xmlns:tesla="urn:tesla-dashcam-telemetry">',
        f"  <trk><name>{escape(track_name)}</name><trkseg>",
    ]
    for s in samples:
        lines.append(f'    <trkpt lat="{s["lat"]:.7f}" lon="{s["lon"]:.7f}">')
        if s.get("time") is not None:
            lines.append(f"      <time>{_iso_time(s['time'], tz)}</time>")
        ext = []
        if s.get("speed_mps") is not None:
            ext.append(f"<tesla:speed_mps>{s['speed_mps']:.3f}</tesla:speed_mps>")
        if s.get("heading") is not None:
            ext.append(f"<tesla:course_deg>{s['heading']:.2f}</tesla:course_deg>")
        if s.get("frame_seq") is not None:
            ext.append(f"<tesla:frame_seq>{s['frame_seq']}</tesla:frame_seq>")
        if s.get("frame_index") is not None:
            ext.append(f"<tesla:frame_index>{s['frame_index']}</tesla:frame_index>")
        if s.get("source_clip"):
            ext.append("<tesla:source_clip>"
                       f"{escape(s['source_clip'])}</tesla:source_clip>")
        if ext:
            lines.append("      <extensions>" + "".join(ext) + "</extensions>")
        lines.append("    </trkpt>")
    lines += ["  </trkseg></trk>", "</gpx>", ""]
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# Column order for the telemetry CSV sidecar (consumed later by gauge overlays).
TELEMETRY_COLUMNS = [
    "time", "source_clip", "frame_index", "frame_seq_no",
    "latitude_deg", "longitude_deg", "heading_deg",
    "vehicle_speed_mps", "gear_state", "accelerator_pedal_position",
    "steering_wheel_angle", "blinker_on_left", "blinker_on_right",
    "brake_applied", "autopilot_state", "linear_acceleration_mps2_x",
    "linear_acceleration_mps2_y", "linear_acceleration_mps2_z", "version",
]


def write_telemetry_csv(samples: list[dict], out_path: str,
                        tz: timezone | None = None) -> None:
    """Write the complete per-frame telemetry as CSV (one row per frame).

    Booleans are written as 1/0 for easy numeric parsing downstream; absent
    numeric fields are empty cells.  `source_clip`/`frame_index` identify the
    exact video frame each row came from (for later PTS mapping).  Times are
    naive local wall-clock unless `tz` supplies a UTC offset.
    """
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(TELEMETRY_COLUMNS)
        for s in samples:
            row = []
            for col in TELEMETRY_COLUMNS:
                v = s.get(col)
                if col == "time":
                    v = _iso_time(s["time"], tz) if s.get("time") is not None else ""
                elif isinstance(v, bool):
                    v = int(v)
                elif v is None:
                    v = ""
                row.append(v)
            w.writerow(row)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def build_gpx(clip_paths: list[str], ffmpeg: str | None = None,
              ffprobe: str | None = None, out_path: str | None = None,
              track_name: str = "Tesla dashcam route",
              csv_path: str | None = None,
              tz: timezone | None = None,
              log=print) -> dict:
    """Extract telemetry from an ORDERED list of clips and write one GPX track.

    The caller controls clip selection and order (e.g. to honor a trim window).
    Clips without SEI are skipped and reported; a missing/corrupt clip logs a
    warning and is treated the same rather than aborting the batch.  If
    `csv_path` is given, the complete per-frame telemetry is also written
    there as CSV.  `tz` (see parse_utc_offset) qualifies output timestamps.
    Returns a summary dict:
        {samples, clips_with_sei, clips_without_sei, bbox, distance_m,
         out_path, csv_path}
    """
    ffmpeg = ffmpeg or find_ffmpeg()
    ffprobe = ffprobe or find_ffprobe(ffmpeg)

    all_samples: list[dict] = []
    with_sei, without_sei = [], []
    for clip in clip_paths:
        name = os.path.basename(clip)
        try:
            samples = extract_samples(clip, ffmpeg, ffprobe)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            log(f"  {name}: WARNING — extraction failed, skipping ({e})")
            without_sei.append(clip)
            continue
        if samples:
            with_sei.append(clip)
            log(f"  {name}: {len(samples)} samples")
            all_samples.extend(samples)
        else:
            without_sei.append(clip)
            log(f"  {name}: no SEI telemetry (skipped)")

    # Defensive guard: clip-boundary timing should be monotonic and frame
    # sequence numbers unique.  Surface anomalies rather than emitting them
    # silently (we do not reorder — the caller chose the clip order).
    back_t = dup_seq = 0
    for a, b in zip(all_samples, all_samples[1:]):
        if a["time"] is not None and b["time"] is not None and b["time"] < a["time"]:
            back_t += 1
        if a["frame_seq"] is not None and a["frame_seq"] == b["frame_seq"]:
            dup_seq += 1
    if back_t:
        log(f"  WARNING: {back_t} backwards time step(s) across clip boundaries")
    if dup_seq:
        log(f"  WARNING: {dup_seq} duplicate frame_seq_no value(s) at boundaries")

    bbox = None
    dist = 0.0
    if all_samples:
        lats = [s["lat"] for s in all_samples]
        lons = [s["lon"] for s in all_samples]
        bbox = (min(lats), min(lons), max(lats), max(lons))
        for a, b in zip(all_samples, all_samples[1:]):
            dist += haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
        if out_path:
            write_gpx(all_samples, out_path, track_name, tz)
        if csv_path:
            write_telemetry_csv(all_samples, csv_path, tz)

    return {
        "samples": all_samples,
        "clips_with_sei": with_sei,
        "clips_without_sei": without_sei,
        "bbox": bbox,
        "distance_m": dist,
        "out_path": out_path if all_samples else None,
        "csv_path": csv_path if all_samples else None,
    }


# --------------------------------------------------------------------------
# Event-folder helpers / CLI
# --------------------------------------------------------------------------

def list_clips(folder: str, camera: str) -> list[str]:
    """Raw clips for one camera in an event folder, sorted chronologically.

    Matches only the strict Tesla naming pattern, so derived files
    (e.g. *-front-combined.mp4) are excluded.
    """
    clips = []
    for name in os.listdir(folder):
        m = CLIP_RE.match(name)
        if m and m.group(5) == camera:
            clips.append(os.path.join(folder, name))
    return sorted(clips)  # filename order == chronological order


def _cmd_probe(folder: str, ffmpeg: str) -> None:
    """Print a per-clip SEI table for all cameras in the folder."""
    rows = []
    for name in sorted(os.listdir(folder)):
        if CLIP_RE.match(name):
            n = count_sei(os.path.join(folder, name), ffmpeg)
            rows.append((name, n))
    if not rows:
        print("No raw dashcam clips found.")
        return
    width = max(len(r[0]) for r in rows)
    print(f"{'clip':<{width}}  SEI  samples")
    for name, n in rows:
        print(f"{name:<{width}}  {'yes' if n else 'no ':<3}  {n if n else '-'}")
    have = sum(1 for _, n in rows if n)
    print(f"\n{have}/{len(rows)} clips contain SEI telemetry.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Extract GPS telemetry from Tesla dashcam SEI data to GPX.")
    ap.add_argument("folder", help="Tesla dashcam event folder")
    ap.add_argument("--camera", default="front", choices=CAMERAS,
                    help="camera angle to extract (default: front)")
    ap.add_argument("-o", "--output", default=None,
                    help="output GPX path (default: <folder>/<foldername>_route.gpx)")
    ap.add_argument("--probe", action="store_true",
                    help="only report which clips contain SEI; write nothing")
    ap.add_argument("--no-telemetry", action="store_true",
                    help="skip the <foldername>_telemetry.csv sidecar")
    ap.add_argument("--utc-offset", default=None, metavar="+HH:MM",
                    help="UTC offset of the clips' local wall-clock (e.g. "
                         "-07:00); without it timestamps are emitted as naive "
                         "local time with no zone designator")
    args = ap.parse_args(argv)

    tz = None
    if args.utc_offset:
        try:
            tz = parse_utc_offset(args.utc_offset)
        except ValueError as e:
            ap.error(str(e))

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        ap.error(f"not a directory: {folder}")

    try:
        ffmpeg = find_ffmpeg()
        ffprobe = find_ffprobe(ffmpeg)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.probe:
        try:
            _cmd_probe(folder, ffmpeg)
        except RuntimeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        return 0

    clips = list_clips(folder, args.camera)
    if not clips:
        print(f"No raw '{args.camera}' clips found in {folder}", file=sys.stderr)
        return 1

    out_path = args.output or os.path.join(
        folder, f"{os.path.basename(folder)}_route.gpx")
    csv_path = None if args.no_telemetry else os.path.join(
        folder, f"{os.path.basename(folder)}_telemetry.csv")
    print(f"Extracting telemetry from {len(clips)} '{args.camera}' clips...")
    try:
        result = build_gpx(clips, ffmpeg, ffprobe, out_path,
                           track_name=f"{os.path.basename(folder)} ({args.camera})",
                           csv_path=csv_path, tz=tz)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    samples = result["samples"]
    if not samples:
        print("No telemetry found in any clip — no GPX written.", file=sys.stderr)
        return 1

    lat_min, lon_min, lat_max, lon_max = result["bbox"]
    print(f"\nWrote {out_path}")
    if result["csv_path"]:
        print(f"Wrote {result['csv_path']}")
    print(f"  trackpoints:      {len(samples)}")
    print(f"  clips with SEI:   {len(result['clips_with_sei'])}")
    print(f"  clips without:    {len(result['clips_without_sei'])}")
    print(f"  bounding box:     lat {lat_min:.6f}..{lat_max:.6f}, "
          f"lon {lon_min:.6f}..{lon_max:.6f}")
    print(f"  total distance:   {result['distance_m'] / 1000:.2f} km "
          f"({result['distance_m'] / 1609.344:.2f} mi)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
