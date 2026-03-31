from __future__ import annotations
import asyncio
import random
import secrets
from decimal import Decimal

from app.rails.base import BasePaymentRail, RailResponse, RailStatus


class CardRail(BasePaymentRail):

    async def charge(
        self,
        amount: Decimal,
        currency: str,
        metadata: dict | None = None,
    ) -> RailResponse:
        await asyncio.sleep(random.uniform(0.1, 0.4))

        if random.random() < 0.10:
            return RailResponse(
                status=RailStatus.FAILED,
                rail_reference="",
                message="Card declined by issuer",
                raw_response={"code": "card_declined"},
            )

        ref = f"card_{secrets.token_hex(8)}"
        card_last_four = (
            str(metadata.get("card_last_four", "4242"))
            if metadata is not None
            else "4242"
        )

        return RailResponse(
            status=RailStatus.SUCCESS,
            rail_reference=ref,
            message="Card charge successful",
            raw_response={
                "reference": ref,
                "amount": str(amount),
                "currency": currency,
                "card_last_four": card_last_four,
            },
        )

    async def verify(self, rail_reference: str) -> RailResponse:
        await asyncio.sleep(0.05)
        return RailResponse(
            status=RailStatus.SUCCESS,
            rail_reference=rail_reference,
            message="Charge verified",
        )

    async def refund(
        self,
        rail_reference: str,
        amount: Decimal,
    ) -> RailResponse:
        await asyncio.sleep(random.uniform(0.1, 0.3))
        ref = f"refund_{secrets.token_hex(8)}"
        return RailResponse(
            status=RailStatus.SUCCESS,
            rail_reference=ref,
            message="Refund processed",
            raw_response={"original_reference": rail_reference},
        )