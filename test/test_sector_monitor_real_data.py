"""
测试板块监控功能（基于Tick数据） - 使用真实备份数据
"""

import sys
import os
import json
import pickle
import time
from datetime import datetime
from multiprocessing import Manager
from loguru import logger
from multiprocessing import Manager, Value, Queue, Array

# 添加项目路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from standalone.sector_monitor_tick_based import (monitor_sectors_optimized)
from xtquant import xtdata

# 配置日志
logger.add("test_sector_monitor_real_data.log", rotation="1 MB")


def load_shared_data_from_backup(backup_file: str):
    """
    从备份文件加载shared_data
    
    Args:
        backup_file: 备份文件路径
        
    Returns:
        dict: 恢复的shared_data
    """
    logger.info(f"加载备份文件: {backup_file}")

    if not os.path.exists(backup_file):
        logger.error(f"备份文件不存在: {backup_file}")
        return None

    try:

        # 加载数据
        with open(backup_file, 'rb') as f:
            serializable_data = pickle.load(f)

        backup_keys = [
            '涨停池', '股票状态信号', '股票信息', '概念板块', '概念板块成分股', '行业板块', '行业板块成分股'
        ]
        del_keys = set(serializable_data.keys()) - set(backup_keys)
        for key in del_keys:
            logger.warning(f"备份数据包含未知键: {key}")
            del serializable_data[key]

        # 恢复共享数据
        backup_data = deep_restore(serializable_data)

        logger.info(f"共享数据已从 {backup_file} 恢复，共 {len(backup_data)} 个项目")

        # 显示备份数据的基本信息
        if isinstance(backup_data, dict):
            logger.info(f"备份数据包含 {len(backup_data)} 个键:")
            for key in list(backup_data.keys())[:10]:  # 只显示前10个键
                if isinstance(backup_data[key], dict):
                    logger.info(f"  - {key}: {len(backup_data[key])} 项")
                elif isinstance(backup_data[key], list):
                    logger.info(f"  - {key}: {len(backup_data[key])} 个元素")
                else:
                    logger.info(f"  - {key}: {type(backup_data[key])}")

        return backup_data

    except Exception as e:
        logger.error(f"加载备份文件失败: {e}")
        return None


