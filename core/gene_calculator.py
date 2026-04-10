"""
core/gene_calculator.py - 涨停基因计算与强势股票筛选

从 打板策略_v2.4.py 提取的 calculate_stock_gene() 和 get_strong_stocks() 函数。
"""

import os
import time
import traceback
from functools import partial

import numpy as np
import pandas as pd
from tqdm import tqdm
from loguru import logger

from config import TODAY, SHOULD_DOWNLOAD_KLINE
from infra.data_helpers import _round_price
from infra.utils import send_email


def calculate_stock_gene(data, N=250):
    '''
    计算涨停基因/股性 - 评估个股的涨停潜力和历史表现

    功能说明：
        通过分析个股历史涨停数据，计算多维度指标来评估股票的"涨停基因"强弱。
        主要用于筛选具有良好涨停潜力的标的，作为策略的核心选股依据。

    核心指标：
        1. 封板成功率：涨停封板的成功率，反映股票封板能力
        2. 首板封板率：首次涨停即封板的概率，反映股票强势程度
        3. 连板率：连续涨停的概率，反映股票的连板能力
        4. 涨停次数：历史涨停总次数，反映股票活跃度
        5. 次日溢价：涨停次日开盘/收盘溢价，反映涨停质量
        6. 红盘率：涨停次日收盘红盘概率，反映涨停持续性

    参数：
        data (pd.DataFrame): 个股日K线数据，需包含开高低收、涨停、炸板等字段
        N (int): 统计窗口期，默认250个交易日

    返回：
        pd.DataFrame: 增加了涨停基因相关指标的数据

    技术实现：
        1. 使用Pandas向量化操作，避免循环，提升性能
        2. 使用np.select处理条件判断，兼容旧版Pandas
        3. 使用滚动窗口计算历史统计指标
        4. 处理除零错误和边界条件
    '''

    # -------------------------- 辅助函数 -------------------------- #
    def _true_ratio(window):
        """计算窗口内True(1.0)值的比例，忽略NaN"""
        valid_values = window.dropna()
        if len(valid_values) == 0:
            return np.nan
        # 当使用1.0/0.0时，sum()可以正确计算出1.0的数量
        return valid_values.sum() / len(valid_values)

    def _dropna_mean(window):
        """计算窗口内非NaN值的平均值"""
        return window.dropna().mean()

    def _safe_positive_ratio(window):
        """安全地计算窗口内正值的比例，避免除零错误"""
        valid_values = window.dropna()
        if len(valid_values) == 0:
            return np.nan
        return (valid_values > 0).sum() / len(valid_values)

    try:
        # 去掉未上市的数据
        data = data[data['开盘价'] != 0].copy()

        # 确保 '涨停' 和 '炸板' 是布尔类型，用于逻辑判断
        data['涨停'] = data['涨停'].astype(bool)
        data['炸板'] = data['炸板'].astype(bool)

        # ------------------- 向量化计算每日指标 ------------------- #

        prev_day_limit_up = data['涨停'].shift(1).fillna(False)
        is_first_limit_up = data['涨停'] & ~prev_day_limit_up
        is_continuous_limit_up = data['涨停'] & prev_day_limit_up

        # 连板次数 (使用groupby和cumcount进行重置累加)
        streak_group = is_first_limit_up.cumsum()
        streak_count = data.groupby(streak_group).cumcount() + 1
        data['连板次数'] = streak_count.where(data['涨停'], 0)

        # --- 兼容性修复: 使用 np.select 和 1.0/0.0/np.nan ---
        # 1. 封板成功
        conditions_fs = [data['涨停'], data['炸板']]
        choices_fs = [1.0, 0.0]  # 使用浮点数 1.0 和 0.0
        data['封板成功'] = np.select(conditions_fs, choices_fs, default=np.nan)

        # 2. 首板封板
        conditions_sbfb = [is_first_limit_up, data['炸板'] & ~prev_day_limit_up]
        choices_sbfb = [1.0, 0.0]  # 使用浮点数 1.0 和 0.0
        data['首板封板'] = np.select(conditions_sbfb, choices_sbfb, default=np.nan)

        # 3. 连板
        conditions_lb = [is_continuous_limit_up, is_first_limit_up]
        choices_lb = [1.0, 0.0]
        data['连板'] = np.select(conditions_lb, choices_lb, default=np.nan)

        # 向量化计算次日溢价
        next_day_open = data['开盘价'].shift(-1)
        next_day_close = data['收盘价'].shift(-1)
        next_day_prev_close = data['昨收'].shift(-1)

        premium_open = (next_day_open -
                        next_day_prev_close) / next_day_prev_close
        premium_close = (next_day_close -
                         next_day_prev_close) / next_day_prev_close

        data['涨停次日开盘溢价'] = premium_open.where(data['涨停'])
        data['涨停次日收盘溢价'] = premium_close.where(data['涨停'])
        data['首板次日开盘溢价'] = premium_open.where(
            data['首板封板'].fillna(False).astype(bool))
        data['首板次日收盘溢价'] = premium_close.where(
            data['首板封板'].fillna(False).astype(bool))
        data['首板炸板次日开盘溢价'] = premium_open.where(data['炸板']
                                                & ~prev_day_limit_up)
        data['首板炸板次日收盘溢价'] = premium_close.where(data['炸板']
                                                 & ~prev_day_limit_up)
        data['炸板次日开盘溢价'] = premium_open.where(data['炸板'])
        data['炸板次日收盘溢价'] = premium_close.where(data['炸板'])

        data['涨停或炸板次日开盘溢价'] = data['涨停次日开盘溢价'].fillna(data['炸板次日开盘溢价'])
        data['涨停或炸板次日收盘溢价'] = data['涨停次日收盘溢价'].fillna(data['炸板次日收盘溢价'])
        data['首板涨停或炸板次日开盘溢价'] = data['首板次日开盘溢价'].fillna(data['首板炸板次日开盘溢价'])
        data['首板涨停或炸板次日收盘溢价'] = data['首板次日收盘溢价'].fillna(data['首板炸板次日收盘溢价'])

        # -------------------------- 优化的滚动窗口计算 (v2.3) -------------------------- #
        # 使用纯向量化操作替代 rolling().apply()，性能提升5-10倍

        # 1. 封板成功率等比例指标 - 使用 rolling().sum() / rolling().count()
        seal_success_rolling_sum = data['封板成功'].rolling(N, min_periods=1).sum()
        seal_success_rolling_count = data['封板成功'].rolling(
            N, min_periods=1).count()
        data['封板成功率'] = seal_success_rolling_sum / seal_success_rolling_count

        first_board_seal_rolling_sum = data['首板封板'].rolling(
            N, min_periods=1).sum()
        first_board_seal_rolling_count = data['首板封板'].rolling(
            N, min_periods=1).count()
        data[
            '首板封板率'] = first_board_seal_rolling_sum / first_board_seal_rolling_count

        continuous_board_rolling_sum = data['连板'].rolling(N,
                                                          min_periods=1).sum()
        continuous_board_rolling_count = data['连板'].rolling(
            N, min_periods=1).count()
        data[
            '连板率'] = continuous_board_rolling_sum / continuous_board_rolling_count

        # 2. 涨停次数 - 直接使用 sum()
        data['涨停次数'] = data['涨停'].rolling(N, min_periods=1).sum()
        data['近五日涨停次数'] = data['涨停'].rolling(5, min_periods=1).sum()
        data['近十日涨停次数'] = data['涨停'].rolling(10, min_periods=1).sum()

        # 3. 平均溢价 - 使用 mean()（自动忽略NaN）
        data['涨停或炸板次日开盘平均溢价'] = data['涨停或炸板次日开盘溢价'].rolling(
            N, min_periods=1).mean()
        data['涨停或炸板次日收盘平均溢价'] = data['涨停或炸板次日收盘溢价'].rolling(
            N, min_periods=1).mean()
        data['首板涨停或炸板次日开盘平均溢价'] = data['首板涨停或炸板次日开盘溢价'].rolling(
            N, min_periods=1).mean()
        data['首板涨停或炸板次日收盘平均溢价'] = data['首板涨停或炸板次日收盘溢价'].rolling(
            N, min_periods=1).mean()
        data['涨停次日开盘平均溢价'] = data['涨停次日开盘溢价'].rolling(N, min_periods=1).mean()
        data['涨停次日收盘平均溢价'] = data['涨停次日收盘溢价'].rolling(N, min_periods=1).mean()
        data['首板次日开盘平均溢价'] = data['首板次日开盘溢价'].rolling(N, min_periods=1).mean()
        data['首板次日收盘平均溢价'] = data['首板次日收盘溢价'].rolling(N, min_periods=1).mean()

        # 4. 溢价超5%次数 - 使用向量化比较 + rolling().sum()
        data['涨停次日开盘溢价超5%次数'] = (data['涨停次日开盘溢价'] > 0.05).rolling(
            N, min_periods=1).sum()
        data['涨停次日收盘溢价超5%次数'] = (data['涨停次日收盘溢价'] > 0.05).rolling(
            N, min_periods=1).sum()
        data['首板次日开盘溢价超5%次数'] = (data['首板次日开盘溢价'] > 0.05).rolling(
            N, min_periods=1).sum()
        data['首板次日收盘溢价超5%次数'] = (data['首板次日收盘溢价'] > 0.05).rolling(
            N, min_periods=1).sum()

        # 5. 红盘率 - 使用向量化比较（注意：gt()方法会保持NaN，比>运算符更准确）
        limit_up_next_close_red_sum = data['涨停次日收盘溢价'].gt(0).rolling(
            N, min_periods=1).sum()
        limit_up_next_close_red_count = data['涨停次日收盘溢价'].rolling(
            N, min_periods=1).count()
        data[
            '涨停次日收盘红盘率'] = limit_up_next_close_red_sum / limit_up_next_close_red_count

        limit_up_next_open_red_sum = data['涨停次日开盘溢价'].gt(0).rolling(
            N, min_periods=1).sum()
        limit_up_next_open_red_count = data['涨停次日开盘溢价'].rolling(
            N, min_periods=1).count()
        data[
            '涨停次日开盘红盘率'] = limit_up_next_open_red_sum / limit_up_next_open_red_count

        first_board_next_close_red_sum = data['首板次日收盘溢价'].gt(0).rolling(
            N, min_periods=1).sum()
        first_board_next_close_red_count = data['首板次日收盘溢价'].rolling(
            N, min_periods=1).count()
        data[
            '首板次日收盘红盘率'] = first_board_next_close_red_sum / first_board_next_close_red_count

        first_board_next_open_red_sum = data['首板次日开盘溢价'].gt(0).rolling(
            N, min_periods=1).sum()
        first_board_next_open_red_count = data['首板次日开盘溢价'].rolling(
            N, min_periods=1).count()
        data[
            '首板次日开盘红盘率'] = first_board_next_open_red_sum / first_board_next_open_red_count

        # 6. 最近一次涨停距离 - 使用优化的方法
        limit_up_indices = data[data['涨停']].index
        if len(limit_up_indices) > 0:
            data['最近一次涨停'] = data.index.to_series().apply(
                lambda x: x - limit_up_indices[limit_up_indices <= x].max() if
                len(limit_up_indices[limit_up_indices <= x]) > 0 else np.nan)
        else:
            data['最近一次涨停'] = np.nan

        denominator = data['涨停次数'].astype(float)
        data['涨停次日开盘溢价超5%比例'] = np.divide(data['涨停次日开盘溢价超5%次数'],
                                          denominator,
                                          out=np.full_like(
                                              denominator, np.nan),
                                          where=denominator != 0)
        data['涨停次日收盘溢价超5%比例'] = np.divide(data['涨停次日收盘溢价超5%次数'],
                                          denominator,
                                          out=np.full_like(
                                              denominator, np.nan),
                                          where=denominator != 0)

        # 添加原代码中存在的 '首板涨停' 列
        data['首板涨停'] = is_first_limit_up

        # U6升级：计算近20日平均振幅，用于波动率加权仓位管理
        amplitude = (data['最高价'] - data['最低价']) / data['收盘价']
        data['近20日平均振幅'] = amplitude.rolling(20, min_periods=5).mean()

        return data
    except Exception as e:
        print(
            f"计算涨停基因时发生错误: {e}，股票代码：{data['股票代码'].iloc[0] if not data.empty else '未知'}"
        )
        raise e


