# kingst-la 설계 — sigrok-cli 호환 로직 분석기 CLI

- 작성: 2026-10-15
- 범위: 1차 (캡처). 2차 이후 항목은 1절 "1차에서 제외" 참고
- 상태: 1차 구현·실기 검증 완료 (9절)

## 1. 목표와 범위

Kingst LA5016 로직 분석기를 macOS에서 **pyusb + numpy만으로** 제어하는 Python CLI `kla`(프로젝트·패키지 `kingst-la`, import 이름 `kingst_la`)를 만든다. 처음 이름은 `la5016`이었고, 다른 Kingst LA 모델도 추가할 수 있는 구조라 2026-10-17에 바꿨다.
옵션 체계와 출력 형식은 **sigrok-cli와 호환**한다. libsigrok·sigrok-cli·KingstVIS는 실행 시 필요 없다
(KingstVIS.app은 펌웨어 추출에만 1회 쓴다).

프로토콜은 libsigrok `src/hardware/kingst-la2016` 드라이버(`protocol.c`, `protocol.h`, `api.c`)를 근거로 옮긴다.

### 1차 범위 (이 문서)

- KingstVIS.app에서 FX2 펌웨어·FPGA 비트스트림 추출
- 장치 초기화 (FX2 펌웨어 업로드 → 재열거 → EEPROM 모델 판별 → FPGA 업로드)
- SDRAM 캡처 모드 (`REG_CAPT_MODE = 0x00`) 캡처·다운로드·RLE 해제
- sigrok-cli 호환 옵션 (5절), 출력 형식 `bits`, `hex`, `csv`, `vcd`, `binary`

### 1차에서 제외 (2차 이후)

| 항목 | 비고 |
|---|---|
| 프로토콜 디코더 `-P` | 순수 Python `sigrokdecode` 호환 계층으로 sigrok 디코더 원본을 그대로 불러오는 방향. 1차 파이프라인의 Sink로 붙인다 |
| 연속 스트리밍 `-c continuous=on` | 4.5절의 경계를 지켜 두면 소스와 USB 수신부만 추가하면 된다 |
| PWM 출력 | 2차에 `-c` 설정 키로 추가 |
| `-i` 파일 입력, `srzip` 출력 | 5차 (23절) |

## 2. 결정 사항

| 항목 | 결정 | 근거 |
|---|---|---|
| 라이선스 | **GPL-3.0-or-later** | GPL-3 sigrok 드라이버를 옮긴다 |
| 패키지·명령 이름 | `kingst-la` (`kingst_la`), 명령 `kla` | 모델에 묶이지 않는 이름. Kingst와 무관한 비공식 도구임을 README에 밝힌다 |
| USB 접근 | **pyusb 동기 I/O** | SDRAM 모드는 캡처 종료 후 다운로드이므로 실시간성이 불필요 |
| 내부 데이터 | numpy `uint16` 샘플 청크의 iterator | 수백 MB 캡처도 메모리에 전부 올리지 않는다. 출력과 디코더가 같은 인터페이스를 쓴다 |
| 펌웨어 배포 | **저장소에 넣지 않는다** (public/private 모두) | Kingst 저작물. 사용자가 자기 KingstVIS.app에서 추출 |
| 펌웨어 위치 | macOS `~/Library/Application Support/kingst-la/firmware/`, Linux `$XDG_DATA_HOME`(기본 `~/.local/share`)`/kingst-la/firmware/`, Windows `%LOCALAPPDATA%\kingst-la\firmware\` | 저장소 밖 |
| 플랫폼 | 1차 검증은 macOS. 코드는 OS 전용 처리 없이 작성 (펌웨어 경로·libusb 위치만 분기) | Linux는 udev 규칙, Windows는 WinUSB 드라이버가 필요 — 실기 확인 후 README에 안내 |
| 문턱 전압 | sigrok과 동일: 이산값 0.4/0.6/0.9/1.2/1.4/2.0/2.5/4.0 V, 기본 1.4 V | 연속값·−4~+4 V 확장은 실기 확인 후 |
| sigrok-cli와 다른 점 | 5.4절에 모아 둔다 | 그 외는 sigrok-cli와 같게 동작해야 한다 |
| Python | ≥ 3.9, 개발 환경 3.13 | |

## 3. 모듈 구성

```
src/kingst_la/
├── __init__.py
├── __main__.py      python -m kingst_la
├── cli.py           sigrok-cli 호환 argparse, 모듈 조립, 종료 코드
├── options.py       -c key=value, SI 접미사, --channels, --triggers, --time 파싱 (순수 함수)
├── firmware.py      KingstVIS.app → Qt rcc 테이블 탐색 → (zlib) → Intel HEX→blob → 로컬 설치
├── usb.py           pyusb 래퍼. usb를 import하는 유일한 모듈. Transport 인터페이스 정의
├── device.py        protocol.c 포팅: 초기화, 설정, 캡처, 다운로드 → raw 바이트 iterator
├── rle.py           raw 바이트 → uint16 샘플 청크 (순수 함수)
├── pipeline.py      Meta, Sink 프로토콜
└── output/
    ├── __init__.py  이름 → Sink 클래스 등록
    ├── bits.py  hex.py  csv.py  vcd.py  binary.py
```

의존 방향: `cli → device → usb`, `cli → rle`, `cli → output → pipeline`.
`options`, `rle`, `output`, `pipeline`은 USB를 모르므로 장비 없이 테스트한다.
`device`는 `usb.Transport`에만 의존하므로 `FakeTransport`로 테스트한다.

### 3.1 `usb.Transport` (인터페이스)

```python
class Transport(Protocol):
    def ctrl_in(self, request: int, value: int, index: int, length: int) -> bytes: ...
    def ctrl_out(self, request: int, value: int, index: int, data: bytes = b"") -> None: ...
    def bulk_write(self, ep: int, data: bytes) -> None: ...
    def bulk_read_stream(self, ep: int, nbytes: int, chunk: int) -> Iterator[bytes]: ...
    def close(self) -> None: ...
```

- 모든 vendor control 요청은 `bmRequestType = VENDOR | DEVICE`, sigrok의 `ctrl_in`/`ctrl_out`과 같은 형태.
- `bulk_read_stream`이 bulk 수신의 유일한 진입점이다. 1차는 512 KiB 동기 `read()` 반복.
  스트리밍 추가 시 이 함수만 비동기 구현(python-libusb1)으로 교체/추가한다.
- 장치 열거·FX2 펌웨어 업로드(0xA0 CPUCS)·재열거 대기는 `usb.py`의 모듈 함수로 둔다.

### 3.2 `pipeline`

```python
@dataclass
class Meta:
    samplerate: int                 # Hz, 장비에 실제 설정된(effective) 값
    channels: list[Channel]         # 활성 채널 (index, name)
    # 총 샘플 수·트리거 위치는 두지 않는다 (스트리밍에서도 Sink가 동작해야 함)

