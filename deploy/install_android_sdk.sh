#!/usr/bin/env bash
# Install the toolchain the app stage needs. Idempotent; safe to re-run.
#
#   bash deploy/install_android_sdk.sh
#
# WHY THIS IS A SCRIPT AND NOT A COMMAND TO PASTE. The app stage died on
# "keytool not found", and the fix was handed over as a 200-character
# one-liner. The serial console mangled it: unzip arrived as "un^zi^p" and
# the URL as "_l^ate^st.^zip", so curl 404ed and bash reported a command that
# does not exist. deploy_check.py's own header already records this - a noVNC
# console silently drops and reorders keystrokes on a large paste - and the
# answer there was the same as here: check the thing in, paste one short line.
#
# WHAT IT INSTALLS, and why each version is the one it is. The app template
# pins AGP 8.7.3 with jvmTarget 17 (apps/template-shift-journal), so the JDK
# floor is 17, and compileSdk 35 is READ FROM THE TEMPLATE below rather than
# written here, so this cannot drift from what the build actually asks for.
#
# It does NOT touch .env beyond adding ANDROID_HOME when absent. A second
# copy of a key is a real hazard on this box: pipeline._load_env takes the
# FIRST occurrence and python-dotenv takes the LAST, so a duplicate makes the
# console and the service disagree about the value.
set -uo pipefail

SDK_ROOT="${ANDROID_SDK_INSTALL_DIR:-/opt/android-sdk}"
APP_DIR="${APP_DIR:-/opt/company_registration}"
CMDLINE_ZIP="commandlinetools-linux-13114758_latest.zip"
CMDLINE_URL="https://dl.google.com/android/repository/${CMDLINE_ZIP}"
UNIT_DROPIN="/etc/systemd/system/company-registration.service.d/android.conf"

