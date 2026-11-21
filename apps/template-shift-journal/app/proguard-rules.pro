# Add project specific ProGuard rules here.
# You can control the set of applied configuration files using the
# proguardFiles setting in build.gradle.kts.

# Keep Compose
# REMOVED: -keep class androidx.compose.** { *; }
#
# That rule kept every class and every member of the whole Compose toolkit,
# which switched tree-shaking off for the thing that makes up almost all of
# this app. Measured on the 16811331 bundle before removing it:
#
#   androidx/compose/material   11,447 classes in classes.dex
#     of which icons            11,400
#   the app actually uses           20 icons
#
# classes.dex was 17.45 MB raw / 5.60 MB compressed, and BUNDLE-METADATA's
# proguard.map was 176 MB raw / 11.22 MB compressed - 65% of the .aab - for
# the same reason: R8 maps everything it is told to keep.
#
# Compose does not need a keep rule. Every Compose artifact ships its own
# consumer ProGuard rules inside its AAR, and AGP keeps anything the
# manifest references, so MainActivity and the Application class survive
# without help. A blanket -keep on a UI toolkit is a cargo-culted rule that
# only ever removes optimisation.
#
# IF A RELEASE BUILD EVER CRASHES WITH ClassNotFoundException OR
# NoSuchMethodException, add a NARROW keep for exactly that class and write
# down why. Do not restore the blanket rule.

# NOTE: the gson rule below matches nothing - this project has no gson
# dependency. Left alone rather than deleted in a change about Compose.

# Keep data classes
-keepclassmembers class * {
    @com.google.gson.annotations.SerializedName <fields>;
}

# Keep enum classes
-keepclassmembers enum * {
    public static **[] values();
    public static ** valueOf(java.lang.String);
}