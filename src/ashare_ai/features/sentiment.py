from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from pydantic import AwareDatetime, Field

from ashare_ai.core.contracts import CanonicalSymbol, Disclosure, FrozenModel, NewsItem

DEFAULT_POSITIVE_TERMS = (
    "增长",
    "预增",
    "中标",
    "回购",
    "增持",
    "突破",
    "profit",
    "growth",
)
DEFAULT_NEGATIVE_TERMS = (
    "亏损",
    "预减",
    "减持",
    "处罚",
    "调查",
    "诉讼",
    "违约",
    "loss",
    "investigation",
)
DEFAULT_RISK_TERMS = ("立案", "退市", "暂停上市", "重大诉讼", "违约", "处罚")


class SentimentFeatures(FrozenModel):
    symbol: CanonicalSymbol
    trading_date: date
    decision_at: AwareDatetime
    document_count: int = Field(ge=0)
    positive_hits: int = Field(ge=0)
    negative_hits: int = Field(ge=0)
    tone_score: float = Field(ge=-1, le=1)
    event_risk_ratio: float = Field(ge=0, le=1)
    official_disclosure_ratio: float = Field(ge=0, le=1)
    completeness: float = Field(ge=0, le=1)


def extract_sentiment_features(
    disclosures: Iterable[Disclosure],
    news: Iterable[NewsItem],
    *,
    symbol: str,
    decision_at: AwareDatetime,
    trading_date: date,
    positive_terms: tuple[str, ...] = DEFAULT_POSITIVE_TERMS,
    negative_terms: tuple[str, ...] = DEFAULT_NEGATIVE_TERMS,
    risk_terms: tuple[str, ...] = DEFAULT_RISK_TERMS,
) -> SentimentFeatures:
    visible_disclosures = [
        item
        for item in disclosures
        if item.symbol == symbol
        and item.available_at <= decision_at
        and item.trading_date <= trading_date
    ]
    visible_news = [
        item
        for item in news
        if symbol in item.related_symbols
        and item.available_at <= decision_at
        and item.trading_date <= trading_date
    ]
    texts = [item.title.casefold() for item in visible_disclosures]
    texts.extend(item.title.casefold() for item in visible_news)
    positive_hits = sum(text.count(term.casefold()) for text in texts for term in positive_terms)
    negative_hits = sum(text.count(term.casefold()) for text in texts for term in negative_terms)
    risk_documents = sum(any(term.casefold() in text for term in risk_terms) for text in texts)
    total_hits = positive_hits + negative_hits
    document_count = len(texts)
    disclosure_count = len(visible_disclosures)
    verified_count = sum(item.official_verified for item in visible_disclosures)
    return SentimentFeatures(
        symbol=symbol,
        trading_date=trading_date,
        decision_at=decision_at,
        document_count=document_count,
        positive_hits=positive_hits,
        negative_hits=negative_hits,
        tone_score=0.0 if total_hits == 0 else (positive_hits - negative_hits) / total_hits,
        event_risk_ratio=0.0 if document_count == 0 else risk_documents / document_count,
        official_disclosure_ratio=(
            0.0 if disclosure_count == 0 else verified_count / disclosure_count
        ),
        completeness=1.0 if document_count else 0.0,
    )
