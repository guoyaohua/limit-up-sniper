#coding=utf-8
import time
from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockAccount
from infra.common_enums import OrderType, PriceType, OrderStatus
from infra.utils import send_email


class MyXtQuantTraderCallback(XtQuantTraderCallback):
    def __init__(self, logger, stategy_name=''):
        super(MyXtQuantTraderCallback, self).__init__()
        self.logger = logger
        self.stategy_name = stategy_name
        # 记录委托下单时间
        self.order_dict = {}

    def on_disconnected(self):
        """
        连接断开
        :return:
        """
        self.logger.warning('连接已断开')
        subject = f'{self.stategy_name} [交易模块异常] 连接断开' if self.stategy_name else '[交易模块异常] 连接断开'
        send_email(subject, '连接已断开')

    def on_stock_order(self, order):
        """
        委托回报推送
        :param order: XtOrder对象
        :return:

        委托XtOrder
        属性	类型	注释
        account_type	int	账号类型，参见数据字典
        account_id	str	资金账号
        stock_code	str	证券代码，例如"600000.SH"
        order_id	int	订单编号
        order_sysid	str	柜台合同编号
        order_time	int	报单时间
        order_type	int	委托类型，参见数据字典
        order_volume	int	委托数量
        price_type	int	报价类型，该字段在返回时为柜台返回类型，不等价于下单传入的price_type，枚举值不一样功能一样，参见数据字典
        price	float	委托价格
        traded_volume	int	成交数量
        traded_price	float	成交均价
        order_status	int	委托状态，参见数据字典
        status_msg	str	委托状态描述，如废单原因
        strategy_name	str	策略名称
        order_remark	str	委托备注
        direction	int	多空方向，股票不适用；参见数据字典
        offset_flag	int	交易操作，用此字段区分股票买卖，期货开、平仓，期权买卖等；参见数据字典
        """
        msg = '[委托回报推送]\t'
        msg += f'证券代码: {order.stock_code}, '
        msg += f'订单编号: {order.order_id}, '
        msg += f'柜台合同编号: {order.order_sysid}, '
        msg += f'报单时间: {order.order_time}, '
        msg += f'委托类型: {OrderType(order.order_type).name}, '
        msg += f'委托数量: {order.order_volume}, '
        msg += f'报价类型: {PriceType(order.price_type).name}, '
        msg += f'委托价格: {order.price}, '
        msg += f'成交数量: {order.traded_volume}, '
        msg += f'成交均价: {order.traded_price}, '
        msg += f'委托状态: {OrderStatus(order.order_status).name}, '
        msg += f'委托状态描述: {order.status_msg}, '
        msg += f'策略名称: {order.strategy_name}, '
        msg += f'委托备注: {order.order_remark}, '
        self.logger.warning(msg)
        subject = f'{self.stategy_name} [委托回报推送] {order.stock_code} {order.order_id}' if self.stategy_name else f'[委托回报推送] {order.stock_code} {order.order_id}'
        send_email(subject, msg)

    def on_stock_trade(self, trade):
        """
        成交变动推送
        :param trade: XtTrade对象
        :return:

        成交XtTrade
        属性	类型	注释
        account_type	int	账号类型，参见数据字典
        account_id	str	资金账号
        stock_code	str	证券代码
        order_type	int	委托类型，参见数据字典
        traded_id	str	成交编号
        traded_time	int	成交时间
        traded_price	float	成交均价
        traded_volume	int	成交数量
        traded_amount	float	成交金额
        order_id	int	订单编号
        order_sysid	str	柜台合同编号
        strategy_name	str	策略名称
        order_remark	str	委托备注
        direction	int	多空方向，股票不适用；参见数据字典
        offset_flag	int	交易操作，用此字段区分股票买卖，期货开、平仓，期权买卖等；参见数据字典

        """
        msg = '[成交变动推送]\t'
        msg += f'证券代码: {trade.stock_code}, '
        msg += f'委托类型: {OrderType(trade.order_type).name}, '
        msg += f'成交编号: {trade.traded_id}, '
        msg += f'成交时间: {trade.traded_time}, '
        msg += f'成交均价: {trade.traded_price}, '
        msg += f'成交数量: {trade.traded_volume}, '
        msg += f'成交金额: {trade.traded_amount}, '
        msg += f'订单编号: {trade.order_id}, '
        msg += f'柜台合同编号: {trade.order_sysid}, '
        msg += f'策略名称: {trade.strategy_name}, '
        msg += f'委托备注: {trade.order_remark}, '
        msg += f'交易操作: {trade.offset_flag}'
        self.logger.warning(msg)
        subject = f'{self.stategy_name} [成交变动推送] {trade.stock_code} {trade.traded_id}' if self.stategy_name else f'[成交变动推送] {trade.stock_code} {trade.traded_id}'
        send_email(subject, msg)

    def on_order_error(self, order_error):
        """
        委托失败推送
        :param order_error:XtOrderError 对象
        :return:

        下单失败错误XtOrderError
        属性	类型	注释
        account_type	int	账号类型，参见数据字典
        account_id	str	资金账号
        order_id	int	订单编号
        error_id	int	下单失败错误码
        error_msg	str	下单失败具体信息
        strategy_name	str	策略名称
        order_remark	str	委托备注

        """
        msg = '[委托失败推送]\t'
        msg += f'资金账号: {order_error.account_id}, '
        msg += f'订单编号: {order_error.order_id}, '
        msg += f'下单失败错误码: {order_error.error_id}, '
        msg += f'下单失败具体信息: {order_error.error_msg}, '
        msg += f'策略名称: {order_error.strategy_name}, '
        msg += f'委托备注: {order_error.order_remark}'
        self.logger.warning(msg)
        subject = f'{self.stategy_name} [委托失败推送] {order_error.order_id} 错误码{order_error.error_id}' if self.stategy_name else f'[委托失败推送] {order_error.order_id} 错误码{order_error.error_id}'
        send_email(subject, msg)

    def on_cancel_error(self, cancel_error):
        """
        撤单失败推送
        :param cancel_error: XtCancelError 对象
        :return:

        撤单失败错误XtCancelError
        属性	类型	注释
        account_type	int	账号类型，参见数据字典
        account_id	str	资金账号
        order_id	int	订单编号
        market	int	交易市场 0:上海 1:深圳
        order_sysid	str	柜台委托编号
        error_id	int	下单失败错误码
        error_msg	str	下单失败具体信息
        """

        msg = '[撤单失败推送]\t'
        msg += f'资金账号: {cancel_error.account_id}, '
        msg += f'订单编号: {cancel_error.order_id}, '
        msg += f'交易市场: {cancel_error.market}, '
        msg += f'柜台委托编号: {cancel_error.order_sysid}, '
        msg += f'下单失败错误码: {cancel_error.error_id}, '
        msg += f'下单失败具体信息: {cancel_error.error_msg}'
        self.logger.warning(msg)
        subject = f'{self.stategy_name} [撤单失败推送] {cancel_error.order_id} 错误码{cancel_error.error_id}' if self.stategy_name else f'[撤单失败推送] {cancel_error.order_id} 错误码{cancel_error.error_id}'
        send_email(subject, msg)

    def on_order_stock_async_response(self, response):
        """
        异步下单回报推送
        :param response: XtOrderResponse 对象
        :return:

        异步下单委托反馈XtOrderResponse
        属性	类型	注释
        account_type	int	账号类型，参见数据字典
        account_id	str	资金账号
        order_id	int	订单编号
        strategy_name	str	策略名称
        order_remark	str	委托备注
        seq	int	异步下单的请求序号
        """
        msg = '[异步下单回报推送]\t'
        msg += f'资金账号: {response.account_id}, '
        msg += f'订单编号: {response.order_id}, '
        msg += f'策略名称: {response.strategy_name}, '
        msg += f'委托备注: {response.order_remark}, '
        msg += f'请求序号: {response.seq}'
        self.logger.warning(msg)
        subject = f'{self.stategy_name} [异步下单回报推送] {response.order_id} 序号{response.seq}' if self.stategy_name else f'[异步下单回报推送] {response.order_id} 序号{response.seq}'
        send_email(subject, msg)

    def on_account_status(self, status):
        """
        :param response: XtAccountStatus 对象
        :return:

        账号状态XtAccountStatus
        属性	类型	注释
        account_type	int	账号类型，参见数据字典
        account_id	str	资金账号
        status	int	账号状态，参见数据字典
        """
        msg = '[账号状态]\t'
        msg += f'资金账号: {status.account_id}, '
        msg += f'账号状态: {status.status}'
        self.logger.warning(msg)
        subject = f'{self.stategy_name} [账号状态变更] {status.account_id} 状态{status.status}' if self.stategy_name else f'[账号状态变更] {status.account_id} 状态{status.status}'
        send_email(subject, msg)


