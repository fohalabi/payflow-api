from __future__ import annotations
import asyncio
import secrets
from decimal import Decimal

from app.rails.base import BasePaymentRail, RailResponse, RailStatus


class WalletRail(BasePaymentRail):

    async def charge(
        self,
        amount: Decimal,
        currency: str,
        metadata: dict | None = None,
    ) -> RailResponse:
        await asyncio.sleep(0.02)

        ref = f"wallet_{secrets.token_hex(8)}"
        from_wallet = metadata.get("from_wallet") if metadata is not None else None
        to_wallet = metadata.get("to_wallet") if metadata is not None else None

        return RailResponse(
            status=RailStatus.SUCCESS,
            rail_reference=ref,
            message="Wallet transfer completed",
            raw_response={
                "reference": ref,
                "from_wallet": from_wallet,
                "to_wallet": to_wallet,
            },
        )

    async def verify(self, rail_reference: str) -> RailResponse:
        return RailResponse(
            status=RailStatus.SUCCESS,
            rail_reference=rail_reference,
            message="Wallet transfer verified",
        )

    async def refund(
        self,
        rail_reference: str,
        amount: Decimal,
    ) -> RailResponse:
        ref = f"wallet_refund_{secrets.token_hex(8)}"
        return RailResponse(
            status=RailStatus.SUCCESS,
            rail_reference=ref,
            message="Wallet refund completed",
        )