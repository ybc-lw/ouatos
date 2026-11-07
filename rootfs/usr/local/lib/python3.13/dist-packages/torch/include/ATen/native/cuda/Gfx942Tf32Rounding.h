#if !defined(TORCH_STABLE_ONLY) && !defined(TORCH_TARGET_VERSION)
#pragma once

#include <ATen/core/Tensor.h>

#ifdef USE_ROCM
namespace at::native {

Tensor round_gfx942_tf32_rne_cuda(const Tensor& input);

} // namespace at::native
#endif // USE_ROCM

#else
#error "This file should not be included when either TORCH_STABLE_ONLY or TORCH_TARGET_VERSION is defined."
#endif  // !defined(TORCH_STABLE_ONLY) && !defined(TORCH_TARGET_VERSION)
