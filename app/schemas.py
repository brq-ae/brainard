"""Pydantic request/response models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MachineCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class MachineCreateResponse(BaseModel):
    id: str
    name: str
    token: str  # plaintext -- shown exactly once, never retrievable again


class MachineListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    created_at: datetime
    last_seen: datetime | None
    status: str


class MachineRevokeResponse(BaseModel):
    id: str
    status: str


class HealthResponse(BaseModel):
    ok: bool
    database: str
