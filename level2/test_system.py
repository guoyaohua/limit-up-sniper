"""
性能测试和验证脚本

测试系统的各个组件性能和正确性
"""

import time
import random
import sys
import logging
from pathlib import Path
from datetime import datetime

# Enable INFO logging (use DEBUG for detailed troubleshooting)
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# Add parent directory to path to allow imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from level2.buffers.ring_buffer import SharedMemoryRingBuffer, BufferConfig
from level2.calculators.l2_calculators import CapitalFlowCalculator, SealAmountCalculator
from level2.enums import get_limit_price


def test_buffer_performance():
    """测试缓冲区写入性能 - 优化后"""
    print("=" * 70)
    print("测试1: 缓冲区写入性能 (单条写入)")
    print("=" * 70)
    
    # 创建测试缓冲区
    config = BufferConfig(name="test_perf", slot_count=100000, slot_data_size=512)
    buffer = SharedMemoryRingBuffer(config, create=True)
    
    # 准备测试数据
    test_data = {
        'type': 'l2order',
        'stock_code': '600000.SH',
        'data': {
            'time': 1234567890000,
            'price': 10.5,
            'volume': 1000,
            'entrustNo': 12345,
            'entrustDirection': 1
        }
    }
    
    # 性能测试
    count = 10000
    start_time = time.time()
    
    for i in range(count):
        buffer.put_msgpack(test_data)
    
    elapsed = time.time() - start_time
    
    print(f"写入 {count} 条数据")
    print(f"总耗时: {elapsed*1000:.2f}ms")
    print(f"平均每次: {elapsed/count*1000000:.2f}μs")
    print(f"吞吐量: {count/elapsed:.0f} 条/秒")
    
    # 验证性能指标
    avg_time_us = elapsed / count * 1000000
    baseline_us = 27.08  # v1.0基准性能
    improvement = baseline_us / avg_time_us
    
    print(f"基准性能: {baseline_us:.2f}μs")
    print(f"性能提升: {improvement:.2f}x")
    
    if avg_time_us < 10:
        print("✅ 性能测试通过 (< 10μs)")
    else:
        print(f"⚠️  性能警告 ({avg_time_us:.2f}μs > 10μs)")
    
    # 清理
    buffer.unlink()
    print()


def test_batch_write_performance():
    """测试批量写入性能 - 新增优化测试"""
    print("=" * 70)
    print("测试1b: 批量写入性能")
    print("=" * 70)
    
    config = BufferConfig(name="test_batch", slot_count=100000, slot_data_size=512)
    buffer = SharedMemoryRingBuffer(config, create=True)
    
    # 准备批量数据 (模拟回调场景)
    batch_size = 100
    batch_data = []
    for i in range(batch_size):
        batch_data.append({
            'type': 'l2order',
            'stock_code': f'{600000+i:06d}.SH',
            'data': {
                'time': 1234567890000 + i,
                'price': 10.5 + i * 0.01,
                'volume': 1000 + i * 10,
                'entrustNo': 12345 + i,
                'entrustDirection': 1
            }
        })
    
    # 测试批量写入
    iterations = 100
    start_time = time.time()
    
    for _ in range(iterations):
        batch_timestamp = int(time.time() * 1000000)
        buffer.put_msgpack_batch(batch_data, batch_timestamp)
    
    elapsed = time.time() - start_time
    total_writes = iterations * batch_size
    
    print(f"批量大小: {batch_size}")
    print(f"批次数: {iterations}")
    print(f"总写入: {total_writes} 条")
    print(f"总耗时: {elapsed*1000:.2f}ms")
    print(f"平均每次: {elapsed/total_writes*1000000:.2f}μs")
    print(f"吞吐量: {total_writes/elapsed:.0f} 条/秒")
    
    avg_time_us = elapsed / total_writes * 1000000
    baseline_us = 27.08
    improvement = baseline_us / avg_time_us
    
    print(f"基准性能: {baseline_us:.2f}μs")
    print(f"性能提升: {improvement:.2f}x")
    
    if avg_time_us < 10:
        print("✅ 批量写入性能优秀 (< 10μs)")
    elif avg_time_us < 15:
        print(f"✅ 批量写入性能良好 ({avg_time_us:.2f}μs < 15μs)")
    else:
        print(f"⚠️  需要进一步优化 ({avg_time_us:.2f}μs)")
    
    buffer.unlink()
    print()


