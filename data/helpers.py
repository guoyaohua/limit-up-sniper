from collections import defaultdict
from loguru import logger


def transform_dict_mapping(original_dict, multi_values=True):
    """
    将字典中的键值映射关系重组转换

    参数:
        original_dict (dict): 输入字典，值可以是列表或单个值
        multi_values (bool): 是否将结果值保存为列表
            - True: 结果字典的值将是列表
            - False: 结果字典的值将是单个值

    返回:
        dict: 重组后的新字典

    示例:
        # 板块->股票的映射转换为股票->板块的映射
        >>> sector_stocks = {
        ...     'BK001': ['000001', '000002'],
        ...     'BK002': ['000002', '000003'],
        ... }
        >>> transform_dict_mapping(sector_stocks)
        {
            '000001': ['BK001'],
            '000002': ['BK001', 'BK002'],
            '000003': ['BK002']
        }

        # 单值映射转换
        >>> simple_dict = {'A': 1, 'B': 2, 'C': 1}
        >>> transform_dict_mapping(simple_dict, multi_values=True)
        {1: ['A', 'C'], 2: ['B']}
    """

    # 使用 defaultdict 来自动处理新键的初始化
    result = defaultdict(list if multi_values else set)

    # 遍历原始字典
    for key, values in original_dict.items():
        # 确保 values 是可迭代对象
        if not isinstance(values, (list, tuple, set)):
            values = [values]

        # 建立新的映射关系
        for value in values:
            if multi_values:
                result[value].append(key)
            else:
                result[value].add(key)

    # 如果不需要多值，将集合转换为单个值
    if not multi_values:
        return {k: next(iter(v)) for k, v in result.items()}

    # 返回普通字典
    return dict(result)


def get_safe_typecode(value):
    """
    安全地从 multiprocessing.Value 或 Synchronized wrapper 中提取 typecode
    """
    try:
        # 直接访问 _typecode 属性
        if hasattr(value, '_typecode'):
            return value._typecode

        # 尝试从底层对象获取
        if hasattr(value, '_obj'):
            # 对于 Array 对象，尝试 _obj._type_._type_
            if hasattr(value._obj, '_type_') and hasattr(
                    value._obj._type_, '_type_'):
                return value._obj._type_._type_
            # 对于 Value 对象，尝试 _obj._type_
            elif hasattr(value._obj, '_type_'):
                return value._obj._type_

        # 尝试从 _value 属性获取
        if hasattr(value, '_value') and hasattr(value._value, '_type_'):
            return value._value._type_

        # 根据实际值推断类型
        if hasattr(value, 'value'):
            val = value.value
            if isinstance(val, bool):
                return 'b'
            elif isinstance(val, int):
                return 'i'
            elif isinstance(val, float):
                return 'f'
            else:
                raise Exception("无法识别的类型")

    except Exception as e:
        logger.error(f"获取 typecode 失败: {e}")
        raise e

    raise Exception("无法识别的类型")
