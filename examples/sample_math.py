"""示例：被测试的目标模块（用于演示测试生成 / 运行测试场景）。"""

from __future__ import annotations


def add(a: float, b: float) -> float:
    """返回两数之和。"""
    return a + b


def divide(a: float, b: float) -> float:
    """返回 a / b；b 为 0 时抛出 ValueError。"""
    if b == 0:
        raise ValueError("除数不能为 0")
    return a / b


def average(numbers):
    """返回列表平均值；空列表抛出 ValueError。"""
    if not numbers:
        raise ValueError("numbers 不能为空")
    return sum(numbers) / len(numbers)


def is_prime(value: int) -> bool:
    """判断 value 是否为素数。"""
    if value < 2:
        return False
    for divisor in range(2, int(value**0.5) + 1):
        if value % divisor == 0:
            return False
    return True
