#!/bin/sh
# Installs baitbook on macOS or Linux. In a terminal:
#
#   curl -LsSf https://raw.githubusercontent.com/shahidee44/baitbook/main/install.sh | sh
#
# 1. installs uv (https://docs.astral.sh/uv) if it isn't there;
# 2. picks the build of llama-cpp-python for this machine: Metal (Apple silicon), CUDA (NVIDIA),
#    ROCm (AMD, when ROCm is set up), Vulkan (any other GPU) or CPU;
# 3. installs baitbook with it (`uv tool install`), in an environment of its own with Python 3.12;
# 4. puts the `baitbook` command on PATH;
# 5. runs `baitbook setup`: it looks for the models and ComfyUI, and adds the app icon.
# Run it again to upgrade. Your settings and data are left alone.
#
# Environment variables that change what it does:
#   BAITBOOK_GPU=cuda|hip|vulkan|metal|cpu  use this build, don't look at the GPU (or name a wheel
#                                           index: cu118, cu124, cu125, cu130, cu132, rocm72, hip-radeon)
#   BAITBOOK_SOURCE=<folder or URL>         install from here instead of GitHub (a local checkout, say)
#   BAITBOOK_NO_SETUP=1                     don't run `baitbook setup` at the end
#   BAITBOOK_NO_MODIFY_PATH=1               don't change PATH (or your shell's startup files)
# uv's own variables (UV_TOOL_DIR, UV_TOOL_BIN_DIR, UV_PYTHON_INSTALL_DIR, ...) work as usual.
#
# POSIX sh, since it's piped into `sh`. Everything runs from main(), on the last line, so a
# download that's cut short runs nothing.

set -u

REPO=https://github.com/shahidee44/baitbook
WHEELS=https://abetlen.github.io/llama-cpp-python/whl

if [ -t 1 ]; then
    HI=$(printf '\033[1;36m'); ERR=$(printf '\033[1;31m'); OFF=$(printf '\033[0m')
else
    HI=; ERR=; OFF=
fi

say() { printf '%s==> %s%s\n' "$HI" "$*" "$OFF"; }
note() { printf '    %s\n' "$*"; }
fail() {
    printf '\n%sbaitbook was not installed: %s%s\n' "$ERR" "$*" "$OFF" >&2
    exit 1
}
has() { command -v "$1" >/dev/null 2>&1; }

download() {
    if has curl; then
        curl -LsSf "$1"
    elif has wget; then
        wget -qO- "$1"
    else
        echo "neither curl nor wget is installed" >&2
        return 1
    fi
}

# uv on PATH, or where astral's installer puts it (not on this shell's PATH yet): sets UV
find_uv() {
    UV=$(command -v uv 2>/dev/null) && [ -n "$UV" ] && return 0
    for dir in "${UV_INSTALL_DIR:-}" "${UV_INSTALL_DIR:+$UV_INSTALL_DIR/bin}" "${XDG_BIN_HOME:-}" \
               "${XDG_DATA_HOME:+$XDG_DATA_HOME/../bin}" "$HOME/.local/bin" "${CARGO_HOME:-$HOME/.cargo}/bin"; do
        if [ -n "$dir" ] && [ -x "$dir/uv" ]; then
            UV="$dir/uv"
            PATH="$dir:$PATH"
            export PATH
            return 0
        fi
    done
    UV=
    return 1
}

# "12.8" -> 1208, so versions compare as numbers
version_number() {
    printf '%s\n' "$1" | awk -F. '{ printf "%d\n", $1 * 100 + $2 }'
}

# glibc 2.35 or newer: what the CUDA and ROCm wheels are built for (manylinux_2_35)
glibc_at_least_2_35() {
    glibc=$(getconf GNU_LIBC_VERSION 2>/dev/null | sed -n 's/^glibc \([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')
    [ -n "$glibc" ] && [ "$(version_number "$glibc")" -ge 235 ]
}

# the PCI vendors of the GPUs Linux knows about: 0x10de NVIDIA, 0x1002 AMD, 0x8086 Intel
gpu_vendors() {
    cat /sys/class/drm/card*/device/vendor 2>/dev/null | sort -u
}

vulkan_loader() {
    for f in /usr/lib/x86_64-linux-gnu/libvulkan.so.1 /lib/x86_64-linux-gnu/libvulkan.so.1 \
             /usr/lib64/libvulkan.so.1 /usr/lib/libvulkan.so.1; do
        [ -e "$f" ] && return 0
    done
    { ldconfig -p 2>/dev/null || /sbin/ldconfig -p 2>/dev/null; } | grep -q 'libvulkan\.so\.1'
}

nvidia_driver() {
    has nvidia-smi && nvidia-smi -L 2>/dev/null | grep -q '^GPU [0-9]'
}

