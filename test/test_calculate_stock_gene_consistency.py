"""
测试脚本：验证 calculate_stock_gene 优化前后结果一致性

使用方法：
    python test_calculate_stock_gene_consistency.py
"""

import pandas as pd
import numpy as np
import sys

# 导入v2.2和v2.3的计算函数
sys.path.insert(0, '.')

def test_consistency():
    """测试计算一致性"""
    
    print("="*80)
    print("涨停基因计算一致性测试")
    print("="*80)
    
    # 创建测试数据
    print("\n1. 生成测试数据...")
    np.random.seed(42)
    n_days = 300
    
    test_data = pd.DataFrame({
        '股票代码': ['000001.SZ'] * n_days,
        '日期': pd.date_range('2024-01-01', periods=n_days),
        '开盘价': np.random.uniform(10, 20, n_days),
        '最高价': np.random.uniform(15, 25, n_days),
        '最低价': np.random.uniform(8, 15, n_days),
        '收盘价': np.random.uniform(10, 20, n_days),
        '昨收': np.random.uniform(10, 20, n_days),
        '成交量': np.random.uniform(1e6, 1e7, n_days),
        '成交额': np.random.uniform(1e8, 1e9, n_days),
    })
    
    # 添加涨停和炸板标记
    test_data['涨停'] = np.random.choice([True, False], n_days, p=[0.05, 0.95])
    test_data['炸板'] = np.random.choice([True, False], n_days, p=[0.02, 0.98])
    
    # 添加一些NaN值来测试NaN处理
    nan_indices = np.random.choice(n_days, size=int(n_days * 0.1), replace=False)
    test_data.loc[nan_indices, '开盘价'] = np.nan
    
    print(f"   - 测试数据天数: {n_days}")
    print(f"   - 涨停天数: {test_data['涨停'].sum()}")
    print(f"   - 炸板天数: {test_data['炸板'].sum()}")
    print(f"   - NaN值数量: {test_data['开盘价'].isna().sum()}")
    
    # 从v2.2导入原版函数
    print("\n2. 导入v2.2版本计算函数...")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("v22", "打板策略_v2.2.py")
        v22_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(v22_module)
        calculate_stock_gene_v22 = v22_module.calculate_stock_gene
        print("   ✓ v2.2版本加载成功")
    except Exception as e:
        print(f"   ✗ v2.2版本加载失败: {e}")
        return False
    
    # 从v2.3导入优化版函数
    print("\n3. 导入v2.3版本计算函数...")
    try:
        spec = importlib.util.spec_from_file_location("v23", "打板策略_v2.3.py")
        v23_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(v23_module)
        calculate_stock_gene_v23 = v23_module.calculate_stock_gene
        print("   ✓ v2.3版本加载成功")
    except Exception as e:
        print(f"   ✗ v2.3版本加载失败: {e}")
        return False
    
    # 执行计算
    print("\n4. 执行涨停基因计算...")
    print("   - 计算v2.2结果...")
    result_v22 = calculate_stock_gene_v22(test_data.copy(), N=250)
    print("   ✓ v2.2计算完成")
    
    print("   - 计算v2.3结果...")
    result_v23 = calculate_stock_gene_v23(test_data.copy(), N=250)
    print("   ✓ v2.3计算完成")
    
    # 比较关键指标
    print("\n5. 比较计算结果...")
    key_columns = [
        '封板成功率', '首板封板率', '连板率', '涨停次数',
        '涨停次日开盘平均溢价', '涨停次日收盘平均溢价',
        '首板次日开盘平均溢价', '首板次日收盘平均溢价',
        '涨停次日开盘溢价超5%次数', '涨停次日收盘溢价超5%次数',
        '涨停次日收盘红盘率', '涨停次日开盘红盘率',
        '首板次日收盘红盘率', '首板次日开盘红盘率',
        '涨停次日开盘溢价超5%比例', '涨停次日收盘溢价超5%比例'
    ]
    
    all_passed = True
    tolerance = 1e-10  # 浮点数误差容忍度
    
    print(f"\n{'指标名称':<30} {'最大差异':>15} {'平均差异':>15} {'状态':>10}")
    print("-" * 75)
    
    for col in key_columns:
        if col not in result_v22.columns or col not in result_v23.columns:
            print(f"{col:<30} {'N/A':>15} {'N/A':>15} {'缺失':>10}")
            all_passed = False
            continue
        
        # 计算差异（忽略NaN）
        diff = (result_v22[col] - result_v23[col]).abs()
        max_diff = diff.max()
        mean_diff = diff.mean()
        
        # 检查是否通过
        passed = max_diff < tolerance or (pd.isna(max_diff) and result_v22[col].isna().all() and result_v23[col].isna().all())
        
        status = "✓ 通过" if passed else "✗ 失败"
        print(f"{col:<30} {max_diff:>15.10f} {mean_diff:>15.10f} {status:>10}")
        
        if not passed:
            all_passed = False
            # 显示详细差异
            print(f"   详细差异统计:")
            print(f"   - v2.2: min={result_v22[col].min():.6f}, max={result_v22[col].max():.6f}, mean={result_v22[col].mean():.6f}")
            print(f"   - v2.3: min={result_v23[col].min():.6f}, max={result_v23[col].max():.6f}, mean={result_v23[col].mean():.6f}")
            print(f"   - NaN数量: v2.2={result_v22[col].isna().sum()}, v2.3={result_v23[col].isna().sum()}")
    
    # 总结
    print("\n" + "="*80)
    if all_passed:
        print("✓ 所有测试通过！v2.3优化版本与v2.2结果完全一致")
        print("="*80)
        return True
    else:
        print("✗ 部分测试失败！请检查不一致的指标")
        print("="*80)
        return False


if __name__ == '__main__':
    success = test_consistency()
    sys.exit(0 if success else 1)