"""
Application configuration using Pydantic Settings.
Loads configuration from environment variables or .env file.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator, ValidationError
from pathlib import Path
import socket
import logging
import sys

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
    log_level: str = "INFO"  # Logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL

    # Server Configuration
    server_host: str = ""  # Auto-detect if not set
    server_port: int = 8000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    @field_validator('admin_password')
    @classmethod
    def validate_admin_password(cls, v: str) -> str:
        """Validate admin password is not empty and has minimum length."""
        if not v or len(v.strip()) == 0:
            raise ValueError("ADMIN_PASSWORD cannot be empty")
        if len(v) < 4:
            raise ValueError("ADMIN_PASSWORD must be at least 4 characters")
        return v

    @field_validator('youtube_api_key')
    @classmethod
    def validate_youtube_api_key(cls, v: str) -> str:
        """Validate YouTube API key is not empty."""
        if not v or len(v.strip()) == 0:
            raise ValueError("YOUTUBE_API_KEY cannot be empty")
        return v

    @field_validator('secret_key')
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        """Validate secret key is not empty and has minimum length."""
        if not v or len(v.strip()) == 0:
            raise ValueError("SECRET_KEY cannot be empty")
        if len(v) < 32:
            raise ValueError(
                "SECRET_KEY must be at least 32 characters for security. "
                "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return v

    @field_validator('log_level')
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level is valid."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(
                f"LOG_LEVEL must be one of: {', '.join(valid_levels)}. Got: {v}"
            )
        return v_upper

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


def load_settings() -> Settings:
    """
    Load and validate settings with helpful error messages.

    Returns:
        Settings instance

    Exits:
        System exit with code 1 if validation fails
    """
    try:
        return Settings()
    except ValidationError as e:
        logger.error("=" * 60)
        logger.error("CONFIGURATION ERROR - Missing or invalid environment variables")
        logger.error("=" * 60)

        for error in e.errors():
            field = error['loc'][0] if error['loc'] else 'unknown'
            msg = error['msg']

            # Convert field name to env var format
            env_var = field.upper()

            logger.error(f"❌ {env_var}: {msg}")

        logger.error("")
        logger.error("Required environment variables:")
        logger.error("  - ADMIN_PASSWORD: Admin login password (min 4 characters)")
        logger.error("  - YOUTUBE_API_KEY: YouTube Data API v3 key")
        logger.error("  - SECRET_KEY: Session signing key (min 32 characters)")
        logger.error("")
        logger.error("Optional environment variables:")
        logger.error("  - LOG_LEVEL: Logging level - DEBUG, INFO, WARNING, ERROR, CRITICAL (default: INFO)")
        logger.error("  - SERVER_HOST: Server IP for Chromecast (auto-detected in dev)")
        logger.error("  - SERVER_PORT: Server port (default: 8000)")
        logger.error("  - DATA_DIR: Data directory path (default: ./data)")
        logger.error("")
        logger.error("Create a .env file or set these environment variables.")
        logger.error("See DEVELOPMENT.md for setup instructions.")
        logger.error("=" * 60)

        sys.exit(1)


# Global settings instance
settings = load_settings()
