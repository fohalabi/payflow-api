from __future__ import annotations
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.api.router import CurrentMerchant, DBSession, RedisClient
from app.models.transaction import Transaction, TransactionStatus
from app.schemas.transaction import (
    IdempotencyResponse,
    TransactionCreate,
    TransactionDetail,
    TransactionFilter,
    TransactionListResponse,
    TransactionResponse,
)
from app.services.transaction_engine import (
    FraudBlockedError,
    TransactionEngine,
    TransactionError,
)

router = APIRouter(prefix="/transactions", tags=["transactions"])


@router.post(
    "",
    response_model=TransactionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_transaction(
    payload: TransactionCreate,
    current: CurrentMerchant,
    db: DBSession,
    redis: RedisClient,
) -> TransactionResponse:
    engine = TransactionEngine(db, redis)

    try:
        result, is_duplicate = await engine.process_payment(
            merchant_id=current.id,
            payload=payload,
        )
    except FraudBlockedError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    except TransactionError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    # Fetch the created transaction to return full response
    txn_result = await db.execute(
        select(Transaction).where(
            Transaction.reference == result["reference"]
        )
    )
    transaction = txn_result.scalar_one()

    response = TransactionResponse.model_validate(transaction)

    if is_duplicate:
        # Surface idempotency hit in response headers
        # handled at middleware level — just return result
        pass

    return response


@router.get("", response_model=TransactionListResponse)
async def list_transactions(
    current: CurrentMerchant,
    db: DBSession,
    status_filter: TransactionStatus | None = Query(default=None, alias="status"),
    currency: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> TransactionListResponse:
    base_query = select(Transaction).where(
        Transaction.merchant_id == current.id
    )

    if status_filter is not None:
        base_query = base_query.where(Transaction.status == status_filter)
    if currency is not None:
        base_query = base_query.where(Transaction.currency == currency.upper())

    # Total count
    count_result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    total = count_result.scalar_one()

    # Paginated results
    offset = (page - 1) * page_size
    result = await db.execute(
        base_query
        .order_by(Transaction.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    transactions = result.scalars().all()

    return TransactionListResponse(
        items=[TransactionResponse.model_validate(t) for t in transactions],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=-(-total // page_size),  # ceiling division
    )


@router.get("/{transaction_id}", response_model=TransactionDetail)
async def get_transaction(
    transaction_id: UUID,
    current: CurrentMerchant,
    db: DBSession,
) -> TransactionDetail:
    result = await db.execute(
        select(Transaction)
        .where(
            Transaction.id == transaction_id,
            Transaction.merchant_id == current.id,
        )
        .options(selectinload(Transaction.journal_entries))
    )
    transaction = result.scalar_one_or_none()

    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transaction not found",
        )

    return TransactionDetail.model_validate(transaction)