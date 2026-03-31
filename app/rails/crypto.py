from __future__ import annotations
import asyncio
import random
import secrets
from decimal import Decimal

from app.rails.base import BasePaymentRail, RailResponse, RailStatus


class CryptoRail(BasePaymentRail):

    async def charge(
        self,
        amount: Decimal,
        currency: str,
        metadata: dict | None = None,
    ) -> RailResponse:
        # Simulate broadcast delay
        await asyncio.sleep(random.uniform(0.3, 0.8))

        tx_hash = f"0x{secrets.token_hex(32)}"
        wallet_address = (
            str(metadata.get("wallet_address", "0x0000000000000000000000000000000000000000"))
            if metadata is not None
            else "0x0000000000000000000000000000000000000000"
        )
        network = (
            str(metadata.get("network", "ethereum"))
            if metadata is not None
            else "ethereum"
        )

        # Crypto always starts as PENDING — needs confirmations
        return RailResponse(
            status=RailStatus.PENDING,
            rail_reference=tx_hash,
            message="Transaction broadcast — awaiting confirmations",
            raw_response={
                "tx_hash": tx_hash,
                "wallet_address": wallet_address,
                "network": network,
                "amount": str(amount),
                "currency": currency,
                "confirmations_required": 3,
                "confirmations_received": 0,
            },
        )

    async def verify(self, rail_reference: str) -> RailResponse:
        await asyncio.sleep(random.uniform(0.2, 0.5))

        # Simulate confirmation progress
        confirmations = random.randint(0, 6)
        if confirmations >= 3:
            return RailResponse(
                status=RailStatus.SUCCESS,
                rail_reference=rail_reference,
                message=f"Transaction confirmed ({confirmations} confirmations)",
                raw_response={
                    "tx_hash": rail_reference,
                    "confirmations_received": confirmations,
                },
            )

        return RailResponse(
            status=RailStatus.PENDING,
            rail_reference=rail_reference,
            message=f"Awaiting confirmations ({confirmations}/3)",
            raw_response={
                "tx_hash": rail_reference,
                "confirmations_received": confirmations,
            },
        )

    async def refund(
        self,
        rail_reference: str,
        amount: Decimal,
    ) -> RailResponse:
        await asyncio.sleep(random.uniform(0.3, 0.8))
        tx_hash = f"0x{secrets.token_hex(32)}"
        return RailResponse(
            status=RailStatus.PENDING,
            rail_reference=tx_hash,
            message="Refund transaction broadcast — awaiting confirmations",
            raw_response={
                "original_tx_hash": rail_reference,
                "refund_tx_hash": tx_hash,
            },
        )