# -*- coding: utf-8 -*-
"""cpu_limit.py — CV/数值库的 CPU 线程上限（2026-09-02 用户要求）。

cv2 的 parallel_for 与 numpy 的 BLAS（OpenBLAS/MKL）默认吃满全部核
（本机 16 核），滚动采集时 CPU 瞬间打满。统一限制为 6 核：

- BLAS 系环境变量（OMP/OPENBLAS/MKL/NUMEXPR/VECLIB）必须在 numpy 首次
  import **之前**设置才生效——本模块必须在入口最顶部调用（main.py），
  run.sh / systemd 单元里 export 同名变量做双保险（覆盖网关拉起的
  agent 子进程与手工脚本）；
- cv2.setNumThreads 随时可调，这里一并设置。

默认值 6，可用环境变量 WECHAT_AGENT_CV_THREADS 覆盖。
"""

import os

DEFAULT_THREADS = 6

_BLAS_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")


def apply(n=None):
    """设置 BLAS 环境变量（仅补缺，不覆盖已有值）+ cv2 线程数。
    返回实际生效的线程数。"""
    n = int(n or os.environ.get("WECHAT_AGENT_CV_THREADS")
            or DEFAULT_THREADS)
    for k in _BLAS_ENV:
        os.environ.setdefault(k, str(n))
    try:
        import cv2
        cv2.setNumThreads(n)
    except Exception:  # noqa: BLE001
        pass
    return n
