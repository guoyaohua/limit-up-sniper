"""
基于真实Level 2数据的性能测试 - 内存优化版本

使用level2/l2_data_sample目录中的真实交易数据进行测试。
采用"直接遍历数组"策略，避免预先创建Python对象，解决内存爆炸问题。

核心优化：
1. 数据保持在DataFrame/numpy数组中（内存占用=原始大小）
2. 测试时动态创建临时字典（用完即弃）
3. 使用缓存避免重复加载和排序
"""

import sys
import time
from loguru import logger
import pickle
import gc
from pathlib import Path
from collections import defaultdict
from datetime import datetime
import pandas as pd
import numpy as np
from tqdm import tqdm
import os

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from level2.calculators.l2_calculators import Level2Calculator
from level2.enums import get_limit_price

from infra.utils import init_logger

# 配置日志

# 初始化日志记录器
init_logger(
    os.path.basename(__file__)[:-3],  # 使用脚本文件名作为日志名称
    log_dir=os.path.join(
        os.getenv('LIMIT_UP_LOG_DIR', os.path.join('logs', 'monitor')),
        'Level2',
    ),
    verbose=True)  # 是否在控制台打印日志


class ChunkedRealDataSimulator:
    """分块真实数据模拟器 - 内存优化版本
    
    在开始时加载所有数据块，按时间顺序迭代事件，当某个chunk的所有事件发送完后释放其内存。
    保证事件发送的时间连续性，同时实现内存的渐进式释放。
    """
    def __init__(self,
                 data_dir: str = "l2_data_sample",
                 chunk_duration_minutes: int = 30):
        """
        Args:
            data_dir: 数据目录
            chunk_duration_minutes: 每个chunk的时间长度（分钟）
        """
        self.data_dir = Path(data_dir)
        self.chunk_duration_minutes = chunk_duration_minutes
        self.chunk_duration_ms = chunk_duration_minutes * 60 * 1000

        # 文件路径
        self.order_file = self.data_dir / "l2order_20250324.feather"
        self.trans_file = self.data_dir / "l2transaction_20250324.feather"
        self.quote_file = self.data_dir / "l2quote_20250324.feather"

        # 昨收价字典
        self.stock_last_close = {}

        # 数据统计
        self.total_events = 0
        self.time_range = None

        # 所有chunks的数据（在load_all_chunks时填充）
        self.all_chunks = [
        ]  # [(chunk_start_time, chunk_end_time, chunk_data), ...]
        self.global_sorted_indices = None  # 全局排序的索引，包含chunk_id

    def load_metadata(self):
        """加载元数据（时间范围、股票列表、昨收价等）不加载完整数据"""
        logger.info("=" * 70)
        logger.info(f"加载元数据（分块处理，chunk={self.chunk_duration_minutes}分钟）...")
        logger.info("=" * 70)

        # 读取时间范围（只读time列，非常快）
        times = []

        if self.order_file.exists():
            df = pd.read_feather(self.order_file, columns=['time'])
            times.extend(df['time'].values)
            logger.info(f"  l2order: {len(df):,} 条记录")
            del df

        if self.trans_file.exists():
            df = pd.read_feather(self.trans_file, columns=['time'])
            times.extend(df['time'].values)
            logger.info(f"  l2transaction: {len(df):,} 条记录")
            del df

        if self.quote_file.exists():
            df = pd.read_feather(self.quote_file,
                                 columns=['time', 'stock_code', 'lastClose'])
            times.extend(df['time'].values)
            logger.info(f"  l2quote: {len(df):,} 条记录")

            # 从quote提取昨收价（使用真实的lastClose字段）
            for _, row in df.groupby('stock_code').first().iterrows():
                stock_code = row.name
                last_close = float(row['lastClose'])
                if last_close > 0:
                    self.stock_last_close[stock_code] = last_close

            del df

        gc.collect()

        self.total_events = len(times)
        self.time_range = (min(times), max(times))

        duration_sec = (self.time_range[1] - self.time_range[0]) / 1000
        logger.info(f"\n总事件数: {self.total_events:,}")
        logger.info(
            f"时间范围: {self._format_time(self.time_range[0])} - {self._format_time(self.time_range[1])}"
        )
        logger.info(f"总时长: {duration_sec:.1f} 秒")
        logger.info(
            f"分块数量: ~{int(duration_sec / 60 / self.chunk_duration_minutes)} 个chunk"
        )
        logger.info(f"股票数量: {len(self.stock_last_close)} 只")
        logger.info("=" * 70)

    def load_all_chunks(self):
        """加载所有数据块并创建全局排序索引
        
        优化策略：一次性读取所有文件，然后根据时间段分块，最后释放原始数据。
        """
        if not self.time_range:
            raise RuntimeError("请先调用 load_metadata()")

        logger.info("\n" + "=" * 70)
        logger.info("加载所有数据块（优化版：一次性读取文件）...")
        logger.info("=" * 70)

        # 步骤1: 一次性读取所有文件
        logger.info("\n📥 步骤1: 读取原始数据文件...")

        # 读取order数据
        if self.order_file.exists():
            logger.info(f"  读取 {self.order_file.name}...")
            df_order_full = pd.read_feather(self.order_file)
            logger.info(f"    {len(df_order_full):,} 条记录")
        else:
            df_order_full = pd.DataFrame()

        # 读取transaction数据
        if self.trans_file.exists():
            logger.info(f"  读取 {self.trans_file.name}...")
            df_trans_full = pd.read_feather(self.trans_file)
            logger.info(f"    {len(df_trans_full):,} 条记录")
        else:
            df_trans_full = pd.DataFrame()

        # 读取quote数据
        if self.quote_file.exists():
            logger.info(f"  读取 {self.quote_file.name}...")
            df_quote_full = pd.read_feather(self.quote_file)
            logger.info(f"    {len(df_quote_full):,} 条记录")
        else:
            df_quote_full = pd.DataFrame()

        # 步骤2: 根据时间段分块
        logger.info(
            f"\n📦 步骤2: 根据时间段分块（chunk={self.chunk_duration_minutes}分钟）...")

        start_time, end_time = self.time_range
        current_chunk_start = start_time
        chunk_num = 0

        while current_chunk_start < end_time:
            chunk_num += 1
            current_chunk_end = min(
                current_chunk_start + self.chunk_duration_ms, end_time)

            logger.info(
                f"\n  Chunk #{chunk_num}: {self._format_time(current_chunk_start)} - {self._format_time(current_chunk_end)}"
            )

            # 分块：根据时间范围筛选数据，并转换为numpy数组（关键性能优化）
            chunk = {}

            # 分块order数据并转换为numpy数组
            if len(df_order_full) > 0:
                mask = (df_order_full['time'] >= current_chunk_start) & (
                    df_order_full['time'] < current_chunk_end)
                df_chunk = df_order_full[mask].reset_index(drop=True)
                if len(df_chunk) > 0:
                    # 转换为numpy数组字典（大幅提升访问速度）
                    chunk['order'] = {
                        'stock_code':
                        df_chunk['stock_code'].astype(str).values,
                        'time': df_chunk['time'].values,
                        'price': df_chunk['price'].values,
                        'volume': df_chunk['volume'].values,
                        'entrustNo': df_chunk['entrustNo'].values,
                        'entrustDirection': df_chunk['entrustDirection'].values
                    }
                else:
                    chunk['order'] = {'time': np.array([], dtype=np.int64)}
            else:
                chunk['order'] = {'time': np.array([], dtype=np.int64)}

            # 分块transaction数据并转换为numpy数组
            if len(df_trans_full) > 0:
                mask = (df_trans_full['time'] >= current_chunk_start) & (
                    df_trans_full['time'] < current_chunk_end)
                df_chunk = df_trans_full[mask].reset_index(drop=True)
                if len(df_chunk) > 0:
                    chunk['trans'] = {
                        'stock_code':
                        df_chunk['stock_code'].astype(str).values,
                        'time': df_chunk['time'].values,
                        'price': df_chunk['price'].values,
                        'volume': df_chunk['volume'].values,
                        'amount': df_chunk['amount'].values,
                        'buyNo': df_chunk['buyNo'].values,
                        'sellNo': df_chunk['sellNo'].values,
                        'tradeFlag': df_chunk['tradeFlag'].values
                    }
                else:
                    chunk['trans'] = {'time': np.array([], dtype=np.int64)}
            else:
                chunk['trans'] = {'time': np.array([], dtype=np.int64)}

            # 分块quote数据并转换为numpy数组
            if len(df_quote_full) > 0:
                mask = (df_quote_full['time'] >= current_chunk_start) & (
                    df_quote_full['time'] < current_chunk_end)
                df_chunk = df_quote_full[mask].reset_index(drop=True)
                if len(df_chunk) > 0:
                    chunk['quote'] = {
                        'stock_code':
                        df_chunk['stock_code'].astype(str).values,
                        'time': df_chunk['time'].values,
                        'lastPrice': df_chunk['lastPrice'].values,
                    }
                    # 可选字段
                    for field in ['bidPrice', 'bidVol', 'askPrice', 'askVol']:
                        if field in df_chunk.columns:
                            chunk['quote'][field] = df_chunk[field].values
                else:
                    chunk['quote'] = {'time': np.array([], dtype=np.int64)}
            else:
                chunk['quote'] = {'time': np.array([], dtype=np.int64)}

            # 计算事件数
            chunk_events = len(chunk['order']) + len(chunk['trans']) + len(
                chunk['quote'])
            logger.info(f"    事件数: {chunk_events:,}")

            # 保存chunk
            self.all_chunks.append(
                (current_chunk_start, current_chunk_end, chunk))

            # 移动到下一个chunk
            current_chunk_start = current_chunk_end

        # 步骤3: 释放原始数据，回收内存
        logger.info(f"\n🗑️  步骤3: 释放原始数据，回收内存...")
        del df_order_full
        del df_trans_full
        del df_quote_full
        gc.collect()
        logger.info(f"  ✅ 原始数据已释放")

        logger.info(f"\n✅ 共创建 {len(self.all_chunks)} 个chunks")

        # 步骤4: 创建全局排序索引
        self._create_global_sorted_indices()

        logger.info("=" * 70)

    def _create_global_sorted_indices(self):
        """创建全局排序的事件索引"""
        logger.info("\n创建全局排序索引...")

        indices = []

        for chunk_id, (_, _, chunk_data) in enumerate(self.all_chunks):
            # 添加order索引
            for i, t in enumerate(chunk_data['order']['time']):
                indices.append((t, 'order', chunk_id, i))

            # 添加trans索引
            for i, t in enumerate(chunk_data['trans']['time']):
                indices.append((t, 'trans', chunk_id, i))

            # 添加quote索引
            for i, t in enumerate(chunk_data['quote']['time']):
                indices.append((t, 'quote', chunk_id, i))

        # 按时间排序
        logger.info(f"排序 {len(indices):,} 条索引...")
        sort_start = time.time()
        indices.sort(key=lambda x: x[0])
        sort_time = time.time() - sort_start
        logger.info(f"排序完成，耗时: {sort_time:.2f} 秒")

        self.global_sorted_indices = indices

        if indices:
            first_time = indices[0][0]
            last_time = indices[-1][0]
            duration = (last_time - first_time) / 1000
            logger.info(
                f"时间范围: {self._format_time(first_time)} - {self._format_time(last_time)}"
            )
            logger.info(f"总时长: {duration:.1f} 秒")
            logger.info(f"平均频率: {len(indices)/duration:.0f} 条/秒")

    def iterate_events_chunked(self):
        """按时间顺序迭代所有事件，逐chunk释放内存
        
        Yields:
            (event_type, stock_code, data_dict)
        """
        if not self.global_sorted_indices:
            raise RuntimeError("请先调用 load_all_chunks()")

        # logger.info("\n" + "=" * 70)
        # logger.info("开始按时间顺序迭代事件（逐chunk释放内存）...")
        # logger.info("=" * 70)

        # 跟踪当前chunk_id和已释放的chunks
        last_chunk_id = -1
        chunks_released = set()
        current_chunk_data = None  # 缓存当前chunk数据，避免每次事件都重复访问

        # 迭代所有事件
        for timestamp, data_type, chunk_id, idx in self.global_sorted_indices:
            # 只在chunk_id变化时才处理chunk切换（大幅减少分支判断）
            if chunk_id != last_chunk_id:
                # 释放之前的chunks
                for cid in range(max(0, last_chunk_id + 1), chunk_id):
                    if cid not in chunks_released:
                        # 获取chunk数据用于删除
                        chunk_start, chunk_end, chunk_data = self.all_chunks[
                            cid]

                        # 完全删除chunk数据的所有引用
                        if chunk_data is not None:
                            del chunk_data['order']
                            del chunk_data['trans']
                            del chunk_data['quote']
                            del chunk_data

                        # 将整个chunk设为None释放所有内存
                        self.all_chunks[cid] = None
                        chunks_released.add(cid)
                        logger.info(f"   🗑️  Chunk #{cid + 1} 所有事件已发送，内存已释放")

                # 更新当前chunk缓存（避免每个事件都访问self.all_chunks[chunk_id]）
                last_chunk_id = chunk_id
                chunk_entry = self.all_chunks[chunk_id]
                if chunk_entry is None:
                    logger.error(f"错误: Chunk #{chunk_id + 1} 数据已被释放但仍在使用!")
                    current_chunk_data = None
                else:
                    _, _, current_chunk_data = chunk_entry

            # 使用缓存的chunk数据（避免重复list访问和tuple解包）
            if current_chunk_data is None:
                continue

            chunk_data = current_chunk_data

            # 生成事件数据
            if data_type == 'order':
                stock_code = chunk_data['order']['stock_code'][idx]
                data = {
                    'time':
                    int(chunk_data['order']['time'][idx]),
                    'price':
                    float(chunk_data['order']['price'][idx]),
                    'volume':
                    int(chunk_data['order']['volume'][idx]),
                    'entrustNo':
                    int(chunk_data['order']['entrustNo'][idx]),
                    'entrustDirection':
                    int(chunk_data['order']['entrustDirection'][idx])
                }
                yield ('l2order', stock_code, data)

            elif data_type == 'trans':
                stock_code = chunk_data['trans']['stock_code'][idx]
                data = {
                    'time': int(chunk_data['trans']['time'][idx]),
                    'price': float(chunk_data['trans']['price'][idx]),
                    'volume': int(chunk_data['trans']['volume'][idx]),
                    'amount': float(chunk_data['trans']['amount'][idx]),
                    'buyNo': int(chunk_data['trans']['buyNo'][idx]),
                    'sellNo': int(chunk_data['trans']['sellNo'][idx]),
                    'tradeFlag': int(chunk_data['trans']['tradeFlag'][idx])
                }
                yield ('l2transaction', stock_code, data)

            elif data_type == 'quote':
                stock_code = chunk_data['quote']['stock_code'][idx]
                data = {
                    'time': int(chunk_data['quote']['time'][idx]),
                    'lastPrice': float(chunk_data['quote']['lastPrice'][idx])
                }

                # 可选字段（现在是numpy数组）
                for field in ['bidPrice', 'bidVol', 'askPrice', 'askVol']:
                    if field in chunk_data['quote']:
                        try:
                            val = chunk_data['quote'][field][idx]
                            if not (isinstance(val, float) and np.isnan(val)):
                                data[field] = val.tolist() if hasattr(
                                    val, 'tolist') else val
                        except:
                            pass

                yield ('l2quote', stock_code, data)

        # 迭代结束后，释放最后一个chunk
        if last_chunk_id >= 0 and last_chunk_id not in chunks_released:
            chunk_start, chunk_end, chunk_data = self.all_chunks[last_chunk_id]

            # 完全删除最后一个chunk的数据
            if chunk_data is not None:
                del chunk_data['order']
                del chunk_data['trans']
                del chunk_data['quote']
                del chunk_data

            self.all_chunks[last_chunk_id] = None
            chunks_released.add(last_chunk_id)
            logger.info(f"   🗑️  Chunk #{last_chunk_id + 1} 所有事件已发送，内存已释放")

        # 最终垃圾回收
        gc.collect()
        logger.info(f"\n✅ 所有事件迭代完成，共释放 {len(chunks_released)} 个chunks")

    def _load_chunk(self, start_time, end_time):
        """加载指定时间范围的数据chunk"""
        chunk = {}

        # 加载order数据
        if self.order_file.exists():
            df = pd.read_feather(self.order_file)
            mask = (df['time'] >= start_time) & (df['time'] < end_time)
            chunk['order'] = df[mask].reset_index(drop=True)
            del df
        else:
            chunk['order'] = pd.DataFrame()

        # 加载trans数据
        if self.trans_file.exists():
            df = pd.read_feather(self.trans_file)
            mask = (df['time'] >= start_time) & (df['time'] < end_time)
            chunk['trans'] = df[mask].reset_index(drop=True)
            del df
        else:
            chunk['trans'] = pd.DataFrame()

        # 加载quote数据
        if self.quote_file.exists():
            df = pd.read_feather(self.quote_file)
            mask = (df['time'] >= start_time) & (df['time'] < end_time)
            chunk['quote'] = df[mask].reset_index(drop=True)
            del df
        else:
            chunk['quote'] = pd.DataFrame()

        gc.collect()
        return chunk

    def _create_chunk_indices(self, chunk_data):
        """创建chunk内的排序索引"""
        indices = []

        # 添加order索引
        for i, t in enumerate(chunk_data['order']['time']):
            indices.append((t, 'order', i))

        # 添加trans索引
        for i, t in enumerate(chunk_data['trans']['time']):
            indices.append((t, 'trans', i))

        # 添加quote索引
        for i, t in enumerate(chunk_data['quote']['time']):
            indices.append((t, 'quote', i))

        # 按时间排序
        indices.sort(key=lambda x: x[0])
        return indices

    def _format_time(self, timestamp_ms):
        """格式化时间戳（毫秒）- 使用本地时间"""
        dt = pd.Timestamp(timestamp_ms, unit='ms',
                          tz='UTC').tz_convert('Asia/Shanghai')
        return dt.strftime('%H:%M:%S.%f')[:-3]

    def get_stock_list(self):
        """获取所有股票代码列表"""
        return sorted(list(self.stock_last_close.keys()))

    def get_stock_last_close(self, stock_code: str) -> float:
        """获取股票昨收价"""
        return self.stock_last_close.get(stock_code, 10.0)