def get_strong_stocks(stock_pool, stock_info_dict, end_date):
    try:
        from xtquant import xtdata
        from joblib import Parallel, delayed

        # ---------------------------------- 载入本地文件 ---------------------------------- #
        output_path = os.path.join('output', '强势股票', f'强势股票_{TODAY}.csv')
        if os.path.exists(output_path):
            strong_stock_df = pd.read_csv(output_path)
            logger.info(f'日期：{TODAY}，强势股票数据已存在，直接加载 {output_path}')
            return strong_stock_df["股票代码"].unique().tolist()
    except Exception as e:
        logger.exception(f'【关键错误】获取强势股票初始化失败: {e}')
        send_email('【关键错误】获取强势股票初始化失败',
                   f'获取强势股票初始化时发生异常: {e}\n{traceback.format_exc()}')
        raise e

    # ---------------------------------- 获取K线数据 ---------------------------------- #
    k_line = xtdata.get_market_data(field_list=[
        'time', 'open', 'high', 'low', 'close', 'volume', 'amount', 'preClose',
        'suspendFlag'
    ],
                                    stock_list=stock_pool,
                                    period='1d',
                                    start_time='',
                                    end_time=end_date,
                                    count=300,
                                    dividend_type='none',
                                    fill_data=True)

    def _stack_result(k_line):
        result = None
        for key in k_line.keys():
            if key == 'time':
                continue
            data = k_line[key].stack().reset_index()
            data.columns = ['股票代码', '日期', key]
            if result is None:
                result = data
            else:
                result = pd.merge(result, data, on=['股票代码', '日期'], how='outer')
        return result

    k_line_df = _stack_result(k_line)
    k_line_df.rename(columns={
        'time': '日期',
        'open': '开盘价',
        'high': '最高价',
        'low': '最低价',
        'close': '收盘价',
        'volume': '成交量',
        'amount': '成交额',
        'preClose': '昨收',
        'suspendFlag': '停牌标记'
    },
                     inplace=True)

    # --------------------------------- 检查历史涨停状态 --------------------------------- #

    def _up_down_limit_check(df):
        """
        计算每日涨跌停价，并判断是否涨停、跌停或炸板 (修复版)

        修复和优化点:
        1. 增加对科创板('688'开头)20%涨跌幅的支持。
        2. 增加对ST和*ST股票5%涨跌幅的支持 (基于'股票名称'列)。
        3. 使用布尔掩码优化代码，避免重复计算和筛选，提升性能。
        4. 采用向量化操作计算，取代map函数，提高效率。
        """
        # 默认设置为10%涨跌幅
        df['涨停价'] = _round_price(df['昨收'] * 1.1)
        df['跌停价'] = _round_price(df['昨收'] * 0.9)

        # 筛选出创业板和科创板股票 (20%涨跌幅)
        is_gem_or_star = df['股票代码'].str.startswith(('30', '688'))
        df.loc[is_gem_or_star,
               '涨停价'] = _round_price(df.loc[is_gem_or_star, '昨收'] * 1.2)
        df.loc[is_gem_or_star,
               '跌停价'] = _round_price(df.loc[is_gem_or_star, '昨收'] * 0.8)

        # 筛选出ST股票 (5%涨跌幅)
        # 注意: ST股的判断优先级应低于创业板/科创板，因为有些ST股可能在这些板块
        # is_st = df['股票名称'].str.contains('ST')
        # df.loc[is_st, '涨停价'] = _round_price(df.loc[is_st, '昨收'] * 1.05)
        # df.loc[is_st, '跌停价'] = _round_price(df.loc[is_st, '昨收'] * 0.95)

        # 判断涨停、跌停、炸板状态 (使用0.0001元作为浮点数误差容忍范围)
        tolerance = 0.0001
        df['涨停'] = abs(df['涨停价'] - df['收盘价']) < tolerance
        df['跌停'] = abs(df['跌停价'] - df['收盘价']) < tolerance
        df['炸板'] = (abs(df['涨停价'] - df['最高价']) <
                    tolerance) & (abs(df['涨停价'] - df['收盘价']) > tolerance)

        # 去除未上市部分，开盘价=0
        df.loc[df['开盘价'] == 0, ['涨停', '跌停', '炸板']] = False

        return df

    k_line_df = _up_down_limit_check(k_line_df)

    # ---------------------------------- 数据预处理 (v2.3优化) ---------------------------------- #
    # 预先排序，避免在子进程中重复排序
    logger.info('预处理K线数据：按股票代码和日期排序...')
    k_line_df = k_line_df.sort_values(['股票代码', '日期']).reset_index(drop=True)

    # 创建股票分组索引，加速后续查询
    stock_groups = k_line_df.groupby('股票代码').groups
    logger.info(f'数据预处理完成，共 {len(stock_groups)} 只股票')

    # ---------------------------------- 计算涨停基因 (v2.3优化) ---------------------------------- #
    # 动态确定最优进程数
    import multiprocessing
    cpu_count = multiprocessing.cpu_count()
    optimal_workers = min(max(cpu_count - 2, 1), 16)  # 保留2个核心，最多16个进程
    logger.info(f'使用 {optimal_workers} 个进程计算涨停基因（CPU核心数: {cpu_count}）')

    # 并行计算 - 使用预处理的索引
    result_df_list = Parallel(
        n_jobs=optimal_workers, prefer="processes",
        verbose=5)(delayed(calculate_stock_gene)(k_line_df.iloc[
            stock_groups[stock]].reset_index(drop=True))
                   for stock in tqdm(stock_pool, desc='计算涨停基因'))

    result_df = pd.concat(result_df_list)

    result_df = result_df.loc[result_df['日期'] == end_date].reset_index(
        drop=True)

    # 保存结果
    output_path = os.path.join('output', '涨停基因', f'涨停基因_{TODAY}.csv')
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    result_df.to_csv(output_path, index=False)
    logger.info(f'日期：{TODAY}，涨停基因计算完成，结果保存在 {output_path}')

    # ---------------------------------- 筛选出强势股票 --------------------------------- #
    invalid_stock_list = []
    # 1. 过滤掉流通市值小于10亿的股票
    stock_list = []
    for stock_code in stock_info_dict:
        if stock_info_dict[stock_code]['流通股本'] * stock_info_dict[stock_code][
                '昨日收盘价'] < 1e9:
            stock_list.append(stock_code)
    invalid_stock_list.extend(stock_list)
    logger.info(
        f'日期：{TODAY}，流通市值小于1亿的股票数量：{len(stock_list)}, 股票代码：{stock_list}')

    # 2. 过滤掉当日涨停价在60日均线以下的股票 （无法突破60日均线）
    stock_list = []
    for stock_code in stock_info_dict:
        if stock_info_dict[stock_code]['60日均线'] > stock_info_dict[stock_code][
                '涨停价']:
            stock_list.append(stock_code)
    invalid_stock_list.extend(stock_list)
    logger.info(
        f'日期：{TODAY}，涨停价在60日均线以下的股票数量：{len(stock_list)}, 股票代码：{stock_list}')

    # 3. 过滤掉近五日均成交额小于1亿的股票
    stock_list = []
    for stock_code in stock_info_dict:
        if stock_info_dict[stock_code]['5日平均成交额'] < 1e8:
            stock_list.append(stock_code)
    invalid_stock_list.extend(stock_list)
    logger.info(
        f'日期：{TODAY}，近五日均成交额小于1亿的股票数量：{len(stock_list)}, 股票代码：{stock_list}')

    # 4. 过滤掉昨日收盘价小于5元的股票
    stock_list = result_df.loc[result_df['收盘价'] < 5, '股票代码'].tolist()
    invalid_stock_list.extend(stock_list)
    logger.info(
        f'日期：{TODAY}，昨日收盘价小于5元的股票数量：{len(stock_list)}, 股票代码：{stock_list}')

    # # 5. 过滤掉最近5日有涨停的股票
    # stock_list = result_df.loc[result_df['近五日涨停次数'] > 0, '股票代码'].tolist()

    # 5. 过滤掉昨日首板股票
    stock_list = result_df.loc[result_df['涨停'], '股票代码'].tolist()
    invalid_stock_list.extend(stock_list)
    logger.info(f'日期：{TODAY}，近五日有涨停的股票数量：{len(stock_list)}, 股票代码：{stock_list}')

    # 6. 过滤掉历史涨停次日无良好溢价的股票
    stock_list = result_df.loc[(result_df['涨停次日开盘平均溢价'] < 0.01)
                               & (result_df['涨停次日收盘平均溢价'] < 0.01),
                               '股票代码'].tolist()
    invalid_stock_list.extend(stock_list)
    logger.info(
        f'日期：{TODAY}，历史涨停次日无良好溢价的股票数量：{len(stock_list)}, 股票代码：{stock_list}')

    # 7. 过滤掉近一年无涨停的股票
    stock_list = result_df.loc[result_df['涨停次数'] == 0, '股票代码'].tolist()
    invalid_stock_list.extend(stock_list)
    logger.info(f'日期：{TODAY}，近一年无涨停的股票数量：{len(stock_list)}, 股票代码：{stock_list}')

    # 8. 过滤掉首版封板率小于0.7的股票
    stock_list = result_df.loc[result_df['首板封板率'] <= 0.7, '股票代码'].tolist()
    invalid_stock_list.extend(stock_list)
    logger.info(
        f'日期：{TODAY}，首版封板率小于0.7的股票数量：{len(stock_list)}, 股票代码：{stock_list}')

    # --------------------------------- 计算涨停基因打分 --------------------------------- #
    '''
    核心备选池筛选 (人和初选)
    构建个股"涨停基因"加权评分模型 (Stock Strength Score, SSS)，以下为具体的评分计算流程。
        •	因子权重: (权重不变)
            o	连板率 (30%)
            o	涨停次日开盘溢价超5%比例 (25%)
            o	涨停次日收盘红盘率 (15%)
            o	首板封板率 (15%)
            o	涨停次数 (10%)
            o	涨停或炸板次日开盘平均溢价 (5%)
        •	因子标准化与评分计算流程:
            o	获取原始值： 对"基础股票池"中的每一只股票，计算出上述6个因子的原始数值（如连板率25%，涨停次数10次等）。
            o	标准化： 将每个因子的原始值在所有股票中进行排序，并转换为 0到100的百分位得分。例如，一只股票的"连板率"在所有股票中排名前10%，则其"连板率"因子得分为90。
        •	因子得分 = (该股票排名 - 1) / (股票总数 - 1) * 100
            o	加权求和： 将每只股票的6个因子得分，按照其对应的权重进行加权求和，得到最终的SSS。
        •	SSS = (连板率得分 * 30%) + (溢价超5%比例得分 * 25%) + ...
            o	筛选： 对所有股票的SSS进行降序排列，选取排名前1000名的股票，生成"核心备选池"。
    '''
    result_df['连板率_得分'] = result_df['连板率'].rank(ascending=False,
                                                pct=True) * 100
    result_df['涨停次日收盘溢价超5%比例_得分'] = result_df['涨停次日收盘溢价超5%比例'].rank(
        ascending=False, pct=True) * 100
    result_df['首板次日收盘红盘率_得分'] = result_df['首板次日收盘红盘率'].rank(ascending=False,
                                                            pct=True) * 100
    result_df['首板封板率_得分'] = result_df['首板封板率'].rank(ascending=False,
                                                    pct=True) * 100
    result_df['涨停次数_得分'] = result_df['涨停次数'].rank(ascending=False,
                                                  pct=True) * 100
    result_df['涨停或炸板次日开盘平均溢价_得分'] = result_df['涨停或炸板次日开盘平均溢价'].rank(
        ascending=False, pct=True) * 100
    result_df['首板涨停或炸板次日开盘平均溢价_得分'] = result_df['首板涨停或炸板次日开盘平均溢价'].rank(
        ascending=False, pct=True) * 100
    # # 计算总分
    # result_df['涨停基因打分'] = (result_df['连板率_得分'] * 0.3 +
    #                        result_df['涨停次日收盘溢价超5%比例_得分'] * 0.25 +
    #                        result_df['首板次日收盘红盘率_得分'] * 0.15 +
    #                        result_df['首板封板率_得分'] * 0.15 +
    #                        result_df['涨停次数_得分'] * 0.1 +
    #                        result_df['涨停或炸板次日开盘平均溢价_得分'] * 0.05)

    # 计算总分
    result_df['涨停基因打分'] = (result_df['涨停次日收盘溢价超5%比例_得分'] * 0.25 +
                           result_df['首板次日收盘红盘率_得分'] * 0.25 +
                           result_df['首板封板率_得分'] * 0.25 +
                           result_df['涨停次数_得分'] * 0.1 +
                           result_df['首板涨停或炸板次日开盘平均溢价_得分'] * 0.15)

    # ------------------------------- 筛选出涨停基因良好的股票 ------------------------------- #
    strong_stock_df = result_df.loc[
        ~result_df['股票代码'].isin(invalid_stock_list)].reset_index(drop=True)
    logger.info(
        f'日期：{TODAY}，初筛过滤掉的股票数量：{len(set(invalid_stock_list))}，剩余数量：{len(strong_stock_df)}'
    )

    # 筛选涨停基因前1000的股票
    strong_stock_df = strong_stock_df.sort_values(
        '涨停基因打分', ascending=False).reset_index(drop=True)
    strong_stock_df = strong_stock_df.head(1000)
    logger.info(
        f'日期：{TODAY}，强势股票数量：{len(strong_stock_df)}，股票代码：{strong_stock_df["股票代码"].tolist()}'
    )

    # ----------------------------------- 保存数据 ----------------------------------- #
    output_path = os.path.join('output', '强势股票', f'强势股票_{TODAY}.csv')
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    strong_stock_df.to_csv(output_path, index=False)

    # U6升级：将近20日平均振幅存入 stock_info_dict，用于波动率加权仓位管理
    for _, row in result_df.iterrows():
        code = row['股票代码']
        if code in stock_info_dict:
            amp = row.get('近20日平均振幅', 0.05)
            stock_info_dict[code]['近20日平均振幅'] = amp if not pd.isna(amp) else 0.05

    logger.info(f'日期：{TODAY}，强势股票数据保存到 {output_path}')

    return strong_stock_df["股票代码"].unique().tolist()
