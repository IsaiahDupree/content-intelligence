"""
Audio-Aware Video Composer
===========================
Creates video compositions with proper audio-video synchronization.

Features:
- Word-level timestamp extraction from TTS audio
- Audio duration analysis before timeline creation
- Systematic timeline generation based on actual audio lengths
- Support for both Motion Canvas and Remotion output formats
"""
import os
import sys
import json
import asyncio
import subprocess
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from loguru import logger

# Add Backend to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Try to import TranscriptionService (requires openai)
try:
    from services.transcription import TranscriptionService
    TRANSCRIPTION_AVAILABLE = True
except ImportError as e:
    logger.warning(f"TranscriptionService not available: {e}")
    TranscriptionService = None
    TRANSCRIPTION_AVAILABLE = False

# Paths
EVERREACH_FOLDER = "/Users/isaiahdupree/Documents/CompetitorResearch/everreach"
OUTPUT_FOLDER = "/Users/isaiahdupree/Documents/CompetitorResearch/everreach/compilations"
MOTION_CANVAS_PROJECT = "/Users/isaiahdupree/Documents/Software/MediaPoster/MotionCanvas"
REMOTION_PROJECT = "/Users/isaiahdupree/Documents/Software/Remotion"


@dataclass
class AudioSegment:
    """Audio segment with timing information"""
    id: str
    path: str
    text: str
    duration: float
    word_timestamps: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class TimelineSegment:
    """A segment in the timeline with precise timing"""
    id: str
    type: str  # "narration", "clip", "title", "cta"
    start_time: float
    end_time: float
    duration: float
    content: Dict[str, Any]


@dataclass
class CompositionTimeline:
    """Full composition timeline with all segments"""
    total_duration: float
    fps: int
    width: int
    height: int
    segments: List[TimelineSegment]
    audio_tracks: List[AudioSegment]


class AudioAnalyzer:
    """
    Analyzes audio files to extract duration and word-level timestamps.
    Uses OpenAI Whisper API for accurate word-level transcription.
    """
    
    def __init__(self):
        """Initialize with TranscriptionService for word-level timestamps"""
        self.transcription_service = None
        self.whisper_enabled = False
        
        if TRANSCRIPTION_AVAILABLE and TranscriptionService:
            try:
                self.transcription_service = TranscriptionService()
                self.whisper_enabled = self.transcription_service.is_enabled()
                if self.whisper_enabled:
                    logger.info("✅ OpenAI Whisper API enabled for word-level timestamps")
                else:
                    logger.warning("⚠️ OpenAI API key not configured - using estimated timestamps")
            except Exception as e:
                logger.warning(f"TranscriptionService init failed: {e}")
        else:
            logger.warning("⚠️ TranscriptionService not available - using estimated timestamps")
    
    def get_audio_duration(self, audio_path: str) -> float:
        """Get precise audio duration using ffprobe"""
        try:
            result = subprocess.run([
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path
            ], capture_output=True, text=True, timeout=10)
            
            duration = float(result.stdout.strip())
            logger.info(f"Audio duration for {Path(audio_path).name}: {duration:.2f}s")
            return duration
        except Exception as e:
            logger.error(f"Failed to get audio duration: {e}")
            return 0.0
    
    async def get_word_timestamps(self, audio_path: str, text: str = "") -> List[Dict[str, Any]]:
        """
        Get word-level timestamps using OpenAI Whisper API.
        Falls back to estimated timestamps if Whisper fails.
        
        Args:
            audio_path: Path to audio file
            text: Optional expected text (used for fallback estimation)
            
        Returns:
            List of {word, start, end} dictionaries
        """
        # Try OpenAI Whisper API first
        if self.whisper_enabled and self.transcription_service:
            try:
                logger.info(f"🎤 Transcribing with OpenAI Whisper: {Path(audio_path).name}")
                result = self.transcription_service.transcribe_audio_only(audio_path)
                
                if "error" not in result and result.get("words"):
                    words = result["words"]
                    logger.success(f"✅ Got {len(words)} word timestamps from Whisper")
                    return words
                elif "error" in result:
                    logger.warning(f"Whisper API error: {result['error']}")
            except Exception as e:
                logger.warning(f"Whisper API failed: {e}")
        
        # Fallback: estimate word timestamps based on duration
        logger.info("Using estimated word timestamps")
        return self._estimate_word_timestamps(audio_path, text)
    
    async def transcribe_video_with_timestamps(self, video_path: str) -> Dict[str, Any]:
        """
        Transcribe a video file and get word-level timestamps.
        
        Args:
            video_path: Path to video file
            
        Returns:
            Dict with text, words, segments, duration
        """
        if not self.whisper_enabled or not self.transcription_service:
            logger.warning("Whisper not available for video transcription")
            return {"text": "", "words": [], "segments": [], "duration": 0}
        
        try:
            logger.info(f"🎬 Transcribing video: {Path(video_path).name}")
            result = self.transcription_service.transcribe_video(video_path)
            
            if "error" not in result:
                word_count = len(result.get("words", []))
                logger.success(f"✅ Video transcribed: {word_count} words, {result.get('duration', 0):.1f}s")
                return result
            else:
                logger.warning(f"Video transcription error: {result['error']}")
                return {"text": "", "words": [], "segments": [], "duration": 0}
        except Exception as e:
            logger.error(f"Video transcription failed: {e}")
            return {"text": "", "words": [], "segments": [], "duration": 0}
    
    def _estimate_word_timestamps(self, audio_path: str, text: str) -> List[Dict[str, Any]]:
        """Estimate word timestamps based on audio duration and word count"""
        duration = self.get_audio_duration(audio_path)
        words = text.split() if text else []
        
        if not words or duration <= 0:
            return []
        
        # Average time per word (accounting for pauses)
        avg_word_duration = duration / len(words) * 0.85  # 85% speaking, 15% pauses
        
        timestamps = []
        current_time = 0.1  # Small initial pause
        
        for word in words:
            # Adjust duration based on word length
            word_duration = avg_word_duration * (0.8 + 0.4 * min(len(word) / 8, 1))
            
            timestamps.append({
                "word": word,
                "start": round(current_time, 3),
                "end": round(current_time + word_duration, 3)
            })
            
            current_time += word_duration
            
            # Add pause after punctuation
            if word[-1] in ".!?":
                current_time += 0.3
            elif word[-1] in ",;:":
                current_time += 0.15
        
        return timestamps