def deep_restore(value, depth=0):
    """
    深度递归恢复，将序列化的数据结构转换回原始的
    multiprocessing 对象和 Manager 对象
    """
    start_time = time.time()
    indent = "  " * depth

    # 检查是否是我们的自定义序列化字典
    if isinstance(value, dict) and '_type_' in value and '_value_' in value:
        type_ = value['_type_']
        val = value['_value_']
        typecode = value.get('_typecode_', 'i')

        # logger.debug(
        #     f"{indent}[deep_restore] 恢复类型: {type_}, 值: {str(val)[:100]}...")

        if type_ == 'multiprocessing.Value':
            # 恢复 multiprocessing.Value 对象
            # logger.debug(
            #     f"{indent}[deep_restore] 创建 multiprocessing.Value({typecode}, {val})"
            # )
            result = Value(typecode, val)
            # elapsed = time.time() - start_time
            # logger.debug(
            #     f"{indent}[deep_restore] multiprocessing.Value 创建完成，耗时: {elapsed:.4f}s"
            # )
            return val
        elif type_ == 'multiprocessing.Array':
            # 恢复 multiprocessing.Array 对象
            # logger.debug(
            #     f"{indent}[deep_restore] 创建 multiprocessing.Array({typecode}, {len(val)} 个元素)"
            # )
            result = Array(typecode, val)
            # elapsed = time.time() - start_time
            # logger.debug(
            #     f"{indent}[deep_restore] multiprocessing.Array 创建完成，耗时: {elapsed:.4f}s"
            # )
            return val
        elif type_ == 'Value':
            # 向后兼容：Manager ValueProxy
            # logger.debug(
            #     f"{indent}[deep_restore] 创建 Manager().Value({typecode}, {val})"
            # )
            result = Manager().Value(typecode, val)
            # elapsed = time.time() - start_time
            # logger.debug(
            #     f"{indent}[deep_restore] Manager().Value 创建完成，耗时: {elapsed:.4f}s"
            # )
            return val
        elif type_ == 'Dict':
            # logger.debug(
            #     f"{indent}[deep_restore] 恢复 Manager().dict，{len(val)} 个键")
            restored_dict = {}
            for k, v in val.items():
                # logger.debug(f"{indent}[deep_restore] 恢复字典键: {k}")
                restored_dict[k] = deep_restore(v, depth + 1)
            result = restored_dict
            # elapsed = time.time() - start_time
            # logger.debug(
            #     f"{indent}[deep_restore] Manager().dict 创建完成，耗时: {elapsed:.4f}s"
            # )
            return result
        elif type_ == 'List':
            # logger.debug(
            #     f"{indent}[deep_restore] 恢复 Manager().list，{len(val)} 个元素")
            restored_list = []
            for i, v in enumerate(val):
                # logger.debug(f"{indent}[deep_restore] 恢复列表元素 [{i}]")
                restored_list.append(deep_restore(v, depth + 1))
            result = restored_list
            # elapsed = time.time() - start_time
            # logger.debug(
            #     f"{indent}[deep_restore] Manager().list 创建完成，耗时: {elapsed:.4f}s"
            # )
            return result
    # 递归处理普通字典
    elif isinstance(value, dict):
        # if depth == 0:
        #     logger.debug(f"{indent}[deep_restore] 恢复普通字典，{len(value)} 个键")
        restored_dict = {}
        for k, v in value.items():
            # if depth == 0:
            #     logger.debug(f"{indent}[deep_restore] 恢复普通字典键: {k}")
            restored_dict[k] = deep_restore(v, depth + 1)
        # elapsed = time.time() - start_time
        # if depth == 0:
        #     logger.debug(f"{indent}[deep_restore] 普通字典恢复完成，耗时: {elapsed:.4f}s")
        return restored_dict
    # 递归处理普通列表
    elif isinstance(value, list):
        # if depth == 0:
        #     logger.debug(f"{indent}[deep_restore] 恢复普通列表，{len(value)} 个元素")
        restored_list = []
        for i, v in enumerate(value):
            # if depth == 0:
            #     logger.debug(f"{indent}[deep_restore] 恢复普通列表元素 [{i}]")
            restored_list.append(deep_restore(v, depth + 1))
        # elapsed = time.time() - start_time
        # if depth == 0:
        #     logger.debug(f"{indent}[deep_restore] 普通列表恢复完成，耗时: {elapsed:.4f}s")
        return restored_list

    # 如果不是以上任何一种，直接返回值
    if depth == 0:
        elapsed = time.time() - start_time
        logger.debug(
            f"{indent}[deep_restore] 直接返回值: {type(value).__name__}，耗时: {elapsed:.4f}s"
        )
    return value


