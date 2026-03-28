from __future__ import annotations
import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountStatus
from app.models.journal_entry import JournalEntry, EntryType
from app.models.transaction import Transaction

logger = logging.getLogger(__name__)


# Custom exceptions

class LedgerError(Exception):
    """Base exception for all ledger errors."""
    pass


class InsufficientFundsError(LedgerError):
    """Raised when an account has insufficient balance."""
    pass


class AccountFrozenError(LedgerError):
    """Raised when trying to transact on a frozen account."""
    pass


class AccountNotFoundError(LedgerError):
    """Raised when an account cannot be found."""
    pass


class LedgerImbalanceError(LedgerError):
    """
    Raised when debits and credits don't balance.
    This should never happen — if it does, it means
    there is a bug in the calling code.
    """
    pass


# Ledger service

class LedgerService:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _get_account_for_update(
        self,
        account_id: UUID,
    ) -> Account:
        """
        Fetch an account with a row-level lock.

        SELECT FOR UPDATE locks this row in PostgreSQL
        until the transaction commits or rolls back.
        Any other transaction trying to lock the same row
        will wait — this is what prevents race conditions
        on balance updates.
        """
        result = await self.db.execute(
            select(Account)
            .where(Account.id == account_id)
            .with_for_update()      # ← the lock
        )
        account = result.scalar_one_or_none()

        if account is None:
            raise AccountNotFoundError(
                f"Account {account_id} not found"
            )
        if account.status == AccountStatus.FROZEN:
            raise AccountFrozenError(
                f"Account {account_id} is frozen"
            )
        if account.status == AccountStatus.CLOSED:
            raise AccountFrozenError(
                f"Account {account_id} is closed"
            )
        return account

    async def _create_journal_entry(
        self,
        transaction: Transaction,
        account: Account,
        entry_type: EntryType,
        amount: Decimal,
        description: str | None = None,
    ) -> JournalEntry:
        """
        Create a single immutable journal entry.
        Updates the account's cached balance atomically.
        """
        # Calculate new running balance
        if entry_type == EntryType.CREDIT:
            new_balance = account.cached_balance + amount
        else:
            new_balance = account.cached_balance - amount

        # Create the immutable journal entry
        entry = JournalEntry(
            transaction_id=transaction.id,
            account_id=account.id,
            entry_type=entry_type,
            amount=amount,
            currency=transaction.currency,
            running_balance=new_balance,
            description=description,
            is_immutable=True,
        )
        self.db.add(entry)

        # Update the cached balance atomically in the same
        # database transaction — they always stay in sync.
        account.cached_balance = new_balance

        logger.info(
            f"Journal entry created: "
            f"{entry_type.value} {amount} {transaction.currency} "
            f"account={account.id} "
            f"new_balance={new_balance}"
        )
        return entry

    async def post_double_entry(
        self,
        transaction: Transaction,
        debit_account_id: UUID,
        credit_account_id: UUID,
        amount: Decimal,
        description: str | None = None,
    ) -> tuple[JournalEntry, JournalEntry]:
        """
        The core double-entry operation.

        Creates exactly two journal entries:
        - One DEBIT on debit_account_id
        - One CREDIT on credit_account_id

        Both entries are created in the same database
        transaction — they either both succeed or both
        fail. There is no in-between state.

        Always locks accounts in a consistent order
        (lower UUID first) to prevent deadlocks when
        two transactions try to lock the same two accounts
        in opposite orders simultaneously.
        """
        if amount <= Decimal("0"):
            raise LedgerError("Amount must be positive")

        # Sort account IDs to ensure consistent lock ordering
        # This prevents deadlocks between concurrent transactions
        ids = sorted([debit_account_id, credit_account_id], key=str)
        accounts: dict[UUID, Account] = {}

        for account_id in ids:
            accounts[account_id] = await self._get_account_for_update(
                account_id
            )

        debit_account = accounts[debit_account_id]
        credit_account = accounts[credit_account_id]

        # Verify sufficient funds before debiting
        if debit_account.cached_balance < amount:
            raise InsufficientFundsError(
                f"Insufficient funds in account {debit_account_id}. "
                f"Balance: {debit_account.cached_balance}, "
                f"Required: {amount}"
            )

        # Create the two entries
        debit_entry = await self._create_journal_entry(
            transaction=transaction,
            account=debit_account,
            entry_type=EntryType.DEBIT,
            amount=amount,
            description=description,
        )

        credit_entry = await self._create_journal_entry(
            transaction=transaction,
            account=credit_account,
            entry_type=EntryType.CREDIT,
            amount=amount,
            description=description,
        )

        # Verify the ledger balances — debits must equal credits.
        # This should always pass — if it doesn't, there is a bug.
        if debit_entry.amount != credit_entry.amount:
            raise LedgerImbalanceError(
                f"Ledger imbalance detected: "
                f"debit={debit_entry.amount} "
                f"credit={credit_entry.amount}"
            )

        return debit_entry, credit_entry

    async def get_balance(
        self,
        account_id: UUID,
    ) -> Decimal:
        """
        Get current balance from the cached_balance column.
        Fast path — single row lookup, no aggregation.
        """
        result = await self.db.execute(
            select(Account.cached_balance)
            .where(Account.id == account_id)
        )
        balance = result.scalar_one_or_none()
        if balance is None:
            raise AccountNotFoundError(
                f"Account {account_id} not found"
            )
        return balance

    async def get_true_balance(
        self,
        account_id: UUID,
    ) -> Decimal:
        """
        Calculate balance by summing all journal entries.
        Slow path — used for reconciliation and audits.

        If this differs from cached_balance, something
        has gone wrong and needs investigation.
        """
        from sqlalchemy import func, case

        result = await self.db.execute(
            select(
                func.coalesce(
                    func.sum(
                        case(
                            (
                                JournalEntry.entry_type == EntryType.CREDIT,
                                JournalEntry.amount,
                            ),
                            else_=-JournalEntry.amount,
                        )
                    ),
                    Decimal("0"),
                )
            ).where(JournalEntry.account_id == account_id)
        )
        return result.scalar_one()

    async def verify_balance_integrity(
        self,
        account_id: UUID,
    ) -> tuple[bool, Decimal, Decimal]:
        """
        Compares cached_balance against the true sum.
        Returns (is_consistent, cached, true_balance).
        Used by the reconciliation worker and audit endpoints.
        """
        cached = await self.get_balance(account_id)
        true_balance = await self.get_true_balance(account_id)
        is_consistent = cached == true_balance

        if not is_consistent:
            logger.error(
                f"Balance integrity failure: "
                f"account={account_id} "
                f"cached={cached} "
                f"true={true_balance} "
                f"diff={abs(cached - true_balance)}"
            )
        return is_consistent, cached, true_balance