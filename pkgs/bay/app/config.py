from pydantic import Field
from pydantic_settings import BaseSettings
from typing import Literal


class Settings(BaseSettings):
    # Server settings
    host: str = Field(default="0.0.0.0", description="Server host")
    port: int = Field(default=8000, description="Server port")
    debug: bool = Field(default=False, description="Debug mode")

    # Ship management settings
    max_ship_num: int = Field(default=10, description="Maximum number of ships")
    behavior_after_max_ship: Literal["reject", "wait"] = Field(
        default="wait", description="Behavior when max ships reached"
    )

    # Authentication
    access_token: str = Field(
        default="secret-token", description="Access token for ship operations"
    )

    # Database settings
    database_url: str = Field(
        default="sqlite+aiosqlite:///./bay.db", description="Database connection URL"
    )

    # Docker settings
    docker_image: str = Field(default="ship:latest", description="Ship container image")
    docker_network: str = Field(default="shipyard", description="Docker network name")

    # Ship default settings
    default_ship_ttl: int = Field(
        default=3600, description="Default ship TTL in seconds"
    )
    default_ship_cpus: float = Field(
        default=1.0, description="Default ship CPU allocation"
    )
    default_ship_memory: str = Field(
        default="512m", description="Default ship memory allocation"
    )

    # Ship health check settings
    ship_health_check_timeout: int = Field(
        default=60, description="Maximum timeout for ship health check in seconds"
    )
    ship_health_check_interval: int = Field(
        default=2, description="Health check interval in seconds"
    )

    # File upload settings
    max_upload_size: int = Field(
        default=100 * 1024 * 1024,
        description="Maximum file upload size in bytes (default: 100MB)",
    )

    ship_data_dir: str = Field(
        default="~/ship_data", description="Base directory for ship data storage"
    )

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
