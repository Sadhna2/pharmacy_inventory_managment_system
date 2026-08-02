"""Read-only anomaly endpoints.

Findings are computed on request rather than stored. That is deliberate: an
alerts table needs an acknowledgement workflow, and an acknowledgement workflow
needs someone to maintain it. Recomputing means a finding disappears the moment
the ledger stops supporting it — including when a bad movement is reversed.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.ai.anomaly import service
from app.ai.anomaly.schemas import AnomalyOut, AnomalyReportOut, AnomalySummaryOut
from app.core.deps import require_feature, require_permission
from app.db.session import get_db
from app.models.identity import User

router = APIRouter(prefix="/ai/anomalies", tags=["AI · anomalies"])

VIEW = require_permission("ai.view")
LIVE = require_feature("features.anomaly")


@router.get("", response_model=AnomalyReportOut)
def list_anomalies(
    lookback_days: int = Query(90, ge=7, le=730),
    kind: list[str] | None = Query(
        None, description="Repeatable. Omit for every detector."
    ),
    warehouse_id: int | None = None,
    min_severity: str = Query("low", pattern="^(low|medium|high)$"),
    db: Session = Depends(get_db),
    _: User = Depends(VIEW),
    __: None = Depends(LIVE),
) -> AnomalyReportOut:
    """Everything worth a second look, most serious first."""
    findings = service.detect(
        db,
        lookback_days=lookback_days,
        kinds=kind,
        warehouse_id=warehouse_id,
        min_severity=min_severity,
    )
    return AnomalyReportOut(
        lookback_days=lookback_days,
        summary=AnomalySummaryOut(**service.summarise(findings)),
        anomalies=[
            AnomalyOut(
                key=f.key,
                kind=f.kind,
                severity=f.severity,
                occurred_at=f.occurred_at,
                product_id=f.product_id,
                product_name=f.product_name,
                sku=f.sku,
                warehouse_id=f.warehouse_id,
                warehouse_name=f.warehouse_name,
                quantity=f.quantity,
                value=f.value,
                score=f.score,
                explanation=f.explanation,
                baseline=f.baseline,
                movement_ids=f.movement_ids,
            )
            # Capped: past a couple of hundred findings the list has stopped
            # being a to-do list and become a second ledger. Tighten the
            # severity filter instead.
            for f in findings[:200]
        ],
    )
