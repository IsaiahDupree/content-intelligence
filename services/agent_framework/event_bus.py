"""
Agent Event Bus - Pub/Sub Framework
=====================================
Central event bus for AI agent communication and monitoring.
Enables real-time tracking of agent thoughts, actions, and progress.
"""

import os
import asyncio
import logging
from typing import Dict, List, Callable, Any, Optional
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4
from collections import deque

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


class AgentType(Enum):
    NARRATIVE_PLANNER = "narrative_planner"
    EXPERIMENT_RUNNER = "experiment_runner"
    CONTENT_ANALYZER = "content_analyzer"
    SCHEDULER = "scheduler"


class EventType(Enum):
    # Lifecycle events
    AGENT_STARTED = "agent_started"
    AGENT_STOPPED = "agent_stopped"
    AGENT_ERROR = "agent_error"
    
    # Thinking events
    THOUGHT = "thought"
    REASONING = "reasoning"
    DECISION = "decision"
    
    # Action events
    ACTION_STARTED = "action_started"
    ACTION_COMPLETED = "action_completed"
    ACTION_FAILED = "action_failed"
    
    # Progress events
    CYCLE_STARTED = "cycle_started"
    CYCLE_COMPLETED = "cycle_completed"
    MILESTONE = "milestone"
    
    # State events
    STATE_CHANGED = "state_changed"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    
    # Data events
    DATA_FETCHED = "data_fetched"
    DATA_PROCESSED = "data_processed"
    PLAN_GENERATED = "plan_generated"
    EXPERIMENT_STARTED = "experiment_started"


@dataclass
class AgentEvent:
    """Event emitted by an AI agent."""
    id: str = field(default_factory=lambda: str(uuid4()))
    agent_type: AgentType = AgentType.NARRATIVE_PLANNER
    event_type: EventType = EventType.THOUGHT
    title: str = ""
    description: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "agent_type": self.agent_type.value,
            "event_type": self.event_type.value,
            "title": self.title,
            "description": self.description,
            "data": self.data,
            "timestamp": self.timestamp.isoformat()
        }


