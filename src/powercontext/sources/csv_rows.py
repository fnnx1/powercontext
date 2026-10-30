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

"""Row-level CSV Sources with a stable primary-key identity."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    AdapterSourceDefinition,
    Source,
    SourceMaterialization,
    TextEvidence,
)
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY

CSV_ROW_SOURCE_NAME = "csv-row"


class CSVDocumentInput(BaseModel):
    """A complete offline CSV document to split into row Sources."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """One validated row selected by its document and primary-key value."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """A captured CSV row whose identity is independent of row position."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def _validate_document_id(document_id: str) -> None:
    if not isinstance(document_id, str) or not document_id or document_id != document_id.strip():
        raise ValueError("document_id must be a non-empty trimmed string")


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the percent-encoded, delimiter-safe identity of one CSV row."""

    _validate_document_id(document_id)
    if not isinstance(primary_key_value, str) or primary_key_value == "":
        raise ValueError("primary_key_value must be a non-empty string")
    source_id = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(source_id) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"csv row source id must not exceed {MAX_SOURCE_ID_LENGTH} characters")
    return source_id


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and fully validate a CSV document before returning any row."""

    _validate_document_id(value.document_id)
    if not isinstance(value.primary_key, str) or value.primary_key == "":
        raise ValueError("primary_key must be a non-empty string")
    try:
        text = value.content.decode("utf-8-sig")
    except (UnicodeDecodeError, AttributeError) as error:
        raise ValueError("CSV content must be valid UTF-8") from error

    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        records = list(reader)
    except csv.Error as error:
        raise ValueError(f"malformed CSV: {error}") from error

    if not records:
        raise ValueError("CSV document has no header")
    header = records[0]
    if not header or any(not column or not column.strip() for column in header):
        raise ValueError("CSV header fields must be non-empty and non-blank")
    if len(set(header)) != len(header):
        raise ValueError("CSV header fields must be unique")
    try:
        key_index = header.index(value.primary_key)
    except ValueError as error:
        raise ValueError("primary_key must exactly match a CSV column") from error

    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()
    width = len(header)
    for record in records[1:]:
        if not record:
            continue
        if len(record) != width:
            raise ValueError("CSV record width does not match the header")
        primary_key_value = record[key_index]
        if not primary_key_value or not primary_key_value.strip():
            raise ValueError("CSV primary key value must be non-empty and non-blank")
        if primary_key_value in seen_keys:
            raise ValueError("CSV primary key values must be unique within a document")
        seen_keys.add(primary_key_value)
        row = CSVRowInput(
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=primary_key_value,
            columns=tuple(header),
            values=tuple(record),
        )
        csv_row_source_id(row.document_id, row.primary_key_value)
        rows.append(row)
    return tuple(rows)


class CSVRowAdapter:
    """Resolve CSV row inputs into captured Source values."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one CSV row as canonical text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row = dict(zip(source.columns, source.values, strict=True))
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowAdapter()
CSV_ROW_SOURCE_DEFINITION: AdapterSourceDefinition[
    CSVRowInput, CSVRowSource, CSVRowSource
] = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    version="1",
    projections=(CSVRowTextEvidenceProjection(),),
)

__all__ = [
    "CSV_ROW_SOURCE_ADAPTER",
    "CSV_ROW_SOURCE_DEFINITION",
    "CSVDocumentInput",
    "CSVRowInput",
    "CSVRowSource",
    "csv_row_source_id",
    "parse_csv_document",
]