def test_fast_write_performance():
    """测试快速写入性能 - 使用预计算timestamp"""
    print("=" * 70)
    print("测试1c: 快速写入性能 (预计算timestamp)")
    print("=" * 70)
    
    config = BufferConfig(name="test_fast", slot_count=100000, slot_data_size=512)
    buffer = SharedMemoryRingBuffer(config, create=True)
    
    test_data = {
        'type': 'l2order',
        'stock_code': '600000.SH',
        'data': {
            'time': 1234567890000,
            'price': 10.5,
            'volume': 1000,
            'entrustNo': 12345,
            'entrustDirection': 1
        }
    }
    
    # 预计算timestamp
    timestamp = int(time.time() * 1000000)
    
    count = 10000
    start_time = time.time()
    
    for i in range(count):
        buffer.put_msgpack_fast(test_data, timestamp)
    
    elapsed = time.time() - start_time
    
    print(f"写入 {count} 条数据")
    print(f"总耗时: {elapsed*1000:.2f}ms")
    print(f"平均每次: {elapsed/count*1000000:.2f}μs")
    print(f"吞吐量: {count/elapsed:.0f} 条/秒")
    
    avg_time_us = elapsed / count * 1000000
    baseline_us = 27.08
    improvement = baseline_us / avg_time_us
    
    print(f"基准性能: {baseline_us:.2f}μs")
    print(f"性能提升: {improvement:.2f}x")
    
    if avg_time_us < 8:
        print("✅ 快速写入性能优秀 (< 8μs)")
    elif avg_time_us < 10:
        print(f"✅ 快速写入达标 ({avg_time_us:.2f}μs < 10μs)")
    else:
        print(f"⚠️  未达到最优性能 ({avg_time_us:.2f}μs)")
    
    buffer.unlink()
    print()


def test_capital_flow_calculator():
    """测试资金流向计算器"""
    print("=" * 70)
    print("测试2: 资金流向计算器")
    print("=" * 70)
    
    calc = CapitalFlowCalculator()
    
    # 模拟委托数据
    order_data = {
        'entrustNo': 1001,
        'entrustDirection': 1,  # 买入
        'volume': 500000,  # 50万股 - 超大单
        'price': 10.0,
        'time': 1234567890000
    }
    
    calc.on_l2order('600000.SH', order_data)
    
    # 模拟成交数据（多笔小成交）
    for i in range(5):
        trans_data = {
            'buyNo': 1001,
            'sellNo': 2001,
            'volume': 100000,  # 每笔10万股
            'amount': 1000000,  # 每笔100万元
            'price': 10.0,
            'tradeFlag': 1,  # 主动买入
            'time': 1234567890000 + i
        }
        calc.on_l2transaction('600000.SH', trans_data)
    
    # 验证结果
    stats = calc.get_stats('600000.SH')
    print(f"股票: 600000.SH")
    print(f"超大单买入: {stats.super_large_buy/10000:.2f}万元")
    print(f"超大单卖出: {stats.super_large_sell/10000:.2f}万元")
    print(f"主力净流入: {stats.net_main/10000:.2f}万元")
    
    if stats.super_large_buy == 5000000:  # 5笔 × 100万
        print("✅ 资金流向计算正确")
    else:
        print(f"❌ 计算错误: 期望 500万，实际 {stats.super_large_buy/10000:.2f}万")
    
    print()


