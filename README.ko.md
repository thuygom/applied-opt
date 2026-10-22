# Applied Opt

**AI 애플리케이션 코드를 리뷰하고, 최적화 diff를 제안하고, 속도와 기능 보존을 함께 검증하는 프레임워크입니다.**

Python 3.11+ · Apache-2.0 · 코어 런타임 외부 의존성 없음 · 호스팅 옵션은 Python 3.12+ 및 Gunicorn 사용 · v0.1 alpha

## 하는 일

| 기능 | 내용 |
| --- | --- |
| 코드리뷰 | 파일·라인 근거로 직렬 호출, 종속 관계, 무제한 병렬 호출, 재시도, 캐시 문제 후보를 찾습니다. |
| 수정 제안 | 안전성을 명시한 독립 async 호출 두 개를 병렬화하는 diff를 만듭니다. |
| 실행 계획 | 의존성·GPU/API 자원 제한·경합 가정을 반영해 직렬과 병렬을 비교합니다. |
| 흐름도 컴파일 | 코드 구조·실행 계획·측정 trace를 Mermaid, PlantUML, SVG로 변환합니다. |
| 라우팅 | 최초 판단 하나로 프롬프트·출력 스키마·후속 검토를 일치시킵니다. |
| 검증 | 전후 p50/p95와 필수 기능 검사 결과를 비교합니다. |
| 대시보드·스킬 | 보고 화면과 의미·기능 중심의 에이전트 리뷰 절차를 제공합니다. |
| MCP 서버 | 코드리뷰·실행 계획·흐름도·측정 비교를 로컬 stdio 도구로 제공합니다. |

코드 스캐너는 로컬 Python AST 규칙입니다. 입력 코드를 실행하거나 모델에 전송하지 않습니다. 에이전트 스킬은 별도로 업무 맥락과 의미를 검토하며, 실행 환경에 따라 사용할 수 있는 모델·도구가 달라집니다.

## 실행

소스 폴더에서 실행합니다. 공개 PyPI 배포를 전제로 하지 않습니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
applied-opt review examples/review_target.py --format markdown
applied-opt demo --output report.json --repetitions 7
applied-opt serve --report report.json --host 127.0.0.1 --port 8787
```

브라우저에서 `http://127.0.0.1:8787`을 엽니다. Windows에서는 `.venv\Scripts\activate`로 가상환경을 활성화합니다.

```bash
applied-opt review ./your-project --base main --format sarif --output review.sarif
applied-opt optimize examples/review_target.py \
  --policy examples/review_policy.toml --output candidate.diff
applied-opt compare baseline.json candidate.json --output comparison.json
```

수정은 기본적으로 diff만 생성합니다. `--apply`를 추가하면 파일에 적용합니다. `--base`는 Git의 추가·변경 라인에 해당하는 지적을 추립니다. 저장소 전체의 의미적 정확성을 보장하는 옵션은 아닙니다.

## 흐름도와 MCP 연결

```bash
applied-opt graph examples/review_target.py --from source --format svg --output flow.svg
applied-opt graph report.json --from report --variant candidate --format mermaid --output flow.mmd
applied-opt graph work.json --from plan --format plantuml --output flow.puml
```

공통 graph JSON을 기준으로 코드 구조·계획·trace를 변환합니다. Mermaid·PlantUML·SVG는 출력 형식이며, 이들 언어를 임의로 읽어 되돌리는 범용 파서는 아닙니다.

MCP 클라이언트의 stdio 명령을 `applied-opt mcp` 또는 `python -m applied_opt.mcp_server`로 설정하면 `review_code`, `plan_execution`, `compile_flow`, `compare_runs` 네 도구를 사용할 수 있습니다. 입력 코드를 실행하거나 저장소를 수정하지 않습니다. [연결 설정·지원 범위](docs/mcp.md)를 참고하세요.

## 최적화 원칙

**병렬화도 비용이 있습니다.** 독립 I/O는 겹쳐 실행할 수 있지만, 같은 GPU·DB 연결·API 제한을 공유하면 p95와 오류율이 나빠질 수 있습니다. 필요한 호출 수가 같으면 병렬화만으로 토큰 비용이 줄지도 않습니다.

**짧은 입력이라는 이유만으로 일정·할일을 생략하지 않습니다.** 라우터가 실패하거나 판단이 불확실하면 전체 분석을 유지합니다. 명시한 필수 기능과 근거가 있는 기능도 보존합니다. 신뢰도 숫자는 실제 정확도를 보증하지 않습니다.

**캐시는 기본으로 꺼져 있습니다.** 결정적이고 읽기 전용인 단계에 명시적으로 켜며, 사용자·권한·데이터·모델·프롬프트·기준 시각·입력을 키에 포함합니다. LLM 생성 결과를 자동 캐싱하지 않습니다.

데모 수치는 합성 지연과 고정 정답으로 검증한 로컬 실행 결과입니다. 운영 LLM 성능, 실제 NLP 분기 정확도, 15초 SLA 달성 실적으로 해석하면 안 됩니다.

## 대시보드와 리뷰 스킬

**웹서버와 호스팅 설정을 이 저장소에 포함했습니다.** `applied-opt host`로 실행하는 Gunicorn 서버 하나가 대시보드, 인증된 Python 코드리뷰 API, 흐름도 컴파일, 보고서 조회를 함께 제공합니다. 리버스 프록시의 HTTPS 공개 주소와 내부 포트가 달라도 선언한 공개 origin을 기준으로 검증합니다.

Docker Compose와 Python 3가 설치되어 있다면 저장소에서 다음 한 줄로 실행합니다.

```bash
python3 deploy/bootstrap.py --up
```

**http://localhost:8787**을 열고, 생성된 `.env`의 `APPLIED_OPT_TOKEN` 값을 대시보드에 입력하세요. 기존 환경 파일은 덮어쓰지 않습니다. VPS에서는 `python3 deploy/bootstrap.py --domain opt.example.com --up`으로 별도 운영 구성과 Caddy HTTPS를 준비합니다. DNS를 해당 서버로 연결하고 80·443 포트를 열어야 하며 인증서 상태는 볼륨에 보존됩니다.

[Render에 배포](https://render.com/deploy?repo=https://github.com/thuygom/applied-opt) 링크는 저장소의 [`render.yaml`](render.yaml)을 읽어 같은 Docker 웹서버를 배포합니다. 토큰은 Render가 생성하고 서버가 플랫폼 공개 URL과 포트를 사용합니다. [호스팅 설명](docs/hosting.md)에 로컬 Docker·VPS·Render 실행법, 환경변수, 보고서 연결, 인증·상태 확인 절차가 있습니다. 배포 설정을 제공하는 것이며, 이 저장소 작성만으로 외부 계정·DNS·서비스 배포가 완료되는 것은 아닙니다.

`skills/review-ai-pipeline`에 스킬 소스가 있습니다. 사용하는 에이전트의 스킬 설치 방식에 따라 등록하세요. 자동 등록·활성화는 포함하지 않습니다. CodeRabbit의 리뷰 흐름을 참고했으며, 제휴하거나 그 구현을 포함한 프로젝트는 아닙니다.

[호스팅](docs/hosting.md) · [설계](docs/architecture.md) · [직렬/병렬 판단](docs/tradeoffs.md) · [실제 코드 연결](docs/integration.md) · [흐름도](docs/diagrams.md) · [MCP](docs/mcp.md)
