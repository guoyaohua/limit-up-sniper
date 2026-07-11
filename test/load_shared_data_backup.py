#!/usr/bin/env python3
"""
共享数据备份加载工具

用于加载打板策略保存的共享数据备份文件，
处理multiprocessing Manager对象的反序列化问题
"""

import pickle
import os
import sys
from datetime import datetime
from loguru import logger

from data.serialization import deep_serialize, deep_restore

def load_shared_data_backup(backup_file_path):
    """
    加载共享数据备份文件
    
    Args:
        backup_file_path (str): 备份文件路径
        
    Returns:
        dict: 反序列化后的数据字典
    """
    try:
        logger.info(f"正在加载备份文件: {backup_file_path}")
        
        if not os.path.exists(backup_file_path):
            logger.error(f"错误: 备份文件不存在: {backup_file_path}")
            return None
            
        with open(backup_file_path, 'rb') as f:
            serializable_data = pickle.load(f)

        logger.info("成功加载序列化数据。")
        restored_data = deep_restore(serializable_data)
        final_data = deep_serialize(restored_data)

        logger.info("共享数据恢复完成。")
        return final_data
        
    except Exception as e:
        logger.error(f"加载共享数据失败: {e}")
        import traceback
        traceback.print_exc()
        return None


def print_data_summary(data):
    """
    打印数据摘要信息
    """
    print("\n" + "="*50)
    print("数据摘要:")
    print("="*50)
    
    for key, value in data.items():
        try:
            if isinstance(value, (list, dict)):
                print(f"{key}: {type(value).__name__} 长度={len(value)}")
            else:
                print(f"{key}: {type(value).__name__} = {value}")
        except Exception:
            print(f"{key}: {type(value).__name__}")
    
    print("="*50)


def main():
    """
    主函数 - 命令行使用示例
    """
    if len(sys.argv) < 2:
        print("使用方法: python load_shared_data_backup.py <备份文件路径>")
        print("例如: python load_shared_data_backup.py data_backup/shared_data_backup_20250711.pkl")
        return
    
    backup_file = sys.argv[1]
    
    # 加载数据
    serializable_data = load_shared_data_backup(backup_file)
    
    if serializable_data:
        # 转换为普通字典
        # The loaded data is already a plain dictionary, so we can use it directly.
        normal_dict = serializable_data
        
        # 打印数据摘要
        print_data_summary(normal_dict)
        
        # 保存为JSON文件（可选）
        import json
        output_file = backup_file.replace('.pkl', '_extracted.json')
        try:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(normal_dict, f, ensure_ascii=False, indent=2, default=str)
            print(f"\n数据已提取并保存为: {output_file}")
        except Exception as e:
            print(f"保存JSON文件失败: {e}")
        
        return normal_dict
    else:
        print("数据加载失败")
        return None


if __name__ == "__main__":
    main()
