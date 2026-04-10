#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
东方财富涨停板/炸板数据解析器
Parse limit-up and broken-board stock data from East Money Finance mhtml files
"""

import os
import re
from typing import List, Dict, Tuple
from datetime import datetime
from bs4 import BeautifulSoup
import html
import quopri


class DFCFZtlbParser:
    """东方财富涨停板列表解析器"""
    
    def __init__(self):
        pass
    
    def _get_stock_suffix(self, stock_code: str, market: str) -> str:
        """
        根据股票代码和市场代码获取后缀
        
        股票代码规则:
        - 60xxxx: 上海主板 .SH
        - 688xxx: 科创板 .SH
        - 000xxx, 001xxx: 深圳主板 .SZ
        - 002xxx, 003xxx: 深圳中小板/创业板 .SZ
        - 300xxx: 创业板 .SZ
        - 8xxxxx, 4xxxxx: 北交所 .BJ
        - 920xxx: 可能是北交所或其他 .BJ
        
        Args:
            stock_code: 股票代码
            market: 市场代码 (0=深圳, 1=上海)
        
        Returns:
            带后缀的股票代码
        """
        # 优先根据股票代码前缀判断
        if stock_code.startswith('6') or stock_code.startswith('5'):
            # 60xxxx, 688xxx 上海主板/科创板
            return f"{stock_code}.SH"
        elif stock_code.startswith('688'):
            # 科创板
            return f"{stock_code}.SH"
        elif stock_code.startswith('000') or stock_code.startswith('001'):
            # 深圳主板
            return f"{stock_code}.SZ"
        elif stock_code.startswith('002') or stock_code.startswith('003'):
            # 深圳中小板/主板
            return f"{stock_code}.SZ"
        elif stock_code.startswith('300') or stock_code.startswith('301'):
            # 创业板
            return f"{stock_code}.SZ"
        elif stock_code.startswith('8') or stock_code.startswith('4'):
            # 北交所
            return f"{stock_code}.BJ"
        elif stock_code.startswith('920'):
            # 可能是北交所或其他特殊代码
            return f"{stock_code}.BJ"
        else:
            # 根据market参数判断
            if market == '1':
                return f"{stock_code}.SH"
            elif market == '0':
                return f"{stock_code}.SZ"
            else:
                # 默认深圳
                return f"{stock_code}.SZ"
    
    def _decode_html_entities(self, text: str) -> str:
        """解码HTML实体"""
        return html.unescape(text)
    
    def _parse_ztlb_stats(self, stats_text: str) -> Tuple[int, int]:
        """
        解析涨停统计字段 (格式: n/m 表示m天中有n次涨停)
        
        Args:
            stats_text: 涨停统计文本，如 "3/4"
        
        Returns:
            (n, m) 元组，其中n是涨停次数，m是总天数
        """
        try:
            parts = stats_text.strip().split('/')
            if len(parts) == 2:
                n = int(parts[0])
                m = int(parts[1])
                return (n, m)
        except:
            pass
        return (0, 0)
    
    def parse_mhtml_file(self, file_path: str) -> List[Dict]:
        """
        解析mhtml文件中的涨停板数据
        
        Args:
            file_path: mhtml文件路径
        
        Returns:
            股票数据列表，每个元素是一个字典包含股票信息
        """
        print(f"正在解析文件: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # 解码quoted-printable编码
        # mhtml文件使用quoted-printable编码,需要先解码
        try:
            # 查找HTML内容部分
            html_start = content.find('<!DOCTYPE html>')
            if html_start == -1:
                html_start = content.find('<html>')
            
            if html_start != -1:
                html_content = content[html_start:]
                # 解码quoted-printable
                decoded_content = quopri.decodestring(html_content.encode('latin1')).decode('utf-8', errors='ignore')
                content = decoded_content
        except Exception as e:
            print(f"解码quoted-printable时出错: {e}, 使用原始内容")
        
        # 使用BeautifulSoup解析HTML
        soup = BeautifulSoup(content, 'html.parser')
        
        stocks = []
        
        # 查找所有包含data-stockcode属性的tr标签
        rows = soup.find_all('tr', {'data-stockcode': True})
        
        for row in rows:
            try:
                # 提取基本信息
                stock_code = row.get('data-stockcode', '')
                stock_name = self._decode_html_entities(row.get('data-stockname', ''))
                market = row.get('data-market', '0')
                
                # 获取带后缀的股票代码
                full_code = self._get_stock_suffix(stock_code, market)
                
                # 提取所有td单元格
                cells = row.find_all('td')
                
                if len(cells) < 16:
                    continue
                
                # 解析各个字段
                data = {
                    'stock_code': full_code,
                    'stock_name': stock_name,
                    'raw_code': stock_code,
                }
                
                # 提取涨跌幅
                try:
                    zdf_cell = cells[3].get_text(strip=True)
                    data['change_pct'] = zdf_cell
                except:
                    data['change_pct'] = ''
                
                # 提取最新价
                try:
                    price_cell = cells[4].get_text(strip=True)
                    data['latest_price'] = price_cell
                except:
                    data['latest_price'] = ''
                
                # 提取首次封板时间
                try:
                    seal_time_cell = cells[11].get_text(strip=True)
                    data['first_seal_time'] = seal_time_cell
                except:
                    data['first_seal_time'] = ''
                
                # 提取炸板次数
                try:
                    bomb_times_cell = cells[12].get_text(strip=True).replace('次', '')
                    data['bomb_times'] = int(bomb_times_cell) if bomb_times_cell.isdigit() else 0
                except:
                    data['bomb_times'] = 0
                
                # 提取涨停统计
                try:
                    stats_cell = cells[13]
                    # 获取红色bold的数字（连板次数）和普通数字（总天数）
                    red_bold = stats_cell.find('span', class_='red bold')
                    normal_span = stats_cell.find_all('span')
                    
                    if red_bold and len(normal_span) >= 2:
                        limit_days = int(red_bold.get_text(strip=True)) if red_bold.get_text(strip=True).isdigit() else 0
                        total_days = int(normal_span[-1].get_text(strip=True)) if normal_span[-1].get_text(strip=True).isdigit() else 0
                        data['limit_days'] = limit_days  # n天中有几次涨停
                        data['total_days'] = total_days  # 总共n天
                    else:
                        data['limit_days'] = 0
                        data['total_days'] = 0
                except:
                    data['limit_days'] = 0
                    data['total_days'] = 0
                
                # 提取连板数标识 (第15列，索引14)
                # 格式为 "首板" 或 "X连板" (如 "2连板", "4连板")
                try:
                    board_label_cell = cells[14]
                    board_label_text = board_label_cell.get_text(strip=True)
                    data['board_label'] = board_label_text  # 原始标识文本
                    
                    # 解析连板数
                    if '首板' in board_label_text:
                        data['is_first_board'] = True
                        data['continuous_board_count'] = 1
                    elif '连板' in board_label_text:
                        data['is_first_board'] = False
                        # 提取数字，如 "4连板" -> 4
                        match = re.search(r'(\d+)连板', board_label_text)
                        if match:
                            data['continuous_board_count'] = int(match.group(1))
                        else:
                            data['continuous_board_count'] = 0
                    else:
                        # 默认情况
                        data['is_first_board'] = True
                        data['continuous_board_count'] = 1
                except:
                    data['board_label'] = ''
                    data['is_first_board'] = True
                    data['continuous_board_count'] = 1
                
                # 提取所属行业 (第16列，索引15)
                try:
                    industry_cell = cells[15].get_text(strip=True)
                    data['industry'] = industry_cell
                except:
                    data['industry'] = ''
                
                stocks.append(data)
                
            except Exception as e:
                print(f"解析行数据出错: {e}")
                continue
        
        print(f"成功解析 {len(stocks)} 只股票")
        return stocks
    
    def classify_stocks(self, stocks: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
        """
        将股票分类为首板和连板
        
        基于"连板数"列的文本标识进行分类：
        - "首板": 归入首板列表
        - "X连板": 归入连板列表 (X >= 2)
        
        Args:
            stocks: 股票数据列表
        
        Returns:
            (首板列表, 连板列表)
        """
        first_board = []  # 首板
        continuous_board = []  # 连板
        
        for stock in stocks:
            is_first_board = stock.get('is_first_board', True)
            continuous_count = stock.get('continuous_board_count', 1)
            board_label = stock.get('board_label', '')
            
            # 基于板块标识进行分类
            if is_first_board or '首板' in board_label:
                # 首板
                first_board.append(stock)
            elif continuous_count >= 2 or '连板' in board_label:
                # 连板 (2连板及以上)
                continuous_board.append(stock)
            else:
                # 默认归入首板
                first_board.append(stock)
        
        return first_board, continuous_board
    
    def save_to_file(self, stocks: List[Dict], output_file: str, title: str = ""):
        """
        保存股票列表到文件（仅保存股票代码，每行一个）
        
        Args:
            stocks: 股票数据列表
            output_file: 输出文件路径
            title: 文件标题（不使用）
        """
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            for stock in stocks:
                code = stock.get('stock_code', '')
                if code:
                    f.write(f"{code}\n")
    
    def parse_and_classify(self, input_file: str, date_str: str, file_type: str):
        """
        解析并分类保存涨停/炸板数据
        
        Args:
            input_file: 输入mhtml文件路径
            date_str: 日期字符串 (如 20251113)
            file_type: 文件类型 ('涨停' 或 '炸板')
        """
        # 解析文件
        stocks = self.parse_mhtml_file(input_file)
        
        if not stocks:
            print(f"警告: 未能从文件中解析到股票数据")
            return
        
        # 输出目录
        output_dir = 'output/涨停列表'
        
        if file_type == '涨停':
            # 分类为首板和连板
            first_board, continuous_board = self.classify_stocks(stocks)
            
            # 保存首板列表
            first_output = os.path.join(output_dir, f'首次涨停_{date_str}.txt')
            self.save_to_file(
                first_board, 
                first_output, 
                f"首次涨停股票列表 - {date_str}"
            )
            print(f"首板列表已保存至: {first_output}")
            
            # 保存全部涨停列表（首板+连板）
            all_output = os.path.join(output_dir, f'涨停_{date_str}.txt')
            self.save_to_file(
                stocks,
                all_output,
                f"全部涨停股票列表 - {date_str}"
            )
            print(f"全部涨停列表已保存至: {all_output}")
            
            # 统计信息
            print(f"\n统计信息:")
            print(f"  总股票数: {len(stocks)}")
            print(f"  首板数量: {len(first_board)}")
            print(f"  连板数量: {len(continuous_board)}")
            
        elif file_type == '炸板':
            # 保存炸板列表
            output_file = os.path.join(output_dir, f'炸板_{date_str}.txt')
            self.save_to_file(
                stocks,
                output_file,
                f"炸板股票列表 - {date_str}"
            )
            print(f"炸板列表已保存至: {output_file}")
            print(f"炸板股票数: {len(stocks)}")


def main():
    """主函数"""
    import glob
    
    parser = DFCFZtlbParser()
    
    # 查找所有mhtml文件
    input_dir = 'output/dfcf_ztlb'
    
    # 处理涨停文件
    zt_files = glob.glob(os.path.join(input_dir, '涨停_*.mhtml'))
    for file_path in sorted(zt_files):
        # 从文件名提取日期
        filename = os.path.basename(file_path)
        match = re.search(r'涨停_(\d{8})\.mhtml', filename)
        if match:
            date_str = match.group(1)
            print(f"\n处理涨停文件: {filename}")
            parser.parse_and_classify(file_path, date_str, '涨停')
    
    # 处理炸板文件
    zb_files = glob.glob(os.path.join(input_dir, '炸板_*.mhtml'))
    for file_path in sorted(zb_files):
        # 从文件名提取日期
        filename = os.path.basename(file_path)
        match = re.search(r'炸板_(\d{8})\.mhtml', filename)
        if match:
            date_str = match.group(1)
            print(f"\n处理炸板文件: {filename}")
            parser.parse_and_classify(file_path, date_str, '炸板')


if __name__ == '__main__':
    main()