# Chapter 02 — The Key Registry

Offline study of the registration rules in one pinned draft of EIP-8450. This is a
small research project: the goal is that the claims below can be checked by
re-running the commands, not that the draft has been verified.

## What is pinned

| Item | Value |
|---|---|
| Draft | EIP-8450, commit `a8a412e4a9355605bbe04fd01e8b2a03f399fb47` (open and unmerged when captured) |
| Consensus implementation | pull request 5782, commit `c031b137b7f6620679fdb1e9bd7d9a22470bc0e2` |

Later changes to the draft are not followed automatically.

## What each layer shows, kept separate

| Layer | Files | Shows | Does not show |
|---|---|---|---|
| Rule model | `registry.py`, `test_registry.py` | the seven acceptance conditions, their order, and that a refusal never changes state | anything cryptographic |
| Real signatures | `crypto_real.py`, `test_crypto.py` | upstream leanXMSS and BLS accept one genuine registration and refuse four forgeries; key and signature sizes match the declared types | the draft as a whole, or any security property |
| Bounded sweep | `sweep.py`, `oracle.py` | no disagreement with a separately written statement of the rules across 3,591 comparisons in a small domain | absence of disagreement outside that domain |
| Recorded model round | `round_replay.json`, `replay_round.py` | eight sequences proposed by one model call, replayed offline with full agreement over 32 comparisons | a finding about the draft: comparisons cover acceptance, reason and first failing position only |
| Web trace | `timeline.py`, `timeline.json`, `build_page.py` | the nine rows on the published page come from the rule model | the model round above; the two are reported separately |

## Run it

Offline, any Python 3.9 or newer:

```
python3 -m unittest discover -s experiments/eip8450 -p 'test_*.py'
python3 experiments/eip8450/replay_round.py
```

Expected: 35 rule tests pass, 3 real-signature tests report as skipped because the
pinned libraries are absent, and the replay prints 32 comparisons with 0
disagreements.

Optional, real signatures. The pinned leanXMSS package publishes wheels for
Python 3.11 only:

```
python3.11 -m venv /tmp/chapter02 && . /tmp/chapter02/bin/activate
pip install py-lean-pq==0.7.0 eth-consensus-specs==1.7.0a13 ckzg
python3 -m unittest discover -s experiments/eip8450 -p 'test_*.py'
```

Then 8 real-signature tests run instead of skipping.

## What stays out of this tree

The model-service adapter, the prompts, the raw provider replies and the cost
record. They name a paid service and carry spend, so they are not part of the
published evidence. The sequences they produced are reproduced here in redacted,
action-name form so the reported comparison is still checkable.

## Known limits

- Two validators, seven actions, sequence length up to three: a bounded domain, not full protocol validation.
- The written statement and the rule model were authored by the same person; agreement is a consistency check, not an independent audit.
- One guardian reply agreed with a fix that its own prompt had already disclosed, so it is corroboration and not independent review.
- The upstream feature specification body was not captured locally, so rule ordering is confirmed against the draft text only.
- Signer leaf management, VRF openings, gossip, per-block limits, parent execution requests, chain reorganisation and the fork upgrade are not modelled.
