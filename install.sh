#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${XHH_REPO_URL:-https://github.com/lucklyt/xhh}"
BRANCH="${XHH_BRANCH:-main}"
TARBALL_URL="${REPO_URL}/archive/refs/heads/${BRANCH}.tar.gz"
INSTALL_DIR="${XHH_INSTALL_DIR:-$HOME/.local/share/xhh}"
BIN_DIR="${XHH_BIN_DIR:-$HOME/.local/bin}"

if [ -t 1 ]; then
  C_RESET='\033[0m'
  C_BOLD='\033[1m'
  C_GREEN='\033[0;32m'
  C_CYAN='\033[0;36m'
  C_YELLOW='\033[0;33m'
  C_RED='\033[0;31m'
else
  C_RESET=''
  C_BOLD=''
  C_GREEN=''
  C_CYAN=''
  C_YELLOW=''
  C_RED=''
fi

info() { printf "${C_CYAN}[xhh]${C_RESET} %s\n" "$*"; }
ok()   { printf "${C_GREEN}[✓]${C_RESET} %s\n" "$*"; }
warn() { printf "${C_YELLOW}[!]${C_RESET} %s\n" "$*"; }
err()  { printf "${C_RED}[✗]${C_RESET} %s\n" "$*" >&2; }

usage() {
  cat <<EOF
小黑盒社区 CLI (xhh) 安装脚本

用法:
  install.sh              从 GitHub 安装或更新 xhh
  install.sh --local      从当前本地源码目录安装 xhh
  install.sh --uninstall  卸载 xhh 与虚拟环境
  install.sh -h, --help   显示此帮助信息

环境变量:
  XHH_INSTALL_DIR   虚拟环境安装目录 (默认: ~/.local/share/xhh)
  XHH_BIN_DIR       可执行文件链接目录 (默认: ~/.local/bin)
  XHH_PYTHON        指定使用的 Python 解释器路径 (需 >= 3.9)
  XHH_BRANCH        指定拉取的 Git 分支 (默认: main)
EOF
}

MODE="remote"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --local)
      MODE="local"
      shift
      ;;
    --uninstall)
      MODE="uninstall"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      err "未知参数: $1"
      usage
      exit 1
      ;;
  esac
done

if [ "$MODE" = "uninstall" ]; then
  info "正在卸载 xhh..."
  if [ -L "$BIN_DIR/xhh" ] || [ -f "$BIN_DIR/xhh" ]; then
    rm -f "$BIN_DIR/xhh"
    ok "已删除 $BIN_DIR/xhh"
  fi
  if [ -d "$INSTALL_DIR" ]; then
    rm -rf "$INSTALL_DIR"
    ok "已删除 $INSTALL_DIR"
  fi
  ok "卸载完成（个人配置 ~/.config/xhh 已保留，如需清理可手动删除）"
  exit 0
fi

check_python() {
  local py="$1"
  [ -x "$py" ] || command -v "$py" >/dev/null 2>&1 || return 1
  "$py" -c 'import sys, venv, ensurepip; assert sys.version_info >= (3, 9)' >/dev/null 2>&1
}

find_python() {
  if [ -n "${XHH_PYTHON:-}" ]; then
    if check_python "$XHH_PYTHON"; then
      printf '%s\n' "$XHH_PYTHON"
      return 0
    fi
    err "指定的 XHH_PYTHON ($XHH_PYTHON) 不可用（需要 Python >= 3.9 且包含 venv/ensurepip 模块）"
    return 1
  fi

  local candidates=(
    "/opt/homebrew/bin/python3.14"
    "/opt/homebrew/opt/python@3.14/bin/python3.14"
    "/opt/homebrew/bin/python3.13"
    "/opt/homebrew/opt/python@3.13/bin/python3.13"
    "/opt/homebrew/bin/python3.12"
    "/opt/homebrew/opt/python@3.12/bin/python3.12"
    "/opt/homebrew/bin/python3.11"
    "/opt/homebrew/opt/python@3.11/bin/python3.11"
    "/opt/homebrew/bin/python3"
    "/usr/local/bin/python3.14"
    "/usr/local/bin/python3.13"
    "/usr/local/bin/python3.12"
    "/usr/local/bin/python3.11"
    "/usr/local/bin/python3"
    "python3.14"
    "python3.13"
    "python3.12"
    "python3.11"
    "python3.10"
    "python3.9"
    "python3"
  )

  for cand in "${candidates[@]}"; do
    local resolved="$cand"
    if [ ! -x "$resolved" ]; then
      resolved="$(command -v "$cand" 2>/dev/null || true)"
    fi
    if [ -n "$resolved" ] && check_python "$resolved"; then
      printf '%s\n' "$resolved"
      return 0
    fi
  done
  return 1
}

