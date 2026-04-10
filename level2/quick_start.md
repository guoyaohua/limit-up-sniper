# Level2 优化版 - 快速开始指南

## 运行测试

由于Python模块导入路径的问题，请使用以下方式运行测试：

### 方法1: 使用提供的测试脚本（推荐）

```bash
cd level2
python run_test.py
```

### 方法2: 从项目根目录运行

```bash
# 从 打板策略 目录运行
python -m level2.test_optimized
```

### 方法3: 使用unittest

```bash
# 从 打板策略 目录运行
python -m unittest level2.test_optimized
```

## 运行基准测试

```bash
cd level2/buffers
python deque_buffer.py
```

这将运行 Deque vs Msgpack 的性能对比测试。

## 运行优化版系统

### 创建启动脚本

创建 `start_optimized.py` 在项目根目录：

```python
import sys
import os

# 确保能找到level2模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from level2.main_optimized import OptimizedLevel2System, OptimizedConfig

def get_stock_list():
    """获取股票列表"""
    # 方法1: 从文件读取
    # with open('stocks.txt', 'r') as f:
    #     return [line.strip() for line in f]
    
    # 方法2: 硬编码测试列表
    stock_list = []
    for i in range(600000, 600100):
        stock_list.append(f"{i}.SH")
    for i in range(1, 100):
        stock_list.append(f"{i:06d}.SZ")
    return stock_list

if __name__ == '__main__':
    stock_list = get_stock_list()
    
    print(f"准备处理 {len(stock_list)} 只股票")
    
    # 创建系统（自动配置）
    system = OptimizedLevel2System(stock_list=stock_list)
    
    # 启动
    try:
        system.start()
    except KeyboardInterrupt:
        print("\n用户中断")
    finally:
        system.stop()
```

然后运行：

```bash
python start_optimized.py
```

## 自定义配置示例

```python
from level2.main_optimized import OptimizedLevel2System, OptimizedConfig

# 自定义配置
config = OptimizedConfig(
    num_partitions=8,        # 8个分区
    num_quote_threads=2,     # 每分区2个quote线程
    num_order_threads=4,     # 每分区4个order线程
    num_trans_threads=2,     # 每分区2个trans线程
    enable_limit_up_flow=True
)

system = OptimizedLevel2System(
    stock_list=stock_list,
    config=config
)
system.start()
```

## 预定义配置

```python
# 小规模（1000只股票）
config = OptimizedConfig.small_scale()

# 中规模（3000只股票）
config = OptimizedConfig.medium_scale()

# 大规模（5000只股票）
config = OptimizedConfig.large_scale()
```

## 常见问题

### Q: ModuleNotFoundError: No module named 'level2'

A: 确保从正确的目录运行，或使用 `python -m` 方式：
```bash
# 从 打板策略 目录
python -m level2.test_optimized
```

### Q: 如何只运行性能基准测试？

A: 运行特定的基准测试函数：
```bash
cd level2
python -c "from buffers.deque_buffer import benchmark_deque_vs_msgpack; benchmark_deque_vs_msgpack()"
```

### Q: 如何查看日志？

A: 系统会在当前目录生成日志文件：
- `level2_optimized_YYYYMMDD.log` - 主进程日志
- `level2_partition_N_YYYYMMDD.log` - 各分区日志

## 下一步

- 阅读完整文档: [`README_OPTIMIZED.md`](README_OPTIMIZED.md)
- 查看优化方案: [`docs/optimization_proposal.md`](docs/optimization_proposal.md)
- 运行性能测试: [`test_optimized.py`](test_optimized.py)