# Releasing

This project is still small enough that a short manual checklist is more useful than release automation.

## Before publishing

1. Make sure the repository name and the URLs in `pyproject.toml` match.
2. Run the unit tests:

   ```bash
   PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
   ```

3. Build the wheel:

   ```bash
   python3 -m pip wheel . --no-deps
   ```

4. Read the README once from the top as if you were a new user.
5. Check that the changelog entry matches the actual surface area of the release.
6. Clean generated artifacts from the checkout:

   ```bash
   rm -rf build dist src/*.egg-info src/agent_keepalive.egg-info src/agent_keepalive/__pycache__ tests/__pycache__
   ```

## First public push

1. Create the repository as `agent-keepalive`.
2. Push the main branch.
3. Confirm that GitHub Actions runs on the first push.
4. Check that the issue templates render correctly.

## Normal release flow

1. Bump the version in:
   - `pyproject.toml`
   - `src/agent_keepalive/__init__.py`
2. Add a changelog entry.
3. Run tests and build the wheel.
4. Commit the release change.
5. Tag the release.
6. Push the branch and tag.

## Notes

- Commit authorship should use the real human or bot identity that owns the work.
- `agent-keepalive` is the only supported public CLI name.
- For now, keep the release process boring and explicit.
