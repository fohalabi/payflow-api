from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum as PyEnum


class RailStatus(str, PyEnum):
    SUCCESS  = "success"
    FAILED   = "failed"
    PENDING  = "pending"    # awaiting external confirmation


@dataclass
class RailResponse:
    status: RailStatus
    rail_reference: str         # external reference from the rail
    message: str
    raw_response: dict | None = None


class BasePaymentRail(ABC):

    @abstractmethod
    async def charge(
        self,
        amount: Decimal,
        currency: str,
        metadata: dict | None = None,
    ) -> RailResponse:
        """Initiate a charge on this rail."""
        ...

    @abstractmethod
    async def verify(self, rail_reference: str) -> RailResponse:
        """Check the status of a charge by its rail reference."""
        ...

    @abstractmethod
    async def refund(
        self,
        rail_reference: str,
        amount: Decimal,
    ) -> RailResponse:
        """Reverse or refund a charge."""
        ...