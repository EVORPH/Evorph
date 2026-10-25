"""Input bounds for live channels.

A bound is a reservation ceiling taken from the provider's published context
capacity. It is deliberately not a tokenizer estimate: this project stays on the
standard library, so no local tokenizer can be proven to match the provider's.
"""

from evorph.contracts import InputError, digest, integer

CONTEXT_BOUND_BASIS = 'provider-context-bound.v1'


def context_bound_meter(context_tokens, basis=CONTEXT_BOUND_BASIS):
    integer(context_tokens, 1, 10000000)
    if type(basis) is not str or not basis or len(basis) > 120:
        raise InputError('Meter basis is required.')

    def meter(raw):
        if type(raw) is not bytes or not raw:
            raise InputError('Metering requires the exact wire request bytes.')
        return {'tokens': context_tokens, 'basis': basis, 'request_digest': digest(raw)}

    return meter
