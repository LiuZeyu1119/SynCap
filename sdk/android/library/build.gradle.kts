plugins {
    id("com.android.library")
    kotlin("android")
    `maven-publish`
}

group = "com.syncap"
version = "0.1.0"

android {
    namespace = "com.syncap.sdk"
    compileSdk = 36
    defaultConfig {
        minSdk = 24
        consumerProguardFiles("consumer-rules.pro")
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    publishing { singleVariant("release") { withSourcesJar() } }
}

kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }

dependencies {
    // The Android AAR carries libjnidispatch.so; the ordinary JNA JAR does not.
    api("net.java.dev.jna:jna:5.18.1@aar")
    api("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.10.2")
}

val verifyNativeLibraries by tasks.registering {
    doLast {
        listOf("arm64-v8a", "x86_64").forEach { abi ->
            val nativeLibrary = file("src/main/jniLibs/$abi/libsyncap_ffi.so")
            check(nativeLibrary.isFile && nativeLibrary.length() > 0) {
                "Missing $nativeLibrary; first run sdk/scripts/build-android.sh."
            }
        }
        check(fileTree("src/main/java/com/syncap/sdk").matching { include("*.kt") }.files.isNotEmpty()) {
            "Missing generated Kotlin bindings; first run sdk/scripts/build-android.sh."
        }
    }
}
tasks.named("preBuild") { dependsOn(verifyNativeLibraries) }

afterEvaluate {
    publishing {
        publications {
            create<MavenPublication>("release") {
                from(components["release"])
                artifactId = "syncap-sdk"
                pom {
                    name.set("SynCap Android SDK")
                    description.set("Device control, capture and verified resumable export using the shared SynCap Rust core")
                }
            }
        }
        repositories {
            maven {
                name = "sdkLocal"
                url = uri(rootProject.layout.buildDirectory.dir("maven"))
            }
        }
    }
}
