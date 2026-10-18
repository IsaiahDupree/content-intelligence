"""
EverReach Compilation Video Generator
======================================
Creates "10 Ways to Network Better" style compilation videos using:
- Downloaded TikTok clips
- Hugging Face TTS for AI narration
- Remotion for video composition with captions

Output: Promotional video for EverReach waitlist
"""
import os
import json
import asyncio
import subprocess
import httpx
from datetime import datetime
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from pathlib import Path
from loguru import logger
import random

# Paths
EVERREACH_FOLDER = "/Users/isaiahdupree/Documents/CompetitorResearch/everreach"
OUTPUT_FOLDER = "/Users/isaiahdupree/Documents/CompetitorResearch/everreach/compilations"
REMOTION_PROJECT = "/Users/isaiahdupree/Documents/Software/Remotion"

# Hugging Face TTS
HF_API_URL = "https://api-inference.huggingface.co/models/facebook/mms-tts-eng"
HF_TOKEN = os.getenv("HUGGINGFACE_TOKEN", "")


@dataclass
class ClipSelection:
    """A selected clip for the compilation"""
    index: int
    video_path: str
    creator: str
    caption: str
    tip_number: int
    tip_title: str
    start_time: float  # Clip start time in source video
    duration: float  # Clip duration
    narration_intro: str  # AI narration before clip
    narration_outro: str  # AI narration after clip (optional)


@dataclass 
class CompilationConfig:
    """Configuration for the compilation video"""
    title: str
    intro_narration: str
    outro_narration: str
    clips: List[ClipSelection]
    cta_text: str
    cta_url: str
    background_music: Optional[str] = None
    total_duration: float = 0.0


# EverReach promotional narration scripts
INTRO_SCRIPTS = [
    "Here are 10 networking tips that will change how you build relationships forever. Stick around until the end for a game-changing tool.",
    "Stop letting your network go cold. These 10 tips will help you stay connected with the people who matter most.",
    "Your network is your net worth. Here are 10 ways to actually maintain those valuable relationships.",
]

OUTRO_SCRIPTS = [
    "If you're tired of forgetting to follow up, check out EverReach. It's the app that reminds you to stay in touch with your most important relationships. Link in bio.",
    "Want a system that actually helps you maintain your network? EverReach is launching soon. Join the waitlist at everreach.app.",
    "Never let another relationship go cold. EverReach helps you track and nurture your most valuable connections. Sign up for early access now.",
]

TIP_INTROS = [
    "Tip number {n}: {title}",
    "Number {n} is crucial: {title}",
    "Here's tip {n}: {title}",
    "{title}. This is tip number {n}.",
    "Tip {n} might surprise you: {title}",
]

TIP_TITLES = [
    "The 2-Minute Follow-Up Rule",
    "Quality Over Quantity",
    "The Power of Warm Introductions",
    "Your Network is Dying While You Scroll",
    "The 50-Person Strategy",
    "Never Eat Alone",
    "Give Before You Ask",
    "The LinkedIn Connection Hack",
    "Follow Up Within 24 Hours",
    "Build Relationships, Not Transactions",
    "The Warmth Score Method",
    "Stop Cold Outreach Forever",
]


