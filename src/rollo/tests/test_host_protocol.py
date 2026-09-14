"""Host wire protocol unit tests (L1: no process, no I/O).

Framing, limits and error mapping are pure functions of bytes, so they are
tested directly.  The cross-process behaviour lives in ``test_host_process.py``.
"""

from __future__ import annotations

import json

import pytest

from rollo.host.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    MAX_FRAME_BYTES,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    FrameTooLarge,
    HostProtocol,
    ProtocolError,
)


def _frame(**overrides) -> bytes:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "session.list", "params": {}}
    payload.update(overrides)
    # ``ensure_ascii=False`` is what puts real UTF-8 bytes on the wire; with the
    # default, non-ASCII would be escaped and the split-sequence case could not
    # be exercised at all.
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def test_a_frame_split_across_reads_decodes_once():
    """A read boundary is not a frame boundary."""

    protocol = HostProtocol()
    wire = _frame()

    assert protocol.feed(wire[:5]) == []
    assert protocol.feed(wire[5:12]) == []
    frames = protocol.feed(wire[12:])

    assert len(frames) == 1
    assert frames[0].method == "session.list"


def test_several_frames_in_one_read_all_decode():
    protocol = HostProtocol()
    wire = _frame(id=1) + _frame(id=2, method="host.initialize") + _frame(id=3)

    frames = protocol.feed(wire)

    assert [frame.id for frame in frames] == [1, 2, 3]
    assert [frame.method for frame in frames] == [
        "session.list",
        "host.initialize",
        "session.list",
    ]


def test_a_multibyte_character_split_across_reads_is_reassembled():
    """UTF-8 sequences may be cut mid-character; decoding waits for the rest."""

    protocol = HostProtocol()
    wire = _frame(params={"note": "中文"})
    cut = wire.index("中".encode("utf-8")) + 1

    assert protocol.feed(wire[:cut]) == []
    frames = protocol.feed(wire[cut:])

    assert len(frames) == 1
    assert frames[0].params == {"note": "中文"}


def test_a_frame_without_a_newline_over_the_limit_is_rejected():
    protocol = HostProtocol(max_frame_bytes=64)

    with pytest.raises(FrameTooLarge):
        protocol.feed(b"x" * 65)
    assert protocol.frames_rejected == 1


def test_a_complete_frame_over_the_limit_is_rejected():
    protocol = HostProtocol(max_frame_bytes=32)

    with pytest.raises(FrameTooLarge):
        protocol.feed(_frame(params={"pad": "y" * 80}))


def test_blank_lines_are_not_frames():
    """A stray newline must not be reported as malformed JSON."""

    protocol = HostProtocol()

    frames = protocol.feed(b"\n\n" + _frame() + b"\n")

    assert [frame.id for frame in frames] == [1]
    assert protocol.frames_rejected == 0


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"{not json}\n", PARSE_ERROR),
        (b'["array"]\n', INVALID_REQUEST),
        (b'{"id":1,"params":{}}\n', INVALID_REQUEST),
        (b'{"id":1,"method":"x","params":[]}\n', INVALID_PARAMS),
        (b"\xff\xfe\n", PARSE_ERROR),
    ],
)
def test_malformed_frames_map_to_stable_codes(raw: bytes, code: int):
    protocol = HostProtocol()

    with pytest.raises(ProtocolError) as caught:
        protocol.feed(raw)

    assert caught.value.code == code
    assert protocol.frames_rejected == 1


def test_a_request_without_an_id_is_a_notification():
    protocol = HostProtocol()
    wire = (json.dumps({"jsonrpc": "2.0", "method": "events.unsubscribe"}) + "\n").encode()

    frames = protocol.feed(wire)

    assert frames[0].is_notification is True
    assert frames[0].params == {}


def test_business_codes_are_declared_or_rejected():
    """An undeclared business code is a programming error, not a wire message."""

    with pytest.raises(ValueError):
        ProtocolError("x", business="made_up_code")


def test_business_code_lands_in_error_data():
    error = ProtocolError("busy", code=METHOD_NOT_FOUND, business="session_busy")

    assert error.to_error()["data"]["code"] == "session_busy"


def test_encode_is_one_utf8_line_without_ascii_escapes():
    encoded = HostProtocol.encode({"jsonrpc": "2.0", "result": {"note": "中文"}})

    assert encoded.endswith(b"\n")
    assert encoded.count(b"\n") == 1
    assert "中文".encode("utf-8") in encoded
    assert json.loads(encoded.decode("utf-8"))["result"] == {"note": "中文"}


def test_protocol_version_is_one():
    assert PROTOCOL_VERSION == 1


def test_default_frame_limit_is_one_mebibyte():
    assert MAX_FRAME_BYTES == 1024 * 1024
