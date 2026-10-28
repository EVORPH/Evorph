# Changelog

Dated in UTC. Entries cover the public repository and the deployed service at
https://evorph.net. Detailed evidence lives in VERIFICATION.md.

## 2026-10-28

- Preserved official-vector bytes across automatic line-ending conversion and
  added a regression test that checks all four files after a real Git checkout.
- Documented the POSIX runtime requirement and the recommended WSL2 route;
  native Windows Python is not supported.
- Added four bounded API workers with separate SQLite connections, a bounded
  queue and explicit overload responses; added credential-free access events.
  These backend changes require deployment before affecting the live service.
- Added a Caddy configuration sample with header/body deadlines and size limits;
  the sample is not automatically installed on a running host.
- Clarified automatic publication versus verified results, the first round's
  missing evidence links, and the absence of an independent archive witness.
- Opened the repository's issue tracker; vulnerability reports remain private.
- Published the supplied contract address in all three site footers with a copy
  action, and synchronized that already-deployed static change into this tree.

## 2026-10-26

- Pinned the strategy parent as a wire constant in the online seeker request;
  verified by a completed live round
  (`experiment-09b2e9fa2e724e8f9215b963cf40414d`).
- Fixed backup manifests to store slash-separated names so a backup restores
  under any supported operating system.
- Stated the official-vector applicability scope (15 of 180, 3 expected
  passes) wherever the published test vectors are described; corrected the
  recorded test counts and re-ran the full backend suite (216 tests).
- Added SECURITY.md, CONTRIBUTING.md, and this changelog; filled in the
  LICENSE copyright line.
- Enabled continuous integration: every push runs the backend suites (against
  a self-built OpenSSL with ML-DSA support) and the frontend checks and tests;
  the first green run completed on 2026-10-26.
- Updated the site status copy so it no longer names a round count that would
  go stale.

## 2026-10-25

- Deployed the service; the site and the public archive went online behind TLS.
- First completed live round (`experiment-0c3398530aa24d999aad97869c628417`).
- Four correctness and trust fixes with a full backend re-run (215 tests at
  that time).
- Two live independent-scenario rounds stopped at the strategy parent guard
  and were published as failed experiments
  (`experiment-c0e4b536f7114c0594ed7936c7e46bce`,
  `experiment-0565d526bc1b4345afbed73b2cd0ebf3`).

## 2026-10-24

- Initial public release: site, backend, deployment files, and the recorded
  verification suite.
