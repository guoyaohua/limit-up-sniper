import os
import json
import traceback
from loguru import logger

from scraper.em_scraper_api import SectorType
from config import STOCK_TO_CONCEPT_MAPPING_FILE, STOCK_TO_INDUSTRY_MAPPING_FILE
from data.helpers import transform_dict_mapping
from infra.utils import send_email


def load_sector_mapping(sector_type: SectorType,
                        mapping_file: str,
                        exclude_stocks: list = None) -> dict:
    """
    加载板块映射数据，如果文件存在则从文件加载，否则重新获取并保存

    Args:
        sector_type: 板块类型（概念板块或行业板块）
        mapping_file: 映射文件路径
        exclude_stocks: 需要排除的股票列表（例如新股），默认为None

    Returns:
        dict: 股票到板块的映射字典
    """

    sector_name = "概念板块" if sector_type == SectorType.CONCEPT else "行业板块"

    logger.info(f"开始获取股票到{sector_name}的映射...")

    if os.path.exists(mapping_file):
        with open(mapping_file, 'r', encoding='utf-8') as f:
            sector_dict = json.load(f)
        logger.info(f"从文件加载{sector_name}映射，共 {len(sector_dict)} 只股票")
    else:
        logger.warning(f"未找到{sector_name}映射文件，请先获取")
        raise FileNotFoundError(
            f"未找到{sector_name}映射文件，请先获取并保存到 {mapping_file}")

    # 排除新股（上市不足5日的股票，涨跌幅与普通股票不同）
    if exclude_stocks:
        excluded_count = 0
        for stock_code in exclude_stocks:
            # 尝试不同的股票代码格式
            stock_code_without_suffix = stock_code.split(
                '.')[0] if '.' in stock_code else stock_code
            if stock_code in sector_dict:
                del sector_dict[stock_code]
                excluded_count += 1
            elif stock_code_without_suffix in sector_dict:
                del sector_dict[stock_code_without_suffix]
                excluded_count += 1
        if excluded_count > 0:
            logger.info(f"从{sector_name}映射中排除了 {excluded_count} 只新股")

    return sector_dict


def load_ths_sector_stocks(sector_code: str, ) -> list:
    """
    加载同花顺板块成分股

    Args:
        sector_code: 板块代码，例如"883993"表示昨日首板表现

    Returns:
        list: 板块成分股列表

    Note:
        如果板块成分股超过5页（约100只股票），需要登录同花顺账号才能获取全部数据。
        程序会自动打开登录页面，请在浏览器中完成登录后继续。
    """
    from scraper.tonghuashun_scraper_combined import TonghuashunAPI
    from core.stock_pool import add_stock_code_suffix
    try:
        # 使用非无头模式，以便用户可以进行登录操作
        with TonghuashunAPI(headless=False) as api:
            logger.info(f"开始获取同花顺板块 {sector_code} 的成分股...")
            logger.info("提示：如果板块成分股超过5页，需要登录同花顺账号")

            stocks_df = api.get_sector_stocks(sector_code)

            if not stocks_df.empty:
                logger.info(f"成功获取 {len(stocks_df)} 只成分股")
                # 添加股票代码后缀
                stock_symbols = [
                    add_stock_code_suffix(code)
                    for code in stocks_df['代码'].tolist()
                ]

                return stock_symbols
            else:
                logger.warning(f"未找到同花顺板块 {sector_code} 的成分股")
                return []
    except KeyboardInterrupt:
        logger.warning("用户中断了操作")
        raise
    except Exception as e:
        logger.error(f"加载同花顺板块 {sector_code} 成分股失败：{e}")
        logger.error(traceback.format_exc())
        raise Exception(f"加载同花顺板块 {sector_code} 成分股失败：{e}")


def load_yesterday_first_limit_up_stock_list(pre_trade_date, stock_pool):
    try:
        # ---------------------------------- 载入本地文件 ---------------------------------- #
        output_path = os.path.join('output', '涨停列表',
                                   f'首次涨停_{pre_trade_date}.txt')
        if os.path.exists(output_path):
            with open(output_path, 'r', encoding='utf-8') as f:
                stock_list = f.read().splitlines()
                logger.info(f'昨日{pre_trade_date}首版涨停列表:{stock_list}')
                return stock_list

        # ------------------------------ 同花顺抓取(更新不及时) ---------------------------- #
        else:
            raise NotImplementedError("请手动获取昨日首版涨停列表")
            # 加载同花顺"昨日首次涨停"板块成分股
            stock_list = load_ths_sector_stocks(sector_code="883993")
            stock_list = set(stock_list) & set(stock_pool)
            logger.info(f'昨日{pre_trade_date}首版涨停列表:{stock_list}')
            return stock_list

    except Exception as e:
        logger.exception(f'【关键错误】获取昨日首版列表失败: {e}')
        send_email('【关键错误】获取昨日首版列表失败',
                   f'获取昨日首版列表失败: {e}\n{traceback.format_exc()}')
        raise e


def load_yesterday_limit_up_stock_list(pre_trade_date, stock_pool):
    try:
        # ---------------------------------- 载入本地文件 ---------------------------------- #
        output_path = os.path.join('output', '涨停列表',
                                   f'涨停_{pre_trade_date}.txt')
        if os.path.exists(output_path):
            with open(output_path, 'r', encoding='utf-8') as f:
                stock_list = f.read().splitlines()
                logger.info(f'昨日{pre_trade_date}涨停列表:{stock_list}')
                return stock_list

        # ------------------------------ 同花顺抓取(更新不及时) ---------------------------- #
        else:
            raise NotImplementedError("请手动获取昨日涨停列表")
            # 加载同花顺"昨日涨停"板块成分股
            stock_list = load_ths_sector_stocks(sector_code="883986")
            stock_list = set(stock_list) & set(stock_pool)
            logger.info(f'昨日{pre_trade_date}涨停列表:{stock_list}')
            return stock_list

    except Exception as e:
        logger.exception(f'【关键错误】获取昨日涨停列表失败: {e}')
        send_email('【关键错误】获取昨日涨停列表失败',
                   f'获取昨日涨停列表失败: {e}\n{traceback.format_exc()}')
        raise e
