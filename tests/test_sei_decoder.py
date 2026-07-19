"""Tests for the hand-rolled SEI/protobuf decoder in tesla_gps.py.

All fixtures are built in-code with struct.pack + correct protobuf wire tags --
no real footage (real SEI carries private GPS, and .gitignore blocks mp4s).
"""
import struct

import pytest

import tesla_gps as g


# --- protobuf wire-format builders -----------------------------------------

def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _tag(field, wt):
    return _varint((field << 3) | wt)


def pv(field, val):   # varint field (wire type 0)
    return _tag(field, 0) + _varint(val)


def pf(field, val):   # fixed32 float (wire type 5)
    return _tag(field, 5) + struct.pack("<f", val)


def pd(field, val):   # fixed64 double (wire type 1)
    return _tag(field, 1) + struct.pack("<d", val)


def make_payload(*, version=1, lat=40.7128, lon=-74.0060, extra=b""):
    """A Tesla SEI payload: magic prefix + a protobuf SeiMetadata starting at
    field 1 (version) so the decoder's 0x08 anchor is unambiguous."""
    proto = pv(1, version) + extra
    if lat is not None:
        proto += pd(11, lat)
    if lon is not None:
        proto += pd(12, lon)
    return g.SEI_MAGIC + proto


# --- decode_sei_metadata ---------------------------------------------------

def test_decode_full_record():
    proto = (pv(1, 2) + pv(2, 4) + pv(3, 123) + pf(4, 12.5)
             + pd(11, 40.7128) + pd(12, -74.0060) + pd(13, 91.25))
    meta = g.decode_sei_metadata(g.SEI_MAGIC + proto)
    assert meta is not None
    assert meta["version"] == 2
    assert meta["gear_state"] == 4
    assert meta["frame_seq_no"] == 123
    assert meta["vehicle_speed_mps"] == pytest.approx(12.5)
    assert meta["latitude_deg"] == pytest.approx(40.7128)
    assert meta["longitude_deg"] == pytest.approx(-74.0060)
    assert meta["heading_deg"] == pytest.approx(91.25)


def test_decode_wrong_magic_returns_none():
    payload = b"\x00\x00\x00\x00" + pv(1, 1) + pd(11, 1.0) + pd(12, 2.0)
    assert g.decode_sei_metadata(payload) is None


def test_decode_missing_latitude_returns_none():
    payload = make_payload(lat=None, lon=-74.0)
    assert g.decode_sei_metadata(payload) is None


def test_decode_missing_longitude_returns_none():
    payload = make_payload(lat=40.0, lon=None)
    assert g.decode_sei_metadata(payload) is None


def test_decode_tolerates_unknown_length_delimited_field():
    # Field 20 as wire type 2 (length-delimited) is not in the schema; the
    # decoder must skip it and still recover lat/lon.
    unknown = _tag(20, 2) + _varint(3) + b"abc"
    meta = g.decode_sei_metadata(make_payload(extra=unknown))
    assert meta is not None
    assert meta["latitude_deg"] == pytest.approx(40.7128)


def test_decode_truncated_varint_does_not_crash():
    # A varint tag/value cut off mid-stream: decoder breaks out; with no lat/lon
    # yet it returns None rather than raising.
    payload = g.SEI_MAGIC + pv(1, 1) + b"\xff\xff\xff"  # dangling continuation
    assert g.decode_sei_metadata(payload) is None


def test_decode_truncated_fixed64_bails_gracefully():
    # A double (wire type 1) tag with fewer than 8 trailing bytes.
    payload = g.SEI_MAGIC + pv(1, 1) + _tag(11, 1) + b"\x00\x00\x00"
    assert g.decode_sei_metadata(payload) is None


def test_decode_lat_lon_before_truncation_still_returned():
    # lat/lon read first, then a truncated fixed64 -- decode stops but keeps what
    # it already has.
    payload = (g.SEI_MAGIC + pv(1, 1) + pd(11, 40.0) + pd(12, -74.0)
               + _tag(13, 1) + b"\x01\x02")
    meta = g.decode_sei_metadata(payload)
    assert meta is not None
    assert meta["latitude_deg"] == pytest.approx(40.0)
    assert "heading_deg" not in meta


# --- _strip_emulation_prevention -------------------------------------------

def test_strip_removes_when_next_byte_le_3():
    assert g._strip_emulation_prevention(b"\x00\x00\x03\x00") == b"\x00\x00\x00"
    assert g._strip_emulation_prevention(b"\x00\x00\x03\x01") == b"\x00\x00\x01"
    assert g._strip_emulation_prevention(b"\x00\x00\x03\x03") == b"\x00\x00\x03"


def test_strip_keeps_when_next_byte_gt_3():
    # 00 00 03 45 -- the 0x03 is real data, must be untouched.
    assert g._strip_emulation_prevention(b"\x00\x00\x03\x45") == b"\x00\x00\x03\x45"


