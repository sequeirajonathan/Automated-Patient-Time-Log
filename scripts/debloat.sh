#!/usr/bin/env bash
# Strip carrier and vendor bloat from the caregiver's phone, reversibly.
#
# WHY THIS IS A SCRIPT AND NOT AN AFTERNOON.
#
# The previous phone was debloated by hand. Nothing was written down, and when
# it was replaced the whole exercise had to start again from nothing — which is
# what prompted this file. A phone gets replaced; the list of what should not
# be on it should not have to be rediscovered each time.
#
# NOTHING HERE IS PERMANENT. Every removal is `pm uninstall -k --user 0`, which
# removes the package for the phone's only user and LEAVES THE APK on the system
# partition. `restore` puts any of it back with `cmd package install-existing`,
# and a factory reset restores everything regardless. This is deliberately the
# reversible form: a phone that records visits for a living is not a place to
# find out the hard way that something was load-bearing.
#
# THE PROTECT LIST IS LOAD-BEARING AND IS TESTED. tests/test_debloat.py reads
# this file and fails if any package the controller actually depends on has
# fallen out of PROTECT — because the failure mode is not "the script broke",
# it is "HHAeXchange+ can no longer sign in and nobody knows why".
#
#   ./debloat.sh audit     every package on the phone, classified
#   ./debloat.sh plan      exactly what `apply` would remove, and nothing else
#   ./debloat.sh apply     remove them, recording each one
#   ./debloat.sh restore   put back everything this script removed
#
set -uo pipefail

ADB=${ADB:-adb}
RECORD=${RECORD:-/var/lib/aptlog/debloat-removed.txt}

# --------------------------------------------------------------- protect list
# Packages that must survive, and WHY. Every line here is something the
# controller reaches for by name; the test cross-checks the constants.
PROTECT=(
  # The care apps themselves (feed.CARE_APPS, autoentry.SUPPORTED).
  com.inmyteam.inmyteam
  com.hhaexchange.uma
  com.hhaexchange.caregiver
  com.tellus.evv.v2

  # HHAeXchange+ SIGNS IN THROUGH A CHROME CUSTOM TAB (macros.WEB_FLOW_HOST).
  # Remove Chrome and that app can never authenticate again. This is the single
  # least obvious entry on the list and the most expensive to get wrong.
  com.android.chrome

  # Sending the OTP onward is `service call isms` through this package
  # (sms.SEND_PKG). No Messages app is needed to RECEIVE — the code is read
  # from the provider — but this is how it goes back out to three phones.
  com.android.mms.service
  com.android.mms
  com.samsung.android.messaging

  # Telephony and the SMS store the code is read out of.
  com.android.phone
  com.android.providers.telephony
  com.android.server.telecom

  # The care apps are Play-distributed and check in with Play services; the
  # portal's push notifications ride FCM.
  com.google.android.gms
  com.google.android.gsf
  com.android.vending

  # WebView backs the Custom Tab and every in-app browser view.
  com.google.android.webview
  com.google.android.trichromelibrary

  # A phone with no input method cannot type an OTP into a field.
  com.samsung.android.honeyboard
  com.google.android.inputmethod.latin

  # System plumbing. Removing any of these bricks the device or the bridge.
  android
  com.android.systemui
  com.android.settings
  com.android.packageinstaller
  com.google.android.packageinstaller
  com.android.permissioncontroller
  com.google.android.permissioncontroller
  com.android.providers.settings
  com.android.providers.media
  com.android.providers.contacts
  com.android.providers.downloads
  com.android.shell
  com.android.certinstaller
  com.samsung.android.providers.contacts
  com.sec.android.app.launcher
  com.samsung.android.app.telephonyui
  com.samsung.android.incallui
  com.samsung.android.dialer
)

