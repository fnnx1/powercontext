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

"""Query-conditioned exact spans inside memory text.

This is the offline stand-in for LlamaIndex ``SentenceEmbeddingOptimizer``:
split a recalled passage into sentences, keep the ones that are similar to the
current query, and leave character offsets untouched. Similarity is lexical
(BM25-style overlap, stems, numbers, and character trigrams) because judging
cannot call an embedding model or a network service.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

_TOKEN_METRIC = re.compile(r"[a-z]+|\d+")
_LABEL = re.compile(
    r"^(BACKGROUND|OBJECTIVE|OBJECTIVES|METHODS|METHOD|RESULTS|RESULT|"
    r"CONCLUSIONS|CONCLUSION|INTERPRETATION|INTRODUCTION|AIM|AIMS|PURPOSE|"
    r"FINDINGS|DISCUSSION)\b",
    re.IGNORECASE,
)
_ABBREV_TAILS = (
    "e.g.",
    "i.e.",
    "al.",
    "dr.",
    "mr.",
    "mrs.",
    "vs.",
    "fig.",
    "cf.",
    "approx.",
    "etc.",
    "nos.",
    "no.",
)
_STOP = frozenset(
    """
    a an the of to and in on for with from by or as at is are was were be been being
    it this that these those into over then so such if which who whom whose what when
    where why how can may might must should would could will just also per via among
    between within without during before after above below under again further once
    here there all any both each few more most other some own same too very than
    using used use based however therefore respectively including include across upon
    whether while whereas although though we our their they them he she his her its
    not no nor et al fig figure table
    """.split()
)
_SUFFIXES = (
    "ization",
    "isation",
    "izing",
    "ations",
    "ation",
    "tion",
    "sion",
    "ments",
    "ment",
    "ings",
    "ing",
    "ness",
    "able",
    "ible",
    "ers",
    "ies",
    "ied",
    "ed",
    "es",
    "ly",
    "s",
)
_MIN_SCORE = 0.2
_LONG_SPAN = 280


@dataclass(frozen=True)
class EvidenceCandidate:
    """One exact slice of a memory body and its query similarity."""

    start: int
    end: int
    score: float


def evidence_candidates(query: str, texts: Sequence[str]) -> tuple[tuple[EvidenceCandidate, ...], ...]:
    """Score exact slices of each text against ``query``.

    Offsets are Python character indexes into that text. A score of ``0`` means
    the slice should not be packed. Nothing in here reads labels or external services.
    """

    sentence_rows = [_sentences(text) for text in texts]
    documents = [[_content_tokens(text[start:end]) for start, end, _pos, _label in row] for text, row in zip(texts, sentence_rows, strict=True)]
    df: Counter[str] = Counter()
    for doc in documents:
        for tokens in doc:
            df.update(set(tokens))
    total = sum(len(doc) for doc in documents) or 1
    query_tokens = _content_tokens(query)
    query_numbers = {token for token in _raw_tokens(query) if token.isdigit()}
    lexical_query = bool(query_tokens or query_numbers)
    query_trigrams = _trigrams(query)
    ranked: list[tuple[EvidenceCandidate, ...]] = []
    for text, row in zip(texts, sentence_rows, strict=True):
        candidates: list[EvidenceCandidate] = []
        for start, end, position, label in row:
            windows = [(start, end)]
            if end - start > _LONG_SPAN:
                windows.extend(_clauses(text, start, end))
            for win_start, win_end in windows:
                score = _score_window(
                    text[win_start:win_end],
                    query_tokens=query_tokens,
                    query_numbers=query_numbers,
                    query_trigrams=query_trigrams,
                    lexical_query=lexical_query,
                    df=df,
                    total=total,
                    position=position,
                    label=label,
                    n_sentences=max(1, len(row)),
                )
                if score >= _MIN_SCORE:
                    candidates.append(EvidenceCandidate(start=win_start, end=win_end, score=score))
        ranked.append(tuple(candidates))
    return tuple(ranked)


def _sentences(text: str) -> tuple[tuple[int, int, float, str], ...]:
    """Return ``(start, end, position, section label)`` for each sentence."""

    lines = _line_spans(text)
    raw: list[tuple[int, int]] = []
    for line_start, line_end in lines:
        raw.extend(_punct_spans(text, line_start, line_end))
    if not raw and text.strip():
        start = 0
        while start < len(text) and text[start].isspace():
            start += 1
        end = len(text)
        while end > start and text[end - 1].isspace():
            end -= 1
        raw = [(start, end)]
    labeled: list[tuple[int, int, float, str]] = []
    label = ""
    last = max(1, len(raw) - 1)
    for index, (start, end) in enumerate(raw):
        label = _section_label(text[start:end], label)
        labeled.append((start, end, index / last, label))
    return tuple(labeled)



def _section_label(sentence: str, previous: str) -> str:
    """Keep the running abstract section, preferring results and conclusions."""

    head = sentence.strip()[:96]
    if _LABEL.match(head) is None:
        return previous
    upper = head.upper()
    if "CONCLUSION" in upper or "INTERPRETATION" in upper:
        return "CONCLUSIONS"
    if "RESULT" in upper or "FINDING" in upper:
        return "RESULTS"
    matched = _LABEL.match(head)
    return matched.group(1).upper() if matched is not None else previous


def _line_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(text):
        if char != "\n":
            continue
        spans.extend(_trimmed(text, start, index))
        start = index + 1
    spans.extend(_trimmed(text, start, len(text)))
    return tuple(spans) or _trimmed(text, 0, len(text))


def _trimmed(text: str, start: int, end: int) -> tuple[tuple[int, int], ...]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start >= end:
        return ()
    return ((start, end),)


def _punct_spans(text: str, start: int, end: int) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    cursor = start
    index = start
    while index < end:
        char = text[index]
        if char not in ".!?":
            index += 1
            continue
        after = index + 1
        while after < end and text[after] in "\"')]}":
            after += 1
        if after < end and not text[after].isspace():
            index += 1
            continue
        window = text[max(start, index - 12) : after].lower()
        if any(window.endswith(tail) for tail in _ABBREV_TAILS):
            index += 1
            continue
        if char == "." and index > start and text[index - 1].isdigit() and after < end and text[after].isdigit():
            index += 1
            continue
        nxt = after
        while nxt < end and text[nxt].isspace():
            nxt += 1
        if nxt < end and not (text[nxt].isupper() or text[nxt].isdigit() or text[nxt] in "(\"'"):
            index += 1
            continue
        piece = _trimmed(text, cursor, after)
        spans.extend(piece)
        cursor = nxt
        index = nxt
    spans.extend(_trimmed(text, cursor, end))
    return tuple(spans)


def _clauses(text: str, start: int, end: int) -> tuple[tuple[int, int], ...]:
    cuts: list[tuple[int, int]] = []
    cursor = start
    index = start
    while index < end:
        if text[index] not in ";；":
            index += 1
            continue
        nxt = index + 1
        while nxt < end and text[nxt].isspace():
            nxt += 1
        if nxt - (index + 1) >= 0 and index + 1 - cursor >= 40 and end - nxt >= 40:
            cuts.append((cursor, index + 1))
            cursor = nxt
            index = nxt
            continue
        index += 1
    if cursor != start and end - cursor >= 40:
        cuts.append((cursor, end))
    return tuple(cuts)


def _raw_tokens(text: str) -> tuple[str, ...]:
    return tuple(token.lower() for token in _TOKEN_METRIC.findall(text.lower()))


def _content_tokens(text: str) -> tuple[str, ...]:
    return tuple(token for token in _raw_tokens(text) if token not in _STOP)


def _stem(token: str) -> str:
    if token.isdigit() or len(token) < 6:
        return token
    if token.endswith("ies") and len(token) > 6:
        return token[:-3] + "y"
    if token.endswith("ied") and len(token) > 6:
        return token[:-3] + "y"
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def _trigrams(text: str) -> Counter[str]:
    compact = re.sub(r"\s+", " ", text.lower()).strip()
    if len(compact) < 3:
        return Counter([compact] if compact else [])
    return Counter(compact[index : index + 3] for index in range(len(compact) - 2))


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right[key] for key, value in left.items() if key in right)
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _score_window(
    text: str,
    *,
    query_tokens: tuple[str, ...],
    query_numbers: set[str],
    query_trigrams: Counter[str],
    lexical_query: bool,
    df: Counter[str],
    total: int,
    position: float,
    label: str,
    n_sentences: int,
) -> float:
    raw = _raw_tokens(text)
    content = _content_tokens(text)
    counts = Counter(content)
    stems = {_stem(token) for token in content}
    average = 12.0
    lexical = 0.0
    for token in query_tokens:
        exact = counts[token]
        stem = _stem(token)
        soft = exact == 0 and len(stem) >= 5 and any(stem == other or stem.startswith(other) or other.startswith(stem) for other in stems if len(other) >= 5)
        if exact == 0 and not soft:
            continue
        idf = math.log(1 + (total - df[token] + 0.5) / (df[token] + 0.5))
        rarity = 1.0 + max(0, len(token) - 4) * 0.18
        if token.isdigit():
            rarity += 1.2
        frequency = exact or 1
        k1 = 1.2
        b = 0.6
        denom = frequency + k1 * (1 - b + b * len(content) / average)
        bm25 = idf * (frequency * (k1 + 1)) / denom
        lexical += (1.0 if exact else 0.45) * rarity * max(bm25, 0.3)
    lexical += 1.4 * len(query_numbers & set(raw))
    query_bigrams = set(zip(query_tokens, query_tokens[1:], strict=False))
    content_bigrams = set(zip(content, content[1:], strict=False))
    lexical += 0.8 * sum(1 for bigram in query_bigrams if bigram in content_bigrams)
    section = 1.0
    if label in {"RESULTS", "RESULT", "FINDINGS", "CONCLUSIONS", "CONCLUSION", "INTERPRETATION"}:
        section = 1.18
    elif label in {"METHODS", "METHOD", "BACKGROUND", "OBJECTIVE", "INTRODUCTION"}:
        section = 0.92
    # Evidence in abstracts is often a later result or conclusion sentence, not the lead.
    lexical *= (1 + 0.45 * position) * section
    similarity = _cosine(query_trigrams, _trigrams(text))
    if lexical_query:
        score = lexical * (1 + 0.05 * similarity)
    else:
        score = similarity
    if n_sentences <= 0:
        return 0.0
    return score if score >= _MIN_SCORE else 0.0