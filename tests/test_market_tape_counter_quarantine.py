"""Real-SQLite coverage for immutable cumulative-counter quarantine."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from services.market_tape.collector import MarketTapeCollector
from services.market_tape.config import MarketTapeConfig
from services.market_tape.intelligence import _bounded_keyword_rows
from services.market_tape.models import (
    MarketContent,
    MetricCounters,
    ProviderBatch,
    SourceReceipt,
    SourceState,
)
from services.market_tape.sinks.supabase import ENTITY_TABLES
from services.market_tape.store import CounterRegressionError, MarketTapeStore


def _config(tmp_path) -> MarketTapeConfig:
    return MarketTapeConfig(
        db_path=tmp_path / "market.sqlite3",
        object_dir=tmp_path / "objects",
        heartbeat_path=tmp_path / "heartbeat.json",
        lock_path=tmp_path / "market.lock",
        platforms=["youtube"],
        topics=["AI automation"],
        supabase_sync_enabled=False,
    )


def _item(
    external_id: str,
    observed_at: datetime,
    views: int,
    *,
    source_id: str = "youtube-data-api",
) -> MarketContent:
    return MarketContent(
        platform="youtube",
        external_id=external_id,
        creator_external_id=f"creator-{external_id}",
        creator_handle=f"creator_{external_id}",
        creator_followers=50,
        published_at=observed_at - timedelta(hours=2),
        observed_at=observed_at,
        source_id=source_id,
        metrics=MetricCounters.from_values(
            views=views,
            likes=max(0, views // 10),
            comments=max(0, views // 50),
            shares=max(0, views // 100),
        ),
        title="AI automation retention system",
        caption="Measured creator workflow",
        url=f"https://www.youtube.com/watch?v={external_id}",
        raw_payload={
            "id": external_id,
            "observed_at": observed_at.isoformat(),
            "viewCount": views,
        },
        discovery_context={
            "surface": "measured_external_query",
            "queries": ["AI automation retention"],
        },
    )


def _record_regression(
    store: MarketTapeStore,
    *,
    run_id: str,
    first: MarketContent,
    regressed: MarketContent,
) -> dict:
    store.start_run(run_id, "recheck")
    assert store.ingest(first, run_id)[0] is True
    with pytest.raises(CounterRegressionError):
        store.ingest(regressed, run_id)
    store.finish_run(run_id)
    with store.connect() as connection:
        return dict(connection.execute(
            """SELECT quality.flag_id, quality.observation_id,
                      observation.observation_key,
                      prior.observation_key AS prior_observation_key
               FROM mt_observation_quality_flags quality
               JOIN mt_market_observations observation
                 ON observation.observation_id = quality.observation_id
               JOIN mt_market_observations prior
                 ON prior.observation_id = quality.prior_observation_id
               WHERE observation.observation_key = ?""",
            (regressed.observation_key,),
        ).fetchone())


def _same_timestamp_pair(
    external_id: str,
    observed_at: datetime,
    *,
    high_key_precedes: bool,
) -> tuple[MarketContent, MarketContent]:
    high = _item(external_id, observed_at, 100)
    for nonce in range(1000):
        low = _item(external_id, observed_at, 0)
        low.raw_payload["identity_nonce"] = nonce
        if (high.observation_key < low.observation_key) is high_key_precedes:
            return high, low
    raise AssertionError("could not construct the required observation-key order")


def test_live_regression_is_retained_rejected_and_excluded_everywhere(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    first_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    first = _item("counter-live", first_at, 100)

    store.start_run("counter-seed", "integration")
    assert store.ingest(first, "counter-seed") == (True, True)
    assert store.aggregate_trends(
        observed_at=first_at,
        run_id="counter-seed",
    ) > 0
    store.finish_run("counter-seed")
    with store.connect() as connection:
        derived_before = {
            "genome": connection.execute(
                "SELECT updated_at FROM mt_content_genomes WHERE video_id = ?",
                (first.video_id,),
            ).fetchone()[0],
            "memberships": connection.execute(
                "SELECT COUNT(*) FROM mt_trend_memberships WHERE video_id = ?",
                (first.video_id,),
            ).fetchone()[0],
            "poll": dict(connection.execute(
                "SELECT * FROM mt_poll_queue WHERE video_id = ?",
                (first.video_id,),
            ).fetchone()),
            "trend_ids": [
                row[0] for row in connection.execute(
                    "SELECT trend_id FROM mt_trend_memberships WHERE video_id = ?",
                    (first.video_id,),
                ).fetchall()
            ],
        }

    bad = _item("counter-live", first_at + timedelta(minutes=5), 0)
    store.start_run("counter-bad", "recheck")
    with pytest.raises(CounterRegressionError) as caught:
        store.ingest(bad, "counter-bad")
    assert caught.value.views == 0
    assert caught.value.prior_views == 100
    with pytest.raises(CounterRegressionError):
        store.ingest(bad, "counter-bad")
    assert store.aggregate_trends(
        observed_at=bad.observed_at,
        run_id="counter-bad",
    ) == 0
    store.finish_run("counter-bad")

    with store.connect() as connection:
        observations = connection.execute(
            """SELECT observation_id, views, source_confidence
               FROM mt_market_observations WHERE video_id = ?
               ORDER BY observed_at, observation_id""",
            (first.video_id,),
        ).fetchall()
        flags = connection.execute(
            "SELECT * FROM mt_observation_quality_flags WHERE video_id = ?",
            (first.video_id,),
        ).fetchall()
        poll_after = dict(connection.execute(
            "SELECT * FROM mt_poll_queue WHERE video_id = ?",
            (first.video_id,),
        ).fetchone())
        genome_after = connection.execute(
            "SELECT updated_at FROM mt_content_genomes WHERE video_id = ?",
            (first.video_id,),
        ).fetchone()[0]
        memberships_after = connection.execute(
            "SELECT COUNT(*) FROM mt_trend_memberships WHERE video_id = ?",
            (first.video_id,),
        ).fetchone()[0]
    assert [(row["views"], row["source_confidence"]) for row in observations] == [
        (100, 1.0),
        (0, 0.0),
    ]
    assert len(flags) == 1
    assert flags[0]["error_code"] == "counter_regression"
    assert json.loads(flags[0]["metadata_json"])["raw_observation_retained"] is True
    assert genome_after == derived_before["genome"]
    assert memberships_after == derived_before["memberships"]
    assert poll_after["last_observed_at"] == derived_before["poll"]["last_observed_at"]

    assert store.list_videos(1)[0]["views"] == 100
    bounded = _bounded_keyword_rows(store, window_hours=24, row_limit=10)
    assert bounded[0]["views"] == 100
    assert bounded[0]["observation_count"] == 1
    details = store._opportunity_content_details(
        derived_before["trend_ids"],
        examples_per_trend=3,
    )
    assert all(
        example["views"] == 100
        for detail in details.values()
        for example in detail["examples"]
    )
    assert all(candle["new_views"] >= 0 for candle in store.social_candles(
        window_minutes=15,
        limit=4,
    ))
    status = store.status()["totals"]
    assert status["observations"] == 2
    assert status["observation_quality_flags"] == 1
    assert status["analytics_eligible_observations"] == 1

    queued = store.enqueue_run_for_sync("counter-bad")
    assert queued > 0
    with store.connect() as connection:
        quality_outbox = connection.execute(
            """SELECT payload_json FROM mt_sync_outbox
               WHERE entity_type = 'observation_quality_flag'"""
        ).fetchone()
    payload = json.loads(quality_outbox[0])
    assert payload["observation_key"] == bad.observation_key
    assert payload["prior_observation_key"] == first.observation_key

    recovery = _item("counter-live", first_at + timedelta(minutes=10), 130)
    store.start_run("counter-recovery", "recheck")
    assert store.ingest(recovery, "counter-recovery") == (True, False)
    store.finish_run("counter-recovery")
    with store.connect() as connection:
        recovered = connection.execute(
            """SELECT views, view_velocity FROM mt_market_observations
               WHERE observation_key = ?""",
            (recovery.observation_key,),
        ).fetchone()
    assert recovered["views"] == 130
    assert recovered["view_velocity"] > 0


def test_schema_backfill_uses_running_max_and_is_idempotent(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    observed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    first = _item("counter-migration", observed_at, 100)
    store.start_run("legacy-counter-run", "archive_bootstrap")
    store.ingest(first, "legacy-counter-run")

    with store.connect() as connection:
        base = dict(connection.execute(
            "SELECT * FROM mt_market_observations WHERE observation_key = ?",
            (first.observation_key,),
        ).fetchone())
        base.pop("observation_id")
        columns = list(base)
        marks = ",".join("?" for _ in columns)
        for index, views in enumerate((0, 50, 100, 120), start=1):
            row = dict(base)
            row["observation_key"] = f"legacy-observation-{index}"
            row["observed_at"] = (
                observed_at + timedelta(minutes=index)
            ).isoformat()
            row["wall_clock_date"] = (
                observed_at + timedelta(minutes=index)
            ).date().isoformat()
            row["views"] = views
            row["source_confidence"] = 1.0
            connection.execute(
                f"INSERT INTO mt_market_observations({','.join(columns)}) VALUES({marks})",
                [row[column] for column in columns],
            )
        connection.execute(
            "DELETE FROM mt_meta WHERE key = 'counter_regression_backfill_v1'"
        )

    migrated = MarketTapeStore(config)
    with migrated.connect() as connection:
        views = [row[0] for row in connection.execute(
            """SELECT observation.views
               FROM mt_observation_quality_flags quality
               JOIN mt_market_observations observation
                 ON observation.observation_id = quality.observation_id
               ORDER BY observation.observed_at"""
        ).fetchall()]
        raw_count = connection.execute(
            "SELECT COUNT(*) FROM mt_market_observations"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """UPDATE mt_observation_quality_flags
                   SET error_code = 'changed' WHERE flag_id = (
                       SELECT flag_id FROM mt_observation_quality_flags LIMIT 1
                   )"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """DELETE FROM mt_observation_quality_flags WHERE flag_id = (
                       SELECT flag_id FROM mt_observation_quality_flags LIMIT 1
                   )"""
            )
    assert views == [0, 50]
    assert raw_count == 5

    reopened = MarketTapeStore(config)
    with reopened.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM mt_observation_quality_flags"
        ).fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    reopened.start_run("post-quality-backfill", "recheck")
    reopened.finish_run("post-quality-backfill")
    reopened.enqueue_run_for_sync("post-quality-backfill")
    reopened.enqueue_run_for_sync("post-quality-backfill")
    with reopened.connect() as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM mt_sync_outbox
               WHERE entity_type = 'observation_quality_flag'"""
        ).fetchone()[0] == 2


