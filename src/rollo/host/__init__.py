"""Stdio host package: the IPC protocol v1 transport for the Application API."""

from __future__ import annotations

from .protocol import (
    BODY_PAGE_DEFAULT_BYTES,
    BODY_PAGE_MAX_BYTES,
    MAX_FRAME_BYTES,
    MAX_PAGE_ITEMS,
    PAGE_BODY_TARGET_BYTES,
    PREVIEW_BYTES,
    PROTOCOL_VERSION,
    TEXT_DELTA_BYTES,
    Frame,
    FrameTooLarge,
    HostProtocol,
    ProtocolError,
)
from .server import CONTROL_METHODS, OBSERVATION_METHODS, HostServer, build_context

__all__ = [
    "BODY_PAGE_DEFAULT_BYTES",
    "BODY_PAGE_MAX_BYTES",
    "CONTROL_METHODS",
    "Frame",
    "FrameTooLarge",
    "HostProtocol",
    "HostServer",
    "MAX_FRAME_BYTES",
    "MAX_PAGE_ITEMS",
    "OBSERVATION_METHODS",
    "PAGE_BODY_TARGET_BYTES",
    "PREVIEW_BYTES",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "TEXT_DELTA_BYTES",
    "build_context",
]
