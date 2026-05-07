# Docstract - AWS Intelligent Document Processing (IDP)

An end-to-end document processing pipeline built on AWS. Upload a PDF, and the system automatically extracts text, classifies the document type, extracts structured fields, validates against a schema, and optionally indexes the content into OpenSearch Serverless for semantic search and RAG-based Q&A.

---

## Architecture

```
S3 Upload (PDF)
      │
      ▼
  Ingest Lambda
      │ triggers
      ▼
Step Functions Pipeline
  ├── Textract (OCR)
  ├── Classify (Bedrock — Nova Pro)
  ├── Human Review (optional, confidence-gated at 0.90)
  ├── Extract structured fields (Bedrock — Nova Pro)
  ├── Validate schema
  ├── Move to processed/rejected
  └── Index to OpenSearch Serverless (optional)
          │
          ▼
    Search / Q&A API (Lambda Function URL)
          │
          ▼
       React UI
```

### CDK Stacks

| Stack | Purpose |
|-------|---------|
| `SharedInfraStack` | Shared S3 output bucket used by all stacks |
| `IdpSearchStack` | OpenSearch Serverless collection, indexer Lambda, search API, QA API, API router — deployed only when `enableSearch=true` |
| `CdkStack` | Full IDP pipeline — S3 buckets, DynamoDB tables, Step Functions, all processing Lambdas |

### Supported Document Types

| Type | Description |
|------|-------------|
| `DriverLicense` | US driver's license |
| `AutoPolicyDeclarations` | Auto insurance policy declarations page |
| `HCFA1500` | Medical claim form (CMS-1500) |
| `CarDamagePhoto` | Vehicle damage photo |
| `Other` | Unrecognized / out-of-scope documents |

---

## Prerequisites

