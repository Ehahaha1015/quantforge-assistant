from .models import FindingClassification, ReviewFinding, TriageResult

BLOCKING = frozenset(
    {
        FindingClassification.CONTRACT_VIOLATION,
        FindingClassification.CORRECTNESS_DEFECT,
        FindingClassification.ARCHITECTURE_VIOLATION,
    }
)


def triage(
    findings: list[ReviewFinding] | tuple[ReviewFinding, ...],
    confirmations: set[str] | None = None,
) -> TriageResult:
    confirmed = confirmations or set()
    blockers: list[ReviewFinding] = []
    unconfirmed: list[ReviewFinding] = []
    deferred: list[ReviewFinding] = []
    for finding in findings:
        if finding.classification not in BLOCKING:
            deferred.append(finding)
        elif finding.confirmed or finding.id in confirmed:
            blockers.append(finding)
        else:
            unconfirmed.append(finding)
    return TriageResult(tuple(blockers), tuple(unconfirmed), tuple(deferred))
