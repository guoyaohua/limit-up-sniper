"""
Level2 统一计算模块（L2 Unified Calculators）

本文件已合并多个 Level2 计算逻辑，目的是“单次解析/单次遍历数据流，多维度同时统计”，减少重复计算：

1) 全量资金流向（原“capital flow”）：大/超大单买卖金额统计
2) 涨停封板金额（原“seal amount”）：基于快照基线 + 增量追踪
3) 涨停期间（板上）资金流向：在涨停状态期间对“主动方”大/超大单做独立统计

对外推荐使用：
- [`Level2Calculator`](level2/calculators/l2_calculators.py:1)：统一处理 quote/order/transaction 的主计算器

兼容性：
- 保留 [`CapitalFlowCalculator`](level2/calculators/l2_calculators.py:1) 作为别名（避免旧代码大量改动）
"""

import logging
import time
from collections import defaultdict
from typing import Dict, Optional

from level2.enums import (
    EntrustDirection,
    Market,
    OrderSize,
    OrderThreshold,
    TradeFlag,
    get_limit_price,
    get_market,
    is_cancel_order,
    is_limit_up_price,
    classify_order_size,
)
from level2.models import (
    CapitalFlowStats,
    LimitUpPeriod,
    OrderInfo,
    SealAmountInfo,
    VirtualOrder,
)

logger = logging.getLogger(__name__)


