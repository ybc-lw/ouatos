from typing import Optional

__all__ = ['__version__', 'debug', 'cuda', 'git_version', 'hip', 'rocm', 'xpu']
__version__ = '2.16.0a0+git7805a5d'
debug = False
cuda: Optional[str] = None
git_version = '7805a5d4019f1d2502043c99352f490e99d6f458'
hip: Optional[str] = None
rocm: Optional[str] = None
xpu: Optional[str] = None
