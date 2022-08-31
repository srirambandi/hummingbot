import aiohttp
import asyncio
import copy
import time
import json
import logging
import math
import base64
from decimal import Decimal
from typing import (
    Dict,
    List,
    Optional,
    Any,
    AsyncIterable,
)

from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.logger import HummingbotLogger
from hummingbot.core.clock import Clock
from hummingbot.core.utils import estimate_fee
from hummingbot.core.utils.async_utils import safe_ensure_future, safe_gather
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.data_type.cancellation_result import CancellationResult
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.limit_order import LimitOrder
from hummingbot.core.event.events import (
    MarketEvent,
    BuyOrderCompletedEvent,
    SellOrderCompletedEvent,
    OrderFilledEvent,
    OrderCancelledEvent,
    BuyOrderCreatedEvent,
    SellOrderCreatedEvent,
    MarketOrderFailureEvent,
    OrderType,
    TradeType,
    TradeFee
)
from hummingbot.connector.exchange_base import ExchangeBase
from hummingbot.connector.exchange.xt.xt_order_book_tracker import XtOrderBookTracker
from hummingbot.connector.exchange.xt.xt_user_stream_tracker import XtUserStreamTracker
from hummingbot.connector.exchange.xt.xt_auth import XtAuth
from hummingbot.connector.exchange.xt.xt_in_flight_order import XtInFlightOrder
from hummingbot.connector.exchange.xt import xt_utils
from hummingbot.connector.exchange.xt import xt_constants as CONSTANTS
from hummingbot.core.data_type.common import OpenOrder
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler

ctce_logger = None
s_decimal_NaN = Decimal("nan")
s_decimal_0 = Decimal(0)


