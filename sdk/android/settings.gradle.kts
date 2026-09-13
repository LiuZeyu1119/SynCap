pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        maven { url = uri("build/maven") }
        google()
        mavenCentral()
    }
}

rootProject.name = "syncap-android-sdk"
include(":library", ":consumer", ":host-tests", ":media", ":bluetooth")
