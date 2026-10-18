# Boundary Tests

This directory contains **boundary tests** that enforce QuantForge's architectural invariants (INV-1 through INV-12).

These tests are **security-critical** and must pass on every commit. Failure indicates a violation of core architectural principles.

## Purpose

Boundary tests verify that:
1. Architectural boundaries cannot be violated
2. Invariants are machine-enforced, not just documented
3. Safety mechanisms fail closed, not open

## Test Files

### `test_inv2_no_llm_sdk.py` - INV-2 Enforcement

**Invariant**: Product package must never import LLM SDKs.

**Purpose**: Enforce ADR-001 AI/product boundary.

**Tests**:
- `test_inv2_no_llm_sdk_imports` - Primary enforcement test (AST tripwire)
- `test_inv2_http_client_is_stdlib_only` - Verify http.client limited to approved modules
- `test_inv2_smoke_test_violations` - Verify test mechanism works
- `test_inv2_each_forbidden_import_is_blocked` - Parameterized test for each forbidden SDK

**Failure mode**: Fail closed - any forbidden import causes test failure.

**If this test fails**:
1. Remove the forbidden import from product package
2. Move LLM-related code to `qf-lab/` (external intelligence layer)
3. Re-run test to verify compliance

### `test_credential_boundary.py` - Credential Boundary Integrity

**Invariant**: Credentials must only be acquired through CredentialBoundary (S0-S13 protocol).

**Purpose**: Enforce credential acquisition authority.

**Tests**:
- `test_boundary_rejects_no_backend` - Fail closed on missing backend
- `test_boundary_uses_rejecting_backend_by_default` - Default fail-closed behavior
- `test_inv_credential_boundary_integrity` - S0-S13 sequence integrity
- `test_inv_credential_boundary_is_sole_authority` - Boundary is sole acquisition path

**Failure mode**: Fail closed - invalid credentials acquisition rejected.

**Complements**: Existing `tests/credentials/test_credentials_static_enforcement.py`

### `test_oos_no_overlap.py` - OOS Seal Enforcement

**Invariant**: Out-of-sample seals prevent test data re-use and overlap.

**Purpose**: Enforce statistical validity of research experiments.

**Tests**:
- `test_inv6_oos_seal_required_for_evidence_pack` - INV-6 enforcement
- `test_oos_seal_prevents_overlap` - Temporal overlap detection
- `test_oos_seal_is_immutable_once_spent` - Seal immutability
- `test_oos_seal_enforcement_exists_in_codebase` - Meta-test
- `test_oos_seal_enforces_fail_closed_on_invalid_inputs` - Fail-closed behavior

**Failure mode**: Fail closed - invalid OOS seal prevents experiment execution.

**Complements**: Existing `tests/research/oos/test_*.py`

## Running Boundary Tests

```bash
# Run all boundary tests
uv run pytest tests/boundaries/ -v

# Run specific boundary test
uv run pytest tests/boundaries/test_inv2_no_llm_sdk.py -v

# Run in CI (fast fail on boundary violation)
uv run pytest tests/boundaries/ -x --tb=short
```

## CI Integration

Boundary tests **must** run on every commit and pull request. They are included in `.github/workflows/test.yml`.

**CI behavior**:
- ✅ All pass → CI succeeds
- ❌ Any fail → CI fails immediately (fail-fast)

## Adding New Boundary Tests

When adding a new invariant (INV-X):

1. Create `test_invX_description.py` in this directory
2. Write tests that:
   - Verify the invariant cannot be violated
   - Test fail-closed behavior
   - Include smoke tests to verify test mechanism
3. Document the invariant in module docstring
4. Add to this README
5. Ensure test runs in CI

## Relationship to Other Tests

**Boundary tests** are distinct from:
- **Unit tests** (`tests/unit/`): Test individual functions in isolation
- **Integration tests** (`tests/integration/`): Test multi-component interactions
- **Property tests** (`tests/properties/`): Test mathematical properties (Hypothesis-based)

**Boundary tests**:
- Enforce architectural invariants
- Are security-critical
- Fail closed by design
- Run on every commit
- Block merge if failing

## Architecture References

- `docs/architecture/ARCHITECTURE.md` - Overall architecture
- `docs/architecture/ADR-001-ai-layer-boundary.md` - AI/product boundary decision
- Invariants INV-1 through INV-12 are defined in ARCHITECTURE.md §5

## Maintenance

**These tests should rarely change.** If a boundary test needs modification:

1. Understand why the invariant changed
2. Update architectural documentation first
3. Get architectural review
4. Update boundary test
5. Verify all downstream code still complies

**Do not weaken boundary tests to make code pass.** Fix the code to comply with the invariant.
