import asyncio
from typing import Dict, Any, List
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

class PersonInsightsService:
    """
    Computes and updates insights for people based on their event history.
    """
    
    async def update_insights_for_person(self, db_session: AsyncSession, person_id: str):
        """
        Recompute insights for a single person and update the person_insights table.
        """
        # 1. Fetch recent events
        events = await self._fetch_person_events(db_session, person_id)
        
        if not events:
            return

        # 2. Compute metrics
        warmth_score = self._calculate_warmth_score(events)
        activity_state = self._determine_activity_state(events)
        channel_prefs = self._calculate_channel_preferences(events)
        
        # 3. Update database
        await self._save_insights(
            db_session, 
            person_id, 
            warmth_score, 
            activity_state, 
            channel_prefs
        )

    async def _fetch_person_events(self, db_session: AsyncSession, person_id: str) -> List[Dict]:
        result = await db_session.execute(
            text("""
                SELECT event_type, channel, occurred_at, sentiment 
                FROM person_events 
                WHERE person_id = :pid 
                ORDER BY occurred_at DESC
                LIMIT 100
            """),
            {"pid": person_id}
        )
        return [dict(row._mapping) for row in result]

    def _calculate_warmth_score(self, events: List[Dict]) -> float:
        """
        Simple heuristic for warmth score (0-100).
        Based on recency and type of interaction.
        """
        score = 0.0
        now = datetime.now() # In real app, use timezone aware
        
        for event in events:
            # Decay factor: events older than 30 days have less weight
            days_ago = (now - event['occurred_at'].replace(tzinfo=None)).days
            if days_ago > 90:
                continue
                
            decay = max(0.1, 1.0 - (days_ago / 30.0))
            
            # Event weights
            weight = 1.0
            etype = event['event_type']
            if etype in ['comment', 'dm_reply']:
                weight = 5.0
            elif etype in ['click', 'save']:
                weight = 3.0
            elif etype in ['like', 'open']:
                weight = 1.0
                
            score += (weight * decay)
            
        return min(100.0, score)

    def _determine_activity_state(self, events: List[Dict]) -> str:
        if not events:
            return 'dormant'
            
        last_event = events[0]['occurred_at'].replace(tzinfo=None)
        days_since_active = (datetime.now() - last_event).days
        
        if days_since_active < 7:
            return 'active'
        elif days_since_active < 30:
            return 'warming'
        elif days_since_active < 90:
            return 'cool'
        else:
            return 'dormant'

    def _calculate_channel_preferences(self, events: List[Dict]) -> Dict[str, float]:
        counts = {}
        total = 0
        for e in events:
            ch = e['channel']
            counts[ch] = counts.get(ch, 0) + 1
            total += 1
            
        if total == 0:
            return {}
            
        return {k: round(v / total, 2) for k, v in counts.items()}

    async def _save_insights(
        self, 
        db_session: AsyncSession, 
        person_id: str,
        warmth: float,
        state: str,
        prefs: Dict
    ):
        # Upsert into person_insights
        await db_session.execute(
            text("""
                INSERT INTO person_insights (
                    person_id, warmth_score, activity_state, channel_preferences, updated_at
                ) VALUES (
                    :pid, :warmth, :state, :prefs, NOW()
                )
                ON CONFLICT (person_id) DO UPDATE SET
                    warmth_score = EXCLUDED.warmth_score,
                    activity_state = EXCLUDED.activity_state,
                    channel_preferences = EXCLUDED.channel_preferences,
                    updated_at = NOW()
            """),
            {
                "pid": person_id,
                "warmth": warmth,
                "state": state,
                "prefs": str(prefs).replace("'", '"') # Simple JSON conversion for now
            }
        )
        await db_session.commit()
