#if !defined(TORCH_STABLE_ONLY) && !defined(TORCH_TARGET_VERSION)
#pragma once

#include <c10/core/Device.h>
#include <c10/macros/Export.h>

namespace c10 {
struct TensorImpl;
}

namespace at {

TORCH_API void set_and_normalize_fake_device(
    c10::TensorImpl* impl,
    c10::Device device);

} // namespace at

#else
#error "This file should not be included when either TORCH_STABLE_ONLY or TORCH_TARGET_VERSION is defined."
#endif  // !defined(TORCH_STABLE_ONLY) && !defined(TORCH_TARGET_VERSION)