class Sink(Protocol):
    def begin(self, meta: Meta) -> None: ...
    def feed(self, samples: np.ndarray) -> None: ...  # uint16, 비트 i = CHi
    def trigger(self) -> None: ...                    # 다음 feed의 첫 샘플이 트리거 지점
    def end(self) -> None: ...
```

트리거 위치는 RLE를 풀어 봐야 알 수 있으므로 헤더가 아니라 **스트림 안의 이벤트**로 전달한다
(libsigrok의 `SR_DF_TRIGGER`와 같은 방식). RLE 디코더가 트리거 지점에서 청크를 나누고
그 사이에 `trigger()`를 호출한다. 출력 형식은 libsigrok처럼 그 위치에 트리거 표시를 남긴다.

## 4. 데이터 흐름

### 4.1 열기·초기화 (`Device.open()`)

1. VID:PID `77a1:01a2` 열거. `-d ...:conn=<bus>.<addr>`로 선택, 없으면 첫 장치.
   선택한 장치의 **bus + port path**를 기억한다 (재열거 후 USB 주소가 바뀌므로).
2. 장치 디스크립터의 `iProduct`가 `LA2016_IPRODUCT_INDEX`(2)와 다르면 FX2 펌웨어가 없는 상태:
   `kingst-la-<pid:04x>.fw` 업로드 → sigrok과 같은 타이밍으로 재탐색한다.
   업로드 후 `RENUM_GONE_DELAY_MS`(1800 ms) 대기 → 200 ms 간격으로 같은 bus + port path,
   `iProduct == 2`인 장치를 찾는다 → 업로드 시점부터 3000 ms(`RENUM_CHECK_PERIOD_MS`) 안에
   못 찾으면 장치 오류.
3. configuration 1, interface 0 claim.
4. EEPROM(`CMD_EEPROM 0xa2`)에서 magic/magic2 읽기 → 모델 테이블 → FPGA stem
   (`la5016a1` / `la5016a2`). 테이블에 없으면 magic 값을 보여 주고 장치 오류.
5. `check_fpga_bitstream()` 실패 시 `upload_fpga_bitstream()`
   (`CMD_FPGA_INIT`, EP2 bulk, 4096 바이트 패딩). 이어 `enable_fpga_bitstream()`.
6. `REG_RUN` 읽어 `0x85eX`가 아니면 경고. `CMD_BULK_RESET`.

펌웨어 파일이 없으면 USB에 접근하기 전에 중단하고 `kla --extract-firmware`를 안내한다.

### 4.2 설정·캡처 (`Device.acquire(cfg) -> (Meta, Iterator[bytes])`)

1. `set_threshold_voltage` → `REG_CAPT_MODE = 0x00` → `set_trigger_config` → `set_sample_config`
   (sigrok 공식 그대로: 기준 클럭 800 MHz / 16비트 분주, 500 MHz는 divider 1 특수 처리,
   captureratio로 pre-trigger 샘플·메모리 계산).
   샘플 수 한도: `--samples` 값, 없으면 `LA2016_NUM_SAMPLES_MAX`(10 G) — 즉 `--samples`도
   `--time`도 없으면 메모리가 찰 때까지 또는 Ctrl-C까지 캡처한다 (sigrok과 동일).
2. `set_run_mode(RUN)`.
3. `REG_RUN` 50 ms 간격 폴링, idle이 될 때까지.
   `--time` 초과 또는 **첫 Ctrl-C** 시 `set_run_mode(HALT)` 후 idle까지 계속 폴링하고,
   그때까지 캡처된 데이터를 정상적으로 다운로드·출력한다.
   **다운로드 중 Ctrl-C**(캡처 중 두 번째 Ctrl-C 포함)는 중단이다: HALT 후 종료하고,
   `-o` 파일은 지운다. stdout으로 이미 내보낸 부분은 그대로 둔다.
4. `get_capture_info()` (`REG_SAMPLING`): `n_rep_packets`, `n_rep_packets_before_trigger`, `write_pos`.
5. `n_bytes = n_rep_packets / packets_per_chunk * transfer_size`,
   `read_pos = (write_pos − n_bytes) & 0xFFFFFFFF` (C의 u32 wrap과 동일하게).
6. `CMD_BULK_RESET` → `REG_BULK`에 `(read_pos, n_bytes)` u32 LE → `CMD_BULK_START`.
7. `bulk_read_stream(EP6, n_bytes, 512 KiB)`의 바이트 청크를 그대로 yield. 진행률은 stderr.
8. 다운로드가 끝나면 (성공·중단 모두) `set_run_mode(HALT)`.

### 4.2.1 닫기 (`Device.close()`)

`set_run_mode(HALT)` → interface release → 장치 닫기. 오류가 나도 남은 단계를 계속한다.
FPGA는 끄지 않는다 (sigrok `WITH_DEINIT_IN_CLOSE 0`): 다음 실행에서 `check_fpga_bitstream()`이
통과해 비트스트림 업로드를 건너뛸 수 있다.

### 4.3 RLE 해제 (`rle.Decoder`)

LA5016(16 ch) 레이아웃: **transfer 16 B = 5 × (u16 핀값 LE, u8 반복수) + u8 시퀀스 번호.**

- 상태를 가진 디코더 객체가 raw 청크를 받아 `uint16` 샘플 청크를 yield한다
  (transfer가 raw 청크 경계에 걸쳐도 처리).
- numpy로 벡터화: 16 B 행으로 reshape → 핀값·반복수 열 추출 → `np.repeat`.
- 출력 청크 상한(기본 4 M 샘플). 반복수 큰 구간이 있어도 메모리 폭주 없음.
- 트리거 (트리거를 설정한 경우에만): 처음 `n_rep_packets_before_trigger`개 패킷을 내보낸 직후
  청크를 나누고 `Sink.trigger()`를 호출한다. 값이 0이면 첫 샘플 앞에서 호출한다.
  트리거를 설정하지 않았으면 이 레지스터 값은 무시한다.
- `--samples N` 도달 시 잘라내고 종료.
- 시퀀스 번호 연속성 검사, 어긋나면 경고 (sigrok은 무시하지만 실기 검증에 유용).

### 4.4 출력

`Sink.begin(meta)` → `feed(chunk)` 반복 (그 사이 최대 1회 `trigger()`) → `end()`. `-C`로 고른 채널만 출력한다.
`-w`가 있으면 트리거 이전 샘플을 버린다.

### 4.5 스트리밍 확장을 위한 경계 (1차에서 지킬 것)

1. 샘플 소스는 모드와 무관하게 "uint16 청크 iterator"를 낸다. 레이아웃 해석(RLE / 스트림 de-interleave)은 각자 순수 함수.
2. `Meta`와 Sink는 총 길이를 몰라도 동작한다. 트리거는 스트림 이벤트이므로 없어도 된다.
3. bulk 수신은 `Transport.bulk_read_stream` 하나 뒤에 숨긴다.

## 5. CLI

### 5.1 옵션

| 옵션 | 예 | 비고 |
|---|---|---|
| `--scan` | | `kingst-la2016:conn=<bus>.<addr> - Kingst LA5016 with 16 channels: CH0 … CH15` |
| `--show` | `-d kingst-la2016 --show` | samplerate 목록, 트리거 종류, threshold 범위, captureratio |
| `-d, --driver` | `-d kingst-la2016:conn=1.5` | 드라이버 이름은 `kingst-la2016`만. 생략 시 첫 장치 |
| `-c, --config` | `-c samplerate=100m:captureratio=10:voltage_threshold=1.4-1.4` | sigrok 키 이름, `:` 구분, `k/m/g` 접미사 |
| `--samples` / `--time` | `--samples 1m`, `--time 100ms` | 동시 지정 시 사용 오류. `--time`의 단위 없는 값은 ms이며, 실제 samplerate로 샘플 수로 바꿔 장비가 정확한 길이에서 스스로 멈추게 한다 (5.4절). 둘 다 없으면 메모리가 찰 때까지 또는 Ctrl-C까지 |
| `-C, --channels` | `-C CH0,CH3-CH5`, `-C CH3=SDA` | 이름 바꾸기 지원 |
| `-t, --triggers` | `-t CH0=r,CH1=1` | `0/1/r/f`. 엣지는 1개만. `e`는 오류 |
| `-w, --wait-trigger` | | 트리거 이전 샘플 버림 |
| `-o, --output-file` | `-o cap.vcd` | 없으면 stdout. `-O` 없으면 확장자로 형식 추정, 모르면 srzip (5.4절) |
| `-O, --output-format` | `-O csv:time=true` | 기본 `bits`. 형식 옵션은 `:key=value` |
| `-l, --loglevel` | `-l 5` | 0–5. 5에서 USB 요청 덤프 |
| `-V, --version` | | |
| `--extract-firmware [PATH]` | | 확장 옵션 (5.4절). PATH 기본 `/Applications/KingstVIS.app` |

설정 키:

| 키 | 값 | 기본 |
|---|---|---|
| `samplerate` | 800 MHz/65536 ~ 500 MHz 사이 아무 값. 장비는 `800M // (800M // 요청값)`(최대 500 MHz)으로 동작하므로, 요청값과 다르면 실제 값을 경고로 알린다. `Meta.samplerate`에는 실제 값을 넣는다. `--show`는 sigrok과 같은 1-2-5 목록을 보여 준다 | 500 MHz (모델 최대) |
| `captureratio` | 0–100 (%) | 50 |
| `voltage_threshold` | `x-x` 형식, x ∈ {0.4, 0.6, 0.9, 1.2, 1.4, 2.0, 2.5, 4.0}. 아래값만 쓰고, lo ≠ hi이거나 목록에 없으면 사용 오류 | 1.4-1.4 |

