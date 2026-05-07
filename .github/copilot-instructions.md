# Copilot instructions for ak-idp

Keep this short — focus on immediately useful facts for an AI coding agent working in this repository.

- **Big picture:** This is an AWS CDK TypeScript project that provisions S3 buckets, Step Functions, and Python Lambdas. The CDK app lives in the `cdk/` folder; the Lambda source lives in the top-level `lambdas/` folder. The Step Functions state machine orchestrates a Textract pipeline: `ingest` -> `textract_start` -> poll (`textract_get`) -> write results.

- **Key files:**
  - CDK app and stack: [cdk/lib/cdk-stack.ts](cdk/lib/cdk-stack.ts)
  - CDK README / commands: [cdk/README.md](cdk/README.md)
  - CDK package.json / scripts: [cdk/package.json](cdk/package.json)
  - Lambdas (Python handlers): [lambdas/ingest/handler.py](lambdas/ingest/handler.py), [lambdas/textract-start/handler.py](lambdas/textract-start/handler.py), [lambdas/textract-get/handler.py](lambdas/textract-get/handler.py)

- **Architecture notes an agent should know:**
  - CDK constructs create two S3 buckets: a `RawDocumentsBucket` (ingest source) and `ProcessedOutputBucket` (results). The stack exports their names.
  - Lambdas are Python 3.12 functions; CDK references their code with `lambda.Code.fromAsset('../lambdas/<name>')` (paths are relative to the `cdk/` directory). Keep relative packaging in mind when running local build/zip tasks.
  - Step Functions definition: the stack wires three Lambdas via `LambdaInvoke` tasks and uses a `Choice`/`Wait` loop to poll Textract job status.
  - Environment variables used by Lambdas: `STATE_MACHINE_ARN` (ingest), `OUTPUT_BUCKET` (textract-get). The handlers expect `lambda_handler(event, context)`.

- **Developer workflows (practical commands):**
  - Install and build CDK: run in `cdk/`:

    npm install
    npm run build

  - Run tests (CDK unit tests use Jest):

    cd cdk && npm run test

  - Typical CDK operations (use `npx` from repo root or from `cdk/`):

    npx cdk synth
    npx cdk diff
    npx cdk deploy

  - Lambda development: the repo contains a Python virtualenv at `env/` for local dev — but runtime in AWS uses built-in `boto3`/AWS SDK. No extra packaging script is present; lambdas are deployed as-is from the `lambdas/` folders via CDK.

- **Code patterns & conventions:**
  - CDK code is strict TypeScript (`tsconfig.json` uses `strict: true` and `noImplicitAny`). When changing the stack, add explicit types and keep variable names unique (the stack currently declares multiple `done`/`isDone` nodes — be careful with name reuse).
  - Lambda handlers follow the simple pattern `lambda_handler(event, context)` and return small JSON shapes used directly by Step Functions (e.g., `{'status': 'IN_PROGRESS', ...}` or `{'status': 'SUCCEEDED', 'textractResultKey': '...'}`). Respect these shapes when modifying flows.
  - Step Functions tasks use `outputPath: '$.Payload'` so Lambda return values become the state's input — returning extra keys will flow to the next task.

- **Integration & external dependencies:**
  - CDK dependencies: `aws-cdk-lib` (v2), `constructs` and TypeScript toolchain in `cdk/package.json`.
  - Lambdas rely on AWS-managed SDKs (`boto3`, Textract) available in the Lambda runtime; avoid vendoring unless adding third-party libs.

- **What to watch for (pitfalls discovered in repository):**
  - TypeScript strictness: fix implicit-any errors by adding explicit annotations (example: `isDone` Choice wiring in `cdk/lib/cdk-stack.ts` currently triggers a `noImplicitAny`-style error if variables or chained references create self-references). Keep declarations local and typed.
  - Asset paths: CDK `fromAsset('../lambdas/...')` is relative to `cdk/` — packaging or CI scripts must run from the correct working directory.

- **Examples to follow when editing:**
  - Add a new Lambda: create `lambdas/<name>/handler.py` with `lambda_handler`, then add a `new lambda.Function(..., code: lambda.Code.fromAsset('../lambdas/<name>'), ...)` entry in `cdk/lib/cdk-stack.ts` and wire permissions/environment variables as needed.

If anything here is unclear or you want more detail for CI packaging, tests, or a recommended local invoke workflow, tell me which area to expand. 