def test_strip_handles_trailing_03_at_end():
    assert g._strip_emulation_prevention(b"\x00\x00\x03") == b"\x00\x00"


def test_strip_noop_when_no_marker():
    data = b"\x01\x02\x03\x04"  # no 00 00 03 sequence
    assert g._strip_emulation_prevention(data) == data


# --- _parse_sei_payloads ---------------------------------------------------

def _sei_nal(messages):
    """Build a type-6 SEI NAL: header 0x06 + messages + rbsp_trailing_bits."""
    body = b"".join(messages)
    return bytes([0x06]) + body + b"\x80"


def _msg(ptype, payload):
    """One SEI message with single-byte type/size (payload < 255 bytes)."""
    return bytes([ptype, len(payload)]) + payload


def test_parse_single_payload():
    nal = _sei_nal([_msg(5, b"hello")])
    got = list(g._parse_sei_payloads(nal))
    assert got == [(5, b"hello")]


def test_parse_multiple_payloads():
    nal = _sei_nal([_msg(5, b"aaa"), _msg(1, b"bb")])
    assert list(g._parse_sei_payloads(nal)) == [(5, b"aaa"), (1, b"bb")]


def test_parse_ff_extended_type_and_size():
    # ptype 255+5 = 260 encoded as FF 05; psize 255+2 = 257 encoded as FF 02.
    payload = b"x" * 257
    body = bytes([0xFF, 5]) + bytes([0xFF, 2]) + payload
    nal = bytes([0x06]) + body + b"\x80"
    got = list(g._parse_sei_payloads(nal))
    assert got == [(260, payload)]


def test_parse_truncated_payload_yields_nothing():
    # Declared size 10 but only 3 bytes present -> bail out, yield nothing.
    nal = bytes([0x06, 5, 10]) + b"abc"
    assert list(g._parse_sei_payloads(nal)) == []


# --- _iter_nals ------------------------------------------------------------

def test_iter_nals_three_and_four_byte_start_codes():
    sei = bytes([0x06]) + b"sei"
    slice_nal = bytes([0x65]) + b"pic"  # 0x65 & 0x1f == 5 (IDR slice)
    stream = (b"\x00\x00\x01" + sei          # 3-byte start code
              + b"\x00\x00\x00\x01" + slice_nal)  # 4-byte start code
    nals = list(g._iter_nals(stream))
    types = [t for t, _ in nals]
    assert 6 in types and 5 in types
    # payload bytes recovered intact for the SEI NAL
    sei_nal = next(n for t, n in nals if t == 6)
    assert sei_nal.startswith(bytes([0x06]))


# --- _extract_raw frame anchoring ------------------------------------------

def _make_slice(first_mb_zero=True):
    header = 0x65  # IDR slice (type 5)
    second = 0x80 if first_mb_zero else 0x00  # high bit set => first_mb_in_slice 0
    return bytes([header, second]) + b"picdata"


def test_extract_raw_binds_sei_to_next_frame(monkeypatch):
    payload = make_payload(lat=40.7128, lon=-74.0060)
    # sanity: our synthetic payload carries no emulation-prevention hazard
    assert b"\x00\x00\x03" not in payload
    sei_nal = _sei_nal([_msg(5, payload)])
    stream = (b"\x00\x00\x01" + sei_nal
              + b"\x00\x00\x01" + _make_slice())
    monkeypatch.setattr(g, "_demux_h264", lambda clip, ffmpeg: stream)
    raw = g._extract_raw("clip.mp4", "ffmpeg")
    assert len(raw) == 1
    frame_index, meta = raw[0]
    assert frame_index == 0
    assert meta["latitude_deg"] == pytest.approx(40.7128)


def test_extract_raw_second_frame_index(monkeypatch):
    payload = make_payload(lat=40.7128, lon=-74.0060)
    sei_nal = _sei_nal([_msg(5, payload)])
    # first picture has no SEI; SEI precedes the SECOND picture -> frame_index 1
    stream = (b"\x00\x00\x01" + _make_slice()
              + b"\x00\x00\x01" + sei_nal
              + b"\x00\x00\x01" + _make_slice())
    monkeypatch.setattr(g, "_demux_h264", lambda clip, ffmpeg: stream)
    raw = g._extract_raw("clip.mp4", "ffmpeg")
    assert len(raw) == 1
    assert raw[0][0] == 1


def test_extract_raw_no_sei_returns_empty(monkeypatch):
    stream = b"\x00\x00\x01" + _make_slice() + b"\x00\x00\x01" + _make_slice()
    monkeypatch.setattr(g, "_demux_h264", lambda clip, ffmpeg: stream)
    assert g._extract_raw("clip.mp4", "ffmpeg") == []
