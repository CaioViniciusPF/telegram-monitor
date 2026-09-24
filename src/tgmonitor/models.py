"""Modelos compartilhados entre repositório e API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from tgmonitor.matching.matcher import InvalidTermError, RuleSpec, clean_terms

ChatKind = Literal["channel", "megagroup", "group", "other"]


class Chat(BaseModel):
    id: int
    title: str
    username: str | None
    kind: ChatKind
    monitored: bool
    updated_at: int


class RuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    include_terms: list[str] = Field(min_length=1, max_length=100)
    exclude_terms: list[str] = Field(default_factory=list, max_length=200)
    max_price_cents: int | None = Field(default=None, gt=0)
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("nome vazio")
        return v

    @field_validator("include_terms", "exclude_terms")
    @classmethod
    def _valid_terms(cls, v: list[str]) -> list[str]:
        try:
            return clean_terms(v)
        except InvalidTermError as e:
            raise ValueError(str(e)) from e

    @field_validator("include_terms")
    @classmethod
    def _at_least_one(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("informe ao menos um termo de inclusão")
        return v


class Rule(RuleIn):
    id: int
    created_at: int
    updated_at: int

    def to_spec(self) -> RuleSpec:
        return RuleSpec(
            id=self.id,
            name=self.name,
            include_terms=tuple(self.include_terms),
            exclude_terms=tuple(self.exclude_terms),
            max_price_cents=self.max_price_cents,
            enabled=self.enabled,
        )


class MatchedRuleRef(BaseModel):
    id: int
    name: str


class Alert(BaseModel):
    id: int
    chat_id: int
    chat_title: str
    message_id: int
    link: str | None
    text: str
    price_cents: int | None
    matched_rules: list[MatchedRuleRef]
    is_edit: bool
    delivered: bool
    created_at: int
    previous_price_cents: int | None = None  # preenchido quando um post editado baixou de preço


class RuntimeSettings(BaseModel):
    """Ajustes editáveis pela tela (persistidos na tabela settings)."""

    dedup_window_hours: float = Field(default=6, gt=0, le=24 * 30)
    alert_retention_days: int = Field(default=30, ge=1, le=3650)
    max_new_message_age_hours: float = Field(default=3, gt=0, le=72)
    max_edit_age_hours: float = Field(default=24, gt=0, le=24 * 7)
    paused: bool = False
