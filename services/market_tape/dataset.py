"""Daily Passport snapshots, coverage certification, and prediction readiness."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import queue
import shutil
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .config import MarketTapeConfig
from .models import isoformat, stable_hash, utc_now
from .predictor import MarketTapePredictor
from .store import MarketTapeStore


DATASET_CONTRACT = "market_tape_daily_dataset_v1"
EXCLUDED_EXPORT_TABLES = {"mt_sync_outbox"}


class MarketTapeDatasetManager:
    """Create recovery-grade datasets without moving the live SQLite spool."""

    def __init__(
        self,
        config: MarketTapeConfig,
        store: Optional[MarketTapeStore] = None,
    ):
        self.config = config
        self.store = store or MarketTapeStore(config)
        self.local_status_path = config.heartbeat_path.with_name(
            "market-tape-dataset-status.json"
        )
        self.latest_success_path = config.heartbeat_path.with_name(
            "market-tape-dataset-latest-success.json"
        )

    def certify(self, dataset_date: str | date | None = None) -> Dict[str, Any]:
        target_date = _target_date(dataset_date)
        checked_at = utc_now()
        if not self.config.dataset_export_enabled:
            result = {
                "contract": DATASET_CONTRACT,
                "state": "disabled",
                "dataset_date": target_date.isoformat(),
                "checked_at": isoformat(checked_at),
            }
            self._record_local_status(result)
            return result

        self._record_progress(target_date, checked_at, "checking_storage")
        storage = self._storage_preflight()
        if storage["state"] != "ready":
            result = {
                "contract": DATASET_CONTRACT,
                "state": "blocked_storage",
                "dataset_date": target_date.isoformat(),
                "checked_at": isoformat(checked_at),
                "storage": storage,
            }
            self._record_local_status(result)
            return result

        self._record_progress(target_date, checked_at, "preparing_predictions", storage=storage)
        prediction_evaluation = self.store.evaluate_predictions(checked_at)
        prediction_training = MarketTapePredictor(self.config, self.store).train()
        prediction_forecast = self.store.forecast_active_trends(checked_at)
        certification_id = (
            f"mt-dataset-{target_date.isoformat()}-"
            f"{checked_at.strftime('%Y%m%dT%H%M%S%fZ')}-"
            f"{stable_hash(str(self.config.db_path))[:8]}"
        )
        staging = self.config.dataset_root / ".staging" / certification_id
        final = self.config.dataset_root / target_date.isoformat() / certification_id
        try:
            staging.mkdir(parents=True, exist_ok=False)
            self._record_progress(
                target_date,
                checked_at,
                "mirroring_raw_objects",
                certification_id=certification_id,
                storage=storage,
                progress={"processed": 0},
            )
            raw_archive = self._mirror_raw_objects(
                lambda progress: self._record_progress(
                    target_date,
                    checked_at,
                    "mirroring_raw_objects",
                    certification_id=certification_id,
                    storage=storage,
                    progress=progress,
                )
            )
            self._record_progress(
                target_date,
                checked_at,
                "writing_sqlite_snapshot",
                certification_id=certification_id,
                storage=storage,
            )
            snapshot = self._write_snapshot(staging)
            self._record_progress(
                target_date,
                checked_at,
                "exporting_tables",
                certification_id=certification_id,
                storage=storage,
            )
            table_exports = self._export_tables(staging / "tables")
            self._record_progress(
                target_date,
                checked_at,
                "finalizing_manifest",
                certification_id=certification_id,
                storage=storage,
            )
            model_artifacts = self._export_models(staging / "models")
            snapshot["path"] = str(final / Path(snapshot["path"]).relative_to(staging))
            for artifact in table_exports:
                artifact["path"] = str(
                    final / Path(artifact["path"]).relative_to(staging)
                )
            for artifact in model_artifacts:
                artifact["path"] = str(
                    final / Path(artifact["path"]).relative_to(staging)
                )
            quality = self._quality_report(
                target_date,
                prediction_evaluation,
                prediction_training,
                prediction_forecast,
                raw_archive,
                snapshot,
            )
            gates = self._certification_gates(quality)
            state = "certified" if all(gates.values()) else "partial"
            manifest = {
                "contract": DATASET_CONTRACT,
                "schema_version": 1,
                "certification_id": certification_id,
                "state": state,
                "dataset_date": target_date.isoformat(),
                "created_at": isoformat(checked_at),
                "database_schema_version": self.store.status()["schema_version"],
                "source_database_path": str(self.config.db_path),
                "dataset_path": str(final),
                "storage": storage,
                "gates": gates,
                "quality": quality,
                "artifacts": {
                    "sqlite_snapshot": snapshot,
                    "tables": table_exports,
                    "raw_archive": raw_archive,
                    "prediction_models": model_artifacts,
                },
            }
            _atomic_json(staging / "certification.json", manifest)
            (staging / "README.md").write_text(
                self._readme(manifest), encoding="utf-8"
            )
            final.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(final)
            manifest["manifest_path"] = str(final / "certification.json")
            latest = {
                "contract": DATASET_CONTRACT,
                "state": state,
                "dataset_date": target_date.isoformat(),
                "certification_id": certification_id,
                "created_at": isoformat(checked_at),
                "manifest_path": manifest["manifest_path"],
            }
            _atomic_json(self.config.dataset_root / "latest.json", latest)
            self._record_local_status(manifest)
            return manifest
        except Exception as error:
            shutil.rmtree(staging, ignore_errors=True)
            result = {
                "contract": DATASET_CONTRACT,
                "state": "failed",
                "dataset_date": target_date.isoformat(),
                "checked_at": isoformat(utc_now()),
                "storage": storage,
                "error_type": error.__class__.__name__,
                "error": str(error)[:1000],
            }
            self._record_local_status(result)
            return result

    def status(self) -> Dict[str, Any]:
        if not self.local_status_path.is_file():
            return {
                "contract": DATASET_CONTRACT,
                "state": "not_run",
                "status_path": str(self.local_status_path),
            }
        try:
            status = json.loads(self.local_status_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return {
                "contract": DATASET_CONTRACT,
                "state": "invalid_status",
                "error": str(error)[:500],
            }
        manifest_path = Path(str(status.get("manifest_path") or ""))
        status["manifest_available"] = bool(
            manifest_path and manifest_path.is_file()
        )
        if self.latest_success_path.is_file():
            try:
                latest_success = json.loads(
                    self.latest_success_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                latest_success = {}
            if latest_success:
                status["latest_success"] = latest_success
        return status

    def _storage_preflight(self) -> Dict[str, Any]:
        result_queue: queue.Queue[Dict[str, Any]] = queue.Queue(maxsize=1)

        def check() -> None:
            try:
                result = self._storage_preflight_blocking()
            except Exception as error:
                result = {
                    "state": "preflight_failed",
                    "mount": str(self.config.passport_mount),
                    "dataset_root": str(self.config.dataset_root),
                    "error_type": error.__class__.__name__,
                    "error": str(error)[:500],
                }
            result_queue.put(result)

        worker = threading.Thread(
            target=check,
            name="market-tape-storage-preflight",
            daemon=True,
        )
        worker.start()
        timeout = max(0.1, self.config.dataset_storage_preflight_timeout_seconds)
        worker.join(timeout)
        if worker.is_alive():
            return {
                "state": "preflight_timeout",
                "mount": str(self.config.passport_mount),
                "dataset_root": str(self.config.dataset_root),
                "timeout_seconds": timeout,
                "error": "external dataset storage did not answer the bounded write probe",
            }
        return result_queue.get_nowait()

    def _storage_preflight_blocking(self) -> Dict[str, Any]:
        mount = self.config.passport_mount
        root = self.config.dataset_root
        if not mount.is_dir():
            return {
                "state": "missing_mount",
                "mount": str(mount),
                "dataset_root": str(root),
            }
        if self.config.dataset_require_mounted_volume and not os.path.ismount(mount):
            return {
                "state": "not_mounted_volume",
                "mount": str(mount),
                "dataset_root": str(root),
            }
        try:
            root.resolve().relative_to(mount.resolve())
        except ValueError:
            return {
                "state": "root_outside_mount",
                "mount": str(mount),
                "dataset_root": str(root),
            }
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / f".write-probe-{os.getpid()}"
            descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(descriptor, b"market-tape\n")
            os.close(descriptor)
            probe.unlink()
            available = shutil.disk_usage(mount).free
        except OSError as error:
            return {
                "state": "not_writable",
                "mount": str(mount),
                "dataset_root": str(root),
                "error": str(error)[:500],
            }
        return {
            "state": "ready",
            "mount": str(mount),
            "dataset_root": str(root),
            "available_bytes": int(available),
        }

    def _write_snapshot(self, output_dir: Path) -> Dict[str, Any]:
        plain = output_dir / "market-tape.sqlite3"
        compressed = output_dir / "market-tape.sqlite3.gz"
        destination = sqlite3.connect(plain)
        try:
            with self.store.connect() as source:
                source.backup(destination)
            quick_check = destination.execute("PRAGMA quick_check").fetchone()[0]
            foreign_key_errors = len(destination.execute("PRAGMA foreign_key_check").fetchall())
        finally:
            destination.close()
        with plain.open("rb") as source, compressed.open("wb") as raw_output:
            with gzip.GzipFile(fileobj=raw_output, mode="wb", compresslevel=6, mtime=0) as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
        plain.unlink()
        return {
            "path": str(compressed),
            "bytes": compressed.stat().st_size,
            "sha256": _file_sha256(compressed),
            "quick_check": quick_check,
            "foreign_key_errors": foreign_key_errors,
        }

    def _export_tables(self, output_dir: Path) -> List[Dict[str, Any]]:
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts: List[Dict[str, Any]] = []
        with self.store.connect() as connection:
            tables = [
                str(row[0]) for row in connection.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type = 'table' AND name LIKE 'mt_%'
                       ORDER BY name"""
                ).fetchall()
                if str(row[0]) not in EXCLUDED_EXPORT_TABLES
            ]
            for table in tables:
                path = output_dir / f"{table}.jsonl.gz"
                row_count = 0
                with path.open("wb") as raw_output:
                    with gzip.GzipFile(
                        fileobj=raw_output,
                        mode="wb",
                        compresslevel=6,
                        mtime=0,
                    ) as output:
                        for row in connection.execute(
                            f"SELECT * FROM {table} ORDER BY rowid"
                        ):
                            encoded = (
                                json.dumps(
                                    dict(row),
                                    sort_keys=True,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                )
                                + "\n"
                            ).encode("utf-8")
                            output.write(encoded)
                            row_count += 1
                artifacts.append({
                    "table": table,
                    "path": str(path),
                    "rows": row_count,
                    "bytes": path.stat().st_size,
                    "sha256": _file_sha256(path),
                })
        return artifacts

    def _export_models(self, output_dir: Path) -> List[Dict[str, Any]]:
        if not self.config.prediction_model_dir.is_dir():
            return []
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts = []
        for source in sorted(self.config.prediction_model_dir.glob("*.json")):
            destination = output_dir / source.name
            shutil.copy2(source, destination)
            artifacts.append({
                "model_file": source.name,
                "path": str(destination),
                "bytes": destination.stat().st_size,
                "sha256": _file_sha256(destination),
            })
        return artifacts

    def _mirror_raw_objects(
        self,
        on_progress: Optional[Callable[[Dict[str, int]], None]] = None,
    ) -> Dict[str, Any]:
        destination_root = self.config.dataset_root.parent / "raw-objects"
        destination_root.mkdir(parents=True, exist_ok=True)
        checked = 0
        copied = 0
        bytes_copied = 0
        destination_deep_verified = 0
        destination_provenance_verified = 0
        missing: List[str] = []
        corrupt: List[str] = []
        with self.store.connect() as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT * FROM mt_raw_objects ORDER BY raw_sha256"
            ).fetchall()]
        existing_mirror_paths = {
            str(path.relative_to(destination_root))
            for path in destination_root.rglob("*.json.gz")
        }
        for index, row in enumerate(rows, start=1):
            if on_progress is not None and (index == 1 or index % 250 == 0):
                on_progress({
                    "processed": index - 1,
                    "total": len(rows),
                    "verified": checked,
                    "missing": len(missing),
                    "corrupt": len(corrupt),
                })
            relative = Path(str(row["object_path"]))
            source = self.config.object_dir / relative
            destination = destination_root / relative
            if not source.is_file():
                missing.append(str(relative))
                continue
            try:
                with gzip.open(source, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            except (OSError, EOFError):
                corrupt.append(str(relative))
                continue
            if digest != row["raw_sha256"]:
                corrupt.append(str(relative))
                continue
            destination_matches = str(relative) in existing_mirror_paths
            if not destination_matches:
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_name(
                    f".{destination.name}.tmp-{os.getpid()}"
                )
                try:
                    shutil.copy2(source, temporary)
                    with gzip.open(temporary, "rb") as handle:
                        destination_digest = hashlib.sha256(handle.read()).hexdigest()
                    if destination_digest != row["raw_sha256"]:
                        corrupt.append(str(relative))
                        continue
                    temporary.replace(destination)
                    copied += 1
                    bytes_copied += destination.stat().st_size
                except (OSError, EOFError):
                    corrupt.append(str(relative))
                    continue
                finally:
                    temporary.unlink(missing_ok=True)
                destination_deep_verified += 1
            else:
                # Only this certifier writes content-addressed mirror paths. An
                # unchanged size therefore reuses the successful copy-time hash
                # receipt instead of reopening every Passport object each day.
                destination_provenance_verified += 1
            checked += 1
        if on_progress is not None:
            on_progress({
                "processed": len(rows),
                "total": len(rows),
                "verified": checked,
                "missing": len(missing),
                "corrupt": len(corrupt),
            })
        return {
            "state": "ready" if not missing and not corrupt else "incomplete",
            "path": str(destination_root),
            "verification_policy": "content-addressed-copy-once-v2",
            "mirror_entries_discovered": len(existing_mirror_paths),
            "registered": len(rows),
            "verified": checked,
            "destination_deep_verified": destination_deep_verified,
            "destination_provenance_verified": destination_provenance_verified,
            "copied": copied,
            "bytes_copied": bytes_copied,
            "missing_count": len(missing),
            "corrupt_count": len(corrupt),
            "missing_examples": missing[:25],
            "corrupt_examples": corrupt[:25],
        }

    def _quality_report(
        self,
        target_date: date,
        prediction_evaluation: Dict[str, Any],
        prediction_training: Dict[str, Any],
        prediction_forecast: Dict[str, Any],
        raw_archive: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        day = target_date.isoformat()
        with self.store.connect() as connection:
            acquired_rows = {
                str(row["platform"]): int(row["videos"])
                for row in connection.execute(
                    """SELECT platform, COUNT(*) AS videos
                       FROM mt_videos WHERE substr(first_seen_at, 1, 10) = ?
                       GROUP BY platform""",
                    (day,),
                ).fetchall()
            }
            collected_rows = {
                str(row["platform"]): dict(row)
                for row in connection.execute(
                    """SELECT observation.platform,
                              COUNT(*) AS collected_observations,
                              COUNT(DISTINCT observation.video_id) AS collected_videos,
                              SUM(observation.views > 0) AS observations_with_views,
                              SUM(observation.creator_followers > 0) AS observations_with_followers
                       FROM mt_market_observations observation
                       JOIN mt_collection_runs run ON run.run_id = observation.run_id
                       WHERE substr(run.started_at, 1, 10) = ?
                       GROUP BY observation.platform""",
                    (day,),
                ).fetchall()
            }
            trajectory_rows = {
                str(row["platform"]): int(row["trajectory_videos"])
                for row in connection.execute(
                    """SELECT video.platform, COUNT(*) AS trajectory_videos
                       FROM (
                           SELECT video_id FROM mt_market_observations
                           GROUP BY video_id HAVING COUNT(*) >= 2
                       ) eligible
                       JOIN mt_videos video USING(video_id)
                       GROUP BY video.platform"""
                ).fetchall()
            }
            platform_rows = []
            for platform in self.config.platforms:
                collected = collected_rows.get(platform, {})
                platform_rows.append({
                    "platform": platform,
                    "acquired_videos": acquired_rows.get(platform, 0),
                    "collected_observations": int(collected.get("collected_observations") or 0),
                    "collected_videos": int(collected.get("collected_videos") or 0),
                    "observations_with_views": int(collected.get("observations_with_views") or 0),
                    "observations_with_followers": int(collected.get("observations_with_followers") or 0),
                    "trajectory_videos": trajectory_rows.get(platform, 0),
                })
            attempt_rows = [dict(row) for row in connection.execute(
                """SELECT lower(trim(query)) AS query, platform,
                          COUNT(*) AS attempts,
                          SUM(result_count) AS results,
                          SUM(state = 'completed') AS completed,
                          SUM(state = 'empty') AS empty,
                          SUM(state IN ('failed', 'timed_out', 'partial')) AS failed
                   FROM mt_query_attempts
                   WHERE substr(attempted_at, 1, 10) = ?
                   GROUP BY lower(trim(query)), platform
                   ORDER BY query, platform""",
                (day,),
            ).fetchall()]
            family_rows = [dict(row) for row in connection.execute(
                """SELECT lower(trim(COALESCE(
                              NULLIF(json_extract(metadata_json, '$.query_family'), ''),
                              query
                          ))) AS query_family,
                          platform,
                          COUNT(*) AS attempts,
                          SUM(result_count) AS results,
                          SUM(state IN ('completed', 'empty', 'partial')) AS completed,
                          SUM(state IN ('failed', 'timed_out')) AS failed
                   FROM mt_query_attempts
                   WHERE substr(attempted_at, 1, 10) = ?
                   GROUP BY query_family, platform
                   ORDER BY query_family, platform""",
                (day,),
            ).fetchall()]
            attribution = dict(connection.execute(
                """SELECT COUNT(*) AS raw_rows,
                          COUNT(DISTINCT video_id || '|' || source_id || '|' || lower(query))
                              AS semantic_rows
                   FROM mt_discovery_attributions"""
            ).fetchone())
            genome = dict(connection.execute(
                """SELECT COUNT(*) AS genomes,
                          SUM(transcript != '') AS transcripts,
                          SUM(text_embedding_ref != '') AS text_embeddings,
                          SUM(visual_embedding_ref != '') AS visual_embeddings,
                          SUM(audio_embedding_ref != '') AS audio_embeddings
                   FROM mt_content_genomes"""
            ).fetchone())
            source_failures = [dict(row) for row in connection.execute(
                """SELECT source_id, platform, state, error_code, error_detail,
                          checked_at, next_retry_at
                   FROM mt_source_health WHERE state != 'ready'
                   ORDER BY platform, source_id"""
            ).fetchall()]
            forecasts = [dict(row) for row in connection.execute(
                """WITH ranked AS (
                       SELECT prediction_id, subject_type, subject_id, model_version,
                              predicted_at, horizon, probability, expected_peak_at,
                              expected_remaining_life_hours,
                              ROW_NUMBER() OVER (
                                  PARTITION BY subject_type, subject_id, model_version, horizon
                                  ORDER BY predicted_at DESC, prediction_id DESC
                              ) AS row_number
                       FROM mt_predictions
                   )
                   SELECT * FROM ranked WHERE row_number = 1
                   ORDER BY probability DESC LIMIT 100"""
            ).fetchall()]

        required_platforms = list(self.config.platforms)
        query_platforms: Dict[str, set[str]] = {}
        for row in attempt_rows:
            query_platforms.setdefault(str(row["query"]), set()).add(str(row["platform"]))
        required_query_families = sorted({
            " ".join(str(topic).casefold().split())
            for topic in self.config.topics
            if str(topic).strip()
        })
        family_platforms: Dict[str, set[str]] = {
            query: set() for query in required_query_families
        }
        for row in family_rows:
            family = str(row["query_family"])
            if family in family_platforms:
                family_platforms[family].add(str(row["platform"]))
        expected_pairs = len(required_query_families) * len(required_platforms)
        observed_pairs = sum(
            len(platforms & set(required_platforms))
            for platforms in family_platforms.values()
        )
        gaps = [
            {"query": query, "missing_platform": platform}
            for query, platforms in sorted(family_platforms.items())
            for platform in required_platforms
            if platform not in platforms
        ]
        platform_quality = {}
        for row in platform_rows:
            platform = str(row.pop("platform"))
            target = self.config.target_for(platform)
            row["target"] = target
            row["target_met"] = int(row["acquired_videos"]) >= target
            platform_quality[platform] = row
        return {
            "integrity": {
                "sqlite_quick_check": snapshot["quick_check"],
                "foreign_key_errors": snapshot["foreign_key_errors"],
                "raw_archive_state": raw_archive["state"],
                "raw_objects_registered": raw_archive["registered"],
                "raw_objects_verified": raw_archive["verified"],
                "raw_objects_missing": raw_archive["missing_count"],
                "raw_objects_corrupt": raw_archive["corrupt_count"],
            },
            "collection": {
                "target": self.config.daily_unique_target,
                "acquired": sum(int(row["acquired_videos"]) for row in platform_rows),
                "platforms": platform_quality,
                "source_failures": source_failures,
            },
            "query_coverage": {
                "queries_attempted": len(query_platforms),
                "query_families_required": required_query_families,
                "platforms_required": required_platforms,
                "expected_query_platform_pairs": expected_pairs,
                "observed_query_platform_pairs": observed_pairs,
                "coverage_ratio": round(observed_pairs / expected_pairs, 6)
                if expected_pairs else 0.0,
                "fully_covered_queries": sum(
                    set(required_platforms).issubset(platforms)
                    for platforms in family_platforms.values()
                ),
                "attempt_rows": attempt_rows,
                "family_attempt_rows": family_rows,
                "gap_count": len(gaps),
                "gap_examples": gaps[:250],
            },
            "lineage": {
                "raw_attribution_rows": int(attribution.get("raw_rows") or 0),
                "semantic_attribution_rows": int(attribution.get("semantic_rows") or 0),
                "redundant_historical_attribution_rows": max(
                    0,
                    int(attribution.get("raw_rows") or 0)
                    - int(attribution.get("semantic_rows") or 0),
                ),
            },
            "feature_completeness": genome,
            "prediction_backtest": prediction_evaluation,
            "prediction_training": prediction_training,
            "prediction_forecast": prediction_forecast,
            "current_forecasts": forecasts,
            "current_opportunities": self.store.trend_opportunities(limit=100),
        }

    def _certification_gates(self, quality: Dict[str, Any]) -> Dict[str, bool]:
        integrity = quality["integrity"]
        collection = quality["collection"]
        coverage = quality["query_coverage"]
        platform_rows = collection["platforms"]
        prediction_models = quality["prediction_backtest"].get("models", [])
        return {
            "sqlite_integrity": (
                integrity["sqlite_quick_check"] == "ok"
                and integrity["foreign_key_errors"] == 0
            ),
            "raw_archive_complete": (
                integrity["raw_archive_state"] == "ready"
                and integrity["raw_objects_registered"] == integrity["raw_objects_verified"]
            ),
            "daily_volume_target": collection["acquired"] >= collection["target"],
            "all_platform_targets": bool(platform_rows) and all(
                bool(row["target_met"]) for row in platform_rows.values()
            ),
            "all_platforms_attempted": all(
                any(
                    row["platform"] == platform
                    for row in coverage["attempt_rows"]
                )
                for platform in coverage["platforms_required"]
            ),
            "full_query_platform_coverage": (
                coverage["expected_query_platform_pairs"] > 0
                and coverage["coverage_ratio"] == 1.0
            ),
            "trajectory_coverage": all(
                int(row["trajectory_videos"]) > 0 for row in platform_rows.values()
            ),
            "prediction_model_validated": any(
                model.get("state") == "validated" for model in prediction_models
            ),
        }

    def _record_local_status(self, result: Dict[str, Any]) -> None:
        self.local_status_path.parent.mkdir(parents=True, exist_ok=True)
        summary = {
            key: result[key]
            for key in (
                "contract", "state", "dataset_date", "checked_at", "created_at",
                "updated_at", "phase", "progress", "certification_id",
                "manifest_path", "storage", "gates",
            )
            if key in result
        }
        _atomic_json(self.local_status_path, summary)
        if (
            summary.get("state") in {"partial", "certified"}
            and summary.get("manifest_path")
        ):
            latest_success = {
                **summary,
                "manifest_available": True,
                "availability_check": "successful_certification_receipt",
            }
            _atomic_json(self.latest_success_path, latest_success)

    def _record_progress(
        self,
        target_date: date,
        checked_at: datetime,
        phase: str,
        *,
        certification_id: str = "",
        storage: Optional[Dict[str, Any]] = None,
        progress: Optional[Dict[str, int]] = None,
    ) -> None:
        self._record_local_status({
            "contract": DATASET_CONTRACT,
            "state": "running",
            "dataset_date": target_date.isoformat(),
            "checked_at": isoformat(checked_at),
            "updated_at": isoformat(utc_now()),
            "phase": phase,
            "certification_id": certification_id,
            "storage": storage or {},
            "progress": progress or {},
        })

    @staticmethod
    def _readme(manifest: Dict[str, Any]) -> str:
        collection = manifest["quality"]["collection"]
        coverage = manifest["quality"]["query_coverage"]
        backtest = manifest["quality"]["prediction_backtest"]
        gates = "\n".join(
            f"- [{'x' if passed else ' '}] {name}"
            for name, passed in manifest["gates"].items()
        )
        return (
            f"# Market Tape Dataset {manifest['dataset_date']}\n\n"
            f"State: **{manifest['state']}**  \n"
            f"Created: `{manifest['created_at']}`\n\n"
            f"Acquired {collection['acquired']} of {collection['target']} daily videos. "
            f"Measured {coverage['observed_query_platform_pairs']} of "
            f"{coverage['expected_query_platform_pairs']} query/platform pairs. "
            f"Prediction labels scored: {backtest.get('scored_labels', 0)}.\n\n"
            "## Certification Gates\n\n"
            f"{gates}\n\n"
            "`certification.json` is authoritative. `market-tape.sqlite3.gz` is the "
            "online recovery snapshot. `tables/` contains deterministic compressed JSONL exports.\n"
        )


def _target_date(value: str | date | None) -> date:
    if isinstance(value, date):
        return value
    if value:
        return date.fromisoformat(str(value))
    return (datetime.now(timezone.utc) - timedelta(days=1)).date()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