### 5.2 출력 형식

libsigrok `src/output/{bits,hex,csv,vcd,binary}.c`의 규칙을 옮긴다.
날짜·버전 문자열을 제외하면 바이트 단위로 같아야 한다. 트리거 표시(각 형식이 트리거 이벤트에서
내는 줄·마커)도 포함한다.

golden 파일은 sigrok-cli를 설치하지 않고 **libsigrok 출력 모듈 소스를 읽고 손으로 작성**한다.
각 golden 파일 옆에 근거가 된 소스 위치(파일·함수)를 주석이나 README로 남긴다.
나중에 sigrok-cli를 쓸 수 있게 되면 `-i`로 같은 입력을 넣어 교차 확인한다 (선택).

| 형식 | 내용 | 옵션 |
|---|---|---|
| `bits` | `CH0:00011110 11100001 …` | `width=` (기본 64) |
| `hex` | `CH0:1e e1 …` | `width=` |
| `csv` | `;` 주석 헤더 + 채널 이름 행 + 값 행 | `time=`, `header=` 등 libsigrok 기본값 따름 |
| `vcd` | `$timescale` 자동, 변화 시에만 기록 | — |
| `binary` | 샘플당 2바이트 LE | — |

### 5.3 종료 코드

| 코드 | 상황 |
|---|---|
| 0 | 성공. 첫 Ctrl-C로 캡처를 멈추고 다운로드·출력까지 마친 경우도 포함 (4.2절 3단계) |
| 1 | 모든 오류: 잘못된 옵션 값, 지원하지 않는 트리거 조합, 펌웨어 없음, 장비 없음, 업로드 실패, 재열거 시간 초과, USB 오류, 미지원 EEPROM magic (sigrok-cli와 같이 1로 통일, 원인은 stderr 메시지로 구분) |
| 130 | 다운로드 중 Ctrl-C 또는 캡처 중 두 번째 Ctrl-C로 중단 (`-o` 파일 삭제) |

### 5.4 sigrok-cli와 다른 점

| 항목 | sigrok-cli | kla | 이유 |
|---|---|---|---|
| `--extract-firmware` | 없음 (별도 스크립트) | 내장 | Mac용 추출기가 sigrok에 없음 |
| `-o`만 주고 `-O` 생략 | `srzip` | 확장자로 추정 (`.vcd`→vcd, `.csv`→csv, `.bin`→binary), 그 밖(`.sr` 포함)은 srzip | `-o cap.vcd`가 zip이 되는 것보다 덜 놀랍다 |
| 지원 드라이버 | 다수 | `kingst-la2016` (LA5016)만 | |
| 출력 형식 | 다수 | bits, hex, csv, vcd, binary, srzip | |
| 입력 형식 | 다수 | srzip만 | |
| `--time` | 드라이버의 msec 한도(시간이 되면 HALT) | 샘플 수로 변환해 하드웨어 한도로 설정 | 길이가 정확하고 폴링 지연과 무관 |

## 6. 펌웨어 추출 (`firmware.py`)

- 입력: `KingstVIS.app/Contents/MacOS/KingstVIS` (확인한 버전 v3.6.6, x86_64 전용 Mach-O, 심볼 stripped —
  `_ZL16qt_resource_data/_name/_struct` 심볼 없음). sigrok의 `sigrok-fwextract-kingst-la2016`은
  ELF 심볼로 테이블을 찾으므로 Mac에서는 쓸 수 없다.
- 확인된 리소스 이름 (rcc 이름 테이블, UTF-16BE):
  - `fwusb/`: `fw01A1`, `fw01A2`, `fw01A3`, `fw01A4`, `fw03A1`
  - `fwfpga/`: `LA1010A0/A1/A2`, `LA1016`, `LA1016A1`, `LA2016`, `LA2016A1/A2`, `LA5016A1`, `LA5016A2`, `LA5032A0`, `MS6218`
  - v3.6.6에서 `"fwfpga"` 이름 항목은 파일 오프셋 ≈ 1198508 부근 (테스트·디버깅 참고용, 코드에 하드코딩 금지)
