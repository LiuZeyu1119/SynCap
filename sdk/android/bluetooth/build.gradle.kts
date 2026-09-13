plugins {
    id("com.android.library")
    kotlin("android")
    `maven-publish`
}

group = "com.syncap"
version = "0.1.0"
android {
    namespace = "com.syncap.sdk.bluetooth"
    compileSdk = 36
    defaultConfig { minSdk = 24; testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner" }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    publishing { singleVariant("release") { withSourcesJar() } }
}
kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }
dependencies {
    api("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.10.2")
    androidTestImplementation("androidx.test:runner:1.7.0")
    androidTestImplementation("androidx.test.ext:junit:1.3.0")
}
afterEvaluate {
    publishing {
        publications {
            create<MavenPublication>("release") {
                from(components["release"])
                artifactId = "syncap-bluetooth-android"
                pom { name.set("SynCap Android Bluetooth SDK") }
            }
        }
        repositories { maven { name = "sdkLocal"; url = uri(rootProject.layout.buildDirectory.dir("maven")) } }
    }
}
