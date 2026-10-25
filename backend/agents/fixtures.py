"""Recorded provider outcomes for offline channel verification."""

from evorph.contracts import InputError, canonical, digest

from .channel import (RESPONSE_SCHEMA, TransportDisconnected, TransportRateLimited, TransportTimeout)


def usage(input_tokens, output_tokens, reasoning_tokens=0):
    return {'input_tokens': input_tokens, 'output_tokens': output_tokens, 'reasoning_tokens': reasoning_tokens}


def response(provider, model_id, payload=None, reported=None, stop_reason='complete', refusal=False,
             schema=RESPONSE_SCHEMA, without_usage_field=False):
    envelope = {'schema_version': schema, 'provider': provider, 'model_id': model_id,
                'stop_reason': stop_reason, 'refusal': refusal, 'payload': payload, 'usage': reported}
    if without_usage_field:
        del envelope['usage']
    return canonical(envelope)


def seeker_payload(plan, statement='Synthetic seeker statement.'):
    return {'plan': plan, 'statement': statement}


def keeper_payload(policy, statement='Synthetic keeper statement.'):
    return {'policy': policy, 'statement': statement}


def reply(raw):
    return {'kind': 'response', 'raw': raw}


def limited(confirmed_no_usage=True):
    return {'kind': 'rate_limited', 'confirmed_no_usage': confirmed_no_usage}


def timed_out():
    return {'kind': 'timeout'}


def disconnected():
    return {'kind': 'disconnected'}


def synthetic_meter(raw):
    """Fixture units are bytes, not a claim about any production tokenizer."""
    return {'tokens': len(raw), 'basis': 'synthetic-byte-meter.v1', 'request_digest': digest(raw)}


class ScriptedTransport:
    """Replays recorded outcomes in order; it holds no credentials and opens no connection."""

    def __init__(self, script=()):
        self.script = list(script)
        self.sent = []

    def send(self, raw, timeout_seconds):
        self.sent.append({'raw': raw, 'timeout_seconds': timeout_seconds})
        if not self.script:
            raise InputError('No recorded outcome remains for this request.')
        step = self.script.pop(0)
        kind = step['kind']
        if kind == 'response':
            return step['raw']
        if kind == 'rate_limited':
            raise TransportRateLimited(step['confirmed_no_usage'])
        if kind == 'timeout':
            raise TransportTimeout()
        if kind == 'disconnected':
            raise TransportDisconnected()
        raise InputError('Unknown recorded outcome.')

    @property
    def sends(self):
        return len(self.sent)
