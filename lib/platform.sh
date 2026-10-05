# shellcheck shell=bash disable=SC2034  # the PLAT_* facts are for the scripts that source this
# lib/platform.sh: what is this machine, and can dgx-serve serve on it?
#
# platform_detect sets PLAT_* facts and PLATFORM, one of:
#   dgx       NVIDIA DGX Spark (GB10, unified memory)
#   linux     Linux with one or more NVIDIA GPUs
#   windows   Windows, through WSL2, with an NVIDIA GPU passed through
#   mac       Apple Silicon Mac
#   unsupported   with PLAT_REASON saying why
#
# Every probe goes through PATH (uname, nvidia-smi, sysctl, sw_vers, docker,
# cmd.exe) or reads files under RACK_SYSROOT, so tests can fake any machine.
# Bash 3.2, GNU or BSD.

_plat_file() { cat "${RACK_SYSROOT:-}$1" 2>/dev/null || true; }

platform_detect() {
  PLAT_OS=$(uname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' || true)
  PLAT_ARCH=$(uname -m 2>/dev/null || true)
  PLAT_KERNEL=$(uname -r 2>/dev/null || true)
  PLAT_WSL=0 PLAT_DGX=0 PLAT_CPU="" PLAT_OS_NAME="" PLAT_OS_VERSION="" PLAT_WINDOWS_BUILD=""
  PLAT_GPU_COUNT=0 PLAT_GPU_NAMES="" PLAT_GPU_MEM_MB="" PLAT_GPU_CC="" PLAT_DRIVER="" PLAT_CUDA=""
  PLAT_MEM_MB=0 PLAT_METAL_MB=0 PLAT_UNIFIED=0
  PLAT_DOCKER=0 PLAT_NVIDIA_RUNTIME=0 PLAT_INIT=none
  local osr
  case "$PLAT_OS" in
    linux)
      case "$(_plat_file /proc/version) $PLAT_KERNEL" in *[Mm]icrosoft*|*WSL*) PLAT_WSL=1 ;; esac
      case "$(_plat_file /sys/class/dmi/id/product_name) $(_plat_file /sys/class/dmi/id/product_family)" in
        *DGX_Spark*|*"DGX Spark"*) PLAT_DGX=1 ;;
      esac
      PLAT_MEM_MB=$(_plat_file /proc/meminfo | awk '/^MemTotal:/{printf "%d", $2/1024}')
      osr=$(_plat_file /etc/os-release)
      PLAT_OS_NAME=$(printf '%s\n' "$osr" | sed -n 's/^PRETTY_NAME=//p' | head -1 | tr -d '"')
      PLAT_OS_VERSION=$(printf '%s\n' "$osr" | sed -n 's/^VERSION_ID=//p' | head -1 | tr -d '"')
      PLAT_CPU=$(_plat_file /proc/cpuinfo | awk -F: '/^model name/{gsub(/^[ \t]+/,"",$2); print $2; exit}' || true)
      [ -d "${RACK_SYSROOT:-}/run/systemd/system" ] && PLAT_INIT=systemd
      if [ "$PLAT_WSL" = 1 ] && have cmd.exe; then
        # "Microsoft Windows [Version 10.0.26100.4652]": the build says 10 or 11
        PLAT_WINDOWS_BUILD=$(cmd.exe /c ver 2>/dev/null | tr -d '\r' | sed -n 's/.*Version [0-9]*\.[0-9]*\.\([0-9]*\).*/\1/p' | head -1 || true)
      fi
      ;;
    darwin)
      local bytes wired
      bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
      PLAT_MEM_MB=$(( ${bytes:-0} / 1048576 ))
      PLAT_UNIFIED=1
      PLAT_CPU=$(sysctl -n machdep.cpu.brand_string 2>/dev/null || true)
      PLAT_OS_VERSION=$(sw_vers -productVersion 2>/dev/null || true)
      PLAT_OS_NAME="macOS $PLAT_OS_VERSION"
      PLAT_INIT=launchd
      # The GPU may wire at most this much: macOS's default is about two thirds
      # of RAM up to 36 GB and three quarters above, unless iogpu.wired_limit_mb
      # was raised by hand.
      wired=$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)
      if [ "${wired:-0}" -gt 0 ] 2>/dev/null; then PLAT_METAL_MB=$wired
      elif [ "$PLAT_MEM_MB" -le 36864 ]; then PLAT_METAL_MB=$(( PLAT_MEM_MB * 2 / 3 ))
      else PLAT_METAL_MB=$(( PLAT_MEM_MB * 3 / 4 )); fi
      ;;
  esac

  if have nvidia-smi; then
    local q line name mem cc drv
    q=$(nvidia-smi --query-gpu=name,memory.total,compute_cap,driver_version --format=csv,noheader,nounits 2>/dev/null) \
      || q=$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits 2>/dev/null | sed 's/, *\([^,]*\)$/, ?, \1/') \
      || q=""
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      name=$(printf '%s' "$line" | cut -d, -f1 | sed 's/^ *//;s/ *$//')
      mem=$(printf '%s' "$line" | cut -d, -f2 | tr -d ' []')
      cc=$(printf '%s' "$line" | cut -d, -f3 | tr -d ' ')
      drv=$(printf '%s' "$line" | cut -d, -f4 | tr -d ' ')
      case "$mem" in ''|*[!0-9]*) mem=0 ;; esac          # [N/A] on unified memory
      PLAT_GPU_COUNT=$((PLAT_GPU_COUNT + 1))
      PLAT_GPU_NAMES="${PLAT_GPU_NAMES:+$PLAT_GPU_NAMES;}$name"
      PLAT_GPU_MEM_MB="${PLAT_GPU_MEM_MB:+$PLAT_GPU_MEM_MB,}$mem"
      PLAT_GPU_CC="${PLAT_GPU_CC:+$PLAT_GPU_CC,}$cc"
      PLAT_DRIVER=$drv
    done <<EOT
