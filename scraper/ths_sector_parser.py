"""
同花顺板块数据解析器
解析从iwencai下载的板块数据，转换为与东方财富格式一致的JSON文件
"""

import json
import os
import pandas as pd
from datetime import datetime
from typing import Dict, List, Set
import re
import glob


class THSSectorParser:
    """同花顺板块数据解析器"""
    def __init__(self, input_file: str):
        """
        初始化解析器
        
        Args:
            input_file: 输入的Excel文件路径
        """
        self.input_file = input_file
        self.output_base_dir = "output"

        # 股票到行业板块的映射
        self.stock_to_industry = {}
        # 股票到概念板块的映射
        self.stock_to_concept = {}
        # 行业板块到股票的映射
        self.industry_to_stocks = {}
        # 概念板块到股票的映射
        self.concept_to_stocks = {}

    def parse_excel(self):
        """解析Excel文件"""
        try:
            # 尝试多种方式读取文件
            # 首先尝试读取关联的HTML数据文件
            base_name = os.path.splitext(self.input_file)[0]
            sheet_file = os.path.join(f"{base_name}.files", "sheet001.htm")

            if os.path.exists(sheet_file):
                # 读取sheet001.htm文件
                print(f"读取数据文件: {sheet_file}")
                with open(sheet_file, 'r', encoding='utf-8') as f:
                    html_content = f.read()
                # 使用pandas读取HTML表格
                from io import StringIO
                dfs = pd.read_html(StringIO(html_content), header=0)
                if dfs:
                    df = dfs[0]
                    # 如果第一行不是列名，手动设置列名
                    if '股票代码' not in df.columns:
                        # 假设第一行是列名
                        df.columns = df.iloc[0]
                        df = df.drop(0).reset_index(drop=True)
                else:
                    raise ValueError("No tables found in HTML file")
            else:
                # 尝试直接读取xls文件
                try:
                    # 首先尝试读取为Excel
                    df = pd.read_excel(self.input_file, engine='openpyxl')
                except:
                    try:
                        # 尝试使用xlrd
                        df = pd.read_excel(self.input_file, engine='xlrd')
                    except:
                        # 尝试读取为HTML（因为有些xls文件实际是HTML格式）
                        # 打开文件并读取HTML内容
                        with open(self.input_file, 'r', encoding='utf-8') as f:
                            html_content = f.read()
                        # 使用pandas读取HTML表格
                        from io import StringIO
                        dfs = pd.read_html(StringIO(html_content))
                        if dfs:
                            df = dfs[0]
                        else:
                            raise ValueError("No tables found in HTML file")

            print(f"成功读取文件，共 {len(df)} 行数据")
            print(f"列名: {df.columns.tolist()}")

            # 检查是否需要设置列名（第一行是标题）
            if df.columns.tolist()[0] not in ['股票代码', '股票简称']:
                # 第一行是列名
                df.columns = df.iloc[0]
                df = df.drop(0).reset_index(drop=True)
                print(f"已设置列名: {df.columns.tolist()}")

            # 打印前几行数据以检查格式
            print("\n前5行数据:")
            print(df.head())

            # 处理数据
            for _, row in df.iterrows():
                # 获取股票代码（去掉.SZ或.SH后缀）
                stock_code = str(row['股票代码']).split('.')[0]
                stock_name = str(row['股票简称'])

                # 解析行业板块（格式：大类-中类-小类）
                industry_str = str(row['所属同花顺行业'])
                if pd.notna(industry_str) and industry_str != 'nan':
                    # 将行业字符串分割，取最细分的类别
                    industry_parts = industry_str.split('-')
                    for industry_name in industry_parts:
                        industry_name = industry_name.strip()
                        # 过滤掉空名称和占位符"--"
                        if industry_name and industry_name != '--':
                            # 由于没有板块代码，使用板块名称作为代码
                            self._add_industry_mapping(stock_code,
                                                       industry_name,
                                                       industry_name)

                # 解析概念板块（格式：概念1;概念2;概念3）
                concept_str = str(row['所属概念'])
                if pd.notna(concept_str) and concept_str != 'nan':
                    concepts = concept_str.split(';')
                    for concept_name in concepts:
                        concept_name = concept_name.strip()
                        # 过滤掉空名称和占位符"--"
                        if concept_name and concept_name != '--':
                            # 由于没有板块代码，使用板块名称作为代码
                            self._add_concept_mapping(stock_code, concept_name,
                                                      concept_name)

            print(f"解析完成:")
            print(
                f"  - 股票数量: {len(self.stock_to_industry)} (行业), {len(self.stock_to_concept)} (概念)"
            )
            print(f"  - 行业板块数量: {len(self.industry_to_stocks)}")
            print(f"  - 概念板块数量: {len(self.concept_to_stocks)}")

        except Exception as e:
            print(f"解析Excel文件出错: {e}")
            raise

    def _add_industry_mapping(self, stock_code: str, sector_code: str,
                              sector_name: str):
        """添加股票与行业板块的映射关系"""
        # 添加股票到行业板块的映射
        if stock_code not in self.stock_to_industry:
            self.stock_to_industry[stock_code] = []
        if sector_code not in self.stock_to_industry[stock_code]:
            self.stock_to_industry[stock_code].append(sector_code)

        # 添加行业板块到股票的映射
        if sector_code not in self.industry_to_stocks:
            self.industry_to_stocks[sector_code] = {
                'name': sector_name,
                'stocks': []
            }
        if stock_code not in self.industry_to_stocks[sector_code]['stocks']:
            self.industry_to_stocks[sector_code]['stocks'].append(stock_code)

    def _add_concept_mapping(self, stock_code: str, sector_code: str,
                             sector_name: str):
        """添加股票与概念板块的映射关系"""
        # 添加股票到概念板块的映射
        if stock_code not in self.stock_to_concept:
            self.stock_to_concept[stock_code] = []
        if sector_code not in self.stock_to_concept[stock_code]:
            self.stock_to_concept[stock_code].append(sector_code)

        # 添加概念板块到股票的映射
        if sector_code not in self.concept_to_stocks:
            self.concept_to_stocks[sector_code] = {
                'name': sector_name,
                'stocks': []
            }
        if stock_code not in self.concept_to_stocks[sector_code]['stocks']:
            self.concept_to_stocks[sector_code]['stocks'].append(stock_code)

    def save_to_json(self):
        """保存数据到JSON文件"""
        # 获取当前日期
        date_str = datetime.now().strftime('%Y%m%d')

        # 创建THS子文件夹
        ths_industry_sector_dir = os.path.join(self.output_base_dir,
                                               'industry_sector_data', 'THS')
        ths_industry_sectors_dir = os.path.join(self.output_base_dir,
                                                'industry_sectors', 'THS')
        ths_concept_sector_dir = os.path.join(self.output_base_dir,
                                              'concept_sector_data', 'THS')
        ths_concept_sectors_dir = os.path.join(self.output_base_dir,
                                               'concept_sectors', 'THS')

        # 历史目录：放在各自THS目录下的history子目录中
        ths_industry_sector_history_dir = os.path.join(ths_industry_sector_dir,
                                                       'history')
        ths_industry_sectors_history_dir = os.path.join(
            ths_industry_sectors_dir, 'history')
        ths_concept_sector_history_dir = os.path.join(ths_concept_sector_dir,
                                                      'history')
        ths_concept_sectors_history_dir = os.path.join(ths_concept_sectors_dir,
                                                       'history')

        # 创建目录
        os.makedirs(ths_industry_sector_dir, exist_ok=True)
        os.makedirs(ths_industry_sectors_dir, exist_ok=True)
        os.makedirs(ths_concept_sector_dir, exist_ok=True)
        os.makedirs(ths_concept_sectors_dir, exist_ok=True)
        os.makedirs(ths_industry_sector_history_dir, exist_ok=True)
        os.makedirs(ths_industry_sectors_history_dir, exist_ok=True)
        os.makedirs(ths_concept_sector_history_dir, exist_ok=True)
        os.makedirs(ths_concept_sectors_history_dir, exist_ok=True)

        # 保存股票到行业板块的映射
        # 最新文件
        industry_stock_file = os.path.join(ths_industry_sector_dir,
                                           'stock_to_industry_mapping.json')
        with open(industry_stock_file, 'w', encoding='utf-8') as f:
            json.dump(self.stock_to_industry, f, ensure_ascii=False, indent=2)
        print(f"保存股票到行业板块映射: {industry_stock_file}")

        # 历史文件：output/industry_sector_data/THS/history/stock_to_industry_mapping_YYYYMMDD.json
        industry_stock_history_file = os.path.join(
            ths_industry_sector_history_dir,
            f'stock_to_industry_mapping_{date_str}.json')
        with open(industry_stock_history_file, 'w', encoding='utf-8') as f:
            json.dump(self.stock_to_industry, f, ensure_ascii=False, indent=2)
        print(f"保存股票到行业板块历史映射: {industry_stock_history_file}")

        # 保存行业板块到股票的映射
        # 历史文件放在 output/industry_sectors/THS/history 下
        industry_sector_history_file = os.path.join(
            ths_industry_sectors_history_dir,
            f'sector_to_stocks_mapping_{date_str}.json')
        industry_sector_latest = os.path.join(
            ths_industry_sectors_dir, 'sector_to_stocks_mapping_latest.json')
        with open(industry_sector_history_file, 'w', encoding='utf-8') as f:
            json.dump(self.industry_to_stocks, f, ensure_ascii=False, indent=2)
        with open(industry_sector_latest, 'w', encoding='utf-8') as f:
            json.dump(self.industry_to_stocks, f, ensure_ascii=False, indent=2)
        print(f"保存行业板块到股票映射: {industry_sector_history_file}")

        # 保存股票到概念板块的映射
        # 最新文件
        concept_stock_file = os.path.join(ths_concept_sector_dir,
                                          'stock_to_concept_mapping.json')
        with open(concept_stock_file, 'w', encoding='utf-8') as f:
            json.dump(self.stock_to_concept, f, ensure_ascii=False, indent=2)
        print(f"保存股票到概念板块映射: {concept_stock_file}")

        # 历史文件：output/concept_sector_data/THS/history/stock_to_concept_mapping_YYYYMMDD.json
        concept_stock_history_file = os.path.join(
            ths_concept_sector_history_dir,
            f'stock_to_concept_mapping_{date_str}.json')
        with open(concept_stock_history_file, 'w', encoding='utf-8') as f:
            json.dump(self.stock_to_concept, f, ensure_ascii=False, indent=2)
        print(f"保存股票到概念板块历史映射: {concept_stock_history_file}")

        # 保存概念板块到股票的映射
        # 历史文件放在 output/concept_sectors/THS/history 下
        concept_sector_history_file = os.path.join(
            ths_concept_sectors_history_dir,
            f'sector_to_stocks_mapping_{date_str}.json')
        concept_sector_latest = os.path.join(
            ths_concept_sectors_dir, 'sector_to_stocks_mapping_latest.json')
        with open(concept_sector_history_file, 'w', encoding='utf-8') as f:
            json.dump(self.concept_to_stocks, f, ensure_ascii=False, indent=2)
        with open(concept_sector_latest, 'w', encoding='utf-8') as f:
            json.dump(self.concept_to_stocks, f, ensure_ascii=False, indent=2)
        print(f"保存概念板块到股票映射: {concept_sector_history_file}")

        # 打印统计信息
        print("\n统计信息:")
        print(f"  行业板块数量: {len(self.industry_to_stocks)}")
        print(f"  概念板块数量: {len(self.concept_to_stocks)}")
        print(f"  股票总数（行业）: {len(self.stock_to_industry)}")
        print(f"  股票总数（概念）: {len(self.stock_to_concept)}")

        # 打印前几个板块示例
        print("\n行业板块示例（前5个）:")
        for i, (code,
                info) in enumerate(list(self.industry_to_stocks.items())[:5]):
            print(f"  {code}: {info['name']} ({len(info['stocks'])}只股票)")

        print("\n概念板块示例（前5个）:")
        for i, (code,
                info) in enumerate(list(self.concept_to_stocks.items())[:5]):
            print(f"  {code}: {info['name']} ({len(info['stocks'])}只股票)")


def main():
    """主函数"""
    # 自动查找最新的文件
    iwencai_dir = 'output/iwencai'
    xls_files = glob.glob(os.path.join(iwencai_dir, '*.xls'))

    if not xls_files:
        print(f"错误：在 {iwencai_dir} 目录中未找到任何 .xls 文件")
        return

    # 根据文件名中的日期排序，获取最新的文件
    # 假设文件名格式为 YYYY-MM-DD.xls
    latest_file = max(xls_files, key=lambda x: os.path.basename(x))
    input_file = latest_file

    print(f"找到最新文件: {input_file}")

    # 创建解析器
    parser = THSSectorParser(input_file)

    # 解析Excel文件
    print(f"开始解析文件: {input_file}")
    parser.parse_excel()

    # 保存到JSON文件
    print("\n开始保存数据...")
    parser.save_to_json()

    print("\n处理完成！")


if __name__ == '__main__':
    main()
