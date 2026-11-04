#if !defined(TORCH_STABLE_ONLY) && !defined(TORCH_TARGET_VERSION)
#pragma once

#include <torch/csrc/utils/pybind.h>

namespace torch::profiler::impl {

// Registers the torch._C._profiler._cuspy submodule on `m` (the
// _profiler module). The GIL-free buffer pool, native decode worker, and
// metadata store these bindings expose live in cuspy_native.h.
void initCuspyBindings(pybind11::module& m);

} // namespace torch::profiler::impl

#else
#error "This file should not be included when either TORCH_STABLE_ONLY or TORCH_TARGET_VERSION is defined."
#endif  // !defined(TORCH_STABLE_ONLY) && !defined(TORCH_TARGET_VERSION)
