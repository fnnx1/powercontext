# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Versioned, chunked Context Packs and prefix-reusing deltas."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

PACK_SCHEMA = "powercontext.context-pack.v1"
DELTA_SCHEMA = "powercontext.context-pack-delta.v1"


class ContextPackError(ValueError):
    """A Context Pack or delta violated the wire protocol."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _b64encode(item: bytes) -> str:
    return base64.b64encode(item).decode("ascii")


def _b64decode(value: object) -> bytes:
    if not isinstance(value, str):
        raise ContextPackError("chunk must be a Base64 string")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise ContextPackError("chunk is not valid Base64") from error
    try:
        decoded.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextPackError("chunk is not valid UTF-8") from error
    return decoded


def _digest_for(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": PACK_SCHEMA, "items": [_b64encode(item) for item in items]}
    return "sha256:" + hashlib.sha256(_canonical(unsigned)).hexdigest()


@dataclass(frozen=True, slots=True)
class ContextPack:
    """A complete context body split into deterministic, valid-UTF-8 chunks."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: tuple[bytes, ...]) -> ContextPack:
        normalized = tuple(items)
        for item in normalized:
            if not isinstance(item, bytes):
                raise ContextPackError("pack items must be bytes")
            try:
                item.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ContextPackError("pack items must be valid UTF-8") from error
        digest = _digest_for(normalized)
        # byte_count is part of the serialized document, so solve its fixed point.
        count = 0
        while True:
            payload = {
                "schema": PACK_SCHEMA,
                "items": [_b64encode(item) for item in normalized],
                "digest": digest,
                "byte_count": count,
            }
            encoded = _canonical(payload)
            if len(encoded) == count:
                return cls(items=normalized, digest=digest, byte_count=count)
            count = len(encoded)

    @classmethod
    def from_content(cls, content: str | bytes, *, chunk_bytes: int = 512) -> ContextPack:
        if not isinstance(chunk_bytes, int) or isinstance(chunk_bytes, bool) or chunk_bytes <= 0:
            raise ContextPackError("chunk_bytes must be a positive integer")
        if isinstance(content, bytes):
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ContextPackError("content must be valid UTF-8") from error
        elif isinstance(content, str):
            text = content
        else:
            raise ContextPackError("content must be str or bytes")
        chunks: list[bytes] = []
        current: list[str] = []
        current_bytes = 0
        for character in text:
            encoded = character.encode("utf-8")
            size = len(encoded)
            if size > chunk_bytes:
                raise ContextPackError("a single character exceeds chunk_bytes")
            if current_bytes + size > chunk_bytes and current:
                chunks.append("".join(current).encode("utf-8"))
                current = []
                current_bytes = 0
            current.append(character)
            current_bytes += size
        if current:
            chunks.append("".join(current).encode("utf-8"))
        return cls.build(tuple(chunks))

    def to_bytes(self) -> bytes:
        payload = {
            "schema": PACK_SCHEMA,
            "items": [_b64encode(item) for item in self.items],
            "digest": self.digest,
            "byte_count": self.byte_count,
        }
        return _canonical(payload)

    @classmethod
    def from_bytes(cls, raw: bytes | bytearray | str) -> ContextPack:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ContextPackError("pack must be valid JSON") from error
        if not isinstance(data, dict) or set(data) != {"schema", "items", "digest", "byte_count"}:
            raise ContextPackError("pack must contain exactly schema, items, digest and byte_count")
        if data["schema"] != PACK_SCHEMA:
            raise ContextPackError("unsupported pack schema")
        items_raw = data["items"]
        if not isinstance(items_raw, list):
            raise ContextPackError("pack items must be an array")
        items = tuple(_b64decode(item) for item in items_raw)
        digest = data["digest"]
        if not isinstance(digest, str) or digest != _digest_for(items):
            raise ContextPackError("pack digest does not match its items")
        count = data["byte_count"]
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ContextPackError("pack byte_count must be a non-negative integer")
        raw_bytes = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
        if count != len(raw_bytes):
            raise ContextPackError("pack byte_count does not match its serialized size")
        return cls(items=items, digest=digest, byte_count=count)


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """A target suffix that reuses a prefix of one exact base pack."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("delta requires two ContextPack values")
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        payload = {
            "schema": DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": [_b64encode(item) for item in self.appended],
        }
        return _canonical(payload)

    @classmethod
    def from_bytes(cls, raw: bytes | bytearray | str) -> ContextPackDelta:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ContextPackError("delta must be valid JSON") from error
        required = {"schema", "base_digest", "target_digest", "prefix_items", "appended"}
        if not isinstance(data, dict) or set(data) != required:
            raise ContextPackError("delta must contain exactly its protocol fields")
        if data["schema"] != DELTA_SCHEMA:
            raise ContextPackError("unsupported delta schema")
        for name in ("base_digest", "target_digest"):
            if not isinstance(data[name], str):
                raise ContextPackError(f"{name} must be a string")
        prefix = data["prefix_items"]
        if not isinstance(prefix, int) or isinstance(prefix, bool) or prefix < 0:
            raise ContextPackError("prefix_items must be a non-negative integer")
        appended_raw = data["appended"]
        if not isinstance(appended_raw, list):
            raise ContextPackError("appended must be an array")
        appended = tuple(_b64decode(item) for item in appended_raw)
        return cls(
            base_digest=data["base_digest"],
            target_digest=data["target_digest"],
            prefix_items=prefix,
            appended=appended,
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Validate a delta against its base and reconstruct the target pack."""

    if not isinstance(base, ContextPack) or not isinstance(delta, ContextPackDelta):
        raise ContextPackError("apply requires a ContextPack and a ContextPackDelta")
    if delta.base_digest != base.digest:
        raise ContextPackError("delta base digest does not match the supplied pack")
    if delta.prefix_items > len(base.items):
        raise ContextPackError("delta prefix exceeds the base pack items")
    rebuilt = ContextPack.build(base.items[: delta.prefix_items] + delta.appended)
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("delta target digest does not match the rebuilt pack")
    return rebuilt


__all__ = [
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
