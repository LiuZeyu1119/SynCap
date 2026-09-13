@echo off
setlocal
set SDK_JAVA=java.exe
if defined JAVA_HOME set "SDK_JAVA=%JAVA_HOME%\bin\java.exe"
"%SDK_JAVA%" -Xmx64m -Xms64m -Dorg.gradle.appname=gradlew -classpath "%~dp0gradle\wrapper\gradle-wrapper.jar" org.gradle.wrapper.GradleWrapperMain %*
exit /b %ERRORLEVEL%