- 심볼이 없으므로 **Qt rcc 이름 테이블을 패턴으로 찾는다.** 이름 항목은
  `u16 길이 BE, u32 해시 BE, UTF-16BE 문자열` 형식이다. `fwusb`, `fwfpga` 디렉터리 이름을 앵커로
  struct 테이블과 data 테이블 위치를 역산한다. 구체적 탐색법은 **구현 계획의 첫 작업(spike)**으로
  실제 바이너리에서 확정하고, 확정한 방법을 코드 주석과 이 절에 남긴다.
- 리소스 플래그에 압축(0x01)이 있으면 앞 4바이트 BE 길이 + zlib.
- FX2 펌웨어가 Intel HEX면 blob으로 변환 (sigrok `maybe_intel_hex_as_blob`과 동일 의미).
- 파일명: `fwusb/fwXXXX` → `kingst-la-XXXX.fw` (소문자), `fwfpga/NAME` → `kingst-NAME-fpga.bitstream` (소문자).
- 추출 대상: 모든 `fwusb/*`, `fwfpga/LA5016*`. 추출 후 파일명·크기·SHA-256을 출력.

## 7. 저장소 위생

- `.gitignore`: `*.fw`, `*.bitstream`, `*.bin`, `firmware/`, 그리고 일반 Python 산출물.
- 테스트는 실제 Kingst 파일을 쓰지 않는다. 가짜 rcc 테이블·가짜 Intel HEX를 테스트 안에서 만든다.
- 문서에는 펌웨어 SHA-256·크기까지만 기록한다.
- `LICENSE`: GPL-3.0 전문. 옮긴 파일 머리에 libsigrok 원저작자 표기와 출처.

## 8. 테스트

### 8.1 장비 없이 (`pytest`, 기본)

| 대상 | 내용 |
|---|---|
| `options` | SI 접미사, 채널 범위·이름 바꾸기, 트리거 문법, 오류 케이스(엣지 2개, 목록에 없는 threshold·lo≠hi, samplerate 범위 밖, `--samples`+`--time`) |
| `rle` | 손으로 만든 transfer → 기대 샘플. 반복수 0/255, transfer가 청크 경계에 걸침, 출력 청크 상한, 트리거 이벤트 위치(0 포함, 트리거 미설정 시 없음), `--samples` 잘라내기, 시퀀스 어긋남 경고 |
| `output/*` | 짧은 샘플 배열(트리거 있음/없음) → golden 파일 비교 (날짜·버전 줄 제외) |
| `firmware` | 가짜 rcc 바이트열(압축/비압축), 가짜 Intel HEX → 기대 파일 |
| `device` | `FakeTransport`: 초기화 분기(펌웨어 유무, 재열거 타이밍·port path 재탐색, FPGA 체크 실패 시 업로드), close 순서, `read_pos` wrap, 레지스터 바이트(분주비, pre-trigger, 트리거 마스크)가 sigrok 공식과 일치 |
| `cli` | FakeTransport 주입 end-to-end: `--scan`, `--samples 100 -O csv` → stdout, 종료 코드 |

### 8.2 실기 (`pytest -m hw`, 기본 skip)

장비 연결 + 펌웨어 추출 상태에서만 실행.

### 8.3 첫 연결 검증 절차

1. `system_profiler SPUSBDataType` — 펌웨어 로드 전 VID:PID, iProduct
2. `kla --extract-firmware` — 파일 목록, 크기, SHA-256
3. `kla --scan -l 5` — FX2 업로드·재열거 후 PID, EEPROM magic(**a1/a2**), `REG_RUN`
4. DSO2000 내장 신호 발생기로 1 kHz·1 MHz 구형파 → `-O vcd` 캡처 → 주기·duty 비교 (자동화 스크립트)
5. 500 MHz / 200 MHz / 최저 samplerate, 트리거 r/f/레벨, captureratio
6. pyusb 동기 read 처리량(MB/s) 측정 — 2차 스트리밍 방식 판단 근거
7. 필요 시 같은 신호를 KingstVIS로 캡처해 교차 비교

### 8.4 1차 완료 기준

- 장비 없는 테스트 전부 통과
- 실기: 1 MHz 구형파를 100 MSa/s로 캡처해 주기 오차 ±1 샘플 이내
- 그 캡처의 VCD를 PulseView 또는 GTKWave로 열 수 있음

## 9. 실기 검증 결과 (2026-10-15, macOS arm64, CH0에 1 kHz 사인파)

| 항목 | 결과 |
|---|---|
| 장치 | `77a1:01a2`, iProduct 2 ("Kingst Logic Analyzer"), 처음 연결 시 이미 FX2 펌웨어·FPGA가 올라와 있었음 |
| FX2 업로드 | 재연결 직후 PID 그대로 `01a2`, iProduct 0 → `kingst-la-01a2.fw` 업로드 → 1805 ms 뒤 같은 port path로 재열거(iProduct 2) → FPGA 업로드. `--scan` 전체 2.97 s |
| 리비전 | EEPROM magic `0x05/0x10` → **la5016a1**, 제조 2021-05 |
| 펌웨어 추출 | KingstVIS v3.6.6에서 rcc 형식 1(14바이트 struct) 확인. FX2 펌웨어 5개, LA5016 비트스트림 3개 (`la5016`, `la5016a1`, `la5016a2`) |
| FPGA 업로드 | 247485 B, 0.94 s, 이후 `check_fpga_bitstream` 통과 |
| 문턱 | 사인파 피크가 0.6~0.9 V라 0.6 V에서만 검출. 연결 안 된 입력은 0.4 V에서 1로 읽힘 |
| 주파수 | 1 MSa/s: 주기 중앙값 1000 샘플, FFT 1000 Hz. 200/500 MSa/s도 FFT 1000 Hz |
| 분주 | ~~300 MSa/s 요청 → 400 MSa/s~~ 잘못된 기록. 실제로는 200 MSa/s (17절 분주비 규칙) |
| 트리거 | `r`/`f` 모두 트리거 지점 전후 값이 0→1 / 1→0, captureratio 50 % 근처 |
| RLE | 시퀀스 번호 끊김 0 (모든 캡처) |
| 처리량 | pyusb 동기 read **42.9 MB/s** (55.7 MB). 스트리밍 최대 25 MB/s보다 큼 |
| Ctrl-C | 첫 번째: HALT 후 1.8 s 분량 저장, 종료 0. 두 번째: 종료 130, `-o` 파일 삭제 |
| 관찰 | 입력에 히스테리시스가 없어, 문턱 근처를 천천히 지나는 사인파에서 2샘플 수준의 글리치가 생김 |

## 10. 미해결

- 문턱 전압 −4~+4 V 연속값 지원 여부 (드라이버 공식은 ±4 V까지 계산함)
- Linux udev / Windows WinUSB 환경

