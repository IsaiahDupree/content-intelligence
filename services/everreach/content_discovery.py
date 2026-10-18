"""
EverReach Content Discovery
===========================
Find and download popular videos aligned with EverReach's content pillars.

Topics:
- Networking / relationship building
- Follow-up strategies
- CRM / contact management
- LinkedIn tips
- Cold outreach / warm intros
- Professional relationships

Uses RapidAPI endpoints:
- instagram-looter2 for Instagram
- tiktok-scraper7 for TikTok
"""
import os
import asyncio
import httpx
import json
from datetime import datetime
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from pathlib import Path
from loguru import logger


def load_rapidapi_key() -> str:
    """Load RapidAPI key from .env file or environment"""
    # Try environment first
    key = os.getenv("RAPIDAPI_KEY", "")
    if key:
        return key
    
    # Try .env file
    env_paths = [
        Path(__file__).parent.parent.parent / ".env",
        Path("/Users/isaiahdupree/Documents/Software/MediaPoster/Backend/.env"),
    ]
    
    for env_path in env_paths:
        if env_path.exists():
            with open(env_path) as f:
                for line in f:
                    if line.startswith('RAPIDAPI_KEY='):
                        return line.strip().split('=', 1)[1].strip('"\'')
    
    return ""


RAPIDAPI_KEY = load_rapidapi_key()
OUTPUT_BASE = "/Users/isaiahdupree/Documents/CompetitorResearch/everreach"

# EverReach aligned search terms
EVERREACH_KEYWORDS = [
    # Core networking
    "networking tips",
    "professional networking",
    "how to network",
    "networking for introverts",
    
    # Follow-up
    "follow up tips",
    "how to follow up",
    "never forget to follow up",
    
    # Relationships
    "business relationships",
    "maintaining relationships",
    "relationship building",
    "warm introductions",
    
    # LinkedIn specific
    "linkedin tips",
    "linkedin networking",
    "linkedin outreach",
    "linkedin connections",
    
    # CRM / Systems
    "contact management",
    "keeping in touch",
    "stay in touch",
    
    # Pain points
    "lost connections",
    "networking mistakes",
    "cold outreach vs warm intro",
]

# Target accounts (creators in this space)
TARGET_ACCOUNTS_INSTAGRAM = [
    "garyvee",
    "thejustinwelsh",
    "chrisdo",
    "alexhormozi",
    "personalbrandlaunch",
    "linkedinexpert",
    "salesgravy",
    "gaborgeorge",
]

TARGET_ACCOUNTS_TIKTOK = [
    "garyvee",
    "alexhormozi",
    "thefloshow",
    "saleswithsara",
]


@dataclass
class DiscoveredVideo:
    """A discovered video from search"""
    platform: str
    video_id: str
    url: str
    caption: str
    likes: int
    views: int
    comments: int
    creator: str
    relevance_score: float
    keywords_matched: List[str]
    downloaded: bool = False
    local_path: Optional[str] = None


@dataclass
class DiscoveryResult:
    """Result of content discovery"""
    query: str
    platform: str
    videos_found: int
    videos_downloaded: int
    videos: List[DiscoveredVideo]


