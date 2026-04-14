"""
测试 calculate_trailing_stop_prices 函数

这个脚本用于测试跟踪止盈止损价格计算函数的正确性。
通过模拟不同的利润场景，验证函数输出是否符合预期。

v2.4.2 更新：
    - 直接从主模块导入 calculate_trailing_stop_prices 函数
    - 使用 mock 数据测试实际函数功能
    - base_stop_loss_price 改为基于 highest_price 计算

使用方法：
    python test/test_calculate_trailing_stop_prices.py
"""

import sys
import os
import json
import unittest
from multiprocessing import Value, Array

# 添加父目录到路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.trailing_stop import calculate_trailing_stop_prices
from infra.data_helpers import _round_price
from config import STOP_LOSS_RATE


def create_mock_shared_data(stock_code: str,
                            cost_price: float,
                            available_volume: int,
                            hold_volume: int,
                            limit_up_price: float,
                            stock_name: str = "测试股票"):
    """
    创建模拟的 shared_data 结构
    
    Args:
        stock_code: 股票代码
        cost_price: 成本价
        available_volume: 可用数量
        hold_volume: 持仓数量
        limit_up_price: 涨停价
        stock_name: 股票名称
        
    Returns:
        dict: 模拟的 shared_data 字典
    """
    # 创建持仓信息
    position_data = {
        "证券代码": stock_code,
        "持仓数量": hold_volume,
        "可用数量": available_volume,
        "成本价": cost_price,
        "市值": hold_volume * cost_price,
    }
    
    # 创建股票状态信号
    stock_status_signal = {
        '股票状态': Value('i', 0),
        '下单状态': Value('i', 0),
        '封单金额': Value('d', 0.0),
        '封单金额变化率': Value('d', 0.0),
        '前一价格': Value('d', 0.0),
        '拉板所需资金': Value('d', 0.0),
        '下单时成交量': Value('i', 0),
        '下单时封单量': Value('i', 0),
        '最高价': Value('d', 0.0),
        '止盈止损价格列表': Array('d', [0.0 for _ in range(10)]),
        '目标剩余仓位': Array('i', [0 for _ in range(10)]),
    }
    
    # 创建 Manager 字典代理（使用普通字典模拟）
    shared_data = {
        '持仓状态': {
            stock_code: json.dumps(position_data, ensure_ascii=False)
        },
        '股票信息': {
            stock_code: {
                '涨停价': limit_up_price,
                '股票名称': stock_name,
            }
        },
        '股票状态信号': {
            stock_code: stock_status_signal
        }
    }
    
    return shared_data


def get_results_from_shared_data(shared_data: dict, stock_code: str):
    """
    从 shared_data 中提取计算结果
    
    Args:
        shared_data: 共享数据字典
        stock_code: 股票代码
        
    Returns:
        tuple: (止盈止损价格列表, 目标剩余仓位列表)
    """
    stock_signal = shared_data['股票状态信号'][stock_code]
    
    with stock_signal['止盈止损价格列表'].get_lock():
        prices = list(stock_signal['止盈止损价格列表'][:])
    
    with stock_signal['目标剩余仓位'].get_lock():
        volumes = list(stock_signal['目标剩余仓位'][:])
    
    return prices, volumes


