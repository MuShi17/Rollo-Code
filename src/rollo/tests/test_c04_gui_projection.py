"""C04 bounded GUI projection contracts.

Layer L2 only: every case here runs against a real ``SQLiteRuntimeStore``.
The "no gap" and "cross session" judgements read the store's own ordinals, so
a fake store could not witness them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rollo.projections import gui_projection as gui
from rollo.projections.base import iter_event_records, source_digest
from rollo.projections.gui_projection import (
    GUI_PROJECTION_VERSION,
    GuiPageTokenError,
    GuiProjection,
)
from rollo.projections.session_projection import SessionProjection
from rollo.runtime_event import RuntimeEvent
from rollo.runtime_store import SQLiteRuntimeStore

SESSION = "session-c04"
OTHER_SESSION = "session-c04-other"


def _body(event: RuntimeEvent) -> str:
    return gui.event_body(event)


def _size(value: str) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _open_event(session_id: str = SESSION) -> RuntimeEvent:
    """The store requires an invocation to open before any other event."""

    return RuntimeEvent.from_dict(
        {
            "schema_version": 2,
            "id": f"open-{session_id}",
            "session_id": session_id,
            "run_id": f"run-{session_id}",
            "invocation_id": f"inv-{session_id}",
            "turn_id": "turn-c04",
            "ts": 0,
            "partial": False,
            "role": "system",
            "author": "agent",
            "content": {
                "kind": "invocation_opened",
                "protocol": "invocation_opened_v1",
                "route": {"provider": "fixture", "model": "fixture-model"},
                "configuration": {"attempt": 1},
                "root": {"kind": "agent"},
                "source": {"kind": "fresh"},
            },
        }
    )


def _text_event(index: int, text: str, *, session_id: str = SESSION) -> RuntimeEvent:
    return RuntimeEvent.from_dict(
        {
            "schema_version": 2,
            "id": f"{session_id}-msg-{index}",
            "session_id": session_id,
            "run_id": f"run-{session_id}",
            "invocation_id": f"inv-{session_id}",
            "turn_id": "turn-c04",
            "ts": index,
            "partial": False,
            "role": "model",
            "author": "agent",
            "content": {"kind": "text", "text": text},
            "metadata": {"lifecycle": "model_final"},
        }
    )


def _terminal_event(*, session_id: str = SESSION) -> RuntimeEvent:
    """A terminal event seals its run, so it must be appended last."""

    return RuntimeEvent.from_dict(
        {
            "schema_version": 2,
            "id": f"terminal-{session_id}",
            "session_id": session_id,
            "run_id": f"run-{session_id}",
            "invocation_id": f"inv-{session_id}",
            "turn_id": "turn-c04",
            "ts": 9_000,
            "partial": False,
            "role": "model",
            "author": "agent",
            "status": "completed",
            "actions": {"run_terminal": {"status": "completed"}},
        }
    )


def _store(tmp_path: Path) -> SQLiteRuntimeStore:
    return SQLiteRuntimeStore(tmp_path / "runtime.sqlite")


def _prefix_digest(store: SQLiteRuntimeStore, session_id: str, high_water: int) -> str:
    records = [
        record
        for record in iter_event_records(store, high_water=high_water)
        if record.event.session_id == session_id and record.ordinal <= high_water
    ]
    return source_digest(records)


def _items(snapshot) -> list[tuple[str, int, str, int]]:
    return [
        (item.message_id, item.ordinal, item.summary, item.size)
        for item in snapshot.messages
    ]


def test_snapshot_carries_the_prefix_boundary_that_was_asked_for(tmp_path: Path):
    """The boundary is verified by content, never by the echoed field."""

    with _store(tmp_path) as store:
        store.append(_open_event())
        store.append(_text_event(1, "one"))
        store.append(_text_event(2, "two"))
        boundary = int(store.high_water(session_id=SESSION))
        store.append(_text_event(3, "three"))

        snapshot = GuiProjection().build(store, session_id=SESSION, high_water=boundary)

        # Independent recomputation at H: this is the assertion that dies if the
        # build ignores high_water and reads the newest value.
        expected = _prefix_digest(store, SESSION, boundary)
        assert snapshot.high_water == boundary
        assert snapshot.source_digest == expected
        assert expected != _prefix_digest(store, SESSION, boundary + 1)
        assert [item.message_id for item in snapshot.messages] == [
            f"{SESSION}-msg-1",
            f"{SESSION}-msg-2",
        ]
        assert [item.ordinal for item in snapshot.messages] == [2, 3]
        assert [item.summary for item in snapshot.messages] == ["one", "two"]
        assert all(item.ordinal <= boundary for item in snapshot.messages)


def test_the_two_field_lists_match_the_actual_classification(tmp_path: Path):
    """The declared lists must agree with how each field is actually derived.

    A field may not appear in both lists, and the split has to match the code:
    ``runs`` is read from the C03 control store, which has no ordinal, so it can
    only be non-prefix.  Listing it as a prefix field let one snapshot claim both
    classifications at once -- two snapshots at the same ``high_water`` with the
    same ``source_digest`` could then report different run states.
    """

    with _store(tmp_path) as store:
        store.append(_open_event())
        snapshot = GuiProjection().build(
            store,
            session_id=SESSION,
            high_water=int(store.high_water(session_id=SESSION)),
        )
        dto = snapshot.to_dict()

        prefix = list(dto["prefix_fields"])
        non_prefix = list(dto["non_prefix_fields"])

        assert prefix == ["messages", "terminals", "errors", "source_digest"]
        assert non_prefix == ["runs", "drafts", "pending_interactions"]
        assert not set(prefix) & set(non_prefix)
        assert set(prefix) | set(non_prefix) == {
            "messages",
            "terminals",
            "errors",
            "source_digest",
            "runs",
            "drafts",
            "pending_interactions",
        }
        # ``runs`` must not be claimed as prefix anywhere in the same payload.
        assert "runs" not in prefix


def test_snapshot_excludes_events_written_while_it_is_building(tmp_path: Path):
    """A commit that lands mid-build belongs to the suffix, not the prefix."""

    with _store(tmp_path) as store:
        store.append(_open_event())
        store.append(_text_event(1, "one"))
        store.append(_text_event(2, "two"))
        boundary = int(store.high_water(session_id=SESSION))
        late = _text_event(3, "late")

        class RacingStore:
            """Ticks once, inside the boundary read the projection performs."""

            def __init__(self, inner: SQLiteRuntimeStore, late_event: RuntimeEvent) -> None:
                self._inner = inner
                self._late = late_event
                self.ticked = False

            def high_water(
                self, *, session_id: str | None = None, run_id: str | None = None
            ) -> int:
                return self._inner.high_water(session_id=session_id, run_id=run_id)

            def read_event_records(self, **kwargs):
                pairs = self._inner.read_event_records(**kwargs)
                if kwargs.get("high_water") is not None and not self.ticked:
                    self.ticked = True
                    self._inner.append(self._late)
                return pairs

            def read_runtime_stream_partials(self, **kwargs):
                return self._inner.read_runtime_stream_partials(**kwargs)

            def read_event(self, event_id: str):
                return self._inner.read_event(event_id)

        racing = RacingStore(store, late)
        snapshot = GuiProjection().build(racing, session_id=SESSION, high_water=boundary)

        assert racing.ticked, "the boundary read never happened; the race was not exercised"
        assert [item.message_id for item in snapshot.messages] == [
            f"{SESSION}-msg-1",
            f"{SESSION}-msg-2",
        ]
        assert snapshot.source_digest == _prefix_digest(store, SESSION, boundary)
        assert store.high_water(session_id=SESSION) == boundary + 1


def test_snapshot_contains_only_the_requested_session(tmp_path: Path):
    """A global high-water would silently fold another session into the view."""

    with _store(tmp_path) as store:
        store.append(_open_event())
        store.append(_text_event(1, "mine-1"))
        store.append(_open_event(OTHER_SESSION))
        store.append(_text_event(1, "theirs-1", session_id=OTHER_SESSION))
        store.append(_text_event(2, "mine-2"))
        store.append(_text_event(2, "theirs-2", session_id=OTHER_SESSION))
        boundary = int(store.high_water(session_id=SESSION))
        other_boundary = int(store.high_water(session_id=OTHER_SESSION))
        # Ordinals are global, so a session-scoped boundary is strictly smaller
        # than the ledger's.  A projection that used the global high-water would
        # fold the other session in.
        assert boundary < store.high_water()
        assert other_boundary == store.high_water()

        snapshot = GuiProjection().build(store, session_id=SESSION, high_water=boundary)
        theirs = GuiProjection().build(
            store, session_id=OTHER_SESSION, high_water=other_boundary
        )

        mine_ids = {item.message_id for item in snapshot.messages}
        their_ids = {item.message_id for item in theirs.messages}
        assert mine_ids & their_ids == set()
        assert mine_ids == {f"{SESSION}-msg-1", f"{SESSION}-msg-2"}
        # Ordinals are global, so the two sessions genuinely interleave in the
        # ledger; the projection still reports each session's own ordinals.
        assert (boundary, other_boundary) == (5, 6)
        assert [item.ordinal for item in snapshot.messages] == [2, 5]
        assert [item.ordinal for item in theirs.messages] == [4, 6]
        assert max(item.ordinal for item in snapshot.messages) <= boundary
        # Union equals both sessions' whole message set.
        assert mine_ids | their_ids == {
            f"{SESSION}-msg-1",
            f"{SESSION}-msg-2",
            f"{OTHER_SESSION}-msg-1",
            f"{OTHER_SESSION}-msg-2",
        }


def test_pages_cover_the_boundary_without_repeats_or_gaps(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(gui, "SNAPSHOT_MESSAGE_PAGE_LIMIT", 3)
    with _store(tmp_path) as store:
        store.append(_open_event())
        for index in range(1, 8):
            store.append(_text_event(index, f"m{index}"))
        boundary = int(store.high_water(session_id=SESSION))
        store.append(_text_event(99, "after"))

        projection = GuiProjection()
        snapshot = projection.build(store, session_id=SESSION, high_water=boundary)
        assert len(snapshot.messages) <= gui.SNAPSHOT_MESSAGE_PAGE_LIMIT
        assert snapshot.message_page.total == 7
        assert snapshot.has_more_messages is True
        assert snapshot.message_page.next_page_token is not None
        # The token data carries the boundary verbatim.
        assert f"\t{boundary}\t" in snapshot.message_page.next_page_token

        ordinals = [item.ordinal for item in snapshot.messages]
        token = snapshot.message_page.next_page_token
        while token is not None:
            # The token carries its own boundary; the caller is not asked to
            # remember one.
            page = projection.read_page_by_token(store, token)
            assert page.high_water == boundary
            assert page.source_digest == snapshot.source_digest
            ordinals.extend(item.ordinal for item in page.messages)
            token = page.message_page.next_page_token

        # A later page of the same boundary is offset-correct.
        later = projection.read_page(
            store, session_id=SESSION, high_water=boundary, page_offset=3
        )
        assert [item.summary for item in later.messages] == ["m4", "m5", "m6"]

        # A snapshot at a different boundary must not hand out the same offset
        # token: the boundary is bound into the token.
        drift = projection.build(store, session_id=SESSION, high_water=boundary + 1)
        assert drift.message_page.next_page_token != snapshot.message_page.next_page_token

        assert ordinals == list(range(2, 9))
        assert len(set(ordinals)) == len(ordinals)


def test_large_body_is_paged_and_never_inlined(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(gui, "SUMMARY_CHAR_LIMIT", 20)
    monkeypatch.setattr(gui, "BODY_PAGE_SIZE", 100)
    text = "".join(f"{index:04d}-" for index in range(80))
    assert len(text.encode("utf-8")) > 300

    with _store(tmp_path) as store:
        store.append(_open_event())
        store.append(_text_event(1, text))
        boundary = int(store.high_water(session_id=SESSION))
        projection = GuiProjection()
        snapshot = projection.build(store, session_id=SESSION, high_water=boundary)

        ref = snapshot.messages[0]
        assert len(ref.summary) == 20
        assert ref.summary != text
        assert ref.size == _size(text)
        # The size is the canonical-JSON UTF-8 byte length, i.e. the quoted
        # encoding, not the raw character count.
        assert ref.size == len(text.encode("utf-8")) + 2
        dumped = json.dumps(snapshot.to_dict(), ensure_ascii=False)
        assert text not in dumped
        assert text[:40] not in dumped

        chunks: list[str] = []
        token: str | None = ref.body_ref
        while token is not None:
            page = projection.read_body(store, token)
            assert page.size == ref.size
            assert page.source_digest == snapshot.source_digest
            assert len(page.text) <= gui.BODY_PAGE_SIZE
            chunks.append(page.text)
            token = page.next_page_token
        assert "".join(chunks) == text
        assert len(chunks) > 1

        with pytest.raises(GuiPageTokenError):
            projection.read_body(store, ref.body_ref + "tampered")

        # The token binds the exact prefix it was minted at: corrupting the
        # carried digest must fail loudly instead of silently reading a prefix
        # that has moved on.  (Bumping high_water alone would still find the
        # same event in a *larger* prefix, so it cannot witness this.)
        forged = ref.body_ref.replace(snapshot.source_digest, "0" * 64)
        assert forged != ref.body_ref
        with pytest.raises(GuiPageTokenError):
            projection.read_body(store, forged)


def test_default_bounds_are_finite_and_enforced(tmp_path: Path):
    """The declared constants, not just the monkeypatched ones, must bite."""

    assert gui.SNAPSHOT_MESSAGE_PAGE_LIMIT == 200
    assert gui.SUMMARY_CHAR_LIMIT == 160
    assert gui.BODY_PAGE_SIZE == 4096

    total = 200 + 5
    long_text = "x" * (16 * 1024)
    with _store(tmp_path) as store:
        store.append(_open_event())
        for index in range(1, total + 1):
            store.append(_text_event(index, long_text))
        boundary = int(store.high_water(session_id=SESSION))
        projection = GuiProjection()
        snapshot = projection.build(store, session_id=SESSION, high_water=boundary)

        assert snapshot.message_page.total == total
        assert len(snapshot.messages) == 200
        assert snapshot.has_more_messages is True
        assert all(len(item.summary) == 160 for item in snapshot.messages)

        # Walking the tokens reaches every message without exceeding one page.
        seen = [item.ordinal for item in snapshot.messages]
        token = snapshot.message_page.next_page_token
        while token is not None:
            page = projection.read_page_by_token(store, token)
            assert len(page.messages) <= 200
            seen.extend(item.ordinal for item in page.messages)
            token = page.message_page.next_page_token
        assert seen == sorted(seen)
        assert len(set(seen)) == len(seen)
        assert len(seen) == total

        # A body page is capped by the declared page size as well.
        body = projection.read_body(store, snapshot.messages[0].body_ref)
        assert len(body.text) == 4096
        assert body.has_more is True


def test_no_gap_equation_between_snapshot_and_event_suffix(tmp_path: Path):
    """merge(snapshot@H, suffix (H, H2]) == project@H2, entry by entry."""

    with _store(tmp_path) as store:
        store.append(_open_event())
        for index in range(1, 4):
            store.append(_text_event(index, f"m{index}"))
        h1 = int(store.high_water(session_id=SESSION))
        projection = GuiProjection()

        # Half one: the snapshot at the boundary the subscription started from.
        merged = _items(projection.build(store, session_id=SESSION, high_water=h1))

        # A message and a terminal land while the consumer is already reading
        # the suffix.
        store.append(_text_event(4, "m4"))
        store.append(_terminal_event())
        h2 = int(store.high_water(session_id=SESSION))

        # Half two: the exact read the service performs, left-open on H.
        for ordinal, _event in store.read_event_records(
            session_id=SESSION, after_ordinal=h1
        ):
            assert ordinal > h1
            merged = _items(
                projection.build(store, session_id=SESSION, high_water=ordinal)
            )

        expected = projection.build(store, session_id=SESSION, high_water=h2)
        assert merged == _items(expected)
        assert [item[1] for item in merged] == [2, 3, 4, 5]
        assert expected.terminals[0]["ordinal"] <= h2
        assert expected.source_digest == _prefix_digest(store, SESSION, h2)


def test_projection_version_is_its_own_namespace():
    assert GUI_PROJECTION_VERSION == "gui-projection-v1"
    assert SessionProjection().projection_version == "projection-v1"
    assert GUI_PROJECTION_VERSION != SessionProjection().projection_version