class XtExchange(ExchangeBase):
    """
    XtExchange connects with XT exchange and provides order book pricing, user account tracking and
    trading functionality.
    """
    API_CALL_TIMEOUT = 10.0
    POLL_INTERVAL = 1.0
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 1.0
    UPDATE_TRADE_STATUS_MIN_INTERVAL = 1.0
    TRADE_LOOK_BACK_INTERVAL = 5 * 60 * 1000    # milliseconds

    @classmethod
    def logger(cls) -> HummingbotLogger:
        global ctce_logger
        if ctce_logger is None:
            ctce_logger = logging.getLogger(__name__)
        return ctce_logger

    def __init__(self,
                 xt_api_key: str,
                 xt_secret_key: str,
                 trading_pairs: Optional[List[str]] = None,
                 trading_required: bool = True
                 ):
        """
        :param xt_api_key: The API key to connect to private XT APIs.
        :param xt_secret_key: The API secret.
        :param trading_pairs: The market trading pairs which to track order book data.
        :param trading_required: Whether actual trading is needed.
        """
        super().__init__()
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._xt_auth = XtAuth(api_key=xt_api_key, secret_key=xt_secret_key)
        self._throttler = AsyncThrottler(CONSTANTS.RATE_LIMITS)
        self._order_book_tracker = XtOrderBookTracker(
            throttler=self._throttler, trading_pairs=trading_pairs
        )
        self._user_stream_tracker = XtUserStreamTracker(
            throttler=self._throttler, xt_auth=self._xt_auth, trading_pairs=trading_pairs
        )
        self._ev_loop = asyncio.get_event_loop()
        self._shared_client = None
        self._poll_notifier = asyncio.Event()
        self._last_timestamp = 0
        self._in_flight_orders = {}  # Dict[client_order_id:str, XtInFlightOrder]
        self._order_not_found_records = {}  # Dict[client_order_id:str, count:int]
        self._trading_rules = {}  # Dict[trading_pair:str, TradingRule]
        self._status_polling_task = None
        self._user_stream_event_listener_task = None
        self._trading_rules_polling_task = None
        self._last_poll_timestamp = 0
        self._real_time_balance_update = False

    @property
    def name(self) -> str:
        return "xt"

    @property
    def order_books(self) -> Dict[str, OrderBook]:
        return self._order_book_tracker.order_books

    @property
    def trading_rules(self) -> Dict[str, TradingRule]:
        return self._trading_rules

    @property
    def in_flight_orders(self) -> Dict[str, XtInFlightOrder]:
        return self._in_flight_orders

    @property
    def status_dict(self) -> Dict[str, bool]:
        """
        A dictionary of statuses of various connector's components.
        """
        return {
            "order_books_initialized": self._order_book_tracker.ready,
            "account_balance": len(self._account_balances) > 0 if self._trading_required else True,
            "trading_rule_initialized": len(self._trading_rules) > 0,
            "user_stream_initialized": True,
        }

    @property
    def ready(self) -> bool:
        """
        :return True when all statuses pass, this might take 5-10 seconds for all the connector's components and
        services to be ready.
        """
        return all(self.status_dict.values())

    @property
    def limit_orders(self) -> List[LimitOrder]:
        return [
            in_flight_order.to_limit_order()
            for in_flight_order in self._in_flight_orders.values()
        ]

    @property
    def tracking_states(self) -> Dict[str, any]:
        """
        :return active in-flight orders in json format, is used to save in sqlite db.
        """
        return {
            key: value.to_json()
            for key, value in self._in_flight_orders.items()
            if not value.is_done
        }

    def restore_tracking_states(self, saved_states: Dict[str, any]):
        """
        Restore in-flight orders from saved tracking states, this is st the connector can pick up on where it left off
        when it disconnects.
        :param saved_states: The saved tracking_states.
        """
        self._in_flight_orders.update({
            key: XtInFlightOrder.from_json(value)
            for key, value in saved_states.items()
        })

    def supported_order_types(self) -> List[OrderType]:
        """
        :return a list of OrderType supported by this connector.
        Note that Market order type is no longer required and will not be used.
        """
        return [OrderType.LIMIT, OrderType.LIMIT_MAKER]

    def start(self, clock: Clock, timestamp: float):
        """
        This function is called automatically by the clock.
        """
        super().start(clock, timestamp)

    def stop(self, clock: Clock):
        """
        This function is called automatically by the clock.
        """
        super().stop(clock)

    async def start_network(self):
        """
        This function is required by NetworkIterator base class and is called automatically.
        It starts tracking order book, polling trading rules,
        updating statuses and tracking user data.
        """
        self._order_book_tracker.start()
        self._trading_rules_polling_task = safe_ensure_future(self._trading_rules_polling_loop())
        if self._trading_required:
            self._status_polling_task = safe_ensure_future(self._status_polling_loop())
            self._user_stream_tracker_task = safe_ensure_future(self._user_stream_tracker.start())
            self._user_stream_event_listener_task = safe_ensure_future(self._user_stream_event_listener())

    async def stop_network(self):
        """
        This function is required by NetworkIterator base class and is called automatically.
        """
        self._order_book_tracker.stop()
        if self._status_polling_task is not None:
            self._status_polling_task.cancel()
            self._status_polling_task = None
        if self._trading_rules_polling_task is not None:
            self._trading_rules_polling_task.cancel()
            self._trading_rules_polling_task = None
        if self._status_polling_task is not None:
            self._status_polling_task.cancel()
            self._status_polling_task = None
        if self._user_stream_tracker_task is not None:
            self._user_stream_tracker_task.cancel()
            self._user_stream_tracker_task = None
        if self._user_stream_event_listener_task is not None:
            self._user_stream_event_listener_task.cancel()
            self._user_stream_event_listener_task = None

    async def check_network(self) -> NetworkStatus:
        """
        This function is required by NetworkIterator base class and is called periodically to check
        the network connection. Simply ping the network (or call any light weight public API).
        """
        try:
            await self._api_request("GET", CONSTANTS.CHECK_NETWORK_PATH_URL, is_auth=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            return NetworkStatus.NOT_CONNECTED
        return NetworkStatus.CONNECTED

    async def _http_client(self) -> aiohttp.ClientSession:
        """
        :returns Shared client session instance
        """
        if self._shared_client is None:
            self._shared_client = aiohttp.ClientSession()
        return self._shared_client

    async def _trading_rules_polling_loop(self):
        """
        Periodically update trading rule.
        """
        while True:
            try:
                await self._update_trading_rules()
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().network(f"Unexpected error while fetching trading rules. Error: {str(e)}",
                                      exc_info=True,
                                      app_warning_msg="Could not fetch new trading rules from XT. "
                                                      "Check network connection.")
                await asyncio.sleep(0.5)

    async def _update_trading_rules(self):
        market_configs = await self._api_request("GET", path_url=CONSTANTS.GET_TRADING_RULES_PATH_URL, is_auth=False)
        self._trading_rules.clear()
        self._trading_rules = self._format_trading_rules(market_configs)

    def _format_trading_rules(self, market_configs: Dict[str, Any]) -> Dict[str, TradingRule]:
        """
        Converts json API response into a dictionary of trading rules.
        :param market_configs: The json API response
        :return A dictionary of trading rules.
        Response Example:
        {
            "ltc_usdt":
            {
                "minAmount": 0.00010,       // minimum order quantity
                "minMoney": 5,       	    // minimum order money
                "pricePoint": 2,            // price decimal point
                "coinPoint": 4,             // number decimal point
                "maker": 0.00100000,        // Active transaction fee
                "taker": 0.00100000         // Passive transaction fee`
            },
            ...
        }
        """
        result = {}
        for market, rule in market_configs.items():
            try:
                trading_pair = xt_utils.convert_from_exchange_trading_pair(market)
                price_decimals = Decimal(str(rule["pricePoint"]))
                # E.g. a price decimal of 2 means 0.01 incremental.
                price_step = Decimal("1") / Decimal(str(math.pow(10, price_decimals)))
                base_decimals = Decimal(str(rule["coinPoint"]))
                base_step = Decimal("1") / Decimal(str(math.pow(10, base_decimals)))
                result[trading_pair] = TradingRule(trading_pair=trading_pair,
                                                   min_order_size=Decimal(str(rule["minAmount"])),
                                                   min_order_value=Decimal(str(rule["minMoney"])),
                                                   min_base_amount_increment=base_step,
                                                   min_price_increment=price_step)
            except Exception:
                self.logger().error(f"Error parsing the trading pair: {trading_pair} with rule: {rule}. Skipping.", exc_info=True)
        return result

    async def _api_request(self,
                           method: str,
                           path_url: str,
                           params: Optional[Dict[str, Any]] = None,
                           is_auth: bool = True) -> Dict[str, Any]:
        """
        Sends an aiohttp request and waits for a response.
        :param method: The HTTP method, e.g. get or post
        :param path_url: The path url or the API end point
        :param params: Request parameters
        :param is_auth: A bool that says if the request needs authorization
        :returns A response in json format.
        """
        params = params or {}
        async with self._throttler.execute_task(path_url):
            url = f"{CONSTANTS.REST_URL}/{path_url}"
            client = await self._http_client()

            if is_auth:
                params = self._xt_auth.get_auth_dict(xt_utils.get_ms_timestamp(), params)

            headers = {
                "Content-Type": 'application/x-www-form-urlencoded'
            }

            if method == "GET":
                response = await client.get(url, params=params, headers=headers)
            elif method == "POST":
                response = await client.post(url, data=params, headers=headers)
            else:
                raise NotImplementedError

            try:
                parsed_response = json.loads(await response.text())
            except Exception as e:
                raise IOError(f"Error parsing data from {url}. Error: {str(e)}")

            if response.status != 200:
                raise IOError(f"Error calling {url}. HTTP status is {response.status}. "
                              f"Message: {parsed_response}")
            if "code" in parsed_response and int(parsed_response["code"]) not in [200, 121, 122]:
                raise IOError(f"{url} API call failed, error message: {parsed_response}")

            return parsed_response

    def get_order_price_quantum(self, trading_pair: str, price: Decimal):
        """
        Returns a price step, a minimum price increment for a given trading pair.
        """
        trading_rule = self._trading_rules[trading_pair]
        return trading_rule.min_price_increment

    def get_order_size_quantum(self, trading_pair: str, order_size: Decimal):
        """
        Returns an order amount step, a minimum amount increment for a given trading pair.
        """
        trading_rule = self._trading_rules[trading_pair]
        return Decimal(trading_rule.min_base_amount_increment)

    def get_order_book(self, trading_pair: str) -> OrderBook:
        if trading_pair not in self._order_book_tracker.order_books:
            raise ValueError(f"No order book exists for '{trading_pair}'.")
        return self._order_book_tracker.order_books[trading_pair]

    def buy(self, trading_pair: str, amount: Decimal, order_type=OrderType.MARKET,
            price: Decimal = s_decimal_NaN, **kwargs) -> str:
        """
        Buys an amount of base asset (of the given trading pair). This function returns immediately.
        To see an actual order, you'll have to wait for BuyOrderCreatedEvent.
        :param trading_pair: The market (e.g. BTC-USDT) to buy from
        :param amount: The amount in base token value
        :param order_type: The order type
        :param price: The price (note: this is no longer optional)
        :returns A new internal order id
        """
        order_id: str = xt_utils.get_new_client_order_id(True, trading_pair)
        safe_ensure_future(self._create_order(TradeType.BUY, order_id, trading_pair, amount, order_type, price))
        return order_id

    def sell(self, trading_pair: str, amount: Decimal, order_type=OrderType.MARKET,
             price: Decimal = s_decimal_NaN, **kwargs) -> str:
        """
        Sells an amount of base asset (of the given trading pair). This function returns immediately.
        To see an actual order, you'll have to wait for SellOrderCreatedEvent.
        :param trading_pair: The market (e.g. BTC-USDT) to sell from
        :param amount: The amount in base token value
        :param order_type: The order type
        :param price: The price (note: this is no longer optional)
        :returns A new internal order id
        """
        order_id: str = xt_utils.get_new_client_order_id(False, trading_pair)
        safe_ensure_future(self._create_order(TradeType.SELL, order_id, trading_pair, amount, order_type, price))
        return order_id

    def cancel(self, trading_pair: str, order_id: str):
        """
        Cancel an order. This function returns immediately.
        To get the cancellation result, you'll have to wait for OrderCancelledEvent.
        :param trading_pair: The market (e.g. BTC-USDT) of the order.
        :param order_id: The internal order id (also called client_order_id)
        """
        safe_ensure_future(self._execute_cancel(trading_pair, order_id))
        return order_id

    async def _create_order(self,
                            trade_type: TradeType,
                            order_id: str,
                            trading_pair: str,
                            amount: Decimal,
                            order_type: OrderType,
                            price: Decimal):
        """
        Calls create-order API end point to place an order, starts tracking the order and triggers order created event.
        :param trade_type: BUY or SELL
        :param order_id: Internal order id (also called client_order_id)
        :param trading_pair: The market to place order
        :param amount: The order amount (in base token value)
        :param order_type: The order type
        :param price: The order price
        """
        if not order_type.is_limit_type():
            raise Exception(f"Unsupported order type: {order_type}")
        trading_rule = self._trading_rules[trading_pair]

        try:
            amount = self.quantize_order_amount(trading_pair, amount)
            price = self.quantize_order_price(trading_pair, price)
            if amount < trading_rule.min_order_size:
                raise ValueError(f"Buy order amount {amount} is lower than the minimum order size "
                                 f"{trading_rule.min_order_size}.")
            params = {
                "market": xt_utils.convert_to_exchange_trading_pair(trading_pair),
                "price": f"{price:f}",
                "number": f"{amount:f}",
                "type": 1 if trade_type is TradeType.BUY else 0,
                "entrustType": 0
            }
            self.start_tracking_order(order_id,
                                      None,
                                      trading_pair,
                                      trade_type,
                                      price,
                                      amount,
                                      order_type)

            order_result = await self._api_request("POST", CONSTANTS.CREATE_ORDER_PATH_URL, params)
            exchange_order_id = str(order_result["data"]["id"])
            tracked_order = self._in_flight_orders.get(order_id)
            if tracked_order is not None:
                self.logger().info(f"Created {order_type.name} {trade_type.name} order {order_id} for "
                                   f"{amount} {trading_pair}.")
                tracked_order.update_exchange_order_id(exchange_order_id)

            event_tag = MarketEvent.BuyOrderCreated if trade_type is TradeType.BUY else MarketEvent.SellOrderCreated
            event_class = BuyOrderCreatedEvent if trade_type is TradeType.BUY else SellOrderCreatedEvent
            self.trigger_event(event_tag,
                               event_class(
                                   self.current_timestamp,
                                   order_type,
                                   trading_pair,
                                   amount,
                                   price,
                                   order_id
                               ))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.stop_tracking_order(order_id)
            self.logger().network(
                f"Error submitting {trade_type.name} {order_type.name} order to XT for "
                f"{amount} {trading_pair} "
                f"{price}.",
                exc_info=True,
                app_warning_msg=str(e)
            )
            self.trigger_event(MarketEvent.OrderFailure,
                               MarketOrderFailureEvent(self.current_timestamp, order_id, order_type))

    def start_tracking_order(self,
                             order_id: str,
                             exchange_order_id: str,
                             trading_pair: str,
                             trade_type: TradeType,
                             price: Decimal,
                             amount: Decimal,
                             order_type: OrderType):
        """
        Starts tracking an order by simply adding it into _in_flight_orders dictionary.
        """
        self._in_flight_orders[order_id] = XtInFlightOrder(
            client_order_id=order_id,
            exchange_order_id=exchange_order_id,
            trading_pair=trading_pair,
            order_type=order_type,
            trade_type=trade_type,
            price=price,
            amount=amount
        )

    def stop_tracking_order(self, order_id: str):
        """
        Stops tracking an order by simply removing it from _in_flight_orders dictionary.
        """
        if order_id in self._in_flight_orders:
            del self._in_flight_orders[order_id]

    async def _execute_cancel(self, trading_pair: str, order_id: str) -> str:
        """
        Executes order cancellation process by first calling cancel-order API. The API result doesn't confirm whether
        the cancellation is successful, it simply states it receives the request.
        :param trading_pair: The market trading pair
        :param order_id: The internal order id
        order.last_state to change to CANCELED
        """
        try:
            tracked_order = self._in_flight_orders.get(order_id)
            if tracked_order is None:
                raise ValueError(f"Failed to cancel order - {order_id}. Order not found.")
            if tracked_order.exchange_order_id is None:
                await tracked_order.get_exchange_order_id()
            ex_order_id = tracked_order.exchange_order_id

            params = {
                "market":   xt_utils.convert_to_exchange_trading_pair(trading_pair),
                "id":       int(ex_order_id)
            }

            response = await self._api_request("POST", CONSTANTS.CANCEL_ORDER_PATH_URL, params)

            # code in {200, 121, 122} is a successful cancel, code in {123, 124} is a failed cancellation
            if int(response["code"]) in [200, 121, 122]:
                return order_id
            else:
                raise ValueError(f"Failed to cancel order - {order_id}. cancellation Message: {response}.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger().network(
                f"Failed to cancel order {order_id}: {str(e)}",
                exc_info=True,
                app_warning_msg=f"Failed to cancel the order {order_id} on Xt. "
                                f"Check API key and network connection."
            )

    async def _status_polling_loop(self):
        """
        Periodically update user balances and order status via REST API. This serves as a fallback measure for web
        socket API updates.
        """
        while True:
            try:
                await self._poll_notifier.wait()
                await safe_gather(
                    self._update_balances(),
                    self._update_trade_status(),
                    self._update_order_status(),
                )
                self._last_poll_timestamp = self.current_timestamp
                self._poll_notifier.clear()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().error(str(e), exc_info=True)
                self.logger().network("Unexpected error while fetching account updates.",
                                      exc_info=True,
                                      app_warning_msg="Could not fetch account updates from XT. "
                                                      "Check API key and network connection.")
                await asyncio.sleep(0.5)

    async def _update_balances(self):
        """
        Calls REST API to update total and available balances.
        """
        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()
        account_info = await self._api_request("GET", CONSTANTS.GET_ACCOUNT_SUMMARY_PATH_URL)
        for asset, account in account_info["data"].items():
            asset_name = asset.upper()
            self._account_available_balances[asset_name] = Decimal(str(account["available"])) - Decimal(str(account["freeze"]))
            self._account_balances[asset_name] = Decimal(str(account["available"]))
            remote_asset_names.add(asset_name)

        asset_names_to_remove = local_asset_names.difference(remote_asset_names)
        for asset_name in asset_names_to_remove:
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

        self._in_flight_orders_snapshot = {k: copy.copy(v) for k, v in self._in_flight_orders.items()}
        self._in_flight_orders_snapshot_timestamp = self.current_timestamp

    async def _update_order_status(self):
        """
        Calls REST API to get status update for each in-flight order.
        """
        last_tick = int(self._last_poll_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)
        current_tick = int(self.current_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)

        if current_tick > last_tick and len(self._in_flight_orders) > 0:

            if self._trading_pairs is None:
                raise Exception("_update_order_status can only be used when trading_pairs are specified.")
            for order in self._in_flight_orders.values():
                await order.get_exchange_order_id()
            tracked_orders: Dict[str, XtInFlightOrder] = self._in_flight_orders.copy()

            batch_size = 100
            orders = tracked_orders.values()
            tasks = []
            for trading_pair in self._trading_pairs:

                tp_orders = [order for order in orders if order.trading_pair == trading_pair]
                order_chunks = []

                for i in range(0, len(tp_orders), batch_size):
                    order_chunks.append(tp_orders[i:i+batch_size])

                for chunk in order_chunks:

                    data = [int(order.exchange_order_id) for order in chunk]
                    data = json.dumps(data)
                    data = base64.b64encode(data.encode('utf-8'))

                    params = {
                        "market": xt_utils.convert_to_exchange_trading_pair(trading_pair),
                        "data": str(data, 'utf-8')
                    }

                    tasks.append(self._api_request("GET", CONSTANTS.GET_ORDER_DETAIL_PATH_URL, params))

            self.logger().debug(f"Polling for order status updates.")
            responses = await safe_gather(*tasks, return_exceptions=True)
            for response in responses:
                if isinstance(response, Exception):
                    raise response
                if "data" not in response:
                    self.logger().info(f"_update_order_status data not in resp: {response}")
                    continue
                orders = response["data"]
                for order in orders:
                    await self._process_order_message(order)

    async def _update_trade_status(self):
        """
        Calls REST API to get trade updates in the last TRADE_LOOK_BACK_INTERVAL milliseconds.
        """
        last_tick = int(self._last_poll_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)
        current_tick = int(self.current_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)

        if current_tick > last_tick and len(self._in_flight_orders) > 0:

            if self._trading_pairs is None:
                raise Exception("_update_order_status can only be used when trading_pairs are specified.")

            tasks = []
            for trading_pair in self._trading_pairs:

                curr_time = int(time.time() * 1000)
                params = {
                    "market":       xt_utils.convert_to_exchange_trading_pair(trading_pair),
                    "startTime":    curr_time - self.TRADE_LOOK_BACK_INTERVAL,
                    "endTime":      curr_time
                }

                tasks.append(self._api_request("GET", CONSTANTS.GET_TRADE_DETAIL_PATH_URL, params))

            self.logger().debug(f"Polling for trade status updates.")
            responses = await safe_gather(*tasks, return_exceptions=True)
            for response in responses:
                if isinstance(response, Exception):
                    raise response
                if "data" not in response:
                    self.logger().info(f"_update_trade_status data not in resp: {response}")
                    continue
                trades = response["data"]
                for trade in trades:
                    await self._process_trade_message_from_trade_status(trade)


    async def _process_order_message(self, order_msg: Dict[str, Any]):
        """
        Updates in-flight order and triggers cancellation or failure event if needed. Sends message to process trade-fills if order completes.
        :param order_msg: The order response from either REST or web socket API (they are of the same format)
        """
        for order in self._in_flight_orders.values():
            await order.get_exchange_order_id()
        exchange_order_id = str(order_msg["id"])
        tracked_orders = list(self._in_flight_orders.values())
        tracked_order = [order for order in tracked_orders if exchange_order_id == order.exchange_order_id]
        if not tracked_order:
            return
        tracked_order = tracked_order[0]
        client_order_id = tracked_order.client_order_id

        # Update order execution status
        tracked_order.last_state = CONSTANTS.ORDER_STATUS[int(order_msg["status"])]

        if tracked_order.is_cancelled:
            self.logger().info(f"Successfully cancelled order {client_order_id}.")
            self.trigger_event(MarketEvent.OrderCancelled,
                               OrderCancelledEvent(
                                   self.current_timestamp,
                                   client_order_id))
            tracked_order.cancelled_event.set()
            self.stop_tracking_order(client_order_id)
        elif tracked_order.is_done:
            await self._process_trade_message_from_order_status(order_msg)
        elif tracked_order.is_failure:
            self.logger().info(f"The market order {client_order_id} has failed according to order status API. ")
            self.trigger_event(MarketEvent.OrderFailure,
                               MarketOrderFailureEvent(
                                   self.current_timestamp,
                                   client_order_id,
                                   tracked_order.order_type
                               ))
            self.stop_tracking_order(client_order_id)

    async def _process_trade_message_from_order_status(self, order_msg: Dict[str, Any]):
        """
        Updates in-flight order and trigger order filled event for order message received from Order Status REST API. Triggers order completed
        event if the total executed amount equals to the specified order amount.
        """
        for order in self._in_flight_orders.values():
            await order.get_exchange_order_id()
        tracked_orders = list(self._in_flight_orders.values())
        tracked_order = [o for o in tracked_orders if str(order_msg["id"]) == o.exchange_order_id]
        if not tracked_order:
            return
        tracked_order = tracked_order[0]
        (delta_trade_amount, delta_trade_price, delta_trade_fee, trade_id) = tracked_order.update_with_order_status(order_msg)
        if not delta_trade_amount:
            return
        self.trigger_event(
            MarketEvent.OrderFilled,
            OrderFilledEvent(
                self.current_timestamp,
                tracked_order.client_order_id,
                tracked_order.trading_pair,
                tracked_order.trade_type,
                tracked_order.order_type,
                delta_trade_price,
                delta_trade_amount,
                TradeFee(0.0, [(tracked_order.fee_asset, float(delta_trade_fee))]),
                exchange_trade_id=trade_id
            )
        )
        if math.isclose(tracked_order.executed_amount_base, tracked_order.amount) or tracked_order.executed_amount_base >= tracked_order.amount:
            tracked_order.last_state = "FILLED"
            self.logger().info(f"The {tracked_order.trade_type.name} order "
                               f"{tracked_order.client_order_id} has completed "
                               f"according to Order Status REST API.")
            event_tag = MarketEvent.BuyOrderCompleted if tracked_order.trade_type is TradeType.BUY \
                else MarketEvent.SellOrderCompleted
            event_class = BuyOrderCompletedEvent if tracked_order.trade_type is TradeType.BUY \
                else SellOrderCompletedEvent
            self.trigger_event(event_tag,
                               event_class(self.current_timestamp,
                                           tracked_order.client_order_id,
                                           tracked_order.base_asset,
                                           tracked_order.quote_asset,
                                           tracked_order.fee_asset,
                                           tracked_order.executed_amount_base,
                                           tracked_order.executed_amount_quote,
                                           tracked_order.fee_paid,
                                           tracked_order.order_type))
            self.stop_tracking_order(tracked_order.client_order_id)

    async def _process_trade_message_from_trade_status(self, trade_msg: Dict[str, Any]):
        """
        Updates in-flight order and trigger order filled event for trade message received from Trade Status REST API. Triggers order completed
        event if the total executed amount equals to the specified order amount.
        """
        for order in self._in_flight_orders.values():
            await order.get_exchange_order_id()
        tracked_orders = list(self._in_flight_orders.values())
        tracked_order = [o for o in tracked_orders if str(trade_msg["orderId"]) == o.exchange_order_id]
        if not tracked_order:
            return
        tracked_order = tracked_order[0]
        (delta_trade_amount, delta_trade_price, delta_trade_fee, trade_id) = tracked_order.update_with_trade_status(trade_msg)
        if not delta_trade_amount:
            return
        self.trigger_event(
            MarketEvent.OrderFilled,
            OrderFilledEvent(
                self.current_timestamp,
                tracked_order.client_order_id,
                tracked_order.trading_pair,
                tracked_order.trade_type,
                tracked_order.order_type,
                delta_trade_price,
                delta_trade_amount,
                TradeFee(0.0, [(tracked_order.fee_asset, float(delta_trade_fee))]),
                exchange_trade_id=trade_id
            )
        )
        if math.isclose(tracked_order.executed_amount_base, tracked_order.amount) or tracked_order.executed_amount_base >= tracked_order.amount:
            tracked_order.last_state = "FILLED"
            self.logger().info(f"The {tracked_order.trade_type.name} order "
                               f"{tracked_order.client_order_id} has completed "
                               f"according to Trade Status REST API.")
            event_tag = MarketEvent.BuyOrderCompleted if tracked_order.trade_type is TradeType.BUY \
                else MarketEvent.SellOrderCompleted
            event_class = BuyOrderCompletedEvent if tracked_order.trade_type is TradeType.BUY \
                else SellOrderCompletedEvent
            self.trigger_event(event_tag,
                               event_class(self.current_timestamp,
                                           tracked_order.client_order_id,
                                           tracked_order.base_asset,
                                           tracked_order.quote_asset,
                                           tracked_order.fee_asset,
                                           tracked_order.executed_amount_base,
                                           tracked_order.executed_amount_quote,
                                           tracked_order.fee_paid,
                                           tracked_order.order_type))
            self.stop_tracking_order(tracked_order.client_order_id)

    async def cancel_all(self, timeout_seconds: float):
        """
        Cancels all in-flight orders and waits for cancellation results.
        Used by bot's top level stop and exit commands (cancelling outstanding orders on exit)
        :param timeout_seconds: The timeout at which the operation will be canceled.
        :returns List of CancellationResult which indicates whether each order is successfully cancelled.
        """
        tracked_orders: Dict[str, XtInFlightOrder] = self._in_flight_orders.copy().items()
        cancellation_results = []

        try:
            orders = self._in_flight_orders.copy()
            for tracked_order in orders.values():
                response = await self._execute_cancel(tracked_order.trading_pair, tracked_order.client_order_id)
                await asyncio.sleep(0.2)

            await asyncio.sleep(5.0)

            open_orders = await self.get_open_orders()
            for cl_order_id, tracked_order in tracked_orders:
                open_order = [o for o in open_orders if o.client_order_id == cl_order_id]
                if not open_order:
                    cancellation_results.append(CancellationResult(cl_order_id, True))
                    self.trigger_event(MarketEvent.OrderCancelled,
                                       OrderCancelledEvent(self.current_timestamp, cl_order_id))
                    self.stop_tracking_order(cl_order_id)
                else:
                    cancellation_results.append(CancellationResult(cl_order_id, False))
        except Exception:
            self.logger().network(
                "Failed to cancel all orders.",
                exc_info=True,
                app_warning_msg="Failed to cancel all orders on XT. Check API key and network connection."
            )
        return cancellation_results

    def tick(self, timestamp: float):
        """
        Is called automatically by the clock for each clock's tick (1 second by default).
        It checks if status polling task is due for execution.
        """
        last_tick = int(self._last_timestamp / self.POLL_INTERVAL)
        current_tick = int(timestamp / self.POLL_INTERVAL)
        if current_tick > last_tick:
            if not self._poll_notifier.is_set():
                self._poll_notifier.set()
        self._last_timestamp = timestamp

    def get_fee(self,
                base_currency: str,
                quote_currency: str,
                order_type: OrderType,
                order_side: TradeType,
                amount: Decimal,
                price: Decimal = s_decimal_NaN) -> TradeFee:
        """
        To get trading fee, this function is simplified by using fee override configuration. Most parameters to this
        function are ignore except order_type. Use OrderType.LIMIT_MAKER to specify you want trading fee for
        maker order.
        """
        is_maker = order_type is OrderType.LIMIT_MAKER
        return TradeFee(percent=self.estimate_fee_pct(is_maker))

    async def _iter_user_event_queue(self) -> AsyncIterable[Dict[str, any]]:
        # while True:
        #     try:
        #         yield await self._user_stream_tracker.user_stream.get()
        #     except asyncio.CancelledError:
        #         raise
        #     except Exception:
        #         self.logger().network(
        #             "Unknown error. Retrying after 1 seconds.",
        #             exc_info=True,
        #             app_warning_msg="Could not fetch user events from Xt. Check API key and network connection."
        #         )
        #         await asyncio.sleep(1.0)
        pass

    async def _user_stream_event_listener(self):
        """
        Listens to message in _user_stream_tracker.user_stream queue. The messages are put in by
        XtAPIUserStreamDataSource.
        """
        # async for event_message in self._iter_user_event_queue():
        #     try:
        #         if "data" not in event_message:
        #             continue
        #         for msg in event_message["data"]:     # data is a list
        #             await self._process_order_message(msg)
        #             await self._process_trade_message_ws(msg)
        #     except asyncio.CancelledError:
        #         raise
        #     except Exception:
        #         self.logger().error("Unexpected error in user stream listener loop.", exc_info=True)
        #         await asyncio.sleep(5.0)
        pass

    async def get_open_orders(self) -> List[OpenOrder]:
        if self._trading_pairs is None:
            raise Exception("get_open_orders can only be used when trading_pairs are specified.")

        page_size = 1000
        responses = []
        for trading_pair in self._trading_pairs:
            page = 1
            while True:
                params = {
                    "market":   xt_utils.convert_to_exchange_trading_pair(trading_pair),
                    "page":     page,
                    "pageSize": page_size
                }
                response = await self._api_request("GET", CONSTANTS.GET_OPEN_ORDERS_PATH_URL, params)
                responses.append(response)
                count = len(response["data"])
                if count < page_size:
                    break
                else:
                    page += 1

        for order in self._in_flight_orders.values():
            await order.get_exchange_order_id()

        ret_val = []
        for response in responses:
            for order in response["data"]:
                exchange_order_id = str(order["id"])
                tracked_orders = list(self._in_flight_orders.values())
                tracked_order = [o for o in tracked_orders if exchange_order_id == o.exchange_order_id]
                if not tracked_order:
                    continue
                tracked_order = tracked_order[0]
                if int(order["entrustType"]) != 0:
                    raise Exception(f"Unsupported order type {order['entrustType']}. Only LIMIT orders are supported.")
                ret_val.append(
                    OpenOrder(
                        client_order_id=tracked_order.client_order_id,
                        trading_pair=tracked_order.trading_pair,
                        price=Decimal(str(order["price"])),
                        amount=Decimal(str(order["number"])),
                        executed_amount=Decimal(str(order["completeNumber"])),
                        status="ACTIVE",
                        order_type=OrderType.LIMIT,
                        is_buy=True if int(order["type"]) == 1 else False,
                        time=int(order["time"]),
                        exchange_order_id=str(order["id"])
                    )
                )
        return ret_val