class RealDataSimulator:
    """真实数据模拟器 - 零拷贝内存优化版本
    
    不预先创建Python对象，数据保持在numpy数组中，
    测试时动态创建临时字典，内存占用=原始DataFrame大小。
    """
    def __init__(self,
                 data_dir: str = "l2_data_sample",
                 use_cache: bool = False):
        self.data_dir = Path(data_dir)
        self.use_cache = use_cache
        self.cache_file = self.data_dir / "sorted_data_cache.pkl"

        # 数据存储为numpy数组（紧凑）
        self.order_data = None
        self.trans_data = None
        self.quote_data = None

        # 排序索引：[(timestamp, data_type, index), ...]
        self.sorted_indices = None

        # 昨收价字典
        self.stock_last_close = {}

    def load_data(self):
        """加载真实Level 2数据（支持缓存）"""
        logger.info("=" * 70)
        logger.info("加载真实Level 2数据（零拷贝优化版本）...")
        logger.info("=" * 70)

        # 检查缓存
        if self.use_cache and self.cache_file.exists():
            logger.info(f"\n发现缓存文件: {self.cache_file}")
            try:
                cache_start = time.time()
                with open(self.cache_file, 'rb') as f:
                    cache_data = pickle.load(f)
                    self.order_data = cache_data['order_data']
                    self.trans_data = cache_data['trans_data']
                    self.quote_data = cache_data['quote_data']
                    self.sorted_indices = cache_data['sorted_indices']
                    self.stock_last_close = cache_data['stock_last_close']
                cache_time = time.time() - cache_start

                logger.info(f"✅ 从缓存加载成功!")
                logger.info(f"   加载时间: {cache_time:.2f} 秒")
                logger.info(f"   事件数量: {len(self.sorted_indices):,} 条")
                logger.info(f"   股票数量: {len(self.stock_last_close)} 只")

                # 估算内存占用
                mem_mb = self._estimate_memory() / 1024 / 1024
                logger.info(f"   内存占用: ~{mem_mb:.1f} MB")

                logger.info("=" * 70)
                return
            except Exception as e:
                logger.warning(f"⚠️  缓存加载失败: {e}")
                logger.info("重新加载原始数据...")

        # 加载原始数据
        self._load_raw_data()

        # 创建时间排序索引
        self._create_sorted_indices()

        # 保存缓存
        if self.use_cache:
            self._save_cache()

        logger.info("=" * 70)

    def _load_raw_data(self):
        """加载原始feather文件到numpy数组"""
        # 加载l2order数据
        order_file = self.data_dir / "l2order_20250324.feather"
        if order_file.exists():
            logger.info(f"\n加载 {order_file}...")
            df_order = pd.read_feather(order_file)
            logger.info(f"  l2order: {len(df_order):,} 条记录")

            # 转换为numpy数组字典（紧凑存储）
            self.order_data = {
                'stock_code': df_order['stock_code'].astype(str).values,
                'time': df_order['time'].values,
                'price': df_order['price'].values,
                'volume': df_order['volume'].values,
                'entrustNo': df_order['entrustNo'].values,
                'entrustDirection': df_order['entrustDirection'].values
            }
            del df_order
            gc.collect()
        else:
            logger.warning(f"文件不存在: {order_file}")
            self.order_data = {'time': np.array([], dtype=np.int64)}

        # 加载l2transaction数据
        trans_file = self.data_dir / "l2transaction_20250324.feather"
        if trans_file.exists():
            logger.info(f"加载 {trans_file}...")
            df_trans = pd.read_feather(trans_file)
            logger.info(f"  l2transaction: {len(df_trans):,} 条记录")

            self.trans_data = {
                'stock_code': df_trans['stock_code'].astype(str).values,
                'time': df_trans['time'].values,
                'price': df_trans['price'].values,
                'volume': df_trans['volume'].values,
                'amount': df_trans['amount'].values,
                'buyNo': df_trans['buyNo'].values,
                'sellNo': df_trans['sellNo'].values,
                'tradeFlag': df_trans['tradeFlag'].values
            }
            del df_trans
            gc.collect()
        else:
            logger.warning(f"文件不存在: {trans_file}")
            self.trans_data = {'time': np.array([], dtype=np.int64)}

        # 加载l2quote数据
        quote_file = self.data_dir / "l2quote_20250324.feather"
        if quote_file.exists():
            logger.info(f"加载 {quote_file}...")
            df_quote = pd.read_feather(quote_file)
            logger.info(f"  l2quote: {len(df_quote):,} 条记录")

            self.quote_data = {
                'stock_code': df_quote['stock_code'].astype(str).values,
                'time': df_quote['time'].values,
                'lastPrice': df_quote['lastPrice'].values,
            }

            # 提取昨收价
            if 'lastClose' in df_quote.columns:
                for i, stock_code in enumerate(self.quote_data['stock_code']):
                    if pd.notna(df_quote['lastClose'].iloc[i]
                                ) and stock_code not in self.stock_last_close:
                        self.stock_last_close[stock_code] = float(
                            df_quote['lastClose'].iloc[i])

            # 可选字段
            for field in ['bidPrice', 'bidVol', 'askPrice', 'askVol']:
                if field in df_quote.columns:
                    self.quote_data[field] = df_quote[field].values

            del df_quote
            gc.collect()
        else:
            logger.warning(f"文件不存在: {quote_file}")
            self.quote_data = {'time': np.array([], dtype=np.int64)}

    def _create_sorted_indices(self):
        """创建时间排序索引（不创建实际对象）"""
        logger.info(f"\n创建时间排序索引...")

        indices = []

        # 添加order索引
        for i, t in enumerate(self.order_data['time']):
            indices.append((t, 'order', i))

        # 添加transaction索引
        for i, t in enumerate(self.trans_data['time']):
            indices.append((t, 'trans', i))

        # 添加quote索引
        for i, t in enumerate(self.quote_data['time']):
            indices.append((t, 'quote', i))

        # 按时间排序
        logger.info(f"排序 {len(indices):,} 条索引...")
        sort_start = time.time()
        indices.sort(key=lambda x: x[0])
        sort_time = time.time() - sort_start
        logger.info(f"排序完成，耗时: {sort_time:.2f} 秒")

        self.sorted_indices = indices

        if indices:
            first_time = indices[0][0]
            last_time = indices[-1][0]
            duration = (last_time - first_time) / 1000  # 毫秒转秒
            logger.info(
                f"时间范围: {self._format_time(first_time)} - {self._format_time(last_time)}"
            )
            logger.info(f"总时长: {duration:.1f} 秒")
            logger.info(f"平均频率: {len(indices)/duration:.0f} 条/秒")

    def _save_cache(self):
        """保存缓存"""
        logger.info(f"\n保存缓存到: {self.cache_file}")
        try:
            cache_start = time.time()
            cache_data = {
                'order_data': self.order_data,
                'trans_data': self.trans_data,
                'quote_data': self.quote_data,
                'sorted_indices': self.sorted_indices,
                'stock_last_close': self.stock_last_close
            }
            with open(self.cache_file, 'wb') as f:
                pickle.dump(cache_data, f, protocol=pickle.HIGHEST_PROTOCOL)
            cache_time = time.time() - cache_start
            cache_size = self.cache_file.stat().st_size / 1024 / 1024
            logger.info(f"✅ 缓存保存成功!")
            logger.info(f"   保存时间: {cache_time:.2f} 秒")
            logger.info(f"   文件大小: {cache_size:.2f} MB")
        except Exception as e:
            logger.warning(f"⚠️  缓存保存失败: {e}")

    def _estimate_memory(self):
        """估算内存占用（字节）"""
        mem = 0
        for data in [self.order_data, self.trans_data, self.quote_data]:
            if data:
                for arr in data.values():
                    if isinstance(arr, np.ndarray):
                        mem += arr.nbytes
        mem += len(self.sorted_indices) * 32  # 索引元组
        return mem

    def _format_time(self, timestamp_ms):
        """格式化时间戳（毫秒）- 使用本地时间"""
        # 时间戳是毫秒级，转换为本地datetime (Asia/Shanghai)
        dt = pd.Timestamp(timestamp_ms, unit='ms',
                          tz='UTC').tz_convert('Asia/Shanghai')
        return dt.strftime('%H:%M:%S.%f')[:-3]

    def get_event_count(self):
        """获取事件总数"""
        return len(self.sorted_indices) if self.sorted_indices else 0

    def get_stock_list(self):
        """获取所有股票代码列表"""
        stocks = set()
        if self.order_data and len(self.order_data['stock_code']) > 0:
            stocks.update(self.order_data['stock_code'])
        if self.trans_data and len(self.trans_data['stock_code']) > 0:
            stocks.update(self.trans_data['stock_code'])
        if self.quote_data and len(self.quote_data['stock_code']) > 0:
            stocks.update(self.quote_data['stock_code'])
        return sorted(list(stocks))

    def get_stock_last_close(self, stock_code: str) -> float:
        """获取股票昨收价"""
        return self.stock_last_close.get(stock_code, 20.0)

    def iterate_events(self):
        """迭代所有事件（动态创建临时对象）
        
        Yields:
            (event_type, stock_code, data_dict)
        """
        for timestamp, data_type, idx in self.sorted_indices:
            if data_type == 'order':
                stock_code = self.order_data['stock_code'][idx]
                data = {
                    'time':
                    int(self.order_data['time'][idx]),
                    'price':
                    float(self.order_data['price'][idx]),
                    'volume':
                    int(self.order_data['volume'][idx]),
                    'entrustNo':
                    int(self.order_data['entrustNo'][idx]),
                    'entrustDirection':
                    int(self.order_data['entrustDirection'][idx])
                }
                yield ('l2order', stock_code, data)

            elif data_type == 'trans':
                stock_code = self.trans_data['stock_code'][idx]
                data = {
                    'time': int(self.trans_data['time'][idx]),
                    'price': float(self.trans_data['price'][idx]),
                    'volume': int(self.trans_data['volume'][idx]),
                    'amount': float(self.trans_data['amount'][idx]),
                    'buyNo': int(self.trans_data['buyNo'][idx]),
                    'sellNo': int(self.trans_data['sellNo'][idx]),
                    'tradeFlag': int(self.trans_data['tradeFlag'][idx])
                }
                yield ('l2transaction', stock_code, data)

            elif data_type == 'quote':
                stock_code = self.quote_data['stock_code'][idx]
                data = {
                    'time': int(self.quote_data['time'][idx]),
                    'lastPrice': float(self.quote_data['lastPrice'][idx])
                }

                # 可选字段
                for field in ['bidPrice', 'bidVol', 'askPrice', 'askVol']:
                    try:
                        data[field] = self.quote_data[field][idx].tolist()
                    except Exception as e:
                        logger.error(
                            f"处理事件出错: l2quote, 股票: {stock_code}, {field} {idx} 数据: {type(self.quote_data[field][idx])} {self.quote_data[field][idx]}, 错误: {e}"
                        )
                        raise e

                yield ('l2quote', stock_code, data)