---

# 2차: 프로토콜 디코더 (`-P`)

- 작성: 2026-10-16
- 근거: libsigrokdecode (`type_decoder.c`, `instance.c`, `decoder.c`, `session.c`), sigrok-cli (`decode.c`, `show.c`, `parsers.c`)

## 11. 목표와 결정

sigrok 디코더 원본(`decoders/*/pd.py`)을 **수정 없이** 실행하는 순수 Python `sigrokdecode` 호환 계층을 만든다.

| 항목 | 결정 |
|---|---|
| 디코더 원본 | `kla --install-decoders`가 libsigrokdecode 고정 커밋(기본 `71f45144…`)의 tarball을 받아 `decoders/`만 사용자 데이터 폴더(`…/kingst-la/decoders/`, `KINGST_LA_DECODERS_DIR`로 변경 가능)에 설치. 저장소에 넣지 않음 |
| 실행 모델 | stack마다 워커 스레드 1개 + 동기 핸드오프 (libsigrokdecode와 같은 의미) |
| 조건 매칭 | numpy 벡터화, C `find_match`와 같은 의미 |
| CLI 범위 | `-P`, `-A`, `-B`, `-L`, `--show -P`, `--protocol-decoder-samplenum`. `-M`은 6차(25절), JSON trace는 이후 |
| 의존성 | 추가 없음 (표준 라이브러리 + numpy) |

## 12. 모듈

```
src/kingst_la/srd/
├── api.py         sigrokdecode 모듈 대역: 상수, Decoder(register/put/wait/has_channel)
├── conditions.py  wait() 조건 → 다음 일치 샘플 찾기 (순수 함수)
├── loader.py      디코더 로드·메타데이터 검증·옵션 변환
├── session.py     stack, 워커 스레드, DecoderSink(pipeline.Sink)
├── annotate.py    annotation 텍스트·-A 필터·-B 바이너리 출력
└── install.py     --install-decoders
```

- 로드 시 `sys.modules["sigrokdecode"] = kingst_la.srd.api`로 등록한다 (같은 이름의 최상위 패키지는 설치하지 않음).
- 디코더 패키지는 디렉터리 이름으로 `sys.modules`에 등록하고(`spec_from_file_location`), `common.srdhelper` import를 위해
  디코더 폴더를 `sys.path` **끝**에 추가한다 (표준 라이브러리를 가리지 않도록).

## 13. 엔진 의미 (libsigrokdecode api_version 3)

**호출 순서:** `Decoder()` → `self.options`를 기본값+사용자 값 dict로 교체 → `metadata(SRD_CONF_SAMPLERATE, rate)`(있으면)
→ `start()` → `samplenum = 0`, `matched = None` → 워커 스레드에서 `decode()` → 청크마다 `flush()`(있으면, stack 위로 재귀).

**`wait(conds)`:**
- 인자 없음/`None`/`{}`/`[]`: 조건 없음. 첫 호출은 현재 샘플(0), 이후는 skip 1.
- dict 안은 AND(삽입 순서로 short-circuit), list는 OR. 값의 첫 글자: `h` `l` `r` `f` `e` `n`. 문자열 키는 skip.
- 현재 샘플을 **포함해** 검사하고 일치한 샘플에서 멈춘다(`samplenum` = 그 샘플). 엣지는 직전 샘플과 비교, 0번 샘플에는 엣지가 없음.
- skip 카운터는 같은 dict의 앞선 항목이 모두 맞은 샘플에서만 증가하고, `n`번 증가 후 일치한다 (`{'skip': 0}`은 현재 샘플).
- 반환: 디코더 채널 순서(channels → optional_channels)의 핀 값 tuple. 미연결 채널은 `0xff`, 그 채널의 조건은 항상 불일치.
- `self.matched`: 조건별 bool tuple.
- 데이터 끝: `samplenum` = 총 샘플 수, `EOFError` → 정상 종료.

**`put(ss, es, output_id, data)`:** ANN `[cls, [str, …]]`(list 검증), PYTHON → 위 decoder들의 `decode(ss, es, data)`를 동기 호출,
BINARY `[cls, bytes]`. `register(type, proto_id=inst_id)`는 같은 (type, proto_id)면 기존 id 반환. instance id는 `<id>-<n>`(1부터).

**옵션 변환:** 기본값 타입 기준. int는 `int(s, 0)`(C `strtoll(...,0)`과 같게 `0x1f`, `010`=8), float, str. 모르는 키는 사용 오류.

**예외:** 디코더 예외는 `srd: <이름>: Protocol decoder instance <inst>: <메시지>`를 stderr에 내고 세션을 멈춘다 (종료 1).
stacked decoder의 `decode()` 예외는 sigrok처럼 로그만 남긴다.

**청크 핸드오프:** `feed(chunk)`는 각 stack에 청크를 넘기고 그 stack이 청크를 다 소비해 다음 데이터를 기다릴 때까지 블록한다.
출력 순서가 결정적이고, 메모리는 청크 1개 분량으로 일정하다.

## 14. CLI

| 옵션 | 동작 |
|---|---|
| `-P dec:ch=CH0:opt=v,dec2` | `-P`마다 stack 1개, `,`는 아래→위. 채널 매핑은 첫 decoder만. 채널 이름은 활성 채널 이름(`CH0` 또는 `-C`의 새 이름) |
| `-A dec[=cls[:cls…]][,dec2…]` | class id, 없으면 row id로 찾음(대소문자·비영숫자 무시). 지정한 decoder만 출력 |
| `-B dec[=bincls]` | 바이너리 바이트를 그대로 출력 (`-o` 있으면 파일로) |
| `--protocol-decoder-samplenum` | 줄 앞에 `ss-es ` |
| `-L` | 출력 형식 + 설치된 디코더 목록 (`  %-20s %s`) |
| `--show -P dec[,dec…]` | sigrok-cli 형식 설명. 장치 불필요 |
| `--install-decoders [all\|PD,PD…]` | 확장. 이름을 주면 그 decoder와 `common/`만 추가·교체 (설치된 것과 같은 커밋일 때만) |
| `--list-decoders [available\|installed]` | 확장. available: tarball을 받아 `pd.py`를 **실행하지 않고** `ast`로 읽어 목록과 설치 여부 표시. installed: 오프라인 목록과 설치 커밋 |
| `--decoders-commit COMMIT` | 확장. 위 두 옵션의 libsigrokdecode 커밋 (기본 고정 커밋) |