class SealAmountCalculator:
    """
    涨停封板金额计算器（已合并到 [`level2/calculators/l2_calculators.py`](level2/calculators/l2_calculators.py:1)）

    目标：
    - "封板金额"与"资金流向/板上资金流向"共享同一条 Level2 数据流输入，避免重复计算。
    - 保持接口风格简单：上层只需要把 quote/order/transaction 依次喂给本对象即可。

    核心定义：
    - 封板金额 = 涨停价位（limit_price）未成交买单金额总和
    - 封板金额（元）= 当前封单量（股） * 涨停价（元）
    - 预警：封板金额 < 2000 万（[`level2.enums.OrderThreshold.SEAL_ALERT_AMOUNT`](level2/enums.py:40)）

    数据来源与算法（混合计算：快照基线 + 增量追踪）：
    1) l2quote 快照：用"买一档"作为基线封单量，每次快照都做一次校准（reset baseline）
    2) l2order 委托：累计"涨停价买单"的新增量（delta_buy）
    3) l2transaction 成交：累计"涨停价成交"和"撤单"的消耗量（delta_consume）

    最终：
    - 实时封单量 = baseline_volume + delta_buy - delta_consume
    - 实时封板金额 = 实时封单量 * limit_price
    """
    def __init__(self, stock_info: Optional[Dict[str, float]] = None):
        """
        初始化封板金额计算器。

        Args:
            stock_info: 股票信息字典，格式为 {stock_code: limit_price}
                        如 {'600000.SH': 11.0, '000001.SZ': 22.0}
        """
        # 封板信息：stock_code -> SealAmountInfo
        self.seal_info: Dict[str, SealAmountInfo] = {}

        # 统计计数（用于监控/调试，不影响计算）
        self.total_limit_up_stocks = 0
        self.total_weak_seal_alerts = 0

        # 初始化时批量设置股票信息
        if stock_info:
            for stock_code, limit_price in stock_info.items():
                self.seal_info[stock_code] = SealAmountInfo(
                    stock_code=stock_code, limit_price=limit_price)

    def set_stock_info(self, stock_code: str, limit_price: float):
        """
        设置单个股票的涨停价信息。

        Args:
            stock_code: 股票代码（如 '600000.SH'）
            limit_price: 涨停价
        """
        if stock_code not in self.seal_info:
            self.seal_info[stock_code] = SealAmountInfo(
                stock_code=stock_code, limit_price=limit_price)
        else:
            self.seal_info[stock_code].limit_price = limit_price

    def _get_seal_info(self, stock_code: str) -> Optional[SealAmountInfo]:
        """
        获取股票的封板信息。

        Args:
            stock_code: 股票代码
            
        Returns:
            SealAmountInfo 对象，如果股票未初始化则返回 None
        """
        return self.seal_info.get(stock_code)

    # ======== 事件处理：quote / order / transaction ========

    def on_l2quote(self, stock_code: str, quote_data: dict):
        """
        处理行情快照（l2quote）：
        - 判断是否涨停
        - 若涨停：用"买一档"校准封单基线（baseline）
        - 若未涨停：仅更新状态（不输出封单）

        Args:
            stock_code: 股票代码
            quote_data: l2quote 数据字典
        """
        info = self._get_seal_info(stock_code)
        if info is None:
            # 股票未初始化，跳过处理
            logger.warning(f"SealAmountCalculator: 股票 {stock_code} 未初始化，跳过封板计算")
            return

        last_price = float(quote_data.get("lastPrice", 0) or 0)
        timestamp = int(quote_data.get("time", 0) or 0)

        prev_is_limit_up = info.is_limit_up
        is_limit = is_limit_up_price(last_price, info.limit_price)

        info.is_limit_up = is_limit
        info.last_quote_time = timestamp

        if is_limit:
            # 涨停状态：尝试取买一（bid1）作为封单基线
            bid_prices = quote_data.get("bidPrice", [])
            bid_vols = quote_data.get("bidVol", [])

            if isinstance(bid_prices, (list, tuple)) and isinstance(
                    bid_vols, (list, tuple)) and bid_prices and bid_vols:
                bid_price = float(bid_prices[0] or 0)
                bid_vol = int(bid_vols[0] or 0)

                # 确认买一价格确为涨停价，才进行校准
                if is_limit_up_price(bid_price, info.limit_price):
                    info.reset_baseline(bid_vol, timestamp)

                    # 弱封板预警（每次校准都会检查一次）
                    if info.is_weak_seal:
                        self.total_weak_seal_alerts += 1
                        logger.critical(
                            f"【封单预警】{stock_code} 封单金额 {info.seal_amount_wan:.2f}万元 < 2000万"
                        )
        else:
            # 非涨停：如从涨停转为非涨停，视为“炸板”
            if prev_is_limit_up:
                logger.info(f"{stock_code} 炸板，封板金额归零（非涨停状态不再输出封单）")

            # 不在涨停状态时，为避免“旧增量”误导后续分析，做一次增量清零
            # （基线不强制清零：后续回封时会由快照重新校准）
            info.delta_buy = 0
            info.delta_consume = 0

    def on_l2order(self, stock_code: str, order_data: dict):
        """
        处理逐笔委托（l2order）：
        - 仅在"涨停状态"下追踪涨停价买单增量
        - 上交所撤买（entrustDirection=3）也会在此处体现为"消耗"（减少封单）
        - 跳过时间戳小于 last_quote_time 的数据（避免重复计算快照前的增量）

        Args:
            stock_code: 股票代码
            order_data: l2order 数据字典
        """
        info = self.seal_info.get(stock_code)
        if info is None or (not info.is_limit_up):
            return

        # 跳过时间戳小于 last_quote_time 的数据（快照校准前的数据不计入增量）
        timestamp = int(order_data.get("time", 0) or 0)
        if info.last_quote_time > 0 and timestamp < info.last_quote_time:
            return

        price = float(order_data.get("price", 0) or 0)
        direction = int(order_data.get("entrustDirection", 0) or 0)
        volume = int(order_data.get("volume", 0) or 0)

        # 涨停价买单：新增封单
        if direction == EntrustDirection.BUY and is_limit_up_price(
                price, info.limit_price):
            info.delta_buy += volume

        # 上交所撤买单：减少封单（深市撤单走 l2transaction）
        if get_market(
                stock_code
        ) == Market.SHANGHAI and direction == EntrustDirection.CANCEL_BUY:
            info.delta_consume += volume

    def on_l2transaction(self, stock_code: str, trans_data: dict):
        """
        处理逐笔成交（l2transaction）：
        - 深市撤单：tradeFlag=3（通过 [`level2.enums.is_cancel_order()`](level2/enums.py:84) 判断）
        - 涨停价成交：消耗封单（减少封单）
        - 跳过时间戳小于 last_quote_time 的数据（避免重复计算快照前的增量）

        Args:
            stock_code: 股票代码
            trans_data: l2transaction 数据字典
        """
        info = self.seal_info.get(stock_code)
        if info is None or (not info.is_limit_up):
            return

        # 跳过时间戳小于 last_quote_time 的数据（快照校准前的数据不计入增量）
        timestamp = int(trans_data.get("time", 0) or 0)
        if info.last_quote_time > 0 and timestamp < info.last_quote_time:
            return

        price = float(trans_data.get("price", 0) or 0)
        volume = int(trans_data.get("volume", 0) or 0)

        # 深交所撤单：直接视作封单消耗
        if is_cancel_order(stock_code, trans_data=trans_data):
            info.delta_consume += volume
            return

        # 涨停价成交：消耗封单
        if is_limit_up_price(price, info.limit_price):
            info.delta_consume += volume

    # ======== 查询接口 ========

    def get_seal_amount(self, stock_code: str) -> float:
        """
        获取实时封板金额（元）。

        Returns:
            若当前不处于涨停状态，返回 0.0
        """
        info = self.seal_info.get(stock_code)
        if info and info.is_limit_up:
            return info.seal_amount
        return 0.0

    def get_seal_info(self, stock_code: str) -> Optional[SealAmountInfo]:
        """获取封板详细信息（可能为 None）"""
        return self.seal_info.get(stock_code)

    def get_all_limit_up_stocks(self) -> Dict[str, SealAmountInfo]:
        """获取所有涨停股票的封板信息"""
        return {
            code: info
            for code, info in self.seal_info.items() if info.is_limit_up
        }

    def get_weak_seal_stocks(self) -> Dict[str, SealAmountInfo]:
        """获取弱封板股票（封板金额 < 2000 万）"""
        return {
            code: info
            for code, info in self.seal_info.items()
            if info.is_limit_up and info.is_weak_seal
        }

    def get_strong_seal_stocks(
            self,
            min_amount: float = 100_000_000) -> Dict[str, SealAmountInfo]:
        """
        获取强封板股票（封板金额 >= min_amount）。

        Args:
            min_amount: 最小封板金额（元），默认 1 亿
        """
        return {
            code: info
            for code, info in self.seal_info.items()
            if info.is_limit_up and info.seal_amount >= min_amount
        }

    def get_sorted_by_seal_amount(self, descending: bool = True) -> list:
        """
        按封板金额（万元）排序，返回 (stock_code, seal_amount_wan) 列表。
        """
        limit_up_stocks = [(code, info.seal_amount_wan)
                           for code, info in self.seal_info.items()
                           if info.is_limit_up]
        limit_up_stocks.sort(key=lambda x: x[1], reverse=descending)
        return limit_up_stocks


