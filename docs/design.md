# la5016 설계 — sigrok-cli 호환 로직 분석기 CLI

- 작성: 2026-10-15
- 범위: 1차 (캡처). 2차 이후 항목은 1절 "1차에서 제외" 참고
- 상태: 1차 구현·실기 검증 완료 (9절)

## 1. 목표와 범위

Kingst LA5016 로직 분석기를 macOS에서 **pyusb + numpy만으로** 제어하는 Python CLI `la5016`을 만든다.
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
| `-i` 파일 입력, `srzip` 출력 | |

## 2. 결정 사항

| 항목 | 결정 | 근거 |
|---|---|---|
| 라이선스 | **GPL-3.0-or-later** | GPL-3 sigrok 드라이버를 옮긴다 |
| 패키지·명령 이름 | `la5016` | |
| USB 접근 | **pyusb 동기 I/O** | SDRAM 모드는 캡처 종료 후 다운로드이므로 실시간성이 불필요 |
| 내부 데이터 | numpy `uint16` 샘플 청크의 iterator | 수백 MB 캡처도 메모리에 전부 올리지 않는다. 출력과 디코더가 같은 인터페이스를 쓴다 |
| 펌웨어 배포 | **저장소에 넣지 않는다** (public/private 모두) | Kingst 저작물. 사용자가 자기 KingstVIS.app에서 추출 |
| 펌웨어 위치 | macOS `~/Library/Application Support/la5016/firmware/`, Linux `$XDG_DATA_HOME`(기본 `~/.local/share`)`/la5016/firmware/`, Windows `%LOCALAPPDATA%\la5016\firmware\` | 저장소 밖 |
| 플랫폼 | 1차 검증은 macOS. 코드는 OS 전용 처리 없이 작성 (펌웨어 경로·libusb 위치만 분기) | Linux는 udev 규칙, Windows는 WinUSB 드라이버가 필요 — 실기 확인 후 README에 안내 |
| 문턱 전압 | sigrok과 동일: 이산값 0.4/0.6/0.9/1.2/1.4/2.0/2.5/4.0 V, 기본 1.4 V | 연속값·−4~+4 V 확장은 실기 확인 후 |
| sigrok-cli와 다른 점 | 5.4절에 모아 둔다 | 그 외는 sigrok-cli와 같게 동작해야 한다 |
| Python | ≥ 3.9, 개발 환경 3.13 | |

## 3. 모듈 구성

```
src/la5016/
├── __init__.py
├── __main__.py      python -m la5016
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

펌웨어 파일이 없으면 USB에 접근하기 전에 중단하고 `la5016 --extract-firmware`를 안내한다.

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
| `-o, --output-file` | `-o cap.vcd` | 없으면 stdout. `-O` 없으면 확장자로 형식 추정 (5.4절) |
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

| 항목 | sigrok-cli | la5016 | 이유 |
|---|---|---|---|
| `--extract-firmware` | 없음 (별도 스크립트) | 내장 | Mac용 추출기가 sigrok에 없음 |
| `-o`만 주고 `-O` 생략 | `srzip` | 확장자로 추정 (`.vcd`→vcd, `.csv`→csv, `.bin`→binary), 모르는 확장자면 사용 오류 | 1차는 srzip 미지원 |
| 지원 드라이버 | 다수 | `kingst-la2016` (LA5016)만 | |
| 출력 형식 | 다수 | bits, hex, csv, vcd, binary | |
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
2. `la5016 --extract-firmware` — 파일 목록, 크기, SHA-256
3. `la5016 --scan -l 5` — FX2 업로드·재열거 후 PID, EEPROM magic(**a1/a2**), `REG_RUN`
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
| 분주 | 300 MSa/s 요청 → 400 MSa/s로 동작 (경고 출력) |
| 트리거 | `r`/`f` 모두 트리거 지점 전후 값이 0→1 / 1→0, captureratio 50 % 근처 |
| RLE | 시퀀스 번호 끊김 0 (모든 캡처) |
| 처리량 | pyusb 동기 read **42.9 MB/s** (55.7 MB). 스트리밍 최대 25 MB/s보다 큼 |
| Ctrl-C | 첫 번째: HALT 후 1.8 s 분량 저장, 종료 0. 두 번째: 종료 130, `-o` 파일 삭제 |
| 관찰 | 입력에 히스테리시스가 없어, 문턱 근처를 천천히 지나는 사인파에서 2샘플 수준의 글리치가 생김 |

## 10. 미해결

- 문턱 전압 −4~+4 V 연속값 지원 여부 (드라이버 공식은 ±4 V까지 계산함)
- Linux udev / Windows WinUSB 환경
