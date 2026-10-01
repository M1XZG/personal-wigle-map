# Contributing

Bug reports, documentation fixes and focused feature proposals are welcome.
This project handles sensitive wireless-survey and location data, so privacy
matters as much as code quality.

## Before opening an issue

Use the issue form that best matches the request:

- A bug in the app, API or deployment belongs in a bug report.
- A supported file that imports incorrectly belongs in an import report.
- A new capability belongs in a feature request.
- Setup questions and documentation gaps have their own forms.

Search existing issues first. For security vulnerabilities, follow
[SECURITY.md](SECURITY.md) and use private vulnerability reporting.

Never post real WiGLE exports, runtime databases, credentials, SSIDs, BSSIDs,
survey tracks or precise coordinates. Reproduce data problems with invented
values or a small synthetic file.

## Making a change

1. Fork the repository and create a branch from `main`.
2. Keep the change focused. Open an issue first for larger design changes or
   new import formats.
3. Add or update tests for changed behaviour.
4. Update documentation when configuration, deployment or user-facing
   behaviour changes.
5. Open a pull request and complete the checklist.

Set up the development environment with:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The FastAPI backend is in `app/`, the dependency-free browser interface is in
`web/`, and tests are in `tests/`. Match the existing style and avoid adding a
new dependency when the standard library or current stack already covers the
need.

## Testing expectations

Run the complete test suite before submitting:

```bash
python -m pytest -q
```

Tests must use synthetic data. A test fixture should be safe to publish and
must not contain copied survey records or real network identifiers.

## Review

Pull requests are reviewed for correctness, privacy, upgrade impact and
maintainability. A maintainer may ask for a smaller change or more evidence
before accepting a new feature. Discussion is welcome; acceptance is not
guaranteed.
