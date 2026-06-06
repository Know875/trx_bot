"""TRX 专属工具函数，供现货和合约策略共享"""
from datetime import datetime
import config


def is_asia_peak():
    """UTC 07:00-15:00 = 亚洲交易高峰（北京 15:00-23:00）"""
    return 7 <= datetime.utcnow().hour < 15


def grid_count():
    return config.TRX_GRID_COUNT if is_asia_peak() else config.TRX_GRID_COUNT_NON_PEAK
