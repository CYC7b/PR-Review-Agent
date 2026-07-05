# PR Review Agent 沙箱镜像构建脚本
# 用法: ./docker/sandbox/build.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_NAME="pr-review-sandbox"
IMAGE_TAG="latest"

echo "构建沙箱镜像 ${IMAGE_NAME}:${IMAGE_TAG} ..."
docker build -t "${IMAGE_NAME}:${IMAGE_TAG}" -f "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}"

echo "完成。可用以下命令测试:"
echo "  docker run --rm --user reviewer --read-only --tmpfs /tmp --tmpfs /workspace --network=none ${IMAGE_NAME}:${IMAGE_TAG} python --version"
