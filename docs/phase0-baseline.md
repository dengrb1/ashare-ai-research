# Phase 0: Baseline Assessment

**Branch**: `codex/fusion-research-only`  
**Date**: 2026-08-26  
**Repository**: F:\code\ashare-ai-src

## Executive Summary

The ashare-ai-src repository is already a **pure research system** with no live trading capabilities. The codebase contains:
- ✅ Research, scoring, backtesting, and portfolio simulation
- ✅ Paper trading plans (analysis only, no execution)
- ✅ PostgreSQL, Redis, Celery worker infrastructure
- ✅ FastAPI REST API with authentication, CSRF, rate limiting
- ✅ React web frontend
- ❌ No QMT integration
- ❌ No xtquant imports
- ❌ No order submission endpoints
- ❌ No live trading execution
- ❌ No broker connections

## Architecture Status

### Current Stack
- **API**: FastAPI on port 8000 (127.0.0.1)
- **Web**: Nginx on port 80 (127.0.0.1)
- **Database**: PostgreSQL (in compose.yaml)
- **Cache/Queue**: Redis (in compose.yaml)
- **Workers**: 
  - `job-worker` (serial, 700MB limit)
  - Optional `research-worker` (DUAL profile)
  - Optional `backtest-worker` (parallel-workers profile)
  - Optional `worker` (parallel-workers profile)

### Resource Limits (compose.yaml)
- API: 384MB
- Web: 32MB
- Job Worker: 700MB
- PostgreSQL: Not explicitly limited
- Redis: Not explicitly limited

### Low-Memory Strategy
All Python services use:
```yaml
MALLOC_ARENA_MAX: "2"
OPENBLAS_NUM_THREADS: "1"
OMP_NUM_THREADS: "1"
MKL_NUM_THREADS: "1"
NUMEXPR_NUM_THREADS: "1"
ARROW_IO_THREADS: "1"
```

## Code Verification

### QMT/Trading References
```bash
$ grep -r "xtquant\|MiniQMT\|QMT" src/ashare_ai --include="*.py"
# (no results)
```

### Order Execution Endpoints
```bash
$ grep -rn "submit.*order\|place.*order\|execute.*trade" src/ashare_ai/api/
# (no results)
```

### Trading-Related Code Found
- `src/ashare_ai/trading/default_rules.py` - Trading rules (paper only)
- `src/ashare_ai/trading/sellability.py` - Position analysis (paper only)
- Trade plan generation (`/api/v1/reports/{report_id}/trade-plans`) - **Analysis only, no execution**

### Research Capabilities Present
- Research jobs (`/api/v1/research/runs`)
- Scoring (`ScoreRow` model)
- Backtesting (`BacktestRun` model)
- Portfolio simulation (`PortfolioRow` model)
- Reports (`ReportRow` model)
- Exit advice (paper simulation, `ExitAdviceRow`)
- Trade plan optimization (paper simulation, `TradePlanRow`)

## Configuration Review

### Current config.py Settings
- ✅ Database: Defaults to SQLite, production expects PostgreSQL
- ✅ Redis: Defaults to localhost:6379
- ✅ Authentication required
- ✅ CSRF protection
- ✅ Rate limiting
- ✅ Production security validation
- ❌ No `research_only_mode` flag yet
- ❌ No `qmt_enabled` flag yet
- ❌ No `execution_mode` flag yet

### Health Endpoint
Current `/api/v1/health` returns:
```json
{
  "status": "ok",
  "version": "...",
  "database": "ok",
  "git_sha": "..."
}
```

Missing fields:
- `qmt_enabled`
- `auto_trading_enabled`
- `execution_mode`

## QMT Repository Inventory

Located at: `F:\code\qmt`

### Components to Migrate (Phase 2)
1. **gateway/** - Rust model gateway (Cargo.toml, src/, config.toml)
2. **tools/console/** - Windows desktop console
3. **tools/llama.cpp/** - Local model runtime
4. **tools/news_bridge/** - News data bridge
5. **scripts/**:
   - `service_control.ps1` - Service lifecycle management
   - `start_stack.ps1`, `stop_stack.ps1` - Stack control
   - `llm_stack.ps1` - Model stack launcher
   - `build_offline_package.ps1` - Packaging
   - `install_package.ps1` - Installer
   - `self_check.ps1` - Health validation
   - `verify_stack.ps1` - Stack verification

### Components NOT to Migrate
- Any MiniQMT references
- Any xtquant imports
- Broker adapters
- Account connection code
- Order submission modules
- Real trading configuration

## Testing Baseline

### Current Test Status
```bash
$ cd F:\code\ashare-ai-src
$ .\.venv\Scripts\python.exe -m pytest -q
# (baseline run needed)
```

### Web Build Status
```bash
$ cd web
$ npm test -- --run
$ npm run build
# (baseline run needed)
```

### Compose Validation
```bash
$ docker compose config
# (should validate without errors)
```

## Memory Status

From memory context:
- User's production is Docker compose deployment
- Native Windows runtime exists at F:\Progress\AshareAI\runtime (secondary)
- Docker deployment uses PostgreSQL:5432, Redis, Web:80, API:8000
- Known issue: Redis AOF corruption recurs
- Workers hang without connect_timeout
- Parquet hash test fails (pre-existing, not regression)

## Next Steps for Phase 1

1. ✅ Add research-only configuration flags to `config.py`
2. ✅ Update health endpoint to return execution mode
3. ✅ Update `HealthResponse` schema
4. ⏳ Add validation to reject any future trading configuration
5. ⏳ Add startup check that refuses to run if trading is enabled
6. ⏳ Create tests for research-only enforcement
7. ⏳ Document frontend expectations
8. ⏳ Remove any trading UI elements from frontend

## Risk Assessment

**Low Risk**: The system already has no trading execution. Phase 1 adds explicit guards to ensure trading can never be accidentally enabled.

**Medium Risk**: QMT migration (Phase 2) requires careful separation of model/gateway components from trading logic.

**Migration Strategy**: Preserve all existing research, scoring, backtest, and portfolio functionality while adding explicit "research-only" boundaries.