say()  { printf '\n=== %s\n' "$*"; }
ok()   { printf '    [ok] %s\n' "$*"; }
bad()  { printf '    [!!] %s\n' "$*"; }
die()  { bad "$*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this as root: it writes to ${SDK_ROOT} and /etc/systemd"

say "0. disk"
# The SDK is about 2G unpacked and Gradle's first build pulls roughly another
# 1G into ~/.gradle. Failing here beats failing half way through a 157M
# download with no space left.
AVAIL_K=$(df -Pk /opt | awk 'NR==2 {print $4}')
AVAIL_G=$(( AVAIL_K / 1024 / 1024 ))
if [ "$AVAIL_K" -lt 5242880 ]; then
  bad "only ${AVAIL_G}G free on /opt; this needs about 5G"
  die "free some space and re-run"
fi
ok "${AVAIL_G}G free on /opt"

say "1. JDK 17"
if ! command -v keytool >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq openjdk-17-jdk-headless \
    || die "could not install openjdk-17-jdk-headless"
fi
command -v keytool >/dev/null 2>&1 || die "keytool still not on PATH"
JAVA_LINE=$(java -version 2>&1 | head -1)
ok "${JAVA_LINE}"
command -v unzip >/dev/null 2>&1 || apt-get install -y -qq unzip \
  || die "could not install unzip"
ok "unzip present"

say "2. command-line tools"
SDKMANAGER="${SDK_ROOT}/cmdline-tools/latest/bin/sdkmanager"
if [ -x "$SDKMANAGER" ]; then
  ok "already installed at ${SDKMANAGER}"
else
  # Clear anything a previous mangled attempt left behind, so a half-written
  # tree cannot be mistaken for an installed one.
  rm -rf "${SDK_ROOT}/cmdline-tools"
  mkdir -p "${SDK_ROOT}/cmdline-tools" || die "cannot create ${SDK_ROOT}"
  TMP_ZIP="$(mktemp -d)/${CMDLINE_ZIP}"
  curl -fsSL -o "$TMP_ZIP" "$CMDLINE_URL" || die "download failed: ${CMDLINE_URL}"
  # Verify it is a zip before unpacking. A proxy error page arrives with a
  # 200 and would otherwise read as a successful download.
  file "$TMP_ZIP" 2>/dev/null | grep -qi zip \
    || unzip -tq "$TMP_ZIP" >/dev/null 2>&1 \
    || die "downloaded file is not a zip archive"
  ok "downloaded $(( $(stat -c %s "$TMP_ZIP") / 1024 / 1024 ))M"
  unzip -q "$TMP_ZIP" -d "${SDK_ROOT}/cmdline-tools" || die "unzip failed"
  mv "${SDK_ROOT}/cmdline-tools/cmdline-tools" "${SDK_ROOT}/cmdline-tools/latest" \
    || die "unexpected archive layout; sdkmanager needs cmdline-tools/latest/"
  rm -f "$TMP_ZIP"
  [ -x "$SDKMANAGER" ] || die "sdkmanager not where expected: ${SDKMANAGER}"
  ok "installed ${SDKMANAGER}"
fi

say "3. platform and build-tools"
# Read compileSdk from the template so this cannot go stale when it is bumped.
GRADLE_APP="${APP_DIR}/apps/template-shift-journal/app/build.gradle.kts"
COMPILE_SDK=""
if [ -r "$GRADLE_APP" ]; then
  COMPILE_SDK=$(grep -oE 'compileSdk[[:space:]]*=[[:space:]]*[0-9]+' "$GRADLE_APP" \
                | grep -oE '[0-9]+' | head -1)
fi
[ -n "$COMPILE_SDK" ] || die "could not read compileSdk from ${GRADLE_APP}"
ok "the template asks for android-${COMPILE_SDK}"

yes 2>/dev/null | "$SDKMANAGER" --sdk_root="$SDK_ROOT" --licenses >/dev/null 2>&1
"$SDKMANAGER" --sdk_root="$SDK_ROOT" \
  "platform-tools" "platforms;android-${COMPILE_SDK}" \
  "build-tools;${COMPILE_SDK}.0.0" \
  || die "sdkmanager could not install the packages"
[ -d "${SDK_ROOT}/platforms/android-${COMPILE_SDK}" ] \
  || die "platforms/android-${COMPILE_SDK} is still missing after install"
ok "platforms/android-${COMPILE_SDK} and build-tools present"

say "4. permissions"
# The service runs as a non-root user. Same fault that made delegate.py
# unreadable: root can read and exec what nobody else can, so an install done
# as root looks fine from this shell and fails inside the service.
chmod -R a+rX "$SDK_ROOT" || die "chmod failed"
GRADLEW="${APP_DIR}/apps/template-shift-journal/gradlew"
[ -f "$GRADLEW" ] && chmod a+rx "$GRADLEW" && ok "gradlew is a+rx"
ok "${SDK_ROOT} is readable and traversable by all"

say "5. ANDROID_HOME for the service"
# In the unit, not only .env: generate.py resolves ANDROID_HOME at import and
# .env is not reliably loaded before that happens.
mkdir -p "$(dirname "$UNIT_DROPIN")"
printf '[Service]\nEnvironment=ANDROID_HOME=%s\n' "$SDK_ROOT" > "$UNIT_DROPIN" \
  || die "could not write ${UNIT_DROPIN}"
ok "wrote ${UNIT_DROPIN}"
systemctl daemon-reload && ok "systemd reloaded"

if grep -q '^ANDROID_HOME=' "${APP_DIR}/.env" 2>/dev/null; then
  ok ".env already has ANDROID_HOME; left alone (a duplicate would make "
  ok "  the console and the service disagree about its value)"
else
  printf 'ANDROID_HOME=%s\n' "$SDK_ROOT" >> "${APP_DIR}/.env" \
    && ok "appended ANDROID_HOME to .env"
fi

systemctl restart company-registration && ok "service restarted"

say "done"
printf '    verify with:\n'
printf '    cd %s && xvfb-run -a .venv/bin/python deploy_check.py\n' "$APP_DIR"
