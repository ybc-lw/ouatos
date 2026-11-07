#if !defined(TORCH_STABLE_ONLY) && !defined(TORCH_TARGET_VERSION)
#pragma once

namespace c10::cuda::CUDAGraphMemory {

// Owns CUDA Graph-specific capture state for the native allocator. Block
// lifecycle and conditional-capture policy are added only when CCA starts
// delegating them.
class CaptureTracker {
 public:
  void captureBegin();
  void captureEnd();
  bool hasActiveCaptures() const {
    return active_captures_ != 0;
  }

 private:
  int active_captures_{0};
};

} // namespace c10::cuda::CUDAGraphMemory

#else
#error "This file should not be included when either TORCH_STABLE_ONLY or TORCH_TARGET_VERSION is defined."
#endif  // !defined(TORCH_STABLE_ONLY) && !defined(TORCH_TARGET_VERSION)
