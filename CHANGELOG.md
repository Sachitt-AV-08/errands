# Changelog

All notable changes to Errands are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- CI matrix (ubuntu/windows × Python 3.10-3.13) with lint, test, coverage gate
- Coverage gate at 89% (measured 90.18% locally)
- `install.ps1` / `install.sh` one-line installers pulling latest GitHub Release
- Release workflow with trusted PyPI publishing, checksums, smoke install
- This CHANGELOG

### Changed
- README stats now measured (94 tests, 7 test files, ~1.5k source lines, 91% coverage gate)
- Ruff auto-fixes applied across codebase (import sorting, collections.abc, zip strict, line length, unused imports)

### Fixed
- Line-too-long in core.py, orvima_bridge.py, recipes.py, test_errands.py, test_orvima_integration.py
- Unused loop variable in test_three_project_e2e.py (`_project`)
- Removed unused imports across source and tests

## [0.1.0] - 2024-XX-XX

### Added
- Initial release: narrow goals run as fixed, verified recipe sequences
- Recipe system: JSON recipes with triggers, preconditions, steps, verification
- Three gates: preflight, Sentinel (plan-aware), verify (after every step)
- `ErrandRunner` with `match()` goal-to-recipe resolution
- Plan context: every step classified with errand name, goal, step intent, next step
- Refusals: `no_match`, `ambiguous`, `precondition_failed`, `drift`, `unsafe`, `verification_failed`
- `sentinel_bridge.py` (optional Sentinel integration) and `orvima_bridge.py` (orvima adapter)
- Core runs without Sentinel/orvima — steps run, nothing gated
- Two shipped recipes: `place_order`, `cancel_subscription`
- 94 tests: end-to-end, recipe selector resolution, shop server fixture, real-browser contract
- Real-browser test against scripted shop (`test_three_project_e2e.py`)
- Deterministic layer generalises to plan text; classifier stays ADVISORY (AUC 0.5378 = coin flip)