def test_real_data_performance():
    """基于真实数据的性能测试 - 只测试 Level2Calculator"""

    print("\n")
    print("╔" + "═" * 68 + "╗")
    print("║" + " " * 8 + "Level 2 真实数据性能测试 (Level2Calculator)" + " " * 5 + "║")
    print("╚" + "═" * 68 + "╝")
    print()

    # 1. 加载真实数据
    simulator = RealDataSimulator()
    simulator.load_data()

    total_events = simulator.get_event_count()
    if total_events == 0:
        logger.error("没有加载到任何数据！")
        return

    # 2. 准备股票信息并初始化 Level2Calculator
    logger.info("\n准备股票信息...")
    stock_list = simulator.get_stock_list()
    logger.info(f"涉及股票数量: {len(stock_list)}")

    # 构建 stock_info 字典：{stock_code: limit_price}
    stock_info = {}
    stocks_with_close = 0
    for stock in stock_list:
        last_close = simulator.get_stock_last_close(stock)
        limit_price = get_limit_price(last_close)
        stock_info[stock] = limit_price
        if stock in simulator.stock_last_close:
            stocks_with_close += 1

    logger.info(f"获取到昨收价的股票: {stocks_with_close}/{len(stock_list)}")

    # 初始化 Level2Calculator（自动创建内置 SealAmountCalculator）
    logger.info("初始化 Level2Calculator...")
    calculator = Level2Calculator(stock_info=stock_info)

    # 3. 性能测试
    logger.info("\n" + "=" * 70)
    logger.info("开始性能测试 - Level2Calculator 统一处理")
    logger.info("=" * 70)

    order_count = 0
    trans_count = 0
    quote_count = 0

    # 性能统计
    process_times = defaultdict(list)
    calculator_times = defaultdict(list)

    start_time = time.time()

    # 使用进度条遍历
    for event_type, stock_code, data in tqdm(simulator.iterate_events(),
                                             total=total_events,
                                             desc="处理事件"):
        event_start = time.time()
        try:
            if event_type == 'l2order':
                calc_start = time.time()
                calculator.on_l2order(stock_code, data)
                calculator_times['l2order'].append(time.time() - calc_start)
                order_count += 1

            elif event_type == 'l2transaction':
                calc_start = time.time()
                calculator.on_l2transaction(stock_code, data)
                calculator_times['l2transaction'].append(time.time() - calc_start)
                trans_count += 1

            elif event_type == 'l2quote':
                calc_start = time.time()
                calculator.on_l2quote(stock_code, data)
                calculator_times['l2quote'].append(time.time() - calc_start)
                quote_count += 1

        except Exception as e:
            logger.error(
                f"处理事件出错: {event_type}, 股票: {stock_code}, 数据: {data}, 错误: {e}")
            raise e

        event_time = time.time() - event_start
        process_times[event_type].append(event_time)

    total_time = time.time() - start_time

    # 4. 输出性能统计
    logger.info("\n" + "=" * 70)
    logger.info("性能测试结果")
    logger.info("=" * 70)

    logger.info(f"\n数据统计:")
    logger.info(f"  l2order:       {order_count:,} 条")
    logger.info(f"  l2transaction: {trans_count:,} 条")
    logger.info(f"  l2quote:       {quote_count:,} 条")
    logger.info(f"  总计:          {total_events:,} 条")

    logger.info(f"\n处理性能:")
    logger.info(f"  总耗时:        {total_time:.2f} 秒")
    logger.info(f"  平均吞吐:      {total_events/total_time:,.0f} 条/秒")

    # 各类型事件的平均处理时间
    logger.info(f"\n各事件类型处理时间:")
    for event_type, times in process_times.items():
        avg_time = sum(times) / len(times) * 1000000
        max_time = max(times) * 1000000
        min_time = min(times) * 1000000
        logger.info(
            f"  {event_type:15s}: 平均 {avg_time:.2f}μs, 最小 {min_time:.2f}μs, 最大 {max_time:.2f}μs"
        )

    # Level2Calculator 性能详情
    logger.info(f"\nLevel2Calculator 各事件处理性能:")
    for event_type, times in calculator_times.items():
        if times:
            avg_time = sum(times) / len(times) * 1000000
            max_time = max(times) * 1000000
            min_time = min(times) * 1000000
            logger.info(
                f"  {event_type:15s}: 平均 {avg_time:.2f}μs, 最小 {min_time:.2f}μs, 最大 {max_time:.2f}μs"
            )

    # 5. 计算结果统计
    logger.info(f"\n" + "=" * 70)
    logger.info("计算结果统计")
    logger.info("=" * 70)

    # 全量资金流向统计
    capital_stats = calculator.get_all_stats()
    logger.info(f"\n全量资金流向统计:")
    logger.info(f"  统计股票数:    {len(capital_stats)}")

    if capital_stats:
        sorted_stocks = sorted(
            capital_stats.items(),
            key=lambda x: x[1].net_large + x[1].net_super_large,
            reverse=True)
        logger.info(f"\n  主力净流入前10:")
        for i, (stock, stats) in enumerate(sorted_stocks[:10], 1):
            net_inflow = (stats.net_large +
                          stats.net_super_large) / 10000
            logger.info(f"    {i:2d}. {stock}: {net_inflow:,.2f}万元")

    # 板上资金流向统计（涨停期间）
    flow_stats = calculator.get_all_limit_up_stats()
    logger.info(f"\n板上资金流向统计 (涨停期间):")
    logger.info(f"  统计股票数:    {len(flow_stats)}")

    top_inflow = calculator.get_top_limit_up_inflow(10)
    if top_inflow:
        logger.info(f"\n  板上主力净流入前10:")
        for i, (stock, net_inflow) in enumerate(top_inflow, 1):
            logger.info(f"    {i:2d}. {stock}: {net_inflow/10000:,.2f}万元")

    # 涨停统计（通过内置的 seal_calc）
    if calculator.seal_calc is not None:
        limit_up_stocks = calculator.seal_calc.get_all_limit_up_stocks()
        logger.info(f"\n涨停股票:")
        logger.info(f"  涨停数量:      {len(limit_up_stocks)}")

        if limit_up_stocks:
            sorted_seals = sorted([(code, info.seal_amount_wan)
                                   for code, info in limit_up_stocks.items()],
                                  key=lambda x: x[1],
                                  reverse=True)
            logger.info(f"\n  封板金额前10:")
            for i, (stock, seal_amount) in enumerate(sorted_seals[:10], 1):
                logger.info(f"    {i:2d}. {stock}: {seal_amount:,.2f}万元")

        # 弱封板预警
        weak_seals = calculator.seal_calc.get_weak_seal_stocks()
        if weak_seals:
            logger.info(f"\n  弱封板股票 (<2000万):")
            for stock, info in list(weak_seals.items())[:10]:
                logger.info(f"    {stock}: {info.seal_amount_wan:.2f}万元")

    # 6. 性能评估
    logger.info(f"\n" + "=" * 70)
    logger.info("性能评估")
    logger.info("=" * 70)

    avg_throughput = total_events / total_time
    peak_requirement = 100000

    logger.info(f"\n单进程吞吐:")
    logger.info(f"  实际吞吐:      {avg_throughput:,.0f} 条/秒")
    logger.info(f"  峰值要求:      {peak_requirement:,} 条/秒")
    logger.info(f"  满足度:        {avg_throughput/peak_requirement*100:.1f}%")

    # 多进程评估
    num_processes = 8
    estimated_throughput = avg_throughput * num_processes
    logger.info(f"\n多进程评估 ({num_processes}进程):")
    logger.info(f"  理论吞吐:      {estimated_throughput:,.0f} 条/秒")
    logger.info(
        f"  峰值满足度:    {estimated_throughput/peak_requirement*100:.1f}%")

    if estimated_throughput >= peak_requirement:
        logger.info(f"  ✅ 可满足峰值要求")
    else:
        recommended = int(peak_requirement / avg_throughput) + 1
        logger.info(f"  ⚠️  建议增加进程数至 {recommended} 个")

    # 内存使用
    logger.info(f"\n内存使用:")
    logger.info(f"\n  数据存储（numpy数组）:")
    mem_mb = simulator._estimate_memory() / 1024 / 1024
    logger.info(f"    总计:          ~{mem_mb:.1f} MB")

    logger.info(f"\n  Level2Calculator:")
    logger.info(
        f"    委托簿条目:    {sum(len(orders) for orders in calculator.order_book.values()):,}"
    )
    logger.info(
        f"    虚拟委托:      {sum(len(orders) for orders in calculator.virtual_orders.values()):,}"
    )
    logger.info(f"    资金流向统计:  {len(calculator.flow_stats)} 只股票")
    logger.info(f"    板上流向统计:  {len(calculator.limit_up_flow)} 只股票")

    if calculator.seal_calc is not None:
        logger.info(f"\n  内置 SealAmountCalculator:")
        logger.info(f"    封板信息:      {len(calculator.seal_calc.seal_info)} 只股票")

    logger.info("\n" + "=" * 70)
    logger.info("测试完成!")
    logger.info("=" * 70)