# ------------------------------------------------------------ removal targets
# Matched as prefixes against the installed list, so a vendor's whole family
# goes in one line. A package that also appears in PROTECT is never removed —
# the protect check runs last and wins.
BLOAT=(
  # The carrier. `com.metro.minus1` is the Sliide "Headlines" panel that owns
  # the home screen and that the containment watchdog keeps having to push
  # against; it is the reason this list starts here.
  com.metro.
  com.metropcs.
  com.sliide
  com.dti.
  com.aura.
  com.ironsource.

  # T-Mobile's telemetry collector, and ONLY that one.
  #
  # `com.tmobile.` as a prefix was the first draft and it is too greedy: it
  # also takes `com.tmobile.dm.cm`, `com.tmobile.dm.ms.services` and
  # `com.tmobile.pr.adapt`, which are carrier DEVICE MANAGEMENT and
  # provisioning. On this handset SMS is load-bearing — the whole OTP path
  # depends on sending and receiving it, and the phone is registered over
  # IWLAN, which is provisioned rather than intrinsic. Removing the thing that
  # provisions IMS to save a few megabytes would be trading the feature for
  # the cleanup.
  #
  # They may well be inert. Nobody has established that, and the way to find
  # out is not on the phone that has to text three people a code.
  com.tmobile.echolocate

  # Facebook's preinstalled stubs, which reinstall the real thing on demand.
  com.facebook.appmanager
  com.facebook.services
  com.facebook.system
  com.facebook.katana

  # Samsung's own extras. Nothing here is used to record a visit.
  com.samsung.android.bixby
  com.samsung.android.visionintelligence
  com.samsung.android.app.spage
  com.samsung.android.game
  com.samsung.android.arzone
  com.samsung.android.aremoji
  com.samsung.android.livestickers
  com.samsung.android.app.tips
  com.samsung.android.kidsinstaller
  com.samsung.android.app.appsedge
  com.samsung.android.service.aircommand
  com.samsung.android.mateagent
  com.samsung.android.shortcutbackupservice
  com.samsung.android.scloud
  com.samsung.android.app.watchmanager
  com.samsung.android.themestore
  com.samsung.android.app.sharelive
  com.samsung.android.voc
  com.samsung.sree
  com.sec.android.app.samsungapps
  com.sec.android.easyMover
  com.samsung.android.wellbeing
  com.samsung.android.forest

  # Google's media and social extras.
  com.google.android.youtube
  com.google.android.apps.youtube
  com.google.android.apps.tachyon
  com.google.android.apps.podcasts
  com.google.android.videos
  com.google.android.play.games
  com.google.android.apps.subscriptions
  com.google.android.music
  com.google.ar.
)

say() { printf '%s\n' "$*"; }
die() { printf 'debloat: %s\n' "$*" >&2; exit 1; }

device_ready() {
  local n
  n=$($ADB devices | tail -n +2 | grep -cw device)
  [ "${n:-0}" -ge 1 ] || die "no device over adb — plug the phone in and accept the debugging prompt"
  [ "${n:-0}" -le 1 ] || die "more than one device attached; set ANDROID_SERIAL and re-run"
}

installed() { $ADB shell pm list packages 2>/dev/null | sed 's/^package://' | tr -d '\r' | sort; }

is_protected() {
  local p=$1 keep
  for keep in "${PROTECT[@]}"; do [ "$p" = "$keep" ] && return 0; done
  return 1
}

is_bloat() {
  local p=$1 pat
  for pat in "${BLOAT[@]}"; do case "$p" in "$pat"*) return 0 ;; esac; done
  return 1
}

targets() {
  local p
  while read -r p; do
    [ -n "$p" ] || continue
    is_protected "$p" && continue
    is_bloat "$p" && printf '%s\n' "$p"
  done
}

case "${1:-plan}" in
  audit)
    device_ready
    installed | while read -r p; do
      if is_protected "$p";  then say "keep    $p"
      elif is_bloat "$p";    then say "REMOVE  $p"
      else                        say "leave   $p"
      fi
    done
    ;;

  plan)
    device_ready
    say "would remove:"
    installed | targets | sed 's/^/  /'
    say ""
    say "count: $(installed | targets | wc -l)"
    ;;

  apply)
    device_ready
    mkdir -p "$(dirname "$RECORD")"
    n=0
    while read -r p; do
      # Re-checked here as well as in `targets`. This is the line that
      # actually removes something, and it is the one worth being paranoid on.
      if is_protected "$p"; then say "refusing to remove protected $p"; continue; fi
      # STDIN CLOSED, OR THIS LOOP RUNS ONCE.
      #
      # `adb` reads from stdin, and inside a `while read` it swallows the rest
      # of the list — the first `apply` removed exactly one package of
      # thirty-five and reported success. Same trap as `ssh` in a read loop,
      # and it fails quietly in the direction of doing too little, which is
      # the only reason it was survivable.
      out=$($ADB shell pm uninstall -k --user 0 "$p" </dev/null 2>&1 | tr -d '\r')
      case "$out" in
        Success*) printf '%s\n' "$p" >> "$RECORD"; say "removed $p"; n=$((n+1)) ;;
        *)        say "skipped $p ($out)" ;;
      esac
    done < <(installed | targets)
    say ""
    say "removed $n packages; recorded in $RECORD"
    say "reboot the phone, then re-check the bridge before trusting it"
    ;;

  restore)
    device_ready
    [ -f "$RECORD" ] || die "nothing recorded at $RECORD"
    while read -r p; do
      [ -n "$p" ] || continue
      # </dev/null for the same reason as `apply` — restoring one package of
      # thirty-five is the worse half of that bug, because it looks like it
      # worked and leaves the phone half-stripped.
      out=$($ADB shell cmd package install-existing "$p" </dev/null 2>&1 | tr -d '\r')
      say "$p: $out"
    done < "$RECORD"
    ;;

  *)
    die "usage: debloat.sh {audit|plan|apply|restore}"
    ;;
esac