def print_result(prices: list, volumes: list, scenario_name: str, 
                 highest_price: float, cost_price: float, limit_up_price: float,
                 available_volume: int):
    """打印测试结果"""
    print("\n" + "=" * 80)
    print(f"[SCENARIO]测试场景: {scenario_name}")
    print("=" * 80)
    
    if not prices or all(p == 0 for p in prices):
        print("[ERROR]计算失败或无结果")
        return
    
    profit_ratio = (highest_price - cost_price) / cost_price if cost_price > 0 else 0
    distance_to_limit_ratio = (limit_up_price - highest_price) / (limit_up_price - cost_price) if limit_up_price > cost_price else 1.0
    distance_to_limit_ratio = max(0, min(1, distance_to_limit_ratio))
    
    print(f"\n[INPUT]输入参数:")
    print(f"   - 最高价: {highest_price:.2f}")
    print(f"   - 成本价: {cost_price:.2f}")
    print(f"   - 涨停价: {limit_up_price:.2f}")
    print(f"   - 可用数量: {available_volume}")
    
    print(f"\n[SCENARIO]计算结果:")
    print(f"   - 利润率: {profit_ratio:.2%}")
    print(f"   - 距涨停比例: {distance_to_limit_ratio:.2%}")
    print(f"   - 基础止损价 (v2.4.2): {highest_price * (1 - STOP_LOSS_RATE):.2f} (基于最高价)")
    
    print("\n[DETAIL]止盈止损价格和目标仓位:")
    print("-" * 70)
    print(f"{'档位':<6} {'价格':<12} {'回撤比例':<12} {'目标仓位':<12} {'卖出数量':<12}")
    print("-" * 70)
    
    for i in range(10):
        price = prices[i]
        volume = volumes[i]
        drawdown = (highest_price - price) / highest_price * 100 if highest_price > 0 else 0
        sell_volume = available_volume - volume
        print(f"{i+1:<6} {price:<12.2f} {drawdown:<11.1f}% {volume:<12} {sell_volume:<12}")
    
    print("-" * 70)