def test_chunked_data_performance(chunk_duration_minutes: int = 30):
    """基于真实数据的分块性能测试（内存优化版）- 只测试 Level2Calculator
    
    Args:
        chunk_duration_minutes: 每个chunk的时间长度（分钟），默认30分钟
    """

    print("\n")
    print("╔" + "═" * 68 + "╗")
    print("║" + " " * 4 + "Level 2 分块数据性能测试 (Level2Calculator)" + " " * 5 + "║")
    print("╚" + "═" * 68 + "╝")
    print()

    # 1. 加载元数据和所有数据块
    simulator = ChunkedRealDataSimulator(
        chunk_duration_minutes=chunk_duration_minutes)
    simulator.load_metadata()
    simulator.load_all_chunks()

    total_events = simulator.total_events
    if total_events == 0:
        logger.error("没有加载到任何数据！")
        return

    # 2. 准备股票信息并初始化 Level2Calculator
    logger.info("\n准备股票信息...")
    stock_list = simulator.get_stock_list()
    logger.info(f"涉及股票数量: {len(stock_list)}")

    # 构建 stock_info 字典：{stock_code: limit_price}
    stock_info = {}
    stocks_with_close = 0
    for stock in stock_list:
        last_close = simulator.get_stock_last_close(stock)
        limit_price = get_limit_price(last_close)
        stock_info[stock] = limit_price
        if stock in simulator.stock_last_close:
            stocks_with_close += 1

    logger.info(f"获取到昨收价的股票: {stocks_with_close}/{len(stock_list)}")

    # 初始化 Level2Calculator（自动创建内置 SealAmountCalculator）
    logger.info("初始化 Level2Calculator...")
    calculator = Level2Calculator(stock_info=stock_info)

    # 3. 分块性能测试
    logger.info("\n" + "=" * 70)
    logger.info("开始分块性能测试 - Level2Calculator 采样测量法")
    logger.info("=" * 70)

    order_count = 0
    trans_count = 0
    quote_count = 0

    # 使用采样策略：只测量每100个事件中的1个，获得统计显著性的同时最小化开销
    # 对于百万级事件，1%采样仍有1万+样本，足够准确
    sample_rate = 100  # 采样率：每100个事件测量1个
    sample_size = max(10000, total_events // sample_rate)  # 预估样本数

    order_times = np.zeros(sample_size, dtype=np.float64)
    trans_times = np.zeros(sample_size, dtype=np.float64)
    quote_times = np.zeros(sample_size, dtype=np.float64)

    order_idx = 0
    trans_idx = 0
    quote_idx = 0

    start_time = time.time()

    # 不使用tqdm进度条，每10000条打印一次进度
    event_count = 0
    print_interval = 10000

    for event_type, stock_code, data in simulator.iterate_events_chunked():
        # 采样测量：只对部分事件进行时间测量
        should_sample = (event_count % sample_rate == 0)

        if event_type == 'l2order':
            if should_sample:
                t0 = time.time()
                calculator.on_l2order(stock_code, data)
                order_times[order_idx] = time.time() - t0
                order_idx += 1
            else:
                calculator.on_l2order(stock_code, data)
            order_count += 1

        elif event_type == 'l2transaction':
            if should_sample:
                t0 = time.time()
                calculator.on_l2transaction(stock_code, data)
                trans_times[trans_idx] = time.time() - t0
                trans_idx += 1
            else:
                calculator.on_l2transaction(stock_code, data)
            trans_count += 1

        elif event_type == 'l2quote':
            if should_sample:
                t0 = time.time()
                calculator.on_l2quote(stock_code, data)
                quote_times[quote_idx] = time.time() - t0
                quote_idx += 1
            else:
                calculator.on_l2quote(stock_code, data)
            quote_count += 1

        event_count += 1
        if event_count % print_interval == 0:
            elapsed = time.time() - start_time
            rate = event_count / elapsed
            logger.info(
                f"  已处理: {event_count:,}/{total_events:,} 事件 ({event_count/total_events*100:.1f}%), 速率: {rate:,.0f} 条/秒"
            )

    total_time = time.time() - start_time

    # 4. 输出性能统计（使用numpy计算，更快更准确）
    logger.info("\n" + "=" * 70)
    logger.info("性能统计")
    logger.info("=" * 70)

    logger.info(f"\n事件处理:")
    logger.info(f"  l2order:       {order_count:,} 条")
    logger.info(f"  l2transaction: {trans_count:,} 条")
    logger.info(f"  l2quote:       {quote_count:,} 条")
    logger.info(f"  总计:          {event_count:,} 条")
    logger.info(f"  处理时间:      {total_time:.2f} 秒")
    logger.info(f"  平均吞吐:      {event_count/total_time:,.0f} 条/秒")

    # Level2Calculator 性能（基于采样数据的统计）
    logger.info(f"\nLevel2Calculator 性能（采样测量，样本率 1/{sample_rate}）:")

    if order_idx > 0:
        times = order_times[:order_idx]
        avg_time = np.mean(times) * 1000000
        p50 = np.percentile(times, 50) * 1000000
        p99 = np.percentile(times, 99) * 1000000
        logger.info(
            f"  l2order:       平均 {avg_time:.2f} μs/条, P50 {p50:.2f} μs, P99 {p99:.2f} μs (样本数: {order_idx:,})"
        )
    if trans_idx > 0:
        times = trans_times[:trans_idx]
        avg_time = np.mean(times) * 1000000
        p50 = np.percentile(times, 50) * 1000000
        p99 = np.percentile(times, 99) * 1000000
        logger.info(
            f"  l2transaction: 平均 {avg_time:.2f} μs/条, P50 {p50:.2f} μs, P99 {p99:.2f} μs (样本数: {trans_idx:,})"
        )
    if quote_idx > 0:
        times = quote_times[:quote_idx]
        avg_time = np.mean(times) * 1000000
        p50 = np.percentile(times, 50) * 1000000
        p99 = np.percentile(times, 99) * 1000000
        logger.info(
            f"  l2quote:       平均 {avg_time:.2f} μs/条, P50 {p50:.2f} μs, P99 {p99:.2f} μs (样本数: {quote_idx:,})"
        )

    # 5. 计算结果统计
    logger.info(f"\n" + "=" * 70)
    logger.info("计算结果")
    logger.info("=" * 70)

    # 全量资金流向统计
    capital_stats = calculator.get_all_stats()
    logger.info(f"\n全量资金流向统计:")
    logger.info(f"  统计股票数:    {len(capital_stats)}")

    if capital_stats:
        sorted_flows = sorted(
            capital_stats.items(),
            key=lambda x: x[1].net_large + x[1].net_super_large,
            reverse=True)
        logger.info(f"\n  主力净流入前10:")
        for i, (stock, stats) in enumerate(sorted_flows[:10], 1):
            net_inflow = (stats.net_large +
                          stats.net_super_large) / 10000
            logger.info(f"    {i:2d}. {stock}: {net_inflow:,.2f}万元")

    # 板上资金流向统计（涨停期间）
    flow_stats = calculator.get_all_limit_up_stats()
    logger.info(f"\n板上资金流向统计 (涨停期间):")
    logger.info(f"  统计股票数:    {len(flow_stats)}")

    top_inflow = calculator.get_top_limit_up_inflow(10)
    if top_inflow:
        logger.info(f"\n  板上主力净流入前10:")
        for i, (stock, net_inflow) in enumerate(top_inflow, 1):
            logger.info(f"    {i:2d}. {stock}: {net_inflow/10000:,.2f}万元")

    # 涨停统计（通过内置的 seal_calc）
    if calculator.seal_calc is not None:
        limit_up_stocks = calculator.seal_calc.get_all_limit_up_stocks()
        logger.info(f"\n涨停股票:")
        logger.info(f"  涨停数量:      {len(limit_up_stocks)}")

        if limit_up_stocks:
            sorted_seals = sorted([(code, info.seal_amount_wan)
                                   for code, info in limit_up_stocks.items()],
                                  key=lambda x: x[1],
                                  reverse=True)
            logger.info(f"\n  封板金额前10:")
            for i, (stock, seal_amount) in enumerate(sorted_seals[:10], 1):
                logger.info(f"    {i:2d}. {stock}: {seal_amount:,.2f}万元")

        # 弱封板预警
        weak_seals = calculator.seal_calc.get_weak_seal_stocks()
        if weak_seals:
            logger.info(f"\n  弱封板股票 (<2000万):")
            for stock, info in list(weak_seals.items())[:10]:
                logger.info(f"    {stock}: {info.seal_amount_wan:.2f}万元")

    # 6. 性能评估
    logger.info(f"\n" + "=" * 70)
    logger.info("性能评估")
    logger.info("=" * 70)

    avg_throughput = total_events / total_time
    peak_requirement = 100000

    logger.info(f"\n单进程吞吐:")
    logger.info(f"  实际吞吐:      {avg_throughput:,.0f} 条/秒")
    logger.info(f"  峰值要求:      {peak_requirement:,} 条/秒")
    logger.info(f"  满足度:        {avg_throughput/peak_requirement*100:.1f}%")

    # 多进程评估
    num_processes = 8
    estimated_throughput = avg_throughput * num_processes
    logger.info(f"\n多进程评估 ({num_processes}进程):")
    logger.info(f"  理论吞吐:      {estimated_throughput:,.0f} 条/秒")
    logger.info(
        f"  峰值满足度:    {estimated_throughput/peak_requirement*100:.1f}%")

    if estimated_throughput >= peak_requirement:
        logger.info(f"  ✅ 可满足峰值要求")
    else:
        recommended = int(peak_requirement / avg_throughput) + 1
        logger.info(f"  ⚠️  建议增加进程数至 {recommended} 个")

    # 内存使用
    logger.info(f"\n内存使用:")
    logger.info(f"  ✅ 分块处理模式 - 每个chunk处理完后立即释放内存")
    logger.info(f"  Chunk时长:     {chunk_duration_minutes} 分钟")
    logger.info(f"  峰值内存:      ~{chunk_duration_minutes/30*500:.0f} MB (估算)")

    logger.info(f"\n  Level2Calculator:")
    logger.info(
        f"    委托簿条目:    {sum(len(orders) for orders in calculator.order_book.values()):,}"
    )
    logger.info(
        f"    虚拟委托:      {sum(len(orders) for orders in calculator.virtual_orders.values()):,}"
    )
    logger.info(f"    资金流向统计:  {len(calculator.flow_stats)} 只股票")
    logger.info(f"    板上流向统计:  {len(calculator.limit_up_flow)} 只股票")

    if calculator.seal_calc is not None:
        logger.info(f"\n  内置 SealAmountCalculator:")
        logger.info(f"    封板信息:      {len(calculator.seal_calc.seal_info)} 只股票")

    logger.info("\n" + "=" * 70)
    logger.info("测试完成!")
    logger.info("=" * 70)


if __name__ == "__main__":
    import sys

    # 默认使用分块模式（内存优化）
    if len(sys.argv) > 1 and sys.argv[1] == '--full':
        # 使用 --full 参数运行完整加载模式
        test_real_data_performance()
    else:
        # 默认使用分块模式，可以通过参数指定chunk大小（分钟）
        chunk_size = 30
        if len(sys.argv) > 1:
            try:
                chunk_size = int(sys.argv[1])
            except:
                pass
        test_chunked_data_performance(chunk_duration_minutes=chunk_size)
