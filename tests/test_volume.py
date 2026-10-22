from pointtable.core.volume import (
    Activity,
    BreakQualifier,
    active_or_passive,
    break_qualifier,
)


def test_active_volume_break_pushes_now():
    decision = break_qualifier(volume_ratio=1.5, active_delta=1000.0)
    assert decision.qualifier is BreakQualifier.EXECUTE_NOW


def test_passive_break_waits_for_close():
    decision = break_qualifier(volume_ratio=0.8, active_delta=0.0)
    assert decision.qualifier is BreakQualifier.DELAY_OBSERVE


def test_divergence_and_limit_up_are_explicit_paths():
    assert break_qualifier(2.0, 100.0, divergence=True).qualifier is BreakQualifier.DIVERGENCE
    assert break_qualifier(0.5, 0.0, limit_up=True).qualifier is BreakQualifier.EXECUTE_NOW


def test_two_level_context_check():
    assert active_or_passive(True, False, False) is Activity.ACTIVE
    assert active_or_passive(True, True, False) is Activity.PASSIVE
    assert active_or_passive(True, False, True) is Activity.PASSIVE
