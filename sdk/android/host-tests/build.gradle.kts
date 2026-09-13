plugins { kotlin("jvm") }

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) }
    sourceSets.main { kotlin.srcDir("../library/src/main/java") }
}

dependencies {
    implementation("net.java.dev.jna:jna:5.18.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.10.2")
    testImplementation(kotlin("test-junit"))
}

tasks.test {
    // The generated Kotlin can stay identical while the Rust implementation changes.
    // Include native artifacts so Gradle cannot reuse stale successful test results.
    inputs.files(rootProject.fileTree("../target/release") {
        include("libsyncap_ffi.dylib", "libsyncap_ffi.so", "syncap_ffi.dll")
    }).withPropertyName("nativeSdk")
    systemProperty("jna.library.path", rootProject.file("../target/release").absolutePath)
    testLogging { events("passed", "skipped", "failed") }
}