class AudioAwareComposer:
    """
    Creates video compositions with proper audio-video synchronization.
    Uses OpenAI Whisper for word-level timestamps on both narrations AND source clips.
    """
    
    def __init__(self):
        self.analyzer = AudioAnalyzer()
        self.manifest_path = Path(EVERREACH_FOLDER) / "discovery_manifest.json"
        self.output_folder = Path(OUTPUT_FOLDER)
        self.output_folder.mkdir(parents=True, exist_ok=True)
        
        # Load discovered videos
        self.videos = self._load_manifest()
        
        # Track transcriptions for source clips
        self.clip_transcriptions: Dict[str, Dict[str, Any]] = {}
    
    async def transcribe_source_clips(self, clips: List[Dict]) -> None:
        """
        Transcribe all source video clips to get word-level timestamps.
        This ensures we don't cut clips in the middle of words.
        """
        print("\n📝 Transcribing source video clips...")
        
        for i, clip in enumerate(clips):
            video_path = clip.get("local_path")
            if not video_path or not Path(video_path).exists():
                continue
            
            print(f"   [{i+1}/{len(clips)}] {Path(video_path).name}...", end=" ")
            
            # Transcribe the video
            result = await self.analyzer.transcribe_video_with_timestamps(video_path)
            
            if result.get("words"):
                self.clip_transcriptions[video_path] = result
                print(f"✅ {len(result['words'])} words")
            else:
                print("⚠️ No words detected")
                self.clip_transcriptions[video_path] = {"words": [], "text": "", "duration": 0}
    
    def find_safe_cut_point(
        self, 
        video_path: str, 
        target_duration: float,
        buffer: float = 0.3
    ) -> float:
        """
        Find a safe cut point that doesn't clip a word.
        
        Args:
            video_path: Path to the video
            target_duration: Desired clip duration
            buffer: Time buffer after last word (seconds)
            
        Returns:
            Safe duration that ends after a complete word
        """
        transcription = self.clip_transcriptions.get(video_path, {})
        words = transcription.get("words", [])
        
        if not words:
            # No transcription available, use target duration
            return target_duration
        
        # Find the last word that ends before target_duration
        safe_end = 0.0
        for word in words:
            word_end = word.get("end", 0)
            if word_end <= target_duration:
                safe_end = word_end
            else:
                break
        
        # Add small buffer after the word
        safe_duration = safe_end + buffer
        
        # Ensure we don't exceed target by too much
        if safe_duration > target_duration + 1.0:
            safe_duration = target_duration
        
        logger.info(f"Safe cut point for {Path(video_path).name}: {safe_duration:.2f}s (target: {target_duration:.2f}s)")
        return max(safe_duration, 1.0)  # Minimum 1 second
    
    def _load_manifest(self) -> List[Dict[str, Any]]:
        """Load the discovery manifest"""
        if not self.manifest_path.exists():
            return []
        
        with open(self.manifest_path) as f:
            data = json.load(f)
        
        return [v for v in data.get("videos", []) if v.get("downloaded") and v.get("local_path")]
    
    async def generate_tts_with_timestamps(
        self,
        text: str,
        output_path: str,
        voice: str = "Samantha"
    ) -> AudioSegment:
        """Generate TTS and extract word timestamps"""
        
        # Generate audio using macOS say
        aiff_path = output_path.replace(".mp3", ".aiff")
        
        cmd = ["say", "-v", voice, "-o", aiff_path, text]
        subprocess.run(cmd, capture_output=True, timeout=30)
        
        # Convert to MP3
        subprocess.run([
            "ffmpeg", "-y", "-i", aiff_path,
            "-acodec", "libmp3lame", "-q:a", "2",
            output_path
        ], capture_output=True, timeout=30)
        
        Path(aiff_path).unlink(missing_ok=True)
        
        # Get duration and timestamps
        duration = self.analyzer.get_audio_duration(output_path)
        word_timestamps = await self.analyzer.get_word_timestamps(output_path, text)
        
        return AudioSegment(
            id=Path(output_path).stem,
            path=output_path,
            text=text,
            duration=duration,
            word_timestamps=word_timestamps
        )
    
    async def build_timeline(
        self,
        clips: List[Dict],
        narrations: Dict[str, AudioSegment],
        clip_duration: float = 8.0
    ) -> CompositionTimeline:
        """
        Build a timeline with precise timing based on audio durations.
        """
        segments = []
        audio_tracks = []
        current_time = 0.0
        
        # 1. Title card (3 seconds)
        segments.append(TimelineSegment(
            id="title",
            type="title",
            start_time=current_time,
            end_time=current_time + 3.0,
            duration=3.0,
            content={
                "text": "10 Networking Tips\nThat Will Change Your Life",
                "style": "title_card"
            }
        ))
        current_time += 3.0
        
        # 2. Intro narration
        if "intro" in narrations:
            intro = narrations["intro"]
            audio_tracks.append(intro)
            
            segments.append(TimelineSegment(
                id="intro_narration",
                type="narration",
                start_time=current_time,
                end_time=current_time + intro.duration,
                duration=intro.duration,
                content={
                    "audio_path": intro.path,
                    "text": intro.text,
                    "word_timestamps": intro.word_timestamps,
                    "style": "intro_bg"
                }
            ))
            current_time += intro.duration + 0.5  # Small gap
        
        # 3. Each tip: narration -> clip
        for i, clip in enumerate(clips):
            tip_key = f"tip_{i+1}"
            
            # Tip narration
            if tip_key in narrations:
                tip_narration = narrations[tip_key]
                audio_tracks.append(tip_narration)
                
                segments.append(TimelineSegment(
                    id=f"tip_{i+1}_intro",
                    type="narration",
                    start_time=current_time,
                    end_time=current_time + tip_narration.duration,
                    duration=tip_narration.duration,
                    content={
                        "tip_number": i + 1,
                        "audio_path": tip_narration.path,
                        "text": tip_narration.text,
                        "word_timestamps": tip_narration.word_timestamps,
                        "style": "tip_intro"
                    }
                ))
                current_time += tip_narration.duration + 0.3
            
            # Video clip - use safe cut point to avoid clipping words
            video_path = clip["local_path"]
            max_duration = min(clip_duration, self._get_video_duration(video_path))
            
            # Find safe cut point that doesn't clip a word
            safe_duration = self.find_safe_cut_point(video_path, max_duration)
            
            # Get transcription for this clip (for captions)
            clip_transcript = self.clip_transcriptions.get(video_path, {})
            clip_words = clip_transcript.get("words", [])
            
            # Filter words that fall within our clip duration
            clip_word_timestamps = [
                w for w in clip_words 
                if w.get("end", 0) <= safe_duration
            ]
            
            segments.append(TimelineSegment(
                id=f"clip_{i+1}",
                type="clip",
                start_time=current_time,
                end_time=current_time + safe_duration,
                duration=safe_duration,
                content={
                    "video_path": video_path,
                    "creator": clip.get("creator", "unknown"),
                    "caption": clip.get("caption", ""),
                    "transcript": clip_transcript.get("text", "")[:200],
                    "word_timestamps": clip_word_timestamps,
                    "trim_start": 0,
                    "trim_end": safe_duration
                }
            ))
            current_time += safe_duration + 0.5
        
        # 4. Outro with CTA
        if "outro" in narrations:
            outro = narrations["outro"]
            audio_tracks.append(outro)
            
            segments.append(TimelineSegment(
                id="outro",
                type="cta",
                start_time=current_time,
                end_time=current_time + outro.duration + 2.0,
                duration=outro.duration + 2.0,
                content={
                    "audio_path": outro.path,
                    "text": outro.text,
                    "word_timestamps": outro.word_timestamps,
                    "cta_text": "Join the Waitlist",
                    "cta_url": "everreach.app",
                    "style": "cta_card"
                }
            ))
            current_time += outro.duration + 2.0
        
        return CompositionTimeline(
            total_duration=current_time,
            fps=30,
            width=1080,
            height=1920,
            segments=segments,
            audio_tracks=audio_tracks
        )
    
    def _get_video_duration(self, video_path: str) -> float:
        """Get video duration"""
        try:
            result = subprocess.run([
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path
            ], capture_output=True, text=True, timeout=10)
            return float(result.stdout.strip())
        except:
            return 60.0  # Default max
    
    def export_motion_canvas_scene(
        self,
        timeline: CompositionTimeline,
        output_dir: Path
    ) -> str:
        """Export timeline as Motion Canvas scene"""
        
        scene_code = f'''import {{makeScene2D, Txt, Rect, Video, Img, Audio}} from '@motion-canvas/2d';
import {{all, chain, waitFor, createRef, easeInOutCubic}} from '@motion-canvas/core';

export default makeScene2D(function* (view) {{
  // Total duration: {timeline.total_duration:.2f}s
  // FPS: {timeline.fps}
  
  const background = createRef<Rect>();
  const title = createRef<Txt>();
  const caption = createRef<Txt>();
  
  // Background
  view.add(
    <Rect
      ref={{background}}
      width={{1080}}
      height={{1920}}
      fill="{{"#1a1a2e"}}"
    />
  );
  
'''
        
        for segment in timeline.segments:
            if segment.type == "title":
                scene_code += f'''
  // {segment.id} ({segment.duration:.2f}s)
  view.add(
    <Txt
      ref={{title}}
      text={{"{segment.content['text'].replace(chr(10), '\\n')}"}}
      fontSize={{72}}
      fontWeight={{700}}
      fill="{{"#ffffff"}}"
      textAlign="center"
    />
  );
  yield* waitFor({segment.duration});
  title().remove();
  
'''
            elif segment.type == "narration":
                word_display = ""
                if segment.content.get("word_timestamps"):
                    words = segment.content["word_timestamps"]
                    word_display = f"// Words: {len(words)}, Duration: {segment.duration:.2f}s"
                
                scene_code += f'''
  // {segment.id} ({segment.duration:.2f}s)
  {word_display}
  // Audio: {Path(segment.content.get('audio_path', '')).name}
  background().fill("#8B5CF6");
  view.add(
    <Txt
      ref={{caption}}
      text={{"{segment.content.get('text', '')[:100]}..."}}
      fontSize={{48}}
      fill="{{"#ffffff"}}"
      textAlign="center"
      y={{400}}
    />
  );
  yield* waitFor({segment.duration});
  caption().remove();
  background().fill("#1a1a2e");
  
'''
            elif segment.type == "clip":
                scene_code += f'''
  // {segment.id} ({segment.duration:.2f}s) - @{segment.content.get('creator', 'unknown')}
  // Video: {Path(segment.content.get('video_path', '')).name}
  view.add(
    <Video
      src={{"{segment.content.get('video_path', '')}"}}
      width={{1080}}
      height={{1920}}
      time={{0}}
      play={{true}}
    />
  );
  view.add(
    <Txt
      text={{"@{segment.content.get('creator', '')}"}}
      fontSize={{32}}
      fill="{{"#ffffff"}}"
      x={{400}}
      y={{800}}
    />
  );
  yield* waitFor({segment.duration});
  
'''
            elif segment.type == "cta":
                scene_code += f'''
  // {segment.id} ({segment.duration:.2f}s)
  background().fill("#8B5CF6");
  view.add(
    <Txt
      text={{"{segment.content.get('cta_text', 'Join the Waitlist')}"}}
      fontSize={{64}}
      fontWeight={{700}}
      fill="{{"#ffffff"}}"
      y={{-50}}
    />
  );
  view.add(
    <Txt
      text={{"{segment.content.get('cta_url', 'everreach.app')}"}}
      fontSize={{72}}
      fontWeight={{700}}
      fill="{{"#ffffff"}}"
      y={{50}}
    />
  );
  yield* waitFor({segment.duration});
  
'''
        
        scene_code += '''
}});
'''
        
        scene_path = output_dir / "everreach_scene.tsx"
        with open(scene_path, "w") as f:
            f.write(scene_code)
        
        return str(scene_path)
    
    def export_remotion_props(
        self,
        timeline: CompositionTimeline,
        output_dir: Path
    ) -> str:
        """Export Remotion props for EverReachCompilation component with word timestamps"""
        
        fps = timeline.fps
        
        # Extract narrations and clips from timeline
        narrations = []
        clips = []
        title_text = ""
        cta_text = "Join the Waitlist"
        cta_url = "everreach.app"
        
        for segment in timeline.segments:
            if segment.type == "title":
                title_text = segment.content.get("text", "")
            elif segment.type == "narration":
                narrations.append({
                    "id": segment.id,
                    "text": segment.content.get("text", ""),
                    "audioSrc": segment.content.get("audio_path", "").replace(str(output_dir) + "/", ""),
                    "duration": segment.duration,
                    "wordTimestamps": segment.content.get("word_timestamps", [])
                })
            elif segment.type == "clip":
                clips.append({
                    "id": segment.id,
                    "videoSrc": segment.content.get("video_path", ""),
                    "duration": segment.duration,
                    "creator": segment.content.get("creator", "unknown"),
                    "transcript": segment.content.get("transcript", ""),
                    "wordTimestamps": segment.content.get("word_timestamps", [])
                })
            elif segment.type == "cta":
                cta_text = segment.content.get("cta_text", cta_text)
                cta_url = segment.content.get("cta_url", cta_url)
        
        props = {
            "title": title_text.split("\n")[0] if title_text else "5 Tips",
            "subtitle": title_text.split("\n")[1] if "\n" in title_text else None,
            "narrations": narrations,
            "clips": clips,
            "ctaText": cta_text,
            "ctaUrl": cta_url,
            "captionStyle": "bouncy",
            "theme": {
                "primaryColor": "#8B5CF6",
                "accentColor": "#00ff88",
                "backgroundColor": "linear-gradient(135deg, #1a0a2e 0%, #0a1a2e 100%)"
            }
        }
        
        props_path = output_dir / "remotion_props.json"
        with open(props_path, "w") as f:
            json.dump(props, f, indent=2)
        
        logger.info(f"Exported Remotion props with {len(narrations)} narrations, {len(clips)} clips")
        return str(props_path)
    
    def export_remotion_composition(
        self,
        timeline: CompositionTimeline,
        output_dir: Path
    ) -> str:
        """Export timeline as Remotion composition"""
        
        # Also export props for the new EverReachCompilation component
        self.export_remotion_props(timeline, output_dir)
        
        # Convert to frames
        fps = timeline.fps
        
        composition = {
            "id": "EverReachCompilation",
            "fps": fps,
            "width": timeline.width,
            "height": timeline.height,
            "durationInFrames": int(timeline.total_duration * fps),
            "defaultProps": {
                "segments": []
            }
        }
        
        for segment in timeline.segments:
            seg_data = {
                "id": segment.id,
                "type": segment.type,
                "startFrame": int(segment.start_time * fps),
                "endFrame": int(segment.end_time * fps),
                "durationFrames": int(segment.duration * fps),
                **segment.content
            }
            composition["defaultProps"]["segments"].append(seg_data)
        
        comp_path = output_dir / "remotion_composition.json"
        with open(comp_path, "w") as f:
            json.dump(composition, f, indent=2)
        
        # Also create a Remotion component
        component_code = f'''import {{AbsoluteFill, Sequence, useCurrentFrame, Video, Audio, Img}} from 'remotion';
import {{interpolate}} from 'remotion';

// Auto-generated EverReach Compilation
// Total Duration: {timeline.total_duration:.2f}s ({int(timeline.total_duration * fps)} frames)
// FPS: {fps}

interface Segment {{
  id: string;
  type: string;
  startFrame: number;
  endFrame: number;
  durationFrames: number;
  [key: string]: any;
}}

export const EverReachCompilation: React.FC<{{segments: Segment[]}}> = ({{segments}}) => {{
  const frame = useCurrentFrame();
  
  return (
    <AbsoluteFill style={{{{backgroundColor: '#1a1a2e'}}}}>
      {{segments.map((segment, index) => (
        <Sequence
          key={{segment.id}}
          from={{segment.startFrame}}
          durationInFrames={{segment.durationFrames}}
        >
          {{segment.type === 'title' && (
            <AbsoluteFill style={{{{
              backgroundColor: '#8B5CF6',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center'
            }}}}>
              <div style={{{{
                color: 'white',
                fontSize: 72,
                fontWeight: 'bold',
                textAlign: 'center',
                whiteSpace: 'pre-line'
              }}}}>
                {{segment.text}}
              </div>
            </AbsoluteFill>
          )}}
          
          {{segment.type === 'narration' && (
            <AbsoluteFill style={{{{
              backgroundColor: '#8B5CF6',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexDirection: 'column'
            }}}}>
              {{segment.tip_number && (
                <div style={{{{color: 'white', fontSize: 120, fontWeight: 'bold'}}}}>
                  TIP #{{segment.tip_number}}
                </div>
              )}}
              <div style={{{{
                color: 'white',
                fontSize: 36,
                textAlign: 'center',
                padding: 40,
                maxWidth: '80%'
              }}}}>
                {{segment.text}}
              </div>
              {{segment.audio_path && <Audio src={{segment.audio_path}} />}}
            </AbsoluteFill>
          )}}
          
          {{segment.type === 'clip' && (
            <AbsoluteFill>
              <Video
                src={{segment.video_path}}
                style={{{{width: '100%', height: '100%', objectFit: 'cover'}}}}
              />
              <div style={{{{
                position: 'absolute',
                bottom: 100,
                right: 20,
                color: 'white',
                fontSize: 32,
                backgroundColor: 'rgba(0,0,0,0.5)',
                padding: '5px 10px',
                borderRadius: 5
              }}}}>
                @{{segment.creator}}
              </div>
            </AbsoluteFill>
          )}}
          
          {{segment.type === 'cta' && (
            <AbsoluteFill style={{{{
              backgroundColor: '#8B5CF6',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              flexDirection: 'column'
            }}}}>
              <div style={{{{color: 'white', fontSize: 64, fontWeight: 'bold'}}}}>
                {{segment.cta_text}}
              </div>
              <div style={{{{color: 'white', fontSize: 72, fontWeight: 'bold', marginTop: 20}}}}>
                {{segment.cta_url}}
              </div>
              {{segment.audio_path && <Audio src={{segment.audio_path}} />}}
            </AbsoluteFill>
          )}}
        </Sequence>
      ))}}
    </AbsoluteFill>
  );
}};
'''
        
        component_path = output_dir / "EverReachCompilation.tsx"
        with open(component_path, "w") as f:
            f.write(component_code)
        
        return str(comp_path)
    
    def export_ffmpeg_script(
        self,
        timeline: CompositionTimeline,
        output_dir: Path
    ) -> str:
        """Export as FFmpeg script with precise timing"""
        
        script = f'''#!/bin/bash
# Audio-Aware FFmpeg Composition
# Total Duration: {timeline.total_duration:.2f}s
# Generated: {datetime.now().isoformat()}

set -e
cd "{output_dir}"

echo "🎬 Building EverReach Compilation..."
echo "   Total duration: {timeline.total_duration:.2f}s"
echo ""

'''
        
        # Create each segment as a separate file
        segment_files = []
        
        for i, segment in enumerate(timeline.segments):
            seg_file = f"seg_{i:03d}_{segment.id}.mp4"
            segment_files.append(seg_file)
            
            if segment.type == "title":
                # Add silent audio so concat works properly (all segments need audio)
                script += f'''
# Segment {i}: {segment.id} ({segment.duration:.2f}s)
echo "Creating segment {i}: {segment.id}..."
ffmpeg -y -f lavfi -i "color=c=0x8B5CF6:s=1080x1920:d={segment.duration}" \\
  -f lavfi -i "anullsrc=r=44100:cl=stereo" \\
  -vf "drawtext=text='{segment.content['text'].replace(chr(10), '\\n')}':fontsize=72:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2:font=Arial" \\
  -c:v libx264 -preset ultrafast -c:a aac -shortest -pix_fmt yuv420p \\
  "{seg_file}"

'''
            elif segment.type == "narration":
                audio_path = segment.content.get("audio_path", "")
                tip_num = segment.content.get("tip_number", "")
                text = segment.content.get("text", "")[:80].replace("'", "\\'")
                
                if tip_num:
                    script += f'''
# Segment {i}: {segment.id} ({segment.duration:.2f}s)
echo "Creating segment {i}: Tip #{tip_num}..."
ffmpeg -y -f lavfi -i "color=c=0x1a1a2e:s=1080x1920:d={segment.duration}" \\
  -i "{audio_path}" \\
  -vf "drawtext=text='TIP #{tip_num}':fontsize=120:fontcolor=0x8B5CF6:x=(w-text_w)/2:y=(h-text_h)/2" \\
  -c:v libx264 -preset ultrafast -c:a aac -shortest -pix_fmt yuv420p \\
  "{seg_file}"

'''
                else:
                    script += f'''
# Segment {i}: {segment.id} ({segment.duration:.2f}s)
echo "Creating segment {i}: {segment.id}..."
ffmpeg -y -f lavfi -i "color=c=0x8B5CF6:s=1080x1920:d={segment.duration}" \\
  -i "{audio_path}" \\
  -vf "drawtext=text='{text}':fontsize=36:fontcolor=white:x=(w-text_w)/2:y=h-200:font=Arial" \\
  -c:v libx264 -preset ultrafast -c:a aac -shortest -pix_fmt yuv420p \\
  "{seg_file}"

'''
            elif segment.type == "clip":
                video_path = segment.content.get("video_path", "")
                creator = segment.content.get("creator", "unknown")
                tip_num = segment.content.get("tip_number", i // 2)
                
                # Safe zones: bottom 400px reserved for TikTok UI, sides 60px padding
                script += f'''
# Segment {i}: {segment.id} ({segment.duration:.2f}s) - @{creator}
echo "Creating segment {i}: @{creator}..."
ffmpeg -y -i "{video_path}" \\
  -t {segment.duration} \\
  -vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,drawtext=text='TIP #{tip_num}':fontsize=48:fontcolor=0x00ff88:x=60:y=120:box=1:boxcolor=black@0.6:boxborderw=10,drawtext=text='@{creator}':fontsize=28:fontcolor=white:x=w-text_w-60:y=h-450:box=1:boxcolor=black@0.5:boxborderw=5" \\
  -c:v libx264 -preset ultrafast -c:a aac -pix_fmt yuv420p \\
  "{seg_file}"

'''
            elif segment.type == "cta":
                audio_path = segment.content.get("audio_path", "")
                cta_text = segment.content.get("cta_text", "Join the Waitlist")
                cta_url = segment.content.get("cta_url", "everreach.app")
                
                script += f'''
# Segment {i}: {segment.id} ({segment.duration:.2f}s)
echo "Creating segment {i}: CTA..."
ffmpeg -y -f lavfi -i "color=c=0x8B5CF6:s=1080x1920:d={segment.duration}" \\
  -i "{audio_path}" \\
  -vf "drawtext=text='{cta_text}':fontsize=64:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-50,drawtext=text='{cta_url}':fontsize=72:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2+50" \\
  -c:v libx264 -preset ultrafast -c:a aac -shortest -pix_fmt yuv420p \\
  "{seg_file}"

'''
        
        # Create concat file
        script += '''
# Create concat list
echo "Creating concat list..."
cat > concat_list.txt << 'CONCAT_EOF'
'''
        for seg_file in segment_files:
            script += f"file '{seg_file}'\n"
        
        script += '''CONCAT_EOF

# Concatenate all segments
echo "Concatenating segments..."
ffmpeg -y -f concat -safe 0 -i concat_list.txt -c copy "everreach_compilation_final.mp4"

# Get final duration
DURATION=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "everreach_compilation_final.mp4")
SIZE=$(du -h "everreach_compilation_final.mp4" | cut -f1)

echo ""
echo "✅ Compilation complete!"
echo "   Duration: ${DURATION}s"
echo "   Size: ${SIZE}"
echo "   Output: everreach_compilation_final.mp4"

# Cleanup (optional - comment out to keep segments)
# rm -f seg_*.mp4 concat_list.txt
'''
        
        script_path = output_dir / "render_compilation.sh"
        with open(script_path, "w") as f:
            f.write(script)
        script_path.chmod(0o755)
        
        return str(script_path)


async def main():
    """Test the audio-aware composer"""
    print("\n" + "="*60)
    print("🎬 AUDIO-AWARE VIDEO COMPOSER TEST")
    print("="*60)
    
    composer = AudioAwareComposer()
    
    if not composer.videos:
        print("❌ No videos found. Run content_discovery.py first.")
        return
    
    # Create output directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(OUTPUT_FOLDER) / f"audio_aware_{timestamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Select top 5 clips for testing
    clips = composer.videos[:5]
    print(f"\n📹 Selected {len(clips)} clips for test")
    
    # Transcribe source video clips for word-level timestamps
    await composer.transcribe_source_clips(clips)
    
    # Generate narrations with timestamps
    print("\n🎤 Generating narrations with word timestamps...")
    narrations = {}
    
    # Intro
    intro_text = "Here are 5 networking tips that will change how you build relationships."
    intro_audio = await composer.generate_tts_with_timestamps(
        intro_text, str(output_dir / "narration_intro.mp3")
    )
    narrations["intro"] = intro_audio
    print(f"   ✅ Intro: {intro_audio.duration:.2f}s, {len(intro_audio.word_timestamps)} words")
    
    # Tips
    tip_titles = [
        "The power of following up",
        "Quality over quantity",
        "Build genuine connections",
        "Give before you ask",
        "Stay top of mind"
    ]
    
    for i, title in enumerate(tip_titles):
        tip_text = f"Tip number {i+1}: {title}"
        tip_audio = await composer.generate_tts_with_timestamps(
            tip_text, str(output_dir / f"narration_tip_{i+1}.mp3")
        )
        narrations[f"tip_{i+1}"] = tip_audio
        print(f"   ✅ Tip {i+1}: {tip_audio.duration:.2f}s")
    
    # Outro
    outro_text = "Want a system that helps you maintain your network? Join the EverReach waitlist today."
    outro_audio = await composer.generate_tts_with_timestamps(
        outro_text, str(output_dir / "narration_outro.mp3")
    )
    narrations["outro"] = outro_audio
    print(f"   ✅ Outro: {outro_audio.duration:.2f}s")
    
    # Build timeline
    print("\n📐 Building audio-aware timeline...")
    timeline = await composer.build_timeline(clips, narrations, clip_duration=6.0)
    
    print(f"   Total duration: {timeline.total_duration:.2f}s")
    print(f"   Segments: {len(timeline.segments)}")
    
    # Print timeline breakdown
    print("\n📋 Timeline breakdown:")
    for seg in timeline.segments:
        print(f"   [{seg.start_time:6.2f}s - {seg.end_time:6.2f}s] {seg.type}: {seg.id}")
    
    # Export formats
    print("\n📦 Exporting compositions...")
    
    # Motion Canvas
    mc_path = composer.export_motion_canvas_scene(timeline, output_dir)
    print(f"   ✅ Motion Canvas: {mc_path}")
    
    # Remotion
    rm_path = composer.export_remotion_composition(timeline, output_dir)
    print(f"   ✅ Remotion: {rm_path}")
    
    # FFmpeg script
    ff_path = composer.export_ffmpeg_script(timeline, output_dir)
    print(f"   ✅ FFmpeg script: {ff_path}")
    
    # Save timeline JSON
    timeline_data = {
        "total_duration": timeline.total_duration,
        "fps": timeline.fps,
        "width": timeline.width,
        "height": timeline.height,
        "segments": [
            {
                "id": s.id,
                "type": s.type,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "duration": s.duration,
                "content": s.content
            }
            for s in timeline.segments
        ]
    }
    
    timeline_path = output_dir / "timeline.json"
    with open(timeline_path, "w") as f:
        json.dump(timeline_data, f, indent=2, default=str)
    
    print(f"\n✅ All exports complete!")
    print(f"   Output folder: {output_dir}")
    print(f"\n🚀 To render with FFmpeg:")
    print(f"   cd {output_dir} && ./render_compilation.sh")


async def main_with_render():
    """Generate and render, then open the video"""
    await main()
    
    # Find the most recent output directory
    compilations_dir = Path(OUTPUT_FOLDER)
    audio_aware_dirs = sorted(compilations_dir.glob("audio_aware_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    
    if not audio_aware_dirs:
        print("❌ No output directory found")
        return
    
    latest_dir = audio_aware_dirs[0]
    render_script = latest_dir / "render_compilation.sh"
    video_path = latest_dir / "everreach_compilation_final.mp4"
    
    # Run the render script
    print(f"\n🎬 Rendering video with FFmpeg...")
    result = subprocess.run(
        ["bash", str(render_script)],
        cwd=str(latest_dir),
        capture_output=True,
        text=True
    )
    
    if result.returncode == 0 and video_path.exists():
        file_size = video_path.stat().st_size / (1024 * 1024)
        print(f"✅ Render complete! Size: {file_size:.1f} MB")
        print(f"\n🎬 Opening video: {video_path}")
        # Open with default video player
        subprocess.run(["open", str(video_path)])
    else:
        print(f"❌ Render failed")
        if result.stderr:
            print(f"Error: {result.stderr[:500]}")


if __name__ == "__main__":
    asyncio.run(main_with_render())
