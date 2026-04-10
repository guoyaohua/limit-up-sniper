"""
简单的测试运行脚本
自动处理路径问题
"""

import sys
import os

# 添加项目根目录到路径
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# 现在可以导入并运行测试
if __name__ == '__main__':
    from level2 import test_optimized
    
    print("=" * 70)
    print("运行 Level2 优化版测试")
    print("=" * 70)
    print()
    
    # 运行测试
    import unittest
    
    # 创建测试套件
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(test_optimized)
    
    # 运行测试
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    print("\n" + "=" * 70)
    if result.wasSuccessful():
        print("✓ 所有测试通过!")
    else:
        print("✗ 部分测试失败")
    print("=" * 70)