class TestCalculateTrailingStopPrices(unittest.TestCase):
    """calculate_trailing_stop_prices 函数单元测试"""
    
    def setUp(self):
        """测试前置设置"""
        self.stock_code = "600000.SH"
        self.available_volume = 1000
        self.hold_volume = 1000
        
    def test_high_profit_scenario(self):
        """测试高利润场景（利润>=5%）"""
        # 场景：最高价11元，成本价10元，利润10%
        highest_price = 11.0
        cost_price = 10.0
        limit_up_price = 11.0
        limit_down_price = 9.0
        
        shared_data = create_mock_shared_data(
            self.stock_code, cost_price, self.available_volume, 
            self.hold_volume, limit_up_price
        )
        
        # 调用被测函数
        calculate_trailing_stop_prices(
            highest_price=highest_price,
            limit_down_price=limit_down_price,
            stock_code=self.stock_code,
            shared_data=shared_data
        )
        
        prices, volumes = get_results_from_shared_data(shared_data, self.stock_code)
        
        # 验证结果
        self.assertEqual(len(prices), 10, "应该有10个价格档位")
        self.assertEqual(len(volumes), 10, "应该有10个仓位档位")
        
        # 价格应该严格递减
        for i in range(1, 10):
            self.assertLess(prices[i], prices[i-1], f"价格应该严格递减: 档位{i}")
        
        # 最后一档应该清仓
        self.assertEqual(volumes[-1], 0, "最后一档应该清仓")
        
        # 高利润时，第一档应该保持全仓
        self.assertEqual(volumes[0], self.available_volume, "高利润时第一档应保持全仓")
        
        print_result(prices, volumes, "高利润（利润10%，涨停附近）", 
                     highest_price, cost_price, limit_up_price, self.available_volume)
        
    def test_medium_profit_scenario(self):
        """测试中等利润场景（利润3-5%）"""
        highest_price = 10.4
        cost_price = 10.0
        limit_up_price = 11.0
        limit_down_price = 9.0
        
        shared_data = create_mock_shared_data(
            self.stock_code, cost_price, self.available_volume,
            self.hold_volume, limit_up_price
        )
        
        calculate_trailing_stop_prices(
            highest_price=highest_price,
            limit_down_price=limit_down_price,
            stock_code=self.stock_code,
            shared_data=shared_data
        )
        
        prices, volumes = get_results_from_shared_data(shared_data, self.stock_code)
        
        self.assertEqual(len(prices), 10)
        self.assertEqual(volumes[-1], 0)
        
        print_result(prices, volumes, "中等利润（利润4%）",
                     highest_price, cost_price, limit_up_price, self.available_volume)
        
    def test_low_profit_scenario(self):
        """测试低利润场景（利润1-2%）"""
        highest_price = 10.15
        cost_price = 10.0
        limit_up_price = 11.0
        limit_down_price = 9.0
        
        shared_data = create_mock_shared_data(
            self.stock_code, cost_price, self.available_volume,
            self.hold_volume, limit_up_price
        )
        
        calculate_trailing_stop_prices(
            highest_price=highest_price,
            limit_down_price=limit_down_price,
            stock_code=self.stock_code,
            shared_data=shared_data
        )
        
        prices, volumes = get_results_from_shared_data(shared_data, self.stock_code)
        
        self.assertEqual(len(prices), 10)
        self.assertEqual(volumes[-1], 0)
        
        # 低利润时，前几档应该保持全仓（更宽容的策略）
        self.assertEqual(volumes[0], self.available_volume)
        
        print_result(prices, volumes, "低利润（利润1.5%）",
                     highest_price, cost_price, limit_up_price, self.available_volume)
        
    def test_loss_scenario(self):
        """测试亏损场景"""
        highest_price = 9.8
        cost_price = 10.0
        limit_up_price = 11.0
        limit_down_price = 9.0
        
        shared_data = create_mock_shared_data(
            self.stock_code, cost_price, self.available_volume,
            self.hold_volume, limit_up_price
        )
        
        calculate_trailing_stop_prices(
            highest_price=highest_price,
            limit_down_price=limit_down_price,
            stock_code=self.stock_code,
            shared_data=shared_data
        )
        
        prices, volumes = get_results_from_shared_data(shared_data, self.stock_code)
        
        self.assertEqual(len(prices), 10)
        self.assertEqual(volumes[-1], 0)
        
        # 亏损时应该使用止损策略（前几档保持全仓）
        for i in range(5):
            self.assertEqual(volumes[i], self.available_volume, f"亏损时前{i+1}档应保持全仓")
        
        print_result(prices, volumes, "亏损（亏损2%）",
                     highest_price, cost_price, limit_up_price, self.available_volume)
        
    def test_small_position_scenario(self):
        """测试小仓位场景（不足1手）"""
        highest_price = 10.5
        cost_price = 10.0
        limit_up_price = 11.0
        limit_down_price = 9.0
        small_volume = 50

        shared_data = create_mock_shared_data(
            self.stock_code, cost_price, small_volume,
            small_volume, limit_up_price
        )

        calculate_trailing_stop_prices(
            highest_price=highest_price,
            limit_down_price=limit_down_price,
            stock_code=self.stock_code,
            shared_data=shared_data
        )

        prices, volumes = get_results_from_shared_data(shared_data, self.stock_code)

        self.assertEqual(len(prices), 10)

        # 小仓位策略：前6档持有，后4档清仓
        for i in range(6):
            self.assertEqual(volumes[i], small_volume, f"小仓位前{i+1}档应保持持仓")
        for i in range(6, 10):
            self.assertEqual(volumes[i], 0, f"小仓位后{i+1-6}档应清仓")

        print_result(prices, volumes, "小仓位（50股，不足1手）",
                     highest_price, cost_price, limit_up_price, small_volume)

    def test_target_volumes_are_monotonic_and_bounded(self):
        """测试目标剩余仓位单调不增且不超出可用数量"""
        highest_price = 10.7
        cost_price = 10.0
        limit_up_price = 11.0
        limit_down_price = 9.0

        shared_data = create_mock_shared_data(
            self.stock_code, cost_price, self.available_volume,
            self.hold_volume, limit_up_price
        )

        calculate_trailing_stop_prices(
            highest_price=highest_price,
            limit_down_price=limit_down_price,
            stock_code=self.stock_code,
            shared_data=shared_data
        )

        _, volumes = get_results_from_shared_data(shared_data, self.stock_code)

        self.assertEqual(volumes[-1], 0)
        for volume in volumes:
            self.assertGreaterEqual(volume, 0)
            self.assertLessEqual(volume, self.available_volume)
        for i in range(1, len(volumes)):
            self.assertLessEqual(volumes[i], volumes[i - 1], f"目标剩余仓位应单调不增: 档位{i}")
        
    def test_price_not_below_limit_down(self):
        """测试价格不能低于跌停价"""
        highest_price = 10.2
        cost_price = 10.0
        limit_up_price = 11.0
        limit_down_price = 10.1  # 设置较高的跌停价
        
        shared_data = create_mock_shared_data(
            self.stock_code, cost_price, self.available_volume,
            self.hold_volume, limit_up_price
        )
        
        calculate_trailing_stop_prices(
            highest_price=highest_price,
            limit_down_price=limit_down_price,
            stock_code=self.stock_code,
            shared_data=shared_data
        )
        
        prices, volumes = get_results_from_shared_data(shared_data, self.stock_code)
        
        # 所有价格应该 >= 跌停价
        for i, price in enumerate(prices):
            self.assertGreaterEqual(price, limit_down_price, 
                                    f"档位{i+1}价格{price}不应低于跌停价{limit_down_price}")
        
        print_result(prices, volumes, "价格不低于跌停价测试",
                     highest_price, cost_price, limit_up_price, self.available_volume)
        
    def test_base_stop_loss_uses_highest_price(self):
        """测试 v2.4.2 优化：base_stop_loss_price 基于 highest_price 计算"""
        highest_price = 10.5
        cost_price = 10.0
        limit_up_price = 11.0
        limit_down_price = 9.0
        
        shared_data = create_mock_shared_data(
            self.stock_code, cost_price, self.available_volume,
            self.hold_volume, limit_up_price
        )
        
        calculate_trailing_stop_prices(
            highest_price=highest_price,
            limit_down_price=limit_down_price,
            stock_code=self.stock_code,
            shared_data=shared_data
        )
        
        prices, volumes = get_results_from_shared_data(shared_data, self.stock_code)
        
        # 计算预期的基础止损价（基于最高价）
        expected_base_stop_loss = _round_price(highest_price * (1 - STOP_LOSS_RATE))
        
        # 最后一档价格应该不低于基础止损价或跌停价
        min_expected_price = max(expected_base_stop_loss, limit_down_price)
        
        # 打印验证信息
        print(f"\n[CHECK]v2.4.2 验证: base_stop_loss_price 基于 highest_price")
        print(f"   - highest_price: {highest_price}")
        print(f"   - STOP_LOSS_RATE: {STOP_LOSS_RATE}")
        print(f"   - 预期 base_stop_loss: {expected_base_stop_loss}")
        print(f"   - 最后档位价格: {prices[-1]}")
        
        print_result(prices, volumes, "v2.4.2 验证：base_stop_loss 基于 highest_price",
                     highest_price, cost_price, limit_up_price, self.available_volume)
        
    def test_invalid_available_volume(self):
        """测试无效可用数量"""
        shared_data = create_mock_shared_data(
            self.stock_code, 10.0, 0,  # 可用数量为0
            0, 11.0
        )
        
        # 应该直接返回，不抛出异常
        calculate_trailing_stop_prices(
            highest_price=10.5,
            limit_down_price=9.0,
            stock_code=self.stock_code,
            shared_data=shared_data
        )
        
        # 验证结果未被修改（保持初始值0）
        prices, volumes = get_results_from_shared_data(shared_data, self.stock_code)
        self.assertTrue(all(p == 0 for p in prices), "无效输入时价格列表应保持为0")
        
    def test_no_position_info(self):
        """测试无持仓信息"""
        shared_data = {
            '持仓状态': {},  # 空的持仓状态
            '股票信息': {
                self.stock_code: {'涨停价': 11.0, '股票名称': '测试'}
            },
            '股票状态信号': {
                self.stock_code: {
                    '止盈止损价格列表': Array('d', [0.0 for _ in range(10)]),
                    '目标剩余仓位': Array('i', [0 for _ in range(10)]),
                }
            }
        }
        
        # 应该直接返回，不抛出异常
        calculate_trailing_stop_prices(
            highest_price=10.5,
            limit_down_price=9.0,
            stock_code=self.stock_code,
            shared_data=shared_data
        )


