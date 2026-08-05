#!/usr/bin/env python3
"""Analyze the audio of albums you seed on OPS and submit sonic fingerprints.

See README.md for setup, or run with --help for options.
"""

import argparse
import json
import os
import sys
import logging
import hashlib
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict, Counter
import re

try:
    import essentia
    import essentia.standard as es
except ImportError:
    print("Error: Essentia is not installed. Install with: pip install essentia")
    sys.exit(1)

try:
    from mutagen import File as MutagenFile
    from mutagen.easyid3 import EasyID3
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
except ImportError:
    print("Error: Mutagen is not installed. Install with: pip install mutagen")
    sys.exit(1)

try:
    import bencodepy
except ImportError:
    print("Error: bencodepy is not installed. Install with: pip install bencodepy")
    sys.exit(1)

try:
    import numpy as np
except ImportError:
    print("Error: NumPy is not installed. Install with: pip install numpy")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Error: requests is not installed. Install with: pip install requests")
    sys.exit(1)


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('sonic_analysis.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# API config, loaded from env vars or CLI args
API_TOKEN = os.environ.get('OPS_API_TOKEN', '')
API_BASE_URL = os.environ.get('OPS_API_URL', 'https://orpheus.network/ajax.php')


def get_torrent_hash(torrent_path: str) -> Optional[str]:
    """Calculate the info hash of a torrent file."""
    try:
        with open(torrent_path, 'rb') as f:
            torrent_data = bencodepy.decode(f.read())
        
        # Get the info dictionary and encode it
        info = torrent_data[b'info']
        info_encoded = bencodepy.encode(info)
        
        # Calculate SHA-1 hash
        info_hash = hashlib.sha1(info_encoded).hexdigest()
        
        return info_hash.upper()
    except Exception as e:
        logger.error(f"Error calculating hash for {torrent_path}: {e}")
        return None


def lookup_torrent(torrent_hash: str) -> Optional[Dict]:
    """Look up torrent group by hash using Orpheus API."""
    headers = {
        'Authorization': f'token {API_TOKEN}'
    }
    
    url = f'{API_BASE_URL}?action=torrentgroup&hash={torrent_hash}'
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.ok:
            data = response.json()
            if data.get('status') == 'success':
                return data.get('response')
            else:
                logger.warning(f"API returned non-success status for hash {torrent_hash}")
                return None
        else:
            logger.warning(f"API request failed with status {response.status_code} for hash {torrent_hash}")
            return None
    except Exception as e:
        logger.error(f"Error querying API for hash {torrent_hash}: {e}")
        return None


class TorrentScanner:
    """Scan and parse torrent files to find OPS torrents"""
    
    OPS_TRACKER = "home.opsfet.ch"
    OPS_COMMENT = "orpheus.network"
    OPS_SOURCE  = "OPS"

    @staticmethod
    def is_ops_torrent(torrent_data: dict) -> bool:
        """
        Check if a torrent belongs to OPS using three fields (in priority order):
        1. announce URL contains the OPS tracker domain
        2. comment contains an orpheus.network URL
        3. info.source is set to "OPS"
        Some clients (qBittorrent 4.4+) strip the announce URL from stored
        .torrent files, so the comment and source fields serve as fallbacks.
        """
        announce = torrent_data.get(b'announce', b'').decode('utf-8', errors='ignore')
        if TorrentScanner.OPS_TRACKER in announce:
            return True

        comment = torrent_data.get(b'comment', b'').decode('utf-8', errors='ignore')
        if TorrentScanner.OPS_COMMENT in comment:
            return True

        source = torrent_data.get(b'info', {}).get(b'source', b'').decode('utf-8', errors='ignore')
        if source == TorrentScanner.OPS_SOURCE:
            return True

        return False

    @staticmethod
    def parse_torrent(torrent_path: str) -> Optional[Dict]:
        """
        Parse a .torrent file and extract metadata.
        Returns dict with tracker, root folder path, and torrent path.
        """
        try:
            with open(torrent_path, 'rb') as f:
                torrent_data = bencodepy.decode(f.read())

            if not TorrentScanner.is_ops_torrent(torrent_data):
                return None

            # Extract file paths from torrent
            info = torrent_data.get(b'info', {})

            # Get the root folder name
            name = info.get(b'name', b'').decode('utf-8', errors='ignore')

            if not name:
                logger.warning(f"No name found in torrent: {torrent_path}")
                return None

            announce = torrent_data.get(b'announce', b'').decode('utf-8', errors='ignore')
            return {
                'tracker': announce,
                'root_folder': name,
                'torrent_path': torrent_path
            }

        except Exception as e:
            logger.debug(f"Error parsing torrent {torrent_path}: {e}")
            return None
    
    @staticmethod
    def find_torrents(directory: str, music_dirs: List[str] = None) -> List[Dict]:
        """
        Find all OPS .torrent files in directory and subdirectories.
        For each torrent, locate the matching album folder either next to
        the .torrent file (default) or in the provided music_dirs search paths.
        """
        torrents = []

        logger.info(f"Scanning for OPS .torrent files in {directory}")

        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.endswith('.torrent'):
                    torrent_path = os.path.join(root, file)
                    torrent_info = TorrentScanner.parse_torrent(torrent_path)

                    if torrent_info:
                        folder_name = torrent_info['root_folder']
                        album_folder_path = None

                        # Build search list: directory containing the .torrent, then music_dirs
                        search_dirs = [root]
                        if music_dirs:
                            search_dirs.extend(music_dirs)

                        # Fast path: album folder sits directly under a search dir
                        for search_dir in search_dirs:
                            candidate = os.path.join(search_dir, folder_name)
                            if os.path.isdir(candidate):
                                album_folder_path = candidate
                                break

                        # Fallback: some clients store downloaded data separately
                        # from the .torrent files AND organise it into subfolders
                        # (by artist, label, etc.), so search each music dir
                        # recursively for a folder matching the torrent name.
                        if not album_folder_path and music_dirs:
                            for search_dir in music_dirs:
                                for sub_root, sub_dirs, _ in os.walk(search_dir):
                                    if folder_name in sub_dirs:
                                        album_folder_path = os.path.join(sub_root, folder_name)
                                        break
                                if album_folder_path:
                                    break

                        if album_folder_path:
                            torrent_info['album_folder'] = album_folder_path
                            torrents.append(torrent_info)
                        else:
                            logger.warning(f"Torrent references folder that doesn't exist: {folder_name}")

        logger.info(f"Found {len(torrents)} OPS torrents")
        return torrents


class MusicMetadataExtractor:
    """Extract metadata from audio files"""
    
    SUPPORTED_FORMATS = {'.mp3', '.flac', '.ogg', '.m4a', '.wav'}
    
    @staticmethod
    def extract_from_tags(filepath: str) -> Optional[Tuple[str, str]]:
        """Extract artist, album from file tags"""
        try:
            audio = MutagenFile(filepath, easy=True)
            if audio is None:
                return None
            
            artist = None
            album = None
            
            # Artist tags
            for tag in ['artist', 'albumartist', 'ARTIST', 'ALBUMARTIST']:
                if tag in audio and audio[tag]:
                    artist = audio[tag][0] if isinstance(audio[tag], list) else audio[tag]
                    if artist:
                        break
            
            # Album tags
            for tag in ['album', 'ALBUM']:
                if tag in audio and audio[tag]:
                    album = audio[tag][0] if isinstance(audio[tag], list) else audio[tag]
                    if album:
                        break
            
            # Clean up values
            artist = artist.strip() if artist else None
            album = album.strip() if album else None
            
            if artist and album:
                return (artist, album)
            return None
        except Exception as e:
            logger.debug(f"Could not read tags from {filepath}: {e}")
            return None


class SonicAnalyzer:
    """Analyze audio files using Essentia"""
    
    def __init__(self):
        # Initialize Essentia algorithms
        self.loader = es.MonoLoader()
        self.rhythm_extractor = es.RhythmExtractor2013()
        self.key_extractor = es.KeyExtractor()
        self.dynamic_complexity = es.DynamicComplexity()
        self.spectral_centroid = es.Centroid(range=22050)
        self.spectral_rolloff = es.RollOff()
        self.mfcc = es.MFCC()
        
    def analyze_file(self, filepath: str) -> Optional[Dict]:
        """
        Perform comprehensive sonic analysis on an audio file
        Returns dictionary of analysis results or None on error
        """
        try:
            # Load audio
            self.loader.configure(filename=filepath, sampleRate=44100)
            audio = self.loader()
            
            if len(audio) == 0:
                logger.warning(f"Empty audio file: {filepath}")
                return None
            
            analysis = {}
            
            # Rhythm analysis
            try:
                bpm, beats, beats_confidence, _, beats_intervals = self.rhythm_extractor(audio)
                analysis['tempo'] = float(bpm)
                analysis['beats_count'] = len(beats)
                analysis['rhythm_confidence'] = float(beats_confidence)
            except Exception as e:
                logger.debug(f"Rhythm extraction failed for {filepath}: {e}")
                analysis['tempo'] = None
            
            # Key analysis
            try:
                key, scale, key_strength = self.key_extractor(audio)
                analysis['key'] = key
                analysis['scale'] = scale
                analysis['key_strength'] = float(key_strength)
            except Exception as e:
                logger.debug(f"Key extraction failed for {filepath}: {e}")
                analysis['key'] = None
                analysis['scale'] = None
            
            # Loudness: mean energy raised to Stevens' power law exponent
            # Uses per-sample energy so it's independent of track duration
            try:
                mean_energy = float(np.mean(audio.astype(np.float64) ** 2))
                analysis['loudness'] = float(mean_energy ** 0.67)
            except Exception as e:
                logger.debug(f"Loudness extraction failed for {filepath}: {e}")
                analysis['loudness'] = None
            
            # Dynamic complexity
            try:
                dynamic_complexity, loudness_range = self.dynamic_complexity(audio)
                analysis['dynamic_complexity'] = float(dynamic_complexity)
                analysis['dynamic_range'] = float(loudness_range)
            except Exception as e:
                logger.debug(f"Dynamic analysis failed for {filepath}: {e}")
                analysis['dynamic_complexity'] = None
            
            # Spectral features
            try:
                # Calculate on frames
                frameSize = 2048
                hopSize = 1024
                frames = es.FrameGenerator(audio, frameSize=frameSize, hopSize=hopSize)
                
                centroids = []
                rolloffs = []
                mfccs = []
                
                window = es.Windowing(type='hann')
                spectrum = es.Spectrum()
                
                for frame in frames:
                    windowed = window(frame)
                    spec = spectrum(windowed)
                    
                    centroid = self.spectral_centroid(spec)
                    centroids.append(centroid)
                    
                    rolloff = self.spectral_rolloff(spec)
                    rolloffs.append(rolloff)
                    
                    _, mfcc_coeffs = self.mfcc(spec)
                    mfccs.append(mfcc_coeffs)
                
                # Average values
                analysis['spectral_centroid'] = float(sum(centroids) / len(centroids)) if centroids else None
                analysis['spectral_rolloff'] = float(sum(rolloffs) / len(rolloffs)) if rolloffs else None
                
                # Average MFCCs across time
                if mfccs:
                    mfcc_array = np.array(mfccs)
                    analysis['mfcc'] = [float(x) for x in np.mean(mfcc_array, axis=0)]
                else:
                    analysis['mfcc'] = None
                    
            except Exception as e:
                logger.debug(f"Spectral analysis failed for {filepath}: {e}")
                analysis['spectral_centroid'] = None
                analysis['spectral_rolloff'] = None
                analysis['mfcc'] = None
            
            # Duration
            analysis['duration'] = float(len(audio) / 44100.0)
            
            return analysis
            
        except Exception as e:
            logger.error(f"Analysis failed for {filepath}: {e}")
            return None


def aggregate_album_analysis(track_analyses: List[Dict]) -> Dict:
    """
    Aggregate track-level analyses into album-level statistics
    """
    if not track_analyses:
        return {}
    
    aggregated = {
        'track_count': len(track_analyses)
    }
    
    # Aggregate tempo (median is more robust than mean for tempo)
    tempos = [t['tempo'] for t in track_analyses if t.get('tempo') is not None]
    if tempos:
        aggregated['tempo_median'] = float(np.median(tempos))
        aggregated['tempo_mean'] = float(np.mean(tempos))
        aggregated['tempo_std'] = float(np.std(tempos))
    else:
        aggregated['tempo_median'] = None
        aggregated['tempo_mean'] = None
        aggregated['tempo_std'] = None
    
    # Most common key and scale
    keys = [t['key'] for t in track_analyses if t.get('key') is not None]
    scales = [t['scale'] for t in track_analyses if t.get('scale') is not None]
    
    if keys:
        key_counter = Counter(keys)
        aggregated['primary_key'] = key_counter.most_common(1)[0][0]
        aggregated['key_consistency'] = key_counter.most_common(1)[0][1] / len(keys)
    else:
        aggregated['primary_key'] = None
        aggregated['key_consistency'] = None
    
    if scales:
        scale_counter = Counter(scales)
        aggregated['primary_scale'] = scale_counter.most_common(1)[0][0]
    else:
        aggregated['primary_scale'] = None
    
    # Average key strength
    key_strengths = [t['key_strength'] for t in track_analyses if t.get('key_strength') is not None]
    aggregated['key_strength_mean'] = float(np.mean(key_strengths)) if key_strengths else None
    
    # Average loudness
    loudnesses = [t['loudness'] for t in track_analyses if t.get('loudness') is not None]
    if loudnesses:
        aggregated['loudness_mean'] = float(np.mean(loudnesses))
        aggregated['loudness_std'] = float(np.std(loudnesses))
    else:
        aggregated['loudness_mean'] = None
        aggregated['loudness_std'] = None
    
    # Average dynamic complexity and range
    dynamic_complexities = [t['dynamic_complexity'] for t in track_analyses if t.get('dynamic_complexity') is not None]
    dynamic_ranges = [t['dynamic_range'] for t in track_analyses if t.get('dynamic_range') is not None]
    
    aggregated['dynamic_complexity_mean'] = float(np.mean(dynamic_complexities)) if dynamic_complexities else None
    aggregated['dynamic_range_mean'] = float(np.mean(dynamic_ranges)) if dynamic_ranges else None
    
    # Average spectral features
    spectral_centroids = [t['spectral_centroid'] for t in track_analyses if t.get('spectral_centroid') is not None]
    spectral_rolloffs = [t['spectral_rolloff'] for t in track_analyses if t.get('spectral_rolloff') is not None]
    
    aggregated['spectral_centroid_mean'] = float(np.mean(spectral_centroids)) if spectral_centroids else None
    aggregated['spectral_rolloff_mean'] = float(np.mean(spectral_rolloffs)) if spectral_rolloffs else None
    
    # Average MFCCs across all tracks
    mfccs = [t['mfcc'] for t in track_analyses if t.get('mfcc') is not None]
    if mfccs:
        mfcc_array = np.array(mfccs)
        aggregated['mfcc_mean'] = [float(x) for x in np.mean(mfcc_array, axis=0)]
    else:
        aggregated['mfcc_mean'] = None
    
    # Total album duration
    durations = [t['duration'] for t in track_analyses if t.get('duration') is not None]
    aggregated['total_duration'] = float(sum(durations)) if durations else None
    aggregated['avg_track_duration'] = float(np.mean(durations)) if durations else None
    
    return aggregated


def find_audio_files_in_folder(folder_path: str) -> List[str]:
    """Find all supported audio files in a folder and its subdirectories (e.g. Disc 1/, Disc 2/)"""
    audio_files = []
    supported_exts = MusicMetadataExtractor.SUPPORTED_FORMATS

    if not os.path.isdir(folder_path):
        return audio_files

    for root, dirs, files in os.walk(folder_path):
        for file in files:
            if Path(file).suffix.lower() in supported_exts:
                audio_files.append(os.path.join(root, file))

    return audio_files


def load_existing_data(output_file: str) -> Dict:
    """Load existing JSON data if it exists"""
    if os.path.exists(output_file):
        try:
            with open(output_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load existing JSON: {e}")
            return {}
    return {}


def save_data(data: Dict, output_file: str):
    """Save data to JSON file"""
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Data saved to {output_file}")
    except Exception as e:
        logger.error(f"Failed to save data: {e}")


def upload_tracks(torrent_hash: str, tracks: List[Dict], upload_url: str, upload_token: str) -> Optional[Dict]:
    """Upload per-track sonic data to the Gazelle instance.

    Submissions are keyed to a specific torrent (identified by its infohash), not
    its group: a deluxe edition with bonus tracks is a distinct torrent and gets
    its own fingerprint. The server resolves the infohash and verifies the user
    is seeding that torrent.
    """
    headers = {
        'Authorization': f'token {upload_token}',
        'Content-Type': 'application/json',
    }
    payload = {
        'hash': torrent_hash,
        'tracks': tracks,
    }
    try:
        response = requests.post(
            f'{upload_url}?action=sonic_upload',
            headers=headers,
            json=payload,
            timeout=30,
        )
        if response.ok:
            data = response.json()
            if data.get('status') == 'success':
                return data.get('response')
            else:
                logger.warning(f"Upload failed for {torrent_hash}: {data.get('error', 'unknown error')}")
                return None
        else:
            logger.warning(f"Upload HTTP {response.status_code} for {torrent_hash}")
            return None
    except Exception as e:
        logger.error(f"Upload error for {torrent_hash}: {e}")
        return None


def upload_existing_data(json_file: str, upload_url: str, upload_token: str):
    """Upload all track data from an existing JSON file to the Gazelle instance."""
    library_data = load_existing_data(json_file)
    if not library_data:
        logger.error("No data to upload")
        return

    total = len(library_data)
    uploaded = 0
    skipped = 0

    for idx, (key, entry) in enumerate(library_data.items(), 1):
        torrent_hash = entry.get('infohash', key)
        tracks = entry.get('tracks')
        label = f"{entry.get('artist', '?')} - {entry.get('album', '?')}"
        if not tracks:
            logger.warning(f"[{idx}/{total}] No track data for {label}, skipping")
            skipped += 1
            continue
        if not torrent_hash:
            logger.warning(f"[{idx}/{total}] No infohash for {label}, skipping (re-analyze to record it)")
            skipped += 1
            continue

        logger.info(f"[{idx}/{total}] Uploading {label} ({len(tracks)} tracks)")
        result = upload_tracks(torrent_hash, tracks, upload_url, upload_token)

        if result:
            logger.info(f"  Stored successfully")
            uploaded += 1
        else:
            skipped += 1

        # Rate limit: 1 second between uploads
        if idx < total:
            time.sleep(1)

    logger.info(f"Upload complete: {uploaded} uploaded, {skipped} skipped out of {total}")


def process_library(music_dir: str, output_file: str, overwrite: bool = False,
                    upload_url: str = None, upload_token: str = None,
                    music_dirs: List[str] = None):
    """Main processing function - analyzes at album level using group IDs"""

    # First, scan for OPS torrents
    torrents = TorrentScanner.find_torrents(music_dir, music_dirs=music_dirs)
    
    if not torrents:
        logger.warning("No OPS torrents found! Exiting.")
        return
    
    total_torrents = len(torrents)
    logger.info(f"Found {total_torrents} OPS torrents to process")
    
    # Load existing data or start fresh
    if overwrite:
        logger.info("Overwrite mode: starting with empty dataset")
        library_data = {}
    else:
        library_data = load_existing_data(output_file)
        logger.info(f"Loaded existing data with {len(library_data)} groups")
    
    # Initialize analyzer
    analyzer = SonicAnalyzer()
    metadata_extractor = MusicMetadataExtractor()
    
    # Process each torrent
    processed = 0
    skipped = 0
    api_failures = 0
    
    for idx, torrent_info in enumerate(torrents, 1):
        logger.info(f"[{idx}/{total_torrents}] {os.path.basename(torrent_info['torrent_path'])}")

        # Calculate torrent hash
        torrent_hash = get_torrent_hash(torrent_info['torrent_path'])
        if not torrent_hash:
            logger.error("could not calculate torrent hash")
            skipped += 1
            continue

        # skip torrents already in the cache before querying the API (keyed by
        # infohash: standard + deluxe editions are distinct torrents)
        if torrent_hash in library_data:
            logger.info("already analyzed, skipping")
            processed += 1
            continue
        
        logger.info(f"Torrent hash: {torrent_hash}")
        
        # Query API for group ID
        api_response = lookup_torrent(torrent_hash)
        if not api_response or 'group' not in api_response:
            logger.warning(f"API lookup failed or returned no group data - skipping torrent")
            api_failures += 1
            skipped += 1
            continue
        
        group_id = api_response['group'].get('id')
        if not group_id:
            logger.warning(f"No group ID in API response - skipping torrent")
            skipped += 1
            continue
        
        group_id_str = str(group_id)
        logger.info(f"Group ID: {group_id_str}")
        
        # Get artist and album info for logging
        artist_name = "Unknown"
        album_name = api_response['group'].get('name', 'Unknown')
        
        if 'musicInfo' in api_response['group'] and 'artists' in api_response['group']['musicInfo']:
            artists = api_response['group']['musicInfo']['artists']
            if artists and len(artists) > 0:
                artist_name = artists[0].get('name', 'Unknown')
        
        logger.info(f"Artist: {artist_name} | Album: {album_name}")
        
        
        # Find audio files in album folder
        album_folder = torrent_info['album_folder']
        audio_files = find_audio_files_in_folder(album_folder)
        
        if not audio_files:
            logger.warning(f"No audio files found in album folder")
            skipped += 1
            continue
        
        logger.info(f"Found {len(audio_files)} tracks in album")
        
        # Analyze all tracks in the album
        track_analyses = []

        for track_file in sorted(audio_files):
            logger.info(f"  Analyzing: {os.path.basename(track_file)}")
            analysis = analyzer.analyze_file(track_file)

            if analysis:
                analysis['filename'] = os.path.basename(track_file)
                track_analyses.append(analysis)
            else:
                logger.warning(f"  Failed to analyze: {os.path.basename(track_file)}")

        if not track_analyses:
            logger.warning(f"Could not analyze any tracks in album")
            skipped += 1
            continue

        logger.info(f"Successfully analyzed {len(track_analyses)}/{len(audio_files)} tracks")

        # Store under infohash with metadata and per-track data. groupID is kept
        # for display/logging only; the fingerprint is keyed to the torrent.
        library_data[torrent_hash] = {
            'infohash': torrent_hash,
            'groupID': group_id,
            'artist': artist_name,
            'album': album_name,
            'year': api_response['group'].get('year'),
            'releaseType': api_response['group'].get('releaseTypeName'),
            'tags': api_response['group'].get('tags', []),
            'tracks': track_analyses,
        }

        processed += 1

        # Save after each album
        save_data(library_data, output_file)
        logger.info(f"Saved: {processed} total groups processed")

        # Upload per-track data if configured
        if upload_url and upload_token:
            result = upload_tracks(torrent_hash, track_analyses, upload_url, upload_token)
            if result:
                logger.info(f"  Uploaded {result.get('trackCount', '?')} tracks successfully")
            else:
                logger.warning(f"  Failed to upload tracks for {torrent_hash}")
            time.sleep(1)  # Rate limit
    
    # Final save
    save_data(library_data, output_file)
    
    logger.info(f"Done: {processed} analyzed, {skipped} skipped, "
                f"{api_failures} API lookup failures, {len(library_data)} in library")


def main():
    parser = argparse.ArgumentParser(
        description='Analyze music library and extract sonic features at album level (OPS torrents only)'
    )
    parser.add_argument(
        'directory',
        nargs='?',
        default='/torrents',
        help='Path to scan for .torrent files (default: /torrents, the Docker mount). '
             'Album folders are expected here too, unless --music-dir is specified.'
    )
    parser.add_argument(
        'output',
        nargs='?',
        default='/output/results.json',
        help='Path to output JSON file (default: /output/results.json, the Docker mount)'
    )
    parser.add_argument(
        '--overwrite',
        action='store_true',
        help='Overwrite existing JSON file instead of updating it'
    )
    parser.add_argument(
        '--api-token',
        default=os.environ.get('OPS_API_TOKEN', ''),
        help='API token for OPS (or set OPS_API_TOKEN env var)'
    )
    parser.add_argument(
        '--api-url',
        default=os.environ.get('OPS_API_URL', 'https://orpheus.network/ajax.php'),
        help='Base URL for the OPS AJAX API (or set OPS_API_URL env var)'
    )
    parser.add_argument(
        '--upload',
        action='store_true',
        help='Upload fingerprints to the Gazelle instance after analysis'
    )
    parser.add_argument(
        '--upload-only',
        nargs='?',
        const='/output/results.json',
        metavar='JSON_FILE',
        help='Skip analysis; upload fingerprints from an existing JSON file '
             '(default: /output/results.json)'
    )
    parser.add_argument(
        '--music-dir',
        action='append',
        metavar='DIR',
        help='Directory to search for album folders (use multiple times for multiple paths). '
             'Needed when .torrent files are stored separately from your music, e.g. rtorrent session dir.'
    )

    args = parser.parse_args()

    # Set module-level API config from CLI args
    global API_TOKEN, API_BASE_URL
    if args.api_token:
        API_TOKEN = args.api_token
    if args.api_url:
        API_BASE_URL = args.api_url

    # Upload-only mode
    if args.upload_only:
        if not API_TOKEN:
            logger.error("API token required for upload. Set OPS_API_TOKEN or use --api-token")
            sys.exit(1)
        logger.info("Upload-only mode: uploading existing fingerprints")
        upload_existing_data(args.upload_only, API_BASE_URL, API_TOKEN)
        return

    # Normal analysis mode
    if not args.directory or not args.output:
        parser.error("directory and output are required unless using --upload-only")

    if not os.path.isdir(args.directory):
        logger.error(f"Directory does not exist: {args.directory}")
        sys.exit(1)

    # Determine upload settings
    upload_url = None
    upload_token = None
    if args.upload:
        if not API_TOKEN:
            logger.error("API token required for upload. Set OPS_API_TOKEN or use --api-token")
            sys.exit(1)
        upload_url = API_BASE_URL
        upload_token = API_TOKEN

    logger.info("Analyzing OPS torrents you are seeding")
    if upload_url:
        logger.info(f"Uploading to {upload_url}")

    process_library(args.directory, args.output, args.overwrite,
                    upload_url=upload_url, upload_token=upload_token,
                    music_dirs=args.music_dir)

    logger.info("Analysis complete (see sonic_analysis.log)")


if __name__ == '__main__':
    main()