- annotation 줄: `[ss-es ]<proto_id>: [<cls>: ]<text>`. 기본은 첫 문자열만. `-l ≥ 3`이면 범위·따옴표, `-l ≥ 4`면 class id와 모든 문자열.
- `-P`가 있으면 로직 출력(`-O`)은 내지 않는다 (sigrok-cli와 같음). `-B`가 있으면 annotation 대신 바이너리만.
- sigrok-cli와 다른 점:
  - 미연결 채널 조건(C에서는 미정의 동작)은 항상 불일치.
  - 잘못된 `-P` 옵션·채널, 필수 채널 누락은 경고가 아니라 사용 오류 (sigrok-cli는 경고 후 그 decoder를 빼거나 이전 매핑을 유지).
  - `-P`와 `-o`를 함께 주면 annotation(또는 `-B` 바이너리)을 그 파일에 쓴다 (sigrok-cli는 `-P`일 때 파일 출력이 없음).
  - 아래 decoder의 `decode()`가 예외 없이 반환되면 그 stack만 멈추고 캡처는 계속한다 (sigrok-cli는 세션 전체를 멈춤).

## 15. 테스트

- 엔진: 테스트 안의 가짜 decoder로 호출 순서·put·stack·예외·옵션 변환 검증.
- `conditions`: C 의미를 샘플 단위로 옮긴 참조 구현과 numpy 구현을 무작위 신호·조건으로 대조.
- 실제 decoder(`-m decoders`, 설치 시에만): 합성 UART/I2C/SPI 신호 → 기대 annotation, 청크 분할 불변성.
- 실기: CH0 1 kHz 신호에 `timing` decoder → 주기 1 ms.

## 16. 검증 결과 (2026-10-16)

| 항목 | 결과 |
|---|---|
| 디코더 로드 | libsigrokdecode `71f45144`의 131개 전부 로드 |
| 조건 매칭 | numpy 구현 = 샘플 단위 참조 구현 (무작위 300건) |
| 합성 신호 | uart(115200, 9600 bin), i2c 주소·데이터·start/stop, spi mode 0, timing 1 kHz, i2c→eeprom24xx stack |
| 속도 | uart 1100만 샘플(1024 바이트) 1 s 미만 |
| 실기 데이터 | 1차 Ctrl-C 테스트에서 저장한 1 MSa/s CH0 1 kHz 캡처(181만 샘플)에 `timing` → 주기 1000±2 µs가 다수, 문턱 글리치에서 2 µs 주기도 보임 |

---

# 3차: 연속 스트리밍 (`-c continuous=on`)

- 작성: 2026-10-16
- 근거: libsigrok kingst-la2016 `stream_data()`, `la2016_start_acquisition()`, `la2016_receive_data()` + 실측

## 17. 실측으로 확인한 하드웨어 사실

**분주비 규칙 (SDRAM·스트리밍 공통).** LA5016이 실제로 쓰는 분주비는 1, 4, 8의 배수뿐이다.
캡처 소요 시간(SDRAM)과 수신 샘플 수/경과 시간(스트리밍)으로 측정했다.

| 레지스터 분주비 | 실제 |
|---|---|
| 1 | 500 MHz |
| 2–7 | 200 MHz (분주비 4와 같음) |
| ≥ 8 | `800 MHz / (분주비 & ~7)` — 예: 20 → 50 MHz, 100 → 8.33 MHz |

libsigrok은 `800 MHz / 분주비`를 실제 레이트로 보고하므로, 1-2-5 목록 밖의 레이트에서는 시간축이 틀린다
(목록의 레이트는 모두 1, 4, 8의 배수라 맞다). kingst-la는 `options.hardware_divider()`로 실제 분주비를 계산해
레지스터에 쓰고, `Meta.samplerate`에 실제 레이트를 넣고, 요청과 다르면 경고한다.

**스트리밍 한계.**
- 분주비 < 8(200·500 MHz)에서는 데이터가 거의 나오지 않는다 → 스트리밍 최대 100 MHz, 그 이상은 사용 오류.
- 대역폭(레이트 × 활성 채널): 160 Mbps까지는 반복 실행에서 끊김 없음. 200 Mbps는 대부분 되지만 약 1/20 확률로
  중간에 데이터가 끊김(장비 버퍼 넘침으로 추정). 16 ch × 20 MHz(320 Mbps, 약 40 MB/s)도 한 번 성공했지만 보장 안 됨.
  sigrok처럼 200 Mbps 이상이면 경고만 한다.
- 시작·정지 직후 데이터 값은 SDRAM 모드와 같다 (문턱 0.4 V에서 미연결 입력 1, CH0 0).

**macOS libusb.** 데이터가 조금씩 들어오는 동안에는 동기 bulk read의 시간 초과가 동작하지 않는다
(버퍼가 찰 때까지 블록). 그래서 읽기 크기를 데이터 속도의 약 50 ms 분량(512 B 단위, 최대 512 KiB)으로 정한다.
pyusb는 시간 초과 시 받은 일부 데이터를 버리므로 이 크기 조정이 데이터 손실도 막는다.

## 18. 구조

```
Device.stream(cfg) ─ REG_CAPT_MODE=0x01, 트리거 0 → BULK_RESET → BulkReader 시작 → RUN → BULK_START
   └─ Stream: iter(raw bytes), stop() = HALT + reader 정지
BulkReader (usb.py): 수신 전용 스레드 + 큐(64 × 읽기 크기). 큐가 차면 overrun, 읽기 시간 초과는 StreamError
stream.expand(raw, sink, channels, limit) ─ Deinterleaver: 활성 채널마다 u16(16샘플, LSB 먼저), 채널 순서대로
```

- de-interleave는 numpy (`unpackbits` + 채널 ≤ 4는 시프트, 그 이상은 `packbits`): 1 ch 151 MB/s, 16 ch 62 MB/s
  (USB 최대 약 40 MB/s보다 빠름).
- 출력·`-P` 디코더·`-o`는 SDRAM 모드와 같은 Sink를 그대로 쓴다.

## 19. CLI

| 항목 | 동작 |
|---|---|
| `-c continuous=on` | on/off/true/false/yes/no/1/0. 기본 samplerate 100 MHz |
| `-t`, `-w` | 사용 오류 (하드웨어 트리거 없음. sigrok은 조용히 무시) |
| `--samples` / `--time` | 도달하면 HALT. 둘 다 없으면 Ctrl-C까지 |
| 첫 Ctrl-C | HALT, 받은 데이터를 모두 출력, 종료 0 |
| 두 번째 Ctrl-C | 중단, `-o` 삭제, 종료 130 |
| 스트림 끊김 | 그때까지의 출력은 **남기고** 종료 1, 메시지로 대역폭을 줄이라고 안내 |

## 20. 검증 (2026-10-16)

| 항목 | 결과 |
|---|---|
| 값 | 16 ch × 1 MHz: 문턱 0.4 V → 전부 `0xfffe`, 1.4 V → 전부 `0` (SDRAM 모드와 같음) |
| 실시간성 | 4 ch × 10 MHz 1 s, 1 ch × 100 MHz 1 s: 샘플 수 정확, 0.9–1.5 s 안에 도착 (`pytest -m hw`) |
| 반복 | 6가지 설정 × 4회: 160 Mbps 이하 24/24 성공, 200 Mbps 11/12 성공 |
| Ctrl-C | 8 ch × 20 MHz 무한 스트리밍 → 3 s 뒤 SIGINT → 2.8 s 분량 저장, 종료 0 |

