#!/usr/bin/env bash
# actions/setup-go 下载失败时的兜底：GitHub go-versions 发行包与 storage.googleapis.com
# 都不可用时（2026-11-08 出现 504 后回退 403），用 Runner 预装的任一 Go，经模块代理
# 下载官方工具链（golang.org/toolchain），并由 sum.golang.org 校验摘要。
# 用法：setup-go-fallback.sh <版本号，如 1.26.5 | go.mod 路径>
set -euo pipefail

spec="${1:?需要 Go 版本号或 go.mod 路径}"
if [[ -f "$spec" ]]; then
  version=$(sed -n 's/^toolchain go//p' "$spec" | head -n1)
  [[ -n "$version" ]] || version=$(sed -n 's/^go //p' "$spec" | head -n1)
else
  version="$spec"
fi
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
  echo "::error::无法识别 Go 版本：${version}"
  exit 1
fi

base=""
if command -v go >/dev/null 2>&1; then
  base=$(command -v go)
else
  base=$(ls -d "${RUNNER_TOOL_CACHE:-/opt/hostedtoolcache}"/go/*/x64/bin/go 2>/dev/null | sort -V | tail -n1 || true)
fi
if [[ -z "$base" || ! -x "$base" ]]; then
  echo "::error::Runner 未预装 Go，无法经模块代理兜底安装 ${version}"
  exit 1
fi

base_version=$(GOTOOLCHAIN=local "$base" env GOVERSION 2>/dev/null || echo 未知)

# 工具链只经官方模块代理获取并强制校验，不走 direct，也不关闭 GOSUMDB。
export GOPROXY=https://proxy.golang.org
export GOSUMDB=sum.golang.org
export GOTOOLCHAIN="go${version}"
goroot=$("$base" env GOROOT)
actual=$("$goroot/bin/go" env GOVERSION)
if [[ "$actual" != "go${version}" ]]; then
  echo "::error::兜底安装得到 ${actual}，期望 go${version}"
  exit 1
fi

echo "$goroot/bin" >> "$GITHUB_PATH"
echo "GOTOOLCHAIN=go${version}" >> "$GITHUB_ENV"
echo "setup-go 下载失败，已经模块代理安装 ${actual}（基础 Go：${base_version}）"