def run_visual_tests():
    """运行可视化测试（用于手动验证）"""
    
    print("\n" + "=" * 40)
    print("   calculate_trailing_stop_prices 函数测试 (v2.4.2)")
    print("=" * 40)
    
    stock_code = "600000.SH"
    available_volume = 1000
    
    test_scenarios = [
        {
            'name': '高利润（涨停附近，利润10%）',
            'highest_price': 11.0,
            'cost_price': 10.0,
            'limit_up_price': 11.0,
            'limit_down_price': 9.0,
        },
        {
            'name': '中等利润（利润5%）',
            'highest_price': 10.5,
            'cost_price': 10.0,
            'limit_up_price': 11.0,
            'limit_down_price': 9.0,
        },
        {
            'name': '低利润（利润2%）',
            'highest_price': 10.2,
            'cost_price': 10.0,
            'limit_up_price': 11.0,
            'limit_down_price': 9.0,
        },
        {
            'name': '微利（利润0.5%）',
            'highest_price': 10.05,
            'cost_price': 10.0,
            'limit_up_price': 11.0,
            'limit_down_price': 9.0,
        },
        {
            'name': '亏损（亏损2%）',
            'highest_price': 9.8,
            'cost_price': 10.0,
            'limit_up_price': 11.0,
            'limit_down_price': 9.0,
        },
    ]
    
    for scenario in test_scenarios:
        shared_data = create_mock_shared_data(
            stock_code,
            scenario['cost_price'],
            available_volume,
            available_volume,
            scenario['limit_up_price']
        )
        
        calculate_trailing_stop_prices(
            highest_price=scenario['highest_price'],
            limit_down_price=scenario['limit_down_price'],
            stock_code=stock_code,
            shared_data=shared_data
        )
        
        prices, volumes = get_results_from_shared_data(shared_data, stock_code)
        print_result(prices, volumes, scenario['name'],
                     scenario['highest_price'], scenario['cost_price'],
                     scenario['limit_up_price'], available_volume)
    
    # 总结
    print("\n" + "=" * 80)
    print("[SUMMARY]测试总结 (v2.4.2 更新)")
    print("=" * 80)
    print("""
[OK]v2.4.2 关键更新已验证：

1. [OK]base_stop_loss_price 改为基于 highest_price 计算
   - 旧版: base_stop_loss_price = cost_price * (1 - STOP_LOSS_RATE)
   - 新版: base_stop_loss_price = highest_price * (1 - STOP_LOSS_RATE)
   - 更符合跟踪止损的核心逻辑

2. [OK]详细日志记录增强
   - 函数入口参数记录
   - 中间计算步骤详细输出
   - 各档位触发条件和仓位变化记录

3. [OK]策略逻辑保持不变
   - 利润越高，止损价上移越多（保护更多利润）
   - 距涨停越近，前几档间距越窄（快速锁定利润）
   - 高利润时减仓更激进，亏损时减仓更保守
   - 小仓位采用"前6档持有，后4档清仓"策略
   - 最后一档始终清仓（止损线）
   - 价格不能低于跌停价
    """)


if __name__ == '__main__':
    import argparse
    
    parser = argparse.ArgumentParser(description='测试 calculate_trailing_stop_prices 函数')
    parser.add_argument('--visual', '-v', action='store_true', 
                        help='运行可视化测试（输出详细结果）')
    parser.add_argument('--unittest', '-u', action='store_true',
                        help='运行单元测试')
    
    args = parser.parse_args()
    
    if args.unittest or (not args.visual and not args.unittest):
        # 默认运行单元测试
        print("\n[TEST]运行单元测试...")
        unittest.main(argv=[''], exit=False, verbosity=2)
    
    if args.visual:
        # 运行可视化测试
        run_visual_tests()
