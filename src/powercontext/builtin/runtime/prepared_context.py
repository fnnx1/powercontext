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

"""Prepare final, bounded context from approved Artifact evidence."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from itertools import chain
from typing import TypeVar

from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.artifacts.experience import Experience, ExperienceSearchHit, render_experience
from powercontext.builtin.artifacts.memory.models import MemoryCitation, MemoryHit
from powercontext.builtin.artifacts.profile.models import Profile
from powercontext.builtin.artifacts.topic_memory import TopicMemory, TopicMemorySearchHit
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.evidence_selection import evidence_candidates
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext
from powercontext.builtin.runtime.prepared_code import (
    CodeEvidenceRef,
    PreparedCodeCandidate,
    assemble_code,
)
from powercontext.builtin.runtime.prepared_text import (
    TRUST_POLICY,
    ContextTextItem,
    fit_context_text_item,
    render_context_text,
)
from powercontext.builtin.runtime.recall_sufficiency import RecallBudgetView

_MIN_TRUNCATED_CONTENT_BYTES = 64
_ELLIPSIS = "…"
_BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
_END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"
_Item = TypeVar("_Item")


@dataclass(frozen=True)
class _PreparedContextEntry:
    origin: PreparedContextOrigin
    kind: str
    citation: dict[str, object]
    content: str
    truncated: bool


@dataclass(frozen=True)
class _EntryFit:
    """One fit attempt on the non-assembly path: the entry that fitted, or why none did.

    The two drop reasons are disjoint and mirror the assembly path's
    :class:`~powercontext.builtin.runtime.prepared_text.FitOutcome`. An entry is dropped
    *below the minimum truncated content* when a shorter rendering exists and fits but is too
    short to be an honest delivery; it is dropped *with no fitting truncation* when no
    shortened rendering fits at all.
    """

    entry: _PreparedContextEntry | None = None
    dropped_below_min_bytes: bool = False
    dropped_no_fitting_truncation: bool = False


@dataclass(frozen=True)
class PreparedContextOmissions:
    """Aggregate counts of the items the byte budget omitted in one build.

    Every count is a per-call aggregate, never per-entry attribution and never a verdict about
    an entry: an entry losing to the budget is not a negative result about that entry.

    ``dropped_items`` is the sum of its two sub-counts, so "the budget could not fit this
    item" can be told apart from "this item was too short to truncate into the remaining
    space". A single merged counter would misreport the second cause as the first.
    """

    truncated_items: int = 0
    dropped_items: int = 0
    dropped_below_min_bytes: int = 0
    dropped_no_fitting_truncation: int = 0


@dataclass(frozen=True)
class MemoryFragment:
    """One literal span selected from an exact Memory entry version."""

    address: MemoryEntryAddress
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class PreparedContextBuild:
    """Final public context and the exact origins selected to produce it.

    ``omissions`` is always filled by the Builder. The RFC 1560 recall trace is deliberately
    **not** a field here: ``ScopedContextApplication._prepare`` returns ``build.context`` and
    discards the rest, so such a field would have no production observer. The trace is
    delivered through the Runtime's optional ``RecallEffortSink`` instead.
    """

    context: PreparedContext
    origins: tuple[PreparedContextOrigin, ...]
    omissions: PreparedContextOmissions = PreparedContextOmissions()
    code_origins: tuple[CodeEvidenceRef, ...] = ()
    fragments: tuple[MemoryFragment, ...] = ()


@dataclass(frozen=True)
class MemoryEntryAddress:
    """Identify one exact Memory entry version across Scope boundaries."""

    memory: ArtifactAddress
    entry_id: str
    entry_version_id: str


PreparedContextOrigin = MemoryCitation | ArtifactRef | MemoryEntryAddress | ArtifactAddress


@dataclass(frozen=True)
class PreparedMemoryCandidates:
    """Memory candidates read from one Scope."""

    scope_id: str
    memory_ref: ArtifactRef | None = None
    hits: tuple[MemoryHit, ...] = ()


@dataclass(frozen=True)
class PreparedExperienceCandidates:
    """Experience candidates read from one Scope."""

    scope_id: str
    hits: tuple[ExperienceSearchHit, ...] = ()


@dataclass(frozen=True)
class PreparedProfileCandidate:
    """The latest committed Profile snapshot read from one Scope."""

    scope_id: str
    profile: Profile


class PreparedContextBuilder:
    """Select and render final context without I/O, persistence, or reranking."""

    memory_candidate_limit = 16
    topic_memory_candidate_limit = 8
    experience_candidate_limit = 8
    candidate_limit = memory_candidate_limit
    entry_limit = 8
    topic_memory_entry_limit = 8
    experience_entry_limit = 2
    max_entry_content_bytes = 2000
    _text_entry_limit: int | None = None

    def empty(self) -> PreparedContext:
        return PreparedContext(status="empty", content=None, content_bytes=0)

    def build(
        self,
        *,
        request: PrepareContextRequest,
        scope_id: str | None = None,
        memory_ref: ArtifactRef | None = None,
        hits: Sequence[MemoryHit] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_hits: Sequence[ExperienceSearchHit] = (),
    ) -> PreparedContext:
        return self.build_result(
            request=request,
            scope_id=scope_id,
            memory_ref=memory_ref,
            hits=hits,
            topic_memory_hits=topic_memory_hits,
            experience_hits=experience_hits,
        ).context

    def build_result(
        self,
        *,
        request: PrepareContextRequest,
        scope_id: str | None = None,
        memory_ref: ArtifactRef | None = None,
        hits: Sequence[MemoryHit] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_hits: Sequence[ExperienceSearchHit] = (),
    ) -> PreparedContextBuild:
        return self.build_scopes_result(
            request=request,
            current_scope_id=scope_id,
            memory_candidates=(
                PreparedMemoryCandidates(scope_id=scope_id or "", memory_ref=memory_ref, hits=tuple(hits)),
            ),
            experience_candidates=(PreparedExperienceCandidates(scope_id=scope_id or "", hits=tuple(experience_hits)),),
            topic_memory_hits=topic_memory_hits,
        )

    def build_scopes_result(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_candidates: Sequence[PreparedExperienceCandidates] = (),
        profile_candidates: Sequence[PreparedProfileCandidate] = (),
        code_candidates: Sequence[PreparedCodeCandidate] = (),
    ) -> PreparedContextBuild:
        if request.include_code:

            def historical(history_request: PrepareContextRequest, entries: int) -> PreparedContextBuild:
                builder = PreparedContextBuilder()
                builder.entry_limit = entries
                builder._text_entry_limit = entries if entries < self.entry_limit else None
                return builder.build_scopes_result(
                    request=history_request.model_copy(update={"include_code": False}),
                    current_scope_id=current_scope_id,
                    memory_candidates=memory_candidates,
                    topic_memory_hits=topic_memory_hits,
                    experience_candidates=experience_candidates,
                    profile_candidates=profile_candidates,
                )

            return assemble_code(request, code_candidates, self.entry_limit, historical)

        if sum(len(candidates.hits) for candidates in memory_candidates) > self.memory_candidate_limit:
            raise PreparedContextInvariantError("memory-candidate-limit")
        if len(topic_memory_hits) > self.topic_memory_candidate_limit:
            raise PreparedContextInvariantError("topic-memory-candidate-limit")
        if sum(len(candidates.hits) for candidates in experience_candidates) > self.experience_candidate_limit:
            raise PreparedContextInvariantError("experience-candidate-limit")

        if request.evidence_selection:
            return self._build_evidence_selection(
                request=request,
                memory_candidates=memory_candidates,
            )
        content, origins, omissions = self._select_entries(
            request=request,
            current_scope_id=current_scope_id,
            memory_candidates=memory_candidates,
            experience_candidates=experience_candidates,
            profile_candidates=profile_candidates,
            topic_memory_hits=topic_memory_hits,
        )
        if content is None:
            return PreparedContextBuild(context=self.empty(), origins=(), omissions=omissions)
        content_bytes = len(content.encode("utf-8"))
        # The assembly path is bounded by construction (every fitted item is measured against
        # `max_bytes`), so the ceiling check belongs to the non-assembly renderer only.
        if request.assembly is None and content_bytes > request.max_bytes:
            raise PreparedContextInvariantError("output-budget")
        return PreparedContextBuild(
            context=PreparedContext(status="ready", content=content, content_bytes=content_bytes),
            origins=origins,
            omissions=omissions,
        )

    def probe_budget(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates] = (),
        topic_memory_hits: Sequence[TopicMemorySearchHit] = (),
        experience_candidates: Sequence[PreparedExperienceCandidates] = (),
        profile_candidates: Sequence[PreparedProfileCandidate] = (),
    ) -> RecallBudgetView:
        """Report what the byte budget does to one candidate set, without delivering it.

        This is the RFC 1560 *budget probe*: one pass of this Builder's own pure selection and
        rendering code, keeping only the counters and discarding the rendered output. It lets
        the gate tell budget-limited thinness from recall-limited thinness.

        Pure and side-effect free: no I/O, no persistence, nothing reported to any sink, and
        the same inputs as :meth:`build_scopes_result` are guaranteed to yield the same
        counters because both go through :meth:`_select_entries`.
        """

        content, origins, omissions = self._select_entries(
            request=request,
            current_scope_id=current_scope_id,
            memory_candidates=memory_candidates,
            experience_candidates=experience_candidates,
            profile_candidates=profile_candidates,
            topic_memory_hits=topic_memory_hits,
        )
        content_bytes = 0 if content is None else len(content.encode("utf-8"))
        return RecallBudgetView(
            max_bytes=request.max_bytes,
            delivered_items=len(origins),
            truncated_items=omissions.truncated_items,
            dropped_items=omissions.dropped_items,
            unused_bytes=max(0, request.max_bytes - content_bytes),
        )

    def _build_evidence_selection(
        self,
        *,
        request: PrepareContextRequest,
        memory_candidates: Sequence[PreparedMemoryCandidates],
    ) -> PreparedContextBuild:
        """Select exact Memory slices for the query inside the complete output budget."""

        sources = _evidence_sources(memory_candidates)
        if not sources:
            return PreparedContextBuild(context=self.empty(), origins=(), omissions=PreparedContextOmissions())
        grouped = evidence_candidates(request.query, tuple(source.text for source in sources))
        flat: list[tuple[float, float, int, int, int]] = []
        for index, source in enumerate(sources):
            for candidate in grouped[index]:
                if candidate.score <= 0 or candidate.start >= candidate.end:
                    continue
                flat.append((candidate.score, source.retrieval_score, index, candidate.start, candidate.end))
        if not flat:
            return PreparedContextBuild(context=self.empty(), origins=(), omissions=PreparedContextOmissions())

        by_score = sorted(flat, key=lambda item: (-item[0], -item[1], item[2], item[3]))
        by_density = sorted(
            flat,
            key=lambda item: (-item[0] / max(1, item[4] - item[3]), -item[1], item[2], item[3]),
        )
        best_choice: list[list[tuple[int, int, float]]] | None = None
        best_key = (-1.0, -1)
        for order in (by_score, by_density):
            choice: list[list[tuple[int, int, float]]] = [[] for _ in sources]
            for score, _retrieval, index, start, end in order:
                trial = _integrate_span(choice, index, start, end, score)
                if trial is None:
                    continue
                fragments = _materialize_evidence(sources, trial)
                if fragments and _evidence_utf8_len(fragments) <= request.max_bytes:
                    choice = trial
            value = sum(score for spans in choice for _start, _end, score in spans)
            covered = sum(end - start for spans in choice for start, end, _score in spans)
            key = (value, covered)
            if key > best_key:
                best_key = key
                best_choice = choice

        choice = best_choice or [[] for _ in sources]
        fragments = _materialize_evidence(sources, choice)
        fragments = _shrink_evidence(sources, choice, fragments, request.max_bytes)
        if not fragments:
            fragments = _prefix_evidence(sources, flat, request.max_bytes)
        if not fragments:
            dropped = len(flat)
            return PreparedContextBuild(
                context=self.empty(),
                origins=(),
                omissions=PreparedContextOmissions(
                    dropped_items=dropped,
                    dropped_no_fitting_truncation=dropped,
                ),
            )
        content = _render_evidence(fragments)
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > request.max_bytes:
            raise PreparedContextInvariantError("output-budget")
        kept = sum(len(spans) for spans in choice) if fragments else 0
        dropped = max(0, len(flat) - kept)
        return PreparedContextBuild(
            context=PreparedContext(status="ready", content=content, content_bytes=content_bytes),
            origins=tuple(fragment.address for fragment in fragments),
            omissions=PreparedContextOmissions(
                dropped_items=dropped,
                dropped_no_fitting_truncation=dropped,
            ),
            fragments=tuple(fragments),
        )

    def _select_entries(
        self,
        *,
        request: PrepareContextRequest,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        topic_memory_hits: Sequence[TopicMemorySearchHit],
        experience_candidates: Sequence[PreparedExperienceCandidates],
        profile_candidates: Sequence[PreparedProfileCandidate],
    ) -> tuple[str | None, tuple[PreparedContextOrigin, ...], PreparedContextOmissions]:
        """Run one complete pure selection pass and render it.

        Returns ``(content, origins, omissions)``; ``content`` is ``None`` exactly when nothing
        was selected, which is the empty-result case for both paths. This is the single
        selection path shared by :meth:`build_scopes_result` and :meth:`probe_budget`, so the
        probe's counters always describe the selection the build would actually perform.
        """

        if request.assembly is not None:
            return self._select_text(
                request,
                current_scope_id=current_scope_id,
                memory_candidates=memory_candidates,
                experience_candidates=experience_candidates,
                profile_candidates=profile_candidates,
                topic_memory_hits=topic_memory_hits,
            )

        memory_entries = _interleave_groups(
            tuple(
                self._memory_entries(
                    candidates.memory_ref,
                    candidates.hits,
                    scope_id=None if candidates.scope_id == current_scope_id else candidates.scope_id or None,
                )
                for candidates in memory_candidates
            )
        )
        topic_memory_entries = self._topic_memory_entries(topic_memory_hits)
        experience_entries = _interleave_groups(
            tuple(
                self._experience_entries(
                    candidates.hits,
                    scope_id=None if candidates.scope_id == current_scope_id else candidates.scope_id or None,
                )
                for candidates in experience_candidates
            )
        )[: self.experience_entry_limit]
        entries, omissions = self._fit_entries(request, memory_entries, topic_memory_entries, experience_entries)
        if not entries:
            return None, (), omissions
        return _render(entries), tuple(entry.origin for entry in entries), omissions

    def _select_text(
        self,
        request: PrepareContextRequest,
        *,
        current_scope_id: str | None,
        memory_candidates: Sequence[PreparedMemoryCandidates],
        experience_candidates: Sequence[PreparedExperienceCandidates],
        profile_candidates: Sequence[PreparedProfileCandidate],
        topic_memory_hits: Sequence[TopicMemorySearchHit],
    ) -> tuple[str | None, tuple[PreparedContextOrigin, ...], PreparedContextOmissions]:
        assembly = request.assembly
        if assembly is None:
            raise PreparedContextInvariantError("text-assembly-missing")
        included: list[ContextTextItem] = []
        origins: list[PreparedContextOrigin] = []
        truncated_items = 0
        dropped_below_min_bytes = 0
        dropped_no_fitting_truncation = 0
        for section in assembly.sections:
            # Unfilled earlier sections leave their capacity available to later ones.
            limit = (
                section.limit
                if self._text_entry_limit is None
                else min(section.limit, self._text_entry_limit - len(included))
            )
            if section.family == "profile":
                entries = self._profile_entries(profile_candidates)
            elif section.family == "topic-memory":
                entries = self._topic_memory_entries(topic_memory_hits, scope_id=current_scope_id)
            else:
                groups = (
                    tuple(
                        tuple(
                            self._memory_entries(group.memory_ref, (hit,), scope_id=group.scope_id)
                            for hit in group.hits
                        )
                        for group in memory_candidates
                    )
                    if section.family == "memory"
                    else tuple(
                        tuple(self._experience_entries((hit,), scope_id=group.scope_id) for hit in group.hits)
                        for group in experience_candidates
                    )
                )
                entries = tuple(chain.from_iterable(_interleave_groups(groups)))
            seen: set[tuple[str, str, str, int, str | None, str | None]] = set()
            rank = 0
            selected_count = 0
            for entry in entries:
                if selected_count >= limit:
                    break
                item = _text_item(entry)
                artifact = item.artifact
                identity = (
                    artifact.scope_id,
                    artifact.artifact.family,
                    artifact.artifact.artifact_id,
                    artifact.artifact.revision,
                    item.entry_id,
                    item.entry_version_id,
                )
                if identity in seen or not item.content.strip():
                    continue
                seen.add(identity)
                rank += 1
                outcome = fit_context_text_item(included, replace(item, recall_rank=rank), assembly, request.max_bytes)
                if outcome.item is None:
                    dropped_below_min_bytes += int(outcome.dropped_below_min_bytes)
                    dropped_no_fitting_truncation += int(outcome.dropped_no_fitting_truncation)
                    continue
                fitted = outcome.item
                included.append(fitted)
                origins.append(entry.origin)
                selected_count += 1
                truncated_items += int(fitted.truncated)
        omissions = PreparedContextOmissions(
            truncated_items=truncated_items,
            dropped_items=dropped_below_min_bytes + dropped_no_fitting_truncation,
            dropped_below_min_bytes=dropped_below_min_bytes,
            dropped_no_fitting_truncation=dropped_no_fitting_truncation,
        )
        if not included:
            return None, (), omissions
        content = render_context_text(included, assembly)
        return content, tuple(origins), omissions

    def _profile_entries(self, candidates: Sequence[PreparedProfileCandidate]) -> tuple[_PreparedContextEntry, ...]:
        entries = []
        for candidate in candidates:
            origin = ArtifactAddress(scope_id=candidate.scope_id, artifact=candidate.profile.as_ref())
            entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="profile",
                    citation={"artifact": origin.model_dump(mode="json")},
                    content=candidate.profile.content.content,
                    truncated=False,
                )
            )
        return tuple(entries)

    def _memory_entries(
        self,
        memory_ref: ArtifactRef | None,
        hits: Sequence[MemoryHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        if hits and memory_ref is None:
            raise PreparedContextInvariantError("memory-ref-missing")
        memory_entries: list[_PreparedContextEntry] = []
        seen: set[tuple[str, str]] = set()
        for hit in hits:
            if hit.memory_ref != memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")

            citation_key = (hit.entry_id, hit.entry_version_id)
            if citation_key in seen:
                continue
            seen.add(citation_key)
            if not hit.entry_id.strip() or not hit.entry_version_id.strip() or not hit.text.strip():
                continue
            citation = MemoryCitation(
                memory_ref=hit.memory_ref,
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            origin: PreparedContextOrigin = citation
            rendered_citation = citation.model_dump(mode="json")
            if scope_id is not None:
                memory = ArtifactAddress(scope_id=scope_id, artifact=hit.memory_ref)
                origin = MemoryEntryAddress(
                    memory=memory,
                    entry_id=hit.entry_id,
                    entry_version_id=hit.entry_version_id,
                )
                rendered_citation = {
                    "memory": memory.model_dump(mode="json"),
                    "entry_id": hit.entry_id,
                    "entry_version_id": hit.entry_version_id,
                }
            memory_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="memory",
                    citation=rendered_citation,
                    content=hit.text,
                    truncated=False,
                )
            )
        return tuple(memory_entries)

    def _topic_memory_entries(
        self,
        hits: Sequence[TopicMemorySearchHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        topic_entries: list[_PreparedContextEntry] = []
        seen_topics: set[tuple[str, int]] = set()
        for hit in hits:
            if hit.artifact_ref.family != TopicMemory.family:
                raise PreparedContextInvariantError("topic-memory-family-mismatch")
            identity = (hit.artifact_ref.artifact_id, hit.artifact_ref.revision)
            if identity in seen_topics:
                continue
            seen_topics.add(identity)
            if len(topic_entries) >= self.topic_memory_entry_limit:
                break
            content = {"title": hit.title, "summary": hit.summary}
            if hit.snippet is not None:
                content["snippet"] = hit.snippet
            origin: PreparedContextOrigin = hit.artifact_ref
            citation = {"artifact_ref": hit.artifact_ref.model_dump(mode="json")}
            body = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            if scope_id is not None:
                origin = ArtifactAddress(scope_id=scope_id, artifact=hit.artifact_ref)
                citation = {"artifact": origin.model_dump(mode="json")}
                body = "\n\n".join(f"{label.capitalize()}: {value}" for label, value in content.items())
            topic_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="topic-memory",
                    citation=citation,
                    content=body,
                    truncated=False,
                )
            )
        return tuple(topic_entries)

    def _experience_entries(
        self,
        hits: Sequence[ExperienceSearchHit],
        *,
        scope_id: str | None = None,
    ) -> tuple[_PreparedContextEntry, ...]:
        experience_entries: list[_PreparedContextEntry] = []
        seen_experiences: set[tuple[str, int]] = set()
        for hit in hits:
            if hit.artifact_ref.family != Experience.family:
                raise PreparedContextInvariantError("experience-family-mismatch")
            identity = (hit.artifact_ref.artifact_id, hit.artifact_ref.revision)
            if identity in seen_experiences:
                continue
            seen_experiences.add(identity)
            origin: PreparedContextOrigin = hit.artifact_ref
            rendered_citation: dict[str, object] = {"artifact_ref": hit.artifact_ref.model_dump(mode="json")}
            if scope_id is not None:
                origin = ArtifactAddress(scope_id=scope_id, artifact=hit.artifact_ref)
                rendered_citation = {"artifact": origin.model_dump(mode="json")}
            experience_entries.append(
                _PreparedContextEntry(
                    origin=origin,
                    kind="experience",
                    citation=rendered_citation,
                    content=render_experience(hit.content),
                    truncated=False,
                )
            )
        return tuple(experience_entries)

    def _fit_entries(
        self,
        request: PrepareContextRequest,
        memory_entries: Sequence[_PreparedContextEntry],
        topic_memory_entries: Sequence[_PreparedContextEntry],
        experience_entries: Sequence[_PreparedContextEntry],
    ) -> tuple[tuple[_PreparedContextEntry, ...], PreparedContextOmissions]:
        entries: list[_PreparedContextEntry] = []
        truncated_items = 0
        dropped_below_min_bytes = 0
        dropped_no_fitting_truncation = 0
        for candidate in _interleave(memory_entries, topic_memory_entries, experience_entries):
            if len(entries) >= self.entry_limit:
                break
            fit = self._fit_entry(
                entries,
                origin=candidate.origin,
                kind=candidate.kind,
                citation=candidate.citation,
                text=candidate.content,
                max_bytes=request.max_bytes,
            )
            if fit.entry is None:
                dropped_below_min_bytes += int(fit.dropped_below_min_bytes)
                dropped_no_fitting_truncation += int(fit.dropped_no_fitting_truncation)
                continue
            entries.append(fit.entry)
            if fit.entry.truncated:
                truncated_items += 1
        return tuple(entries), PreparedContextOmissions(
            truncated_items=truncated_items,
            dropped_items=dropped_below_min_bytes + dropped_no_fitting_truncation,
            dropped_below_min_bytes=dropped_below_min_bytes,
            dropped_no_fitting_truncation=dropped_no_fitting_truncation,
        )

    def _fit_entry(
        self,
        entries: Sequence[_PreparedContextEntry],
        *,
        origin: PreparedContextOrigin,
        kind: str,
        citation: dict[str, object],
        text: str,
        max_bytes: int,
    ) -> _EntryFit:
        """Fit one entry into the remaining budget, reporting why it was dropped if it was.

        The two ``None`` paths of the original code are kept apart: ``below-min-bytes`` means a
        shorter rendering exists and fits but is too short to deliver honestly, while
        ``no-fitting-truncation`` means no shortened rendering fitted at all.
        """

        source_bytes = len(text.encode("utf-8"))
        entry_budget = min(source_bytes, self.max_entry_content_bytes)
        candidate = _PreparedContextEntry(
            origin=origin,
            kind=kind,
            citation=citation,
            content=text if source_bytes <= entry_budget else _truncate_utf8(text, entry_budget),
            truncated=source_bytes > entry_budget,
        )
        if _rendered_bytes((*entries, candidate)) <= max_bytes:
            return _EntryFit(entry=candidate)
        if source_bytes < _MIN_TRUNCATED_CONTENT_BYTES:
            return _EntryFit(dropped_below_min_bytes=True)

        lower = _MIN_TRUNCATED_CONTENT_BYTES
        upper = min(entry_budget, source_bytes - 1)
        best: _PreparedContextEntry | None = None
        while lower <= upper:
            byte_budget = (lower + upper) // 2
            candidate = _PreparedContextEntry(
                origin=origin,
                kind=kind,
                citation=citation,
                content=_truncate_utf8(text, byte_budget),
                truncated=True,
            )
            if len(candidate.content.encode("utf-8")) < _MIN_TRUNCATED_CONTENT_BYTES:
                lower = byte_budget + 1
                continue
            if _rendered_bytes((*entries, candidate)) <= max_bytes:
                best = candidate
                lower = byte_budget + 1
            else:
                upper = byte_budget - 1
        if best is None:
            return _EntryFit(dropped_no_fitting_truncation=True)
        return _EntryFit(entry=best)


def _interleave(
    memory: Sequence[_PreparedContextEntry],
    topics: Sequence[_PreparedContextEntry],
    experiences: Sequence[_PreparedContextEntry],
) -> tuple[_PreparedContextEntry, ...]:
    """Preserve each family's rank while alternating Memory, Topic, Experience."""

    ordered: list[_PreparedContextEntry] = []
    for index in range(max(len(memory), len(topics), len(experiences))):
        if index < len(memory):
            ordered.append(memory[index])
        if index < len(topics):
            ordered.append(topics[index])
        if index < len(experiences):
            ordered.append(experiences[index])
    return tuple(ordered)