class EverReachContentDiscovery:
    """
    Discover and download content aligned with EverReach themes.
    """
    
    def __init__(self, output_folder: str = OUTPUT_BASE):
        self.output_folder = output_folder
        self.rapidapi_key = RAPIDAPI_KEY
        
        # API endpoints
        self.instagram_api = "https://instagram-looter2.p.rapidapi.com"
        self.tiktok_api = "https://tiktok-scraper7.p.rapidapi.com"
        
        # Ensure output folder exists
        Path(output_folder).mkdir(parents=True, exist_ok=True)
    
    async def discover_instagram_by_hashtag(
        self,
        hashtag: str,
        max_results: int = 20
    ) -> List[DiscoveredVideo]:
        """Search Instagram by hashtag"""
        videos = []
        
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(
                    f"{self.instagram_api}/hashtag-posts",
                    params={"name": hashtag.replace("#", "")},
                    headers={
                        "X-RapidAPI-Key": self.rapidapi_key,
                        "X-RapidAPI-Host": "instagram-looter2.p.rapidapi.com"
                    }
                )
                
                if response.status_code != 200:
                    logger.warning(f"Instagram hashtag search failed: {response.status_code}")
                    return videos
                
                data = response.json()
                items = data.get("data", {}).get("items", [])[:max_results]
                
                for item in items:
                    # Only get video posts
                    if item.get("media_type") != 2:  # 2 = video
                        continue
                    
                    code = item.get("code", "")
                    caption_text = item.get("caption", {}).get("text", "") if item.get("caption") else ""
                    
                    # Calculate relevance
                    relevance, matched = self._calculate_relevance(caption_text)
                    
                    videos.append(DiscoveredVideo(
                        platform="instagram",
                        video_id=code,
                        url=f"https://www.instagram.com/reel/{code}/",
                        caption=caption_text[:500],
                        likes=item.get("like_count", 0),
                        views=item.get("play_count", 0) or item.get("view_count", 0),
                        comments=item.get("comment_count", 0),
                        creator=item.get("user", {}).get("username", "unknown"),
                        relevance_score=relevance,
                        keywords_matched=matched
                    ))
                
        except Exception as e:
            logger.error(f"Instagram hashtag search error: {e}")
        
        # Sort by relevance and engagement
        videos.sort(key=lambda v: (v.relevance_score, v.views), reverse=True)
        return videos
    
    async def discover_instagram_from_account(
        self,
        username: str,
        max_results: int = 20
    ) -> List[DiscoveredVideo]:
        """Get reels from a specific Instagram account"""
        videos = []
        
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(
                    f"{self.instagram_api}/reels",
                    params={"username": username},
                    headers={
                        "X-RapidAPI-Key": self.rapidapi_key,
                        "X-RapidAPI-Host": "instagram-looter2.p.rapidapi.com"
                    }
                )
                
                if response.status_code != 200:
                    logger.warning(f"Instagram account fetch failed for @{username}: {response.status_code}")
                    return videos
                
                data = response.json()
                items = data.get("data", {}).get("items", [])[:max_results]
                
                for item in items:
                    code = item.get("code", "")
                    caption_text = item.get("caption", {}).get("text", "") if item.get("caption") else ""
                    
                    # Calculate relevance to EverReach topics
                    relevance, matched = self._calculate_relevance(caption_text)
                    
                    # Only include if somewhat relevant
                    if relevance > 0:
                        videos.append(DiscoveredVideo(
                            platform="instagram",
                            video_id=code,
                            url=f"https://www.instagram.com/reel/{code}/",
                            caption=caption_text[:500],
                            likes=item.get("like_count", 0),
                            views=item.get("play_count", 0) or item.get("view_count", 0),
                            comments=item.get("comment_count", 0),
                            creator=username,
                            relevance_score=relevance,
                            keywords_matched=matched
                        ))
                
        except Exception as e:
            logger.error(f"Instagram account fetch error for @{username}: {e}")
        
        videos.sort(key=lambda v: (v.relevance_score, v.views), reverse=True)
        return videos
    
    async def discover_tiktok_by_keyword(
        self,
        keyword: str,
        max_results: int = 20
    ) -> List[DiscoveredVideo]:
        """Search TikTok by keyword"""
        videos = []
        
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(
                    f"{self.tiktok_api}/feed/search",
                    params={"keywords": keyword, "count": max_results, "region": "us"},
                    headers={
                        "X-RapidAPI-Key": self.rapidapi_key,
                        "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"
                    }
                )
                
                if response.status_code != 200:
                    logger.warning(f"TikTok search failed: {response.status_code}")
                    return videos
                
                data = response.json()
                items = data.get("data", {}).get("videos", [])[:max_results]
                
                for item in items:
                    video_id = item.get("video_id", "")
                    caption_text = item.get("title", "")
                    author = item.get("author", {}).get("unique_id", "unknown")
                    
                    # Calculate relevance
                    relevance, matched = self._calculate_relevance(caption_text)
                    
                    videos.append(DiscoveredVideo(
                        platform="tiktok",
                        video_id=video_id,
                        url=f"https://www.tiktok.com/@{author}/video/{video_id}",
                        caption=caption_text[:500],
                        likes=item.get("digg_count", 0),
                        views=item.get("play_count", 0),
                        comments=item.get("comment_count", 0),
                        creator=author,
                        relevance_score=relevance,
                        keywords_matched=matched
                    ))
                
        except Exception as e:
            logger.error(f"TikTok search error: {e}")
        
        videos.sort(key=lambda v: (v.relevance_score, v.views), reverse=True)
        return videos
    
    async def download_video(
        self,
        video: DiscoveredVideo,
        subfolder: str = "discovered"
    ) -> bool:
        """Download a discovered video"""
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                if video.platform == "instagram":
                    # Get video URL from Instagram
                    response = await client.get(
                        f"{self.instagram_api}/post",
                        params={"link": video.url},
                        headers={
                            "X-RapidAPI-Key": self.rapidapi_key,
                            "X-RapidAPI-Host": "instagram-looter2.p.rapidapi.com"
                        }
                    )
                    
                    if response.status_code != 200:
                        return False
                    
                    data = response.json()
                    video_url = data.get("data", {}).get("video_url")
                    
                    if not video_url:
                        return False
                    
                elif video.platform == "tiktok":
                    # Get video URL from TikTok
                    response = await client.get(
                        f"{self.tiktok_api}/",
                        params={"url": video.url, "hd": "1"},
                        headers={
                            "X-RapidAPI-Key": self.rapidapi_key,
                            "X-RapidAPI-Host": "tiktok-scraper7.p.rapidapi.com"
                        }
                    )
                    
                    if response.status_code != 200:
                        return False
                    
                    data = response.json()
                    video_url = data.get("data", {}).get("play") or data.get("data", {}).get("hdplay")
                    
                    if not video_url:
                        return False
                else:
                    return False
                
                # Download the video file
                output_path = Path(self.output_folder) / subfolder
                output_path.mkdir(parents=True, exist_ok=True)
                
                prefix = "ig" if video.platform == "instagram" else "tt"
                filename = f"{prefix}_{video.video_id}.mp4"
                file_path = output_path / filename
                
                # Skip if exists
                if file_path.exists():
                    video.downloaded = True
                    video.local_path = str(file_path)
                    return True
                
                # Download
                video_response = await client.get(video_url, follow_redirects=True)
                
                if video_response.status_code == 200:
                    with open(file_path, "wb") as f:
                        f.write(video_response.content)
                    
                    video.downloaded = True
                    video.local_path = str(file_path)
                    logger.info(f"Downloaded: {filename}")
                    return True
                    
        except Exception as e:
            logger.error(f"Download error for {video.video_id}: {e}")
        
        return False
    
    async def run_full_discovery(
        self,
        max_per_source: int = 10,
        download: bool = True
    ) -> Dict[str, Any]:
        """
        Run full discovery across all sources and keywords.
        """
        all_videos: List[DiscoveredVideo] = []
        
        print("\n" + "="*60)
        print("🔍 EVERREACH CONTENT DISCOVERY")
        print("="*60)
        
        # 1. Search Instagram hashtags
        print("\n📸 Searching Instagram hashtags...")
        hashtags = ["networking", "linkedintips", "businessnetworking", "followup", "crmtips"]
        for hashtag in hashtags:
            print(f"   #{hashtag}...", end=" ")
            videos = await self.discover_instagram_by_hashtag(hashtag, max_per_source)
            all_videos.extend(videos)
            print(f"found {len(videos)} videos")
            await asyncio.sleep(1)
        
        # 2. Search Instagram accounts
        print("\n📸 Checking Instagram accounts...")
        for account in TARGET_ACCOUNTS_INSTAGRAM[:5]:
            print(f"   @{account}...", end=" ")
            videos = await self.discover_instagram_from_account(account, max_per_source)
            all_videos.extend(videos)
            print(f"found {len(videos)} relevant videos")
            await asyncio.sleep(1)
        
        # 3. Search TikTok keywords
        print("\n🎵 Searching TikTok...")
        tiktok_keywords = ["networking tips", "linkedin tips", "how to follow up", "business relationships"]
        for keyword in tiktok_keywords:
            print(f"   '{keyword}'...", end=" ")
            videos = await self.discover_tiktok_by_keyword(keyword, max_per_source)
            all_videos.extend(videos)
            print(f"found {len(videos)} videos")
            await asyncio.sleep(1)
        
        # Remove duplicates by video_id
        seen_ids = set()
        unique_videos = []
        for v in all_videos:
            if v.video_id not in seen_ids:
                seen_ids.add(v.video_id)
                unique_videos.append(v)
        
        # Sort by relevance and views
        unique_videos.sort(key=lambda v: (v.relevance_score, v.views), reverse=True)
        
        # Take top results
        top_videos = unique_videos[:50]
        
        print(f"\n✅ Found {len(unique_videos)} unique videos")
        print(f"   Top {len(top_videos)} selected for download")
        
        # Download if requested
        downloaded = 0
        if download and top_videos:
            print("\n📥 Downloading videos...")
            for i, video in enumerate(top_videos):
                print(f"   [{i+1}/{len(top_videos)}] {video.creator}: {video.caption[:50]}...", end=" ")
                success = await self.download_video(video)
                if success:
                    downloaded += 1
                    print("✅")
                else:
                    print("❌")
                await asyncio.sleep(2)  # Rate limit
        
        # Save manifest
        manifest = {
            "discovery_date": datetime.now().isoformat(),
            "total_found": len(unique_videos),
            "downloaded": downloaded,
            "videos": [
                {
                    "platform": v.platform,
                    "video_id": v.video_id,
                    "url": v.url,
                    "caption": v.caption,
                    "creator": v.creator,
                    "likes": v.likes,
                    "views": v.views,
                    "relevance_score": v.relevance_score,
                    "keywords_matched": v.keywords_matched,
                    "downloaded": v.downloaded,
                    "local_path": v.local_path
                }
                for v in top_videos
            ]
        }
        
        manifest_path = Path(self.output_folder) / "discovery_manifest.json"
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        
        print(f"\n📋 Manifest saved: {manifest_path}")
        print(f"\n✅ Discovery complete!")
        print(f"   Videos found: {len(unique_videos)}")
        print(f"   Videos downloaded: {downloaded}")
        print(f"   Output folder: {self.output_folder}")
        
        return manifest
    
    def _calculate_relevance(self, text: str) -> tuple[float, List[str]]:
        """Calculate relevance score based on keywords"""
        if not text:
            return 0.0, []
        
        text_lower = text.lower()
        matched = []
        
        # High-value keywords
        high_value = [
            "networking", "follow up", "follow-up", "followup",
            "relationship", "connections", "linkedin", "outreach",
            "crm", "contact", "keep in touch", "stay in touch",
            "warm intro", "referral"
        ]
        
        # Medium-value keywords
        medium_value = [
            "business", "professional", "career", "entrepreneur",
            "sales", "clients", "customers", "leads"
        ]
        
        score = 0.0
        
        for kw in high_value:
            if kw in text_lower:
                score += 0.3
                matched.append(kw)
        
        for kw in medium_value:
            if kw in text_lower:
                score += 0.1
                matched.append(kw)
        
        # Cap at 1.0
        return min(score, 1.0), matched


async def main():
    """Run content discovery"""
    if not RAPIDAPI_KEY:
        print("❌ RAPIDAPI_KEY not set!")
        print("   Set it with: export RAPIDAPI_KEY=your_key")
        return
    
    discovery = EverReachContentDiscovery()
    result = await discovery.run_full_discovery(
        max_per_source=10,
        download=True
    )
    
    # Show top finds
    print("\n📊 TOP DISCOVERIES:")
    print("-"*60)
    for i, v in enumerate(result["videos"][:10], 1):
        print(f"{i}. @{v['creator']} ({v['platform']})")
        print(f"   {v['caption'][:60]}...")
        print(f"   👁 {v['views']:,} views | ❤️ {v['likes']:,} likes | 🎯 {v['relevance_score']:.1f}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
