#if !defined(TORCH_STABLE_ONLY) && !defined(TORCH_TARGET_VERSION)
#pragma once

#include <c10/util/IntegerDivider.h>

namespace at::cuda::detail {

using c10::DivMod;
using c10::IntDivider;

}  // namespace at::cuda::detail

#else
#error "This file should not be included when either TORCH_STABLE_ONLY or TORCH_TARGET_VERSION is defined."
#endif  // !defined(TORCH_STABLE_ONLY) && !defined(TORCH_TARGET_VERSION)
