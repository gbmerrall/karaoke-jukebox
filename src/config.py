"""
Configuration settings for the karaoke app.
"""
import os
from typing import Optional
from pydantic_settings import BaseSettings

class QueueSettings(BaseSettings):
    """Queue-related configuration settings."""
    # Queue cleanup settings
    CLEANUP_THRESHOLD_HOURS: int = int(os.getenv("QUEUE_CLEANUP_THRESHOLD_HOURS", "4"))
    CLEANUP_INTERVAL_HOURS: int = int(os.getenv("QUEUE_CLEANUP_INTERVAL_HOURS", "1"))
    
    # Queue size limits
    MAX_QUEUE_SIZE: Optional[int] = int(os.getenv("MAX_QUEUE_SIZE", "0"))  # 0 means no limit
    
    # self-referential address of the server for chromecast to connect to
    # needs to be specified since Docker does not have a public IP address
    SERVER_ADDRESS: str = os.getenv("SERVER_ADDRESS", "http://192.168.86.250:8100")

    class Config:
        env_file = ".env"
        case_sensitive = True

# Create a global settings instance
queue_settings = QueueSettings() 