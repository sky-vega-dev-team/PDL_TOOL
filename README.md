# PDL_TOOL

Pantech 다운로드(PDL) 프로토콜로 개별 파티션을 **플래시 / 백업**하고, EFS 백업·복원,
기기정보 읽기, 리부팅까지 하는 개발용 도구. Vega / IM-100(ef71s, MSM8937) 대상.

PDL(긴급 다운로드) 모드의 기기와 직접 USB로 통신한다. PDL 전체 이미지를 매번
만들어 올리는 대신, `aboot` / `boot` / `recovery` 같은 raw 파티션을 하나씩 바로
써 넣는 fastboot 식 개발 루프를 제공한다. (Pantech LK `app/pdl` 소스를 역분석해 구현)

기기 자체 검증과 **협력**한다(우회 아님): 올바른 섹션 체크섬을 보내 부트로더의
RAM 사전검사와 eMMC 읽기-재검사가 모두 통과해야 하고, 새 파티션 테이블을 보내지
않아 기기의 기존 GPT를 유지하며, secure-boot 상태는 건드리지 않는다.

## 기능

- **파티션 플래시** — raw 이미지를 지정 파티션에 기록(체크섬 검증). `--boot recovery`로
  misc BCB를 써서 리커버리로 부팅.
- **파티션 백업** — 파티션 전체를 파일로 읽어옴(`DL_PARTITION_DUMP`, 청크·전체 체크섬 검증).
- **기기정보 읽기** — `phoneinfo` 파티션에서 모델명/바이너리버전/빌드일시/EFS크기/
  secure model id 등을 읽어 표시.
- **EFS 백업/복원** — `modemst1` / `modemst2` / `fsg` 3개를 한 번에.
- **리붓** — 일반 / 리커버리(둘 다 PDL에서 직접) / 부트로더 / EDL / PDL(일반부팅 후 adb 전환).
- 백업물은 전부 `backup/` 폴더로 저장.

## 요구사항

- Python 3, `pyusb` (`pip install pyusb`), libusb
- raw USB 접근 → `sudo` 또는 `10a9:1104` udev 규칙
- 기기를 PDL 모드로: `adb reboot oem-33` (또는 하드웨어 키 조합)
- 기기 파티션 레이아웃이 담긴 `gpt_main0.bin` (해당 펌웨어에서 추출)

## 사용법

### 대화형 메뉴 (권장)

```sh
./pdlmenu.sh [gpt_main0.bin 경로]
# 또는
GPT=/경로/gpt_main0.bin ./pdlmenu.sh
```

메인 메뉴: 파티션 플래시 / 파티션 백업 / 기기정보 읽기 / EFS 백업·복원 / 리붓.
플래시·백업 후에는 [일반부팅 리붓 / 메인메뉴로] 중 선택.

### CLI

```sh
python3 pdl_tool.py list  --gpt gpt_main0.bin                 # 파티션/크기 보기
python3 pdl_tool.py flash recovery=twrp.img --gpt gpt_main0.bin --boot recovery -y
python3 pdl_tool.py backup modemst1=backup/modemst1.img --gpt gpt_main0.bin
python3 pdl_tool.py menu  --gpt gpt_main0.bin                 # 대화형 메뉴
```

## 동작 원리 / 하드웨어 메모

역분석으로 확인한, 이 기기에서 중요한 제약:

- **PDL 세션은 부팅당 1회.** 기기측 `dloadpdl_main`은 `DL_FINISH` 또는 에러로 루프를
  빠져나오면 무조건 재부팅한다. 그래서 플래시·백업·EFS·기기정보를 **하나의 세션**에서
  처리해야 하며(메뉴가 단일 프로세스인 이유), 끝낼 때 리부팅으로 빠져나간다.
- **덤프(읽기)는 512바이트 블록 정렬 필수.** 기기측 `mmc_read`가 정렬을 요구해서,
  정렬되지 않은 크기를 읽으면 기기가 크래시·재부팅한다. 백업은 512정렬로 읽는다.
- **리부팅 사유는 eMMC가 아니라 IMEM 레지스터**에 저장된다. 따라서 PDL에서 직접
  가능한 건 일반부팅(`DL_FINISH`)과 리커버리(misc BCB `boot-recovery`, offset 0) 뿐.
  부트로더/EDL/PDL은 일반부팅 후 `adb reboot bootloader|edl|oem-33`로 전환한다.
- 체크섬 = 이미지의 리틀엔디안 u32 워드 합. 섹션 ID = GPT 엔트리 unique-GUID의 앞 4바이트.

## 주의

- `flash` / `EFS 복원`은 파티션을 덮어쓴다. EFS(`modemst1/2`, `fsg`)는 모뎀/보정/
  IMEI 관련 영역이므로 복원 전 백업을 보관할 것.
- sparse 이미지(system/userdata)는 이 도구로 쓰지 않는다 — QFIL 또는 PDL 리팩을 사용.
- 자신의 기기에 대해서만 사용할 것.