**실제 신호로 재확인 (CH0 1 kHz 사인파, 문턱 0.4 V).** 캡처를 `-O binary`로 받아 FFT 기본 주파수를 구했다.
kingst-la가 보고한(실제) 레이트로 계산하면 모든 경우 1000.00 Hz:

| 모드 | 요청 → 실제 레이트 | FFT |
|---|---|---|
| SDRAM | 1 M, 10 M, 100 M, 500 M (그대로) | 1000.00 Hz |
| SDRAM | 40 M → 50 M, 8 M → 8.33 M, 400 M → 200 M | 1000.00 Hz (요청값으로 계산했다면 1250 / 1042 / 500 Hz) |
| 스트리밍 | 1 M (4 ch 3 s / 1 ch 2 s), 10 M (2 s), 100 M (1 s) | 1000.00 Hz |
| 스트리밍 | 40 M → 50 M (1 s) | 1000.00 Hz |

`timing` 디코더(`format=samples`)로도 SDRAM 1 MHz, 스트리밍 1 MHz 모두 주기 1000샘플이 최빈값.

---

# 4차: PWM 출력 (`-g PWMx -c … --set`)

- 작성: 2026-10-17
- 근거: libsigrok kingst-la2016 `set_pwm_config()`, api.c 채널 그룹 + 실측

## 21. 동작

| 항목 | 내용 |
|---|---|
| CLI | `kla -g PWM1 -c enabled=on:output_frequency=1k:duty_cycle=25 --set` (sigrok-cli의 `-g`/`--set`). `-g`가 있으면 `-c` 키는 `enabled`, `output_frequency`(SI 접미사, 소수 가능), `duty_cycle`(%, `25%`도 허용) |
| 레지스터 | sigrok과 같음. 200 MHz 클럭, `REG_PWM1 0x70`/`REG_PWM2 0x78`에 `(주기 = int(200M/f), high = int(주기×duty/100 + 0.5))` u32 LE, `REG_PWM_EN 0x02` 비트 0/1. 해당 채널 끄기 → 값 쓰기 → 켜기 |
| 범위 | 200 MHz/2³² (≈0.047 Hz) ~ 20 MHz, duty 0 초과 100 이하 |
| 기본값 | PWM1 1 kHz, PWM2 100 kHz, duty 50 % (sigrok) |
| 실제 값 | 정수 레지스터로 생기는 실제 주파수·duty를 출력 (`PWM2: on, 123.457 kHz, duty 10 %`) |
| 지속 | 프로세스가 끝나도 출력은 계속된다. 장치를 열 때 PWM을 건드리지 않으므로 `--set` 다음 명령으로 캡처할 수 있다 |
| `-c`에 값만 주면 | `enabled`를 생략하고 주파수/duty만 주면 켠다. 주지 않은 값은 마지막 설정값 |
| 제약 | `-g`는 `--set`과만 쓴다 (sigrok-cli도 `-g`가 모든 `-c`에 적용되므로 캡처 설정과 섞을 수 없음) |

**실측: PWM 레지스터는 쓰기 전용이다.** `REG_PWM_EN`, `REG_PWM1/2`는 켠 뒤에도 0으로 읽힌다. 그래서

- 마지막 설정을 사용자 데이터 폴더의 `pwm.json`에 기록하고, 다른 채널을 바꿀 때 이 기록으로 enable 마스크를 만든다
  (읽어서 만들면 다른 출력이 꺼진다). FPGA 비트스트림을 새로 올리면(전원 재투입) 기록을 지운다.
- `--show`의 PWM 상태는 "kingst-la가 마지막으로 설정한 값"이다. KingstVIS 등 다른 프로그램이 바꾼 것은 모른다.
- sigrok `check_fpga_bitstream()`은 `REG_PWM_EN & 3 == 0`도 요구하지만, 항상 0으로 읽히므로 이 검사를 뺐다
  (sigrok에서는 결과적으로 같음; PWM이 켜져 있어도 비트스트림을 다시 올리지 않게 하려는 의도를 명시).

## 22. 검증 (2026-10-17, PWM2 → CH0 직결)

| 설정 | 캡처 | 측정 |
|---|---|---|
| 1 kHz, 25 % / 75 % | 1 MSa/s | 주기 1000.00 샘플(표준편차 0), duty 0.2500 / 0.7500 |
| 10 kHz 50 %, 1 MHz 50 % | 10 / 100 MSa/s | 정확 |
| 123.4 kHz 10 % → 실제 123.457 kHz | 100 MSa/s | 810.00 샘플 = 123457 Hz, 0.1000 |
| 3 MHz 33.3 % → 실제 3.0303 MHz 33.33 % | 500 MSa/s | 165.00 샘플, duty 0.338 (1샘플 = 0.6 %) |
| 20 MHz 50 % (최대) | 500 MSa/s | 25.00 샘플 = 20 MHz |
| 10 Hz 50 % | 20 kSa/s | 2000.00 샘플 |
| 25 MHz | — | 사용 오류 |

같은 신호로 다시 확인한 것 (사인파에서는 문턱 글리치 때문에 애매했던 항목):

- `timing` 디코더: 999개 주기 전부 `1.000 ms (1.000 kHz)`.
- 스트리밍 10 MHz × 4 ch 2 s: 1999개 주기 전부 10000 샘플 — 손실·중복 없음.
- 트리거 `r`/`f` + `-w`: 트리거 직후 High 250샘플(25 %) / Low부터 시작.
- PWM1 → CH0으로 옮겨 재확인: 1 kHz 25 %에서 `timing` 999개 주기 전부 1.000 ms, 루프백 테스트(`KINGST_LA_TEST_PWM=1`) 통과.
  PWM1을 2 kHz로 켠 채 PWM2를 켜고 끄는 `--set`을 두 번 실행해도 PWM1은 계속 2 kHz (상태 파일로 enable 마스크 유지).

**찾은 버그:** 스트림을 멈춘 뒤 수신 스레드가 EP6 읽기를 붙잡고 있어, 같은 프로세스의 다음 SDRAM 다운로드 데이터를
가로챘다 (CLI는 매번 프로세스가 끝나 드러나지 않았음). `Stream.stop()`이 스레드 종료를 기다리게 하고 읽기 시간 초과를
데이터 속도에 맞춰(최소 250 ms) 줄였다. SDRAM/스트리밍 교대 30회 모두 정상.

---

# 5차: `.sr`(srzip) 저장과 `-i` 입력

- 작성: 2026-10-17
- 근거: libsigrok `src/output/srzip.c`, `src/session_file.c`(`sr_session_load`), `src/session_driver.c`