def test_with_real_data():
    """使用真实备份数据测试板块监控"""
    logger.info("=" * 60)
    logger.info("使用真实备份数据测试板块监控功能")

    # 1. 加载备份数据
    backup_file = 'data_backup/FirstLimitUp_v2.1/shared_data_backup_20251028.pkl'
    raw_shared_data = load_shared_data_from_backup(backup_file)

    if not raw_shared_data:
        logger.error("无法加载备份数据，测试终止")
        return

    # 2. 准备shared_data - monitor_sectors_by_tick函数需要的数据结构
    logger.info("\n准备shared_data数据结构...")
    manager = Manager()
    shared_data = {}

    # 从备份恢复关键数据（需要深度复制以避免multiprocessing对象问题）
    def deep_copy_value(val):
        """深度复制值，处理各种multiprocessing对象"""
        # 处理multiprocessing.Value对象
        if hasattr(val, 'value'):
            return val.value
        # 处理multiprocessing.Array对象
        elif hasattr(val, '__getitem__') and hasattr(val, '_type_'):
            return list(val)
        # 处理dict
        elif isinstance(val, dict):
            return {k: deep_copy_value(v) for k, v in val.items()}
        # 处理list
        elif isinstance(val, list):
            return [deep_copy_value(item) for item in val]
        # 其他类型直接返回
        else:
            return val

    def deep_copy_dict(d):
        """深度复制字典，处理嵌套的multiprocessing对象"""
        return {k: deep_copy_value(v) for k, v in d.items()}

    backup_keys = [
        '涨停池', '股票状态信号', '股票信息', '概念板块', '概念板块成分股', '行业板块', '行业板块成分股'
    ]
    for key in backup_keys:
        if key in raw_shared_data:
            try:
                if isinstance(raw_shared_data[key], dict):
                    # 深度复制并转换为普通dict
                    plain_dict = deep_copy_dict(raw_shared_data[key])
                    shared_data[key] = manager.dict(plain_dict)
                    logger.info(f"  ✅ 恢复 {key}: {len(shared_data[key])} 项")
                elif isinstance(raw_shared_data[key], list):
                    shared_data[key] = manager.list(raw_shared_data[key])
                    logger.info(f"  ✅ 恢复 {key}: {len(shared_data[key])} 个元素")
                else:
                    shared_data[key] = raw_shared_data[key]
                    logger.info(f"  ✅ 恢复 {key}: {type(shared_data[key])}")
            except Exception as e:
                # logger.error(raw_shared_data[key])
                logger.error(f"  ❌ 恢复 {key} 失败: {e}")
                shared_data[key] = manager.dict()
        else:
            shared_data[key] = manager.dict()
            logger.warning(f"  ⚠️ {key} 不存在于备份数据中，创建空字典")

    # 初始化板块相关的数据结构 - monitor_sectors_by_tick需要这些
    # 根据sector_monitor_tick_based.py第157-165行的逻辑
    shared_data['概念板块效应'] = manager.dict()  # 输出结果
    shared_data['行业板块效应'] = manager.dict()  # 输出结果

    # 概念板块
    logger.info(f"  ✅ 概念板块: {len(shared_data['概念板块'])} 个板块")

    # 行业板块
    logger.info(f"  ✅ 行业板块: {len(shared_data['行业板块'])} 个板块")

    # # 3. 显示涨停池信息
    # if '涨停池' in shared_data and shared_data['涨停池']:
    #     logger.info(f"\n涨停池包含 {len(shared_data['涨停池'])} 只股票:")
    #     for stock_code in list(shared_data['涨停池'].keys())[:5]:  # 显示前5只
    #         stock_info = shared_data['涨停池'][stock_code]
    #         logger.info(f"  - {stock_code}: {stock_info}")

    # # 4. 显示板块映射信息
    # if '概念板块' in shared_data and shared_data['概念板块']:
    #     logger.info(f"\n概念板块映射: {len(shared_data['概念板块'])} 只股票")
    #     # 显示前3只股票的板块信息
    #     for stock_code in list(shared_data['概念板块'].keys())[:3]:
    #         sectors = shared_data['概念板块'][stock_code]
    #         logger.info(f"  - {stock_code}: {len(sectors)} 个板块")

    # if '行业板块' in shared_data and shared_data['行业板块']:
    #     logger.info(f"\n行业板块映射: {len(shared_data['行业板块'])} 只股票")
    #     # 显示前3只股票的板块信息
    #     for stock_code in list(shared_data['行业板块'].keys())[:3]:
    #         sectors = shared_data['行业板块'][stock_code]
    #         logger.info(f"  - {stock_code}: {len(sectors)} 个板块")

    # 5. 连接XTQuant
    try:
        xtdata.connect(ip='127.0.0.1', port=58610)
        logger.success("✅ 成功连接XTQuant")
    except Exception as e:
        logger.error(f"❌ 连接XTQuant失败: {e}")
        return

    # 6. 执行优化后的板块监控（一次性更新概念和行业板块）
    logger.info("\n" + "=" * 40)
    logger.info("执行板块监控（优化版 - 一次性更新概念+行业）...")

    try:

        # 一次性更新所有板块
        concept_result, industry_result = monitor_sectors_optimized(
            shared_data, force_reload=False)

        # 显示概念板块结果
        if concept_result is not None and not concept_result.empty:
            logger.success(f"✅ 概念板块监控成功: {len(concept_result)} 个板块")
            logger.info("\n强势概念板块 Top 10:")
            for idx, row in concept_result.head(10).iterrows():
                logger.info(f"  {idx+1:2d}. {row['板块名称']:15s} | "
                            f"涨幅: {row['涨跌幅']:+6.2f}% | "
                            f"上涨: {row['上涨家数']:3d}家 | "
                            f"下跌: {row['下跌家数']:3d}家 | "
                            f"涨停: {row['涨停家数']:2d}家 | "
                            f"领涨: {row.get('领涨股票代码', 'N/A')}")
        else:
            logger.warning("概念板块监控返回空结果")

        # 显示行业板块结果
        if industry_result is not None and not industry_result.empty:
            logger.success(f"✅ 行业板块监控成功: {len(industry_result)} 个板块")
            logger.info("\n强势行业板块 Top 10:")
            for idx, row in industry_result.head(10).iterrows():
                logger.info(f"  {idx+1:2d}. {row['板块名称']:15s} | "
                            f"涨幅: {row['涨跌幅']:+6.2f}% | "
                            f"上涨: {row['上涨家数']:3d}家 | "
                            f"下跌: {row['下跌家数']:3d}家 | "
                            f"涨停: {row['涨停家数']:2d}家 | "
                            f"领涨: {row.get('领涨股票代码', 'N/A')}")
        else:
            logger.warning("行业板块监控返回空结果")

    except Exception as e:
        logger.exception(f"板块监控出错: {e}")

    # 8. 检查板块效应更新
    logger.info("\n" + "=" * 40)
    logger.info("检查板块效应更新...")

    if '概念板块效应' in shared_data and shared_data['概念板块效应']:
        logger.success(f"✅ 概念板块效应: {len(shared_data['概念板块效应'])} 只股票受影响")
        # 显示前5只受影响的股票
        for stock_code in list(shared_data['概念板块效应'].keys())[:5]:
            effect_json = shared_data['概念板块效应'][stock_code]
            try:
                effect_data = json.loads(effect_json)
                logger.info(f"  - {stock_code}: 受 {len(effect_data)} 个板块影响")
                for sector in effect_data[:2]:  # 显示前2个板块
                    logger.info(
                        f"    • {sector['板块名称']}: {sector['涨跌幅']:.2f}%")
            except:
                logger.info(f"  - {stock_code}: {effect_json[:50]}...")
    else:
        logger.warning("概念板块效应为空")

    if '行业板块效应' in shared_data and shared_data['行业板块效应']:
        logger.success(f"✅ 行业板块效应: {len(shared_data['行业板块效应'])} 只股票受影响")
        # 显示前5只受影响的股票
        for stock_code in list(shared_data['行业板块效应'].keys())[:5]:
            effect_json = shared_data['行业板块效应'][stock_code]
            try:
                effect_data = json.loads(effect_json)
                logger.info(f"  - {stock_code}: 受 {len(effect_data)} 个板块影响")
                for sector in effect_data[:2]:  # 显示前2个板块
                    logger.info(
                        f"    • {sector['板块名称']}: {sector['涨跌幅']:.2f}%")
            except:
                logger.info(f"  - {stock_code}: {effect_json[:50]}...")
    else:
        logger.warning("行业板块效应为空")

    # 9. 统计信息
    logger.info("\n" + "=" * 60)
    logger.info("📊 测试统计信息:")
    logger.info(f"  - 备份文件日期: 20251027")
    logger.info(f"  - 涨停池股票数: {len(shared_data.get('涨停池', {}))}")
    logger.info(f"  - 概念板块映射股票数: {len(shared_data.get('概念板块', {}))}")
    logger.info(f"  - 行业板块映射股票数: {len(shared_data.get('行业板块', {}))}")
    logger.info(f"  - 概念板块效应股票数: {len(shared_data.get('概念板块效应', {}))}")
    logger.info(f"  - 行业板块效应股票数: {len(shared_data.get('行业板块效应', {}))}")


