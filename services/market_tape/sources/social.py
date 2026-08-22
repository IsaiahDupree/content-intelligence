"""TikTok, Instagram, X, Facebook, and Threads acquisition adapters."""

from __future__ import annotations

import os
import re
from datetime import timedelta
from typing import Any, Dict, Iterable, List, Sequence

from .base import MarketSource, SourceCredentialError
from ..models import MarketContent, MetricCounters, ProviderBatch, QueryAttempt, parse_datetime, utc_now
from ..source_urls import normalize_tiktok_handle, normalize_tiktok_source_url


HASHTAG_RE = re.compile(r"#([\w-]+)", re.UNICODE)


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _hashtags(text: str, supplied: Iterable[Any] = ()) -> List[str]:
    values = [str(value.get("hashtagName") or value.get("name") or "") if isinstance(value, dict) else str(value) for value in supplied]
    values.extend(HASHTAG_RE.findall(text or ""))
    return sorted({value.strip().lstrip("#").lower() for value in values if value.strip().lstrip("#")})


def _first_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


class TikTokResearchSource(MarketSource):
    source_id = "tiktok-research-api-v2"
    platform = "tiktok"

    def __init__(self, *args: Any, base_url: str = "https://open.tiktokapis.com", **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.access_token = os.getenv("TIKTOK_RESEARCH_ACCESS_TOKEN", "").strip()
        self._configured_access_token = self.access_token
        self.client_key = os.getenv("TIKTOK_CLIENT_KEY", "").strip()
        self.client_secret = os.getenv("TIKTOK_CLIENT_SECRET", "").strip()

    def credentials_available(self) -> bool:
        return bool(self.access_token or (self.client_key and self.client_secret))

    def missing_credentials(self) -> List[str]:
        if self.credentials_available():
            return []
        return ["TIKTOK_RESEARCH_ACCESS_TOKEN or TIKTOK_CLIENT_KEY+TIKTOK_CLIENT_SECRET"]

    def credential_material(self) -> Sequence[str]:
        if self._configured_access_token:
            return (self._configured_access_token,)
        return (self.client_key, self.client_secret)

    def _token(self) -> str:
        if self.access_token:
            return self.access_token
        if not self.client_key or not self.client_secret:
            raise SourceCredentialError("TikTok Research credentials are unavailable")
        data = self.request_json("POST", f"{self.base_url}/v2/oauth/token/", form={
            "client_key": self.client_key,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        })
        self.access_token = str(data.get("access_token", ""))
        if not self.access_token:
            raise SourceCredentialError("TikTok did not issue a research access token")
        return self.access_token

    def discover(self, max_items: int) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            token = self._token()
            observed = utc_now()
            # Research data is intentionally queried behind the real-time edge.
            end = (observed - timedelta(days=2)).date()
            start = end - timedelta(days=1)
            output: Dict[str, MarketContent] = {}
            query_counts: Dict[str, int] = {}
            cursor = ""
            for topic in self.config.topics:
                if len(output) >= max_items or self.request_count >= self.request_budget:
                    break
                data = self.request_json(
                    "POST",
                    f"{self.base_url}/v2/research/video/query/",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    params={"fields": "id,video_description,create_time,region_code,share_count,view_count,like_count,comment_count,music_id,hashtag_names,username,voice_to_text,favorites_count,video_duration"},
                    json_body={
                        "query": {"and": [{"operation": "EQ", "field_name": "keyword", "field_values": [topic]}]},
                        "max_count": min(100, max_items - len(output)),
                        "cursor": 0,
                        "start_date": start.strftime("%Y%m%d"),
                        "end_date": end.strftime("%Y%m%d"),
                        "is_random": True,
                    },
                )
                result = _first_dict(data.get("data"))
                cursor = str(result.get("cursor", ""))
                videos = result.get("videos", [])
                query_counts[topic] = len(videos) if isinstance(videos, list) else 0
                for raw in videos:
                    if isinstance(raw, dict) and raw.get("id"):
                        item = self._normalize(raw, observed, {"topic": topic})
                        output[item.external_id] = item
            batch = self.success_batch(started, list(output.values()), operation="discover", cursor=cursor, metadata={"data_lag_days": 2})
            batch.query_attempts = _query_attempts(
                self, started, batch.receipt.finished_at, query_counts,
                metadata={"data_lag_days": 2},
            )
            return batch
        except Exception as error:
            return self.blocked_batch(started, error)

    def refresh(self, tracked: Sequence[Dict[str, Any]]) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            token = self._token()
            observed = utc_now()
            output: List[MarketContent] = []
            for offset in range(0, len(tracked), 100):
                rows = tracked[offset:offset + 100]
                data = self.request_json(
                    "POST",
                    f"{self.base_url}/v2/research/video/query/",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    params={"fields": "id,video_description,create_time,region_code,share_count,view_count,like_count,comment_count,music_id,hashtag_names,username,voice_to_text,favorites_count,video_duration"},
                    json_body={
                        "query": {"and": [{"operation": "IN", "field_name": "video_id", "field_values": [str(row["external_id"]) for row in rows]}]},
                        "max_count": len(rows), "cursor": 0,
                        "start_date": (observed - timedelta(days=30)).strftime("%Y%m%d"),
                        "end_date": (observed - timedelta(days=2)).strftime("%Y%m%d"),
                    },
                )
                prior = {str(row["external_id"]): row for row in rows}
                for raw in _first_dict(data.get("data")).get("videos", []):
                    if isinstance(raw, dict) and raw.get("id"):
                        output.append(self._normalize(raw, observed, prior.get(str(raw["id"]), {})))
            return self.success_batch(started, output, operation="refresh")
        except Exception as error:
            return self.blocked_batch(started, error)

    def _normalize(self, raw: Dict[str, Any], observed: Any, prior: Dict[str, Any]) -> MarketContent:
        external_id = str(raw.get("id"))
        username = normalize_tiktok_handle(
            raw.get("username"),
            prior.get("creator_handle"),
            prior.get("creator_external_id"),
            source_url=prior.get("url"),
        )
        text = str(raw.get("video_description") or prior.get("caption") or "")
        return MarketContent(
            platform=self.platform, external_id=external_id, creator_external_id=username or "unknown",
            creator_handle=username, published_at=parse_datetime(raw.get("create_time") or prior.get("published_at")),
            observed_at=observed, source_id=self.source_id,
            metrics=MetricCounters.from_values(
                views=raw.get("view_count"), likes=raw.get("like_count"), comments=raw.get("comment_count"),
                shares=raw.get("share_count"), saves=raw.get("favorites_count"),
            ),
            caption=text, description=str(raw.get("voice_to_text") or ""),
            url=normalize_tiktok_source_url(
                prior.get("url"), external_id, username,
            ),
            duration_seconds=raw.get("video_duration"), hashtags=_hashtags(text, raw.get("hashtag_names", [])),
            audio_id=str(raw.get("music_id") or ""), raw_payload=raw, discovery_context=prior,
        )


class TikTokRapidSource(MarketSource):
    source_id = "tiktok-rapidapi-discovery"
    platform = "tiktok"
    metered = True
    credential_names = ("RAPIDAPI_KEY",)

    def __init__(self, *args: Any, base_url: str = "https://tiktok-scraper7.p.rapidapi.com", **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.host = self.base_url.split("//", 1)[-1].split("/", 1)[0]

    def _headers(self) -> Dict[str, str]:
        return {"X-RapidAPI-Key": os.getenv("RAPIDAPI_KEY", ""), "X-RapidAPI-Host": self.host}

    def discover(self, max_items: int) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            observed = utc_now()
            output: Dict[str, MarketContent] = {}
            query_counts: Dict[str, int] = {}
            for region in self.config.regions[:2]:
                if len(output) >= max_items:
                    break
                data = self.request_json("GET", f"{self.base_url}/feed/list", headers=self._headers(), params={"region": region, "count": "30"})
                self._add_payload(output, data, observed, {"surface": "feed", "region": region})
            for topic in self.config.topics:
                if len(output) >= max_items or self.request_count >= self.request_budget:
                    break
                tag = re.sub(r"[^a-z0-9]", "", topic.lower())
                data = self.request_json("GET", f"{self.base_url}/challenge/posts", headers=self._headers(), params={"challenge_name": tag, "count": "30"})
                query_counts[topic] = self._add_payload(
                    output, data, observed, {"surface": "hashtag", "topic": topic}
                )
            batch = self.success_batch(started, list(output.values())[:max_items], operation="discover", metadata={"billing": "external_rapidapi_plan"})
            batch.query_attempts = _query_attempts(
                self, started, batch.receipt.finished_at, query_counts,
                metadata={"surface": "hashtag"},
            )
            return batch
        except Exception as error:
            return self.blocked_batch(started, error)

    def refresh(self, tracked: Sequence[Dict[str, Any]]) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            observed = utc_now()
            output: List[MarketContent] = []
            for prior in tracked:
                if self.request_count >= self.request_budget:
                    break
                data = self.request_json("GET", f"{self.base_url}/video/info", headers=self._headers(), params={"video_id": prior["external_id"]})
                raw = _first_dict(data.get("data"))
                if raw:
                    output.append(self._normalize(raw, observed, prior))
            return self.success_batch(started, output, operation="refresh", metadata={"billing": "external_rapidapi_plan"})
        except Exception as error:
            return self.blocked_batch(started, error)

    def _add_payload(self, output: Dict[str, MarketContent], data: Dict[str, Any], observed: Any, context: Dict[str, Any]) -> int:
        payload = data.get("data", [])
        if isinstance(payload, dict):
            payload = payload.get("videos", payload.get("items", []))
        count = 0
        for raw in payload if isinstance(payload, list) else []:
            if isinstance(raw, dict):
                item = self._normalize(raw, observed, context)
                if item.external_id:
                    output[item.external_id] = item
                    count += 1
        return count

    def _normalize(self, raw: Dict[str, Any], observed: Any, prior: Dict[str, Any]) -> MarketContent:
        external_id = str(raw.get("video_id") or raw.get("aweme_id") or raw.get("id") or "")
        author = _first_dict(raw.get("author"))
        username = normalize_tiktok_handle(
            author.get("unique_id"),
            author.get("uniqueId"),
            raw.get("author"),
            prior.get("creator_handle"),
            prior.get("creator_external_id"),
            source_url=prior.get("url"),
        )
        creator_external_id = normalize_tiktok_handle(author.get("id"), username) or "unknown"
        stats = _first_dict(raw.get("stats"))
        text = str(raw.get("title") or raw.get("desc") or prior.get("caption") or "")
        music = _first_dict(raw.get("music_info") or raw.get("music"))
        return MarketContent(
            platform=self.platform, external_id=external_id, creator_external_id=creator_external_id,
            creator_handle=username, creator_name=str(author.get("nickname") or ""),
            creator_followers=_int(author.get("follower_count") or author.get("followerCount")),
            published_at=parse_datetime(raw.get("create_time") or raw.get("createTime") or prior.get("published_at")),
            observed_at=observed, source_id=self.source_id,
            metrics=MetricCounters.from_values(
                views=raw.get("play_count") or raw.get("playCount") or stats.get("playCount"),
                likes=raw.get("digg_count") or raw.get("diggCount") or stats.get("diggCount"),
                comments=raw.get("comment_count") or raw.get("commentCount") or stats.get("commentCount"),
                shares=raw.get("share_count") or raw.get("shareCount") or stats.get("shareCount"),
                saves=raw.get("collect_count") or raw.get("collectCount") or stats.get("collectCount"),
            ),
            caption=text, url=normalize_tiktok_source_url(
                prior.get("url"), external_id, username,
            ),
            thumbnail_url=str(raw.get("cover") or raw.get("originCover") or ""),
            duration_seconds=raw.get("duration") or _first_dict(raw.get("video")).get("duration"),
            hashtags=_hashtags(text, raw.get("textExtra", [])), audio_id=str(music.get("id") or ""),
            audio_title=str(music.get("title") or ""), raw_payload=raw, discovery_context=prior,
        )


class InstagramRapidSource(MarketSource):
    source_id = "instagram-rapidapi-discovery"
    platform = "instagram"
    metered = True
    credential_names = ("RAPIDAPI_KEY",)

    def __init__(self, *args: Any, base_url: str = "https://instagram-looter2.p.rapidapi.com", **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.host = self.base_url.split("//", 1)[-1].split("/", 1)[0]

    def _headers(self) -> Dict[str, str]:
        return {"X-RapidAPI-Key": os.getenv("RAPIDAPI_KEY", ""), "X-RapidAPI-Host": self.host}

    def discover(self, max_items: int) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            observed = utc_now()
            output: Dict[str, MarketContent] = {}
            query_counts: Dict[str, int] = {}
            for topic in self.config.topics:
                if len(output) >= max_items or self.request_count >= self.request_budget:
                    break
                tag = re.sub(r"[^a-z0-9]", "", topic.lower())
                data = self.request_json("GET", f"{self.base_url}/v1/hashtag", headers=self._headers(), params={"tag": tag})
                payload = data.get("items", data.get("data", []))
                if isinstance(payload, dict):
                    payload = payload.get("top_posts", []) + payload.get("recent_posts", [])
                query_counts[topic] = len(payload) if isinstance(payload, list) else 0
                for raw in payload if isinstance(payload, list) else []:
                    if isinstance(raw, dict) and isinstance(raw.get("node"), dict):
                        raw = raw["node"]
                    if isinstance(raw, dict):
                        item = self._normalize(raw, observed, {"topic": topic})
                        output[item.external_id] = item
            batch = self.success_batch(started, list(output.values())[:max_items], operation="discover", metadata={"billing": "external_rapidapi_plan"})
            batch.query_attempts = _query_attempts(
                self, started, batch.receipt.finished_at, query_counts,
                metadata={"surface": "hashtag"},
            )
            return batch
        except Exception as error:
            return self.blocked_batch(started, error)

    def refresh(self, tracked: Sequence[Dict[str, Any]]) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            observed = utc_now()
            output: List[MarketContent] = []
            for prior in tracked:
                if self.request_count >= self.request_budget:
                    break
                shortcode = prior.get("shortcode") or str(prior.get("url", "")).strip("/").split("/")[-1]
                if not shortcode:
                    continue
                data = self.request_json("GET", f"{self.base_url}/post-info", headers=self._headers(), params={"code": shortcode})
                raw = _first_dict(data.get("item") or data.get("data") or data)
                if raw:
                    output.append(self._normalize(raw, observed, prior))
            return self.success_batch(started, output, operation="refresh", metadata={"billing": "external_rapidapi_plan"})
        except Exception as error:
            return self.blocked_batch(started, error)

    def _normalize(self, raw: Dict[str, Any], observed: Any, prior: Dict[str, Any]) -> MarketContent:
        external_id = str(raw.get("pk") or raw.get("id") or raw.get("shortcode") or raw.get("code") or "")
        user = _first_dict(raw.get("user") or raw.get("owner"))
        username = str(user.get("username") or prior.get("creator_handle") or "unknown")
        caption_value = raw.get("caption", "")
        text = str(caption_value.get("text", "") if isinstance(caption_value, dict) else caption_value or raw.get("edge_media_to_caption", {}).get("edges", [{}])[0].get("node", {}).get("text", ""))
        shortcode = str(raw.get("code") or raw.get("shortcode") or prior.get("shortcode") or "")
        music = _first_dict(raw.get("music_info") or _first_dict(raw.get("clips_metadata")).get("music_info"))
        return MarketContent(
            platform=self.platform, external_id=external_id, creator_external_id=str(user.get("pk") or user.get("id") or username),
            creator_handle=username, creator_name=str(user.get("full_name") or ""),
            creator_followers=_int(user.get("follower_count")),
            published_at=parse_datetime(raw.get("taken_at") or raw.get("taken_at_timestamp") or prior.get("published_at")),
            observed_at=observed, source_id=self.source_id,
            metrics=MetricCounters.from_values(
                views=raw.get("play_count") or raw.get("view_count") or raw.get("video_view_count"),
                likes=raw.get("like_count") or _first_dict(raw.get("edge_liked_by")).get("count"),
                comments=raw.get("comment_count") or _first_dict(raw.get("edge_media_to_comment")).get("count"),
                shares=raw.get("reshare_count"), saves=raw.get("save_count"),
            ),
            caption=text, url=f"https://www.instagram.com/p/{shortcode}/" if shortcode else str(prior.get("url") or ""),
            thumbnail_url=str(raw.get("thumbnail_url") or raw.get("display_url") or raw.get("image_versions2", {}).get("candidates", [{}])[0].get("url", "")),
            media_type="video" if raw.get("is_video") or raw.get("media_type") == 2 else "image",
            duration_seconds=raw.get("video_duration"), hashtags=_hashtags(text),
            audio_id=str(music.get("audio_id") or ""), audio_title=str(music.get("title") or ""),
            raw_payload=raw, discovery_context={**prior, "shortcode": shortcode},
        )


class XRecentSearchSource(MarketSource):
    source_id = "x-api-v2-recent-search"
    platform = "x"
    metered = True

    def __init__(self, *args: Any, base_url: str = "https://api.x.com/2", **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.base_url = base_url.rstrip("/")
        self.token = os.getenv("X_BEARER_TOKEN", "").strip() or os.getenv("TWITTER_BEARER_TOKEN", "").strip()

    def credentials_available(self) -> bool:
        return bool(self.token)

    def missing_credentials(self) -> List[str]:
        return [] if self.token else ["X_BEARER_TOKEN or TWITTER_BEARER_TOKEN"]

    def credential_material(self) -> Sequence[str]:
        return (self.token,)

    def discover(self, max_items: int) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            observed = utc_now()
            output: Dict[str, MarketContent] = {}
            topics = self.config.topics
            query_counts: Dict[str, int] = {}
            for offset in range(0, len(topics), 3):
                if len(output) >= max_items or self.request_count >= self.request_budget:
                    break
                query_group = topics[offset:offset + 3]
                terms = " OR ".join(f'"{topic}"' if " " in topic else topic for topic in query_group)
                data = self.request_json("GET", f"{self.base_url}/tweets/search/recent", headers={"Authorization": f"Bearer {self.token}"}, params={
                    "query": f"({terms}) has:media -is:retweet",
                    "max_results": min(100, max(10, max_items - len(output))),
                    "tweet.fields": "created_at,public_metrics,author_id,lang,entities,attachments",
                    "expansions": "author_id",
                    "user.fields": "id,name,username,public_metrics",
                })
                users = {str(user.get("id")): user for user in _first_dict(data.get("includes")).get("users", []) if isinstance(user, dict)}
                records = data.get("data", [])
                for topic in query_group:
                    query_counts[topic] = len(records) if isinstance(records, list) else 0
                for raw in records:
                    if isinstance(raw, dict) and raw.get("id"):
                        item = self._normalize(raw, users.get(str(raw.get("author_id")), {}), observed)
                        output[item.external_id] = item
            batch = self.success_batch(started, list(output.values())[:max_items], operation="discover", metadata={"billing": "x_api_plan"})
            batch.query_attempts = _query_attempts(
                self, started, batch.receipt.finished_at, query_counts,
                metadata={"surface": "recent_search_or_group"},
            )
            return batch
        except Exception as error:
            return self.blocked_batch(started, error)

    def refresh(self, tracked: Sequence[Dict[str, Any]]) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            observed = utc_now()
            output: List[MarketContent] = []
            for offset in range(0, len(tracked), 100):
                rows = tracked[offset:offset + 100]
                data = self.request_json("GET", f"{self.base_url}/tweets", headers={"Authorization": f"Bearer {self.token}"}, params={
                    "ids": ",".join(str(row["external_id"]) for row in rows),
                    "tweet.fields": "created_at,public_metrics,author_id,lang,entities,attachments",
                    "expansions": "author_id", "user.fields": "id,name,username,public_metrics",
                })
                users = {str(user.get("id")): user for user in _first_dict(data.get("includes")).get("users", []) if isinstance(user, dict)}
                for raw in data.get("data", []):
                    if isinstance(raw, dict):
                        output.append(self._normalize(raw, users.get(str(raw.get("author_id")), {}), observed))
            return self.success_batch(started, output, operation="refresh", metadata={"billing": "x_api_plan"})
        except Exception as error:
            return self.blocked_batch(started, error)

    def _normalize(self, raw: Dict[str, Any], user: Dict[str, Any], observed: Any) -> MarketContent:
        metrics = _first_dict(raw.get("public_metrics"))
        username = str(user.get("username") or raw.get("author_id") or "unknown")
        text = str(raw.get("text") or "")
        entities = _first_dict(raw.get("entities"))
        return MarketContent(
            platform=self.platform, external_id=str(raw.get("id")),
            creator_external_id=str(raw.get("author_id") or username), creator_handle=username,
            creator_name=str(user.get("name") or ""),
            creator_followers=_int(_first_dict(user.get("public_metrics")).get("followers_count")),
            published_at=parse_datetime(raw.get("created_at")), observed_at=observed, source_id=self.source_id,
            metrics=MetricCounters.from_values(
                views=metrics.get("impression_count"), likes=metrics.get("like_count"),
                comments=metrics.get("reply_count"), shares=_int(metrics.get("retweet_count")) + _int(metrics.get("quote_count")),
                saves=metrics.get("bookmark_count"),
            ),
            caption=text, language=str(raw.get("lang") or ""), url=f"https://x.com/{username}/status/{raw.get('id')}",
            hashtags=_hashtags(text, entities.get("hashtags", [])), raw_payload={"tweet": raw, "user": user},
        )


class ThreadsKeywordSearchSource(MarketSource):
    """Public Threads discovery and object refresh through the Threads Graph API."""

    source_id = "threads-graph-keyword-search"
    platform = "threads"
    credential_names = ("THREADS_ACCESS_TOKEN",)

    fields = (
        "id,media_product_type,media_type,media_url,permalink,owner,username,"
        "text,timestamp,shortcode,thumbnail_url,is_quote_post"
    )

    def __init__(
        self,
        *args: Any,
        base_url: str = "https://graph.threads.net",
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.base_url = base_url.rstrip("/")

    @property
    def token(self) -> str:
        return os.getenv("THREADS_ACCESS_TOKEN", "").strip()

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def discover(self, max_items: int) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            observed = utc_now()
            output: Dict[str, MarketContent] = {}
            query_counts: Dict[str, int] = {}
            cursor = ""
            for topic in self.config.topics:
                if len(output) >= max_items or self.request_count >= self.request_budget:
                    break
                data = self.request_json(
                    "GET",
                    f"{self.base_url}/keyword_search",
                    headers=self._headers(),
                    params={
                        "q": topic,
                        "search_type": "RECENT",
                        "search_mode": "KEYWORD",
                        "limit": min(50, max_items - len(output)),
                        "fields": self.fields,
                    },
                )
                records = data.get("data", [])
                query_counts[topic] = len(records) if isinstance(records, list) else 0
                for raw in records if isinstance(records, list) else []:
                    if not isinstance(raw, dict) or not raw.get("id"):
                        continue
                    item = self._normalize(raw, observed, {
                        "topic": topic,
                        "search_type": "RECENT",
                        "search_mode": "KEYWORD",
                    })
                    output[item.external_id] = item
                    if len(output) >= max_items:
                        break
                cursors = _first_dict(_first_dict(data.get("paging")).get("cursors"))
                cursor = str(cursors.get("after") or cursor)
            batch = self.success_batch(
                started,
                list(output.values())[:max_items],
                operation="discover",
                cursor=cursor,
                metadata={
                    "scope": "public_keyword_search",
                    "endpoint": "keyword_search",
                    "search_type": "RECENT",
                    "search_mode": "KEYWORD",
                    "required_scope": "threads_keyword_search",
                    "engagement_metrics_observed": any(
                        item.discovery_context.get("engagement_metrics_observed", False)
                        for item in output.values()
                    ),
                },
            )
            batch.query_attempts = _query_attempts(
                self,
                started,
                batch.receipt.finished_at,
                query_counts,
                metadata={
                    "surface": "keyword_search",
                    "search_type": "RECENT",
                    "search_mode": "KEYWORD",
                    "required_scope": "threads_keyword_search",
                    "metric_contract": "content_metadata_unless_provider_counters_present",
                },
            )
            return batch
        except Exception as error:
            return self.blocked_batch(started, error)

    def refresh(self, tracked: Sequence[Dict[str, Any]]) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            observed = utc_now()
            output: List[MarketContent] = []
            for prior in tracked:
                if self.request_count >= self.request_budget:
                    break
                external_id = str(prior.get("external_id") or "").strip()
                if not external_id:
                    continue
                data = self.request_json(
                    "GET",
                    f"{self.base_url}/{external_id}",
                    headers=self._headers(),
                    params={"fields": self.fields},
                )
                if data.get("id"):
                    output.append(self._normalize(data, observed, prior))
            return self.success_batch(
                started,
                output,
                operation="refresh",
                metadata={
                    "scope": "public_object_lookup",
                    "endpoint": "thread_object",
                    "required_scope": "threads_basic",
                    "engagement_metrics_observed": any(
                        item.discovery_context.get("engagement_metrics_observed", False)
                        for item in output
                    ),
                },
            )
        except Exception as error:
            return self.blocked_batch(started, error)

    def _normalize(
        self,
        raw: Dict[str, Any],
        observed: Any,
        prior: Dict[str, Any] | None = None,
    ) -> MarketContent:
        prior = prior or {}
        owner = _first_dict(raw.get("owner"))
        username = str(
            raw.get("username") or prior.get("creator_handle") or "unknown"
        ).strip().lstrip("@") or "unknown"
        creator_external_id = str(
            owner.get("id") or prior.get("creator_external_id") or username
        ).strip() or "unknown"
        text = str(raw.get("text") or prior.get("caption") or "")
        metric_values = {
            "views": raw.get("views"),
            "likes": raw.get("like_count"),
            "comments": raw.get("reply_count"),
            "shares": raw.get("repost_count"),
        }
        engagement_metrics_observed = any(
            key in raw and value is not None
            for key, value in (
                ("views", metric_values["views"]),
                ("like_count", metric_values["likes"]),
                ("reply_count", metric_values["comments"]),
                ("repost_count", metric_values["shares"]),
            )
        )
        return MarketContent(
            platform=self.platform,
            external_id=str(raw.get("id")),
            creator_external_id=creator_external_id,
            creator_handle=username,
            published_at=parse_datetime(
                raw.get("timestamp") or prior.get("published_at")
            ),
            observed_at=observed,
            source_id=self.source_id,
            metrics=MetricCounters.from_values(**metric_values),
            caption=text,
            url=str(raw.get("permalink") or prior.get("url") or ""),
            thumbnail_url=str(raw.get("thumbnail_url") or ""),
            media_type=str(raw.get("media_type") or "post").lower(),
            hashtags=_hashtags(text),
            raw_payload=raw,
            discovery_context={
                **prior,
                "engagement_metrics_observed": engagement_metrics_observed,
                "metric_contract": (
                    "provider_counters"
                    if engagement_metrics_observed
                    else "content_metadata_only"
                ),
            },
        )


class MetaGraphSource(MarketSource):
    """Authorized account media from Instagram or Facebook."""

    def __init__(
        self,
        *args: Any,
        platform: str,
        account_env: str,
        token_envs: Sequence[str],
        source_id: str,
        edge: str,
        fields: str,
        base_url: str = "https://graph.facebook.com/v23.0",
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.platform = platform
        self.account_env = account_env
        self.token_envs = token_envs
        self.source_id = source_id
        self.edge = edge
        self.fields = fields
        self.base_url = base_url.rstrip("/")

    @property
    def account_id(self) -> str:
        return os.getenv(self.account_env, "").strip()

    @property
    def token(self) -> str:
        for name in self.token_envs:
            value = os.getenv(name, "").strip()
            if value:
                return value
        return ""

    def credentials_available(self) -> bool:
        return bool(self.account_id.isdigit() and self.token)

    def missing_credentials(self) -> List[str]:
        missing = []
        if not self.account_id or not self.account_id.isdigit():
            missing.append(self.account_env)
        if not self.token:
            missing.append(" or ".join(self.token_envs))
        return missing

    def credential_material(self) -> Sequence[str]:
        return (self.account_id, self.token)

    def discover(self, max_items: int) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            data = self.request_json("GET", f"{self.base_url}/{self.account_id}/{self.edge}", headers={"Authorization": f"Bearer {self.token}"}, params={
                "fields": self.fields, "limit": min(100, max_items),
            })
            observed = utc_now()
            items = [self._normalize(raw, observed) for raw in data.get("data", []) if isinstance(raw, dict) and raw.get("id")]
            return self.success_batch(started, items, operation="discover", cursor=str(_first_dict(data.get("paging")).get("cursors", {}).get("after", "")), metadata={"scope": "authorized_account"})
        except Exception as error:
            return self.blocked_batch(started, error)

    def refresh(self, tracked: Sequence[Dict[str, Any]]) -> ProviderBatch:
        started = utc_now()
        try:
            self.preflight()
            observed = utc_now()
            output: List[MarketContent] = []
            for prior in tracked:
                if self.request_count >= self.request_budget:
                    break
                data = self.request_json("GET", f"{self.base_url}/{prior['external_id']}", headers={"Authorization": f"Bearer {self.token}"}, params={"fields": self.fields})
                if data.get("id"):
                    output.append(self._normalize(data, observed, prior))
            return self.success_batch(started, output, operation="refresh", metadata={"scope": "authorized_account"})
        except Exception as error:
            return self.blocked_batch(started, error)

    def _normalize(self, raw: Dict[str, Any], observed: Any, prior: Dict[str, Any] | None = None) -> MarketContent:
        prior = prior or {}
        external_id = str(raw.get("id"))
        text = str(raw.get("caption") or raw.get("description") or raw.get("message") or raw.get("text") or prior.get("caption") or "")
        username = str(raw.get("username") or prior.get("creator_handle") or self.account_id)
        likes = raw.get("like_count")
        comments = raw.get("comments_count")
        if isinstance(raw.get("likes"), dict):
            likes = _first_dict(raw["likes"].get("summary")).get("total_count", likes)
        if isinstance(raw.get("comments"), dict):
            comments = _first_dict(raw["comments"].get("summary")).get("total_count", comments)
        return MarketContent(
            platform=self.platform, external_id=external_id, creator_external_id=self.account_id,
            creator_handle=username, published_at=parse_datetime(raw.get("timestamp") or raw.get("created_time") or prior.get("published_at")),
            observed_at=observed, source_id=self.source_id,
            metrics=MetricCounters.from_values(
                views=raw.get("views") or raw.get("view_count"), likes=likes, comments=comments,
                shares=_first_dict(raw.get("shares")).get("count"), saves=0,
            ),
            caption=text, url=str(raw.get("permalink") or raw.get("permalink_url") or prior.get("url") or ""),
            thumbnail_url=str(raw.get("thumbnail_url") or ""), media_type=str(raw.get("media_type") or "video").lower(),
            hashtags=_hashtags(text), raw_payload=raw,
        )


def _query_attempts(
    source: MarketSource,
    started_at: Any,
    finished_at: Any,
    query_counts: Dict[str, int],
    *,
    metadata: Dict[str, Any] | None = None,
) -> List[QueryAttempt]:
    return [
        QueryAttempt(
            run_id=source.run_id,
            source_id=source.source_id,
            platform=source.platform,
            query=query,
            attempted_at=started_at,
            finished_at=finished_at,
            state="completed" if count else "empty",
            result_count=max(0, int(count)),
            request_count=1,
            metadata={"query_family": query, **(metadata or {})},
        )
        for query, count in query_counts.items()
    ]