# sets GPU (cuda, hip, vulkan, metal, cpu or source) from what this machine has
detect_gpu() {
    case "$OS" in
    Darwin)
        if [ "$ARCH" = arm64 ]; then
            GPU=metal
        elif [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; then
            fail "this terminal runs under Rosetta (as x86_64). Open a native terminal and run this again."
        else
            GPU=source
        fi
        ;;
    Linux)
        vendors=$(gpu_vendors | tr '\n' ' ' | sed 's/ *$//')
        [ -n "$vendors" ] && note "found GPUs from PCI vendors: $vendors"
        if [ "$ARCH" != x86_64 ]; then
            GPU=cpu
            note "there are only CPU builds for $ARCH"
        elif nvidia_driver; then
            GPU=cuda
            note "NVIDIA driver answers: $(nvidia-smi -L 2>/dev/null | head -n 1)"
        elif echo "$vendors" | grep -q 0x1002 && [ -e /dev/kfd ] && { has rocminfo || [ -d /opt/rocm ]; }; then
            GPU=hip
        elif echo "$vendors" | grep -q -e 0x10de -e 0x1002 -e 0x8086 && vulkan_loader; then
            GPU=vulkan
        else
            GPU=cpu
        fi
        if [ "$GPU" = cuda ] || [ "$GPU" = hip ]; then
            if ! glibc_at_least_2_35; then
                note "the $GPU builds need glibc 2.35 or newer (Ubuntu 22.04, Debian 12, Fedora 36 or later)"
                if [ "$ARCH" = x86_64 ] && vulkan_loader; then GPU=vulkan; else GPU=cpu; fi
            fi
        fi
        ;;
    esac
}

# sets BACKEND from GPU=cuda: the wheel index for the newest CUDA the driver runs
pick_cuda() {
    cuda=''
    cc=''
    if has nvidia-smi; then
        cuda=$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: *\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' | head -n 1)
        cc=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -n 1 | tr -d ' ')
    fi
    case "$cc" in [0-9]*.[0-9]*) ;; *) cc= ;; esac
    if [ -z "$cuda" ]; then
        BACKEND=cu124
        note "couldn't read the driver's CUDA version; trying CUDA 12.4"
        return
    fi
    note "the driver runs CUDA $cuda${cc:+, compute capability $cc}"
    # wheels for 0.3.36: cu118, cu121-cu125, cu130, cu132; the CUDA 13 ones need compute capability 7.5+
    v=$(version_number "$cuda")
    if [ "$v" -ge 1300 ] && [ -n "$cc" ] && [ "$(version_number "$cc")" -ge 705 ]; then BACKEND=cu130
    elif [ "$v" -ge 1205 ]; then BACKEND=cu125
    elif [ "$v" -ge 1200 ]; then BACKEND=cu124
    elif [ "$v" -ge 1108 ]; then BACKEND=cu118
    else fail "the NVIDIA driver only runs CUDA $cuda. Update it, or run this again with BAITBOOK_GPU=vulkan."
    fi
}