PYTHON_BIN="$(find_python || true)"
if [ -z "$PYTHON_BIN" ]; then
  err "未找到可用的 Python >= 3.9（需含 venv 与 ensurepip 模块）。"
  err "macOS 可运行: brew install python@3.12"
  err "Debian/Ubuntu 可运行: sudo apt-get install -y python3 python3-venv"
  exit 1
fi

PY_VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
info "使用 Python ${PY_VER} ($PYTHON_BIN)"

TMP_DIR=""
cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
}
trap cleanup EXIT

SRC_DIR=""
if [ "$MODE" = "local" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    SRC_DIR="$SCRIPT_DIR"
  elif [ -f "$PWD/pyproject.toml" ]; then
    SRC_DIR="$PWD"
  else
    err "本地目录未找到 pyproject.toml"
    exit 1
  fi
  info "使用本地源码目录: $SRC_DIR"
else
  TMP_DIR="$(mktemp -d)"
  SRC_DIR="$TMP_DIR/xhh-src"
  info "正在从 GitHub 下载小黑盒 CLI (${REPO_URL})..."

  CLONED=0
  if command -v git >/dev/null 2>&1; then
    if GIT_TERMINAL_PROMPT=0 git clone --depth 1 --branch "$BRANCH" "${REPO_URL}.git" "$SRC_DIR" >/dev/null 2>&1; then
      CLONED=1
    fi
  fi

  if [ "$CLONED" -eq 0 ]; then
    mkdir -p "$SRC_DIR"
    if command -v curl >/dev/null 2>&1; then
      curl -fsSL "$TARBALL_URL" | tar -xz -C "$SRC_DIR" --strip-components=1
    elif command -v wget >/dev/null 2>&1; then
      wget -qO- "$TARBALL_URL" | tar -xz -C "$SRC_DIR" --strip-components=1
    else
      err "需要 git、curl 或 wget 以下载源码"
      exit 1
    fi
  fi
fi

VENV_DIR="$INSTALL_DIR/venv"
mkdir -p "$INSTALL_DIR" "$BIN_DIR"

if [ ! -d "$VENV_DIR" ] || [ ! -x "$VENV_DIR/bin/python" ]; then
  info "正在创建独立虚拟环境 ($VENV_DIR)..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

info "正在安装 xhh 及依赖..."
"$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null 2>&1 || true
"$VENV_DIR/bin/pip" install --upgrade "$SRC_DIR"

ln -sf "$VENV_DIR/bin/xhh" "$BIN_DIR/xhh"
ok "已安装可执行命令: $BIN_DIR/xhh"

PATH_UPDATED=0
RC_FILE=""
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  if [ "$BIN_DIR" = "$HOME/.local/bin" ] && [ -z "${XHH_NO_MODIFY_PATH:-}" ]; then
    case "${SHELL:-}" in
      */zsh)  RC_FILE="$HOME/.zshrc" ;;
      */bash) RC_FILE="$HOME/.bashrc" ;;
      *)
        if [ -f "$HOME/.zshrc" ]; then
          RC_FILE="$HOME/.zshrc"
        elif [ -f "$HOME/.bashrc" ]; then
          RC_FILE="$HOME/.bashrc"
        fi
        ;;
    esac
    if [ -n "$RC_FILE" ]; then
      if [ ! -f "$RC_FILE" ] || ! grep -qs '\.local/bin' "$RC_FILE"; then
        printf '\n# Added by xhh installer\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC_FILE"
        PATH_UPDATED=1
        ok "已将 \$HOME/.local/bin 写入 $RC_FILE"
      fi
    fi
  fi
fi

VER_OUT="$("$BIN_DIR/xhh" --version 2>/dev/null || true)"
printf "\n${C_BOLD}${C_GREEN}🎉 小黑盒社区 CLI 安装成功！${C_RESET} (%s)\n\n" "${VER_OUT:-xhh}"

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  if [ "$PATH_UPDATED" -eq 1 ] && [ -n "$RC_FILE" ]; then
    warn "请先执行以下命令刷新 PATH（或重新打开终端）："
    printf "  source %s\n\n" "$RC_FILE"
  else
    warn "$BIN_DIR 当前不在 PATH 中，请添加："
    printf "  export PATH=\"%s:\$PATH\"\n\n" "$BIN_DIR"
  fi
fi

cat <<'EOF'
快速开始:
  xhh              启动全屏交互式 TUI
  xhh login        终端扫码登录小黑盒
  xhh feed         查看首页推荐流
  xhh hot          查看黑盒热议 / 热搜 / 热门游戏榜
  xhh topic CS2    查看指定游戏板块帖子
EOF
