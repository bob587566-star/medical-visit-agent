"""Shared workflow priority, distinct from a clinical urgency classification."""
from .schemas import Risk


def requires_human_support(risk: Risk) -> bool:
    return risk.level == 'emergency' or risk.kind == 'self_harm'


def support_status(risk: Risk) -> str:
    return 'emergency' if risk.level == 'emergency' else 'support_required'


def risk_order(risk: Risk) -> tuple[int, bool]:
    # Never downgrade an existing medical emergency; prioritize crisis support
    # among otherwise equally urgent results.
    return ({'unknown': 0, 'prompt': 1, 'emergency': 2}[risk.level], risk.kind == 'self_harm')
