from src.orchestrator.unified_engine import UnifiedSignalBatch, _explain_no_entry


def test_entry_diagnostics_field_exists():
    batch = UnifiedSignalBatch()
    assert batch.entry_diagnostics == {}


def test_retreating_sector_has_highest_priority():
    reason = _explain_no_entry(
        market_mode='retreat',
        sector_status='retreating',
        tech_data={
            'tech_signals': {'vote_score': 3},
            'institutional_holding': {'vote_score': -2},
        },
    )
    assert '板块退潮' in reason
    assert '机构 -2' in reason


def test_positive_tech_without_strategy_trigger_is_explicit():
    reason = _explain_no_entry(
        market_mode='defend',
        sector_status='main_trend',
        tech_data={
            'tech_signals': {'vote_score': 1},
            'institutional_holding': {'vote_score': 0},
        },
    )
    assert '四种入场策略均未达到触发阈值' in reason
    assert '模式:防守' in reason
    assert '板块:主线' in reason


def test_strategy_blockers_are_listed():
    reason = _explain_no_entry(
        market_mode='defend',
        sector_status='main_trend',
        tech_data={
            'tech_signals': {'vote_score': 1},
            'institutional_holding': {'vote_score': 0},
            'current_price': 10.0,
            'ma20': 9.0,
            'recent_high': 11.0,
            'volume_ratio': 1.0,
            'ma25': 9.0,
            'today_volume': 100,
            'volume_ma60': 200,
            'change_pct': 1.0,
        },
    )

    assert '策略检查:' in reason
    assert '恐慌抄底: 正常行情，未触发' in reason
    assert '套利低吸: 周线MACD未向上' in reason
    assert '确认追强: 仅进攻模式启用' in reason
    assert '价量突破: K线不足60日(实际0条)，暂不判断' in reason
