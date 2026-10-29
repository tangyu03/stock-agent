"""stock-agent v6 point table package."""
import os

# Windows 多核机器上 OpenBLAS 按逻辑核心数（14 核）预分配线程缓冲区，
# 分配失败重试 10 次后直接放弃（"Memory allocation still failed after 10 retries"）。
# 本包只有行情拉取与点位计算，无重线性代数，单线程无损失。
# 必须在 numpy/pandas（经 data/akshare.py 懒加载）首次使用前设置，故放包入口。
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
