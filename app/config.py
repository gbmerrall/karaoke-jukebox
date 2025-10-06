"""
Application configuration using Pydantic Settings.
Loads configuration from environment variables or .env file.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
import socket
import logging

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Admin Configuration
    admin_password: str

    # YouTube API Configuration
    youtube_api_key: str

    # Queue Management
    queue_cleanup_threshold_hours: int = 4
    queue_cleanup_interval_hours: int = 1
    max_queue_size: int = 0  # 0 means unlimited

    # Application Configuration
    secret_key: str
    data_dir: Path = Path("./data")

    # Server Configuration
    server_host: str = ""  # Auto-detect if not set
    server_port: int = 8000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    def get_db_path(self) -> Path:
        """Get the full path to the SQLite database file."""
        return self.data_dir / "karaoke.db"

    def get_videos_dir(self) -> Path:
        """Get the full path to the videos directory."""
        return self.data_dir / "videos"

    def get_video_path(self, video_id: str) -> Path:
        """Get the full path to a specific video file."""
        return self.get_videos_dir() / f"{video_id}.mp4"

    def is_docker(self) -> bool:
        """
        Check if running inside a Docker container.

        Returns:
            True if running in Docker, False otherwise
        """
        return Path("/.dockerenv").exists()

    def get_local_ip(self) -> str:
        """
        Auto-detect the local network IP address.

        Returns:
            Local IP address or 'localhost' if detection fails
        """
        try:
            # Create a socket connection to determine local IP
            # This doesn't actually send data, just determines routing
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            return local_ip
        except Exception as e:
            logger.warning(f"Failed to auto-detect local IP: {e}")
            return "localhost"

    def get_server_host(self) -> str:
        """
        Get the server host for generating Chromecast URLs.

        Returns:
            Server host (IP or hostname)

        Logic:
        - If SERVER_HOST is explicitly set, use it
        - Else if running in Docker, return empty (requires explicit config)
        - Else auto-detect local network IP
        """
        if self.server_host:
            # Explicit configuration wins
            return self.server_host

        if self.is_docker():
            # Docker requires explicit configuration
            logger.error(
                "Running in Docker but SERVER_HOST is not set! "
                "Chromecast will not be able to reach the server. "
                "Set SERVER_HOST to the Docker host's IP address in your .env file."
            )
            return ""

        # Auto-detect for development
        detected_ip = self.get_local_ip()
        logger.info(f"Auto-detected server host: {detected_ip}")
        return detected_ip

    def get_video_url(self, video_id: str, request_host: str = None) -> str:
        """
        Generate the HTTP URL for a video file that Chromecast can access.

        Args:
            video_id: YouTube video ID
            request_host: Optional host from the current request (e.g., request.url.hostname)

        Returns:
            Full HTTP URL to the video file
        """
        host = self.get_server_host() or request_host or "localhost"
        url = f"http://{host}:{self.server_port}/data/videos/{video_id}.mp4"

        if not host or host == "localhost":
            logger.warning(
                f"Generated Chromecast URL with '{host}' - this may not be accessible! "
                f"URL: {url}"
            )

        return url


# Global settings instance
settings = Settings()
