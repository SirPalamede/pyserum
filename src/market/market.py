"""Market module to interact with Serum DEX."""
from __future__ import annotations

import logging
from typing import List

from solana.account import Account
from solana.publickey import PublicKey
from solana.rpc.api import Client
from solana.transaction import Transaction, TransactionInstruction
from spl.token.constants import WRAPPED_SOL_MINT  # type: ignore # TODO: Remove ignore.

import src.instructions as instructions
import src.market.types as t

from .._layouts.open_orders import OPEN_ORDERS_LAYOUT
from ..enums import OrderType, Side
from ..open_orders_account import OpenOrdersAccount, make_create_account_instruction
from ..utils import load_bytes_data
from ._internal.queue import decode_event_queue, decode_request_queue
from .orderbook import OrderBook
from .state import MarketState


# pylint: disable=too-many-public-methods
class Market:
    """Represents a Serum Market."""

    logger = logging.getLogger("pyserum.market.Market")

    def __init__(
        self,
        conn: Client,
        market_state: MarketState,
        opts: t.MarketOpts = t.MarketOpts(),
    ) -> None:
        self._skip_preflight = opts.skip_preflight
        self._confirmations = opts.confirmations
        self._conn = conn

        self.state = market_state

    @staticmethod
    # pylint: disable=unused-argument
    def load(
        conn: Client,
        market_address: PublicKey,
        program_id: PublicKey = instructions.DEFAULT_DEX_PROGRAM_ID,
        opts: t.MarketOpts = t.MarketOpts(),
    ) -> Market:
        """Factory method to create a Market."""
        market_state = MarketState.load(conn, market_address, program_id)
        return Market(conn, market_state, opts)

    def support_srm_fee_discounts(self) -> bool:
        raise NotImplementedError("support_srm_fee_discounts not implemented")

    def find_fee_discount_keys(self, owner: PublicKey, cache_duration: int):
        raise NotImplementedError("find_fee_discount_keys not implemented")

    def find_best_fee_discount_key(self, owner: PublicKey, cache_duration: int):
        raise NotImplementedError("find_best_fee_discount_key not implemented")

    def find_open_orders_accounts_for_owner(self, owner_address: PublicKey) -> List[OpenOrdersAccount]:
        return OpenOrdersAccount.find_for_market_and_owner(
            self._conn, self.state.public_key(), owner_address, self.state.program_id()
        )

    def find_quote_token_accounts_for_owner(self, owner_address: PublicKey, include_unwrapped_sol: bool = False):
        raise NotImplementedError("find_quote_token_accounts_for_owner not implemented")

    def load_bids(self) -> OrderBook:
        """Load the bid order book"""
        bytes_data = load_bytes_data(self.state.bids(), self._conn)
        return OrderBook.from_bytes(self.state, bytes_data)

    def load_asks(self) -> OrderBook:
        """Load the Ask order book."""
        bytes_data = load_bytes_data(self.state.asks(), self._conn)
        return OrderBook.from_bytes(self.state, bytes_data)

    def load_orders_for_owner(self) -> List[t.Order]:
        raise NotImplementedError("load_orders_for_owner not implemented")

    def load_base_token_for_owner(self):
        raise NotImplementedError("load_base_token_for_owner not implemented")

    def load_event_queue(self) -> List[t.Event]:
        bytes_data = load_bytes_data(self.state.event_queue(), self._conn)
        return decode_event_queue(bytes_data)

    def load_request_queue(self) -> List[t.Request]:
        bytes_data = load_bytes_data(self.state.request_queue(), self._conn)
        return decode_request_queue(bytes_data)

    def load_fills(self, limit=100) -> List[t.FilledOrder]:
        bytes_data = load_bytes_data(self.state.event_queue(), self._conn)
        events = decode_event_queue(bytes_data, limit)
        return [
            self.parse_fill_event(event)
            for event in events
            if event.event_flags.fill and event.native_quantity_paid > 0
        ]

    def parse_fill_event(self, event) -> t.FilledOrder:
        if event.event_flags.bid:
            side = Side.Buy
            price_before_fees = (
                event.native_quantity_released + event.native_fee_or_rebate
                if event.event_flags.maker
                else event.native_quantity_released - event.native_fee_or_rebate
            )
        else:
            side = Side.Sell
            price_before_fees = (
                event.native_quantity_released - event.native_fee_or_rebate
                if event.event_flags.maker
                else event.native_quantity_released + event.native_fee_or_rebate
            )

        price = (price_before_fees * self.state.base_spl_token_multiplier()) / (
            self.state.quote_spl_token_multiplier() * event.native_quantity_paid
        )
        size = event.native_quantity_paid / self.state.base_spl_token_multiplier()
        return t.FilledOrder(
            order_id=int.from_bytes(event.order_id, "little"),
            side=side,
            price=price,
            size=size,
            fee_cost=event.native_fee_or_rebate * (1 if event.event_flags.maker else -1),
        )

    def place_order(  # pylint: disable=too-many-arguments
        self,
        payer: PublicKey,
        owner: Account,
        order_type: OrderType,
        side: Side,
        limit_price: int,
        max_quantity: int,
        client_id: int = 0,
    ):  # TODO: Add open_orders_address_key param and fee_discount_pubkey
        transaction = Transaction()
        signers: List[Account] = [owner]
        open_order_accounts = self.find_open_orders_accounts_for_owner(owner.public_key())
        if not open_order_accounts:
            new_open_orders_account = Account()
            mbfre_resp = self._conn.get_minimum_balance_for_rent_exemption(OPEN_ORDERS_LAYOUT.sizeof())
            balanced_needed = mbfre_resp["result"]
            transaction.add(
                make_create_account_instruction(
                    owner.public_key(),
                    new_open_orders_account.public_key(),
                    balanced_needed,
                    self.state.program_id(),
                )
            )
            signers.append(new_open_orders_account)
            # TODO: Cache new_open_orders_account

        # TODO: Handle open_orders_address_key
        # TODO: Handle fee_discount_pubkey

        if payer == owner.public_key():
            raise ValueError("Invalid payer account")
        if (side == side.Buy and self.state.quote_mint() == WRAPPED_SOL_MINT) or (
            side == side.Sell and self.state.base_mint == WRAPPED_SOL_MINT
        ):
            # TODO: Handle wrapped sol account
            raise NotImplementedError("WRAPPED_SOL_MINT is currently unsupported")

        transaction.add(
            self.make_place_order_instruction(
                payer,
                owner,
                order_type,
                side,
                limit_price,
                max_quantity,
                client_id,
                open_order_accounts[0].address if open_order_accounts else new_open_orders_account.public_key(),
            )
        )
        return self._send_transaction(transaction, *signers)

    def make_place_order_instruction(  # pylint: disable=too-many-arguments
        self,
        payer: PublicKey,
        owner: Account,
        order_type: OrderType,
        side: Side,
        limit_price: int,
        max_quantity: int,
        client_id: int,
        open_order_account: PublicKey,
    ) -> TransactionInstruction:
        if self.state.base_size_number_to_lots(max_quantity) < 0:
            raise Exception("Size lot %d is too small" % max_quantity)
        if self.state.price_number_to_lots(limit_price) < 0:
            raise Exception("Price lot %d is too small" % limit_price)
        return instructions.new_order(
            instructions.NewOrderParams(
                market=self.state.public_key(),
                open_orders=open_order_account,
                payer=payer,
                owner=owner.public_key(),
                request_queue=self.state.request_queue(),
                base_vault=self.state.base_vault(),
                quote_vault=self.state.quote_vault(),
                side=side,
                limit_price=limit_price,
                max_quantity=max_quantity,
                order_type=order_type,
                client_id=client_id,
                program_id=self.state.program_id(),
            )
        )

    def cancel_order_by_client_id(self, owner: str) -> str:
        raise NotImplementedError("cancel_order_by_client_id not implemented")

    def cancel_order(self, owner: Account, order: t.Order) -> str:
        txn = Transaction().add(self.make_cancel_order_instruction(owner.public_key(), order))
        return self._send_transaction(txn, owner)

    def match_orders(self, fee_payer: Account, limit: int) -> str:
        txn = Transaction().add(self.make_match_orders_instruction(limit))
        return self._send_transaction(txn, fee_payer)

    def make_cancel_order_instruction(self, owner: PublicKey, order: t.Order) -> TransactionInstruction:
        params = instructions.CancelOrderParams(
            market=self.state.public_key(),
            owner=owner,
            open_orders=order.open_order_address,
            request_queue=self.state.request_queue(),
            side=order.side,
            order_id=order.order_id,
            open_orders_slot=order.open_order_slot,
            program_id=self.state.program_id(),
        )
        return instructions.cancel_order(params)

    def make_match_orders_instruction(self, limit: int) -> TransactionInstruction:
        params = instructions.MatchOrdersParams(
            market=self.state.public_key(),
            request_queue=self.state.request_queue(),
            event_queue=self.state.event_queue(),
            bids=self.state.bids(),
            asks=self.state.asks(),
            base_vault=self.state.base_vault(),
            quote_vault=self.state.quote_vault(),
            limit=limit,
            program_id=self.state.program_id(),
        )
        return instructions.match_orders(params)

    def settle_funds(
        self, owner: Account, open_orders: OpenOrdersAccount, base_wallet: PublicKey, quote_wallet: PublicKey
    ) -> str:
        raise NotImplementedError("settle_funds not implemented")

    def _send_transaction(self, transaction: Transaction, *signers: Account) -> str:
        res = self._conn.send_transaction(transaction, *signers, skip_preflight=self._skip_preflight)
        if self._confirmations > 0:
            self.logger.warning("Cannot confirm transaction yet.")
        signature = res.get("result")
        if not signature:
            raise Exception("Transaction not sent successfully")
        return str(signature)