- [AWS CLI](https://aws.amazon.com/cli/) configured with a profile that has sufficient IAM permissions
- [AWS CDK v2](https://docs.aws.amazon.com/cdk/v2/guide/getting_started.html) — `npm install -g aws-cdk`
- Node.js 18+
- Python 3.12
- Bedrock model access enabled in your AWS account for:
  - `amazon.titan-embed-text-v2:0` (embeddings)
  - `us.amazon.nova-pro-v1:0` via a cross-region inference profile (classification, extraction, Q&A)

---

## Project Structure

```
ak-idp/
├── cdk/                        # CDK infrastructure (TypeScript)
│   ├── bin/cdk.ts              # Stack entry point
│   ├── lib/
│   │   ├── cdk-shared-infra-stack.ts
│   │   ├── cdk-stack.ts        # Main IDP pipeline stack
│   │   └── cdk-search-stack.ts # Optional search stack
│   └── cdk.json                # CDK config + context defaults
├── lambdas/                    # Python / Node.js Lambda handlers
│   ├── ingest/                 # S3 trigger → starts Step Functions
│   ├── textract_start/         # Initiates Amazon Textract job
│   ├── textract_get/           # Polls Textract for results
│   ├── classify/               # Bedrock-based document classification
│   ├── extract/                # Structured field extraction
│   ├── validate_schema/        # JSON schema validation
│   ├── create_review_request/  # Human review task creation
│   ├── review_api/             # Human review decision handler
│   ├── move_to_processed/      # Archives approved documents
│   ├── move_to_rejected/       # Archives rejected documents
│   ├── search_indexer/         # Chunks + embeds + indexes to OpenSearch
│   ├── search_api/             # Semantic search endpoint
│   ├── qa_api/                 # RAG question-answering endpoint
│   └── api_router/             # HTTP router (Function URL entry point)
├── layers/
│   ├── common/                 # Shared Python utilities
│   └── opensearch/             # opensearch-py + dependencies
├── config/
│   └── schema_registry/        # Per-document-type JSON schemas and extraction prompts
└── ui/                         # React + Vite frontend
    ├── src/pages/
    │   ├── SearchPage.tsx
    │   └── QaPage.tsx
    └── .env.local.example      # UI environment variable template
```

---

## Deployment

### 1. Install CDK dependencies

```bash
cd cdk
npm install
```

### 2. Bootstrap CDK (first time only per account/region)

```bash
AWS_PROFILE=your-profile npx cdk bootstrap
```

### 3. Deploy — with OpenSearch search enabled (default)

```bash
AWS_PROFILE=your-profile npx cdk deploy --all
```

This deploys all three stacks: `SharedInfraStack`, `IdpSearchStack`, and `CdkStack`.

### 4. Deploy — without OpenSearch (pipeline only)

```bash
AWS_PROFILE=your-profile npx cdk deploy --all -c enableSearch=false
```

Only `SharedInfraStack` and `CdkStack` are deployed. The Step Functions pipeline runs normally and skips the indexing step.

### 5. Note the stack outputs

After deploy, CDK prints outputs. Note:

| Output | Description |
|--------|-------------|
| `CdkStack.InputBucket` | Upload PDFs here to trigger the pipeline |
| `IdpSearchStack.ApiBaseUrl` | Base URL for the search/QA API router |
| `IdpSearchStack.AossEndpoint` | OpenSearch Serverless collection endpoint |

---

## Running the Pipeline

### Upload a document

```bash
aws s3 cp your-document.pdf s3://<InputBucket>/incoming/ --profile your-profile
```

This triggers the Ingest Lambda, which starts the Step Functions state machine. You can monitor progress in the [Step Functions console](https://console.aws.amazon.com/states).

### Pipeline stages

1. **Textract** — extracts raw text from the PDF
2. **Classify** — Bedrock identifies the document type and confidence score
3. **Human review** — if confidence < 0.90 or type is `Other`, the pipeline pauses and waits for a human decision via the Review API
4. **Extract** — Bedrock extracts structured fields based on the document type schema
5. **Validate** — extracted fields are validated against the JSON schema; invalid documents move to `rejected/`
6. **Index** — chunks of text are embedded (Titan v2) and indexed to OpenSearch Serverless (if `enableSearch=true`)

---

## Search & Q&A API

Base URL: the `ApiBaseUrl` output from `IdpSearchStack`.

### Health check

```bash
curl https://<ApiBaseUrl>/
```

### Semantic search

```bash
curl -X POST https://<ApiBaseUrl>/search \
  -H "Content-Type: application/json" \
  -d '{
    "q": "patient diagnosis",
    "size": 5,
    "filters": {
      "doc_types": ["HCFA1500"],
      "min_confidence": 0.9
    }
  }'
```

### RAG Question & Answer

```bash
curl -X POST https://<ApiBaseUrl>/qa \
  -H "Content-Type: application/json" \
  -d '{"question": "What is the policy number on the declarations page?"}'
```

Response includes an answer and citations with page references.

---

## UI

The React frontend provides Search and Q&A pages.

### Setup

```bash
cd ui
npm install

# Copy the env template and fill in your deployed API URL
cp .env.local.example .env.local
# Edit .env.local and set VITE_SEARCH_URL and VITE_QA_URL to your ApiBaseUrl
```

### Run locally

```bash
npm run dev
# Open http://localhost:5173
```

### Build for production

```bash
npm run build
```

---

## Configuration

### Search toggle (`cdk/cdk.json`)

```json
{
  "context": {
    "enableSearch": "true"
  }
}
```

Set to `"false"` to deploy without OpenSearch. Can also be passed at deploy time:

```bash
npx cdk deploy --all -c enableSearch=false
```

### Document schemas

Each document type has its own directory under `config/schema_registry/doc_types/<Type>/`:

```
<Type>/
├── base.schema.json       # Base field definitions
└── v1/
    ├── typed.schema.json  # Full JSON schema for validation
    ├── mapping.json       # OpenSearch field mappings
    └── extraction_prompt.txt  # Bedrock extraction prompt
```

To add a new document type, create a new directory following the same structure and add it to `config/schema_registry/registry.json`.

---

## Cleanup

To tear down all deployed stacks:

```bash
AWS_PROFILE=your-profile npx cdk destroy --all
```

Note: S3 buckets and DynamoDB tables use `RemovalPolicy.RETAIN` by default and will **not** be deleted automatically. Delete them manually from the AWS console if needed.

---

## Key AWS Services Used

- **Amazon S3** — document storage (input, output, config)
- **AWS Step Functions** — pipeline orchestration
- **Amazon Textract** — OCR and document text extraction
- **Amazon Bedrock** — classification, extraction, embedding, Q&A (Nova Pro + Titan Embed v2)
- **Amazon OpenSearch Serverless** — vector + keyword hybrid search index
- **AWS Lambda** — all processing and API logic
- **Amazon DynamoDB** — document metadata and human review tasks
- **Amazon SNS** — human review notifications
- **AWS CDK** — infrastructure as code
