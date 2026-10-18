"""
Run Manager Service
====================
Manages agent runs, steps, events, and artifacts.
Central coordinator for the automation center.
"""

import os
import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
from dataclasses import dataclass
from enum import Enum
from uuid import uuid4
from pathlib import Path

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")

# Load registries
REGISTRY_PATH = Path(__file__).parent / "registries"


class RunStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    PAUSED = "paused"


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class EventSeverity(Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class StepDefinition:
    """Step definition from registry."""
    key: str
    name: str
    order: int
    description: str
    expected_events: List[str]
    outputs: List[str]


class RunManager:
    """
    Manages the lifecycle of agent runs.
    
    Responsibilities:
    - Create and track runs
    - Manage step progression
    - Emit standardized events
    - Store artifacts
    - Coordinate with event bus
    """
    
    _instance: Optional['RunManager'] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self.engine = create_engine(DATABASE_URL)
        self._step_registry = self._load_step_registry()
        self._topic_registry = self._load_topic_registry()
        
        logger.info("[RunManager] Initialized")
    
    def _load_step_registry(self) -> Dict:
        """Load step registry from JSON."""
        try:
            with open(REGISTRY_PATH / "step_registry.json") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[RunManager] Failed to load step registry: {e}")
            return {"agents": {}}
    
    def _load_topic_registry(self) -> Dict:
        """Load topic registry from JSON."""
        try:
            with open(REGISTRY_PATH / "topic_registry.json") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[RunManager] Failed to load topic registry: {e}")
            return {"topics": {}}
    
    def get_steps_for_agent(self, agent_type: str) -> List[StepDefinition]:
        """Get step definitions for an agent type."""
        agent_config = self._step_registry.get("agents", {}).get(agent_type, {})
        steps = agent_config.get("steps", [])
        
        return [
            StepDefinition(
                key=s["key"],
                name=s["name"],
                order=s["order"],
                description=s["description"],
                expected_events=s.get("expected_events", []),
                outputs=s.get("outputs", [])
            )
            for s in steps
        ]
    
    # =========================================================================
    # RUN MANAGEMENT
    # =========================================================================
    
    def create_run(
        self,
        agent_type: str,
        schedule_id: Optional[str] = None,
        context: Dict[str, Any] = None
    ) -> str:
        """Create a new run and initialize its steps."""
        run_id = str(uuid4())
        steps = self.get_steps_for_agent(agent_type)
        
        with self.engine.connect() as conn:
            # Create run
            conn.execute(text("""
                INSERT INTO agent_runs (id, agent_type, schedule_id, status, 
                    progress_total, root_context_json)
                VALUES (:id, :agent_type, :schedule_id, 'queued', 
                    :total, :context)
            """), {
                "id": run_id,
                "agent_type": agent_type,
                "schedule_id": schedule_id,
                "total": len(steps),
                "context": json.dumps(context or {})
            })
            
            # Create steps
            for step in steps:
                conn.execute(text("""
                    INSERT INTO agent_steps (id, run_id, step_key, step_name, step_order, status)
                    VALUES (:id, :run_id, :key, :name, :order, 'pending')
                """), {
                    "id": str(uuid4()),
                    "run_id": run_id,
                    "key": step.key,
                    "name": step.name,
                    "order": step.order
                })
            
            conn.commit()
        
        # Emit run.queued event
        self.emit_event(
            run_id=run_id,
            topic="shared.run.queued",
            event_type="run.queued",
            message=f"Run queued for {agent_type}",
            payload={"agent_type": agent_type, "steps": len(steps)}
        )
        
        logger.info(f"[RunManager] Created run {run_id} for {agent_type} with {len(steps)} steps")
        return run_id
    
    def start_run(self, run_id: str):
        """Mark a run as started."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE agent_runs 
                SET status = 'running', started_at = NOW(), last_heartbeat_at = NOW()
                WHERE id = :id
            """), {"id": run_id})
            conn.commit()
        
        self.emit_event(
            run_id=run_id,
            topic="shared.run.started",
            event_type="run.started",
            message="Run started"
        )
    
    def complete_run(self, run_id: str):
        """Mark a run as completed."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE agent_runs 
                SET status = 'succeeded', finished_at = NOW()
                WHERE id = :id
            """), {"id": run_id})
            conn.commit()
        
        self.emit_event(
            run_id=run_id,
            topic="shared.run.completed",
            event_type="run.completed",
            message="Run completed successfully"
        )
    
    def fail_run(self, run_id: str, error: str):
        """Mark a run as failed."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE agent_runs 
                SET status = 'failed', finished_at = NOW(), error_message = :error
                WHERE id = :id
            """), {"id": run_id, "error": error})
            conn.commit()
        
        self.emit_event(
            run_id=run_id,
            topic="shared.run.failed",
            event_type="run.failed",
            severity="error",
            message=f"Run failed: {error}",
            payload={"error": error}
        )
    
    def pause_run(self, run_id: str):
        """Pause a running run."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE agent_runs SET status = 'paused' WHERE id = :id
            """), {"id": run_id})
            conn.commit()
        
        self.emit_event(
            run_id=run_id,
            event_type="run.paused",
            message="Run paused"
        )
    
    def cancel_run(self, run_id: str):
        """Cancel a run."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE agent_runs 
                SET status = 'canceled', finished_at = NOW()
                WHERE id = :id
            """), {"id": run_id})
            conn.commit()
        
        self.emit_event(
            run_id=run_id,
            event_type="run.canceled",
            message="Run canceled"
        )
    
    def update_progress(self, run_id: str, current: int):
        """Update run progress."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE agent_runs 
                SET progress_current = :current, last_heartbeat_at = NOW()
                WHERE id = :id
            """), {"id": run_id, "current": current})
            conn.commit()
    
    def heartbeat(self, run_id: str):
        """Update run heartbeat."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE agent_runs SET last_heartbeat_at = NOW() WHERE id = :id
            """), {"id": run_id})
            conn.commit()
    
    # =========================================================================
    # STEP MANAGEMENT
    # =========================================================================
    
    def start_step(self, run_id: str, step_key: str, summary: str = None) -> str:
        """Start a step within a run."""
        step_id = None
        
        with self.engine.connect() as conn:
            # Get step id
            result = conn.execute(text("""
                SELECT id FROM agent_steps WHERE run_id = :run_id AND step_key = :key
            """), {"run_id": run_id, "key": step_key})
            row = result.fetchone()
            
            if row:
                step_id = str(row[0])
                conn.execute(text("""
                    UPDATE agent_steps 
                    SET status = 'running', started_at = NOW(), summary = :summary
                    WHERE id = :id
                """), {"id": step_id, "summary": summary})
                conn.commit()
        
        if step_id:
            self.emit_event(
                run_id=run_id,
                step_id=step_id,
                event_type="step.started",
                message=f"Step started: {step_key}",
                payload={"step_key": step_key}
            )
        
        return step_id
    
    def complete_step(self, run_id: str, step_key: str, summary: str = None):
        """Complete a step."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE agent_steps 
                SET status = 'completed', finished_at = NOW(), 
                    duration_ms = EXTRACT(EPOCH FROM (NOW() - started_at)) * 1000,
                    summary = COALESCE(:summary, summary)
                WHERE run_id = :run_id AND step_key = :key
                RETURNING id
            """), {"run_id": run_id, "key": step_key, "summary": summary})
            row = result.fetchone()
            conn.commit()
            
            if row:
                self.emit_event(
                    run_id=run_id,
                    step_id=str(row[0]),
                    event_type="step.completed",
                    message=f"Step completed: {step_key}",
                    payload={"step_key": step_key, "summary": summary}
                )
                
                # Update run progress
                completed = conn.execute(text("""
                    SELECT COUNT(*) FROM agent_steps 
                    WHERE run_id = :run_id AND status = 'completed'
                """), {"run_id": run_id}).scalar()
                self.update_progress(run_id, completed)
    
    def fail_step(self, run_id: str, step_key: str, error: str):
        """Fail a step."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                UPDATE agent_steps 
                SET status = 'failed', finished_at = NOW(), error_message = :error
                WHERE run_id = :run_id AND step_key = :key
                RETURNING id
            """), {"run_id": run_id, "key": step_key, "error": error})
            row = result.fetchone()
            conn.commit()
            
            if row:
                self.emit_event(
                    run_id=run_id,
                    step_id=str(row[0]),
                    event_type="step.failed",
                    severity="error",
                    message=f"Step failed: {step_key}",
                    payload={"step_key": step_key, "error": error}
                )
    
    def skip_step(self, run_id: str, step_key: str, reason: str = None):
        """Skip a step."""
        with self.engine.connect() as conn:
            conn.execute(text("""
                UPDATE agent_steps 
                SET status = 'skipped', summary = :reason
                WHERE run_id = :run_id AND step_key = :key
            """), {"run_id": run_id, "key": step_key, "reason": reason})
            conn.commit()
    
    # =========================================================================
    # EVENT EMISSION
    # =========================================================================
    
    def emit_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        topic: str = None,
        step_id: str = None,
        severity: str = "info",
        source_service: str = "RunManager",
        payload: Dict = None
    ):
        """Emit an event to the timeline."""
        event_id = str(uuid4())
        
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO agent_events (id, run_id, step_id, topic, event_type, 
                    severity, source_service, message, payload_json, ts)
                VALUES (:id, :run_id, :step_id, :topic, :event_type,
                    :severity, :source, :message, :payload, NOW())
            """), {
                "id": event_id,
                "run_id": run_id,
                "step_id": step_id,
                "topic": topic or f"agent.{event_type}",
                "event_type": event_type,
                "severity": severity,
                "source": source_service,
                "message": message,
                "payload": json.dumps(payload or {})
            })
            conn.commit()
        
        return event_id
    
    def emit_thought(self, run_id: str, step_id: str, thought: str, source: str = None):
        """Emit a thought event."""
        self.emit_event(
            run_id=run_id,
            step_id=step_id,
            event_type="thought.summary",
            message=thought,
            source_service=source or "AI"
        )
    
    def emit_decision(self, run_id: str, step_id: str, decision: str, rationale: str = None):
        """Emit a decision event."""
        self.emit_event(
            run_id=run_id,
            step_id=step_id,
            event_type="decision",
            message=decision,
            payload={"rationale": rationale}
        )
    
    def emit_action(self, run_id: str, step_id: str, action: str, details: Dict = None):
        """Emit an action event."""
        self.emit_event(
            run_id=run_id,
            step_id=step_id,
            event_type="action.performed",
            message=action,
            payload=details
        )
    
    # =========================================================================
    # ARTIFACT MANAGEMENT
    # =========================================================================
    
    def create_artifact(
        self,
        run_id: str,
        kind: str,
        name: str,
        content: Any = None,
        uri: str = None,
        step_id: str = None,
        metadata: Dict = None
    ) -> str:
        """Create an artifact for a run."""
        artifact_id = str(uuid4())
        
        with self.engine.connect() as conn:
            conn.execute(text("""
                INSERT INTO agent_artifacts (id, run_id, step_id, kind, name, 
                    content_json, uri, metadata_json)
                VALUES (:id, :run_id, :step_id, :kind, :name, 
                    :content, :uri, :metadata)
            """), {
                "id": artifact_id,
                "run_id": run_id,
                "step_id": step_id,
                "kind": kind,
                "name": name,
                "content": json.dumps(content) if content else None,
                "uri": uri,
                "metadata": json.dumps(metadata or {})
            })
            conn.commit()
        
        self.emit_event(
            run_id=run_id,
            step_id=step_id,
            event_type="artifact.created",
            message=f"Artifact created: {name}",
            payload={"kind": kind, "artifact_id": artifact_id}
        )
        
        return artifact_id
    
    # =========================================================================
    # QUERIES
    # =========================================================================
    
    def get_run(self, run_id: str) -> Optional[Dict]:
        """Get run details."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT r.*, s.schedule_name, s.topic
                FROM agent_runs r
                LEFT JOIN agent_schedules s ON r.schedule_id = s.id
                WHERE r.id = :id
            """), {"id": run_id})
            row = result.fetchone()
            
            if not row:
                return None
            
            return {
                "id": str(row[0]),
                "workspace_id": str(row[1]) if row[1] else None,
                "agent_type": row[2],
                "schedule_id": str(row[3]) if row[3] else None,
                "status": row[4],
                "progress_current": row[5],
                "progress_total": row[6],
                "started_at": row[7].isoformat() if row[7] else None,
                "finished_at": row[8].isoformat() if row[8] else None,
                "context": row[10],
                "schedule_name": row[12] if len(row) > 12 else None,
                "topic": row[13] if len(row) > 13 else None
            }
    
    def get_run_steps(self, run_id: str) -> List[Dict]:
        """Get steps for a run."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, step_key, step_name, step_order, status, 
                    started_at, finished_at, duration_ms, summary
                FROM agent_steps
                WHERE run_id = :run_id
                ORDER BY step_order
            """), {"run_id": run_id})
            
            return [
                {
                    "id": str(row[0]),
                    "step_key": row[1],
                    "step_name": row[2],
                    "step_order": row[3],
                    "status": row[4],
                    "started_at": row[5].isoformat() if row[5] else None,
                    "finished_at": row[6].isoformat() if row[6] else None,
                    "duration_ms": row[7],
                    "summary": row[8]
                }
                for row in result
            ]
    
    def get_run_timeline(self, run_id: str, limit: int = 100) -> List[Dict]:
        """Get event timeline for a run."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT e.id, e.step_id, s.step_name, e.ts, e.topic, 
                    e.event_type, e.severity, e.source_service, e.message, e.payload_json
                FROM agent_events e
                LEFT JOIN agent_steps s ON e.step_id = s.id
                WHERE e.run_id = :run_id
                ORDER BY e.ts DESC
                LIMIT :limit
            """), {"run_id": run_id, "limit": limit})
            
            return [
                {
                    "id": str(row[0]),
                    "step_id": str(row[1]) if row[1] else None,
                    "step_name": row[2],
                    "timestamp": row[3].isoformat() if row[3] else None,
                    "topic": row[4],
                    "event_type": row[5],
                    "severity": row[6],
                    "source_service": row[7],
                    "message": row[8],
                    "payload": row[9]
                }
                for row in result
            ]
    
    def get_run_artifacts(self, run_id: str) -> List[Dict]:
        """Get artifacts for a run."""
        with self.engine.connect() as conn:
            result = conn.execute(text("""
                SELECT id, kind, name, uri, content_json, metadata_json, created_at
                FROM agent_artifacts
                WHERE run_id = :run_id
                ORDER BY created_at
            """), {"run_id": run_id})
            
            return [
                {
                    "id": str(row[0]),
                    "kind": row[1],
                    "name": row[2],
                    "uri": row[3],
                    "content": row[4],
                    "metadata": row[5],
                    "created_at": row[6].isoformat() if row[6] else None
                }
                for row in result
            ]
    
    def get_recent_runs(self, agent_type: str = None, limit: int = 20) -> List[Dict]:
        """Get recent runs."""
        with self.engine.connect() as conn:
            query = """
                SELECT r.id, r.agent_type, r.status, r.progress_current, r.progress_total,
                    r.started_at, r.finished_at, r.created_at, s.schedule_name,
                    (SELECT message FROM agent_events WHERE run_id = r.id ORDER BY ts DESC LIMIT 1)
                FROM agent_runs r
                LEFT JOIN agent_schedules s ON r.schedule_id = s.id
            """
            params = {"limit": limit}
            
            if agent_type:
                query += " WHERE r.agent_type = :agent_type"
                params["agent_type"] = agent_type
            
            query += " ORDER BY r.created_at DESC LIMIT :limit"
            
            result = conn.execute(text(query), params)
            
            return [
                {
                    "id": str(row[0]),
                    "agent_type": row[1],
                    "status": row[2],
                    "progress_current": row[3],
                    "progress_total": row[4],
                    "started_at": row[5].isoformat() if row[5] else None,
                    "finished_at": row[6].isoformat() if row[6] else None,
                    "created_at": row[7].isoformat() if row[7] else None,
                    "schedule_name": row[8],
                    "last_event": row[9]
                }
                for row in result
            ]


# Global instance
def get_run_manager() -> RunManager:
    """Get the global run manager instance."""
    return RunManager()
