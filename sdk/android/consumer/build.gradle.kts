plugins {
    id("com.android.application")
    kotlin("android")
}

android {
    namespace = "com.syncap.example"
    compileSdk = 36
    if (providers.gradleProperty("testReleaseSdk").orNull == "true") {
        testBuildType = "release"
    }
    defaultConfig {
        applicationId = "com.syncap.example"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildTypes {
        getByName("release") {
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
            testProguardFiles("test-rules.pro")
            // Explicit test-only invocation: exercise R8 output on an emulator.
            if (providers.gradleProperty("testReleaseSdk").orNull == "true") {
                signingConfig = signingConfigs.getByName("debug")
                proguardFiles("test-host-rules.pro")
            }
        }
    }
    packaging { jniLibs { pickFirsts += "lib/**/libc++_shared.so" } }
}

kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }

dependencies {
    if (providers.gradleProperty("usePublishedSdk").orNull == "true") {
        implementation("com.syncap:syncap-sdk:0.1.0")
        implementation("com.syncap:syncap-bluetooth-android:0.1.0")
        implementation("com.syncap:syncap-media-android:0.1.1")
    } else {
        implementation(project(":library"))
        implementation(project(":bluetooth"))
        implementation(project(":media"))
    }
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    androidTestImplementation("androidx.test:runner:1.7.0")
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
    // AndroidX Test references these annotations; R8 also resolves test bytecode.
    androidTestImplementation("com.google.errorprone:error_prone_annotations:2.30.0")
}