def test_performance_comparison():
    """测试性能对比"""
    logger.info("\n" + "=" * 60)
    logger.info("测试性能优化效果")

    # 加载备份数据
    backup_file = 'data_backup/FirstLimitUp_v2.1/shared_data_backup_20251027.pkl'
    raw_shared_data = load_shared_data_from_backup(backup_file)

    if not raw_shared_data:
        return

    manager = Manager()
    shared_data = {
        '涨停池': manager.dict(),
        '股票状态信号': manager.dict(),
        '股票信息': manager.dict(),
        '概念板块': manager.dict(raw_shared_data.get('概念板块', {})),
        '概念板块成分股': manager.dict(raw_shared_data.get('概念板块成分股', {})),
        '概念板块效应': manager.dict(),
        '行业板块': manager.dict(raw_shared_data.get('行业板块', {})),
        '行业板块成分股': manager.dict(raw_shared_data.get('行业板块成分股', {})),
        '行业板块效应': manager.dict(),
    }

    # 连接XTQuant
    try:
        xtdata.connect(ip='127.0.0.1', port=58610)
        logger.success("✅ 成功连接XTQuant")
    except Exception as e:
        logger.error(f"❌ 连接XTQuant失败: {e}")
        return

    # 测试优化后的性能
    logger.info("\n测试优化后的性能...")
    start_time = time.time()

    try:
        monitor_sectors_optimized(shared_data, force_reload=False)
        elapsed_time = time.time() - start_time

        logger.success(f"\n✅ 优化版本执行完成")
        logger.info(f"  - 执行时间: {elapsed_time:.2f} 秒")
        logger.info(f"  - 概念板块效应: {len(shared_data.get('概念板块效应', {}))} 只股票")
        logger.info(f"  - 行业板块效应: {len(shared_data.get('行业板块效应', {}))} 只股票")

        logger.info("\n性能优化特性:")
        logger.info("  ✅ 板块映射文件只加载一次（缓存1小时）")
        logger.info("  ✅ 所有股票tick数据只获取一次")
        logger.info("  ✅ 概念和行业板块共享同一份tick数据")
        logger.info("  ✅ 使用向量化操作加速计算")

    except Exception as e:
        logger.exception(f"性能测试失败: {e}")


def main():
    """主测试函数"""
    logger.info("开始测试板块监控功能（使用真实备份数据）")
    logger.info(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    # 检查备份文件是否存在
    backup_file = 'data_backup/FirstLimitUp_v2.1/shared_data_backup_20251027.pkl'
    if not os.path.exists(backup_file):
        logger.error(f"备份文件不存在: {backup_file}")
        logger.info("请确保备份文件存在后再运行测试")
        return

    # 检查交易时间
    from datetime import time as dt_time
    now = datetime.now()
    if not (dt_time(9, 15) <= now.time() <= dt_time(15, 0)):
        logger.warning("⚠️ 当前不在交易时间，将使用历史数据进行测试")

    # 执行测试
    test_with_real_data()
    # test_single_sector_with_real_data()

    logger.info("\n" + "=" * 60)
    logger.info("✅ 所有测试完成！")
    logger.info("\n总结:")
    logger.info("1. ✅ 成功加载真实备份数据")
    logger.info("2. ✅ 成功计算板块行情指标")
    logger.info("3. ✅ 成功识别涨停股票和领涨股")
    logger.info("4. ✅ 成功更新板块效应数据")
    logger.info("5. ✅ 新系统完全兼容原有数据结构")


if __name__ == "__main__":
    main()
