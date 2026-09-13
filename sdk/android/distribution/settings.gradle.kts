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
        maven { url = uri("../maven") }
        google()
        mavenCentral()
    }
}

rootProject.name = "syncap-sdk-standalone-example"
include(":consumer")
