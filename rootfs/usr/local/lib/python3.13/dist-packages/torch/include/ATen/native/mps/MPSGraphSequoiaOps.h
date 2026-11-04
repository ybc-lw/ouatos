#if !defined(TORCH_STABLE_ONLY) && !defined(TORCH_TARGET_VERSION)
#pragma once

#include <c10/macros/Macros.h>

C10_DIAGNOSTIC_PUSH_AND_IGNORED_IF_DEFINED("-Wdeprecated-declarations")
C10_DIAGNOSTIC_PUSH_AND_IGNORED_IF_DEFINED("-Wobjc-property-no-attribute")
#include <MetalPerformanceShadersGraph/MetalPerformanceShadersGraph.h>
C10_DIAGNOSTIC_POP()
C10_DIAGNOSTIC_POP()

#if !defined(__MAC_26_0) &&             \
    (!defined(MAC_OS_X_VERSION_26_0) || \
     (MAC_OS_X_VERSION_MIN_REQUIRED < MAC_OS_X_VERSION_26_0))
#define MTLLanguageVersion4_0 ((MTLLanguageVersion)((4 << 16) + 0))
#endif // Building for target older than MacOS-26

#else
#error "This file should not be included when either TORCH_STABLE_ONLY or TORCH_TARGET_VERSION is defined."
#endif  // !defined(TORCH_STABLE_ONLY) && !defined(TORCH_TARGET_VERSION)
