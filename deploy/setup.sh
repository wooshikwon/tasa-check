#!/bin/bash
# 서버 초기 세팅 스크립트 (Ubuntu 22.04 ARM)
set -e

echo "=== 시스템 패키지 업데이트 ==="
sudo apt update && sudo apt upgrade -y

echo "=== uv 설치 ==="
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

echo "=== 프로젝트 디렉토리 생성 ==="
sudo mkdir -p /opt/tasa-check
sudo chown ubuntu:ubuntu /opt/tasa-check

echo "=== 코드 클론 ==="
git clone git@github.com:wooshikwon/tasa-check.git /opt/tasa-check

echo "=== 의존성 설치 ==="
cd /opt/tasa-check
uv sync

echo "=== data 디렉토리 생성 ==="
mkdir -p /opt/tasa-check/data

echo "=== .env 파일 ==="
if [ ! -f /opt/tasa-check/.env ]; then
    cp /opt/tasa-check/.env.example /opt/tasa-check/.env
    echo "[!] /opt/tasa-check/.env 파일을 편집하여 환경변수를 입력하세요"
fi

echo "=== systemd 서비스 등록 ==="
sudo cp /opt/tasa-check/deploy/tasa-check.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tasa-check

echo "=== 완료 ==="
echo ".env 파일 편집 후 'sudo systemctl start tasa-check' 로 시작하세요"
echo "로그 확인: journalctl -u tasa-check -f"
