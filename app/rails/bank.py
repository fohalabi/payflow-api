from __future__ import annotations
import asyncio
import random
import secrets
from decimal import Decimal

from app.rails.base import BasePaymentRail, RailResponse, RailStatus


class BankTransferRail(BasePaymentRail):

    async def charge(
        self,
        amount: Decimal,
        currency: str,
        metadata: dict | None = None,
    ) -> RailResponse:
        await asyncio.sleep(random.uniform(0.2, 0.6))

        ref = f"bank_{secrets.token_hex(8)}"
        account_number = (
            str(metadata.get("account_number", "0123456789"))
            if metadata is not None
            else "0123456789"
        )
        bank_code = (
            str(metadata.get("bank_code", "044"))
            if metadata is not None
            else "044"
        )

        return RailResponse(
            status=RailStatus.PENDING,
            rail_reference=ref,
            message="Transfer initiated — awaiting bank confirmation",
            raw_response={
                "reference": ref,
                "account_number": account_number,
                "bank_code": bank_code,
            },
        )

    async def verify(self, rail_reference: str) -> RailResponse:
        await asyncio.sleep(0.1)
        if random.random() < 0.90:
            return RailResponse(
                status=RailStatus.SUCCESS,
                rail_reference=rail_reference,
                message="Transfer settled",
            )
        return RailResponse(
            status=RailStatus.PENDING,
            rail_reference=rail_reference,
            message="Transfer still pending",
        )

    async def refund(
        self,
        rail_reference: str,
        amount: Decimal,
    ) -> RailResponse:
        await asyncio.sleep(random.uniform(0.3, 0.8))
        ref = f"reversal_{secrets.token_hex(8)}"
        return RailResponse(
            status=RailStatus.SUCCESS,
            rail_reference=ref,
            message="Transfer reversal initiated",
            raw_response={"original_reference": rail_reference},
        )