main() {
    ORIG_PATH=$PATH  # the caller's PATH, before uv's folder is added to it here
    OS=$(uname -s)
    ARCH=$(uname -m)
    case "$OS" in
    Darwin | Linux) ;;
    *) fail "this script is for macOS and Linux ($OS here). On Windows, use install.ps1." ;;
    esac

    # ---- 1. uv ----
    if find_uv; then
        say "Using uv: $UV"
    else
        say "Installing uv, the Python package manager that installs baitbook (https://docs.astral.sh/uv)"
        if [ "${BAITBOOK_NO_MODIFY_PATH:-}" = 1 ]; then
            download https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh
        else
            download https://astral.sh/uv/install.sh | sh
        fi
        find_uv || fail "uv didn't install. Install it by hand (https://docs.astral.sh/uv/getting-started/installation/), then run this again."
        say "Installed uv: $UV"
    fi

    # ---- 2. which llama-cpp-python build ----
    say "Choosing the llama-cpp-python build for this machine"
    GPU=$(printf '%s' "${BAITBOOK_GPU:-}" | tr '[:upper:]' '[:lower:]' | tr -d ' ')
    if [ -n "$GPU" ]; then
        note "BAITBOOK_GPU=$GPU"
    else
        detect_gpu
    fi
    case "$GPU" in
    cuda)
        [ "$OS" = Linux ] || fail "there are no CUDA builds for macOS."
        pick_cuda
        WHAT="NVIDIA CUDA"
        ;;
    hip | rocm | amd)
        [ "$OS" = Linux ] || fail "there are no ROCm builds for macOS."
        BACKEND=rocm72; WHAT="AMD ROCm 7.2"
        ;;
    vulkan)
        [ "$OS" = Linux ] || fail "there are no Vulkan builds for macOS; Apple silicon uses metal."
        BACKEND=vulkan; WHAT="Vulkan"
        ;;
    metal)
        [ "$OS" = Darwin ] && [ "$ARCH" = arm64 ] || fail "metal is for Macs with Apple silicon."
        BACKEND=metal; WHAT="Apple Metal"
        ;;
    cpu)
        if [ "$OS" = Darwin ] && [ "$ARCH" != arm64 ]; then GPU=source; else BACKEND=cpu; WHAT="CPU only (slow for big models)"; fi
        ;;
    cu[0-9]* | rocm[0-9]* | hip-radeon)
        BACKEND=$GPU; WHAT=$GPU
        ;;
    source) ;;
    *) fail "BAITBOOK_GPU=$GPU isn't one of cuda, hip, vulkan, metal, cpu." ;;
    esac
    if [ "$GPU" = source ]; then
        # an Intel Mac: no prebuilt wheel, so uv compiles llama-cpp-python from PyPI's source
        BACKEND=
        note "there's no prebuilt llama-cpp-python for Intel Macs: it's compiled during the install (several minutes)"
        xcode-select -p >/dev/null 2>&1 || fail "compiling needs Apple's command line tools: run \`xcode-select --install\`, then this again."
    else
        note "build: $WHAT, from $WHEELS/$BACKEND"
        note "not right? run it again piped to \`BAITBOOK_GPU=vulkan sh\` (or cuda, hip, metal, cpu)"
    fi

    # ---- 3. baitbook ----
    src=${BAITBOOK_SOURCE:-}
    if [ -z "$src" ]; then
        src="$REPO/archive/refs/heads/main.zip"  # needs no git, unlike git+https://
    elif [ -d "$src" ]; then
        src="file://$(cd "$src" && pwd -P | sed 's/ /%20/g')"
    fi
    say "Installing baitbook from $src"
    note "the first time, this downloads Python 3.12 and a few hundred MB of packages"
    set -- tool install --python 3.12 --force --reinstall "baitbook[llm] @ $src"
    [ -n "$BACKEND" ] && set -- "$@" --extra-index-url "$WHEELS/$BACKEND"
    "$UV" "$@" </dev/null || fail "uv couldn't install baitbook (the reason is above)."

    # ---- 4. PATH ----
    bin=$("$UV" tool dir --bin) || fail "uv couldn't say where it put baitbook."
    exe="$bin/baitbook"
    [ -x "$exe" ] || fail "uv says it installed baitbook, but there's no $exe."
    new_terminal=
    case ":$ORIG_PATH:" in
    *":$bin:"*) ;;
    *)
        if [ "${BAITBOOK_NO_MODIFY_PATH:-}" = 1 ]; then
            note "$bin isn't on PATH (BAITBOOK_NO_MODIFY_PATH is set): add it yourself to type \`baitbook\`"
        else
            say "Adding $bin to your PATH"
            # with the caller's PATH: update-shell does nothing if the folder is on PATH already
            PATH="$ORIG_PATH" "$UV" tool update-shell </dev/null ||
                note "if a new terminal can't find \`baitbook\`, add $bin to PATH yourself"
            new_terminal=1
        fi
        case ":$PATH:" in *":$bin:"*) ;; *) PATH="$bin:$PATH"; export PATH ;; esac
        ;;
    esac
    version=$("$exe" --version </dev/null) || fail "baitbook was installed but doesn't start: try $exe --version"
    say "Installed $version"

    # ---- 5. setup ----
    if [ "${BAITBOOK_NO_SETUP:-}" = 1 ]; then
        note "skipping \`baitbook setup\` (BAITBOOK_NO_SETUP=1)"
    elif (exec </dev/tty) 2>/dev/null; then
        # stdin is this script (curl | sh), so setup asks its questions on the terminal
        say "Running baitbook setup"
        "$exe" setup </dev/tty || note "setup has more to do: run \`baitbook setup\` again once the files it names are in place."
    else
        say "Running baitbook setup --check (there's no terminal to ask questions on)"
        "$exe" setup --check </dev/null || note "setup has more to do: run \`baitbook setup\` in a terminal."
    fi

    # ---- 6. what's next ----
    echo
    say "Done. Next:"
    note "baitbook            open the app (or click baitbook among your apps)"
    note "baitbook setup      check the models and ComfyUI again, add the app icon"
    note "baitbook status     where your settings and data live"
    note "baitbook --help     everything else"
    [ -n "$new_terminal" ] && note "Open a new terminal first (or run: export PATH=\"$bin:\$PATH\"): this one doesn't know the new PATH yet."
    note "To upgrade, run the install command again."
}

main "$@"
