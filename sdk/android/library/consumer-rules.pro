# JNA reflects structure fields and native-interface method names.
-keep class com.sun.jna.** { *; }
-keep class * extends com.sun.jna.Structure { *; }
-keep class * implements com.sun.jna.Library { *; }
-keep class * implements com.sun.jna.Callback { *; }
-keep class com.syncap.sdk.** { *; }
-dontwarn java.awt.**
