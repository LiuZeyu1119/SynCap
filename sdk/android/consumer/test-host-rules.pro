# Only enabled for -PtestReleaseSdk=true; never part of the published SDK rules.
# AndroidX Test lives in a separately shrunk APK. The shared Kotlin runtime is
# supplied by the target APK, including ABI that only the test runner calls.
# Preserve it here to avoid removed LazyKt/Lambda classes and reordered Intrinsics
# parameters; this is an instrumentation-host concession, not a production rule.
-keep class kotlin.** { *; }
-keep class kotlinx.coroutines.** { *; }
# AppCompat now supplies Tracing in the host APK; AndroidJUnitRunner calls its ABI.
-keep class androidx.tracing.** { *; }
