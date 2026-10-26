# Contributing

There are two ways to contribute: through the research itself, or through the
code.

## Research contributions

The live service at https://evorph.net accepts public challenge submissions.
A submission is a migration scenario — a sequence of delivered, mutated,
duplicated, and advanced messages — aimed at a published candidate version.
The fixed verifier judges every submission against the reference
implementation; a confirmed divergence enters the public archive under your
contributor alias and can become part of later evaluation sets. Submissions
are idempotent per request key, and their status is queryable. No account and
no payment are involved.

## Code contributions

1. Branch from `main`.
2. Backend: `cd backend/tests && PYTHONPATH=..:. python3 -m unittest discover -p 'test_*.py' -v`.
   This requires OpenSSL 3.5 or newer with ML-DSA support on `PATH`, or named
   by `EVORPH_OPENSSL`. Frontend: `npm run check` and `npm test` in `frontend/`.
   Continuous integration runs both suites on every push.
3. Keep the public tree clean: no personal data, no local absolute paths, no
   credentials, no generated state files.
4. Describe what the change does and why, and name the verification record it
   affects, if any.

Documentation claims in VERIFICATION.md are dated records: correct them with a
new dated entry rather than rewriting an old one.