## 23. 형식과 동작

**파일 구조.** zip(deflate) 안에

| 항목 | 내용 |
|---|---|
| `version` | `2` |
| `metadata` | GKeyFile. `[global] sigrok version=…`, `[device 1]`에 `capturefile=logic-1`, `total probes`, `samplerate`(`sr_samplerate_string`, 예 `12.208 kHz`), `total analog=0`, 켠 채널만 `probe<index+1>=이름`, 데이터가 있으면 `unitsize` |
| `logic-1-1`, `logic-1-2`, … | 원시 샘플, 4 MiB 단위. 샘플마다 장치의 모든 비트(LA5016은 2바이트 LE, 비트 i = CHi) |

- 꺼진 채널 비트는 0으로 지운다. 트리거 위치는 형식에 없으므로 저장되지 않는다 (sigrok도 같음).
- metadata는 마지막에 쓴다. libsigrok은 처음에 쓰고 첫 데이터에서 `unitsize`를 넣어 교체하는데, 결과는 같다
  (읽는 쪽은 이름으로 찾는다). 데이터가 0개면 `unitsize`가 없는 것도 같다.
- 표준 출력이나 seek가 안 되는 스트림에는 쓸 수 없다 (`needs a file (-o FILE)`).
- `Meta.total_channels`(기본 16)를 새로 두어, 입력 파일의 채널 수가 `total probes`·`unitsize`와
  bits/hex/csv/vcd 헤더의 `n/16`에 그대로 반영된다.

**입력 (`-i FILE [-I srzip]`).** `input/srzip.py`

- version 1·2. `capturefile` 이름의 단일 항목이 있으면 그것, 없으면 `<이름>-1`, `-2`, … 를 빠지는 번호까지 읽는다.
- unitsize는 1~N 모두 받는다. 샘플을 uint16으로 만들므로 probe 17 이상이 켜진 파일은 오류.
  항목 경계가 샘플 경계와 달라도 되고, 끝의 불완전한 샘플은 버린다 (session_driver.c와 같음).
- GKeyFile 이스케이프(`\s \n \t \r \\`)와 `#` 주석 처리. samplerate는 `sr_parse_sizestring`처럼 `1 MHz`, `20k`, `500000`.
- `-C`는 파일의 채널 이름 또는 `CHn`(비트 n)으로 고르고 `이름=새이름`으로 바꾼다 (`options.select_channels`).
- `--samples`·`--time`은 앞부분만 처리. `-d`·`-c`·`-t`·`-w`·`-g`·`--set`·`--scan`·`--show`와 함께 쓰면 사용 오류.
  `-o`가 입력 파일 자체면 오류(덮어쓰기 방지).
- 출력·`-P` 디코더는 장치 캡처와 같은 Sink 경로를 쓴다.

## 24. 검증 (2026-10-17, PWM1 → CH0)

| 항목 | 결과 |
|---|---|
| SDRAM 1 MHz 2 M 샘플 → `.sr` (12.7 KB) → `-i … -P timing` | 2000개 high 250 µs, 1999개 low 750 µs |
| 스트리밍 → `.sr` 3 s: 4 ch × 10 MHz, 1 ch × 50 MHz, 1 ch × 100 MHz | 모두 실시간으로 저장 (3.3 s), 100 MHz 3억 샘플 605 KB |
| 100 MHz 3억 샘플 재검사 | 펄스 폭 25000/75000 샘플뿐 (한 번은 에지 바운스 1·8샘플, 합은 정확히 한 주기) |

**찾은 하드웨어 현상: 1채널 스트림의 시작 워드.** 위 검사에서 맨 앞 32샘플 안에 16샘플짜리 펄스가 보였다.
PWM 주기 10샘플 신호로 위상을 맞춰 보면, 1채널 스트림의 **앞 1~2워드(최대 32샘플)는 올바른 파형이지만 그 뒤와
위상이 이어지지 않는다** — 그보다 이른 시점의 샘플이고 그 뒤에 시간 공백이 있다.

| 조건 | 빈도 |
|---|---|
| 1채널, 1/10/50/100 MHz | 약 10–40 % (실행마다 0, 1 또는 2워드) |
| 2·4·8채널 | 75회 중 0 |
| RUN과 BULK_START 순서를 바꿔도 | 같음 |
| 직전 실행에서 받은 워드 수와 | 상관 없음 |

원인은 FPGA/FX2 내부라 알 수 없다 (sigrok도 같은 순서로 시작하므로 같은 현상이 있을 것). 1채널 스트림에서 앞 4바이트를
버린다 (`STREAM_SKIP_ONE_CHANNEL`). 시작이 32샘플 늦어질 뿐이다. 수정 후 10/50/100 MHz 1채널 80회 모두 위상 연속,
`test_pwm_one_channel_stream_starts_in_phase`는 수정 전 코드에서 실패하고 수정 후 통과한다.

---

# 6차: 디코더 meta 출력 (`-M`)

- 작성: 2026-10-17
- 근거: sigrok-cli `main.c`, `decode.c`(`setup_pd_meta`, `show_pd_meta`), libsigrokdecode `type_decoder.c`(`convert_meta`)

## 25. 동작

| 항목 | 내용 |
|---|---|
| 옵션 | `-M PD[,PD...]` — 디코더 id만 (클래스 선택 없음). 빈 항목에서 목록 끝. 스택에 없는 id는 `Protocol decoder 'x' not found.` |
| 우선순위 | 한 종류만 출력: `-B` > `-M` > `-A` (sigrok-cli와 같음. `-M`과 `-A`를 같이 주면 `-A`는 무시) |
| 형식 | `[ss-es ]proto_id: 이름: 값`. 샘플 번호는 `--protocol-decoder-samplenum` 또는 `-l 3` 이상 |
| 값 | `g_variant_print(…, FALSE)`: int64는 `%d`, double은 `g_ascii_dtostr`(`%.17g`)에 `.`/`e`/`n`이 없으면 `.0` (`115200.0`, `0.10000000000000001`, `inf`) |
| 형 검사 | `convert_meta()`처럼 `int` 등록이면 `isinstance(int)`(bool 포함), `float`이면 `isinstance(float)`. 다르면 C와 같은 TypeError로 디코더 오류 |

meta를 내는 설치 디코더는 `i2c`, `spi`(Bitrate), `jitter`(missed 횟수), `usb_power_delivery`. `pwm`은 등록만 하고 내지 않는다.

**검증 (2026-10-17).** 합성 신호: spi 8비트가 71샘플(1 MHz) → `Bitrate: 112676` (= int(8 / 71 µs)), i2c 한 줄.
실기: PWM1 100 kHz → CH0을 `spi:clk=CH0:mosi=CH0`로 10 MHz 0.1 s 캡처 → 1250줄 모두 `Bitrate: 114122`
(= 8 / 701샘플), `-l 3`에서 `38-738 spi-1: …`.

