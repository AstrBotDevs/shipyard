from sqlmodel import SQLModel, Field, Column, DateTime
from pydantic import BaseModel, ConfigDict
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import uuid


# Database Models
class ShipBase(SQLModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    status: int = Field(default=1, description="1: running, 0: stopped")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
    )
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
    )
    container_id: Optional[str] = Field(default=None)
    ip_address: Optional[str] = Field(default=None)
    ttl: int = Field(description="Time to live in seconds")
    max_session_num: int = Field(
        default=1, description="Maximum number of sessions that can use this ship"
    )
    current_session_num: int = Field(
        default=0, description="Current number of active sessions"
    )


class Ship(ShipBase, table=True):
    __tablename__ = "ships"  # type: ignore


class SessionShipBase(SQLModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    session_id: str = Field(description="Session ID")
    ship_id: str = Field(foreign_key="ships.id", description="Ship ID")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
    )
    last_activity: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
    )
    expires_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True)),
        description="When this session's access to the ship expires"
    )
    initial_ttl: int = Field(
        description="Initial TTL in seconds for this session (used for refresh)"
    )


class SessionShip(SessionShipBase, table=True):
    __tablename__ = "session_ships"  # type: ignore


# API Request/Response Models
class ShipSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpus: Optional[float] = Field(None, gt=0, description="CPU allocation")
    memory: Optional[str] = Field(
        None, description="Memory allocation, e.g., '512m', '1g'"
    )


class CreateShipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl: int = Field(..., gt=0, description="Time to live in seconds")
    spec: Optional[ShipSpec] = Field(None, description="Ship specifications")
    max_session_num: int = Field(
        default=1, gt=0, description="Maximum number of sessions that can use this ship"
    )


class ShipResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: int
    created_at: datetime
    updated_at: datetime
    container_id: Optional[str]
    ip_address: Optional[str]
    ttl: int
    max_session_num: int
    current_session_num: int


class ExecRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(
        ..., description="Operation type, e.g., 'shell/exec', 'ipython/exec'"
    )
    payload: Optional[Dict[str, Any]] = Field(None, description="Operation payload")


class ExecResponse(BaseModel):
    success: bool
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class ExtendTTLRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl: int = Field(..., gt=0, description="New TTL in seconds")


class ErrorResponse(BaseModel):
    detail: str


class LogsResponse(BaseModel):
    logs: str


class UploadFileResponse(BaseModel):
    success: bool
    message: str
    file_path: Optional[str] = None
    error: Optional[str] = None


class DownloadFileResponse(BaseModel):
    success: bool
    message: str
    error: Optional[str] = None
