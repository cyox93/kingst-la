# la5016 1차 구현 계획

> 설계: [design.md](design.md). 커밋은 1차 구현 완료 후 한 번.

**목표:** LA5016을 pyusb로 초기화·캡처하고 sigrok-cli 호환 옵션으로 bits/hex/csv/vcd/binary를 출력한다.

**구조:** `cli → device → usb(Transport)`, `cli → rle → output(Sink)`. 순수 모듈은 장비 없이 TDD.

**스택:** Python ≥ 3.9, pyusb, numpy, pytest.

## 작업 순서

| # | 작업 | 파일 | 검증 |
|---|---|---|---|
| 0 | 골격: `pyproject.toml`, `LICENSE`(GPL-3), `pipeline.py`(Meta, Channel, Sink) | `src/la5016/{__init__,__main__,pipeline}.py` | `pip install -e .[dev]`, `python -m la5016 -V` |
| 1 | **spike** 펌웨어 추출: 실제 KingstVIS에서 rcc 테이블 탐색법 확정 | `src/la5016/firmware.py`, `tests/test_firmware.py` | 가짜 rcc 바이트열 테스트 + 실제 추출 후 크기·SHA-256, FX2 fw가 Intel HEX면 blob 변환 |
| 2 | options 파서 | `src/la5016/options.py`, `tests/test_options.py` | SI 접미사, 채널, 트리거, threshold 목록, samplerate 범위·effective, 오류 케이스 |
| 3 | RLE 디코더 | `src/la5016/rle.py`, `tests/test_rle.py` | 반복수 0/255, 경계 걸침, 청크 상한, 트리거 이벤트(0 포함), samples 한도, 시퀀스 경고 |
| 4 | 출력 형식 | `src/la5016/output/*.py`, `tests/test_output.py`, `tests/golden/*` | libsigrok `src/output/*.c` 기반 golden, 트리거 있음/없음 |
| 5 | USB Transport + FX2 업로드·재열거 | `src/la5016/usb.py` | 실기: 장치 열기, iProduct, port path |
| 6 | device: 식별(EEPROM)·FPGA 체크/업로드/enable·설정·캡처·다운로드·close | `src/la5016/device.py`, `tests/test_device.py` | FakeTransport 레지스터 바이트 테스트 + 실기 `--scan` |
| 7 | CLI 조립 | `src/la5016/cli.py`, `tests/test_cli.py` | FakeTransport end-to-end, 종료 코드, Ctrl-C |
| 8 | 실기 검증 | `tests/test_hw.py`(`-m hw`), `examples/measure.py` | CH0 사인파 캡처 → VCD → 주파수·duty 계산, 처리량 측정 |

## 완료 기준

- `pytest` 전부 통과 (장비 없음)
- 실기: CH0 입력 신호를 캡처해 주기를 계산하고, 발생기 설정과 비교
- VCD가 형식상 유효 (GTKWave/PulseView 호환 헤더)

---

# 2차 구현 계획 — 프로토콜 디코더

| # | 작업 | 파일 | 검증 |
|---|---|---|---|
| 1 | 조건 매칭 + 참조 구현 | `srd/conditions.py`, `tests/test_srd_conditions.py` | 무작위 대조, skip/엣지/첫 샘플/미연결 |
| 2 | api + session (스레드 핸드오프, put, stack, flush, 예외) | `srd/api.py`, `srd/session.py`, `tests/test_srd_engine.py` | 가짜 decoder |
| 3 | loader (메타데이터 검증, 옵션 변환, import 경로) | `srd/loader.py` | 가짜 decoder 패키지 디렉터리 |
| 4 | install (tarball → decoders/) | `srd/install.py` | 로컬 tarball로 테스트 |
| 5 | annotate (-A, -B, samplenum, loglevel 형식) + `-L`, `--show -P` | `srd/annotate.py`, `cli.py` | 문자열 비교 |
| 6 | 실제 decoder 테스트 | `tests/test_srd_decoders.py` (`-m decoders`) | uart/i2c/spi/timing |
| 7 | 실기 | `timing` on CH0 | 1 ms |

---

# 3차 구현 계획 — 연속 스트리밍

| # | 작업 | 파일 | 검증 |
|---|---|---|---|
| 1 | de-interleave | `stream.py`, `tests/test_stream.py` | 참조 인코더 왕복, 청크 분할, 처리량 > 25 MB/s |
| 2 | 수신 스레드 | `usb.py` `BulkReader` | 실기 처리량·끊김 |
| 3 | 장치 시작/정지 | `device.py` `Device.stream`, `Stream` | FakeTransport 순서 테스트 |
| 4 | CLI | `cli.py` `-c continuous=on` | 가짜 장치 end-to-end, 끊김 시 출력 유지 |
| 5 | 실기 | `tests/test_hw.py` | 실시간성, 분주비 규칙 |
