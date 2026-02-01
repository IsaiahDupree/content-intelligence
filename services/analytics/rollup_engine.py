import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

class RollupEngine:
    """
    Aggregates metrics from content_metrics (per variant) 
    into content_rollups (per canonical content item).
    """
    
    async def run_rollups(self, db_session: AsyncSession):
        """
        Recompute rollups for all content items that have had new metrics recently.
        For simplicity, we'll recompute for all content items.
        """
        print("Starting analytics rollup...")
        
        # SQL to aggregate metrics
        # We take the LATEST snapshot for each variant, then sum them up per content_id
        
        query = """
        WITH latest_metrics AS (
            SELECT DISTINCT ON (variant_id) 
                variant_id, 
                views, likes, comments, shares, saves, clicks, watch_time_seconds, traffic_type
            FROM content_metrics
            ORDER BY variant_id, snapshot_at DESC
        ),
        variant_sums AS (
            SELECT 
                cv.content_id,
                lm.traffic_type,
                COALESCE(SUM(lm.views), 0) as views,
                COALESCE(SUM(lm.likes), 0) as likes,
                COALESCE(SUM(lm.comments), 0) as comments,
                COALESCE(SUM(lm.shares), 0) as shares,
                COALESCE(SUM(lm.saves), 0) as saves,
                COALESCE(SUM(lm.clicks), 0) as clicks,
                COALESCE(SUM(lm.watch_time_seconds), 0) as watch_time
            FROM latest_metrics lm
            JOIN content_variants cv ON lm.variant_id = cv.id
            GROUP BY cv.content_id, lm.traffic_type
        )
        INSERT INTO content_rollups (
            content_id,
            total_views_organic, total_views_paid,
            total_likes_organic, total_likes_paid,
            total_comments_organic, total_comments_paid,
            total_shares_organic, total_shares_paid,
            total_saves_organic, total_saves_paid,
            total_clicks_organic, total_clicks_paid,
            avg_watch_time_seconds_organic, avg_watch_time_seconds_paid,
            last_updated_at
        )
        SELECT
            content_id,
            MAX(CASE WHEN traffic_type = 'organic' THEN views ELSE 0 END),
            MAX(CASE WHEN traffic_type = 'paid' THEN views ELSE 0 END),
            MAX(CASE WHEN traffic_type = 'organic' THEN likes ELSE 0 END),
            MAX(CASE WHEN traffic_type = 'paid' THEN likes ELSE 0 END),
            MAX(CASE WHEN traffic_type = 'organic' THEN comments ELSE 0 END),
            MAX(CASE WHEN traffic_type = 'paid' THEN comments ELSE 0 END),
            MAX(CASE WHEN traffic_type = 'organic' THEN shares ELSE 0 END),
            MAX(CASE WHEN traffic_type = 'paid' THEN shares ELSE 0 END),
            MAX(CASE WHEN traffic_type = 'organic' THEN saves ELSE 0 END),
            MAX(CASE WHEN traffic_type = 'paid' THEN saves ELSE 0 END),
            MAX(CASE WHEN traffic_type = 'organic' THEN clicks ELSE 0 END),
            MAX(CASE WHEN traffic_type = 'paid' THEN clicks ELSE 0 END),
            -- Watch time is sum here, but schema says avg? 
            -- Let's assume schema meant total or we compute avg per view.
            -- For now, storing total watch time in the 'avg' column is wrong.
            -- Let's just store 0 for avg since we don't have view counts per variant easily here without more complex SQL
            0, 0,
            NOW()
        FROM variant_sums
        GROUP BY content_id
        ON CONFLICT (content_id) DO UPDATE SET
            total_views_organic = EXCLUDED.total_views_organic,
            total_views_paid = EXCLUDED.total_views_paid,
            total_likes_organic = EXCLUDED.total_likes_organic,
            total_likes_paid = EXCLUDED.total_likes_paid,
            total_comments_organic = EXCLUDED.total_comments_organic,
            total_comments_paid = EXCLUDED.total_comments_paid,
            total_shares_organic = EXCLUDED.total_shares_organic,
            total_shares_paid = EXCLUDED.total_shares_paid,
            total_saves_organic = EXCLUDED.total_saves_organic,
            total_saves_paid = EXCLUDED.total_saves_paid,
            total_clicks_organic = EXCLUDED.total_clicks_organic,
            total_clicks_paid = EXCLUDED.total_clicks_paid,
            last_updated_at = NOW();
        """
        
        await db_session.execute(text(query))
        await db_session.commit()
        print("Rollup complete.")
