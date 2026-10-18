"""
Ingestion Service for PRD2
Handles file scanning and Supabase asset creation
"""
import os
import mimetypes
from pathlib import Path
from typing import List, Optional
from uuid import UUID, uuid4

from models.supabase_models import (
    MediaAsset, SourceType, MediaType, MediaStatus, Platform
)

class IngestionService:
    """Service to ingest media from local directory to Supabase structure"""
    
    def __init__(self, watch_directory: str):
        self.watch_directory = Path(watch_directory)
        self.scanned_assets: List[MediaAsset] = []

    def scan_directory(self) -> List[Path]:
        """Scan directory for valid media files"""
        if not self.watch_directory.exists():
            raise FileNotFoundError(f"Directory not found: {self.watch_directory}")
            
        valid_extensions = {'.mp4', '.mov', '.jpg', '.jpeg', '.png'}
        files = []
        for ext in valid_extensions:
            files.extend(list(self.watch_directory.rglob(f"*{ext}")))
        return sorted(files)

    def process_file(self, file_path: Path, owner_id: UUID) -> MediaAsset:
        """Process a single file into a MediaAsset"""
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Determine media type
        mime_type, _ = mimetypes.guess_type(file_path)
        media_type = MediaType.VIDEO if mime_type and mime_type.startswith('video') else MediaType.IMAGE
        
        # Create asset
        asset = MediaAsset(
            id=uuid4(),
            owner_id=owner_id,
            source_type=SourceType.LOCAL_UPLOAD,
            storage_path=str(file_path.absolute()),  # Storing absolute path for now
            media_type=media_type,
            status=MediaStatus.INGESTED,
            platform_hint=Platform.MULTI
        )
        
        # Determine metadata (placeholder for heavy extraction)
        if media_type == MediaType.VIDEO:
            # In a real impl, we'd use moviepy/ffmpeg here
            asset.duration_sec = 0.0 
            asset.resolution = "unknown"
            
        return asset

    def ingest_new_files(self, owner_id: UUID) -> List[MediaAsset]:
        """Scan and ingest files that haven't been tracked yet"""
        files = self.scan_directory()
        new_assets = []
        
        # In real DB, we'd check existing paths
        # Here we just process all for the service logic
        for f in files:
            try:
                asset = self.process_file(f, owner_id)
                new_assets.append(asset)
            except Exception as e:
                print(f"Error processing {f}: {e}")
                
        self.scanned_assets.extend(new_assets)
        return new_assets

    def simulate_supabase_upload(self, asset: MediaAsset) -> bool:
        """Mock upload step"""
        # In real impl, this uploads to Supabase Storage
        return True
