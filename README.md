# aws-azure-rag-pipeline

A retrieval-augmented generation (RAG) pipeline for infrastructure documentation and code. Designed to give AI agents grounded, context-aware answers about your AWS and Azure infrastructure.

Pairs with [aws-azure-mcp-infra-server](https://github.com/joshphillis/aws-azure-mcp-infra-server) — agents can call `query_knowledge_base` as an MCP tool to retrieve relevant context before taking infrastructure actions.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│              AI Agent (Claude / GPT)                    │
│       (aws-bedrock-eks-agent-platform)                  │
│       (azure-openai-aks-agent-platform)                 │
└──────────────┬──────────────────────┬───────────────────┘
               │ MCP Tool Call        │ HTTP POST /query
               ▼                      ▼
┌──────────────────────┐   ┌─────────────────────────────┐
│  aws-azure-mcp-      │   │   aws-azure-rag-pipeline    │
│  infra-server        │──▶│                             │
│  (live infra state)  │   │  FastAPI  ──▶  Qdrant       │
└──────────────────────┘   │  /query       (vectors)     │
                           │  /ingest                    │
                           └──────────────┬──────────────┘
                                          │ embed
                                          ▼
                                   OpenAI / Azure OpenAI
                                   text-embedding-3-small
```

---

## What Gets Ingested

| Content Type | Extensions | Chunking Strategy |
|---|---|---|
| Documentation | `.md` `.txt` `.rst` | Split by heading, then by ~512 tokens |
| Runbooks | `.md` files in `/runbooks` | Same as docs |
| Code | `.py` `.ts` `.go` `.tf` `.hcl` | Split by function/class, then by 100 lines |
| Config | `.yaml` `.yml` `.json` | By line count |

---

## Quickstart

### 1. Configure

```bash
git clone https://github.com/joshphillis/aws-azure-rag-pipeline.git
cd aws-azure-rag-pipeline
cp .env.example .env
# Add your OPENAI_API_KEY (or Azure OpenAI credentials)
```

### 2. Run locally

```bash
docker compose up --build
```

This starts:
- **RAG API** on port `8000`
- **Qdrant** on port `6333`

### 3. Ingest your infrastructure repos

```bash
# Ingest a local repo
curl -X POST http://localhost:8000/ingest/directory \
  -H "Content-Type: application/json" \
  -d '{"directory": "/path/to/aws-bedrock-eks-agent-platform", "repo_name": "bedrock-agent"}'

# Or ingest raw text
curl -X POST http://localhost:8000/ingest/text \
  -H "Content-Type: application/json" \
  -d '{"text": "# EKS Runbook\n...", "source": "runbooks/eks.md", "doc_type": "runbook"}'
```

### 4. Query

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{"query": "How do I scale the EKS node group?", "top_k": 5}'
```

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Liveness probe |
| `/ready` | GET | Readiness probe (checks Qdrant) |
| `/info` | GET | Collection statistics |
| `/query` | POST | Semantic search |
| `/ingest/text` | POST | Ingest raw text |
| `/ingest/directory` | POST | Ingest a directory (background) |

### Query request

```json
{
  "query": "How do I configure IRSA for EKS?",
  "top_k": 5,
  "doc_type": "docs",
  "source_filter": null
}
```

---

## Kubernetes Deployment

```bash
cd terraform
terraform init
terraform apply \
  -var="openai_api_key=YOUR_KEY" \
  -var="replicas=2"
```

---

## Configuration

| Variable | Description | Default |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI API key | — |
| `AZURE_OPENAI_ENDPOINT` | Azure OpenAI endpoint (optional) | — |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI key (optional) | — |
| `QDRANT_URL` | Qdrant connection URL | `http://localhost:6333` |
| `QDRANT_COLLECTION` | Collection name | `infra-knowledge` |
| `EMBEDDING_MODEL` | Embedding model | `text-embedding-3-small` |

---

## Related Repositories

| Repo | Description |
|---|---|
| [aws-azure-mcp-infra-server](https://github.com/joshphillis/aws-azure-mcp-infra-server) | MCP server for live AWS/Azure infra + agent memory |
| [aws-bedrock-eks-agent-platform](https://github.com/joshphillis/aws-bedrock-eks-agent-platform) | AI agent platform on AWS Bedrock + EKS |
| [azure-openai-aks-agent-platform](https://github.com/joshphillis/azure-openai-aks-agent-platform) | AI agent platform on Azure OpenAI + AKS |

---

## License

MIT
