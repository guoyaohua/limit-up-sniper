"""
测试板块监控功能（基于Tick数据）
"""

import sys
import os
import json
import time
from datetime import datetime
from multiprocessing import Manager
from loguru import logger

# 添加项目路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from standalone.sector_monitor_tick_based import (
    monitor_sectors_by_tick,
    load_sector_mapping,
    calculate_sector_metrics
)
from infra.common_enums import StockLimitStatusInt
from xtquant import xtdata

# 配置日志
logger.add("test_sector_monitor.log", rotation="1 MB")


def test_load_mappings():
    """测试加载板块映射文件"""
    logger.info("=" * 50)
    logger.info("测试1: 加载板块映射文件")
    
    # 测试概念板块映射
    concept_mapping = load_sector_mapping('output/concept_sectors/sector_to_stocks_mapping_latest.json')
    if concept_mapping:
        logger.success(f"✅ 成功加载概念板块映射: {len(concept_mapping)} 个板块")
        # 显示前3个板块
        for i, (code, info) in enumerate(list(concept_mapping.items())[:3]):
            logger.info(f"  板块 {code}: {info['name']}, 成分股数: {len(info['stocks'])}")
    else:
        logger.error("❌ 无法加载概念板块映射")
    
    # 测试行业板块映射
    industry_mapping = load_sector_mapping('output/industry_sectors/sector_to_stocks_mapping_latest.json')
    if industry_mapping:
        logger.success(f"✅ 成功加载行业板块映射: {len(industry_mapping)} 个板块")
        # 显示前3个板块
        for i, (code, info) in enumerate(list(industry_mapping.items())[:3]):
            logger.info(f"  板块 {code}: {info['name']}, 成分股数: {len(info['stocks'])}")
    else:
        logger.error("❌ 无法加载行业板块映射")
    
    return concept_mapping, industry_mapping


def test_tick_data_fetch():
    """测试获取Tick数据"""
    logger.info("=" * 50)
    logger.info("测试2: 获取Tick数据")
    
    # 连接xtdata
    try:
        xtdata.connect(ip='127.0.0.1', port=58610)
        logger.success("✅ 成功连接XTQuant")
    except Exception as e:
        logger.error(f"❌ 连接XTQuant失败: {e}")
        return None
    
    # 测试股票列表
    test_stocks = ['000001.SZ', '000002.SZ', '600000.SH', '600036.SH', '300001.SZ']
    
    logger.info(f"获取 {len(test_stocks)} 只股票的tick数据...")
    tick_data = xtdata.get_full_tick(test_stocks)
    
    if tick_data:
        logger.success(f"✅ 成功获取tick数据: {len(tick_data)} 只股票")
        
        # 转换为DataFrame并显示
        import pandas as pd
        df = pd.DataFrame(tick_data).T.reset_index(names='股票代码')
        df['涨跌幅'] = (df['lastPrice'] - df['lastClose']) / df['lastClose'] * 100
        
        logger.info("\n最新Tick数据示例:")
        for idx, row in df.head(3).iterrows():
            logger.info(f"  {row['股票代码']}: 最新价={row['lastPrice']:.2f}, "
                       f"涨跌幅={row['涨跌幅']:.2f}%, "
                       f"成交量={row.get('volume', 0)}")
        
        return df
    else:
        logger.error("❌ 无法获取tick数据")
        return None


def test_sector_calculation():
    """测试板块指标计算"""
    logger.info("=" * 50)
    logger.info("测试3: 板块指标计算")
    
    # 创建模拟的共享数据
    manager = Manager()
    shared_data = {
        '涨停池': manager.dict(),
        '股票状态信号': manager.dict(),
        '股票信息': manager.dict(),
    }
    
    # 模拟一些涨停股票
    test_limit_stocks = {
        '000001.SZ': {'首次涨停时间': '09:30:00'},
        '600000.SH': {'首次涨停时间': '09:35:00'},
    }
    shared_data['涨停池'].update(test_limit_stocks)
    
    # 模拟股票状态
    for stock in test_limit_stocks:
        shared_data['股票状态信号'][stock] = {
            '股票状态': manager.Value('i', StockLimitStatusInt.LIMIT_UP)
        }
    
    # 获取真实的tick数据
    tick_df = test_tick_data_fetch()
    if tick_df is None:
        logger.warning("跳过板块计算测试（无tick数据）")
        return
    
    # 测试计算一个板块的指标
    test_sector_stocks = ['000001', '000002', '600000', '600036']  # 不含后缀
    
    metrics = calculate_sector_metrics(
        tick_df,
        test_sector_stocks,
        shared_data['涨停池'],
        shared_data
    )
    
    if metrics:
        logger.success("✅ 成功计算板块指标:")
        logger.info(f"  平均涨跌幅: {metrics['平均涨跌幅']:.2f}%")
        logger.info(f"  上涨家数: {metrics['上涨家数']}")
        logger.info(f"  下跌家数: {metrics['下跌家数']}")
        logger.info(f"  涨停家数: {metrics['涨停家数']}")
        logger.info(f"  领涨股票: {metrics['领涨股票代码']} ({metrics['领涨股票涨跌幅']:.2f}%)")
    else:
        logger.error("❌ 板块指标计算失败")


