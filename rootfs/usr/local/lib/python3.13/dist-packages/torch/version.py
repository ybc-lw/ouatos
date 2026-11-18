from typing import Optional

__all__ = ['__version__', 'debug', 'cuda', 'git_version', 'hip', 'rocm', 'xpu']
__version__ = '2.14.0+cpu'
debug = False
cuda: Optional[str] = None
git_version = '2b3ec34829036a65cd9d1398ea72a0167dc37470'
hip: Optional[str] = None
rocm: Optional[str] = None
xpu: Optional[str] = None
