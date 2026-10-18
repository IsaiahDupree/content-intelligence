import asyncio
from typing import List, Dict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

class SegmentEngine:
    """
    Manages user segments (dynamic and static).
    """
    
    async def refresh_dynamic_segments(self, db_session: AsyncSession):
        """
        Re-evaluate membership for all dynamic segments.
        """
        # 1. Fetch all dynamic segments
        segments = await self._fetch_dynamic_segments(db_session)
        
        for segment in segments:
            await self._update_segment_membership(db_session, segment)

    async def _fetch_dynamic_segments(self, db_session: AsyncSession) -> List[Dict]:
        result = await db_session.execute(
            text("SELECT id, definition FROM segments WHERE is_dynamic = true")
        )
        return [dict(row._mapping) for row in result]

    async def _update_segment_membership(self, db_session: AsyncSession, segment: Dict):
        segment_id = segment['id']
        definition = segment['definition'] # e.g. {"min_warmth": 50, "activity_state": "active"}
        
        # Construct query based on definition (simplified DSL)
        # In a real app, this would be a robust query builder
        
        query_parts = ["SELECT id FROM people p JOIN person_insights pi ON p.id = pi.person_id WHERE 1=1"]
        params = {}
        
        if 'min_warmth' in definition:
            query_parts.append("AND pi.warmth_score >= :min_warmth")
            params['min_warmth'] = definition['min_warmth']
            
        if 'activity_state' in definition:
            query_parts.append("AND pi.activity_state = :state")
            params['state'] = definition['activity_state']
            
        query = " ".join(query_parts)
        
        # Execute query to find matching people
        result = await db_session.execute(text(query), params)
        matching_person_ids = [row.id for row in result]
        
        # Update segment_members table
        # Full refresh strategy: Delete all and re-insert (or diff)
        # For simplicity: Delete all for this segment and re-insert
        
        await db_session.execute(
            text("DELETE FROM segment_members WHERE segment_id = :sid"),
            {"sid": segment_id}
        )
        
        if matching_person_ids:
            values = [{"sid": segment_id, "pid": pid} for pid in matching_person_ids]
            await db_session.execute(
                text("INSERT INTO segment_members (segment_id, person_id) VALUES (:sid, :pid)"),
                values
            )
            
        await db_session.commit()
        print(f"Updated segment {segment_id}: {len(matching_person_ids)} members")
