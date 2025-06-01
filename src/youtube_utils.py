"""
youtube_utils.py

YouTube search and download utilities using pytubefix.
"""
from pytubefix import Search, YouTube
import os
import structlog
import time
import httpx
import re

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer()
    ]
)
logger = structlog.get_logger()

def parse_iso8601_duration(duration):
    """
    Convert ISO 8601 duration (e.g. 'PT6M26S') to '6:26' or '1:02:03'.
    """
    match = re.match(
        r'PT(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?',
        duration
    )
    if not match:
        return duration  # fallback to original if parsing fails
    parts = match.groupdict(default='0')
    hours = int(parts['hours'])
    minutes = int(parts['minutes'])
    seconds = int(parts['seconds'])
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    else:
        return f"{minutes}:{seconds:02}"

def search_youtube(query: str, max_results: int = 10):
    """
    Search YouTube for videos matching the query using pytubefix, then retrieve channel, duration, and view count
    using the YouTube Data API. Returns a list of dicts with title, youtube_id, url, channel, duration, and view_count.
    Requires the YOUTUBE_API_KEY environment variable to be set.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise RuntimeError("Please set the YOUTUBE_API_KEY environment variable.")
    query = (query or '').strip()
    if not query:
        logger.warning("search_youtube_empty_query")
        return []
    search_term = query + ' karaoke'
    logger.info("search_youtube_start", search_term=search_term, max_results=max_results)
    search_start = time.monotonic()
    results = Search(search_term).videos[:max_results]
    search_elapsed = time.monotonic() - search_start
    logger.info("search_youtube_pytubefix_time", elapsed=f"{search_elapsed:.2f}s", result_count=len(results))
    video_ids = [video.video_id for video in results]
    if not video_ids:
        return []
    api_url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet,contentDetails,statistics",
        "id": ",".join(video_ids),
        "key": api_key
    }
    api_start = time.monotonic()
    resp = httpx.get(api_url, params=params)
    resp.raise_for_status()
    data = resp.json()
    api_elapsed = time.monotonic() - api_start
    logger.info("search_youtube_dataapi_time", elapsed=f"{api_elapsed:.2f}s", api_result_count=len(data.get('items', [])))
    output = []
    for item in data.get('items', []):
        vid = item['id']
        snippet = item['snippet']
        title = snippet['title']
        channel = snippet['channelTitle']
        duration = parse_iso8601_duration(item['contentDetails']['duration'])
        view_count = item['statistics'].get('viewCount', 'N/A')
        url = f'https://youtube.com/watch?v={vid}'
        output.append({
            'title': title,
            'youtube_id': vid,
            'url': url,
            'channel': channel,
            'duration': duration,
            'view_count': view_count
        })
    return output

def download_youtube_video(youtube_id: str, target_dir: str = 'data') -> str:
    """
    Download a YouTube video by ID using pytubefix.
    Saves the file in target_dir with the YouTube ID as filename (mp4).
    Returns the file path if successful, or raises an exception.
    """
    logger.info("download_youtube_video_called", youtube_id=youtube_id, target_dir=target_dir)
    youtube_id = (youtube_id or '').strip()
    if not youtube_id:
        logger.warning("download_youtube_video_invalid_id")
        raise ValueError('Invalid YouTube ID')
    os.makedirs(target_dir, exist_ok=True)
    file_path = os.path.join(target_dir, f'{youtube_id}.mp4')
    if os.path.exists(file_path):
        logger.info("download_youtube_video_already_exists", youtube_id=youtube_id, file_path=file_path)
        return file_path
    url = f'https://youtube.com/watch?v={youtube_id}'
    try:
        yt = YouTube(url)
        stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
        if not stream:
            logger.error("download_youtube_video_no_stream", youtube_id=youtube_id)
            raise RuntimeError('No suitable stream found')
        stream.download(output_path=target_dir, filename=f'{youtube_id}.mp4')
        logger.info("download_youtube_video_success", youtube_id=youtube_id, file_path=file_path)
        return file_path
    except Exception as e:
        logger.error("download_youtube_video_error", youtube_id=youtube_id, error=str(e))
        raise 