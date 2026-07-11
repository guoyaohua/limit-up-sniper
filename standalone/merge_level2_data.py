"""
Level 2 数据合并脚本
合并 l2order, l2quote, l2transaction 三种类型的数据
"""
import os
import glob
import pandas as pd
import pickle
from pathlib import Path
from typing import List, Dict
import pyarrow.feather as feather
from datetime import datetime
from tqdm import tqdm


def load_pkl_file(file_path: str) -> pd.DataFrame:
    """
    加载单个pkl文件
    
    Args:
        file_path: pkl文件路径
        
    Returns:
        DataFrame
    """
    try:
        # 使用pandas.read_pickle读取，会自动处理各种数据类型
        data = pd.read_pickle(file_path)
        
        # 如果已经是DataFrame，直接返回
        if isinstance(data, pd.DataFrame):
            return data
        # 如果是其他类型，尝试转换为DataFrame
        else:
            return pd.DataFrame(data)
    except Exception as e:
        print(f"Error loading {file_path}: {e}")
        import traceback
        traceback.print_exc()
        return pd.DataFrame()


def merge_level2_data(base_dir: str, date: str, output_dir: str = None) -> List[str]:
    """
    合并指定日期的Level 2数据，按数据类型分别保存
    
    Args:
        base_dir: Level 2数据根目录 (例如: F:\\level2\\)
        date: 日期字符串 (例如: '20250324')
        output_dir: 输出目录，如果为None则使用base_dir/merged
        
    Returns:
        输出文件路径列表
    """
    base_path = Path(base_dir)
    
    # 三种数据类型
    data_types = ['l2order', 'l2quote', 'l2transaction']
    
    # 准备输出目录
    if output_dir is None:
        output_dir = base_path / "merged"
    else:
        output_dir = Path(output_dir)
    
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"开始处理日期: {date}")
    print(f"数据目录: {base_path}")
    print(f"输出目录: {output_dir}")
    print("=" * 60)
    
    output_files = []
    
    # 分别处理每种数据类型
    for data_type in data_types:
        type_dir = base_path / data_type
        
        print(f"\n处理 {data_type}:")
        print("-" * 60)
        
        if not type_dir.exists():
            print(f"  Warning: 目录不存在: {type_dir}")
            continue
            
        # 查找该日期的所有pkl文件
        pattern = f"*_{date}.pkl"
        pkl_files = list(type_dir.glob(pattern))
        
        print(f"  找到 {len(pkl_files)} 个股票文件")
        
        if not pkl_files:
            print(f"  跳过 {data_type} (无数据)")
            continue
        
        # 存储该类型的所有数据
        type_dataframes = []
        
        # 加载所有文件（带进度条）
        for pkl_file in tqdm(pkl_files, desc=f"  加载{data_type}文件", unit="file"):
            df = load_pkl_file(str(pkl_file))
            
            if not df.empty:
                # 添加股票代码（从文件名提取）
                stock_code = pkl_file.stem.split('_')[0]  # 例如: 603999.SH
                df['stock_code'] = stock_code
                
                type_dataframes.append(df)
        
        if not type_dataframes:
            print(f"  跳过 {data_type} (无有效数据)")
            continue
        
        # 合并该类型的所有DataFrame
        print(f"\n  合并 {len(type_dataframes)} 个股票的数据...")
        merged_df = pd.concat(type_dataframes, ignore_index=True)
        print(f"  合并后总记录数: {len(merged_df):,}")
        
        # 确定时间戳列名
        timestamp_cols = [col for col in merged_df.columns
                         if col.lower() in ['timestamp', 'time', 'datetime', 'trade_time', 'quote_time', 'transact_time']]
        
        if timestamp_cols:
            timestamp_col = timestamp_cols[0]
            print(f"  按时间戳排序 (列名: {timestamp_col})...")
            
            # 直接按时间戳排序，不做任何转换
            merged_df = merged_df.sort_values(timestamp_col).reset_index(drop=True)
            print(f"  排序完成")
            print(f"  时间范围: {merged_df[timestamp_col].iloc[0]} 到 {merged_df[timestamp_col].iloc[-1]}")
        else:
            print(f"  Warning: 未找到时间戳列，数据未排序")
            print(f"  可用列: {list(merged_df.columns)}")
        
        # 输出文件路径
        output_file = output_dir / f"{data_type}_{date}.feather"
        
        # 保存为feather格式
        print(f"\n  保存数据到: {output_file.name}")
        feather.write_feather(merged_df, output_file)
        
        # 输出统计信息
        print(f"  - 总记录数: {len(merged_df):,}")
        print(f"  - 股票数量: {merged_df['stock_code'].nunique()}")
        print(f"  - 文件大小: {output_file.stat().st_size / 1024 / 1024:.2f} MB")
        print(f"  ✓ {data_type} 合并完成!")
        
        output_files.append(str(output_file))
    
    print("\n" + "=" * 60)
    print(f"所有数据类型处理完成! 共生成 {len(output_files)} 个文件")
    print("=" * 60)
    
    return output_files


def verify_feather_files(file_paths: List[str]):
    """
    验证feather文件
    
    Args:
        file_paths: feather文件路径列表
    """
    print("\n验证输出文件...")
    print("=" * 60)
    
    for file_path in file_paths:
        file_name = Path(file_path).name
        print(f"\n检查文件: {file_name}")
        print("-" * 60)
        
        try:
            df = feather.read_feather(file_path)
            print(f"✓ 文件读取成功")
            print(f"  记录数: {len(df):,}")
            print(f"  列数: {len(df.columns)}")
            print(f"  列名: {list(df.columns)}")
            print(f"\n前3行数据:")
            print(df.head(3))
        except Exception as e:
            print(f"✗ 文件读取失败: {e}")


if __name__ == "__main__":
    # 配置参数
    BASE_DIR = os.getenv('LEVEL2_DATA_DIR', os.path.join('output', 'level2'))
    DATE = os.getenv('LEVEL2_DATE', datetime.now().strftime('%Y%m%d'))
    OUTPUT_DIR = os.getenv('LEVEL2_MERGED_DIR')
    
    # 执行合并
    try:
        output_files = merge_level2_data(BASE_DIR, DATE, OUTPUT_DIR)
        
        if output_files:
            # 验证输出文件
            verify_feather_files(output_files)
    except Exception as e:
        print(f"\nError: 执行过程中出现错误: {e}")
        import traceback
        traceback.print_exc()