class EverReachCompilationGenerator:
    """
    Generates promotional compilation videos for EverReach.
    """
    
    def __init__(self):
        self.manifest_path = Path(EVERREACH_FOLDER) / "discovery_manifest.json"
        self.output_folder = Path(OUTPUT_FOLDER)
        self.output_folder.mkdir(parents=True, exist_ok=True)
        
        # Load discovered videos
        self.videos = self._load_manifest()
    
    def _load_manifest(self) -> List[Dict[str, Any]]:
        """Load the discovery manifest"""
        if not self.manifest_path.exists():
            logger.warning("No discovery manifest found")
            return []
        
        with open(self.manifest_path) as f:
            data = json.load(f)
        
        # Filter to only downloaded videos
        return [v for v in data.get("videos", []) if v.get("downloaded") and v.get("local_path")]
    
    async def generate_tts_audio(
        self,
        text: str,
        output_path: str
    ) -> bool:
        """Generate TTS audio using Hugging Face"""
        try:
            # Try local TTS first (faster)
            result = await self._generate_local_tts(text, output_path)
            if result:
                return True
            
            # Fallback to Hugging Face API
            if HF_TOKEN:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(
                        HF_API_URL,
                        headers={"Authorization": f"Bearer {HF_TOKEN}"},
                        json={"inputs": text}
                    )
                    
                    if response.status_code == 200:
                        with open(output_path, "wb") as f:
                            f.write(response.content)
                        return True
            
            logger.warning(f"TTS generation failed for: {text[:50]}...")
            return False
            
        except Exception as e:
            logger.error(f"TTS error: {e}")
            return False
    
    async def _generate_local_tts(self, text: str, output_path: str) -> bool:
        """Generate TTS using local macOS say command"""
        try:
            # Use macOS say command with a good voice
            cmd = [
                "say",
                "-v", "Samantha",  # Natural US English voice
                "-o", output_path.replace(".mp3", ".aiff"),
                text
            ]
            
            result = subprocess.run(cmd, capture_output=True, timeout=30)
            
            if result.returncode == 0:
                # Convert AIFF to MP3
                aiff_path = output_path.replace(".mp3", ".aiff")
                subprocess.run([
                    "ffmpeg", "-y", "-i", aiff_path,
                    "-acodec", "libmp3lame", "-q:a", "2",
                    output_path
                ], capture_output=True, timeout=30)
                
                # Clean up AIFF
                Path(aiff_path).unlink(missing_ok=True)
                return Path(output_path).exists()
            
            return False
            
        except Exception as e:
            logger.warning(f"Local TTS failed: {e}")
            return False
    
    def select_best_clips(self, count: int = 10) -> List[Dict[str, Any]]:
        """Select the best clips for the compilation"""
        if not self.videos:
            logger.error("No videos available")
            return []
        
        # Sort by relevance and views
        sorted_videos = sorted(
            self.videos,
            key=lambda v: (v.get("relevance_score", 0), v.get("views", 0)),
            reverse=True
        )
        
        # Take top N
        selected = sorted_videos[:count]
        
        # Assign tip numbers and titles
        for i, video in enumerate(selected):
            video["tip_number"] = i + 1
            video["tip_title"] = TIP_TITLES[i % len(TIP_TITLES)]
        
        return selected
    
    async def create_compilation(
        self,
        num_tips: int = 10,
        clip_duration: float = 8.0
    ) -> Dict[str, Any]:
        """
        Create a full compilation video.
        
        Args:
            num_tips: Number of tips to include
            clip_duration: Duration of each clip in seconds
        """
        print("\n" + "="*60)
        print("🎬 EVERREACH COMPILATION GENERATOR")
        print("="*60)
        
        # Select clips
        print(f"\n1. Selecting top {num_tips} clips...")
        clips = self.select_best_clips(num_tips)
        
        if len(clips) < num_tips:
            print(f"   ⚠️ Only {len(clips)} clips available")
            num_tips = len(clips)
        
        print(f"   ✅ Selected {len(clips)} clips")
        
        # Create output directory for this compilation
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        comp_dir = self.output_folder / f"compilation_{timestamp}"
        comp_dir.mkdir(parents=True, exist_ok=True)
        
        # Generate narration audio files
        print("\n2. Generating AI narration...")
        narration_files = []
        
        # Intro narration
        intro_text = random.choice(INTRO_SCRIPTS)
        intro_path = comp_dir / "narration_intro.mp3"
        print(f"   Intro: {intro_text[:50]}...")
        success = await self.generate_tts_audio(intro_text, str(intro_path))
        if success:
            narration_files.append({"type": "intro", "path": str(intro_path), "text": intro_text})
            print("   ✅ Intro generated")
        
        # Tip intros
        for i, clip in enumerate(clips):
            tip_text = random.choice(TIP_INTROS).format(
                n=clip["tip_number"],
                title=clip["tip_title"]
            )
            tip_path = comp_dir / f"narration_tip_{i+1}.mp3"
            print(f"   Tip {i+1}: {tip_text[:40]}...", end=" ")
            success = await self.generate_tts_audio(tip_text, str(tip_path))
            if success:
                narration_files.append({
                    "type": "tip",
                    "tip_number": i+1,
                    "path": str(tip_path),
                    "text": tip_text
                })
                print("✅")
            else:
                print("❌")
        
        # Outro narration
        outro_text = random.choice(OUTRO_SCRIPTS)
        outro_path = comp_dir / "narration_outro.mp3"
        print(f"   Outro: {outro_text[:50]}...")
        success = await self.generate_tts_audio(outro_text, str(outro_path))
        if success:
            narration_files.append({"type": "outro", "path": str(outro_path), "text": outro_text})
            print("   ✅ Outro generated")
        
        # Generate timeline JSON for Remotion
        print("\n3. Creating Remotion timeline...")
        timeline = await self._create_timeline(clips, narration_files, clip_duration, comp_dir)
        
        timeline_path = comp_dir / "timeline.json"
        with open(timeline_path, "w") as f:
            json.dump(timeline, f, indent=2)
        print(f"   ✅ Timeline saved: {timeline_path}")
        
        # Create FFmpeg concat script as backup
        print("\n4. Creating FFmpeg concat script...")
        concat_script = await self._create_ffmpeg_script(clips, narration_files, clip_duration, comp_dir)
        
        script_path = comp_dir / "concat_script.sh"
        with open(script_path, "w") as f:
            f.write(concat_script)
        script_path.chmod(0o755)
        print(f"   ✅ Script saved: {script_path}")
        
        # Run FFmpeg to create compilation
        print("\n5. Rendering compilation video...")
        output_video = comp_dir / "everreach_compilation.mp4"
        
        render_success = await self._render_with_ffmpeg(clips, narration_files, clip_duration, comp_dir, output_video)
        
        if render_success:
            file_size = output_video.stat().st_size / (1024 * 1024)
            print(f"   ✅ Video rendered: {output_video}")
            print(f"   📦 Size: {file_size:.1f} MB")
        else:
            print("   ❌ Rendering failed - check script manually")
        
        # Save compilation config
        config = {
            "created_at": datetime.now().isoformat(),
            "num_tips": num_tips,
            "clip_duration": clip_duration,
            "clips": clips,
            "narration_files": narration_files,
            "output_video": str(output_video) if render_success else None,
            "timeline_path": str(timeline_path),
            "intro_text": intro_text,
            "outro_text": outro_text
        }
        
        config_path = comp_dir / "compilation_config.json"
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2, default=str)
        
        print(f"\n✅ Compilation complete!")
        print(f"   Output folder: {comp_dir}")
        
        return config
    
    async def _create_timeline(
        self,
        clips: List[Dict],
        narrations: List[Dict],
        clip_duration: float,
        output_dir: Path
    ) -> Dict[str, Any]:
        """Create Remotion timeline JSON"""
        layers = []
        current_time = 0.0
        
        # Get audio duration helper
        def get_audio_duration(path: str) -> float:
            try:
                result = subprocess.run([
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    path
                ], capture_output=True, text=True, timeout=10)
                return float(result.stdout.strip())
            except:
                return 2.0  # Default 2 seconds
        
        # Add intro title card
        layers.append({
            "id": "title_card",
            "type": "text",
            "content": "10 Networking Tips\nThat Will Change Your Life",
            "start": current_time,
            "end": current_time + 3.0,
            "style": {
                "fontSize": 72,
                "fontWeight": "bold",
                "color": "#ffffff",
                "textAlign": "center",
                "backgroundColor": "#8B5CF6"
            }
        })
        current_time += 3.0
        
        # Add intro narration
        intro_narration = next((n for n in narrations if n["type"] == "intro"), None)
        if intro_narration:
            duration = get_audio_duration(intro_narration["path"])
            layers.append({
                "id": "intro_audio",
                "type": "audio",
                "source": intro_narration["path"],
                "start": current_time,
                "end": current_time + duration
            })
            layers.append({
                "id": "intro_caption",
                "type": "text",
                "content": intro_narration["text"],
                "start": current_time,
                "end": current_time + duration,
                "style": {"position": "bottom", "fontSize": 36}
            })
            current_time += duration + 0.5
        
        # Add each clip with narration
        for i, clip in enumerate(clips):
            tip_narration = next((n for n in narrations if n.get("tip_number") == i+1), None)
            
            # Tip intro narration
            if tip_narration:
                duration = get_audio_duration(tip_narration["path"])
                
                # Tip number overlay
                layers.append({
                    "id": f"tip_{i+1}_number",
                    "type": "text",
                    "content": f"TIP #{i+1}",
                    "start": current_time,
                    "end": current_time + duration,
                    "style": {
                        "fontSize": 96,
                        "fontWeight": "bold",
                        "color": "#8B5CF6",
                        "textAlign": "center"
                    }
                })
                
                layers.append({
                    "id": f"tip_{i+1}_audio",
                    "type": "audio",
                    "source": tip_narration["path"],
                    "start": current_time,
                    "end": current_time + duration
                })
                current_time += duration + 0.3
            
            # Video clip
            layers.append({
                "id": f"clip_{i+1}",
                "type": "video",
                "source": clip["local_path"],
                "start": current_time,
                "end": current_time + clip_duration,
                "trim": {"start": 0, "end": clip_duration}
            })
            
            # Caption overlay
            layers.append({
                "id": f"clip_{i+1}_caption",
                "type": "text",
                "content": f"@{clip['creator']}",
                "start": current_time,
                "end": current_time + clip_duration,
                "style": {
                    "position": "bottom-right",
                    "fontSize": 24,
                    "opacity": 0.8
                }
            })
            
            current_time += clip_duration + 0.5
        
        # Add outro
        outro_narration = next((n for n in narrations if n["type"] == "outro"), None)
        if outro_narration:
            duration = get_audio_duration(outro_narration["path"])
            
            # CTA card
            layers.append({
                "id": "cta_card",
                "type": "text",
                "content": "Join the Waitlist\neverreach.app",
                "start": current_time,
                "end": current_time + duration + 2.0,
                "style": {
                    "fontSize": 64,
                    "fontWeight": "bold",
                    "color": "#ffffff",
                    "backgroundColor": "#8B5CF6",
                    "textAlign": "center"
                }
            })
            
            layers.append({
                "id": "outro_audio",
                "type": "audio",
                "source": outro_narration["path"],
                "start": current_time,
                "end": current_time + duration
            })
            
            current_time += duration + 2.0
        
        return {
            "version": "1.0",
            "composition": "EverReachCompilation",
            "fps": 30,
            "width": 1080,
            "height": 1920,
            "duration": current_time,
            "layers": layers
        }
    
    async def _create_ffmpeg_script(
        self,
        clips: List[Dict],
        narrations: List[Dict],
        clip_duration: float,
        output_dir: Path
    ) -> str:
        """Create FFmpeg concat script"""
        script = "#!/bin/bash\n\n"
        script += f"# EverReach Compilation Generator\n"
        script += f"# Generated: {datetime.now().isoformat()}\n\n"
        script += f"cd \"{output_dir}\"\n\n"
        
        # Create segments list
        segments = []
        
        # Process each clip - trim to duration
        for i, clip in enumerate(clips):
            segment_path = f"segment_{i+1}.mp4"
            script += f"# Segment {i+1}: @{clip['creator']}\n"
            script += f"ffmpeg -y -i \"{clip['local_path']}\" -t {clip_duration} "
            script += f"-vf \"scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1\" "
            script += f"-c:v libx264 -preset fast -c:a aac \"{segment_path}\"\n\n"
            segments.append(segment_path)
        
        # Create concat file
        script += "# Create concat list\n"
        script += "cat > concat_list.txt << EOF\n"
        for seg in segments:
            script += f"file '{seg}'\n"
        script += "EOF\n\n"
        
        # Concat all segments
        script += "# Concatenate all segments\n"
        script += "ffmpeg -y -f concat -safe 0 -i concat_list.txt -c copy everreach_compilation.mp4\n\n"
        
        # Cleanup
        script += "# Cleanup segments\n"
        script += "rm -f segment_*.mp4 concat_list.txt\n"
        
        return script
    
    async def _render_with_ffmpeg(
        self,
        clips: List[Dict],
        narrations: List[Dict],
        clip_duration: float,
        output_dir: Path,
        output_path: Path
    ) -> bool:
        """Render compilation using FFmpeg"""
        try:
            segments = []
            
            # Get intro narration
            intro = next((n for n in narrations if n["type"] == "intro"), None)
            outro = next((n for n in narrations if n["type"] == "outro"), None)
            
            # Create intro segment with narration
            if intro and Path(intro["path"]).exists():
                intro_seg = output_dir / "seg_intro.mp4"
                # Create video from audio with text
                cmd = [
                    "ffmpeg", "-y",
                    "-f", "lavfi", "-i", "color=c=0x8B5CF6:s=1080x1920:d=5",
                    "-i", intro["path"],
                    "-vf", f"drawtext=text='10 Networking Tips':fontsize=72:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-50,drawtext=text='That Will Change Your Life':fontsize=48:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2+50",
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    str(intro_seg)
                ]
                subprocess.run(cmd, capture_output=True, timeout=60)
                if intro_seg.exists():
                    segments.append(str(intro_seg))
            
            # Process each clip
            for i, clip in enumerate(clips):
                if not Path(clip["local_path"]).exists():
                    continue
                
                seg_path = output_dir / f"seg_clip_{i+1}.mp4"
                
                # Get tip narration
                tip_narration = next((n for n in narrations if n.get("tip_number") == i+1), None)
                
                # First create tip intro if narration exists
                if tip_narration and Path(tip_narration["path"]).exists():
                    tip_intro_seg = output_dir / f"seg_tip_intro_{i+1}.mp4"
                    cmd = [
                        "ffmpeg", "-y",
                        "-f", "lavfi", "-i", "color=c=0x1a1a2e:s=1080x1920:d=3",
                        "-i", tip_narration["path"],
                        "-vf", f"drawtext=text='TIP #{i+1}':fontsize=120:fontcolor=0x8B5CF6:x=(w-text_w)/2:y=(h-text_h)/2",
                        "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                        str(tip_intro_seg)
                    ]
                    subprocess.run(cmd, capture_output=True, timeout=60)
                    if tip_intro_seg.exists():
                        segments.append(str(tip_intro_seg))
                
                # Then add the clip
                cmd = [
                    "ffmpeg", "-y",
                    "-i", clip["local_path"],
                    "-t", str(clip_duration),
                    "-vf", f"scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,drawtext=text='@{clip['creator']}':fontsize=32:fontcolor=white:x=w-text_w-20:y=h-text_h-100:box=1:boxcolor=black@0.5:boxborderw=5",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    str(seg_path)
                ]
                subprocess.run(cmd, capture_output=True, timeout=120)
                if seg_path.exists():
                    segments.append(str(seg_path))
            
            # Create outro segment
            if outro and Path(outro["path"]).exists():
                outro_seg = output_dir / "seg_outro.mp4"
                cmd = [
                    "ffmpeg", "-y",
                    "-f", "lavfi", "-i", "color=c=0x8B5CF6:s=1080x1920:d=8",
                    "-i", outro["path"],
                    "-vf", "drawtext=text='Join the Waitlist':fontsize=64:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-80,drawtext=text='everreach.app':fontsize=72:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2+20",
                    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    str(outro_seg)
                ]
                subprocess.run(cmd, capture_output=True, timeout=60)
                if outro_seg.exists():
                    segments.append(str(outro_seg))
            
            if not segments:
                logger.error("No segments created")
                return False
            
            # Create concat file
            concat_file = output_dir / "concat.txt"
            with open(concat_file, "w") as f:
                for seg in segments:
                    f.write(f"file '{seg}'\n")
            
            # Concat all segments
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_file),
                "-c", "copy",
                str(output_path)
            ]
            
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            
            # Cleanup
            for seg in segments:
                Path(seg).unlink(missing_ok=True)
            concat_file.unlink(missing_ok=True)
            
            return output_path.exists()
            
        except Exception as e:
            logger.error(f"FFmpeg render error: {e}")
            return False


async def main():
    """Generate EverReach compilation video"""
    generator = EverReachCompilationGenerator()
    
    if not generator.videos:
        print("❌ No videos found. Run content_discovery.py first.")
        return
    
    print(f"📹 Found {len(generator.videos)} videos to work with")
    
    result = await generator.create_compilation(
        num_tips=10,
        clip_duration=8.0
    )
    
    if result.get("output_video"):
        print(f"\n🎉 Video ready: {result['output_video']}")


if __name__ == "__main__":
    asyncio.run(main())
