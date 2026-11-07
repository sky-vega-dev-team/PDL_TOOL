#!/usr/bin/env bash
# Pantech PDL 대화형 메뉴 런처 (IM-100 / ef71s)
#
#   파티션 플래시 / 파티션 백업 / 기기정보 / EFS 백업·복원 / 리붓 을
#   하나의 PDL 세션으로 처리한다. (PDL은 세션당 한 번만 진입 가능하므로
#   메뉴 전체가 python 단일 프로세스로 돈다 - 끝낼 때 리부팅으로 빠져나감)
#
# 사용:
#   ./pdlmenu.sh [gpt경로]
#   GPT=/경로/gpt_main0.bin BACKUPDIR=/경로/backup ./pdlmenu.sh
#
# 폰을 먼저 PDL 모드로:  adb reboot oem-33  (또는 하드웨어 키 조합)

set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/pdl_tool.py"

# GPT 소스: 1) 인자  2) $GPT  3) 기본(IM-100S_S0228122/gpt_main0.bin)
GPT="${1:-${GPT:-$HERE/../IM-100S_S0228122/gpt_main0.bin}}"
BACKUPDIR="${BACKUPDIR:-$HERE/backup}"
VID=10a9; PID=1104

if [ ! -f "$GPT" ]; then
  echo "GPT 파일을 찾을 수 없음: $GPT"
  echo "사용법: $0 [gpt_main0.bin 경로]   (또는 GPT=... 환경변수)"
  exit 1
fi

# PDL 장치 확인 (lsusb 없을 수 있어 python으로)
echo "PDL 장치($VID:$PID) 확인 중..."
if ! python3 - "$VID" "$PID" <<'EOF'
import sys, usb.core
vid, pid = int(sys.argv[1], 16), int(sys.argv[2], 16)
sys.exit(0 if usb.core.find(idVendor=vid, idProduct=pid) else 1)
EOF
then
  echo
  echo "PDL 모드 장치가 안 보입니다. 폰을 PDL 모드로 넣으세요:"
  echo "    adb reboot oem-33"
  echo "그리고 다시 실행하세요."
  exit 1
fi

# root 권한 필요(raw USB). 이미 root면 그대로, 아니면 sudo.
RUN=(python3 "$PY" menu --gpt "$GPT" --backupdir "$BACKUPDIR")
if [ "$(id -u)" -ne 0 ]; then
  RUN=(sudo python3 "$PY" menu --gpt "$GPT" --backupdir "$BACKUPDIR")
fi

exec "${RUN[@]}"
