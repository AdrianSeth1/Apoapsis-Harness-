#!/usr/bin/env bash
# Report, and optionally stop, any llama-server already holding a given model.
#
#   resident_model_server.sh report <model-path>
#   resident_model_server.sh stop   <model-path>
#
# Prints one "<pid>\t<command line>" line per match. Matching is on the
# process's own command line naming that exact model file, never on a port:
# two copies of the same weights cannot both be resident, so a process whose
# command line names this model IS the conflict. A llama-server serving a
# different model is reported by neither mode and never signalled.
#
# This lives in a file rather than being passed to `bash -c` through
# `wsl.exe`, which does not preserve newlines in a relayed argument and turns
# any readable multi-line script into one malformed line.
set -uo pipefail

MODE="${1:?usage: resident_model_server.sh <report|stop|restore> <model-path|command>}"
TARGET="${2:?usage: resident_model_server.sh <report|stop|restore> <model-path|command>}"

if test "${MODE}" = "restore"; then
  # `setsid`, and then wait before returning. Each `wsl.exe` invocation is its
  # own session and WSL reaps that session's processes on exit, so a server
  # backgrounded and left unattended is started and killed within the same
  # second. setsid detaches it; the sleep gives the detach time to complete
  # before this shell -- and with it the session -- goes away.
  setsid bash -c "${TARGET}" </dev/null >/dev/null 2>&1 &
  sleep 3
  exit 0
fi

PIDS=""
for COMM in /proc/[0-9]*/comm; do
  test -r "${COMM}" || continue
  test "$(cat "${COMM}" 2>/dev/null)" = "llama-server" || continue
  PID="${COMM#/proc/}"
  PID="${PID%/comm}"
  test -r "/proc/${PID}/cmdline" || continue
  CMDLINE="$(tr '\0' ' ' < "/proc/${PID}/cmdline" 2>/dev/null)"
  case "${CMDLINE}" in
    *"${TARGET}"*)
      printf '%s\t%s\n' "${PID}" "${CMDLINE}"
      PIDS="${PIDS} ${PID}"
      ;;
  esac
done

if test "${MODE}" = "stop" && test -n "${PIDS}"; then
  # TERM, then a bounded wait, then KILL. A model server that will not release
  # the GPU has to go: the run that follows cannot start while it holds the
  # weights, and reporting success having left it alive would be a lie.
  kill ${PIDS} 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    REMAINING=""
    for PID in ${PIDS}; do
      test -d "/proc/${PID}" && REMAINING="${REMAINING} ${PID}"
    done
    test -z "${REMAINING}" && break
    sleep 1
  done
  test -n "${REMAINING}" && kill -9 ${REMAINING} 2>/dev/null || true
fi

exit 0