$q
EOT
    PLAT_CUDA=$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: *\([0-9.]*\).*/\1/p' | head -1 || true)
  fi
  [ "$PLAT_DGX" = 1 ] && PLAT_UNIFIED=1

  if have docker && docker info >/dev/null 2>&1; then
    PLAT_DOCKER=1
    docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q '"nvidia"' && PLAT_NVIDIA_RUNTIME=1
  fi
  # spark-2 serves with --gpus all while `docker info` lists no "nvidia"
  # runtime (measured): the toolkit's binaries are the better signal.
  if have nvidia-ctk || have nvidia-container-cli || have nvidia-container-runtime-hook; then
    PLAT_NVIDIA_RUNTIME=1
  fi

  PLATFORM=unsupported PLAT_REASON=""
  case "$PLAT_OS" in
    darwin)
      if [ "$PLAT_ARCH" = arm64 ]; then PLATFORM=mac
      else PLAT_REASON="an Intel Mac has no GPU dgx-serve can serve on: Macs need Apple Silicon"; fi ;;
    linux)
      if [ "$PLAT_WSL" = 1 ]; then
        if [ "$PLAT_GPU_COUNT" -gt 0 ]; then PLATFORM=windows
        else PLAT_REASON="WSL2 sees no NVIDIA GPU: install or update the NVIDIA driver on Windows (not inside WSL), then wsl --shutdown"; fi
      elif [ "$PLAT_DGX" = 1 ]; then PLATFORM=dgx
      elif [ "$PLAT_GPU_COUNT" -gt 0 ]; then PLATFORM=linux
      else PLAT_REASON="no NVIDIA GPU found (nvidia-smi): dgx-serve serves on NVIDIA GPUs or Apple Silicon"; fi ;;
    *) PLAT_REASON="unsupported operating system: ${PLAT_OS:-unknown}" ;;
  esac
}

# Memory the model may use, in MiB: unified memory on a Spark or a Mac (the
# Mac's Metal budget), the sum of GPU memory elsewhere.
platform_model_budget_mb() {
  case "$PLATFORM" in
    mac) printf '%s' "$PLAT_METAL_MB" ;;
    dgx) printf '%s' "$PLAT_MEM_MB" ;;
    *) printf '%s' "$PLAT_GPU_MEM_MB" | tr ',' '\n' | awk '{s+=$1} END{printf "%d", s}' ;;
  esac
}

# Is this machine inside the 1.0 support matrix? Sets PLAT_SUPPORTED (1/0)
# and PLAT_SUPPORT_NOTE. rack init refuses an unsupported machine with the note.
platform_support_check() {
  PLAT_SUPPORTED=0 PLAT_SUPPORT_NOTE=""
  case "$PLATFORM" in
    dgx) PLAT_SUPPORTED=1 ;;
    linux)
      case "$PLAT_OS_NAME" in
        Ubuntu*) case "$PLAT_OS_VERSION" in 22.04|24.04) PLAT_SUPPORTED=1 ;; esac ;;
      esac
      [ "$PLAT_SUPPORTED" = 1 ] || PLAT_SUPPORT_NOTE="supported Linux is Ubuntu 22.04 or 24.04 (this is ${PLAT_OS_NAME:-unknown})" ;;
    windows)
      case "$PLAT_OS_NAME" in Ubuntu*) case "$PLAT_OS_VERSION" in 24.04) PLAT_SUPPORTED=1 ;; esac ;; esac
      [ "$PLAT_SUPPORTED" = 1 ] || PLAT_SUPPORT_NOTE="supported WSL2 distribution is Ubuntu 24.04 (this is ${PLAT_OS_NAME:-unknown})"
      if [ "$PLAT_SUPPORTED" = 1 ] && [ -n "$PLAT_WINDOWS_BUILD" ] && [ "$PLAT_WINDOWS_BUILD" -lt 22000 ] 2>/dev/null; then
        PLAT_SUPPORTED=0; PLAT_SUPPORT_NOTE="Windows 11 is required (this is build $PLAT_WINDOWS_BUILD, Windows 10)"
      fi ;;
    mac)
      case "$PLAT_OS_VERSION" in
        1[4-9]*|2[0-9]*) PLAT_SUPPORTED=1 ;;
        *) PLAT_SUPPORT_NOTE="macOS 14 or later is required (this is ${PLAT_OS_VERSION:-unknown})" ;;
      esac ;;
    *) PLAT_SUPPORT_NOTE=$PLAT_REASON ;;
  esac
}

