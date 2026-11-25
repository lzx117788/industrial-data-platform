import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Nonempty = Annotated[str, Field(min_length=1, max_length=128)]


def parse_time(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timestamp must include timezone, e.g. 2026-11-25T08:00:00+08:00")
    return value.astimezone(UTC).replace(tzinfo=None)


def iso(value):
    return value.isoformat(timespec="milliseconds") + "Z" if value else None


class BaseRecord(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    external_id: Nonempty
    updated_at: datetime

    @field_validator("updated_at", mode="before")
    @classmethod
    def timestamp(cls, value):
        return parse_time(value)


class DeviceRecord(BaseRecord):
    name: Nonempty
    category: Nonempty
    location: Nonempty
    status: Literal["active", "inactive", "maintenance"]
    aliases: dict[Nonempty, Nonempty] = Field(default_factory=dict)

    @field_validator("aliases", mode="before")
    @classmethod
    def aliases_json(cls, value):
        return json.loads(value) if isinstance(value, str) else value


class AlarmRecord(BaseRecord):
    device_ref: Nonempty
    code: Annotated[str, Field(min_length=1, max_length=64)]
    severity: Literal["info", "warning", "critical"]
    message: Annotated[str, Field(min_length=1, max_length=4000)]
    occurred_at: datetime

    @field_validator("severity", mode="before")
    @classmethod
    def normalize_severity(cls, value):
        mapping = {
            "INFO": "info",
            "LOW": "info",
            "WARN": "warning",
            "HIGH": "critical",
            "CRIT": "critical",
        }
        return mapping.get(str(value).strip().upper(), str(value).strip().lower())

    @field_validator("occurred_at", mode="before")
    @classmethod
    def occurred_time(cls, value):
        return parse_time(value)


class MaintenanceRecord(BaseRecord):
    device_ref: Nonempty
    description: Annotated[str, Field(min_length=1, max_length=4000)]
    technician: Annotated[str, Field(min_length=1, max_length=64)]
    cost: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    performed_at: datetime

    @field_validator("performed_at", mode="before")
    @classmethod
    def performed_time(cls, value):
        return parse_time(value)


RECORD_TYPES = {"device": DeviceRecord, "alarm": AlarmRecord, "maintenance": MaintenanceRecord}


class SourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    code: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    name: Nonempty
    kind: Literal["csv", "http", "sqlite"]
    entity: Literal["device", "alarm", "maintenance"]
    config: dict


class SyncRequest(BaseModel):
    full: bool = False