def test_monitor_integration():
    """测试完整的监控流程"""
    logger.info("=" * 50)
    logger.info("测试4: 完整监控流程")
    
    # 创建完整的共享数据结构
    manager = Manager()
    shared_data = {
        '概念板块': manager.dict(),
        '概念板块成分股': manager.dict(),
        '概念板块效应': manager.dict(),
        '行业板块': manager.dict(), 
        '行业板块成分股': manager.dict(),
        '行业板块效应': manager.dict(),
        '涨停池': manager.dict(),
        '股票状态信号': manager.dict(),
        '股票信息': manager.dict(),
    }
    
    # 加载映射数据
    concept_mapping, industry_mapping = test_load_mappings()
    
    # 构建反向映射（股票到板块的映射）
    if concept_mapping:
        stock_to_concepts = {}
        concept_stocks = {}
        for sector_code, sector_info in concept_mapping.items():
            concept_stocks[sector_code] = sector_info['stocks']
            for stock in sector_info['stocks']:
                if stock not in stock_to_concepts:
                    stock_to_concepts[stock] = []
                stock_to_concepts[stock].append(sector_code)
        
        shared_data['概念板块'].update(stock_to_concepts)
        shared_data['概念板块成分股'].update(concept_stocks)
        logger.info(f"✅ 构建概念板块反向映射: {len(stock_to_concepts)} 只股票")
    
    if industry_mapping:
        stock_to_industries = {}
        industry_stocks = {}
        for sector_code, sector_info in industry_mapping.items():
            industry_stocks[sector_code] = sector_info['stocks']
            for stock in sector_info['stocks']:
                if stock not in stock_to_industries:
                    stock_to_industries[stock] = []
                stock_to_industries[stock].append(sector_code)
        
        shared_data['行业板块'].update(stock_to_industries)
        shared_data['行业板块成分股'].update(industry_stocks)
        logger.info(f"✅ 构建行业板块反向映射: {len(stock_to_industries)} 只股票")
    
    # 执行监控
    try:
        logger.info("\n开始监控概念板块...")
        concept_result = monitor_sectors_by_tick(shared_data, 'concept')
        if concept_result is not None and not concept_result.empty:
            logger.success(f"✅ 概念板块监控成功: {len(concept_result)} 个板块")
            logger.info("\n强势概念板块 Top 5:")
            for idx, row in concept_result.head(5).iterrows():
                logger.info(f"  {row['板块名称']}: 涨幅={row['涨跌幅']:.2f}%, "
                           f"上涨={row['上涨家数']}家, 涨停={row['涨停家数']}家")
        
        logger.info("\n开始监控行业板块...")
        industry_result = monitor_sectors_by_tick(shared_data, 'industry')
        if industry_result is not None and not industry_result.empty:
            logger.success(f"✅ 行业板块监控成功: {len(industry_result)} 个板块")
            logger.info("\n强势行业板块 Top 5:")
            for idx, row in industry_result.head(5).iterrows():
                logger.info(f"  {row['板块名称']}: 涨幅={row['涨跌幅']:.2f}%, "
                           f"上涨={row['上涨家数']}家, 涨停={row['涨停家数']}家")
        
        # 检查板块效应
        if shared_data['概念板块效应']:
            logger.info(f"\n✅ 概念板块效应: {len(shared_data['概念板块效应'])} 只股票受影响")
        
        if shared_data['行业板块效应']:
            logger.info(f"✅ 行业板块效应: {len(shared_data['行业板块效应'])} 只股票受影响")
            
    except Exception as e:
        logger.exception(f"❌ 监控过程出错: {e}")


def main():
    """主测试函数"""
    logger.info("开始测试板块监控功能（基于Tick数据）")
    logger.info(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # 检查交易时间
    from datetime import time as dt_time
    now = datetime.now()
    if not (dt_time(9, 15) <= now.time() <= dt_time(15, 0)):
        logger.warning("⚠️ 当前不在交易时间，部分测试可能无法获取实时数据")
    
    # 执行测试
    test_load_mappings()
    test_tick_data_fetch()
    test_sector_calculation()
    test_monitor_integration()
    
    logger.info("\n" + "=" * 50)
    logger.info("✅ 所有测试完成！")
    logger.info("说明:")
    logger.info("1. 新的板块监控基于tick数据实时计算，不再依赖网页爬虫")
    logger.info("2. 支持识别涨停股票并选择第一个涨停的作为领涨股")
    logger.info("3. 每10秒更新一次板块数据")
    logger.info("4. 保持与原有数据结构的兼容性")


if __name__ == "__main__":
    main()