platform_describe() {
  local gib=$(( (PLAT_MEM_MB + 512) / 1024 )) first
  first=$(printf '%s' "$PLAT_GPU_NAMES" | cut -d';' -f1)
  case "$PLATFORM" in
    dgx) printf 'DGX Spark (%s, %s GiB unified memory)' "${first:-GB10}" "$gib" ;;
    linux) printf 'Linux with %s x %s (%s GiB GPU memory in total)' "$PLAT_GPU_COUNT" "$first" "$(( ($(platform_model_budget_mb) + 512) / 1024 ))" ;;
    windows) printf 'Windows (WSL2) with %s x %s (%s GiB GPU memory)' "$PLAT_GPU_COUNT" "$first" "$(( ($(platform_model_budget_mb) + 512) / 1024 ))" ;;
    mac) printf 'Mac (%s, %s GiB unified memory, about %s GiB usable for models)' "${PLAT_CPU:-Apple Silicon}" "$gib" "$(( (PLAT_METAL_MB + 512) / 1024 ))" ;;
    *) printf 'unsupported machine: %s' "$PLAT_REASON" ;;
  esac
}

platform_json() {
  platform_support_check
  printf '{"schema":%s,"platform":%s,"description":%s,"supported":%s,"support_note":%s,' \
    "$RACK_JSON_SCHEMA" "$(json_str "$PLATFORM")" "$(json_str "$(platform_describe)")" \
    "$(json_bool "$PLAT_SUPPORTED")" "$(json_str "$PLAT_SUPPORT_NOTE")"
  printf '"os":%s,"os_name":%s,"os_version":%s,"arch":%s,"kernel":%s,"wsl":%s,"windows_build":%s,"dgx":%s,"cpu":%s,' \
    "$(json_str "$PLAT_OS")" "$(json_str "$PLAT_OS_NAME")" "$(json_str "$PLAT_OS_VERSION")" "$(json_str "$PLAT_ARCH")" \
    "$(json_str "$PLAT_KERNEL")" "$(json_bool "$PLAT_WSL")" "$(json_num "$PLAT_WINDOWS_BUILD")" "$(json_bool "$PLAT_DGX")" \
    "$(json_str "$PLAT_CPU")"
  printf '"memory_mb":%s,"unified_memory":%s,"metal_budget_mb":%s,"model_budget_mb":%s,' \
    "$(json_num "$PLAT_MEM_MB")" "$(json_bool "$PLAT_UNIFIED")" "$(json_num "$PLAT_METAL_MB")" "$(json_num "$(platform_model_budget_mb)")"
  printf '"gpus":['
  local i=1 name mem cc sep=""
  while [ "$i" -le "$PLAT_GPU_COUNT" ]; do
    name=$(printf '%s' "$PLAT_GPU_NAMES" | cut -d';' -f"$i")
    mem=$(printf '%s' "$PLAT_GPU_MEM_MB" | cut -d, -f"$i")
    cc=$(printf '%s' "$PLAT_GPU_CC" | cut -d, -f"$i")
    printf '%s{"name":%s,"memory_mb":%s,"compute_capability":%s}' "$sep" "$(json_str "$name")" "$(json_num "$mem")" "$(json_str "$cc")"
    sep=,; i=$((i + 1))
  done
  printf '],"driver":%s,"cuda":%s,"docker":%s,"nvidia_container_runtime":%s,"init":%s,"reason":%s}\n' \
    "$(json_str "$PLAT_DRIVER")" "$(json_str "$PLAT_CUDA")" "$(json_bool "$PLAT_DOCKER")" \
    "$(json_bool "$PLAT_NVIDIA_RUNTIME")" "$(json_str "$PLAT_INIT")" "$(json_str "$PLAT_REASON")"
}