def _interleave_groups(groups: Sequence[Sequence[_Item]]) -> tuple[_Item, ...]:
    ordered: list[_Item] = []
    for index in range(max((len(group) for group in groups), default=0)):
        ordered.extend(group[index] for group in groups if index < len(group))
    return tuple(ordered)


def _render(entries: Sequence[_PreparedContextEntry]) -> str:
    envelope = {
        "trust": "untrusted_history",
        "items": [_render_entry(entry) for entry in entries],
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


def _text_item(entry: _PreparedContextEntry) -> ContextTextItem:
    origin = entry.origin
    if isinstance(origin, MemoryEntryAddress):
        return ContextTextItem(
            artifact=origin.memory,
            content=entry.content,
            recall_rank=0,
            entry_id=origin.entry_id,
            entry_version_id=origin.entry_version_id,
        )
    if isinstance(origin, ArtifactAddress):
        return ContextTextItem(artifact=origin, content=entry.content, recall_rank=0)
    raise PreparedContextInvariantError("text-scope-missing")


def _render_entry(entry: _PreparedContextEntry) -> dict[str, object]:
    rendered: dict[str, object] = {
        "citation": entry.citation,
        "content": entry.content,
        "truncated": entry.truncated,
    }
    if entry.kind != "memory":
        rendered["kind"] = entry.kind
    return rendered


def _rendered_bytes(entries: Sequence[_PreparedContextEntry]) -> int:
    return len(_render(entries).encode("utf-8"))


def _truncate_utf8(text: str, byte_budget: int) -> str:
    prefix_budget = byte_budget - len(_ELLIPSIS.encode("utf-8"))
    encoded_prefix = text.encode("utf-8")[:prefix_budget]
    return f"{encoded_prefix.decode('utf-8', errors='ignore')}{_ELLIPSIS}"


def _render_evidence(fragments: Sequence[MemoryFragment]) -> str:
    items = []
    for fragment in fragments:
        address = fragment.address
        items.append(
            {
                "citation": {
                    "scope_id": address.memory.scope_id,
                    "memory_ref": address.memory.artifact.model_dump(mode="json"),
                    "entry_id": address.entry_id,
                    "entry_version_id": address.entry_version_id,
                },
                "content": fragment.text,
                "start": fragment.start,
                "end": fragment.end,
            }
        )
    envelope = {"trust": "untrusted_history", "items": items}
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


_SENTENCE_RE = re.compile(r"[^\n.!?。！？]+(?:[.!?。！？]+|$)")
_QUERY_TOKEN_RE = re.compile(r"[a-z]+|\d+|[\u4e00-\u9fff]")
_QUERY_STOP = frozenset(
    "a an and are as at be by did do does for from had has have how in is it its of on or "
    "that the their there these this to was were what when where which who why will with".split()
)


def _query_tokens(text: str) -> tuple[str, ...]:
    return tuple(
        token
        for token in _QUERY_TOKEN_RE.findall(text.lower())
        if token not in _QUERY_STOP
    )


def _split_evidence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in _SENTENCE_RE.finditer(text):
        start, end = match.span()
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start >= end:
            continue
        # Keep long sentences addressable in bounded pieces.
        while end - start > 400:
            cut = text.rfind(" ", start + 240, start + 400)
            if cut <= start:
                cut = start + 400
            spans.append((start, cut))
            start = cut
            while start < end and text[start].isspace():
                start += 1
        if start < end:
            spans.append((start, end))
    return spans


def _select_evidence_fragments(
    *,
    query: str,
    memory_candidates: Sequence[PreparedMemoryCandidates],
    max_bytes: int,
) -> list[MemoryFragment]:
    entries: list[tuple[MemoryEntryAddress, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for group in memory_candidates:
        memory_ref = group.memory_ref
        if memory_ref is None:
            continue
        for hit in group.hits:
            key = (group.scope_id, hit.entry_id, hit.entry_version_id)
            if key in seen or not hit.text.strip():
                continue
            seen.add(key)
            address = MemoryEntryAddress(
                memory=ArtifactAddress(scope_id=group.scope_id, artifact=memory_ref),
                entry_id=hit.entry_id,
                entry_version_id=hit.entry_version_id,
            )
            entries.append((address, hit.text))
    if not entries:
        return []

    query_terms = _query_tokens(query)
    term_set = set(query_terms)
    document_frequency: dict[str, int] = {}
    for _address, text in entries:
        present = set(_query_tokens(text))
        for term in term_set & present:
            document_frequency[term] = document_frequency.get(term, 0) + 1
    weights = {
        term: 1.0 + math.log(1.0 + (len(entries) + 1.0) / (document_frequency.get(term, 0) + 1.0))
        for term in term_set
    }

    spans: list[tuple[float, int, int, MemoryEntryAddress, int, int]] = []
    order = 0
    doc_strength: list[float] = []
    for doc_index, (address, text) in enumerate(entries):
        text_lower = text.lower()
        doc_spans: list[tuple[float, int, int]] = []
        rank_bonus = 0.08 * (1.0 - doc_index / max(len(entries), 1))
        doc_tokens = set(_query_tokens(text))
        doc_coverage = len(term_set & doc_tokens) / max(len(term_set), 1)
        for start, end in _split_evidence_spans(text):
            content = text[start:end]
            tokens = _query_tokens(content)
            present = term_set.intersection(tokens)
            score = sum(weights[term] for term in present) + 0.15 * len(present) + rank_bonus
            if " " + query.strip().lower() + " " in f" {text_lower} ":
                score += 1.5
            doc_spans.append((score, start, end))
            if score > 0:
                spans.append((score, order, doc_index, address, start, end))
                order += 1
        doc_spans.sort(reverse=True)
        strength = sum(item[0] for item in doc_spans[:3])
        doc_strength.append(strength)
        # A complete document can cover evidence that straddles sentence boundaries.
        if max_bytes >= 1600 and strength > 0 and len(text.encode("utf-8")) <= max_bytes - 300:
            spans.append((strength * 2.0, order, doc_index, address, 0, len(text)))
            order += 1
        # Adjacent high-scoring sentences are often one logical evidence span.
        for (left_score, left_start, left_end), (right_score, right_start, right_end) in zip(
            doc_spans, doc_spans[1:]
        ):
            if abs(left_start - right_end) <= 2 or abs(right_start - left_end) <= 2:
                start = min(left_start, right_start)
                end = max(left_end, right_end)
                spans.append((left_score + right_score * 0.8, order, doc_index, address, start, end))
                order += 1

    spans.sort(key=lambda item: (-item[0], item[1]))
    selected: list[tuple[float, MemoryEntryAddress, int, int]] = []
    for score, _order, _doc, address, start, end in spans:
        if max_bytes <= 0:
            break
        if any(
            existing_address == address and existing_start <= start and end <= existing_end
            for _score, existing_address, existing_start, existing_end in selected
        ):
            continue
        text = next(text for item_address, text in entries if item_address == address)
        fragment = MemoryFragment(address=address, start=start, end=end, text=text[start:end])
        selected_fragments = [
            MemoryFragment(a, s, e, next(text for item_address, text in entries if item_address == a)[s:e])
            for _sc, a, s, e in selected
        ]
        rendered = _render_evidence([*selected_fragments, fragment])
        if len(rendered.encode("utf-8")) > max_bytes:
            fitted_end = _fit_evidence_end(
                selected,
                entries,
                address,
                start,
                end,
                max_bytes,
            )
            if fitted_end is None:
                continue
            fragment = MemoryFragment(address=address, start=start, end=fitted_end, text=text[start:fitted_end])
        selected.append((score, address, fragment.start, fragment.end))

    # Merge adjacent or overlapping pieces in the same entry to avoid duplicate citations.
    merged: list[tuple[float, MemoryEntryAddress, int, int]] = []
    for score, address, start, end in sorted(
        selected,
        key=lambda item: (
            item[1].memory.scope_id,
            item[1].entry_id,
            item[1].entry_version_id,
            item[2],
            item[3],
        ),
    ):
        if merged and merged[-1][1] == address and start <= merged[-1][3] + 80:
            old_score, old_address, old_start, old_end = merged[-1]
            merged[-1] = (max(old_score, score), old_address, old_start, max(old_end, end))
        else:
            merged.append((score, address, start, end))

    # Try to complete an evidence sentence span with its immediate neighbours;
    # SciFact-style rationales often span two adjacent sentences.
    span_cache: dict[tuple[str, str, str], list[tuple[int, int]]] = {}
    merged.sort(key=lambda item: (-item[0], item[1].entry_id, item[2]))
    for position, (score, address, start, end) in enumerate(tuple(merged)):
        item_text = next(t for ia, t in entries if ia == address)
        key = (address.memory.scope_id, address.entry_id, address.entry_version_id)
        spans = span_cache.setdefault(key, _split_evidence_spans(item_text))
        index = next((i for i, (s, e) in enumerate(spans) if s <= start and end <= e), None)
        if index is None:
            continue
        for neighbour_index in (index - 1, index + 1, index - 2, index + 2):
            if not 0 <= neighbour_index < len(spans):
                continue
            ns, ne = spans[neighbour_index]
            candidate_start = min(start, ns)
            candidate_end = max(end, ne)
            trial = list(merged)
            trial[position] = (score, address, candidate_start, candidate_end)
            trial_fragments = []
            for _sc, addr, s, e in trial:
                text_item = next(t for ia, t in entries if ia == addr)
                trial_fragments.append(MemoryFragment(address=addr, start=s, end=e, text=text_item[s:e]))
            if len(_render_evidence(trial_fragments).encode("utf-8")) <= max_bytes:
                merged = trial
                start, end = candidate_start, candidate_end
    merged.sort(key=lambda item: (item[1].memory.scope_id, item[1].entry_id, item[2], item[3]))
    fragments: list[MemoryFragment] = []
    for _score, address, start, end in merged:
        item_text = next(t for ia, t in entries if ia == address)
        fragments.append(MemoryFragment(address=address, start=start, end=end, text=item_text[start:end]))
    content = _render_evidence(fragments)
    while fragments and len(content.encode("utf-8")) > max_bytes:
        fragments.pop()
        content = _render_evidence(fragments)
    return fragments


def _fit_evidence_end(
    selected: Sequence[tuple[float, MemoryEntryAddress, int, int]],
    entries: Sequence[tuple[MemoryEntryAddress, str]],
    address: MemoryEntryAddress,
    start: int,
    end: int,
    max_bytes: int,
) -> int | None:
    current_text = next(t for ia, t in entries if ia == address)
    low, high = start + 1, end
    best: int | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = MemoryFragment(address=address, start=start, end=middle, text=current_text[start:middle])
        selected_fragments = [
            MemoryFragment(a, s, e, next(t for ia, t in entries if ia == a)[s:e])
            for _sc, a, s, e in selected
        ]
        rendered = _render_evidence([*selected_fragments, candidate])
        if len(rendered.encode("utf-8")) <= max_bytes:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


@dataclass(frozen=True)
class _EvidenceSource:
    address: MemoryEntryAddress
    text: str
    retrieval_score: float


def _evidence_sources(memory_candidates: Sequence[PreparedMemoryCandidates]) -> tuple[_EvidenceSource, ...]:
    sources: list[_EvidenceSource] = []
    for candidates in memory_candidates:
        if candidates.hits and candidates.memory_ref is None:
            raise PreparedContextInvariantError("memory-ref-missing")
        seen: set[tuple[str, str]] = set()
        for hit in candidates.hits:
            if hit.memory_ref != candidates.memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")
            identity = (hit.entry_id, hit.entry_version_id)
            if identity in seen:
                continue
            seen.add(identity)
            if not hit.entry_id.strip() or not hit.entry_version_id.strip() or not hit.text.strip():
                continue
            sources.append(
                _EvidenceSource(
                    address=MemoryEntryAddress(
                        memory=ArtifactAddress(scope_id=candidates.scope_id, artifact=hit.memory_ref),
                        entry_id=hit.entry_id,
                        entry_version_id=hit.entry_version_id,
                    ),
                    text=hit.text,
                    retrieval_score=float(hit.score),
                )
            )
    return tuple(sources)


def _integrate_span(
    choice: Sequence[Sequence[tuple[int, int, float]]],
    index: int,
    start: int,
    end: int,
    score: float,
) -> list[list[tuple[int, int, float]]] | None:
    current = list(choice[index])
    covered: list[int] = []
    for slot, (old_start, old_end, _score) in enumerate(current):
        if old_start <= start and end <= old_end:
            return None
        if start <= old_start and old_end <= end:
            covered.append(slot)
    trial = [list(spans) for spans in choice]
    trial[index] = [span for slot, span in enumerate(current) if slot not in covered]
    trial[index].append((start, end, score))
    return trial


def _coalesce_spans(text: str, spans: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    ordered = sorted(spans)
    merged: list[tuple[int, int]] = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end or not text[prev_end:start].strip():
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    extended: list[tuple[int, int]] = []
    for slot, (start, end) in enumerate(merged):
        limit = merged[slot + 1][0] if slot + 1 < len(merged) else len(text)
        while end < limit and text[end].isspace():
            end += 1
        if start < end:
            extended.append((int(start), int(end)))
    return extended


def _fragments_for(
    sources: Sequence[_EvidenceSource],
    spans_per_source: Sequence[Sequence[tuple[int, int]]],
) -> list[MemoryFragment]:
    fragments: list[MemoryFragment] = []
    for source, spans in zip(sources, spans_per_source, strict=True):
        for start, end in spans:
            if not (0 <= start < end <= len(source.text)):
                continue
            snippet = source.text[start:end]
            if not snippet.strip():
                continue
            fragments.append(
                MemoryFragment(
                    address=source.address,
                    start=int(start),
                    end=int(end),
                    text=snippet,
                )
            )
    return fragments


def _geometry(
    sources: Sequence[_EvidenceSource],
    choice: Sequence[Sequence[tuple[int, int, float]]],
) -> list[list[tuple[int, int]]]:
    spans_per_source = [
        _coalesce_spans(source.text, [(start, end) for start, end, _score in spans])
        for source, spans in zip(sources, choice, strict=True)
    ]
    return _bridge_spans(sources, spans_per_source)


def _bridge_spans(
    sources: Sequence[_EvidenceSource],
    spans_per_source: Sequence[Sequence[tuple[int, int]]],
) -> list[list[tuple[int, int]]]:
    """Join nearby slices when one citation is cheaper than the text between them."""

    current = [list(spans) for spans in spans_per_source]
    while True:
        base = _evidence_utf8_len(_fragments_for(sources, current))
        best: list[list[tuple[int, int]]] | None = None
        best_len = base
        for index, spans in enumerate(current):
            if len(spans) < 2:
                continue
            for slot in range(len(spans) - 1):
                trial = [list(item) for item in current]
                left = trial[index]
                trial[index] = left[:slot] + [(left[slot][0], left[slot + 1][1])] + left[slot + 2 :]
                size = _evidence_utf8_len(_fragments_for(sources, trial))
                if size < best_len:
                    best_len = size
                    best = trial
        if best is None:
            return current
        current = best


def _materialize_evidence(
    sources: Sequence[_EvidenceSource],
    choice: Sequence[Sequence[tuple[int, int, float]]],
) -> list[MemoryFragment]:
    return _fragments_for(sources, _geometry(sources, choice))


def _shrink_evidence(
    sources: Sequence[_EvidenceSource],
    choice: Sequence[Sequence[tuple[int, int, float]]],
    fragments: list[MemoryFragment],
    max_bytes: int,
) -> list[MemoryFragment]:
    scored = [
        (
            sum(
                score
                for start, end, score in choice[index]
                if start < fragment.end and end > fragment.start and sources[index].address == fragment.address
            ),
            fragment,
        )
        for index, _source in enumerate(sources)
        for fragment in fragments
        if fragment.address == sources[index].address
    ]
    unique: list[tuple[float, MemoryFragment]] = []
    seen: set[tuple[str, str, str, int, int]] = set()
    for score, fragment in scored:
        key = (
            fragment.address.memory.scope_id,
            fragment.address.entry_id,
            fragment.address.entry_version_id,
            fragment.start,
            fragment.end,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append((score, fragment))
    kept = unique
    while kept and _evidence_utf8_len([fragment for _score, fragment in kept]) > max_bytes:
        drop = min(range(len(kept)), key=lambda slot: (kept[slot][0], kept[slot][1].end - kept[slot][1].start))
        del kept[drop]
    return [fragment for _score, fragment in kept]


def _prefix_evidence(
    sources: Sequence[_EvidenceSource],
    flat: Sequence[tuple[float, float, int, int, int]],
    max_bytes: int,
) -> list[MemoryFragment]:
    if not flat:
        return []
    _score, _retrieval, index, start, end = max(flat, key=lambda item: (item[0], -item[2], -item[3]))
    source = sources[index]
    lower = start + 1
    upper = end
    best_end: int | None = None
    while lower <= upper:
        middle = (lower + upper) // 2
        snippet = source.text[start:middle]
        fragment = MemoryFragment(
            address=source.address,
            start=int(start),
            end=int(middle),
            text=snippet,
        )
        if snippet.strip() and _evidence_utf8_len([fragment]) <= max_bytes:
            best_end = middle
            lower = middle + 1
        else:
            upper = middle - 1
    if best_end is None:
        return []
    return [
        MemoryFragment(
            address=source.address,
            start=int(start),
            end=int(best_end),
            text=source.text[start:best_end],
        )
    ]


def _evidence_utf8_len(fragments: Sequence[MemoryFragment]) -> int:
    if not fragments:
        return 0
    return len(_render_evidence(fragments).encode("utf-8"))