def test_seal_amount_calculator():
    """测试封板金额计算器"""
    print("=" * 70)
    print("测试3: 封板金额计算器")
    print("=" * 70)
    
    calc = SealAmountCalculator()
    
    # 设置股票信息
    calc.set_stock_info('600000.SH', last_close=10.0, is_st=False)
    
    # 模拟行情快照（涨停）
    limit_price = get_limit_price(10.0, is_st=False)
    quote_data = {
        'lastPrice': limit_price,
        'bidPrice': [limit_price, 10.8, 10.7],
        'bidVol': [1000000, 500000, 300000],  # 买一100万股封单
        'time': 1234567890000
    }
    
    calc.on_l2quote('600000.SH', quote_data)
    
    # 验证封板金额
    seal_info = calc.get_seal_info('600000.SH')
    print(f"股票: 600000.SH")
    print(f"涨停价: {seal_info.limit_price:.2f}")
    print(f"封单量: {seal_info.current_volume:,}股")
    print(f"封板金额: {seal_info.seal_amount_wan:.2f}万元")
    print(f"是否涨停: {seal_info.is_limit_up}")
    
    expected_amount = 1000000 * limit_price / 10000
    if abs(seal_info.seal_amount_wan - expected_amount) < 0.01:
        print("✅ 封板金额计算正确")
    else:
        print(f"❌ 计算错误: 期望 {expected_amount:.2f}万，实际 {seal_info.seal_amount_wan:.2f}万")
    
    print()


def test_integrated_scenario():
    """测试综合场景"""
    print("=" * 70)
    print("测试4: 综合场景测试")
    print("=" * 70)
    
    # 创建计算器
    seal_calc = SealAmountCalculator()
    flow_calc = CapitalFlowCalculator(seal_calculator=seal_calc, enable_limit_up_flow=True)
    
    # 设置股票信息
    stock_code = '000001.SZ'
    last_close = 20.0
    seal_calc.set_stock_info(stock_code, last_close)
    limit_price = get_limit_price(last_close)
    
    print(f"测试股票: {stock_code}")
    print(f"昨收价: {last_close:.2f}")
    print(f"涨停价: {limit_price:.2f}")
    print()
    
    # 场景1: 股票涨停
    print("场景1: 股票涨停")
    quote_data = {
        'lastPrice': limit_price,
        'bidPrice': [limit_price],
        'bidVol': [5000000],  # 500万股封单
        'time': 93000000000  # 9:30
    }
    flow_calc.on_l2quote(stock_code, quote_data)
    seal_info = seal_calc.get_seal_info(stock_code)
    print(f"  封板金额: {seal_info.seal_amount_wan:.2f}万元")
    
    # 场景2: 板上大单买入
    print("\n场景2: 板上大单买入")
    order_data = {
        'entrustNo': 3001,
        'entrustDirection': 1,
        'volume': 200000,  # 20万股 - 大单
        'price': limit_price,
        'time': 93100000000
    }
    flow_calc.on_l2order(stock_code, order_data)
    
    trans_data = {
        'buyNo': 3001,
        'sellNo': 4001,
        'volume': 200000,
        'amount': 200000 * limit_price,
        'price': limit_price,
        'tradeFlag': 1,
        'time': 93100000000
    }
    flow_calc.on_l2transaction(stock_code, trans_data)
    
    # 检查板上资金流向
    limit_up_stats = flow_calc.get_limit_up_stats(stock_code)
    seal_info = seal_calc.get_seal_info(stock_code)
    
    # Debug信息
    print(f"  调试信息:")
    print(f"    涨停状态: {seal_info.is_limit_up if seal_info else 'N/A'}")
    print(f"    委托簿中是否有3001: {3001 in flow_calc.order_book.get(stock_code, {})}")
    
    if limit_up_stats:
        print(f"  板上超大单买入: {limit_up_stats.super_large_buy/10000:.2f}万元")
        print(f"  板上大单买入: {limit_up_stats.large_buy/10000:.2f}万元")
        # 200,000股 × 22元 = 440万元，符合超大单标准(≥100万元)
        if limit_up_stats.super_large_buy > 0 or limit_up_stats.large_buy > 0:
            print("✅ 板上资金流向统计正确")
        else:
            print("❌ 板上资金流向统计错误")
    else:
        print("❌ 没有板上资金流向统计数据")
    
    # 场景3: 炸板
    print("\n场景3: 炸板")
    quote_data['lastPrice'] = limit_price - 0.05
    flow_calc.on_l2quote(stock_code, quote_data)
    seal_info = seal_calc.get_seal_info(stock_code)
    print(f"  是否涨停: {seal_info.is_limit_up}")
    
    print()