class Level2Calculator:
    """
    Level2 统一计算器（推荐使用）

    说明：
    - 历史上这里叫 "CapitalFlowCalculator"，但当前它已经能够通用处理所有 Level2 数据：
      - l2quote：盘口缓存 + 涨停状态/封板金额校准 + 涨停时段追踪
      - l2order：委托簿维护 + 封板金额增量
      - l2transaction：资金流向统计 + 封板金额消耗 + 板上资金流向

    因此更名为 `Level2Calculator` 更贴合职责；同时保留旧名作为兼容别名。
    """
    def __init__(
        self,
        stock_info: Optional[Dict[str, float]] = None,
        enable_limit_up_flow: bool = True,
    ):
        """
        初始化 Level2 统一计算器。

        Args:
            stock_info: 股票信息字典，格式为 {stock_code: limit_price}
                        如 {'600000.SH': 11.0, '000001.SZ': 22.0}
                        用于封板金额计算和板上资金流向追踪
            enable_limit_up_flow: 是否启用板上资金流向统计，默认 True
        """
        self.enable_limit_up_flow = enable_limit_up_flow

        # 自动创建封板计算器（用于判断涨停状态 + 维护封板金额增量）
        self.seal_calc = SealAmountCalculator(stock_info=stock_info)

        # 委托簿：stock_code -> {entrust_no: OrderInfo}
        self.order_book: Dict[str, Dict[int, OrderInfo]] = defaultdict(dict)

        # 上交所虚拟委托追踪：stock_code -> {entrust_no: VirtualOrder}
        self.virtual_orders: Dict[str, Dict[int,
                                            VirtualOrder]] = defaultdict(dict)

        # 资金流向统计：stock_code -> CapitalFlowStats
        self.flow_stats: Dict[str, CapitalFlowStats] = {}

        # ====== 板上(涨停期间)资金流向：复用订单/成交判定逻辑，不重复计算 ======
        # 仅在 self.enable_limit_up_flow=True 时累计；并且只累计“主动方”大单/超大单
        self.limit_up_flow: Dict[str, CapitalFlowStats] = {}

        # 涨停时段记录：stock_code -> List[LimitUpPeriod]
        self.limit_up_periods: Dict[str, list] = {}

        # 当前涨停状态：stock_code -> bool
        self._prev_limit_up_status: Dict[str, bool] = {}

        # 最新盘口（用于判断“是否仍可能在簿上”，比 last_price 更严谨）
        # 说明：涨停/跌停时一侧可能全为0（无挂单），best_bid/best_ask 会保持为 0.0
        self.best_bid: Dict[str, float] = {}
        self.best_ask: Dict[str, float] = {}

        # 最新快照时间（用于 freshness guard + 仅盘中清理）
        self.last_quote_time: Dict[str, int] = {}

        # 统计计数
        self.total_orders_processed = 0
        self.total_transactions_processed = 0
        self.total_cancels_processed = 0

    def _get_or_create_stats(self, stock_code: str) -> CapitalFlowStats:
        """获取或创建资金流向统计对象"""
        if stock_code not in self.flow_stats:
            self.flow_stats[stock_code] = CapitalFlowStats(
                stock_code=stock_code)
        return self.flow_stats[stock_code]

    @staticmethod
    def _is_continuous_trading() -> bool:
        """
        仅允许在盘中（连续竞价）做清理，跳过盘前/集合竞价/盘后。

        这里不再依赖数据流里的 time 字段，直接用本机当前时间判断（更简单、也更符合“只在盘中做清理”的目标）。
        连续竞价大致区间（留出一些空余）：
        - 09:35:00 ~ 11:25:00
        - 13:05:00 ~ 14:55:00
        """
        t = time.localtime()
        hhmmss = t.tm_hour * 10000 + t.tm_min * 100 + t.tm_sec
        return (93500 <= hhmmss <= 112500) or (130500 <= hhmmss <= 145500)

    def on_l2quote(self, stock_code: str, quote_data: dict):
        """
        处理行情快照（用于缓存 best_bid / best_ask，辅助委托清理）
        + 可选：复用 SealAmountCalculator 维护涨停状态，并追踪涨停时段。

        说明：涨停/跌停时一侧可能全为0（无挂单），此时 best_bid/best_ask 会记录为 0.0。

        Args:
            stock_code: 股票代码
            quote_data: l2quote数据字典
        """
        # 0) 先让封板计算器处理（避免上层重复调用）
        if self.seal_calc is not None:
            self.seal_calc.on_l2quote(stock_code, quote_data)

        # 1) 盘口缓存（用于订单清理）
        bid_prices = quote_data.get('bidPrice')
        best_bid = 0.0
        if isinstance(bid_prices, (list, tuple)) and bid_prices:
            # bidPrice[0] 即买一；涨停/跌停可能为 0
            best_bid = bid_prices[0]
        self.best_bid[stock_code] = best_bid

        ask_prices = quote_data.get('askPrice')
        best_ask = 0.0
        if isinstance(ask_prices, (list, tuple)) and ask_prices:
            # askPrice[0] 即卖一；涨停/跌停可能为 0
            best_ask = ask_prices[0]
        self.best_ask[stock_code] = best_ask

        # freshness guard + 仅盘中清理：记录最新 quote time（约定一定是 int）
        qt = quote_data.get('time', 0)
        if qt > 0:
            self.last_quote_time[stock_code] = qt

        # 2) 追踪涨停时段（仅在启用板上资金流向时）
        if self.enable_limit_up_flow and self.seal_calc is not None:
            seal_info = self.seal_calc.get_seal_info(stock_code)
            if seal_info:
                timestamp = int(quote_data.get('time', 0))
                self._track_limit_up_status(stock_code, seal_info.is_limit_up,
                                            timestamp)

    def on_l2order(self, stock_code: str, order_data: dict):
        """
        处理逐笔委托
        
        Args:
            stock_code: 股票代码
            order_data: l2order数据字典
        """
        self.total_orders_processed += 1

        # 1. 判断是否撤单（上交所）
        if is_cancel_order(stock_code, order_data=order_data):
            entrust_no = int(order_data['entrustNo'])
            self.order_book[stock_code].pop(entrust_no, None)
            self.total_cancels_processed += 1

            # 同步给封板计算器（撤买会影响封单增量）
            if self.seal_calc is not None:
                self.seal_calc.on_l2order(stock_code, order_data)
            return

        # 2. 记录委托信息
        order_info = OrderInfo(
            entrust_no=int(order_data['entrustNo']),
            stock_code=stock_code,
            direction=int(order_data['entrustDirection']),
            total_volume=int(order_data['volume']),
            price=float(order_data.get('price', 0)),
            timestamp=int(order_data.get('time', 0)),
        )

        self.order_book[stock_code][order_info.entrust_no] = order_info

        # 3) 同步给封板计算器（只在涨停时会记录增量）
        if self.seal_calc is not None:
            self.seal_calc.on_l2order(stock_code, order_data)

    def on_l2transaction(self, stock_code: str, trans_data: dict):
        """
        处理逐笔成交
        
        Args:
            stock_code: 股票代码
            trans_data: l2transaction数据字典
        """
        self.total_transactions_processed += 1

        # 1. 深交所撤单判断
        if is_cancel_order(stock_code, trans_data=trans_data):
            # 深交所撤单在成交数据中标识
            buy_no = int(trans_data.get('buyNo', 0))
            sell_no = int(trans_data.get('sellNo', 0))
            self.order_book[stock_code].pop(buy_no, None)
            self.order_book[stock_code].pop(sell_no, None)
            self.total_cancels_processed += 1

            # 同步给封板计算器（撤单会影响封单消耗增量）
            if self.seal_calc is not None:
                self.seal_calc.on_l2transaction(stock_code, trans_data)
            return

        # 2. 判断主动买卖方向
        trade_flag = int(trans_data.get('tradeFlag', 0))
        is_buy = (trade_flag == TradeFlag.BUY)  # 1=外盘（主动买入）

        # 3. 获取成交数据
        volume = int(trans_data['volume'])
        amount = float(trans_data['amount'])
        timestamp = int(trans_data.get('time', 0))

        # 4. 查找买卖双方委托
        buy_no = int(trans_data['buyNo'])
        sell_no = int(trans_data['sellNo'])
        buy_order = self.order_book[stock_code].get(buy_no)
        sell_order = self.order_book[stock_code].get(sell_no)

        # 5. 上交所特殊处理：虚拟委托追踪和订单累计成交更新
        market = get_market(stock_code)

        # 买方订单大小判定
        buy_previous_size = None  # 记录调整前的订单大小
        buy_previous_amount = 0.0  # 记录调整前的累计金额

        if market == Market.SHANGHAI and buy_order is None:
            # 上交所可能省略已全成交的委托，使用虚拟委托追踪
            if buy_no not in self.virtual_orders[stock_code]:
                self.virtual_orders[stock_code][buy_no] = VirtualOrder(buy_no)

            vo = self.virtual_orders[stock_code][buy_no]

            buy_previous_size = vo.last_order_size
            buy_previous_amount = vo.total_amount

            vo.total_volume += volume
            vo.total_amount += amount
            vo.last_update_time = timestamp
            vo.transaction_count += 1

            buy_order_size = classify_order_size(vo.total_volume,
                                                 vo.total_amount)

            vo.last_order_size = buy_order_size
        elif buy_order:
            # 沪市主动买入：同一订单号可能对应多次成交，需累计成交量/金额后再判定
            if market == Market.SHANGHAI and is_buy:
                buy_previous_size = buy_order.last_order_size  # 保存上次分类
                buy_previous_amount = buy_order.filled_amount  # 保存之前累计的金额
                buy_order.filled_volume += volume
                buy_order.filled_amount += amount
                # 使用 total_volume(原始委托) + filled 计算订单大小
                total_vol = buy_order.total_volume + buy_order.filled_volume
                total_amt = buy_order.total_volume * buy_order.price + buy_order.filled_amount
                buy_order_size = classify_order_size(total_vol, total_amt)
                buy_order.last_order_size = buy_order_size  # 更新最新分类
            else:
                # 深市或沪市被动方：使用原始委托量判定
                buy_order_size = buy_order.order_size
        else:
            buy_order_size = classify_order_size(volume, amount)

        # 卖方订单大小判定
        sell_previous_size = None  # 记录调整前的订单大小
        sell_previous_amount = 0.0  # 记录调整前的累计金额

        if market == Market.SHANGHAI and sell_order is None:
            if sell_no not in self.virtual_orders[stock_code]:
                self.virtual_orders[stock_code][sell_no] = VirtualOrder(
                    sell_no)

            vo = self.virtual_orders[stock_code][sell_no]

            sell_previous_size = vo.last_order_size
            sell_previous_amount = vo.total_amount

            vo.total_volume += volume
            vo.total_amount += amount
            vo.last_update_time = timestamp
            vo.transaction_count += 1

            sell_order_size = classify_order_size(vo.total_volume,
                                                  vo.total_amount)

            vo.last_order_size = sell_order_size
        elif sell_order:
            # 沪市主动卖出：同一订单号可能对应多次成交，需累计成交量/金额后再判定
            if market == Market.SHANGHAI and not is_buy:
                sell_previous_size = sell_order.last_order_size  # 保存上次分类
                sell_previous_amount = sell_order.filled_amount  # 保存之前累计的金额
                sell_order.filled_volume += volume
                sell_order.filled_amount += amount
                # 使用 total_volume(原始委托) + filled 计算订单大小
                total_vol = sell_order.total_volume + sell_order.filled_volume
                total_amt = sell_order.total_volume * sell_order.price + sell_order.filled_amount
                sell_order_size = classify_order_size(total_vol, total_amt)
                sell_order.last_order_size = sell_order_size  # 更新最新分类
            else:
                # 深市或沪市被动方：使用原始委托量判定
                sell_order_size = sell_order.order_size
        else:
            sell_order_size = classify_order_size(volume, amount)

        # 6. 累计资金流向 - 同时考虑买卖双方的订单大小
        stats = self._get_or_create_stats(stock_code)
        stats.last_update_time = timestamp

        # 根据双方订单大小累计资金流向
        # 买方是大单时计入大单买入，卖方是大单时计入大单卖出

        # 处理买方：沪市需要调整之前的分类
        # 虚拟订单(buy_order is None)始终是主动方，真实订单仅主动买入(is_buy)需要调整
        should_adjust_buy = (market == Market.SHANGHAI
                             and buy_previous_size is not None
                             and buy_previous_size != buy_order_size
                             and (buy_order is None or is_buy))

        if should_adjust_buy:
            # 订单大小分类发生变化，需要调整之前累计的金额
            # 从旧分类中减去之前累计的金额
            if buy_previous_size == OrderSize.SUPER_LARGE:
                stats.super_large_buy -= buy_previous_amount
                stats.super_large_buy_count -= 1
            elif buy_previous_size == OrderSize.LARGE:
                stats.large_buy -= buy_previous_amount
                stats.large_buy_count -= 1
            elif buy_previous_size == OrderSize.MEDIUM:
                stats.medium_buy -= buy_previous_amount
            else:  # SMALL
                stats.small_buy -= buy_previous_amount

            # 加到新分类中（包含之前累计的+本次的）
            if buy_order:
                new_total_amount = buy_order.filled_amount
            else:
                # 虚拟委托
                new_total_amount = self.virtual_orders[stock_code][
                    buy_no].total_amount

            if buy_order_size == OrderSize.SUPER_LARGE:
                stats.super_large_buy += new_total_amount
                stats.super_large_buy_count += 1
            elif buy_order_size == OrderSize.LARGE:
                stats.large_buy += new_total_amount
                stats.large_buy_count += 1
            elif buy_order_size == OrderSize.MEDIUM:
                stats.medium_buy += new_total_amount
            else:  # SMALL
                stats.small_buy += new_total_amount
        else:
            # 正常累计（深市或沪市被动方或首次成交）
            if buy_order_size == OrderSize.SUPER_LARGE:
                stats.super_large_buy += amount
                stats.super_large_buy_count += 1
            elif buy_order_size == OrderSize.LARGE:
                stats.large_buy += amount
                stats.large_buy_count += 1
            elif buy_order_size == OrderSize.MEDIUM:
                stats.medium_buy += amount
            else:  # SMALL
                stats.small_buy += amount

        # 处理卖方：沪市需要调整之前的分类
        # 虚拟订单(sell_order is None)始终是主动方，真实订单仅主动卖出(not is_buy)需要调整
        should_adjust_sell = (market == Market.SHANGHAI
                              and sell_previous_size is not None
                              and sell_previous_size != sell_order_size
                              and (sell_order is None or not is_buy))

        if should_adjust_sell:
            # 订单大小分类发生变化，需要调整之前累计的金额
            # 从旧分类中减去之前累计的金额
            if sell_previous_size == OrderSize.SUPER_LARGE:
                stats.super_large_sell -= sell_previous_amount
                stats.super_large_sell_count -= 1
            elif sell_previous_size == OrderSize.LARGE:
                stats.large_sell -= sell_previous_amount
                stats.large_sell_count -= 1
            elif sell_previous_size == OrderSize.MEDIUM:
                stats.medium_sell -= sell_previous_amount
            else:  # SMALL
                stats.small_sell -= sell_previous_amount

            # 加到新分类中（包含之前累计的+本次的）
            if sell_order:
                new_total_amount = sell_order.filled_amount
            else:
                # 虚拟委托
                new_total_amount = self.virtual_orders[stock_code][
                    sell_no].total_amount

            if sell_order_size == OrderSize.SUPER_LARGE:
                stats.super_large_sell += new_total_amount
                stats.super_large_sell_count += 1
            elif sell_order_size == OrderSize.LARGE:
                stats.large_sell += new_total_amount
                stats.large_sell_count += 1
            elif sell_order_size == OrderSize.MEDIUM:
                stats.medium_sell += new_total_amount
            else:  # SMALL
                stats.small_sell += new_total_amount
        else:
            # 正常累计（深市或沪市被动方或首次成交）
            if sell_order_size == OrderSize.SUPER_LARGE:
                stats.super_large_sell += amount
                stats.super_large_sell_count += 1
            elif sell_order_size == OrderSize.LARGE:
                stats.large_sell += amount
                stats.large_sell_count += 1
            elif sell_order_size == OrderSize.MEDIUM:
                stats.medium_sell += amount
            else:  # SMALL
                stats.small_sell += amount

        # 7) 同步给封板计算器（涨停时用于扣减封单消耗）
        if self.seal_calc is not None:
            self.seal_calc.on_l2transaction(stock_code, trans_data)

        # 8) 板上资金流向（仅涨停状态期间；仅统计“主动方”大单/超大单）
        if self.enable_limit_up_flow and self.seal_calc is not None:
            seal_info = self.seal_calc.get_seal_info(stock_code)
            if seal_info and seal_info.is_limit_up:
                self._accumulate_limit_up_flow(
                    stock_code=stock_code,
                    market=market,
                    is_buy=is_buy,
                    amount=amount,
                    timestamp=timestamp,
                    buy_no=buy_no,
                    sell_no=sell_no,
                    buy_order=buy_order,
                    sell_order=sell_order,
                    buy_previous_size=buy_previous_size,
                    buy_previous_amount=buy_previous_amount,
                    buy_order_size=buy_order_size,
                    sell_previous_size=sell_previous_size,
                    sell_previous_amount=sell_previous_amount,
                    sell_order_size=sell_order_size,
                )

    def _get_or_create_limit_up_stats(self,
                                      stock_code: str) -> CapitalFlowStats:
        """获取或创建板上资金流向统计对象"""
        if stock_code not in self.limit_up_flow:
            self.limit_up_flow[stock_code] = CapitalFlowStats(
                stock_code=stock_code)
        return self.limit_up_flow[stock_code]

    def _track_limit_up_status(self, stock_code: str, is_limit_up: bool,
                               timestamp: int):
        """追踪涨停状态变化，用于记录涨停时段。"""
        prev_status = self._prev_limit_up_status.get(stock_code, False)

        if stock_code not in self.limit_up_periods:
            self.limit_up_periods[stock_code] = []

        if is_limit_up and not prev_status:
            # 首次涨停或回封
            period = LimitUpPeriod(stock_code=stock_code,
                                   start_time=timestamp,
                                   is_active=True)
            self.limit_up_periods[stock_code].append(period)
        elif (not is_limit_up) and prev_status:
            # 炸板
            periods = self.limit_up_periods[stock_code]
            if periods and periods[-1].is_active:
                periods[-1].end_time = timestamp
                periods[-1].is_active = False

        self._prev_limit_up_status[stock_code] = is_limit_up

    def _accumulate_limit_up_flow(
        self,
        stock_code: str,
        market: Market,
        is_buy: bool,
        amount: float,
        timestamp: int,
        buy_no: int,
        sell_no: int,
        buy_order: Optional[OrderInfo],
        sell_order: Optional[OrderInfo],
        buy_previous_size: Optional[OrderSize],
        buy_previous_amount: float,
        buy_order_size: OrderSize,
        sell_previous_size: Optional[OrderSize],
        sell_previous_amount: float,
        sell_order_size: OrderSize,
    ):
        """
        累计板上资金流向（只统计涨停状态期间的“主动方”大单/超大单）。

        说明：这里复用 `on_l2transaction()` 已经计算出的订单大小、沪市调整信息，
        避免重复查询与重复分类计算。
        """
        stats = self._get_or_create_limit_up_stats(stock_code)
        stats.last_update_time = timestamp

        if is_buy:
            # 主动买入：使用买方订单大小
            active_size = buy_order_size
            prev_size = buy_previous_size
            prev_amount = buy_previous_amount

            should_adjust = (market == Market.SHANGHAI
                             and prev_size is not None
                             and prev_size != active_size
                             and (buy_order is None or is_buy))

            if should_adjust:
                # 先回滚旧分类（仅回滚我们曾经统计过的桶）
                if prev_size == OrderSize.SUPER_LARGE:
                    stats.super_large_buy -= prev_amount
                    stats.super_large_buy_count -= 1
                elif prev_size == OrderSize.LARGE:
                    stats.large_buy -= prev_amount
                    stats.large_buy_count -= 1

                # 再加到新分类（包含之前累计的+本次的）
                if buy_order is not None:
                    new_total_amount = buy_order.filled_amount
                else:
                    new_total_amount = self.virtual_orders[stock_code][
                        buy_no].total_amount

                if active_size == OrderSize.SUPER_LARGE:
                    stats.super_large_buy += new_total_amount
                    stats.super_large_buy_count += 1
                elif active_size == OrderSize.LARGE:
                    stats.large_buy += new_total_amount
                    stats.large_buy_count += 1
            else:
                if active_size == OrderSize.SUPER_LARGE:
                    stats.super_large_buy += amount
                    stats.super_large_buy_count += 1
                elif active_size == OrderSize.LARGE:
                    stats.large_buy += amount
                    stats.large_buy_count += 1

        else:
            # 主动卖出：使用卖方订单大小
            active_size = sell_order_size
            prev_size = sell_previous_size
            prev_amount = sell_previous_amount

            should_adjust = (market == Market.SHANGHAI
                             and prev_size is not None
                             and prev_size != active_size
                             and (sell_order is None or (not is_buy)))

            if should_adjust:
                if prev_size == OrderSize.SUPER_LARGE:
                    stats.super_large_sell -= prev_amount
                    stats.super_large_sell_count -= 1
                elif prev_size == OrderSize.LARGE:
                    stats.large_sell -= prev_amount
                    stats.large_sell_count -= 1

                if sell_order is not None:
                    new_total_amount = sell_order.filled_amount
                else:
                    new_total_amount = self.virtual_orders[stock_code][
                        sell_no].total_amount

                if active_size == OrderSize.SUPER_LARGE:
                    stats.super_large_sell += new_total_amount
                    stats.super_large_sell_count += 1
                elif active_size == OrderSize.LARGE:
                    stats.large_sell += new_total_amount
                    stats.large_sell_count += 1
            else:
                if active_size == OrderSize.SUPER_LARGE:
                    stats.super_large_sell += amount
                    stats.super_large_sell_count += 1
                elif active_size == OrderSize.LARGE:
                    stats.large_sell += amount
                    stats.large_sell_count += 1

    def get_limit_up_stats(self,
                           stock_code: str) -> Optional[CapitalFlowStats]:
        """获取指定股票的板上资金流向统计"""
        return self.limit_up_flow.get(stock_code)

    def get_limit_up_net_inflow(self, stock_code: str) -> float:
        """获取板上主力净流入（超大单+大单）"""
        stats = self.get_limit_up_stats(stock_code)
        return stats.net_main if stats else 0.0

    def get_all_limit_up_stats(self) -> Dict[str, CapitalFlowStats]:
        """获取所有股票的板上资金流向统计"""
        return self.limit_up_flow.copy()

    def get_limit_up_periods(self, stock_code: str) -> list:
        """获取涨停时段记录"""
        return self.limit_up_periods.get(stock_code, [])

    def get_total_limit_up_duration(self, stock_code: str) -> int:
        """获取累计涨停时长（毫秒）"""
        periods = self.get_limit_up_periods(stock_code)
        total_duration = 0
        for period in periods:
            if period.end_time > 0:
                total_duration += period.duration_ms
            elif period.is_active:
                # 当前仍在涨停中：这里保持与旧实现一致的“兜底计算方式”
                current_time = int(time.time() * 1000000)
                total_duration += (current_time - period.start_time) // 1000
        return total_duration

    def get_top_limit_up_inflow(self, n: int = 10) -> list:
        """获取板上主力净流入前N的股票"""
        result = [(code, stats.net_main)
                  for code, stats in self.limit_up_flow.items()]
        result.sort(key=lambda x: x[1], reverse=True)
        return result[:n]

    def get_combined_report(self, stock_code: str) -> Dict:
        """获取综合报告（全量 + 板上 + 封板）"""
        full_stats = self.get_stats(stock_code)
        board_stats = self.get_limit_up_stats(stock_code)
        seal_info = self.seal_calc.get_seal_info(
            stock_code) if self.seal_calc is not None else None
        periods = self.get_limit_up_periods(stock_code)

        report = {
            'stock_code':
            stock_code,
            'is_limit_up':
            seal_info.is_limit_up if seal_info else False,
            'seal_amount_wan':
            seal_info.seal_amount_wan if seal_info else 0,
            'limit_up_times':
            len(periods),
            'total_limit_up_duration_ms':
            self.get_total_limit_up_duration(stock_code),
        }

        if full_stats:
            report['full'] = full_stats.to_dict()
        if board_stats:
            report['limit_up'] = board_stats.to_dict()

        return report

    def get_stats(self, stock_code: str) -> Optional[CapitalFlowStats]:
        """获取指定股票的资金流向统计"""
        return self.flow_stats.get(stock_code)

    def get_net_inflow(self, stock_code: str) -> float:
        """获取主力净流入（超大单+大单）"""
        stats = self.get_stats(stock_code)
        if stats:
            return stats.net_main
        return 0.0

    def get_all_stats(self) -> Dict[str, CapitalFlowStats]:
        """获取所有股票的资金流向统计"""
        return self.flow_stats.copy()

    def get_top_inflow(self, n: int = 10) -> list:
        """
        获取主力净流入前N的股票
        
        Args:
            n: 返回数量
            
        Returns:
            [(stock_code, net_inflow), ...]
        """
        result = [(code, stats.net_main)
                  for code, stats in self.flow_stats.items()]
        result.sort(key=lambda x: x[1], reverse=True)
        return result[:n]

    def get_top_outflow(self, n: int = 10) -> list:
        """
        获取主力净流出前N的股票
        
        Args:
            n: 返回数量
            
        Returns:
            [(stock_code, net_outflow), ...]
        """
        result = [(code, stats.net_main)
                  for code, stats in self.flow_stats.items()]
        result.sort(key=lambda x: x[1])
        return result[:n]

    def cleanup_old_orders(self, max_age_seconds: int = 3600):
        """
        清理旧的委托记录，防止内存泄漏。

        旧实现仅用 age_seconds 清理并不可靠：时间到了不代表委托“已无用”。
        新策略只清理“可从盘口严格证明不可能仍在簿上”的委托（避免沪市误删，不使用 filled_volume/total_volume）：

        - 买单：若 best_bid > 0 且 order.price > best_bid，则该买单不可能仍在簿上（否则 best_bid 至少应为 order.price）
        - 卖单：若 best_ask > 0 且 order.price < best_ask，则该卖单不可能仍在簿上（否则 best_ask 至多应为 order.price）

        涨停/跌停特殊情况（某一侧全为0）：
        - 若 best_bid == 0 且 best_ask > 0：盘口无任何买单，所有 buy 委托记录都可清理
        - 若 best_ask == 0 且 best_bid > 0：盘口无任何卖单，所有 sell 委托记录都可清理

        额外约束：
        - 仅盘中（连续竞价）执行清理：跳过盘前/集合竞价/盘后
        - freshness guard：quote_time < order.timestamp 时，跳过该订单清理（避免用过期盘口误删）

        Args:
            max_age_seconds: 虚拟委托兜底最大保留时间（秒）
        """
        max_age_ms = max_age_seconds * 1000
        cleaned_count = 0

        # 清理真实委托（不再按 age_seconds 直接清理）
        for stock_code in list(self.order_book.keys()):
            quote_ts = self.last_quote_time.get(stock_code, 0)
            if not self._is_continuous_trading():
                continue

            orders = self.order_book[stock_code]
            best_bid = float(self.best_bid.get(stock_code, 0.0) or 0.0)
            best_ask = float(self.best_ask.get(stock_code, 0.0) or 0.0)

            for entrust_no in list(orders.keys()):
                order = orders[entrust_no]
                if order.price <= 0:
                    continue

                # freshness guard
                if order.timestamp > 0 and quote_ts > 0 and quote_ts < order.timestamp:
                    continue

                should_cleanup = False

                if order.is_buy:
                    if best_bid > 0:
                        if order.price > best_bid:
                            should_cleanup = True
                    elif best_ask > 0:
                        # 无买盘（典型：跌停封死），不应存在任何挂买单
                        should_cleanup = True

                elif order.is_sell:
                    if best_ask > 0:
                        if order.price < best_ask:
                            should_cleanup = True
                    elif best_bid > 0:
                        # 无卖盘（典型：涨停封死），不应存在任何挂卖单
                        should_cleanup = True

                else:
                    logger.warning(
                        f"未知买入方向： {stock_code} entrust_no={entrust_no}, {order}"
                    )

                if should_cleanup:
                    del orders[entrust_no]
                    cleaned_count += 1

            if not orders:
                self.order_book.pop(stock_code, None)

        # 清理虚拟委托（使用 vo.last_update_time 兜底；同样仅盘中执行）
        for stock_code in list(self.virtual_orders.keys()):
            quote_ts = self.last_quote_time.get(stock_code, 0)
            if not self._is_continuous_trading():
                continue

            v_orders = self.virtual_orders[stock_code]
            for entrust_no in list(v_orders.keys()):
                vo = v_orders[entrust_no]
                # 直接使用 vo.last_update_time 作为“最后活跃时间”
                # 注意：last_update_time 来自数据流 time 字段；因此依然只在盘中(quote_ts)进行该兜底清理
                if vo.last_update_time > 0 and quote_ts > 0 and quote_ts - vo.last_update_time > max_age_ms:
                    del v_orders[entrust_no]
                    cleaned_count += 1

            if not v_orders:
                self.virtual_orders.pop(stock_code, None)

        if cleaned_count > 0:
            logger.info(f"Cleaned {cleaned_count} old orders")

    def get_summary(self) -> Dict:
        """获取计算器运行统计"""
        return {
            'total_stocks':
            len(self.flow_stats),
            'total_orders_in_book':
            sum(len(orders) for orders in self.order_book.values()),
            'total_virtual_orders':
            sum(len(orders) for orders in self.virtual_orders.values()),
            'total_orders_processed':
            self.total_orders_processed,
            'total_transactions_processed':
            self.total_transactions_processed,
            'total_cancels_processed':
            self.total_cancels_processed
        }


# ======== Backward compatibility ========
# 旧代码/旧文档中大量引用 CapitalFlowCalculator，这里保留别名，避免全仓库强制改名。
CapitalFlowCalculator = Level2Calculator