def test_mixed_batch_tracks_regression_separately_from_true_duplicate(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    first_at = datetime.now(timezone.utc) - timedelta(minutes=15)
    seeded = [
        _item(external_id, first_at, 100)
        for external_id in ("mixed-valid", "mixed-bad", "mixed-duplicate")
    ]
    store.start_run("mixed-seed", "integration")
    for item in seeded:
        store.ingest(item, "mixed-seed")
    store.finish_run("mixed-seed")

    now = first_at + timedelta(minutes=5)
    items = [
        _item("mixed-valid", now, 150),
        _item("mixed-bad", now, 0),
        seeded[2],
    ]
    store.start_run("mixed-refresh", "recheck")
    receipt = SourceReceipt(
        run_id="mixed-refresh",
        source_id="youtube-data-api",
        platform="youtube",
        state=SourceState.READY,
        started_at=now,
        finished_at=now,
        request_count=1,
        refreshed_count=3,
    )
    batch = ProviderBatch(items, receipt)
    accepted = MarketTapeCollector(
        config,
        store,
        source_builder=lambda *_: [],
    )._persist_batch(batch, "mixed-refresh")
    rejected = set(receipt.metadata["rejected_video_ids"])
    returned = {item.video_id for item in items}
    store.defer_unchanged_polls(returned - accepted - rejected)
    store.finish_run("mixed-refresh")

    assert accepted == {items[0].video_id}
    assert rejected == {items[1].video_id}
    assert receipt.accepted_count == 1
    assert receipt.duplicate_count == 1
    assert receipt.failed_count == 1
    assert receipt.metadata["counter_regression_count"] == 1
    with store.connect() as connection:
        queue_errors = {
            row["video_id"]: row["last_error_code"]
            for row in connection.execute(
                """SELECT video_id, last_error_code FROM mt_poll_queue
                   WHERE video_id IN (?, ?, ?)""",
                tuple(item.video_id for item in items),
            ).fetchall()
        }
    assert queue_errors[items[1].video_id] == "counter_regression"
    assert queue_errors[items[2].video_id] == "unchanged_source_snapshot"


def test_flag_identity_is_global_across_independent_sqlite_spools(tmp_path):
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    shared_first = _item("cross-spool-shared", observed_at, 100)
    shared_bad = _item(
        "cross-spool-shared",
        observed_at + timedelta(minutes=1),
        0,
    )

    first_store = MarketTapeStore(_config(tmp_path / "spool-a"))
    first_shared = _record_regression(
        first_store,
        run_id="spool-a-shared",
        first=shared_first,
        regressed=shared_bad,
    )

    second_store = MarketTapeStore(_config(tmp_path / "spool-b"))
    different_first = _item("cross-spool-different", observed_at, 200)
    different_bad = _item(
        "cross-spool-different",
        observed_at + timedelta(minutes=1),
        10,
    )
    second_different = _record_regression(
        second_store,
        run_id="spool-b-different",
        first=different_first,
        regressed=different_bad,
    )
    second_shared = _record_regression(
        second_store,
        run_id="spool-b-shared",
        first=shared_first,
        regressed=shared_bad,
    )

    expected_shared_id = f"counter-regression:{shared_bad.observation_key}"
    assert first_shared["observation_key"] == second_shared["observation_key"]
    assert first_shared["prior_observation_key"] == shared_first.observation_key
    assert second_shared["prior_observation_key"] == shared_first.observation_key
    assert first_shared["observation_id"] != second_shared["observation_id"]
    assert first_shared["flag_id"] == second_shared["flag_id"] == expected_shared_id

    # These two rows deliberately have the same database-local id. Their
    # globally different observation keys must still produce different ids.
    assert first_shared["observation_id"] == second_different["observation_id"]
    assert first_shared["observation_key"] != second_different["observation_key"]
    assert first_shared["flag_id"] != second_different["flag_id"]

    # Exercise the remote natural-key contract in another real SQLite database:
    # a duplicate observation is idempotent while distinct observations survive.
    with sqlite3.connect(tmp_path / "central-quality.sqlite3") as central:
        central.execute(
            """CREATE TABLE quality_flags(
                   flag_id TEXT PRIMARY KEY,
                   observation_key TEXT NOT NULL UNIQUE
               )"""
        )
        for flag in (first_shared, second_shared, second_different):
            central.execute(
                """INSERT INTO quality_flags(flag_id, observation_key)
                   VALUES(?, ?) ON CONFLICT(observation_key) DO NOTHING""",
                (flag["flag_id"], flag["observation_key"]),
            )
        assert central.execute(
            "SELECT COUNT(*) FROM quality_flags"
        ).fetchone()[0] == 2
    assert ENTITY_TABLES["observation_quality_flag"][1] == "observation_key"


def test_legacy_local_flag_and_pending_outbox_are_canonicalized_for_sync(tmp_path):
    config = _config(tmp_path)
    store = MarketTapeStore(config)
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    first = _item("legacy-sync-flag", observed_at, 100)
    regressed = _item(
        "legacy-sync-flag",
        observed_at + timedelta(minutes=1),
        0,
    )
    row = _record_regression(
        store,
        run_id="legacy-sync-run",
        first=first,
        regressed=regressed,
    )
    legacy_id = f"counter-regression:{row['observation_id']}"
    canonical_id = f"counter-regression:{regressed.observation_key}"

    # Recreate the on-disk shape written before flag ids became global. The
    # quality row remains immutable after the store is reopened.
    with store.connect() as connection:
        connection.execute("DROP TRIGGER mt_observation_quality_flags_no_update")
        connection.execute(
            """UPDATE mt_observation_quality_flags SET flag_id = ?
               WHERE observation_id = ?""",
            (legacy_id, row["observation_id"]),
        )
    reopened = MarketTapeStore(config)
    assert reopened.enqueue_run_for_sync("legacy-sync-run") > 0
    with reopened.connect() as connection:
        persisted_flag_id = connection.execute(
            """SELECT flag_id FROM mt_observation_quality_flags
               WHERE observation_id = ?""",
            (row["observation_id"],),
        ).fetchone()[0]
        persisted_outbox = connection.execute(
            """SELECT outbox_id, entity_key, payload_json
               FROM mt_sync_outbox
               WHERE entity_type = 'observation_quality_flag'"""
        ).fetchone()
        legacy_payload = json.loads(persisted_outbox["payload_json"])
        legacy_payload["flag_id"] = legacy_id
        connection.execute(
            """UPDATE mt_sync_outbox
               SET entity_key = ?, payload_json = ?
               WHERE outbox_id = ?""",
            (
                legacy_id,
                json.dumps(legacy_payload, sort_keys=True),
                persisted_outbox["outbox_id"],
            ),
        )
    assert persisted_flag_id == legacy_id

    reopened.enqueue_missing_for_sync()
    reopened.enqueue_run_for_sync("legacy-sync-run")
    with reopened.connect() as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM mt_sync_outbox
               WHERE entity_type = 'observation_quality_flag'"""
        ).fetchone()[0] == 1

    pending = [
        record
        for record in reopened.pending_outbox(5000)
        if record["entity_type"] == "observation_quality_flag"
    ]
    assert len(pending) == 1
    assert pending[0]["entity_key"] == canonical_id
    assert pending[0]["payload"]["flag_id"] == canonical_id
    assert pending[0]["payload"]["observation_key"] == regressed.observation_key


def test_live_same_timestamp_decision_matches_backfill_tuple_order(tmp_path):
    store = MarketTapeStore(_config(tmp_path))
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=5)

    future_high, canonical_first_low = _same_timestamp_pair(
        "same-time-future-high",
        observed_at,
        high_key_precedes=False,
    )
    store.start_run("same-time-future", "recheck")
    assert store.ingest(future_high, "same-time-future")[0] is True
    assert store.ingest(canonical_first_low, "same-time-future")[0] is True
    store.finish_run("same-time-future")

    prior_high, canonical_later_low = _same_timestamp_pair(
        "same-time-prior-high",
        observed_at,
        high_key_precedes=True,
    )
    store.start_run("same-time-prior", "recheck")
    assert store.ingest(prior_high, "same-time-prior")[0] is True
    with pytest.raises(CounterRegressionError):
        store.ingest(canonical_later_low, "same-time-prior")
    store.finish_run("same-time-prior")

    with store.connect() as connection:
        accepted = connection.execute(
            """SELECT source_confidence FROM mt_market_observations
               WHERE observation_key = ?""",
            (canonical_first_low.observation_key,),
        ).fetchone()[0]
        rejected = connection.execute(
            """SELECT source_confidence FROM mt_market_observations
               WHERE observation_key = ?""",
            (canonical_later_low.observation_key,),
        ).fetchone()[0]
        flag_keys = {
            row[0] for row in connection.execute(
                """SELECT observation.observation_key
                   FROM mt_observation_quality_flags quality
                   JOIN mt_market_observations observation
                     ON observation.observation_id = quality.observation_id"""
            ).fetchall()
        }
    assert accepted == 1.0
    assert canonical_first_low.observation_key not in flag_keys
    assert rejected == 0.0
    assert canonical_later_low.observation_key in flag_keys


def test_current_run_enqueues_retroactive_old_run_flag_once(tmp_path):
    store = MarketTapeStore(_config(tmp_path))
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    current_high, old_low = _same_timestamp_pair(
        "retroactive-old-run",
        observed_at,
        high_key_precedes=True,
    )

    store.start_run("retroactive-old", "archive_bootstrap")
    assert store.ingest(old_low, "retroactive-old")[0] is True
    store.finish_run("retroactive-old")
    store.start_run("retroactive-current", "recheck")
    assert store.ingest(current_high, "retroactive-current")[0] is True
    store.finish_run("retroactive-current")

    with store.connect() as connection:
        quality = dict(connection.execute(
            """SELECT quality.*, observation.observation_key,
                      prior.observation_key AS prior_observation_key
               FROM mt_observation_quality_flags quality
               JOIN mt_market_observations observation
                 ON observation.observation_id = quality.observation_id
               JOIN mt_market_observations prior
                 ON prior.observation_id = quality.prior_observation_id"""
        ).fetchone())
        assert connection.execute(
            """SELECT COUNT(*) FROM mt_sync_outbox
               WHERE entity_type = 'observation_quality_flag'"""
        ).fetchone()[0] == 0
    assert quality["run_id"] == "retroactive-old"
    assert quality["observation_key"] == old_low.observation_key
    assert quality["prior_observation_key"] == current_high.observation_key

    assert store.enqueue_run_for_sync("retroactive-current") > 0
    with store.connect() as connection:
        quality_outbox = connection.execute(
            """SELECT outbox_id, entity_key, payload_json, synced_at
               FROM mt_sync_outbox
               WHERE entity_type = 'observation_quality_flag'"""
        ).fetchall()
        queued_keys = {
            (row["entity_type"], row["entity_key"])
            for row in connection.execute(
                """SELECT entity_type, entity_key FROM mt_sync_outbox
                   WHERE entity_type IN ('run', 'observation')"""
            ).fetchall()
        }
    assert len(quality_outbox) == 1
    expected_flag_id = f"counter-regression:{old_low.observation_key}"
    assert quality_outbox[0]["entity_key"] == expected_flag_id
    assert json.loads(quality_outbox[0]["payload_json"])["flag_id"] == expected_flag_id
    assert ("run", "retroactive-old") in queued_keys
    assert ("observation", old_low.observation_key) in queued_keys
    assert ("observation", current_high.observation_key) in queued_keys

    store.enqueue_run_for_sync("retroactive-current")
    store.mark_outbox_synced([quality_outbox[0]["outbox_id"]])
    store.start_run("retroactive-next", "recheck")
    store.finish_run("retroactive-next")
    store.enqueue_run_for_sync("retroactive-next")
    with store.connect() as connection:
        rows = connection.execute(
            """SELECT entity_key, synced_at FROM mt_sync_outbox
               WHERE entity_type = 'observation_quality_flag'"""
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["entity_key"] == expected_flag_id
    assert rows[0]["synced_at"] is not None