def test_shanghai_shenzhen_difference():
    """测试沪深差异处理"""
    print("=" * 70)
    print("测试5: 沪深差异处理")
    print("=" * 70)
    
    calc = CapitalFlowCalculator()
    
    # 上交所撤单 (entrustDirection=3)
    print("上交所撤单测试:")
    sh_cancel_order = {
        'entrustNo': 5001,
        'entrustDirection': 3,  # 撤买
        'volume': 100000,
        'price': 10.0,
        'time': 1234567890000
    }
    
    calc.on_l2order('600000.SH', sh_cancel_order)
    # 撤单应该不会被记录在委托簿中
    if 5001 not in calc.order_book.get('600000.SH', {}):
        print("  ✅ 上交所撤单处理正确")
    else:
        print("  ❌ 上交所撤单处理错误")
    
    # 深交所撤单 (tradeFlag=3)
    print("\n深交所撤单测试:")
    sz_cancel_trans = {
        'buyNo': 6001,
        'sellNo': 7001,
        'volume': 100000,
        'amount': 1000000,
        'price': 10.0,
        'tradeFlag': 3,  # 撤单
        'time': 1234567890000
    }
    
    calc.on_l2transaction('000001.SZ', sz_cancel_trans)
    # 撤单应该不会累计资金流向
    stats = calc.get_stats('000001.SZ')
    if stats is None or stats.net_main == 0:
        print("  ✅ 深交所撤单处理正确")
    else:
        print("  ❌ 深交所撤单处理错误")
    
    print()