def get_trader_entity(logger, client_path, stock_account, stategy_name=''):
    """
    获取交易接口实例
    :param logger: 日志对象
    :return: 交易接口实例
    """
    # session_id为会话编号，策略使用方对于不同的Python策略需要使用不同的会话编号
    session_id = int(time.time())
    xt_trader = XtQuantTrader(client_path, session_id)

    # 创建资金账号为 stock_account 的证券账号对象
    acc = StockAccount(stock_account)
    # StockAccount可以用第二个参数指定账号类型，如沪港通传'HUGANGTONG'，深港通传'SHENGANGTONG'
    # acc = StockAccount('<redacted-account>','STOCK')

    # 启动交易线程
    xt_trader.start()

    # 建立交易连接，返回0表示连接成功
    connect_result = xt_trader.connect()

    while connect_result != 0:
        logger.error('连接失败, 请检查行情端是否正常运行')
        time.sleep(5)
        connect_result = xt_trader.connect()

    logger.info(f'连接成功, 会话编号: {session_id}')

    # 创建交易回调类对象，并声明接收回调
    callback = MyXtQuantTraderCallback(logger, stategy_name)
    xt_trader.register_callback(callback)
    # 对交易回调进行订阅，订阅后可以收到交易主推，返回0表示订阅成功
    subscribe_result = xt_trader.subscribe(acc)

    while subscribe_result != 0:
        logger.error('交易回调订阅失败, 请检查行情端是否正常运行')
        send_email(f'{stategy_name} [交易模块异常]', '交易回调订阅失败, 请检查行情端是否正常运行')
        time.sleep(5)
        subscribe_result = xt_trader.subscribe(acc)

    return xt_trader, acc
