#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
东方财富板块映射获取器 - 独立脚本
功能：获取东方财富概念板块和行业板块的映射并保存到文件

"""

import os
import json
import shutil
from tqdm import tqdm
from datetime import datetime
from loguru import logger
from datetime import datetime
from scraper.em_scraper_api import SectorType, get_stock_to_sector_mapping_enhanced


class EastMoneySectorMappingScraper:
    """东方财富板块映射数据获取器"""
    def __init__(self, output_dir="output", max_workers=10):
        """
        初始化获取器
        
        Args:
            output_dir: 输出目录，默认为 "output"
        """
        self.output_dir = output_dir
        self.concept_mapping_file = os.path.join(
            output_dir, "concept_sector_data", "stock_to_concept_mapping.json")
        self.industry_mapping_file = os.path.join(
            output_dir, "industry_sector_data",
            "stock_to_industry_mapping.json")
        # 板块-成分股映射文件路径
        self.concept_sectors_dir = os.path.join(output_dir, "concept_sectors")
        self.industry_sectors_dir = os.path.join(output_dir,
                                                 "industry_sectors")
        self.max_workers = max_workers

    def load_sector_mapping(self,
                            sector_type: SectorType,
                            mapping_file: str,
                            batch_size: int = 5) -> dict:
        """
        加载板块映射数据，如果文件存在则从文件加载，否则重新获取并保存
        
        Args:
            sector_type: 板块类型（概念板块或行业板块）
            mapping_file: 映射文件路径
            batch_size: 批量获取时的批次大小
            
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
            # 如果文件不存在，则重新获取
            # 创建tqdm进度条作为回调函数
            pbar = tqdm(desc=f"获取{sector_name}映射")

            def progress_callback(current, total):
                pbar.total = total
                pbar.n = current
                pbar.refresh()
                print(total, current)

            try:
                sector_dict = get_stock_to_sector_mapping_enhanced(
                    sector_type,
                    max_workers=self.max_workers,
                    batch_size=batch_size,
                    use_anti_spider=False,
                    progress_callback=progress_callback)
            finally:
                pbar.close()

            # 确保目录存在
            if not os.path.exists(os.path.dirname(mapping_file)):
                os.makedirs(os.path.dirname(mapping_file), exist_ok=True)

            # 保存板块映射到文件
            with open(mapping_file, 'w', encoding='utf-8') as f:
                json.dump(sector_dict, f, ensure_ascii=False, indent=2)
            logger.info(f"获取到 {len(sector_dict)} 只股票的{sector_name}映射并保存到文件")

        return sector_dict

    def get_concept_sector_mapping(self,
                                   batch_size: int = 5,
                                   force_update: bool = False) -> dict:
        """
        获取概念板块映射
        
        Args:
            batch_size: 批量获取时的批次大小
            force_update: 是否强制更新（备份现有文件后重新获取）
            
        Returns:
            dict: 股票到概念板块的映射字典
        """
        if force_update and os.path.exists(self.concept_mapping_file):
            backup_file = self._backup_existing_file(self.concept_mapping_file)
            logger.info(f"已备份现有概念板块映射文件至: {backup_file}")

        return self.load_sector_mapping(SectorType.CONCEPT,
                                        self.concept_mapping_file, batch_size)

    def get_industry_sector_mapping(self,
                                    batch_size: int = 5,
                                    force_update: bool = False) -> dict:
        """
        获取行业板块映射
        
        Args:
            batch_size: 批量获取时的批次大小
            force_update: 是否强制更新（备份现有文件后重新获取）
            
        Returns:
            dict: 股票到行业板块的映射字典
        """
        if force_update and os.path.exists(self.industry_mapping_file):
            backup_file = self._backup_existing_file(
                self.industry_mapping_file)
            logger.info(f"已备份现有行业板块映射文件至: {backup_file}")

        return self.load_sector_mapping(SectorType.INDUSTRY,
                                        self.industry_mapping_file, batch_size)

    def get_all_sector_mappings(self,
                                batch_size: int = 5,
                                force_update: bool = False) -> tuple:
        """
        获取所有板块映射（概念和行业）
        
        Args:
            batch_size: 批量获取时的批次大小
            force_update: 是否强制更新（备份现有文件后重新获取）
            
        Returns:
            tuple: (概念板块映射字典, 行业板块映射字典)
        """
        logger.info("=" * 60)
        logger.info("开始获取东方财富板块映射数据")
        logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        # 获取概念板块映射
        logger.info("1. 获取概念板块映射...")
        concept_mapping = self.get_concept_sector_mapping(
            batch_size, force_update)

        # 获取行业板块映射
        logger.info("\n2. 获取行业板块映射...")
        industry_mapping = self.get_industry_sector_mapping(
            batch_size, force_update)

        logger.info("\n" + "=" * 60)
        logger.info("板块映射数据获取完成")
        logger.info(f"概念板块映射: {len(concept_mapping)} 只股票")
        logger.info(f"行业板块映射: {len(industry_mapping)} 只股票")
        logger.info(f"概念板块映射文件: {self.concept_mapping_file}")
        logger.info(f"行业板块映射文件: {self.industry_mapping_file}")
        logger.info("=" * 60)

        return concept_mapping, industry_mapping

    def build_sector_to_stocks_mapping(self, stock_to_sector_mapping: dict,
                                       sector_type: SectorType) -> dict:
        """
        构建板块到成分股的映射
        
        Args:
            stock_to_sector_mapping: 股票到板块的映射字典
            sector_type: 板块类型
            
        Returns:
            dict: 板块代码到{板块名称, 成分股列表}的映射
        """
        from scraper.em_scraper_api import EnhancedSectorDataFetcher, SectorConfig

        sector_to_stocks = {}

        # 获取所有板块信息
        config = SectorConfig()
        fetcher = EnhancedSectorDataFetcher(config,
                                            sector_type,
                                            use_anti_spider=False)

        logger.info(f"开始构建{sector_type.value}板块到成分股映射...")

        # 获取板块列表
        raw_sectors = fetcher.fetch_all_quotes(max_pages=100)
        if not raw_sectors:
            logger.error("未能获取板块列表")
            return sector_to_stocks

        # 构建板块代码到名称的映射
        sector_code_to_name = {
            sector.get('f12'): sector.get('f14')
            for sector in raw_sectors
            if sector.get('f12') and sector.get('f14')
        }

        # 反转股票-板块映射为板块-股票映射
        for stock_code, sector_codes in stock_to_sector_mapping.items():
            for sector_code in sector_codes:
                if sector_code not in sector_to_stocks:
                    sector_to_stocks[sector_code] = {
                        "name": sector_code_to_name.get(sector_code, "未知板块"),
                        "stocks": []
                    }
                sector_to_stocks[sector_code]["stocks"].append(stock_code)

        # 统计信息
        logger.info(f"构建完成: {len(sector_to_stocks)} 个{sector_type.value}板块")
        for sector_code, info in list(sector_to_stocks.items())[:5]:  # 显示前5个样本
            logger.info(
                f"  {sector_code} ({info['name']}): {len(info['stocks'])} 只股票")

        return sector_to_stocks

    def save_sector_to_stocks_mapping(self,
                                      sector_to_stocks: dict,
                                      sector_type: SectorType,
                                      force_backup: bool = True) -> str:
        """
        保存板块到成分股的映射
        
        Args:
            sector_to_stocks: 板块到成分股的映射字典
            sector_type: 板块类型
            force_backup: 是否备份现有文件
            
        Returns:
            str: 保存的文件路径
        """
        # 确定保存目录
        if sector_type == SectorType.CONCEPT:
            save_dir = self.concept_sectors_dir
        else:
            save_dir = self.industry_sectors_dir

        # 确保目录存在
        os.makedirs(save_dir, exist_ok=True)

        # 文件名包含日期
        timestamp = datetime.now().strftime("%Y%m%d")
        filename = f"sector_to_stocks_mapping_{timestamp}.json"
        filepath = os.path.join(save_dir, filename)

        # 如果文件已存在，先备份
        if os.path.exists(filepath) and force_backup:
            backup_file = self._backup_existing_file(filepath)
            logger.info(f"已备份现有文件至: {backup_file}")

        # 保存映射数据
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(sector_to_stocks, f, ensure_ascii=False, indent=2)

        logger.info(f"{sector_type.value}板块映射已保存至: {filepath}")

        # 同时保存一份不带日期的文件作为最新版本
        latest_filepath = os.path.join(save_dir,
                                       "sector_to_stocks_mapping_latest.json")
        with open(latest_filepath, 'w', encoding='utf-8') as f:
            json.dump(sector_to_stocks, f, ensure_ascii=False, indent=2)

        return filepath

    def get_all_sector_mappings_enhanced(self,
                                         batch_size: int = 5,
                                         force_update: bool = False) -> tuple:
        """
        获取所有板块映射（增强版：包含板块-成分股映射）
        
        Args:
            batch_size: 批量获取时的批次大小
            force_update: 是否强制更新（备份现有文件后重新获取）
            
        Returns:
            tuple: (概念板块映射字典, 行业板块映射字典, 概念板块-成分股字典, 行业板块-成分股字典)
        """
        logger.info("=" * 60)
        logger.info("开始获取东方财富板块映射数据（增强版）")
        logger.info(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        # 获取股票-板块映射
        concept_mapping, industry_mapping = self.get_all_sector_mappings(
            batch_size, force_update)

        # 构建板块-成分股映射
        logger.info("\n3. 构建概念板块-成分股映射...")
        concept_sector_to_stocks = self.build_sector_to_stocks_mapping(
            concept_mapping, SectorType.CONCEPT)

        logger.info("\n4. 构建行业板块-成分股映射...")
        industry_sector_to_stocks = self.build_sector_to_stocks_mapping(
            industry_mapping, SectorType.INDUSTRY)

        # 保存板块-成分股映射
        logger.info("\n5. 保存板块-成分股映射...")
        concept_filepath = self.save_sector_to_stocks_mapping(
            concept_sector_to_stocks, SectorType.CONCEPT)
        industry_filepath = self.save_sector_to_stocks_mapping(
            industry_sector_to_stocks, SectorType.INDUSTRY)

        logger.info("\n" + "=" * 60)
        logger.info("板块映射数据获取完成（增强版）")
        logger.info(f"概念板块数量: {len(concept_sector_to_stocks)}")
        logger.info(f"行业板块数量: {len(industry_sector_to_stocks)}")
        logger.info(f"概念板块-成分股文件: {concept_filepath}")
        logger.info(f"行业板块-成分股文件: {industry_filepath}")
        logger.info("=" * 60)

        return concept_mapping, industry_mapping, concept_sector_to_stocks, industry_sector_to_stocks

    def _backup_existing_file(self, file_path: str) -> str:
        """
        备份现有文件
        
        Args:
            file_path: 要备份的文件路径
            
        Returns:
            str: 备份文件路径
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{file_path}.backup_{timestamp}"
        shutil.move(file_path, backup_path)
        return backup_path

    def print_sample_data(self,
                          concept_mapping: dict,
                          industry_mapping: dict,
                          sample_size: int = 5):
        """
        打印样本数据
        
        Args:
            concept_mapping: 概念板块映射字典
            industry_mapping: 行业板块映射字典
            sample_size: 样本大小
        """
        logger.info(f"\n概念板块映射样本数据 (前{sample_size}个):")
        for i, (stock_code, sectors) in enumerate(concept_mapping.items()):
            if i >= sample_size:
                break
            logger.info(f"  {stock_code}: {sectors}")

        logger.info(f"\n行业板块映射样本数据 (前{sample_size}个):")
        for i, (stock_code, sectors) in enumerate(industry_mapping.items()):
            if i >= sample_size:
                break
            logger.info(f"  {stock_code}: {sectors}")


def main():
    """主函数"""
    # 创建获取器实例
    scraper = EastMoneySectorMappingScraper()

    try:
        # 获取所有板块映射（增强版）
        concept_mapping, industry_mapping, concept_sectors, industry_sectors = scraper.get_all_sector_mappings_enhanced(
            batch_size=100,
            force_update=True  # 设置为True可强制更新
        )

        # 打印样本数据
        scraper.print_sample_data(concept_mapping, industry_mapping)

        # 打印板块-成分股样本
        logger.info("\n" + "=" * 60)
        logger.info("板块-成分股映射样本数据:")

        logger.info("\n概念板块样本 (前3个):")
        for i, (sector_code, info) in enumerate(concept_sectors.items()):
            if i >= 3:
                break
            logger.info(
                f"  {sector_code} ({info['name']}): {len(info['stocks'])} 只股票")
            logger.info(f"    前5只股票: {info['stocks'][:5]}")

        logger.info("\n行业板块样本 (前3个):")
        for i, (sector_code, info) in enumerate(industry_sectors.items()):
            if i >= 3:
                break
            logger.info(
                f"  {sector_code} ({info['name']}): {len(info['stocks'])} 只股票")
            logger.info(f"    前5只股票: {info['stocks'][:5]}")

    except Exception as e:
        logger.exception(f"获取板块映射数据时发生错误: {e}")


if __name__ == "__main__":
    main()