def test_calculator_performance():
    """测试计算器性能 - 确保处理速度满足实时要求"""
    print("=" * 70)
    print("测试6: 计算器性能测试")
    print("=" * 70)
    
    # 统一计算器（封板金额 + 全量资金流向 + 板上资金流向）
    # - SealAmountCalculator: 仅负责“封板金额/涨停状态”
    # - Level2Calculator: 处理所有 Level2 数据（quote/order/transaction），并可复用 seal_calc
    from level2.calculators.l2_calculators import SealAmountCalculator, Level2Calculator
    
    # 创建计算器
    seal_calc = SealAmountCalculator()
    flow_calc = Level2Calculator(seal_calculator=seal_calc, enable_limit_up_flow=True)
    
    # 设置100只股票
    stock_list = [f"{i:06d}.SZ" for i in range(1, 101)]
    for stock in stock_list:
        seal_calc.set_stock_info(stock, last_close=20.0)
    
    # 准备测试数据
    test_order = {
        'entrustNo': 1001,
        'entrustDirection': 1,
        'volume': 200000,
        'price': 22.0,
        'time': 93000000000
    }
    
    test_trans = {
        'buyNo': 1001,
        'sellNo': 2001,
        'volume': 200000,
        'amount': 4400000.0,
        'price': 22.0,
        'tradeFlag': 1,
        'time': 93000000000
    }
    
    test_quote = {
        'lastPrice': 22.0,
        'bidPrice': [22.0],
        'bidVol': [1000000],
        'time': 93000000000
    }
    
    # 性能测试1: l2order处理
    count = 10000
    start = time.time()
    for i in range(count):
        stock = stock_list[i % len(stock_list)]
        test_order['entrustNo'] = 1001 + i
        flow_calc.on_l2order(stock, test_order)
    order_time = time.time() - start
    
    print(f"\n1. l2order处理性能:")
    print(f"   处理 {count} 条委托")
    print(f"   总耗时: {order_time*1000:.2f}ms")
    print(f"   平均: {order_time/count*1000000:.2f}μs/条")
    print(f"   吞吐: {count/order_time:.0f} 条/秒")
    
    if order_time/count*1000000 < 100:  # 目标<100μs
        print("   ✅ 性能达标 (<100μs)")
    else:
        print(f"   ⚠️  性能警告 (>{order_time/count*1000000:.2f}μs)")
    
    # 性能测试2: l2transaction处理
    start = time.time()
    for i in range(count):
        stock = stock_list[i % len(stock_list)]
        test_trans['buyNo'] = 1001 + i
        flow_calc.on_l2transaction(stock, test_trans)
    trans_time = time.time() - start
    
    print(f"\n2. l2transaction处理性能:")
    print(f"   处理 {count} 条成交")
    print(f"   总耗时: {trans_time*1000:.2f}ms")
    print(f"   平均: {trans_time/count*1000000:.2f}μs/条")
    print(f"   吞吐: {count/trans_time:.0f} 条/秒")
    
    if trans_time/count*1000000 < 100:
        print("   ✅ 性能达标 (<100μs)")
    else:
        print(f"   ⚠️  性能警告 (>{trans_time/count*1000000:.2f}μs)")
    
    # 性能测试3: l2quote处理
    start = time.time()
    for i in range(count):
        stock = stock_list[i % len(stock_list)]
        flow_calc.on_l2quote(stock, test_quote)
    quote_time = time.time() - start
    
    print(f"\n3. l2quote处理性能:")
    print(f"   处理 {count} 条快照")
    print(f"   总耗时: {quote_time*1000:.2f}ms")
    print(f"   平均: {quote_time/count*1000000:.2f}μs/条")
    print(f"   吞吐: {count/quote_time:.0f} 条/秒")
    
    if quote_time/count*1000000 < 100:
        print("   ✅ 性能达标 (<100μs)")
    else:
        print(f"   ⚠️  性能警告 (>{quote_time/count*1000000:.2f}μs)")
    
    # 综合吞吐量测试
    total_time = order_time + trans_time + quote_time
    total_count = count * 3
    avg_throughput = total_count / total_time
    
    print(f"\n4. 综合性能:")
    print(f"   总处理数据: {total_count} 条")
    print(f"   总耗时: {total_time*1000:.2f}ms")
    print(f"   综合吞吐: {avg_throughput:.0f} 条/秒")
    
    # 峰值场景：9:25集合竞价
    peak_requirement = 100000  # 10万笔/秒
    print(f"\n5. 峰值场景评估 (9:25集合竞价):")
    print(f"   要求吞吐: {peak_requirement:,} 条/秒")
    print(f"   当前吞吐: {avg_throughput:,.0f} 条/秒")
    print(f"   理论可处理: {avg_throughput/peak_requirement*100:.1f}%")
    
    if avg_throughput >= peak_requirement:
        print("   ✅ 可满足峰值要求")
    else:
        print(f"   ⚠️  建议增加消费者进程数至 {int(peak_requirement/avg_throughput * 8) + 1} 个")
    
    # 内存使用统计
    print(f"\n6. 内存使用:")
    print(f"   委托簿条目: {sum(len(orders) for orders in flow_calc.order_book.values())}")
    print(f"   虚拟委托: {sum(len(orders) for orders in flow_calc.virtual_orders.values())}")
    print(f"   资金流向统计: {len(flow_calc.flow_stats)} 只股票")
    print(f"   板上流向统计: {len(flow_calc.limit_up_flow)} 只股票")
    
    print()


def run_all_tests():
    """运行所有测试"""
    print("\n")
    print("╔" + "═" * 68 + "╗")
    print("║" + " " * 12 + "Level 2 系统性能测试和验证 v2.0" + " " * 12 + "║")
    print("╚" + "═" * 68 + "╝")
    print()
    
    try:
        # 性能优化测试
        test_buffer_performance()
        test_batch_write_performance()
        test_fast_write_performance()
        
        # 功能测试
        test_capital_flow_calculator()
        test_seal_amount_calculator()
        test_integrated_scenario()
        test_shanghai_shenzhen_difference()
        test_calculator_performance()
        
        print("=" * 70)
        print("所有测试完成!")
        print("=" * 70)
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    run_all_tests()