class AgentEventBus:
    """
    Central pub/sub event bus for AI agents.
    
    Topics:
    - agent.{agent_type}.events - All events from an agent
    - agent.{agent_type}.thoughts - Thoughts and reasoning
    - agent.{agent_type}.actions - Actions taken
    - agent.all - All agent events
    """
    
    _instance: Optional['AgentEventBus'] = None
    
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
        
        # Subscribers by topic
        self._subscribers: Dict[str, List[Callable]] = {}
        
        # Event history per agent (in-memory buffer)
        self._event_history: Dict[str, deque] = {}
        self._max_history = 100
        
        # Agent states
        self._agent_states: Dict[str, Dict[str, Any]] = {}
        
        logger.info("[EventBus] Agent Event Bus initialized")
    
    def subscribe(self, topic: str, callback: Callable[[AgentEvent], None]):
        """Subscribe to a topic."""
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        self._subscribers[topic].append(callback)
        logger.debug(f"[EventBus] Subscribed to {topic}")
    
    def unsubscribe(self, topic: str, callback: Callable):
        """Unsubscribe from a topic."""
        if topic in self._subscribers:
            self._subscribers[topic].remove(callback)
    
    async def publish(self, event: AgentEvent):
        """Publish an event to all relevant topics."""
        agent_key = event.agent_type.value
        
        # Store in history
        if agent_key not in self._event_history:
            self._event_history[agent_key] = deque(maxlen=self._max_history)
        self._event_history[agent_key].append(event)
        
        # Update agent state
        self._update_agent_state(event)
        
        # Persist to database
        await self._persist_event(event)
        
        # Notify subscribers
        topics = [
            f"agent.{agent_key}.events",
            f"agent.{agent_key}.{event.event_type.value}",
            "agent.all"
        ]
        
        for topic in topics:
            if topic in self._subscribers:
                for callback in self._subscribers[topic]:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(event)
                        else:
                            callback(event)
                    except Exception as e:
                        logger.error(f"[EventBus] Subscriber error: {e}")
        
        logger.debug(f"[EventBus] Published {event.event_type.value} from {agent_key}")
    
    def _update_agent_state(self, event: AgentEvent):
        """Update tracked agent state from event."""
        agent_key = event.agent_type.value
        
        if agent_key not in self._agent_states:
            self._agent_states[agent_key] = {
                "status": "unknown",
                "last_event": None,
                "cycle_count": 0,
                "error_count": 0,
                "started_at": None
            }
        
        state = self._agent_states[agent_key]
        state["last_event"] = event.to_dict()
        
        if event.event_type == EventType.AGENT_STARTED:
            state["status"] = "running"
            state["started_at"] = event.timestamp.isoformat()
        elif event.event_type == EventType.AGENT_STOPPED:
            state["status"] = "stopped"
        elif event.event_type == EventType.AGENT_ERROR:
            state["status"] = "error"
            state["error_count"] += 1
        elif event.event_type == EventType.CYCLE_COMPLETED:
            state["cycle_count"] += 1
        elif event.event_type == EventType.WAITING_APPROVAL:
            state["status"] = "awaiting_approval"
        elif event.event_type == EventType.STATE_CHANGED:
            state["status"] = event.data.get("new_state", state["status"])
    
    async def _persist_event(self, event: AgentEvent):
        """Persist event to database."""
        import json
        try:
            # Properly serialize event data to JSON
            event_data_json = json.dumps(event.data) if event.data else "{}"
            
            with self.engine.connect() as conn:
                conn.execute(text("""
                    INSERT INTO agent_events (
                        id, agent_type, event_type, title, description, 
                        event_data, created_at
                    ) VALUES (
                        :id, :agent_type, :event_type, :title, :description,
                        :event_data, :created_at
                    )
                """), {
                    "id": event.id,
                    "agent_type": event.agent_type.value,
                    "event_type": event.event_type.value,
                    "title": event.title,
                    "description": event.description,
                    "event_data": event_data_json,
                    "created_at": event.timestamp
                })
                conn.commit()
        except Exception as e:
            logger.warning(f"[EventBus] Failed to persist event: {e}")
    
    def get_agent_state(self, agent_type: AgentType) -> Dict[str, Any]:
        """Get current state of an agent."""
        return self._agent_states.get(agent_type.value, {
            "status": "not_started",
            "last_event": None
        })
    
    def get_agent_history(
        self,
        agent_type: AgentType,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get recent event history for an agent."""
        agent_key = agent_type.value
        if agent_key not in self._event_history:
            return []
        
        events = list(self._event_history[agent_key])[-limit:]
        return [e.to_dict() for e in events]
    
    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """Get states of all agents."""
        return self._agent_states.copy()
    
    async def get_timeline(
        self,
        agent_type: Optional[AgentType] = None,
        event_types: Optional[List[EventType]] = None,
        limit: int = 100,
        since: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Get event timeline, optionally filtered."""
        events = []
        
        # Get from in-memory history
        if agent_type:
            agent_key = agent_type.value
            if agent_key in self._event_history:
                events = list(self._event_history[agent_key])
        else:
            for history in self._event_history.values():
                events.extend(list(history))
        
        # Filter by event type
        if event_types:
            type_values = [et.value for et in event_types]
            events = [e for e in events if e.event_type.value in type_values]
        
        # Filter by time
        if since:
            events = [e for e in events if e.timestamp >= since]
        
        # Sort by timestamp
        events.sort(key=lambda e: e.timestamp, reverse=True)
        
        return [e.to_dict() for e in events[:limit]]


# Global event bus instance
def get_event_bus() -> AgentEventBus:
    """Get the global event bus instance."""
    return AgentEventBus()


# Helper class for agents to emit events
class AgentEventEmitter:
    """Helper class for agents to easily emit events."""
    
    def __init__(self, agent_type: AgentType):
        self.agent_type = agent_type
        self.bus = get_event_bus()
    
    async def emit(
        self,
        event_type: EventType,
        title: str,
        description: str = "",
        data: Dict[str, Any] = None
    ):
        """Emit an event."""
        event = AgentEvent(
            agent_type=self.agent_type,
            event_type=event_type,
            title=title,
            description=description,
            data=data or {}
        )
        await self.bus.publish(event)
    
    async def thought(self, title: str, description: str = ""):
        """Emit a thought event."""
        await self.emit(EventType.THOUGHT, title, description)
    
    async def action(self, title: str, description: str = "", data: Dict = None):
        """Emit an action event."""
        await self.emit(EventType.ACTION_STARTED, title, description, data)
    
    async def action_complete(self, title: str, result: Any = None):
        """Emit action completed event."""
        await self.emit(EventType.ACTION_COMPLETED, title, data={"result": result})
    
    async def milestone(self, title: str, description: str = ""):
        """Emit a milestone event."""
        await self.emit(EventType.MILESTONE, title, description)
    
    async def state_change(self, old_state: str, new_state: str):
        """Emit state change event."""
        await self.emit(
            EventType.STATE_CHANGED,
            f"State: {old_state} → {new_state}",
            data={"old_state": old_state, "new_state": new_state}
        )
    
    async def error(self, title: str, error: str):
        """Emit error event."""
        await self.emit(EventType.AGENT_ERROR, title, error)
