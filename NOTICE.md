# Notices

Third-party material shipped in this repository, and where its license text lives.

## NIST ACVP test vectors

`backend/vectors/` contains unmodified files from the NIST Automated Cryptographic Validation Program Gen/Vals code, published by the National Institute of Standards and Technology.

- Upstream repository: `usnistgov/ACVP-Server`
- Upstream commit: `975de31eb83d87039ec88934fdc47d8c312b892d`
- Vector set: `gen-val/json-files/ML-DSA-sigVer-FIPS204`
- Files: `prompt.json` (3,125,947 bytes), `expectedResults.json` (13,956 bytes), `registration.json` (914 bytes)
- `UPSTREAM_NOTICE.txt` is the upstream `README.md`, preserved byte for byte because the NIST license requires the entire notice to stay intact.
- `manifest.json` records the upstream path, sha256, and size of every file so the copy can be re-verified.

NIST-developed software is provided by NIST as a public service and is expressly provided "AS IS", without warranty of any kind. The full notice, including the disclaimer and the acknowledgment requirement, is in `backend/vectors/UPSTREAM_NOTICE.txt`. Nothing in this repository is endorsed by NIST, and these vectors are used for signature-verification testing only.

## three.js

`frontend/dist/assets/vendor/three.module.js` and `three.core.js` are three.js, release `0.186.1`, MIT licensed, copyright the three.js authors. The license text is at `frontend/dist/assets/vendor/THREE-LICENSE.txt`. The project's own procedural sculptures are original work built on top of it.

## Fonts

Self-hosted in `frontend/dist/assets/fonts/`, both under the SIL Open Font License 1.1:

- Sora (`sora-variable.ttf`), license text `sora-OFL.txt`
- IBM Plex Mono (`ibm-plex-mono-400.ttf`, `ibm-plex-mono-500.ttf`), license text `ibmplexmono-OFL.txt`

## Photograph

`frontend/dist/assets/research/quantum-hardware.jpg` — cryogenic wiring in the Advanced Quantum Testbed dilution refrigerator at Berkeley Lab, from the U.S. Quantum image gallery at https://www.quantum.gov/quantum-image-gallery/. It is used as context on the homepage with a visible credit and a link to the source. It is not this project's hardware, and no result produced here was measured on it.

## Visual references

Two public sites were looked at for composition and motion only: https://precedent.markets/ and https://www.predictouts.xyz/. Their text, artwork, and code were not copied. Award bodies that informed the quality bar are references for what the work was aiming at, not awards, affiliations, or endorsements received by this project.
