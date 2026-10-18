"""bili-note 版本号单一来源；顺便在最早时机修好 CUDA DLL 搜索路径。"""

from . import cuda_bootstrap

cuda_bootstrap.apply()

__version__ = "3.0.0"
