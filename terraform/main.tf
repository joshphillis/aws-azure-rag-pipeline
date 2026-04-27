terraform {
  required_version = ">= 1.5.0"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.27"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.13"
    }
  }
}

# ── Variables ──────────────────────────────────────────────────────────────────

variable "namespace" {
  description = "Kubernetes namespace"
  type        = string
  default     = "rag-system"
}

variable "image" {
  description = "RAG API Docker image"
  type        = string
  default     = "ghcr.io/joshphillis/aws-azure-rag-pipeline:latest"
}

variable "replicas" {
  description = "Number of RAG API replicas"
  type        = number
  default     = 2
}

variable "openai_api_key" {
  description = "OpenAI API key (use External Secrets in production)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "qdrant_storage_size" {
  description = "Qdrant persistent volume size"
  type        = string
  default     = "10Gi"
}

# ── Namespace ──────────────────────────────────────────────────────────────────

resource "kubernetes_namespace" "rag" {
  metadata {
    name = var.namespace
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

# ── Secret ─────────────────────────────────────────────────────────────────────

resource "kubernetes_secret" "rag_credentials" {
  metadata {
    name      = "rag-credentials"
    namespace = kubernetes_namespace.rag.metadata[0].name
  }
  data = {
    OPENAI_API_KEY = var.openai_api_key
  }
}

# ── Qdrant (via Helm) ──────────────────────────────────────────────────────────

resource "helm_release" "qdrant" {
  name       = "qdrant"
  namespace  = kubernetes_namespace.rag.metadata[0].name
  repository = "https://qdrant.github.io/qdrant-helm"
  chart      = "qdrant"
  version    = "0.8.4"

  set {
    name  = "replicaCount"
    value = "1"
  }

  set {
    name  = "persistence.size"
    value = var.qdrant_storage_size
  }
}

# ── RAG API Deployment ─────────────────────────────────────────────────────────

resource "kubernetes_deployment" "rag_api" {
  metadata {
    name      = "rag-api"
    namespace = kubernetes_namespace.rag.metadata[0].name
    labels    = { app = "rag-api" }
  }

  spec {
    replicas = var.replicas

    selector {
      match_labels = { app = "rag-api" }
    }

    template {
      metadata {
        labels = { app = "rag-api" }
        annotations = {
          "prometheus.io/scrape" = "true"
          "prometheus.io/port"   = "8000"
          "prometheus.io/path"   = "/metrics"
        }
      }

      spec {
        container {
          name  = "rag-api"
          image = var.image

          port { container_port = 8000 }

          env {
            name  = "QDRANT_URL"
            value = "http://qdrant:6333"
          }

          env {
            name  = "QDRANT_COLLECTION"
            value = "infra-knowledge"
          }

          env_from {
            secret_ref {
              name = kubernetes_secret.rag_credentials.metadata[0].name
            }
          }

          resources {
            requests = { cpu = "200m", memory = "512Mi" }
            limits   = { cpu = "1000m", memory = "1Gi" }
          }

          liveness_probe {
            http_get { path = "/health", port = 8000 }
            initial_delay_seconds = 15
            period_seconds        = 20
          }

          readiness_probe {
            http_get { path = "/ready", port = 8000 }
            initial_delay_seconds = 10
            period_seconds        = 10
          }
        }
      }
    }
  }
}

# ── Services ───────────────────────────────────────────────────────────────────

resource "kubernetes_service" "rag_api" {
  metadata {
    name      = "rag-api"
    namespace = kubernetes_namespace.rag.metadata[0].name
  }
  spec {
    selector = { app = "rag-api" }
    port {
      port        = 80
      target_port = 8000
    }
    type = "ClusterIP"
  }
}

# ── Outputs ────────────────────────────────────────────────────────────────────

output "rag_api_service" {
  value = kubernetes_service.rag_api.metadata[0].name
}

output "qdrant_release" {
  value = helm_release.qdrant.name
}

output "namespace" {
  value = kubernetes_namespace.rag.metadata[0].name
}
