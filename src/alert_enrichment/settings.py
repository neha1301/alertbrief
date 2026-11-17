"""Configuration via environment variables (12-factor, no config files baked in)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- HTTP server -------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    # --- Slack -------------------------------------------------------------
    # Either provide a webhook URL (real delivery) or keep sink mode, which
    # writes the exact webhook body to a JSON-lines file for local demos.
    slack_webhook_url: SecretStr | None = None
    slack_mode: Literal["auto", "webhook", "sink"] = "auto"
    slack_sink_path: str = "var/slack-sink.jsonl"
    slack_timeout_seconds: float = 5.0
    slack_max_retries: int = 2

    # --- Deduplication -----------------------------------------------------
    dedup_backend: Literal["memory", "redis"] = "memory"
    dedup_ttl_seconds: int = Field(default=600, ge=1)
    dedup_stale_reservation_seconds: float = Field(default=30.0, gt=0)
    redis_url: str | None = None

    # --- Kubernetes --------------------------------------------------------
    kubernetes_in_cluster: bool | None = None
    kubeconfig: str | None = None
    k8s_request_timeout_seconds: float = Field(default=1.5, gt=0)
    enrichment_budget_seconds: float = Field(default=4.0, gt=0)
    max_concurrent_enrichments: int = Field(default=4, ge=1, le=32)

    # --- Enrichment knobs --------------------------------------------------
    log_tail_lines: int = Field(default=80, ge=1, le=5000)
    max_error_lines: int = Field(default=15, ge=1, le=100)
    max_log_containers: int = Field(default=3, ge=1, le=20)
    max_events: int = Field(default=5, ge=0, le=50)
    max_alerts_per_request: int = Field(default=10, ge=1, le=100)

    # --- Links -------------------------------------------------------------
    alertmanager_url: str = ""
    grafana_url: str = ""

    def slack_enabled(self) -> bool:
        return self.slack_mode != "sink"

    def slack_webhook_value(self) -> str:
        """Return the configured webhook, treating blank values as unset.

        Kubernetes Secrets created from an empty literal are ``""`` rather than
        absent, so truthiness of ``SecretStr`` alone is not enough.
        """

        if self.slack_webhook_url is None:
            return ""
        return self.slack_webhook_url.get_secret_value().strip()

    def resolved_slack_mode(self) -> Literal["webhook", "sink"]:
        if self.slack_mode != "auto":
            return self.slack_mode
        return "webhook" if self.slack_webhook_value() else "sink"
