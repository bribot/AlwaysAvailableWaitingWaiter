"""Request/response models."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class OrderItemIn(BaseModel):
    """One line in a createOrder/createPending request.

    `menu_id` is preferred; `name` is accepted as a fallback fuzzy match
    against the menu.
    """

    menu_id: str | None = None
    name: str | None = None
    quantity: int = Field(default=1, ge=1, le=99)
    modifiers: list[str] = Field(default_factory=list)

    @field_validator("modifiers")
    @classmethod
    def _clean_modifiers(cls, value: list[str]) -> list[str]:
        return [m.strip() for m in value if m and m.strip()][:8]

    @field_validator("menu_id", "name")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) and value.strip() else None


class CreateOrderRequest(BaseModel):
    items: list[OrderItemIn] = Field(default_factory=list, max_length=50)


class MenuItemOut(BaseModel):
    id: str
    name: str
    category: str
    price: float
    prep_minutes: int
    available: bool


class DisplayItemOut(BaseModel):
    """One line on the e-paper panel, pre-formatted for the firmware."""

    itemName: str
    details: str
    price: float
    time: float
    delivered: bool


class ItemDetailOut(DisplayItemOut):
    """Everything about one order line, for the single-item lookup."""

    menu_id: str | None
    quantity: int
    modifiers: list[str]


class DeviceOut(BaseModel):
    device_number: int
    table_number: int
    order_id: int | None
    order: list[DisplayItemOut]


class AssignDeviceRequest(BaseModel):
    table_number: int = Field(ge=1, le=999)


class TableSummaryOut(BaseModel):
    """One row in the admin dashboard's table list - no items, just counts."""

    table_number: int
    version: int
    order_id: int | None
    item_count: int
    pending_count: int
