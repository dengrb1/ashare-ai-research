"""Durable registry of factor-genome candidates and the active pointer.

The registry is the only place that can change which genome the runtime uses.
It never rewrites a stored revision: approving a candidate flips its status and
moves :class:`~ashare_ai.storage.models.ActiveFactorGenome`, so every historical
score, decision and backtest report stays reproducible from the parameters that
produced it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai.core.hashing import stable_hash
from ashare_ai.quant.factor_evolution import FactorCandidate
from ashare_ai.scoring.factor import DEFAULT_FACTOR_GENOME, FactorGenome
from ashare_ai.storage.models import ActiveFactorGenome, FactorGenomeVersion

SCOPE = "default"

# The frozen default genome is always available as the bootstrap active version
# even before any candidate has been approved.
DEFAULT_CANDIDATE_ID = stable_hash(
    {"genome": DEFAULT_FACTOR_GENOME.model_dump(mode="json"), "kind": "default"}
)


def load_active_factor_genome() -> FactorGenome:
    """Read the active pointer, falling back to the frozen default on failure."""

    try:
        from ashare_ai.storage.database import SessionLocal

        with SessionLocal() as session:
            return FactorGenomeRegistry(session).active_genome()
    except Exception:
        return DEFAULT_FACTOR_GENOME


class FactorGenomeRegistry:
    """Read and write the factor-genome version registry."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ read

    def active_genome(self) -> FactorGenome:
        """Return the active genome, falling back to the frozen default."""

        pointer = self.session.get(ActiveFactorGenome, SCOPE)
        if pointer is None:
            return DEFAULT_FACTOR_GENOME
        row = self.session.get(FactorGenomeVersion, pointer.candidate_id)
        if row is None:
            return DEFAULT_FACTOR_GENOME
        return FactorGenome.model_validate(row.genome)

    def active_version(self) -> dict[str, object]:
        """Describe the active version for the admin API."""

        pointer = self.session.get(ActiveFactorGenome, SCOPE)
        if pointer is None:
            return {
                "candidate_id": DEFAULT_CANDIDATE_ID,
                "status": "ACTIVE",
                "source": "DETERMINISTIC",
                "formula_version": DEFAULT_FACTOR_GENOME.formula_version,
                "genome": DEFAULT_FACTOR_GENOME.model_dump(mode="json"),
                "parameter_sha256": DEFAULT_FACTOR_GENOME.parameter_sha256,
                "metrics": None,
                "activated_at": None,
                "activated_by": None,
                "is_default": True,
            }
        row = self.session.get(FactorGenomeVersion, pointer.candidate_id)
        if row is None:
            return {
                "candidate_id": DEFAULT_CANDIDATE_ID,
                "status": "ACTIVE",
                "source": "DETERMINISTIC",
                "formula_version": DEFAULT_FACTOR_GENOME.formula_version,
                "genome": DEFAULT_FACTOR_GENOME.model_dump(mode="json"),
                "parameter_sha256": DEFAULT_FACTOR_GENOME.parameter_sha256,
                "metrics": None,
                "activated_at": None,
                "activated_by": None,
                "is_default": True,
            }
        return {
            "candidate_id": row.candidate_id,
            "status": row.status,
            "source": row.source,
            "formula_version": row.formula_version,
            "genome": row.genome,
            "parameter_sha256": row.parameter_sha256,
            "metrics": row.metrics,
            "activated_at": pointer.activated_at.isoformat(),
            "activated_by": pointer.activated_by,
            "is_default": False,
        }

    def list_versions(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[dict[str, object]]:
        """List stored revisions, newest first."""

        statement = select(FactorGenomeVersion).order_by(
            FactorGenomeVersion.created_at.desc()
        )
        if status:
            statement = statement.where(FactorGenomeVersion.status == status)
        rows = self.session.scalars(statement.limit(max(1, min(limit, 200)))).all()
        return [self._describe(row) for row in rows]

    def get_version(self, candidate_id: str) -> dict[str, object] | None:
        row = self.session.get(FactorGenomeVersion, candidate_id)
        return self._describe(row) if row else None

    # ----------------------------------------------------------------- write

    def store_candidates(
        self, candidates: tuple[FactorCandidate, ...], *, proposals: dict[str, object] | None = None
    ) -> list[str]:
        """Persist candidates idempotently; existing rows are left untouched.

        Re-running the weekly job with the same dataset and parameters produces
        the same ``candidate_id``, so a repeated run stores nothing new.
        """

        stored: list[str] = []
        for candidate in candidates:
            if self.session.get(FactorGenomeVersion, candidate.candidate_id) is not None:
                stored.append(candidate.candidate_id)
                continue
            proposal = (proposals or {}).get(candidate.candidate_id)
            self.session.add(
                FactorGenomeVersion(
                    candidate_id=candidate.candidate_id,
                    status=candidate.status,
                    source=candidate.source,
                    formula_version=candidate.formula_version,
                    genome=candidate.genome.model_dump(mode="json"),
                    parameter_sha256=candidate.genome.parameter_sha256,
                    metrics=candidate.metrics.model_dump(mode="json"),
                    dataset_sha256=candidate.dataset_sha256,
                    proposal_sha256=candidate.proposal_sha256,
                    hypothesis=getattr(proposal, "hypothesis", None),
                    risk_notes=getattr(proposal, "risk_notes", None),
                    explanation=getattr(proposal, "explanation", None),
                    decision_at=candidate.decision_at,
                    created_at=candidate.created_at,
                )
            )
            stored.append(candidate.candidate_id)
        self.session.flush()
        return stored

    def approve(
        self, candidate_id: str, *, user_id: str | None, note: str | None = None
    ) -> dict[str, object]:
        """Activate a candidate, remembering the version it replaced."""

        row = self.session.get(FactorGenomeVersion, candidate_id)
        if row is None:
            raise ValueError("factor genome candidate not found")
        if row.status not in {"CANDIDATE", "ROLLED_BACK"}:
            raise ValueError(f"candidate in status {row.status} cannot be approved")

        now = datetime.now(UTC)
        pointer = self.session.get(ActiveFactorGenome, SCOPE)
        previous = pointer.candidate_id if pointer else None
        if previous and previous != candidate_id:
            previous_row = self.session.get(FactorGenomeVersion, previous)
            if previous_row is not None:
                previous_row.status = "ROLLED_BACK"

        row.status = "ACTIVE"
        row.reviewed_by = user_id
        row.reviewed_at = now
        row.review_note = note
        if pointer is None:
            self.session.add(
                ActiveFactorGenome(
                    scope=SCOPE,
                    candidate_id=candidate_id,
                    previous_candidate_id=previous,
                    activated_by=user_id,
                    activated_at=now,
                )
            )
        else:
            pointer.previous_candidate_id = previous
            pointer.candidate_id = candidate_id
            pointer.activated_by = user_id
            pointer.activated_at = now
        self.session.flush()
        return self.active_version()

    def reject(
        self, candidate_id: str, *, user_id: str | None, note: str | None = None
    ) -> dict[str, object]:
        """Reject a pending candidate without touching the active version."""

        row = self.session.get(FactorGenomeVersion, candidate_id)
        if row is None:
            raise ValueError("factor genome candidate not found")
        if row.status == "ACTIVE":
            raise ValueError("the active factor genome cannot be rejected; roll it back instead")
        row.status = "REJECTED"
        row.reviewed_by = user_id
        row.reviewed_at = datetime.now(UTC)
        row.review_note = note
        self.session.flush()
        return self._describe(row)

    def rollback(self, *, user_id: str | None, note: str | None = None) -> dict[str, object]:
        """Restore the version that the active one replaced."""

        pointer = self.session.get(ActiveFactorGenome, SCOPE)
        if pointer is None:
            raise ValueError("no active factor genome version to roll back")
        # The first approved candidate replaced the built-in frozen default,
        # which has no database row.  Removing the pointer restores that
        # default without rewriting any historical candidate.
        if not pointer.previous_candidate_id:
            current = self.session.get(FactorGenomeVersion, pointer.candidate_id)
            if current is not None:
                current.status = "ROLLED_BACK"
            self.session.delete(pointer)
            self.session.flush()
            return self.active_version()
        target = self.session.get(FactorGenomeVersion, pointer.previous_candidate_id)
        if target is None:
            raise ValueError("previous factor genome version is missing")
        current = self.session.get(FactorGenomeVersion, pointer.candidate_id)
        if current is not None:
            current.status = "ROLLED_BACK"
        target.status = "ACTIVE"
        target.reviewed_by = user_id
        target.reviewed_at = datetime.now(UTC)
        target.review_note = note
        pointer.previous_candidate_id = None
        pointer.candidate_id = target.candidate_id
        pointer.activated_by = user_id
        pointer.activated_at = datetime.now(UTC)
        self.session.flush()
        return self.active_version()

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _describe(row: FactorGenomeVersion) -> dict[str, object]:
        return {
            "candidate_id": row.candidate_id,
            "status": row.status,
            "source": row.source,
            "formula_version": row.formula_version,
            "genome": row.genome,
            "parameter_sha256": row.parameter_sha256,
            "metrics": row.metrics,
            "dataset_sha256": row.dataset_sha256,
            "proposal_sha256": row.proposal_sha256,
            "hypothesis": row.hypothesis,
            "risk_notes": row.risk_notes,
            "explanation": row.explanation,
            "decision_at": row.decision_at.isoformat(),
            "created_at": row.created_at.isoformat(),
            "reviewed_by": row.reviewed_by,
            "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
            "review_note": row.review_note,
        }
